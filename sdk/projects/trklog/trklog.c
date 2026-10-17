/*
 * trklog: does the contrast-AF window of stock C-AF+TR follow the subject tracker? No hooks,
 * RAM reads only.
 *
 * Every TRKLOG_POLL_TICKS the plugin task reads the TakeFcs state and the current FCS packet;
 * while the tracker runs or a +TR packet is current it logs every group of watched fields that
 * changed. Never set debug bits in firmware RAM (0x605AED3C bit 0x80
 * blocks S-AF).
 */
#include "log.h"

/* firmware data, v1.6 */
#define TRKLOG_TAKE_PTR 0x60575E50u	/* TakeFcs state */
#define TRKLOG_CTX_PTR 0x605AED1Cu	/* fcs_mvbuf_ptr: ctx, 0 before the first AF */
#define TRKLOG_W_PTR 0x605AED24u	/* fcs_iaf_work_ptr: W */
#define TRKLOG_PULSE 0x605522D2u	/* s16 lens pulse[0] */
#define TRKLOG_CAF_COM 0x605A9654u	/* u16 fcs_caf_info com */
#define TRKLOG_TR 0x605A95C8u		/* fcs_tr_info, 20 B */
#define TRKLOG_WIN 0x605A9624u		/* fcs_tr_win */
#define TRKLOG_PEND 0x605A8C44u		/* u8 flag, then 4 x {hs, hw, vs, vw} u16 */
#define TRKLOG_TRK 0x605CDB00u		/* tracker output block */
#define TRKLOG_TRK_TGT 0x605CDACCu	/* u16 x 4 target windows */

/* take (TakeFcs state), u8 fields */
#define TRKLOG_TAKE_FOCUS 0x05u		/* mode_focus, 2 = C-AF */
#define TRKLOG_TAKE_TRACK 0x08u		/* mode_af_tracking */
#define TRKLOG_TAKE_TRFLAG 0x17u	/* af_tracking_flag -> packet +TR */
#define TRKLOG_TAKE_POST 0x18u		/* tracking_post_flag: 1 tracker, 2 EMS */
#define TRKLOG_TAKE_RESULT 0x26u	/* af_result */

/* FCS_PACKET (*(ctx + 4)) */
#define TRKLOG_CTX_PKT 0x04u
#define TRKLOG_PKT_TR 0x1Cu		/* u8 +TR */
#define TRKLOG_PKT_CONT 0x27u		/* u8 area_continue */

/* ctx: live AF windows (fcs_af_window_pending_apply 0x6E7D8D87) */
#define TRKLOG_CTX_WFLAGS 0x380u	/* u16, |= 0x780 at apply */
#define TRKLOG_CTX_WIN 0x3C2u		/* 4 x {hs, hw, vs, vw} u16 */

/* W (IAF work) */
#define TRKLOG_W_PHASE 0x04u		/* u16 */
#define TRKLOG_W_SUB 0x06u		/* u16 */
#define TRKLOG_W_PEAK 0x50u		/* s32 */
#define TRKLOG_W_MOT 0x298u		/* pointer; +0x36/+0x38 s16 window delta (fcs_tr_frame_update) */
#define TRKLOG_MOT_DX 0x36u

#define TRKLOG_POLL_TICKS 10		/* ~10 ms with 1 ms ticks */
#define TRKLOG_RUN_US (10u * 60u * 1000000u)
#define TRKLOG_HEARTBEAT_US (10u * 1000000u)
#define TRKLOG_CAP_POLLS 20u		/* log size checked every ~200 ms */
#define TRKLOG_BOOT_CAP (1792u << 10)	/* bytes this boot */
#define TRKLOG_FILE_CAP (3584u << 10)	/* whole file, below the library's 4 MiB delete-at-init cap */
#define TRKLOG_FLUSH_MS 500u
#define TRKLOG_FLUSH_TRIES 50u
#define TRKLOG_ARGS 4
#define TRKLOG_GROUPS 9
#define TRKLOG_PHASE_PEAK 0x1500u	/* FtAF end after a peak with +TR */

struct trklog_regs {
	uint32_t take;
	uint32_t ctx;
	uint32_t w;
	uint32_t pkt;
};

struct trklog_group {
	const char *fmt;
	void (*read)(const struct trklog_regs *r, uint32_t *v);
	bool active_only;
	uint8_t nkey;			/* leading values compared; the rest are snapshots */
};

struct trklog_state {
	uint32_t polls;
	uint32_t active_polls;
	uint32_t lines;
	bool active;
	uint32_t com;			/* last packet command */
	uint32_t phase;			/* last W+4 */
	uint32_t lost;			/* last 0x605CDB20 */
	uint32_t n_active;		/* activations */
	uint32_t n_com1200;		/* packet changes to 0x1200 while active */
	uint32_t n_peak;		/* phase changes to 0x1500 while active */
	uint32_t n_lost;		/* lost flag 0 -> 1 */
	uint32_t prev[TRKLOG_GROUPS][TRKLOG_ARGS];
	bool seen[TRKLOG_GROUPS];
};

