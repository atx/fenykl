/*
 * The CPU1 harness (plugin.toml cpu1_sources) and worker. Per case: the data and the reference
 * output (cases.c), then per variant one calibration call, NB_REPS timed reps of `iters` calls
 * (iters chosen so a rep takes about NB_REP_US), a 1-tick sleep after each rep, and a check run
 * on a poisoned output buffer. Long kernels call the tick once per output row; it sleeps a tick
 * whenever NB_SLICE_US of work have passed and the sleep is taken out of the time, so the worker
 * blocks at least every ~5 ms (CPU1 rule: every ~10 ms). Live modes time the kernel on the newest
 * FD frame CPU0 publishes in nb_sh.fd_addr (re-read before every rep); cases.c checks them.
 */
#include "log.h"
#include "nnbench.h"

#define NB_REP_US 4000u
#define NB_REPS 5
#define NB_REPS_LONG 3		/* calls over NB_LONG_US */
#define NB_LONG_US 50000u
#define NB_ITERS_MAX 4000u
#define NB_SLICE_US 5000u

#define NB_NOW() FENYKL_LOG_TIMER_NOW()
#define NB_ELAPSED(a, b) FENYKL_LOG_ELAPSED(a, b)

FENYKL_SHARED struct nb_shared nb_sh;

/* The IPU writes the slots straight to DRAM: read cached after a purge on this core. */
const uint8_t *nb_live_frame(void)
{
	uint32_t a = FENYKL_SHARED_PTR(nb_sh)->fd_addr;

	return a ? (const uint8_t *)((a & 0x3FFFFFFFu) | 0x40000000u) : NULL;
}

void nb_live_purge(void)
{
	fw_dcache_purge_all_irqsafe();
}

struct nb_clock {
	uint32_t slice;		/* end of the last sleep */
	uint32_t excl;		/* sleeps inside timed work */
};

static void nb_tick_sleep(struct nb_run *r)
{
	struct nb_clock *c = r->tick_ctx;
	uint32_t now = NB_NOW();

	if (NB_ELAPSED(c->slice, now) < NB_SLICE_US)
		return;
	fw_OlyTaskSleep(1);
	c->slice = NB_NOW();
	c->excl += NB_ELAPSED(now, c->slice);
}

static void nb_sleep(struct nb_clock *c)
{
	fw_OlyTaskSleep(1);
	c->slice = NB_NOW();
}

/* Microseconds of `iters` calls, sleeps excluded; cold and live: a purge before each call, not
   timed. */
static uint32_t nb_time(struct nb_run *r, struct nb_clock *c, uint32_t iters)
{
	uint32_t us = 0, excl = c->excl, t0;
	int purge = r->k->mode == NB_COLD || r->k->mode == NB_LIVE;

	if (r->k->mode >= NB_LIVE) {
		const uint8_t *f = nb_live_frame();

		if (f)
			r->rin = r->k->mode == NB_LIVE ? f : FENYKL_UNCACHED(f);
	}
	t0 = NB_NOW();
	for (uint32_t i = 0; i < iters; i++) {
		if (purge) {
			fw_dcache_purge_all_irqsafe();
			t0 = NB_NOW();
		}
		r->v->run(r);
		if (purge)
			us += NB_ELAPSED(t0, NB_NOW());
	}
	if (!purge)
		us = NB_ELAPSED(t0, NB_NOW());
	return us - (c->excl - excl);
}

