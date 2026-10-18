/* Camera probe: IPU engine-1 contention between a live-view resize task and still capture.
 *
 * Engine 1 of the IPU develops and JPEG-encodes stills (task REC_IMAGE) and runs the post-shot review
 * jobs (REC_VIEW). A plugin that resizes the shown display buffer on engine 1 once per live-view frame
 * (img_exec ucMode 0x15, YUYV 540x480 -> 160x120, ~2.2 ms per job) shares it with them through the
 * firmware's IPU lock word 0x8FF00100 (bit 2 engine 1, bit 4 engine 2): a test-and-set, no queue, the
 * waiter retries once per tick (img_hw_lock with OlyTaskSleep(0) = sleep to the next tick).
 *
 * Parts:
 *  - img_exec hook: every stock engine-1 job and every engine-2 job other than the live-view pass
 *    (mode 7) is logged (duration, mode, task, status, caller, ulFlag); live-view passes are counted.
 *    A stock job that found the lock bit of its engine set at entry gets a "W" record, with the wait
 *    if the holder was our resize job.
 *  - event handler (prio 0, passes everything): HALF/FULL key presses, non-key events.
 *  - poll task (priority 10, every tick): the REC take-handle table and the REC task step words,
 *    and the capture indicator derived from them (CAP 1 from a FULL press or a busy take handle until
 *    no handle is busy). At the end of each capture window: our jobs run / skipped in it (SHOT).
 *    Our jobs are logged one by one (R) only when slower than 3 ms, failed or the lock was taken.
 *  - resize task (priority 12): one job per new face-detection frame publish, in the mode of the
 *    current phase: OFF, WAIT (plain img_exec), SKIP (skip if engine 1 is locked), GATED (skip while
 *    a still is in progress, then as SKIP).
 *
 * Phases advance with the FULL presses: shot n (0-based) runs in mode (n / 3) % 4 for n < 24, OFF
 * afterwards. A status != 0 of our job is logged; one that took over 100 ms (IPU timeout) or the third
 * error stops the resize task for the boot. No firmware state is written. Log: C:\FENYKL.LOG.
 */
#include <fenykl.h>
#include "event.h"
#include "log.h"

#define LL_START_MS 5000
#define LL_STACK_SIZE 0x1000u
#define LL_POLL_PRIO 10
#define LL_RSZ_PRIO 12
#define LL_PRESSES_PER_PHASE 3u
#define LL_PHASES 8u
#define LL_SLOW_US 100000u
#define LL_MAX_ERRORS 3u
#define LL_PRESS_HOLD_US 3000000u /* capture assumed from a FULL press until a take handle appears */
#define LL_R_LOG_US 3000u /* a resize job is logged when slower than this or the lock was taken */
#define LL_SUM_US 30000000u
#define LL_EVENT_BUDGET 20u /* non-key event records per second */

/* Live-view addresses, verified by earlier probes (framepaths, lvrsz). */
#define LL_FD_PUB 0x606067B0u /* u32: FD slot published by mov_fd_publish, changes once per FD frame */
#define LL_IMG1 0x6034C880u /* u32: display buffer on screen (img1 shadow), 800x600 YUYV */
#define LL_IPU_LOCK 0x8FF00100u
#define LL_LOCK_E1 0x2u
#define LL_LOCK_E2 0x4u

/*
 * REC take-handle table, 25 entries of 0x18 bytes (rec_dbg.c "dbginfo" dump at 0x6E8E6FCA prints
 * "TH id addr spf1 cid pv fr resobj ps" from it). REC_MAIN sets fr = 0, ps = 0xFF, resobj = 0 when a
 * still job (command 0x10) starts; REC_IMAGE sets fr = 1 when it has developed the image (and posts
 * APPEVENT 0x040800F0); REC_FILE sets ps (1 = reply posted) after its file chain returns. pv = 0xFF
 * is set by command 0x13 (review).
 */