static struct trklog_state trklog;

static FENYKL_LOG_MODULE(trklog_mod, "trklog", FENYKL_LOG_INFO);

static uint32_t trklog_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t trklog_rd16(uint32_t a)
{
	return *(volatile uint16_t *)a;
}

static uint32_t trklog_rds16(uint32_t a)
{
	return (uint32_t)(int32_t)*(volatile int16_t *)a;
}

static uint32_t trklog_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

/* Two u16 at a as hi << 16 | lo. */
static uint32_t trklog_xy(uint32_t a)
{
	return trklog_rd16(a) << 16 | trklog_rd16(a + 2);
}

/* A pointer read from firmware RAM that may be stale or unset: DRAM only. */
static bool trklog_ptr_ok(uint32_t p)
{
	return p >= 0x60000000u && p < 0x70000000u && !(p & 3);
}

static void trklog_read_packet(const struct trklog_regs *r, uint32_t *v)
{
	v[0] = r->pkt ? trklog_rd16(r->pkt) : 0;
	v[1] = r->pkt ? trklog_rd8(r->pkt + TRKLOG_PKT_TR) : 0;
	v[2] = r->pkt ? trklog_rd8(r->pkt + TRKLOG_PKT_CONT) : 0;
	v[3] = trklog_rd16(TRKLOG_CAF_COM);
}

static void trklog_read_take(const struct trklog_regs *r, uint32_t *v)
{
	if (!r->take) {
		memset(v, 0xFF, TRKLOG_ARGS * sizeof(*v));
		return;
	}
	v[0] = trklog_rd8(r->take + TRKLOG_TAKE_FOCUS) << 8 | trklog_rd8(r->take + TRKLOG_TAKE_TRACK);
	v[1] = trklog_rd8(r->take + TRKLOG_TAKE_TRFLAG);
	v[2] = trklog_rd8(r->take + TRKLOG_TAKE_POST);
	v[3] = trklog_rd8(r->take + TRKLOG_TAKE_RESULT);
}

/* Positions, 640x480: fused, AF, HUD frame, FCS tracking window. */
static void trklog_read_pos(const struct trklog_regs *r, uint32_t *v)
{
	(void)r;
	v[0] = trklog_xy(TRKLOG_TRK + 0x02);
	v[1] = trklog_xy(TRKLOG_TRK + 0x12);
	v[2] = trklog_xy(TRKLOG_TRK + 0x0A);
	v[3] = trklog_xy(TRKLOG_WIN);
}

static void trklog_read_trk(const struct trklog_regs *r, uint32_t *v)
{
	uint32_t t = TRKLOG_TRK;

	(void)r;
	v[0] = trklog_rd8(t + 0x16) << 24 | trklog_rd8(t + 0x17) << 16 | trklog_rd8(t + 0x1C) << 8 |
	       trklog_rd8(t + 0x1F);
	v[1] = trklog_rd8(t + 0x20) << 8 | trklog_rd8(t + 0x21);
	v[2] = trklog_xy(t + 0x0E);
	v[3] = 0;
}

static void trklog_read_win(const struct trklog_regs *r, uint32_t *v)
{
	(void)r;
	v[0] = trklog_xy(TRKLOG_WIN + 4);
	v[1] = trklog_xy(TRKLOG_WIN + 8);
	v[2] = trklog_rd8(TRKLOG_WIN + 0xC) << 16 | trklog_rd8(TRKLOG_WIN + 0xD) << 8 |
	       trklog_rd8(TRKLOG_WIN + 0xE);
	v[3] = 0;
}

static void trklog_read_tr(const struct trklog_regs *r, uint32_t *v)
{
	(void)r;
	v[0] = trklog_rd16(TRKLOG_TR);
	v[1] = trklog_xy(TRKLOG_TR + 2);
	v[2] = trklog_xy(TRKLOG_TR + 6);
	v[3] = trklog_rd8(TRKLOG_TR + 0x10) << 24 | trklog_rd8(TRKLOG_TR + 0x11) << 16 |
	       trklog_rd8(TRKLOG_TR + 0x12) << 8 | trklog_rd8(TRKLOG_TR + 0x13);
}