static void nb_measure(struct nb_run *r, struct nb_clock *c, struct nb_row *row)
{
	uint32_t us[NB_REPS], one, iters, reps;

	if (r->k->mode == NB_UNCACHED) {
		fw_dcache_purge_all_irqsafe(); /* the prepared input to DRAM */
		r->rin = FENYKL_UNCACHED(r->in);
	}
	nb_sleep(c);
	one = nb_time(r, c, 1);
	iters = one ? NB_REP_US / one : NB_ITERS_MAX;
	iters = iters < 1 ? 1 : iters > NB_ITERS_MAX ? NB_ITERS_MAX : iters;
	reps = one > NB_LONG_US ? NB_REPS_LONG : NB_REPS;
	for (uint32_t i = 0; i < reps; i++) {
		nb_sleep(c);
		us[i] = nb_time(r, c, iters);
	}
	nb_sleep(c);
	row->bad = nb_var_check(r);
	row->status = row->bad ? NB_BAD : NB_OK;
	if (r->k->mode >= NB_LIVE) {
		row->got = r->live_addr;
		row->want = r->live_hash;
		row->got2 = r->live_tries;
		if (!r->live_tries)
			row->status = NB_NOCHK;
	}
	row->iters = iters;
	row->reps = (uint8_t)reps;
	row->us_min = nb_min(us, reps);
	row->us_med = nb_median(us, reps);
	row->ops = r->ops;
	row->ops2 = r->ops2;
	row->code = r->code;
	row->nblk = r->nblk;
	for (uint32_t i = 0; i < r->nblk; i++)
		row->blk[i] = r->blk[i];
}

static void nb_push(volatile struct nb_shared *sh, const struct nb_row *row)
{
	while (sh->head - sh->tail >= NB_RING) {
		if (fenykl_cpu1_stop_requested())
			return;
		fw_OlyTaskSleep(1);
	}
	*(struct nb_row *)&sh->ring[sh->head % NB_RING] = *row;
	__asm__ volatile("" ::: "memory");
	sh->head++;
}

static void nb_run_stage(volatile struct nb_shared *sh, unsigned int s)
{
	const struct nb_stage *st = &nb_stages[s];
	struct nb_clock clk = { NB_NOW(), 0 };
	struct nb_run r = { .tick = nb_tick_sleep, .tick_ctx = &clk };

	if (st->isa) {
		/* Whole lines: the CPU0 data linked next to it must not share one. */
		static struct nb_row rows[NB_ISA_ROWS] __attribute__((aligned(64)));
		unsigned int n = nb_isa_stage(st->isa - 1, rows);

		for (unsigned int i = 0; i < n; i++) {
			rows[i].stage = (uint16_t)s;
			rows[i].ci = (uint16_t)i;
			nb_push(sh, &rows[i]);
		}
		return;
	}
	for (unsigned int ci = 0; ci < st->ncases && !fenykl_cpu1_stop_requested(); ci++) {
		struct nb_row row = { .stage = (uint16_t)s, .ci = (uint16_t)ci };

		sh->ci = ci;
		nb_sleep(&clk);
		int b = nb_case_begin(&r, &st->cases[ci]);

		if (b < 0) {
			row.status = b == -2 ? NB_SKIP : NB_BAD;
			row.name = b == -2 ? "no live FD frame (not in live view?)" : "no memory for the case";
			nb_push(sh, &row);
			continue;
		}
		for (unsigned int vi = 0; vi < r.f->nvar; vi++) {
			int b = nb_var_begin(&r, vi);

			row = (struct nb_row){ .stage = (uint16_t)s, .ci = (uint16_t)ci, .var = (uint8_t)vi };
			if (!b)
				continue;
			if (b < 0) {
				row.status = NB_BAD;
				row.name = "no memory for the variant";
			} else {
				nb_measure(&r, &clk, &row);
			}
			nb_push(sh, &row);
		}
	}
}

FENYKL_CPU1_WORKER(nb_worker);
void nb_worker(void *arg)
{
	volatile struct nb_shared *sh = FENYKL_SHARED_PTR(nb_sh);

	(void)arg;
	sh->core = FENYKL_CORE_ID();
	while (!fenykl_cpu1_stop_requested()) {
		uint32_t cmd = sh->cmd;

		if (cmd == sh->done || cmd == 0 || cmd > nb_nstages) {
			fw_OlyTaskSleep(1);
			continue;
		}
		nb_run_stage(sh, cmd - 1);
		__asm__ volatile("" ::: "memory");
		sh->done = cmd;
	}
}