#define LL_TH 0x6061062Cu
#define LL_TH_N 25u
#define LL_TH_SIZE 0x18u
#define LL_TH_FR 0x14u
#define LL_TH_PS 0x15u
#define LL_TH_PV 0x16u
#define LL_PS_BUSY 0xFFu

/* REC step words main, prepro, image, view, file, sound: last breadcrumb (task << 24 | step). */
#define LL_RS 0x60610614u
#define LL_RS_N 6u

/* Current task id: TCB = **(kernel block + 0x60), id = u16 TCB + 0x10 (k_ref_tsk 0x6E68432A). */
#define LL_KCB 0x8FF00004u

/* IMG_CTRL (0xC8 bytes), field names from the firmware's own debug dump (mov_dbg.c). */
#define IC_SIZE 0xC8u
#define IC_R1ADDR 0x00u
#define IC_W1ADDR 0x10u
#define IC_FLAG 0x54u
#define IC_PARAM 0x60u
#define IC_IN_MEM_HW 0x6Au
#define IC_IN_USE_HW 0x6Cu
#define IC_IN_USE_VW 0x6Eu
#define IC_IN_AREA_HW 0x72u
#define IC_IN_AREA_VW 0x74u
#define IC_IN_AREA_HC 0x76u
#define IC_IN_AREA_VC 0x78u
#define IC_OUT_WHOLE_HW 0x8Eu
#define IC_OUT_WHOLE_VW 0x90u
#define IC_OUT_AREA_HW 0x9Cu
#define IC_OUT_AREA_VW 0x9Eu
#define IC_ZOOM 0xB2u
#define IC_MODE 0xC4u
#define IC_MODE_LV 0x07u
#define IC_MODE_RESIZE 0x15u
#define IC_FLAG_ENGINE2 0x10000000u
#define IP_SIZE 0x80u

#define LL_DISP_STRIDE 800u
#define LL_DISP_W 540u
#define LL_DISP_H 480u
#define LL_OUT_W 160u
#define LL_OUT_H 120u
#define LL_OUT_BYTES (LL_OUT_W * LL_OUT_H * 2u)

enum ll_mode { LL_OFF, LL_WAIT, LL_SKIP, LL_GATED, LL_MODES };

struct ll_stats {
	uint32_t frames, run, skip_busy, skip_gate, errors;
	uint32_t us_sum, us_max;
};

struct ll_state {
	/* resize job */
	uint8_t ctrl[IC_SIZE] __attribute__((aligned(4)));
	uint8_t param[IP_SIZE] __attribute__((aligned(4)));
	volatile uint32_t ours_busy, ours_end;
	struct ll_stats st[LL_MODES];
	uint32_t errors;
	uint8_t dead;
	/* events */
	volatile uint32_t presses;
	volatile uint32_t press_t;
	volatile uint8_t press_pending;
	uint32_t ev_window_t, ev_window_n, ev_dropped;
	/* capture indicator, written by the poll task */
	volatile uint8_t capture;
	struct ll_stats win; /* totals at the start of the capture window */
	volatile uint32_t win_max; /* longest resize job in the window */
	uint8_t th_fr[LL_TH_N], th_ps[LL_TH_N], th_pv[LL_TH_N];
	uint32_t rs[LL_RS_N];
	/* hook counters (CPU0) */
	volatile uint32_t lv_n, lv_sum, lv_max;
	volatile uint32_t e1_n, other_n;
};

static struct ll_state ll;
FENYKL_SHARED uint32_t ll_cpu1;

/* Resize output: only the IPU writes it; whole 64-byte lines so no cache line is shared. */
static uint8_t ll_out[LL_OUT_BYTES] __attribute__((aligned(64)));

FENYKL_LOG_MODULE(ll_log, "lvlock", FENYKL_LOG_DEBUG);

static const char *const ll_mode_name[LL_MODES] = { "OFF", "WAIT", "SKIP", "GATED" };

static uint32_t ll_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

static uint8_t ll_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t ll_now(void)
{
	return FENYKL_LOG_TIMER_NOW();
}

