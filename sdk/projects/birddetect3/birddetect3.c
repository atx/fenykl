/*
 * birddetect3: the fenykl-nn bird detector wide-k285 on CPU1, on IPU-resized colour live-view frames,
 * steering the camera's face frame and face-priority AF to the bird.
 *
 * The control task (CPU0, priority 12) polls the FD slot address the MOV task publishes once per
 * live-view frame (0x606067B0). On a new publish while the CPU1 worker (worker.c) is idle it resizes
 * the display buffer on screen (800x600 YUYV, live image x 0..539, y 0..479, the full 4:3 field) to
 * 96x72 YUYV with a standalone IPU job (img_exec ucMode 0x15 on engine 1, as the lvrsz probe ran it
 * at 160x120 and 320x240) and hands the frame to the worker. The job is skipped while a still is
 * in progress (a REC take handle with ps = 0xFF, from the job start to the file close) and while the
 * engine-1 lock bit is set: it never waits for the lock (the lvlock probe measured no cost to the
 * stills that way). The first jobs are checked for unwritten bytes and a guard overrun.
 *
 * For each result it runs the model's post-processing (inject.c: the frame fires if its top cell
 * reaches the model threshold; the point is a 3x3 centroid around that cell) and publishes the
 * point of a firing frame to the fd_hw_shutdown hook (hook.c), which makes it the only face
 * candidate, as is. It logs the self-test, one summary per second (with the fires) and the point
 * and score on change of the target.
 * Runs until power-off. Log: C:\FENYKL.LOG.
 */
#include "birddetect3.h"
#include "../birddetect/fd_frame.h"

#define BD3_PROP_FACE 0x9121u		/* u8 Face Priority */

#define BD3_STACK 0x2000u
#define BD3_SELFTEST_WAIT_MS 5000u
#define BD3_WIN_US 1000000u
#define BD3_QUIET_S 10u			/* "no FD frames" warning after this many silent seconds */
#define BD3_TGT_LOGS 4u			/* target change lines per second at most */
#define BD3_WIN_MAX 32u			/* results kept per second for the medians */
#define BD3_STAGE_LOG_S 5u		/* stage times every this many seconds */

/* The resize job. Addresses verified by the lvrsz and lvlock probes. */
#define BD3_IMG1 0x6034C880u		/* u32: display buffer on screen (img1 shadow), 800x600 YUYV */
#define BD3_IPU_LOCK 0x8FF00100u	/* firmware IPU lock word */
#define BD3_LOCK_E1 0x2u		/* engine 1 busy */
#define BD3_DISP_STRIDE 800u
#define BD3_DISP_W 540u
#define BD3_DISP_H 480u
#define BD3_RSZ_SLOW_US 100000u		/* an error status after this long: IPU timeout */
#define BD3_RSZ_MAX_ERRORS 3u
#define BD3_RSZ_VERBOSE 5u		/* jobs logged one by one */
#define BD3_RSZ_CHECKS 3u		/* jobs checked for unwritten bytes and the guard */
#define BD3_GUARD 64u
#define BD3_FILL 0xA5u
#define BD3_GUARD_BYTE 0x5Au

/* REC take-handle table: 25 entries of 0x18 bytes; ps = 0xFF from a still job's start (REC_MAIN)
   until its file is closed (REC_FILE). */
#define BD3_TH 0x6061062Cu
#define BD3_TH_N 25u
#define BD3_TH_SIZE 0x18u
#define BD3_TH_PS 0x15u
#define BD3_PS_BUSY 0xFFu

/* IMG_CTRL (0xC8 bytes), field names from the firmware's own debug dump (mov_dbg.c). */
#define IC_SIZE 0xC8u
#define IC_R1ADDR 0x00u
#define IC_W1ADDR 0x10u
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
#define IC_MODE_RESIZE 0x15u
#define IP_SIZE 0x80u			/* IMG_PARAM: zero = no picture processing */

