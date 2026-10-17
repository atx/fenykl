/* CPU1 side of cpu1frame (plugin.toml cpu1_sources: built with FENYKL_CPU1). Runs as the borrowed task
   0x1002 at priority 15. For each request it sums the FD frame through the uncached alias, then through
   the cached alias after purging its own D-cache, in timed chunks with a 1-tick sleep after each. */
#include "shared.h"

#define CHUNK_ROWS (FD_HEIGHT / CHUNKS)
#define HOT_ROWS 6

/* CPU1-private: read cached only by the worker, whole lines. */
static uint32_t hot[HOT_ROWS * ROW_WORDS] __attribute__((aligned(64)));

/* Byte sum of `rows` rows of FD_WIDTH bytes, read as words. Two accumulators of two 16-bit lanes each;
   a lane collects one byte per word of a row (160 x 255 < 0x10000), folded per row. */
static uint32_t sum_rows(const volatile uint32_t *p, unsigned rows)
{
	uint32_t total = 0;

	for (unsigned r = 0; r < rows; r++) {
		const volatile uint32_t *end = p + ROW_WORDS;
		uint32_t even = 0, odd = 0;
		while (p != end) {
			uint32_t w = *p++;
			even += w & 0x00FF00FFu;
			odd += (w >> 8) & 0x00FF00FFu;
		}
		total += (even & 0xFFFFu) + (even >> 16) + (odd & 0xFFFFu) + (odd >> 16);
	}
	return total;
}

static void beat(volatile struct shared *s)
{
	fw_OlyTaskSleep(1);
	s->beats++;
}

/* Sums FD_BYTES of `frame` (or FD_BYTES of passes over `hot` if frame is 0) in CHUNKS timed chunks;
   the time of the chunks, without the sleeps, goes to *us. */
static uint32_t timed_sum(volatile struct shared *s, const volatile uint32_t *frame, int purge,
			  uint32_t *us)
{
	uint32_t sum = 0, total = 0;

	for (int i = 0; i < CHUNKS; i++) {
		uint32_t t0 = TIMER_NOW();
		if (purge)
			fw_dcache_purge_all_irqsafe();
		if (frame) {
			sum += sum_rows(frame + i * CHUNK_ROWS * ROW_WORDS, CHUNK_ROWS);
		} else {
			for (int k = 0; k < CHUNK_ROWS / HOT_ROWS; k++)
				sum += sum_rows(hot, HOT_ROWS);
		}
		uint32_t dt = ELAPSED(t0, TIMER_NOW());
		total += dt;
		if (dt > s->chunk_max)
			s->chunk_max = dt;
		beat(s);
	}
	*us = total;
	return sum;
}

static void serve(volatile struct shared *s, uint32_t seq)
{
	uint32_t addr = s->req_addr, us_u, us_c;
	uint32_t sum_u = timed_sum(s, FENYKL_UNCACHED(addr), 0, &us_u);
	uint32_t sum_c = timed_sum(s, CACHED(addr), 1, &us_c);

	s->sum_u = sum_u;
	s->us_u = us_u;
	s->sum_c = sum_c;
	s->us_c = us_c;
	s->res_seq = seq;
}

FENYKL_CPU1_WORKER(worker);
void worker(void *arg)
{
	volatile struct shared *s = FENYKL_SHARED_PTR(shared);
	uint32_t us, best = 0xFFFFFFFFu;

	(void)arg;
	s->core_id = FENYKL_CORE_ID();
	s->phase = 1;
	for (int i = 0; i < 3; i++) { /* the first pass warms the cache */
		timed_sum(s, 0, 0, &us);
		if (us < best)
			best = us;
	}
	s->hot_us = best;
	s->phase = 2;
	uint32_t done = s->res_seq;
	for (;;) {
		uint32_t seq = s->req_seq;
		if (seq != done) {
			serve(s, seq);
			done = seq;
		}
		if (fenykl_cpu1_stop_requested())
			break;
		beat(s);
	}
	s->phase = 3;
}