static uint32_t ll_since(uint32_t t)
{
	return FENYKL_LOG_ELAPSED(t, FENYKL_LOG_TIMER_NOW());
}

static enum ll_mode ll_mode_for(uint32_t presses)
{
	if (presses >= LL_PRESSES_PER_PHASE * LL_PHASES)
		return LL_OFF;
	return (enum ll_mode)((presses / LL_PRESSES_PER_PHASE) % LL_MODES);
}

static uint32_t ll_task_id(void)
{
	uint32_t kcb = ll_rd32(LL_KCB + 4u * FENYKL_CORE_ID());
	uint32_t p, tcb;

	if (!kcb)
		return 0xFF;
	p = ll_rd32(kcb + 0x60u);
	if (!p)
		return 0xFF;
	tcb = ll_rd32(p);
	if (!tcb)
		return 0xFF;
	return *(volatile uint16_t *)(tcb + 0x10u) & 0x7Fu;
}

/*
 * The img_exec hook. Records are stamped at the job's end, so its start is stamp - dt.
 * J record: dt (us), mode << 24 | engine2 << 23 | task << 16 | status, caller return address, ulFlag.
 */
static unsigned int ll_img_exec(void *ctrl, uint32_t ra)
{
	const volatile uint8_t *c = ctrl;
	uint32_t flag, mode, eng2, lock, lock_bit, ours_busy, t0, dt, st;

	if (FENYKL_CORE_ID() != 0) {
		(*FENYKL_SHARED_PTR(ll_cpu1))++;
		return fw_orig_img_exec(ctrl);
	}
	if (!ctrl)
		return fw_orig_img_exec(ctrl);
	flag = *(const volatile uint32_t *)(c + IC_FLAG);
	mode = c[IC_MODE];
	eng2 = (flag & IC_FLAG_ENGINE2) != 0;

	if (ctrl == ll.ctrl) {
		ll.ours_busy = 1;
		st = fw_orig_img_exec(ctrl);
		ll.ours_end = ll_now();
		ll.ours_busy = 0;
		return st;
	}

	lock_bit = eng2 ? LL_LOCK_E2 : LL_LOCK_E1;
	lock = ll_rd32(LL_IPU_LOCK);
	ours_busy = ll.ours_busy;
	t0 = ll_now();
	st = fw_orig_img_exec(ctrl);
	dt = ll_since(t0);

	if (eng2 && mode == IC_MODE_LV) {
		ll.lv_n++;
		ll.lv_sum += dt;
		if (dt > ll.lv_max)
			ll.lv_max = dt;
		return st;
	}
	if (eng2)
		ll.other_n++;
	else
		ll.e1_n++;
	fenykl_log_info(&ll_log, "J %u %x %x %x", dt,
			mode << 24 | eng2 << 23 | ll_task_id() << 16 | (st & 0xFFFFu), ra, flag);
	if (lock & lock_bit) {
		uint32_t wait = ours_busy ? FENYKL_LOG_ELAPSED(t0, ll.ours_end) : 0;

		fenykl_log_info(&ll_log, "W lk %x ours %u wait %u", lock, ours_busy, wait);
	}
	return st;
}

/*
 * Bound by hand instead of FENYKL_HOOK to get the caller: the generated entry stub calls
 * fenykl_hookwrap_img_exec(r, s) with r = D0 D1 A0 A1 and s = the caller's (4,SP); the firmware CALL
 * stored the return address at the caller's (0,SP), i.e. s[-1].
 */
unsigned int fenykl_hookwrap_img_exec(const uint32_t *r, const uint32_t *s);
unsigned int fenykl_hookwrap_img_exec(const uint32_t *r, const uint32_t *s)
{
	return ll_img_exec((void *)r[2], s[-1]);
}

static enum fenykl_event_action ll_on_event(struct fenykl_event *ev, void *arg)
{
	uint32_t e = ev->event;