/* Resize jobs in a window. */
struct bd3_rsz {
	uint32_t run, skip_gate, skip_busy, skip_disp, errors;
	uint32_t us_sum, us_max;
};

/* Per-second window. */
struct bd3_win {
	uint32_t pubs, results, fires, tgt_logs, tgt_skipped;
	int32_t best;			/* highest score */
	struct bd3_rsz rsz;
	uint32_t n;			/* entries in us[][] */
	uint32_t us[BD3_STAGES + 1][BD3_WIN_MAX];	/* stage times, then the wall time */
};

struct bd3_state {
	uint8_t ctrl[IC_SIZE] __attribute__((aligned(4)));
	uint8_t param[IP_SIZE] __attribute__((aligned(4)));
	uint32_t pub_addr;		/* last value of BD_FD_PUB */
	uint32_t bad, bad_last;
	bool pending;			/* a publish not yet used */
	uint32_t seq;			/* requests made */
	uint32_t taken;			/* last request whose result was taken */
	uint32_t jobs, errors;		/* resize jobs run, failed */
	bool dead;			/* resizing stopped */
	uint32_t target;		/* last target word (inject.h), 0 = none */
	int prop;			/* Face Priority, -1 = no prop data */
	uint32_t quiet_s;		/* seconds without an FD publish */
	uint32_t t_win, secs;
	uint32_t last_cycles, last_injected, last_cleared;
	struct bd3_win win;
};

FENYKL_SHARED struct bd3_shared bd3_shared;

static struct bd3_state bd3 = { .prop = -2 };

/* The resize output: written by the IPU only, read by CPU1 (cached, after a purge) and by CPU0's
   checks through the uncached alias; whole 64-byte lines, so no line is shared. */
static uint8_t bd3_frame[BD3_FRAME_BYTES + BD3_GUARD] __attribute__((aligned(64)));

static FENYKL_LOG_MODULE(bd3_mod, "birddet3", FENYKL_LOG_INFO);

void bd3_worker_cpu1(void);	/* FENYKL_CPU1_WORKER(bd3_worker) in worker.c */

static uint32_t bd3_rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

static uint8_t bd3_rd8(uint32_t a)
{
	return *(volatile uint8_t *)(uintptr_t)a;
}

/* A changed BD_FD_PUB is one publish: true for a valid slot (the newest is in bd3.pub_addr). */
static bool bd3_poll(void)
{
	uint32_t a = bd3_rd32(BD_FD_PUB);

	if (a == bd3.pub_addr)
		return false;
	bd3.pub_addr = a;
	bd3.win.pubs++;
	if (bd_fd_is_slot(a))
		return true;
	bd3.bad++;
	bd3.bad_last = a;
	return false;
}

static bool bd3_capturing(void)
{
	for (uint32_t i = 0; i < BD3_TH_N; i++)
		if (bd3_rd8(BD3_TH + i * BD3_TH_SIZE + BD3_TH_PS) == BD3_PS_BUSY)
			return true;
	return false;
}

/* IMG_CTRL like the stock YC resize job of REC_IMAGE (0x6E8DF67C), engine 1 (ulFlag 0). */
static void bd3_build(uint32_t disp)
{
	uint8_t *c = bd3.ctrl;

	memset(c, 0, sizeof(bd3.ctrl));
	*(uint32_t *)(c + IC_R1ADDR) = (uint32_t)FENYKL_UNCACHED(disp);
	*(uint32_t *)(c + IC_W1ADDR) = (uint32_t)FENYKL_UNCACHED(bd3_frame);
	*(uint32_t *)(c + IC_PARAM) = (uint32_t)bd3.param;
	*(uint16_t *)(c + IC_IN_MEM_HW) = BD3_DISP_STRIDE;
	*(uint16_t *)(c + IC_IN_USE_HW) = BD3_DISP_W;
	*(uint16_t *)(c + IC_IN_USE_VW) = BD3_DISP_H;
	*(uint16_t *)(c + IC_IN_AREA_HW) = BD3_DISP_W;
	*(uint16_t *)(c + IC_IN_AREA_VW) = BD3_DISP_H;
	*(uint16_t *)(c + IC_IN_AREA_HC) = BD3_DISP_W / 2;
	*(uint16_t *)(c + IC_IN_AREA_VC) = BD3_DISP_H / 2;
	*(uint16_t *)(c + IC_OUT_WHOLE_HW) = BD3_IN_W;
	*(uint16_t *)(c + IC_OUT_WHOLE_VW) = BD3_IN_H;
	*(uint16_t *)(c + IC_OUT_AREA_HW) = BD3_IN_W;
	*(uint16_t *)(c + IC_OUT_AREA_VW) = BD3_IN_H;
	*(uint16_t *)(c + IC_ZOOM) = 100;
	c[IC_MODE] = IC_MODE_RESIZE;
}

