/*
 * birddetect: the fenykl-nn bird detector on CPU1, on the live-view FD frames, steering the
 * camera's face frame and face-priority AF to the bird.
 *
 * The control task (CPU0, priority 12) polls the FD slot address the MOV task publishes once per
 * live-view frame (0x606067B0) and hands the newest slot to the CPU1 worker (worker.c)
 * whenever it is idle; publishes in between are dropped. For each result it picks the bird (the
 * strongest local maximum >= BD_THRESHOLD, fenykl-nn report 3.1) and publishes its cell centre to
 * the fd_hw_shutdown hook (hook.c), which makes it the only face candidate. It logs the self-test,
 * one summary per second and the chosen cell on change. Runs until power-off. Log: C:\FENYKL.LOG.
 */
#include "birddetect.h"

#define BD_FD_PUB 0x606067B0u		/* u32: FD slot published by mov_fd_publish */
#define BD_FD_SLOT0 0xADC56300u		/* ROM table 0x6E9EA9EC: 5 slots of 0x4B000 bytes */
#define BD_FD_SLOT_SIZE 0x4B000u
#define BD_FD_SLOTS 5u
#define BD_PROP_FACE 0x9121u		/* u8 Face Priority */

#define BD_STACK 0x2000u
#define BD_SELFTEST_WAIT_MS 5000u
#define BD_WIN_US 1000000u
#define BD_QUIET_S 10u			/* "no FD frames" warning after this many silent seconds */
#define BD_CELL_LOGS 4u			/* cell change lines per second at most */
#define BD_WIN_MAX 32u			/* results kept per second for the medians */
#define BD_NO_CELL 0xFFu

/* Per-second window. */
struct bd_win {
	uint32_t pubs, dropped, results, birds, cycles, injected, cleared, cell_logs, cell_skipped;
	int32_t best;			/* strongest logit of any cell */
	uint32_t n;			/* entries in us[][] */
	uint16_t us[BD_STAGES + 1][BD_WIN_MAX];	/* stage times, then the wall time */
};

struct bd_state {
	uint32_t pub_addr;		/* last value of BD_FD_PUB */
	uint32_t bad, bad_last;
	uint32_t seq;			/* requests made */
	uint32_t taken;			/* last request whose result was taken */
	uint8_t cell;			/* last chosen cell, BD_NO_CELL = none */
	int prop;			/* Face Priority, -1 = no prop data */
	uint32_t quiet_s;		/* seconds without an FD publish */
	uint32_t t_win, secs;
	uint32_t last_cycles, last_injected, last_cleared;
	struct bd_win win;
};

FENYKL_SHARED struct bd_shared bd_shared;

static struct bd_state bd = { .cell = BD_NO_CELL, .prop = -2 };

static FENYKL_LOG_MODULE(bd_mod, "birddet", FENYKL_LOG_INFO);

void bd_worker_cpu1(void);	/* FENYKL_CPU1_WORKER(bd_worker) in worker.c */

static uint32_t bd_rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

static bool bd_is_slot(uint32_t addr)
{
	uint32_t off = (uint32_t)FENYKL_UNCACHED(addr) - BD_FD_SLOT0;

	return off % BD_FD_SLOT_SIZE == 0 && off / BD_FD_SLOT_SIZE < BD_FD_SLOTS;
}

/* A changed BD_FD_PUB is one publish: true for a valid slot (the newest is in bd.pub_addr). */
static bool bd_poll(void)
{
	uint32_t a = bd_rd32(BD_FD_PUB);

	if (a == bd.pub_addr)
		return false;
	bd.pub_addr = a;
	bd.win.pubs++;
	if (bd_is_slot(a))
		return true;
	bd.bad++;
	bd.bad_last = a;
	return false;
}

static void bd_request(volatile struct bd_shared *s)
{
	s->req_addr = (uint32_t)FENYKL_UNCACHED(bd.pub_addr);
	s->req_t = BD_NOW();
	s->req_seq = ++bd.seq;
}

/* fenykl-nn report 3.1 / 4.2: the strongest cell that is >= T and >= its up to 8 neighbours (first
   in raster order on a tie), or BD_NO_CELL. */
static uint8_t bd_pick(const int32_t *l)
{
	uint8_t best = BD_NO_CELL;

	for (int y = 0; y < (int)BD_GRID_H; y++) {
		for (int x = 0; x < (int)BD_GRID_W; x++) {
			int32_t v = l[y * BD_GRID_W + x];
			bool peak = v >= BD_THRESHOLD;

			for (int dy = -1; peak && dy <= 1; dy++) {
				for (int dx = -1; dx <= 1; dx++) {
					int ny = y + dy, nx = x + dx;

					if (ny < 0 || ny >= (int)BD_GRID_H || nx < 0 ||
					    nx >= (int)BD_GRID_W)
						continue;
					if (l[ny * BD_GRID_W + nx] > v)
						peak = false;
				}
			}
			if (peak && (best == BD_NO_CELL || v > l[best]))
				best = (uint8_t)(y * BD_GRID_W + x);
		}
	}
	return best;
}

