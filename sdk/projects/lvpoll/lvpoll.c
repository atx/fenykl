/* E-M10 II live-view poller: every LV_POLL_TICKS reads MVR's flip records, the display driver's img1
   shadow, the display buffer pool and, while the driver reports display 0 up, five display controller
   registers. Logs flips per second, the first flips after each live-view start and the slow state on
   change. Reads only: no firmware call but the log library's and the PSW primitives, no firmware RAM
   write. Stops after LV_RUN_S. */
#include <fenykl.h>
#include <log.h>

#define LV_POLL_TICKS 10
#define LV_RUN_S 200
#define LV_LINES 3000		/* change lines over the run */
#define LV_LINES_PER_S 30
#define LV_SEQ 12		/* flips logged one by one after a pause */
#define LV_PAUSE_US 1000000u	/* no flip for this long: the next flips are logged again */

#define LV_MVR_SHOWN 0xA1C2BE00u	/* u32, written by mvr_disp_update after a flip */
#define LV_MVR_PREV 0xA1C2BE04u		/* u32, the shown address before that flip */
#define LV_IMG1_SHADOW 0xA1C1A050u	/* u32, display driver's img1 address */
#define LV_IMG1_PENDING 0xA1C1C28Cu	/* u32, img1 address waiting for the tear-free write */
#define LV_POOL 0xA1C2BCE8u		/* 5 x 16 B {u32 addr, u16 id, u8, .., u16 state @+0xC} */
#define LV_POOL_N 5
#define LV_POOL_COUNT 0xA1C2BDBAu	/* u16, records in use */
#define LV_DISP0_STATE 0xA1C19FC2u	/* u8, the driver reads display 0 registers only when 2 */
#define LV_DISP0_UP 2
#define LV_DC_LATCH 0x9C000030u
#define LV_DC_IMG1 0x9C000038u		/* programmed img1 address */
#define LV_DC_WIN 0x9C0000A4u
#define LV_DC_FMT 0x9C000194u
#define LV_DC_ACTIVE 0x9C001C38u	/* img1 address the hardware scans out */
#define LV_U8(a) (*(volatile uint8_t *)(a))
#define LV_U16(a) (*(volatile uint16_t *)(a))
#define LV_U32(a) (*(volatile uint32_t *)(a))

struct lv_dc {
	uint32_t up;		/* display 0 state byte; the registers are read only when it is 2 */
	uint32_t latch, img1, active, win, fmt;
};

struct lv_pool {
	uint32_t addr[LV_POOL_N];
	uint32_t id[LV_POOL_N];	/* u16 id << 16 | u8 +6 << 8 | (u8)state */
	uint32_t count;
};

static struct {
	uint32_t shown, prev;	/* last flip pair seen */
	uint32_t shadow, active;
	uint32_t last_flip;	/* timer at the last flip seen */
	struct lv_dc dc, dc_logged;
	struct lv_pool pool, pool_logged;
	/* per second */
	uint32_t flips, multi, shadow_n, active_n, dt_min, dt_max;
	unsigned int seq, lines, lines_sec, skipped;
} lv;

static FENYKL_LOG_MODULE(lv_mod, "lvpoll", FENYKL_LOG_INFO);

/* One more change line if neither the run nor the per-second budget is spent. */
static bool lv_budget(unsigned int n)
{
	if (lv.lines + n > LV_LINES || lv.lines_sec + n > LV_LINES_PER_S) {
		lv.skipped++;
		return false;
	}
	lv.lines += n;
	lv.lines_sec += n;
	return true;
}

/* MVR writes prev, then shown; equal reads before and after rule out a half-done pair. */
static void lv_read_flip(uint32_t *shown, uint32_t *prev)
{
	uint32_t s;

	do {
		s = LV_U32(LV_MVR_SHOWN);
		*prev = LV_U32(LV_MVR_PREV);
		*shown = LV_U32(LV_MVR_SHOWN);
	} while (s != *shown);
}

/* The state test and the register reads run with interrupts masked, so no CPU0 task can take display
   0 down in between. */
static void lv_read_dc(struct lv_dc *d)
{
	uint32_t psw = fw_psw_get();

	memset(d, 0, sizeof(*d));
	fw_irq_disable_all();
	d->up = LV_U8(LV_DISP0_STATE);
	if (d->up == LV_DISP0_UP) {
		d->latch = LV_U32(LV_DC_LATCH);
		d->img1 = LV_U32(LV_DC_IMG1);
		d->active = LV_U32(LV_DC_ACTIVE);
		d->win = LV_U32(LV_DC_WIN);
		d->fmt = LV_U32(LV_DC_FMT);
	}
	fw_irq_mask_restore(psw);
}

static void lv_read_pool(struct lv_pool *p)
{
	for (unsigned int i = 0; i < LV_POOL_N; i++) {
		uint32_t r = LV_POOL + 16 * i;

		p->addr[i] = LV_U32(r);
		p->id[i] = (uint32_t)LV_U16(r + 4) << 16 | LV_U8(r + 6) << 8 | (LV_U16(r + 0xC) & 0xFFu);
	}
	p->count = LV_U16(LV_POOL_COUNT);
}