static void bd3_fill(void)
{
	volatile uint8_t *p = FENYKL_UNCACHED(bd3_frame);

	for (uint32_t i = 0; i < BD3_FRAME_BYTES; i++)
		p[i] = BD3_FILL;
	for (uint32_t i = 0; i < BD3_GUARD; i++)
		p[BD3_FRAME_BYTES + i] = BD3_GUARD_BYTE;
}

/* False if the job wrote past the frame or left most of it unwritten. */
static bool bd3_check_out(void)
{
	const volatile uint8_t *p = FENYKL_UNCACHED(bd3_frame);
	uint32_t fill = 0, guard = 0, sum[4] = { 0 };

	for (uint32_t i = 0; i < BD3_FRAME_BYTES; i++) {
		fill += p[i] == BD3_FILL;
		sum[i & 3] += p[i];
	}
	for (uint32_t i = 0; i < BD3_GUARD; i++)
		guard += p[BD3_FRAME_BYTES + i] != BD3_GUARD_BYTE;
	fenykl_log_info(&bd3_mod, "rsz check %u: unwritten %u of %u, guard bad %u", bd3.jobs, fill,
			BD3_FRAME_BYTES, guard);
	fenykl_log_info(&bd3_mod, "rsz check %u: mean Y %u U %u V %u", bd3.jobs,
			(sum[0] + sum[2]) / (BD3_FRAME_BYTES / 2), sum[1] / (BD3_FRAME_BYTES / 4),
			sum[3] / (BD3_FRAME_BYTES / 4));
	if (guard) {
		fenykl_log_err(&bd3_mod, "resize wrote past the 96x72 frame");
		return false;
	}
	if (fill > BD3_FRAME_BYTES / 2) {
		fenykl_log_err(&bd3_mod, "resize left the frame unwritten");
		return false;
	}
	return true;
}

static void bd3_request(volatile struct bd3_shared *s)
{
	s->req_addr = (uint32_t)FENYKL_UNCACHED(bd3_frame);
	s->req_t = BD3_NOW();
	s->req_seq = ++bd3.seq;
}

static void bd3_resize_stop(const char *why)
{
	int r = fenykl_hook_remove(&fenykl_hook_fd_hw_shutdown);

	bd3.dead = true;
	bd3_hook.target = 0;
	fenykl_log_err(&bd3_mod, "resize stopped (%s), errors %u: no detection, hook remove %d", why,
			bd3.errors, r);
}