static void bd_publish_target(uint8_t cell)
{
	if (cell == BD_NO_CELL) {
		bd_hook.target = 0;
		return;
	}
	bd_hook.target_us = BD_NOW();
	bd_hook.target = bd_inj_target(cell % BD_GRID_W, cell / BD_GRID_W);
}

static void bd_log_cell(uint8_t cell, int32_t logit)
{
	if (cell == bd.cell)
		return;
	bd.cell = cell;
	if (bd.win.cell_logs >= BD_CELL_LOGS) {
		bd.win.cell_skipped++;
		return;
	}
	bd.win.cell_logs++;
	if (cell == BD_NO_CELL)
		fenykl_log_info(&bd_mod, "cell none");
	else
		fenykl_log_info(&bd_mod, "cell %u,%u logit %d", cell % BD_GRID_W, cell / BD_GRID_W,
				logit);
}

static void bd_take_result(volatile struct bd_shared *s)
{
	struct bd_win *w = &bd.win;
	int32_t logit[BD_CELLS];
	uint8_t cell;

	for (unsigned int i = 0; i < BD_CELLS; i++) {
		logit[i] = s->logit[i];
		if ((w->results == 0 && i == 0) || logit[i] > w->best)
			w->best = logit[i];
	}
	cell = bd_pick(logit);
	bd_publish_target(cell);
	if (w->n < BD_WIN_MAX) {
		for (unsigned int i = 0; i < BD_STAGES; i++)
			w->us[i][w->n] = (uint16_t)(s->us[i] > 0xFFFF ? 0xFFFF : s->us[i]);
		w->us[BD_STAGES][w->n] = (uint16_t)(s->wall_us > 0xFFFF ? 0xFFFF : s->wall_us);
		w->n++;
	}
	w->results++;
	if (cell != BD_NO_CELL)
		w->birds++;
	bd_log_cell(cell, cell == BD_NO_CELL ? 0 : logit[cell]);
}

/* Median (upper) and maximum of n values; sorts v. */
static void bd_med_max(uint16_t *v, uint32_t n, uint32_t *med, uint32_t *max)
{
	for (uint32_t i = 1; i < n; i++) {
		uint16_t x = v[i];
		uint32_t j = i;

		for (; j > 0 && v[j - 1] > x; j--)
			v[j] = v[j - 1];
		v[j] = x;
	}
	*med = n ? v[n / 2] : 0;
	*max = n ? v[n - 1] : 0;
}

static void bd_poll_prop(void)
{
	const volatile uint8_t *p = fw_prop_get_obj(BD_PROP_FACE);
	int v = p ? p[0] : -1;

	if (v == bd.prop)
		return;
	bd.prop = v;
	fenykl_log_info(&bd_mod, "face priority %d%s", v,
			v == 0 ? ": FD loop stopped, no face frame to steer" : "");
}

static void bd_log_win(volatile struct bd_shared *s)
{
	struct bd_win *w = &bd.win;
	uint32_t med[BD_STAGES + 1], max[BD_STAGES + 1];
	uint32_t cycles = bd_hook.cycles, inj = bd_hook.injected, clr = bd_hook.cleared;

	bd.secs++;
	if (!w->pubs) {
		bd.quiet_s++;
		if (bd.quiet_s == BD_QUIET_S)
			fenykl_log_warn(&bd_mod, "t=%us no FD frames for %u s (not in live view?)",
					bd.secs, BD_QUIET_S);
	} else {
		if (bd.quiet_s >= BD_QUIET_S)
			fenykl_log_info(&bd_mod, "t=%us FD frames again", bd.secs);
		bd.quiet_s = 0;
	}
	if (w->pubs || cycles != bd.last_cycles) {
		for (unsigned int i = 0; i <= BD_STAGES; i++)
			bd_med_max(w->us[i], w->n, &med[i], &max[i]);
		fenykl_log_info(&bd_mod, "t=%us inf %u drop %u bird %u", bd.secs, w->results,
				w->dropped, w->birds);
		fenykl_log_info(&bd_mod, "  best %d fd %u inj %u clr %u", w->results ? w->best : 0,
				cycles - bd.last_cycles, inj - bd.last_injected,
				clr - bd.last_cleared);
		fenykl_log_info(&bd_mod, "  us sample %u/%u stem %u/%u", med[BD_ST_SAMPLE],
				max[BD_ST_SAMPLE], med[BD_ST_STEM], max[BD_ST_STEM]);
		fenykl_log_info(&bd_mod, "  us l2 %u/%u trunk %u/%u", med[BD_ST_L2], max[BD_ST_L2],
				med[BD_ST_TRUNK], max[BD_ST_TRUNK]);
		fenykl_log_info(&bd_mod, "  us wall %u/%u lat %u slice %u", med[BD_STAGES],
				max[BD_STAGES], s->lat_us, s->slice_max_us);
		if (w->cell_skipped)
			fenykl_log_info(&bd_mod, "  cell changes not logged %u", w->cell_skipped);
	}
	bd.last_cycles = cycles;
	bd.last_injected = inj;
	bd.last_cleared = clr;
	*w = (struct bd_win){ 0 };
}

