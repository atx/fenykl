/*
 * bchklog: battery A/D probe. Once a second the plugin task reads the battery voltage (A/D channel
 * 4) and the thermistor (channel 0x0B) with bchk_ad_read, holding the battery-check semaphore as
 * every firmware battery-check command does, and logs them with the battery-check RAM state.
 * Like the firmware's own 1 s source check it reads only while that check is enabled (not in
 * standby). Log only: no hooks, no writes to firmware data.
 */
#include "log.h"

/* firmware data, v1.6 */
#define BCHKLOG_SEM 5			/* SBCHKCORE */
#define BCHKLOG_CH_VBAT 4u
#define BCHKLOG_CH_TMP 0x0Bu
#define BCHKLOG_SAMPLES 0x6055F4A8u	/* u16[5] of the last conversion, either channel */
#define BCHKLOG_MODE 0x6055F4B4u	/* u8 0 end, 1 rec, 2 play, 4 pc, 5 movie */
#define BCHKLOG_LEVEL 0x6055F4B6u	/* u16 100, 75, 50, 1 empty, 0 unset */
#define BCHKLOG_SRC 0x6055F4B8u		/* u16 0x100 AC / DC coupler, 0x200 battery */
#define BCHKLOG_SRC_ON 0x6055F4BAu	/* u8 1 s source check enabled (0 in standby) */
#define BCHKLOG_PRESENT 0x6055F4BBu	/* u8 bit 0 thermistor present */
#define BCHKLOG_TMP 0x6055F4C0u		/* u16 last thermistor value, 0xF0 none */
#define BCHKLOG_GUI 0x60702278u		/* u8 GUI super state, u8 state */

#define BCHKLOG_NSAMPLES 5u
#define BCHKLOG_SEM_TICKS 500		/* 1 ms ticks */
#define BCHKLOG_PERIOD_US 1000000u
#define BCHKLOG_RUN_S (20u * 60u)
#define BCHKLOG_RAW_S 60u
#define BCHKLOG_SUM_S 30u
#define BCHKLOG_FLUSH_MS 1000u
#define BCHKLOG_FLUSH_TRIES 50u
#define BCHKLOG_LINE 128u

/* w field of the s line */
#define BCHKLOG_STANDBY 1

struct bchklog_read {
	int w;				/* 0 read, BCHKLOG_STANDBY, else the semaphore result */
	uint32_t vbat;
	uint32_t tmp;
	uint32_t us;			/* semaphore wait plus both reads */
	uint16_t raw_vbat[BCHKLOG_NSAMPLES];
	uint16_t raw_tmp[BCHKLOG_NSAMPLES];
};

struct bchklog_ram {
	uint32_t level;
	uint32_t src;
	uint32_t tmp;
	uint32_t present;
	uint32_t mode;
	uint32_t gui;
};

struct bchklog_state {
	uint32_t seq;			/* seconds since start, one per s line */
	struct bchklog_ram prev;
	uint32_t reads;			/* in the summary window */
	uint32_t skips;
	uint32_t vbat_min, vbat_max;
	uint32_t tmp_min, tmp_max;
	uint32_t us_max;
	char line[BCHKLOG_LINE];
};

static struct bchklog_state bchklog;

static FENYKL_LOG_MODULE(bchklog_mod, "bchk", FENYKL_LOG_INFO);

static uint32_t bchklog_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t bchklog_rd16(uint32_t a)
{
	return *(volatile uint16_t *)a;
}

static unsigned int bchklog_len(const char *s)
{
	unsigned int n = 0;

	while (s[n])
		n++;
	return n;
}

static char *bchklog_end(void)
{
	return bchklog.line + bchklog_len(bchklog.line);
}

static void bchklog_put(void)
{
	fenykl_log_text(&bchklog_mod, FENYKL_LOG_INFO, bchklog.line, bchklog_len(bchklog.line));
}

static void bchklog_copy_samples(uint16_t *dst)
{
	for (unsigned int i = 0; i < BCHKLOG_NSAMPLES; i++)
		dst[i] = bchklog_rd16(BCHKLOG_SAMPLES + 2 * i);
}

/* Both reads under SBCHKCORE: the sample buffer and the completion event are shared. */
static void bchklog_sample(struct bchklog_read *r)
{
	uint32_t t0 = FENYKL_LOG_TIMER_NOW();

	r->w = fw_OlySemaphoreWait(BCHKLOG_SEM, BCHKLOG_SEM_TICKS);
	if (r->w)
		return;
	if (!bchklog_rd8(BCHKLOG_SRC_ON)) {
		fw_OlySemaphoreSignal(BCHKLOG_SEM);
		r->w = BCHKLOG_STANDBY;
		return;
	}
	r->vbat = fw_bchk_ad_read(BCHKLOG_CH_VBAT);
	bchklog_copy_samples(r->raw_vbat);
	r->tmp = fw_bchk_ad_read(BCHKLOG_CH_TMP);
	bchklog_copy_samples(r->raw_tmp);
	fw_OlySemaphoreSignal(BCHKLOG_SEM);
	r->us = FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());
}