/* One resize into bd3_frame, then the request, unless skipped. */
static void bd3_resize(volatile struct bd3_shared *s)
{
	struct bd3_rsz *w = &bd3.win.rsz;
	bool check = bd3.jobs < BD3_RSZ_CHECKS;
	uint32_t lock, disp, t0, dt;
	unsigned int st;

	if (bd3_capturing()) {
		w->skip_gate++;
		return;
	}
	lock = bd3_rd32(BD3_IPU_LOCK);
	if (lock & BD3_LOCK_E1) {
		w->skip_busy++;
		return;
	}
	disp = bd3_rd32(BD3_IMG1);
	if (!disp) {
		w->skip_disp++;
		return;
	}
	if (check)
		bd3_fill();
	bd3_build(disp);
	t0 = BD3_NOW();
	st = fw_img_exec(bd3.ctrl);
	dt = BD3_ELAPSED(t0, BD3_NOW());
	bd3.jobs++;
	if (bd3.jobs <= BD3_RSZ_VERBOSE) {
		fenykl_log_info(&bd3_mod, "rsz %u: disp %08x lock %x", bd3.jobs, disp, lock);
		fenykl_log_info(&bd3_mod, "rsz %u: status %x %u us", bd3.jobs, st, dt);
	}
	if (st) {
		w->errors++;
		bd3.errors++;
		fenykl_log_err(&bd3_mod, "resize status %x after %u us", st, dt);
		if (dt > BD3_RSZ_SLOW_US || bd3.errors >= BD3_RSZ_MAX_ERRORS)
			bd3_resize_stop("errors");
		return;
	}
	if (check && !bd3_check_out()) {
		bd3_resize_stop("bad output");
		return;
	}
	w->run++;
	w->us_sum += dt;
	if (dt > w->us_max)
		w->us_max = dt;
	bd3_request(s);
}

static void bd3_publish_target(uint32_t target)
{
	if (!target) {
		bd3_hook.target = 0;
		return;
	}
	bd3_hook.target_us = BD3_NOW();
	bd3_hook.target = target;
}

static void bd3_log_target(uint32_t target, const struct bd3_det *d)
{
	if (target == bd3.target)
		return;
	bd3.target = target;
	if (bd3.win.tgt_logs >= BD3_TGT_LOGS) {
		bd3.win.tgt_skipped++;
		return;
	}
	bd3.win.tgt_logs++;
	if (!target)
		fenykl_log_info(&bd3_mod, "bird none score %d", d->score);
	else
		fenykl_log_info(&bd3_mod, "bird %u,%u score %d", target & 0xFFFFu,
				(target >> 16) & 0x7FFFu, d->score);
}

static void bd3_take_result(volatile struct bd3_shared *s)
{
	struct bd3_win *w = &bd3.win;
	int32_t logit[BD3_CELLS];
	struct bd3_det d;
	uint32_t target;

	for (unsigned int i = 0; i < BD3_CELLS; i++)
		logit[i] = s->logit[i];
	bd3_post(logit, &d);
	target = d.fire ? bd3_inj_target(d.x_q4, d.y_q4) : 0;
	if (w->results == 0 || d.score > w->best)
		w->best = d.score;
	if (!bd3.dead)
		bd3_publish_target(target);
	if (w->n < BD3_WIN_MAX) {
		for (unsigned int i = 0; i < BD3_STAGES; i++)
			w->us[i][w->n] = s->us[i];
		w->us[BD3_STAGES][w->n] = s->wall_us;
		w->n++;
	}
	w->results++;
	if (d.fire)
		w->fires++;
	bd3_log_target(target, &d);
}

/* Median (upper) of n values; sorts v. */
static uint32_t bd3_median(uint32_t *v, uint32_t n)
{
	for (uint32_t i = 1; i < n; i++) {
		uint32_t x = v[i];
		uint32_t j = i;

		for (; j > 0 && v[j - 1] > x; j--)
			v[j] = v[j - 1];
		v[j] = x;
	}
	return n ? v[n / 2] : 0;
}

static void bd3_poll_prop(void)
{
	const volatile uint8_t *p = fw_prop_get_obj(BD3_PROP_FACE);
	int v = p ? p[0] : -1;

	if (v == bd3.prop)
		return;
	bd3.prop = v;
	fenykl_log_info(&bd3_mod, "face priority %d%s", v,
			v == 0 ? ": FD loop stopped, no face frame to steer" : "");
}