	(void)arg;
	if (e == (FENYKL_EVENT_KEY_PRESS | 0x0101u)) {
		uint32_t n = ll.presses;

		ll.press_t = ll_now();
		ll.press_pending = 1;
		ll.presses = n + 1;
		fenykl_log_info(&ll_log, "FULL %u mode %u", n, ll_mode_for(n));
		return FENYKL_EVENT_PASS;
	}
	if (e == (FENYKL_EVENT_KEY_PRESS | 0x0100u) || e == (FENYKL_EVENT_KEY_RELEASE | 0x0100u) ||
	    e == (FENYKL_EVENT_KEY_RELEASE | 0x0101u)) {
		fenykl_log_info(&ll_log, "K %x", e);
		return FENYKL_EVENT_PASS;
	}
	if (FENYKL_EVENT_IS_KEY(e))
		return FENYKL_EVENT_PASS;
	if (FENYKL_LOG_ELAPSED(ll.ev_window_t, ll_now()) > 1000000u) {
		ll.ev_window_t = ll_now();
		ll.ev_window_n = 0;
	}
	if (ll.ev_window_n >= LL_EVENT_BUDGET) {
		ll.ev_dropped++;
		return FENYKL_EVENT_PASS;
	}
	ll.ev_window_n++;
	fenykl_log_info(&ll_log, "E %x p %x f %x", e, ev->param, ev->flag);
	return FENYKL_EVENT_PASS;
}

static struct fenykl_event_handler ll_handler = {
	.name = "lvlock", .fn = ll_on_event, .prio = 0, .enabled = true,
};

/* Reads the take-handle table and the step words, logs changes, updates the capture indicator. */
static void ll_poll_once(void)
{
	uint32_t busy = 0;

	for (uint32_t i = 0; i < LL_TH_N; i++) {
		uint32_t e = LL_TH + i * LL_TH_SIZE;
		uint8_t fr = ll_rd8(e + LL_TH_FR), ps = ll_rd8(e + LL_TH_PS), pv = ll_rd8(e + LL_TH_PV);

		busy += ps == LL_PS_BUSY;
		if (fr == ll.th_fr[i] && ps == ll.th_ps[i] && pv == ll.th_pv[i])
			continue;
		ll.th_fr[i] = fr;
		ll.th_ps[i] = ps;
		ll.th_pv[i] = pv;
		fenykl_log_info(&ll_log, "TH %u fr %x ps %x pv %x", i + 1, fr, ps, pv);
	}
	for (uint32_t i = 0; i < LL_RS_N; i++) {
		uint32_t v = ll_rd32(LL_RS + 4u * i);

		if (v == ll.rs[i])
			continue;
		ll.rs[i] = v;
		fenykl_log_info(&ll_log, "RS %u %x", i, v);
	}
	if (busy)
		ll.press_pending = 0;
	else if (ll.press_pending && ll_since(ll.press_t) > LL_PRESS_HOLD_US)
		ll.press_pending = 0;

	uint8_t cap = busy || ll.press_pending;

	if (cap == ll.capture)
		return;
	ll.capture = cap;
	fenykl_log_info(&ll_log, "CAP %u busy %u", cap, busy);

	struct ll_stats sum = { 0 };

	for (unsigned int m = 0; m < LL_MODES; m++) {
		sum.run += ll.st[m].run;
		sum.skip_busy += ll.st[m].skip_busy;
		sum.skip_gate += ll.st[m].skip_gate;
	}
	if (cap) {
		ll.win = sum;
		ll.win_max = 0;
		return;
	}
	fenykl_log_info(&ll_log, "SHOT run %u skipb %u skipg %u rmax %u", sum.run - ll.win.run,
			sum.skip_busy - ll.win.skip_busy, sum.skip_gate - ll.win.skip_gate, ll.win_max);
}

static void ll_poll_init(void)
{
	for (uint32_t i = 0; i < LL_TH_N; i++) {
		uint32_t e = LL_TH + i * LL_TH_SIZE;

		ll.th_fr[i] = ll_rd8(e + LL_TH_FR);
		ll.th_ps[i] = ll_rd8(e + LL_TH_PS);
		ll.th_pv[i] = ll_rd8(e + LL_TH_PV);
	}
	for (uint32_t i = 0; i < LL_RS_N; i++)
		ll.rs[i] = ll_rd32(LL_RS + 4u * i);
}