static void bchklog_read_ram(struct bchklog_ram *m)
{
	m->level = bchklog_rd16(BCHKLOG_LEVEL);
	m->src = bchklog_rd16(BCHKLOG_SRC);
	m->tmp = bchklog_rd16(BCHKLOG_TMP);
	m->present = bchklog_rd8(BCHKLOG_PRESENT) & 1;
	m->mode = bchklog_rd8(BCHKLOG_MODE);
	m->gui = bchklog_rd8(BCHKLOG_GUI) << 8 | bchklog_rd8(BCHKLOG_GUI + 1);
}

static void bchklog_window_reset(void)
{
	bchklog.reads = 0;
	bchklog.skips = 0;
	bchklog.vbat_min = 0xFFFF;
	bchklog.vbat_max = 0;
	bchklog.tmp_min = 0xFFFF;
	bchklog.tmp_max = 0;
	bchklog.us_max = 0;
}

static void bchklog_window_add(const struct bchklog_read *r)
{
	if (r->w) {
		bchklog.skips++;
		return;
	}
	bchklog.reads++;
	if (r->vbat < bchklog.vbat_min)
		bchklog.vbat_min = r->vbat;
	if (r->vbat > bchklog.vbat_max)
		bchklog.vbat_max = r->vbat;
	if (r->tmp < bchklog.tmp_min)
		bchklog.tmp_min = r->tmp;
	if (r->tmp > bchklog.tmp_max)
		bchklog.tmp_max = r->tmp;
	if (r->us > bchklog.us_max)
		bchklog.us_max = r->us;
}

static void bchklog_log_second(const struct bchklog_read *r, const struct bchklog_ram *m)
{
	fw_sprintf(bchklog.line, "s %u w %d b %u %u t %u %u", bchklog.seq, r->w, r->vbat,
		   r->vbat >> 2, r->tmp, r->tmp >> 2);
	fw_sprintf(bchklog_end(), " L %u S %x T %x P %u M %u G %04x", m->level, m->src, m->tmp,
		   m->present, m->mode, m->gui);
	bchklog_put();
}

static void bchklog_log_raw(const struct bchklog_read *r)
{
	const uint16_t *v = r->raw_vbat, *t = r->raw_tmp;

	if (r->w)
		return;
	fw_sprintf(bchklog.line, "r %u b %u %u %u %u %u", bchklog.seq, v[0], v[1], v[2], v[3],
		   v[4]);
	fw_sprintf(bchklog_end(), " t %u %u %u %u %u", t[0], t[1], t[2], t[3], t[4]);
	bchklog_put();
}

static void bchklog_log_change(const struct bchklog_ram *m)
{
	const struct bchklog_ram *p = &bchklog.prev;

	if (m->level == p->level && m->src == p->src)
		return;
	fw_sprintf(bchklog.line, "c %u L %u > %u S %x > %x M %u > %u", bchklog.seq, p->level,
		   m->level, p->src, m->src, p->mode, m->mode);
	bchklog_put();
}

static void bchklog_log_summary(void)
{
	fw_sprintf(bchklog.line, "m %u n %u k %u b %u..%u t %u..%u x %u", bchklog.seq,
		   bchklog.reads, bchklog.skips, bchklog.vbat_min, bchklog.vbat_max,
		   bchklog.tmp_min, bchklog.tmp_max, bchklog.us_max);
	bchklog_put();
	bchklog_window_reset();
}

static void bchklog_tick(void)
{
	struct bchklog_read r = { 0 };
	struct bchklog_ram m;

	bchklog_sample(&r);
	bchklog_read_ram(&m);
	bchklog_log_change(&m);
	bchklog.prev = m;
	bchklog_log_second(&r, &m);
	if (bchklog.seq % BCHKLOG_RAW_S == 0)
		bchklog_log_raw(&r);
	bchklog_window_add(&r);
	if (bchklog.seq % BCHKLOG_SUM_S == BCHKLOG_SUM_S - 1)
		bchklog_log_summary();
	bchklog.seq++;
}

static void bchklog_final_flush(void)
{
	for (unsigned int i = 0; i < BCHKLOG_FLUSH_TRIES; i++) {
		if (fenykl_log_flush() != FENYKL_LOG_EBUSY)
			return;
		fw_OlyTaskSleep(20);
	}
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config cfg = { .flush_ms = BCHKLOG_FLUSH_MS };
	int init = fenykl_log_init(&cfg);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;

	(void)load_base;
	(void)image_size;
	fenykl_log_info(&bchklog_mod, "bchklog: log init %d task %d", init, task);
	bchklog_read_ram(&bchklog.prev);
	bchklog_window_reset();

	while (bchklog.seq < BCHKLOG_RUN_S) {
		uint32_t t0 = FENYKL_LOG_TIMER_NOW(), spent;

		bchklog_tick();
		spent = FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());
		if (spent < BCHKLOG_PERIOD_US)
			fw_OlyTaskSleep((BCHKLOG_PERIOD_US - spent) / 1000u);
	}

	fenykl_log_get_stats(&st);
	fenykl_log_info(&bchklog_mod, "end after %u s: log bytes %u lost %u", bchklog.seq, st.bytes,
			st.overwritten0);
	bchklog_final_flush();
}