/* Medians of this window's stage times (us, CPU1 work without sleeps). */
static void bd3_log_stages(volatile struct bd3_shared *s, const uint32_t *med)
{
	fenykl_log_info(&bd3_mod, "  us in %u l0 %u l1 %u l2 %u", med[BD3_ST_INPUT],
			med[BD3_ST_L0], med[BD3_ST_L1], med[BD3_ST_L2]);
	fenykl_log_info(&bd3_mod, "  us l3 %u l4 %u l5 %u l6 %u", med[BD3_ST_L3], med[BD3_ST_L4],
			med[BD3_ST_L4 + 1], med[BD3_ST_L4 + 2]);
	fenykl_log_info(&bd3_mod, "  us l7 %u l8 %u l9 %u wall %u", med[BD3_ST_L4 + 3],
			med[BD3_ST_L4 + 4], med[BD3_ST_L9], med[BD3_STAGES]);
	fenykl_log_info(&bd3_mod, "  us lat %u slice %u", s->lat_us, s->slice_max_us);
}

static void bd3_log_win(volatile struct bd3_shared *s)
{
	struct bd3_win *w = &bd3.win;
	const struct bd3_rsz *r = &w->rsz;
	uint32_t med[BD3_STAGES + 1];
	uint32_t cycles = bd3_hook.cycles, inj = bd3_hook.injected, clr = bd3_hook.cleared;

	bd3.secs++;
	if (!w->pubs) {
		bd3.quiet_s++;
		if (bd3.quiet_s == BD3_QUIET_S)
			fenykl_log_warn(&bd3_mod, "t=%us no FD frames for %u s (not in live view?)",
					bd3.secs, BD3_QUIET_S);
	} else {
		if (bd3.quiet_s >= BD3_QUIET_S)
			fenykl_log_info(&bd3_mod, "t=%us FD frames again", bd3.secs);
		bd3.quiet_s = 0;
	}
	if (w->pubs || cycles != bd3.last_cycles) {
		for (unsigned int i = 0; i <= BD3_STAGES; i++)
			med[i] = bd3_median(w->us[i], w->n);
		fenykl_log_info(&bd3_mod, "t=%us inf %u fire %u best %d", bd3.secs, w->results,
				w->fires, w->results ? w->best : 0);
		fenykl_log_info(&bd3_mod, "  fd %u inj %u clr %u pubs %u", cycles - bd3.last_cycles,
				inj - bd3.last_injected, clr - bd3.last_cleared, w->pubs);
		fenykl_log_info(&bd3_mod, "  rsz %u us avg %u max %u err %u", r->run,
				r->run ? r->us_sum / r->run : 0, r->us_max, r->errors);
		if (r->skip_gate || r->skip_busy || r->skip_disp)
			fenykl_log_info(&bd3_mod, "  rsz skip cap %u busy %u disp %u", r->skip_gate,
					r->skip_busy, r->skip_disp);
		if (bd3.secs % BD3_STAGE_LOG_S == 0)
			bd3_log_stages(s, med);
		if (w->tgt_skipped)
			fenykl_log_info(&bd3_mod, "  target changes not logged %u", w->tgt_skipped);
	}
	bd3.last_cycles = cycles;
	bd3.last_injected = inj;
	bd3.last_cleared = clr;
	*w = (struct bd3_win){ 0 };
}

/* Up to wait_s seconds for three publishes, so the address really moves. */
static bool bd3_wait_fd(uint32_t wait_s)
{
	uint32_t t0 = BD3_NOW(), n = 0;

	bd3.pub_addr = bd3_rd32(BD_FD_PUB);
	while (n < 3 && BD3_ELAPSED(t0, BD3_NOW()) < wait_s * 1000000u) {
		if (bd3_poll())
			n++;
		fw_OlyTaskSleep(1);
	}
	return n >= 3;
}

