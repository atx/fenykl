/*
 * birddetect3: the CPU1 worker (plugin.toml cpu1_sources, built with FENYKL_CPU1), the borrowed task
 * 0x1002 at priority 15. It first runs the self-test patterns through the network, then serves one
 * request at a time: copies the requested 96x72 YUYV frame into the network input, runs the
 * network and writes the 12x9 logits and the stage times into .shared.
 *
 * Coherency: the IPU writes the frame straight to DRAM, so CPU1 can only hold stale clean lines of
 * it; the copy reads it through the cached alias right after a D-cache purge. Blocking: the pause
 * callback of bd3_net_run sleeps a tick whenever BD3_SLICE_US of work have passed.
 */
#include "birddetect3.h"

struct bd3_clock {
	volatile struct bd3_shared *s;
	uint32_t mark;		/* end of the last timed piece */
	uint32_t slice;		/* end of the last sleep */
	uint32_t slice_max;
	uint32_t work;		/* sleeps excluded */
	uint32_t us[BD3_STAGES];
};

/* CPU1-private; its own 64-byte lines (aligned type). */
static struct bd3_net bd3_net;

static void bd3_sleep(volatile struct bd3_shared *s)
{
	fw_OlyTaskSleep(1);
	s->beats++;
}

static void bd3_clock_start(struct bd3_clock *c, volatile struct bd3_shared *s)
{
	*c = (struct bd3_clock){ .s = s };
	c->mark = BD3_NOW();
	c->slice = c->mark;
}

static void bd3_on_pause(void *ctx, enum bd3_stage st, bool end)
{
	struct bd3_clock *c = ctx;
	uint32_t now = BD3_NOW(), piece = BD3_ELAPSED(c->mark, now), work = BD3_ELAPSED(c->slice, now);

	(void)end;
	c->us[st] += piece;
	c->work += piece;
	c->mark = now;
	if (work < BD3_SLICE_US)
		return;
	if (work > c->slice_max)
		c->slice_max = work;
	bd3_sleep(c->s);
	c->mark = BD3_NOW();
	c->slice = c->mark;
}

static void bd3_selftest(volatile struct bd3_shared *s)
{
	struct bd3_clock c;

	bd3_clock_start(&c, s);
	for (unsigned int p = 0; p < BD3_PATTERNS; p++) {
		bd3_net_pattern(&bd3_net, p);
		bd3_net_run(&bd3_net, bd3_on_pause, &c);
		s->selftest[p] = bd3_logit_sum(bd3_net.logit);
	}
	s->selftest_us = c.work / BD3_PATTERNS;
}

static void bd3_infer(volatile struct bd3_shared *s, uint32_t seq)
{
	uint32_t addr = s->req_addr, t0 = BD3_NOW();
	struct bd3_clock c;

	s->lat_us = BD3_ELAPSED(s->req_t, t0);
	bd3_clock_start(&c, s);
	fw_dcache_purge_all_irqsafe();
	bd3_net_input(&bd3_net, BD3_CACHED(addr));
	bd3_on_pause(&c, BD3_ST_INPUT, true);
	bd3_net_run(&bd3_net, bd3_on_pause, &c);

	for (unsigned int i = 0; i < BD3_CELLS; i++)
		s->logit[i] = bd3_net.logit[i];
	for (unsigned int i = 0; i < BD3_STAGES; i++)
		s->us[i] = c.us[i];
	s->slice_max_us = c.slice_max;
	s->wall_us = BD3_ELAPSED(t0, BD3_NOW());
	s->res_seq = seq;
}

FENYKL_CPU1_WORKER(bd3_worker);
void bd3_worker(void *arg)
{
	volatile struct bd3_shared *s = FENYKL_SHARED_PTR(bd3_shared);
	uint32_t done;

	(void)arg;
	s->core_id = FENYKL_CORE_ID();
	bd3_net_init(&bd3_net);
	bd3_selftest(s);
	s->phase = 1;
	done = s->req_seq;
	s->res_seq = done;
	s->phase = 2;
	while (!fenykl_cpu1_stop_requested()) {
		uint32_t seq = s->req_seq;

		if (seq != done) {
			bd3_infer(s, seq);
			done = seq;
		}
		bd3_sleep(s);
	}
	s->phase = 3;
}
