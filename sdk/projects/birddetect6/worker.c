/*
 * birddetect6: the CPU1 worker (plugin.toml cpu1_sources, built with FENYKL_CPU1), the borrowed task
 * 0x1002 at priority 15. It first runs the self-test patterns through the network and its
 * post-processing (inject.c, pure code shared with CPU0), then serves one request at a time: copies
 * the requested 96x72 YUYV frame into the network input, runs the network and writes both 12x9
 * logit grids and the stage times into .shared.
 *
 * Coherency: the IPU writes the frame straight to DRAM, so CPU1 can only hold stale clean lines of
 * it; the copy reads it through the cached alias right after a D-cache purge. Blocking: the pause
 * callback of bd6_net_run sleeps a tick whenever BD6_SLICE_US of work have passed.
 */
#include "birddetect6.h"

struct bd6_clock {
	volatile struct bd6_shared *s;
	uint32_t mark;		/* end of the last timed piece */
	uint32_t slice;		/* end of the last sleep */
	uint32_t slice_max;
	uint32_t work;		/* sleeps excluded */
	uint32_t us[BD6_STAGES];
};

/* CPU1-private; its own 64-byte lines (aligned type). */
static struct bd6_net bd6_net;

static void bd6_sleep(volatile struct bd6_shared *s)
{
	fw_OlyTaskSleep(1);
	s->beats++;
}

static void bd6_clock_start(struct bd6_clock *c, volatile struct bd6_shared *s)
{
	*c = (struct bd6_clock){ .s = s };
	c->mark = BD6_NOW();
	c->slice = c->mark;
}

static void bd6_on_pause(void *ctx, enum bd6_stage st, bool end)
{
	struct bd6_clock *c = ctx;
	uint32_t now = BD6_NOW(), piece = BD6_ELAPSED(c->mark, now), work = BD6_ELAPSED(c->slice, now);

	(void)end;
	c->us[st] += piece;
	c->work += piece;
	c->mark = now;
	if (work < BD6_SLICE_US)
		return;
	if (work > c->slice_max)
		c->slice_max = work;
	bd6_sleep(c->s);
	c->mark = BD6_NOW();
	c->slice = c->mark;
}

static void bd6_selftest(volatile struct bd6_shared *s)
{
	struct bd6_clock c;
	struct bd6_det d;

	bd6_clock_start(&c, s);
	for (unsigned int p = 0; p < BD6_PATTERNS; p++) {
		bd6_net_pattern(&bd6_net, p);
		bd6_net_run(&bd6_net, bd6_on_pause, &c);
		s->selftest[p] = bd6_logit_sum(bd6_net.logit);
		bd6_post(bd6_net.logit, &d);
		s->selftest_det[p] = d;
	}
	s->selftest_us = c.work / BD6_PATTERNS;
}

static void bd6_infer(volatile struct bd6_shared *s, uint32_t seq)
{
	uint32_t addr = s->req_addr, t0 = BD6_NOW();
	struct bd6_clock c;

	s->lat_us = BD6_ELAPSED(s->req_t, t0);
	bd6_clock_start(&c, s);
	fw_dcache_purge_all_irqsafe();
	bd6_net_input(&bd6_net, BD6_CACHED(addr));
	bd6_on_pause(&c, BD6_ST_INPUT, true);
	bd6_net_run(&bd6_net, bd6_on_pause, &c);

	for (unsigned int i = 0; i < BD6_LOGITS; i++)
		s->logit[i] = bd6_net.logit[i];
	for (unsigned int i = 0; i < BD6_STAGES; i++)
		s->us[i] = c.us[i];
	s->slice_max_us = c.slice_max;
	s->wall_us = BD6_ELAPSED(t0, BD6_NOW());
	s->res_seq = seq;
}

FENYKL_CPU1_WORKER(bd6_worker);
void bd6_worker(void *arg)
{
	volatile struct bd6_shared *s = FENYKL_SHARED_PTR(bd6_shared);
	uint32_t done;

	(void)arg;
	s->core_id = FENYKL_CORE_ID();
	bd6_net_init(&bd6_net);
	bd6_selftest(s);
	s->phase = 1;
	done = s->req_seq;
	s->res_seq = done;
	s->phase = 2;
	while (!fenykl_cpu1_stop_requested()) {
		uint32_t seq = s->req_seq;

		if (seq != done) {
			bd6_infer(s, seq);
			done = seq;
		}
		bd6_sleep(s);
	}
	s->phase = 3;
}