static bool bd3_selftest(volatile struct bd3_shared *s)
{
	uint32_t ms;
	bool ok = true;

	for (ms = 0; s->phase < 1; ms += 10) {
		if (ms >= BD3_SELFTEST_WAIT_MS) {
			fenykl_log_err(&bd3_mod, "self-test: no result in %u ms", ms);
			return false;
		}
		fw_OlyTaskSleep(10);
	}
	for (unsigned int p = 0; p < BD3_PATTERNS; p++) {
		bool eq = s->selftest[p] == bd3_selftest_sum[p];

		fenykl_log_info(&bd3_mod, "self-test %u: %08x want %08x %s", p, s->selftest[p],
				bd3_selftest_sum[p], eq ? "ok" : "BAD");
		ok = ok && eq;
	}
	fenykl_log_info(&bd3_mod, "self-test %s after %u ms (worker core %u, %u us per inference)",
			ok ? "PASS" : "FAIL", ms, s->core_id, s->selftest_us);
	return ok;
}

static void bd3_run(volatile struct bd3_shared *s)
{
	bd3.t_win = BD3_NOW();
	bd3.last_cycles = bd3_hook.cycles;
	for (;;) {
		bool idle = s->res_seq == bd3.seq;

		if (bd3_poll())
			bd3.pending = true;
		if (idle && bd3.taken != bd3.seq) {
			bd3_take_result(s);
			bd3.taken = bd3.seq;
		}
		if (idle && bd3.pending && !bd3.dead) {
			bd3.pending = false;
			bd3_resize(s);
		}
		if (BD3_ELAPSED(bd3.t_win, BD3_NOW()) >= BD3_WIN_US) {
			bd3.t_win += BD3_WIN_US;
			bd3_poll_prop();
			bd3_log_win(s);
		}
		fw_OlyTaskSleep(1);
	}
}

FENYKL_TASK(bd3_control);
void bd3_control(void)
{
	volatile struct bd3_shared *s = FENYKL_SHARED_PTR(bd3_shared);
	int r;

	bd3_poll_prop();
	while (!bd3_wait_fd(BD3_QUIET_S))
		fenykl_log_warn(&bd3_mod, "no FD frames for %u s: waiting for live view", BD3_QUIET_S);
	fenykl_log_info(&bd3_mod, "fd: pub %08x, bad %u last bad %08x", bd3.pub_addr, bd3.bad,
			bd3.bad_last);
	fenykl_log_info(&bd3_mod, "frame %08x lock %x", (uint32_t)bd3_frame,
			bd3_rd32(BD3_IPU_LOCK));
	r = fenykl_cpu1_start(bd3_worker_cpu1, 0);
	fenykl_log_info(&bd3_mod, "cpu1 start %d (expect 0)", r);
	if (r)
		return;
	if (!bd3_selftest(s)) {
		r = fenykl_cpu1_stop(1000);
		fenykl_log_err(&bd3_mod, "no detection: worker stopped %d, FD left alone", r);
		return;
	}
	r = bd3_hook_install();
	fenykl_log_info(&bd3_mod, "hook fd_hw_shutdown install %d (expect 0)", r);
	if (r) {
		r = fenykl_cpu1_stop(1000);
		fenykl_log_err(&bd3_mod, "no hook: worker stopped %d", r);
		return;
	}
	bd3_run(s);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);
	void *stack;

	fenykl_log_task_start();
	fenykl_log_info(&bd3_mod, "birddetect3, load %08x size %x, log init %d", load_base,
			image_size, r);
	fenykl_log_info(&bd3_mod, "model %s (fenykl-nn %s) threshold %d", bd3_model_name,
			bd3_model_commit, bd3_threshold);
	stack = fw_malloc(BD3_STACK);
	if (!stack) {
		fenykl_log_err(&bd3_mod, "no stack");
		return;
	}
	/* Priority 12: fenykl_cpu1_start/stop spin on CPU0 (never above PER_WDT, 11). */
	r = fw_OlyTaskCreate("BIRDDET3", bd3_control_task, 2, 0, 12, BD3_STACK, stack);
	fenykl_log_info(&bd3_mod, "task BIRDDET3 pri 12: %d", r);
}