FENYKL_TASK(ll_poll);
void ll_poll(void)
{
	for (;;) {
		ll_poll_once();
		fw_OlyTaskSleep(1);
	}
}

/* IMG_CTRL like the stock YC resize job of REC_IMAGE (0x6E8DF67C), engine 1 (ulFlag 0). */
static void ll_build(uint32_t disp)
{
	uint8_t *c = ll.ctrl;

	memset(c, 0, sizeof(ll.ctrl));
	*(uint32_t *)(c + IC_R1ADDR) = (uint32_t)FENYKL_UNCACHED(disp);
	*(uint32_t *)(c + IC_W1ADDR) = (uint32_t)FENYKL_UNCACHED(ll_out);
	*(uint32_t *)(c + IC_PARAM) = (uint32_t)ll.param;
	*(uint16_t *)(c + IC_IN_MEM_HW) = LL_DISP_STRIDE;
	*(uint16_t *)(c + IC_IN_USE_HW) = LL_DISP_W;
	*(uint16_t *)(c + IC_IN_USE_VW) = LL_DISP_H;
	*(uint16_t *)(c + IC_IN_AREA_HW) = LL_DISP_W;
	*(uint16_t *)(c + IC_IN_AREA_VW) = LL_DISP_H;
	*(uint16_t *)(c + IC_IN_AREA_HC) = LL_DISP_W / 2;
	*(uint16_t *)(c + IC_IN_AREA_VC) = LL_DISP_H / 2;
	*(uint16_t *)(c + IC_OUT_WHOLE_HW) = LL_OUT_W;
	*(uint16_t *)(c + IC_OUT_WHOLE_VW) = LL_OUT_H;
	*(uint16_t *)(c + IC_OUT_AREA_HW) = LL_OUT_W;
	*(uint16_t *)(c + IC_OUT_AREA_VW) = LL_OUT_H;
	*(uint16_t *)(c + IC_ZOOM) = 100;
	c[IC_MODE] = IC_MODE_RESIZE;
}

static void ll_run_job(enum ll_mode m, uint32_t lock)
{
	struct ll_stats *s = &ll.st[m];
	uint32_t disp = ll_rd32(LL_IMG1);

	if (!disp)
		return;
	ll_build(disp);
	uint32_t t0 = ll_now();
	unsigned int st = fw_img_exec(ll.ctrl);
	uint32_t dt = ll_since(t0);

	if (dt > ll.win_max)
		ll.win_max = dt;
	if (st || dt > LL_R_LOG_US || (lock & LL_LOCK_E1))
		fenykl_log_info(&ll_log, "R %u lk %x st %x m %u", dt, lock, st, m);
	if (st) {
		s->errors++;
		ll.errors++;
		fenykl_log_err(&ll_log, "resize status %x after %u us", st, dt);
		if (dt > LL_SLOW_US || ll.errors >= LL_MAX_ERRORS) {
			fenykl_log_err(&ll_log, "resize stopped (errors %u)", ll.errors);
			ll.dead = 1;
		}
		return;
	}
	s->run++;
	s->us_sum += dt;
	if (dt > s->us_max)
		s->us_max = dt;
}

static void ll_frame(enum ll_mode m)
{
	struct ll_stats *s = &ll.st[m];
	uint32_t lock = ll_rd32(LL_IPU_LOCK);

	s->frames++;
	if (m == LL_OFF || ll.dead)
		return;
	if (m == LL_GATED && ll.capture) {
		s->skip_gate++;
		return;
	}
	if ((m == LL_SKIP || m == LL_GATED) && (lock & LL_LOCK_E1)) {
		s->skip_busy++;
		return;
	}
	ll_run_job(m, lock);
}

