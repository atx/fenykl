/*
 * birddetect: the CPU1 worker (plugin.toml cpu1_sources, built with FENYKL_CPU1), the borrowed task
 * 0x1002 at priority 15. It first runs the self-test patterns through the network, then
 * serves one request at a time: point-samples the requested FD slot to 96x72, runs the network
 * and writes the 12x9 logits and the stage times into .shared.
 *
 * Coherency: the IPU writes the slot straight to DRAM, so CPU1 can only hold stale clean
 * lines of it; the sampling reads it through the cached alias right after a D-cache purge. Blocking:
 * the pause callback of bd_net_run sleeps a tick whenever BD_SLICE_US of work have passed.
 */
#include "birddetect.h"

struct bd_clock {
	volatile struct bd_shared *s;
	uint32_t mark;		/* end of the last timed piece */
	uint32_t slice;		/* end of the last sleep */
	uint32_t slice_max;
	uint32_t us[BD_STAGES];
};

static struct bd_net bd_net __attribute__((aligned(64)));

static void bd_sleep(volatile struct bd_shared *s)
{
	fw_OlyTaskSleep(1);
	s->beats++;
}

static void bd_clock_start(struct bd_clock *c, volatile struct bd_shared *s)
{
	*c = (struct bd_clock){ .s = s };
	c->mark = BD_NOW();
	c->slice = c->mark;
}

static void bd_on_pause(void *ctx, enum bd_group g)
{
	struct bd_clock *c = ctx;
	uint32_t now = BD_NOW(), work = BD_ELAPSED(c->slice, now);

	c->us[BD_ST_STEM + g] += BD_ELAPSED(c->mark, now);
	c->mark = now;
	if (work < BD_SLICE_US)
		return;
	if (work > c->slice_max)
		c->slice_max = work;
	bd_sleep(c->s);
	c->mark = BD_NOW();
	c->slice = c->mark;
}

static void bd_selftest(volatile struct bd_shared *s)
{
	struct bd_clock c;

	bd_clock_start(&c, s);
	for (unsigned int p = 0; p < BD_PATTERNS; p++) {
		bd_net_pattern(&bd_net, p);
		bd_net_run(&bd_net, bd_on_pause, &c);
		s->selftest[p] = bd_logit_sum(bd_net.logit);
	}
}

static void bd_infer(volatile struct bd_shared *s, uint32_t seq)
{
	uint32_t addr = s->req_addr, t0 = BD_NOW();
	struct bd_clock c;

	s->lat_us = BD_ELAPSED(s->req_t, t0);
	bd_clock_start(&c, s);
	fw_dcache_purge_all_irqsafe();
	bd_net_sample(&bd_net, BD_CACHED(addr));
	c.mark = BD_NOW();
	c.us[BD_ST_SAMPLE] = BD_ELAPSED(c.slice, c.mark);
	bd_net_run(&bd_net, bd_on_pause, &c);

	for (unsigned int i = 0; i < BD_CELLS; i++)
		s->logit[i] = bd_net.logit[i];
	for (unsigned int i = 0; i < BD_STAGES; i++)
		s->us[i] = c.us[i];
	s->slice_max_us = c.slice_max;
	s->wall_us = BD_ELAPSED(t0, BD_NOW());
	s->res_seq = seq;
}

FENYKL_CPU1_WORKER(bd_worker);
void bd_worker(void *arg)
{
	volatile struct bd_shared *s = FENYKL_SHARED_PTR(bd_shared);
	uint32_t done;

	(void)arg;
	s->core_id = FENYKL_CORE_ID();
	bd_net_init(&bd_net);
	bd_selftest(s);
	s->phase = 1;
	done = s->req_seq;
	s->res_seq = done;
	s->phase = 2;
	while (!fenykl_cpu1_stop_requested()) {
		uint32_t seq = s->req_seq;

		if (seq != done) {
			bd_infer(s, seq);
			done = seq;
		}
		bd_sleep(s);
	}
	s->phase = 3;
}