static void lv_poll_flip(uint32_t now)
{
	uint32_t shown, prev, dt;

	lv_read_flip(&shown, &prev);
	if (shown == lv.shown)
		return;
	/* prev == the last shown: one flip; else two or more. A lower bound: an even number of flips over
	   two buffers leaves shown unchanged */
	if (prev == lv.shown) {
		lv.flips++;
	} else {
		lv.flips += 2;
		lv.multi++;
	}
	dt = FENYKL_LOG_ELAPSED(lv.last_flip, now);
	if (dt >= LV_PAUSE_US) {
		lv.seq = 0;
	} else {
		lv.dt_min = dt < lv.dt_min ? dt : lv.dt_min;
		lv.dt_max = dt > lv.dt_max ? dt : lv.dt_max;
	}
	if (lv.seq < LV_SEQ && lv_budget(1)) {
		lv.seq++;
		fenykl_log_info(&lv_mod, "flip %u: shown %08x prev %08x dt %u", lv.seq, shown, prev,
				lv.seq > 1 ? dt : 0);
	}
	lv.shown = shown;
	lv.prev = prev;
	lv.last_flip = now;
}

static void lv_poll(uint32_t now)
{
	uint32_t shadow = LV_U32(LV_IMG1_SHADOW);
	struct lv_dc d;
	struct lv_pool p;

	lv_poll_flip(now);
	if (shadow != lv.shadow) {
		lv.shadow = shadow;
		lv.shadow_n++;
	}
	lv_read_dc(&d);
	if (d.active != lv.active) {
		lv.active = d.active;
		lv.active_n++;
	}
	lv.dc = d;
	/* the per-frame registers are sampled in the second line; window and format on change */
	if ((d.up != lv.dc_logged.up || d.win != lv.dc_logged.win || d.fmt != lv.dc_logged.fmt) &&
	    lv_budget(1)) {
		lv.dc_logged = d;
		fenykl_log_info(&lv_mod, "disp0 state %u win %08x fmt %08x latch %08x", d.up, d.win, d.fmt,
				d.latch);
	}
	lv_read_pool(&p);
	lv.pool = p;
}

/* Once a second: flip counts and samples while anything moves; the pool when its addresses or count
   differ from the last logged copy. */
static void lv_second(void)
{
	struct lv_pool *p = &lv.pool;

	lv.lines_sec = 0;
	if ((lv.flips || lv.shadow_n || lv.active_n) && lv_budget(2)) {
		fenykl_log_info(&lv_mod, "sec flips %u multi %u shadow %u active %u", lv.flips, lv.multi,
				lv.shadow_n, lv.active_n);
		fenykl_log_info(&lv_mod, "sec dt %u..%u us, shown %08x active %08x",
				lv.dt_min == ~0u ? 0 : lv.dt_min, lv.dt_max, lv.shown, lv.active);
	}
	lv.flips = lv.multi = lv.shadow_n = lv.active_n = lv.dt_max = 0;
	lv.dt_min = ~0u;
	if ((memcmp(p->addr, lv.pool_logged.addr, sizeof(p->addr)) ||
	     p->count != lv.pool_logged.count) &&
	    lv_budget(1 + LV_POOL_N)) {
		lv.pool_logged = *p;
		fenykl_log_info(&lv_mod, "pool count %u, img1 shadow %08x pending %08x", p->count,
				lv.shadow, LV_U32(LV_IMG1_PENDING));
		for (unsigned int i = 0; i < LV_POOL_N; i++)
			fenykl_log_info(&lv_mod, "pool %u: addr %08x id %08x", i, p->addr[i], p->id[i]);
	}
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;
	uint32_t start = FENYKL_LOG_TIMER_NOW(), sec = start, now = start;

	fenykl_log_info(&lv_mod, "lvpoll: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	lv_read_flip(&lv.shown, &lv.prev);
	lv.last_flip = start;
	lv.dc_logged.up = ~0u; /* the first poll logs the display state */
	lv.pool_logged.count = ~0u;
	lv.dt_min = ~0u;
	while (FENYKL_LOG_ELAPSED(start, now) < LV_RUN_S * 1000000u) {
		lv_poll(now);
		if (FENYKL_LOG_ELAPSED(sec, now) >= 1000000u) {
			sec = now;
			lv_second();
		}
		fw_OlyTaskSleep(LV_POLL_TICKS);
		now = FENYKL_LOG_TIMER_NOW();
	}
	fenykl_log_get_stats(&st);
	fenykl_log_info(&lv_mod, "lvpoll end: %u s, %u lines, %u skipped, %u overwritten",
			FENYKL_LOG_ELAPSED(start, now) / 1000000u, lv.lines, lv.skipped,
			st.overwritten0);
}