/* Up to wait_s seconds for three publishes, so the address really moves. */
static bool bd_wait_fd(uint32_t wait_s)
{
	uint32_t t0 = BD_NOW(), n = 0;

	bd.pub_addr = bd_rd32(BD_FD_PUB);
	while (n < 3 && BD_ELAPSED(t0, BD_NOW()) < wait_s * 1000000u) {
		if (bd_poll())
			n++;
		fw_OlyTaskSleep(1);
	}
	return n >= 3;
}

static bool bd_selftest(volatile struct bd_shared *s)
{
	uint32_t ms;
	bool ok = true;

	for (ms = 0; s->phase < 1; ms += 10) {
		if (ms >= BD_SELFTEST_WAIT_MS) {
			fenykl_log_err(&bd_mod, "self-test: no result in %u ms", ms);
			return false;
		}
		fw_OlyTaskSleep(10);
	}
	for (unsigned int p = 0; p < BD_PATTERNS; p++) {
		bool eq = s->selftest[p] == bd_selftest_sum[p];

		fenykl_log_info(&bd_mod, "self-test %u: %08x want %08x %s", p, s->selftest[p],
				bd_selftest_sum[p], eq ? "ok" : "BAD");
		ok = ok && eq;
	}
	fenykl_log_info(&bd_mod, "self-test %s after %u ms (worker core %u)", ok ? "PASS" : "FAIL",
			ms, s->core_id);
	return ok;
}

static void bd_run(volatile struct bd_shared *s)
{
	bd.t_win = BD_NOW();
	bd.last_cycles = bd_hook.cycles;
	for (;;) {
		bool fresh = bd_poll();
		bool idle = s->res_seq == bd.seq;

		if (idle && bd.taken != bd.seq) {
			bd_take_result(s);
			bd.taken = bd.seq;
		}
		if (fresh && idle)
			bd_request(s);
		else if (fresh)
			bd.win.dropped++;
		if (BD_ELAPSED(bd.t_win, BD_NOW()) >= BD_WIN_US) {
			bd.t_win += BD_WIN_US;
			bd_poll_prop();
			bd_log_win(s);
		}
		fw_OlyTaskSleep(1);
	}
}

FENYKL_TASK(bd_control);
void bd_control(void)
{
	volatile struct bd_shared *s = FENYKL_SHARED_PTR(bd_shared);
	int r;

	bd_poll_prop();
	while (!bd_wait_fd(BD_QUIET_S))
		fenykl_log_warn(&bd_mod, "no FD frames for %u s: waiting for live view", BD_QUIET_S);
	fenykl_log_info(&bd_mod, "fd: pub %08x, bad %u last bad %08x", bd.pub_addr, bd.bad,
			bd.bad_last);
	r = fenykl_cpu1_start(bd_worker_cpu1, 0);
	fenykl_log_info(&bd_mod, "cpu1 start %d (expect 0)", r);
	if (r)
		return;
	if (!bd_selftest(s)) {
		r = fenykl_cpu1_stop(1000);
		fenykl_log_err(&bd_mod, "no detection: worker stopped %d, FD left alone", r);
		return;
	}
	r = bd_hook_install();
	fenykl_log_info(&bd_mod, "hook fd_hw_shutdown install %d (expect 0)", r);
	if (r) {
		r = fenykl_cpu1_stop(1000);
		fenykl_log_err(&bd_mod, "no hook: worker stopped %d", r);
		return;
	}
	bd_run(s);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);
	void *stack;

	fenykl_log_task_start();
	fenykl_log_info(&bd_mod, "birddetect, load %08x size %x, log init %d", load_base,
			image_size, r);
	fenykl_log_info(&bd_mod, "model %s (fenykl-nn %s)", bd_model_name, bd_model_commit);
	stack = fw_malloc(BD_STACK);
	if (!stack) {
		fenykl_log_err(&bd_mod, "no stack");
		return;
	}
	/* Priority 12: fenykl_cpu1_start/stop spin on CPU0 (never above PER_WDT, 11). */
	r = fw_OlyTaskCreate("BIRDDET", bd_control_task, 2, 0, 12, BD_STACK, stack);
	fenykl_log_info(&bd_mod, "task BIRDDET pri 12: %d", r);
}