static void ll_summary(void)
{
	for (unsigned int m = 0; m < LL_MODES; m++) {
		const struct ll_stats *s = &ll.st[m];

		fenykl_log_info(&ll_log, "SUM %s fd %u run %u err %u", ll_mode_name[m], s->frames, s->run,
				s->errors);
		fenykl_log_info(&ll_log, "SUM %s skipb %u skipg %u", ll_mode_name[m], s->skip_busy,
				s->skip_gate);
		fenykl_log_info(&ll_log, "SUM %s avg %u max %u", ll_mode_name[m],
				s->run ? s->us_sum / s->run : 0, s->us_max);
	}
	fenykl_log_info(&ll_log, "SUM lv n %u avg %u max %u", ll.lv_n, ll.lv_n ? ll.lv_sum / ll.lv_n : 0,
			ll.lv_max);
	fenykl_log_info(&ll_log, "SUM e1 %u other %u cpu1 %u evdrop %u", ll.e1_n, ll.other_n,
			*FENYKL_SHARED_PTR(ll_cpu1), ll.ev_dropped);
}

FENYKL_TASK(ll_rsz);
void ll_rsz(void)
{
	uint32_t pub = ll_rd32(LL_FD_PUB);
	uint32_t sum_t = ll_now();
	enum ll_mode cur = LL_MODES;
	uint8_t done = 0;

	for (;;) {
		uint32_t presses = ll.presses;
		enum ll_mode m = ll_mode_for(presses ? presses - 1 : 0); /* the shot of the last press */
		uint32_t a;

		if (m != cur) {
			ll_summary();
			fenykl_log_info(&ll_log, "MODE %s presses %u", ll_mode_name[m], presses);
			cur = m;
		}
		if (!done && presses >= LL_PRESSES_PER_PHASE * LL_PHASES && !ll.capture) {
			ll_summary();
			fenykl_log_info(&ll_log, "lvlock procedure done");
			done = 1;
		}
		if (ll_since(sum_t) > LL_SUM_US) {
			ll_summary();
			sum_t = ll_now();
		}
		a = ll_rd32(LL_FD_PUB);
		if (a && a != pub) {
			pub = a;
			ll_frame(m);
		}
		fw_OlyTaskSleep(1);
	}
}

static int ll_start_task(const char *name, void (*entry)(void), int prio)
{
	void *stack = fw_malloc(LL_STACK_SIZE);

	if (!stack) {
		fenykl_log_err(&ll_log, "no stack for %s", name);
		return -1;
	}
	if (fw_OlyTaskCreate(name, entry, 2, 0, prio, LL_STACK_SIZE, stack) < 0) {
		fenykl_log_err(&ll_log, "task %s not created", name);
		return -1;
	}
	return 0;
}

FENYKL_TASK(ll_main);
void ll_main(void)
{
	int r;

	fw_OlyTaskSleep(LL_START_MS);
	memset(ll.param, 0, sizeof(ll.param));
	ll_poll_init();
	r = fenykl_hook_install(&fenykl_hook_img_exec);
	if (r) {
		fenykl_log_err(&ll_log, "img_exec hook install failed %d", r);
		return;
	}
	r = fenykl_event_add(&ll_handler);
	if (!r)
		r = fenykl_event_install();
	if (r) {
		fenykl_log_err(&ll_log, "event layer failed %d", r);
		return;
	}
	if (ll_start_task("LLPOLL", ll_poll_task, LL_POLL_PRIO))
		return;
	if (ll_start_task("LLRSZ", ll_rsz_task, LL_RSZ_PRIO))
		return;
	fenykl_log_info(&ll_log, "lvlock ready: lock %x img1 %x fd %x", ll_rd32(LL_IPU_LOCK),
			ll_rd32(LL_IMG1), ll_rd32(LL_FD_PUB));
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);

	fenykl_log_task_start();
	fenykl_log_info(&ll_log, "lvlock start, load %x size %x, log init %d", load_base, image_size, r);
	ll_start_task("LVLOCK", ll_main_task, 12);
}