/* FtAF phase (compared); pulse, peak and window delta are snapshots. */
static void trklog_read_ftaf(const struct trklog_regs *r, uint32_t *v)
{
	uint32_t mot;

	if (!r->w) {
		memset(v, 0, TRKLOG_ARGS * sizeof(*v));
		return;
	}
	mot = trklog_rd32(r->w + TRKLOG_W_MOT);
	v[0] = trklog_rd16(r->w + TRKLOG_W_PHASE) << 16 | trklog_rd16(r->w + TRKLOG_W_SUB);
	v[1] = trklog_rds16(TRKLOG_PULSE);
	v[2] = trklog_rd32(r->w + TRKLOG_W_PEAK);
	v[3] = trklog_ptr_ok(mot) ? trklog_xy(mot + TRKLOG_MOT_DX) : 0;
}

/* AF window 0, Bayer coordinates: pending table written by fcs_af_window_box_set. */
static void trklog_read_pend(const struct trklog_regs *r, uint32_t *v)
{
	uint32_t p = TRKLOG_PEND + 2;

	(void)r;
	v[0] = trklog_rd8(TRKLOG_PEND);
	v[1] = trklog_rd16(p) << 16 | trklog_rd16(p + 4);
	v[2] = trklog_rd16(p + 2) << 16 | trklog_rd16(p + 6);
	v[3] = 0;
}

/* AF window 0 as applied to the evaluation (ctx+0x3C2). */
static void trklog_read_live(const struct trklog_regs *r, uint32_t *v)
{
	uint32_t p = r->ctx + TRKLOG_CTX_WIN;

	if (!r->ctx) {
		memset(v, 0, TRKLOG_ARGS * sizeof(*v));
		return;
	}
	v[0] = trklog_rd16(r->ctx + TRKLOG_CTX_WFLAGS);
	v[1] = trklog_rd16(p) << 16 | trklog_rd16(p + 4);
	v[2] = trklog_rd16(p + 2) << 16 | trklog_rd16(p + 6);
	v[3] = 0;
}

enum {
	TRKLOG_G_PACKET,
	TRKLOG_G_TAKE,
	TRKLOG_G_POS,
	TRKLOG_G_TRK,
	TRKLOG_G_WIN,
	TRKLOG_G_TR,
	TRKLOG_G_FTAF,
	TRKLOG_G_PEND,
	TRKLOG_G_LIVE,
};

static const struct trklog_group trklog_groups[] = {
	[TRKLOG_G_PACKET] = { "k com %04x tr %u cont %u caf %04x", trklog_read_packet, false, 4 },
	[TRKLOG_G_TAKE] = { "m %04x trflag %u post %u res %u", trklog_read_take, false, 4 },
	[TRKLOG_G_POS] = { "p %08x %08x %08x %08x", trklog_read_pos, true, 4 },
	[TRKLOG_G_TRK] = { "t %08x lost %04x hsz %08x", trklog_read_trk, true, 3 },
	[TRKLOG_G_WIN] = { "w %08x sz %08x st %06x", trklog_read_win, true, 3 },
	[TRKLOG_G_TR] = { "r %04x fc %08x yc %08x fl %08x", trklog_read_tr, true, 4 },
	[TRKLOG_G_FTAF] = { "f %08x pulse %d peak %d d %08x", trklog_read_ftaf, true, 1 },
	[TRKLOG_G_PEND] = { "ap %u %08x %08x", trklog_read_pend, true, 3 },
	[TRKLOG_G_LIVE] = { "al %04x %08x %08x", trklog_read_live, true, 3 },
};

_Static_assert(sizeof(trklog_groups) / sizeof(trklog_groups[0]) == TRKLOG_GROUPS,
	       "one prev[] row per group");

/* Counts the events the report needs, from the compared values of a changed group. */
static void trklog_count(unsigned int i, const uint32_t *v)
{
	if (!trklog.active)
		return;
	if (i == TRKLOG_G_PACKET && v[0] == 0x1200 && trklog.com != 0x1200)
		trklog.n_com1200++;
	if (i == TRKLOG_G_FTAF && v[0] >> 16 == TRKLOG_PHASE_PEAK && trklog.phase != TRKLOG_PHASE_PEAK)
		trklog.n_peak++;
	if (i == TRKLOG_G_TRK && v[1] >> 8 && !trklog.lost)
		trklog.n_lost++;
}

static void trklog_note(unsigned int i, const uint32_t *v)
{
	if (i == TRKLOG_G_PACKET)
		trklog.com = v[0];
	if (i == TRKLOG_G_FTAF)
		trklog.phase = v[0] >> 16;
	if (i == TRKLOG_G_TRK)
		trklog.lost = v[1] >> 8;
}

static void trklog_poll_group(unsigned int i, const struct trklog_regs *r)
{
	const struct trklog_group *g = &trklog_groups[i];
	uint32_t v[TRKLOG_ARGS];

	if (g->active_only && !trklog.active)
		return;
	g->read(r, v);
	if (trklog.seen[i] && !memcmp(v, trklog.prev[i], g->nkey * sizeof(*v)))
		return;
	trklog_count(i, v);
	trklog_note(i, v);
	fenykl_log_info(&trklog_mod, g->fmt, v[0], v[1], v[2], v[3]);
	memcpy(trklog.prev[i], v, sizeof(v));
	trklog.seen[i] = true;
	trklog.lines++;
}

static void trklog_targets(const char *when)
{
	fenykl_log_info(&trklog_mod, "%s: targets %08x %08x", when, trklog_xy(TRKLOG_TRK_TGT),
			trklog_xy(TRKLOG_TRK_TGT + 4));
}

/* Active while the tracker runs (take+0x18) or a +TR packet is current. */
static void trklog_set_active(const struct trklog_regs *r)
{
	bool post = r->take && trklog_rd8(r->take + TRKLOG_TAKE_POST);
	bool tr = r->pkt && trklog_rd8(r->pkt + TRKLOG_PKT_TR) == 1;
	bool active = post || tr;

	if (active == trklog.active)
		return;
	trklog.active = active;
	fenykl_log_info(&trklog_mod, "act %u post %u tr %u", active, post, tr);
	if (!active)
		return;
	trklog.n_active++;
	/* re-log every active-only group at the start of each session */
	for (unsigned int i = 0; i < TRKLOG_GROUPS; i++)
		if (trklog_groups[i].active_only)
			trklog.seen[i] = false;
	trklog_targets("act");
}

static void trklog_poll(void)
{
	struct trklog_regs r = { 0 };
	uint32_t take = trklog_rd32(TRKLOG_TAKE_PTR);
	uint32_t ctx = trklog_rd32(TRKLOG_CTX_PTR);
	uint32_t w = trklog_rd32(TRKLOG_W_PTR);

	r.take = trklog_ptr_ok(take) ? take : 0;
	r.ctx = trklog_ptr_ok(ctx) ? ctx : 0;
	r.w = trklog_ptr_ok(w) ? w : 0;
	if (r.ctx) {
		uint32_t pkt = trklog_rd32(r.ctx + TRKLOG_CTX_PKT);

		r.pkt = trklog_ptr_ok(pkt) ? pkt : 0;
	}
	trklog.polls++;
	trklog_set_active(&r);
	if (trklog.active)
		trklog.active_polls++;
	for (unsigned int i = 0; i < TRKLOG_GROUPS; i++)
		trklog_poll_group(i, &r);
}

static void trklog_heartbeat(const char *when)
{
	struct fenykl_log_stats st;

	fenykl_log_get_stats(&st);
	fenykl_log_info(&trklog_mod, "%s: polls %u active %u lines %u", when, trklog.polls,
			trklog.active_polls, trklog.lines);
	fenykl_log_info(&trklog_mod, "%s: act %u com1200 %u peaks %u", when, trklog.n_active,
			trklog.n_com1200, trklog.n_peak);
	fenykl_log_info(&trklog_mod, "%s: trklost %u, log bytes %u lost %u", when, trklog.n_lost,
			st.bytes, st.overwritten0);
	if (trklog.active)
		trklog_targets(when);
}

static bool trklog_log_full(void)
{
	struct fenykl_log_stats st;

	fenykl_log_get_stats(&st);
	return st.bytes >= TRKLOG_BOOT_CAP || st.file_size + st.bytes >= TRKLOG_FILE_CAP;
}

static void trklog_final_flush(void)
{
	for (unsigned int i = 0; i < TRKLOG_FLUSH_TRIES; i++) {
		if (fenykl_log_flush() != FENYKL_LOG_EBUSY)
			return;
		fw_OlyTaskSleep(TRKLOG_POLL_TICKS);
	}
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config cfg = { .flush_ms = TRKLOG_FLUSH_MS };
	int init = fenykl_log_init(&cfg);
	int task = fenykl_log_task_start();
	uint32_t elapsed = 0, beat = 0, last;
	const char *reason = "time";

	(void)load_base;
	(void)image_size;
	fenykl_log_info(&trklog_mod, "trklog: log init %d task %d", init, task);

	last = FENYKL_LOG_TIMER_NOW();
	while (elapsed < TRKLOG_RUN_US) {
		uint32_t now;

		trklog_poll();
		if (trklog.polls % TRKLOG_CAP_POLLS == 0 && trklog_log_full()) {
			reason = "log cap";
			break;
		}
		fw_OlyTaskSleep(TRKLOG_POLL_TICKS);
		now = FENYKL_LOG_TIMER_NOW();
		elapsed += FENYKL_LOG_ELAPSED(last, now);
		beat += FENYKL_LOG_ELAPSED(last, now);
		last = now;
		if (beat >= TRKLOG_HEARTBEAT_US) {
			beat = 0;
			trklog_heartbeat("hb");
		}
	}

	trklog_heartbeat("end");
	fenykl_log_info(&trklog_mod, "end: %s after %u s", reason, elapsed / 1000000u);
	trklog_final_flush();
}
