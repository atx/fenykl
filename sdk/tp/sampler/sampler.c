/*
 * Memory sampler test program (docs/devmon.md, Memory sampler), driven by `fenykl dev sample`.
 * arg is the uncached address of a struct smp_ctl the host filled in; the ring of records follows
 * it. Every access to the block and the ring goes through that uncached address, because the
 * host's READ runs on CPU1 and sees DRAM, not CPU0's D-cache.
 * - periodic: a record every period_us.
 * - change: all words read every period_us, a record when a trigger word (masked) differs from
 *   the last pass; the first pass is always recorded.
 * A word may go through a pointer (SMP_DEREF); the pointer is followed only into RAM, and read
 * once per pass for all words behind it.
 * Ends after duration_ms or when the host sets stop. Late passes count as overruns, records the
 * ring has no room for as drops; both also go into the next record's lost field.
 * Calibration reads every word SMP_CALIBRATE extra times before the run.
 */
#include <stdbool.h>
#include <tp.h>
#include "sampler.h"

#define SMP_TIMER_MASK 0x3FFFFFFFu
#define SMP_TIMER_HALF 0x20000000u
#define SMP_SLEEP_MIN_US 2000	/* a sleep of n ms lasts up to n + 1 ms */
#define SMP_SLEEP_MAX_MS 100u	/* stop and the duration are checked between sleeps */
#define SMP_CALIBRATE 16u
#define SMP_RING_MAX 0x100000u
#define SMP_REC_MAX (SMP_REC_HEAD + SMP_WORDS_MAX)
/* RAM as the monitor's READ accepts it: DRAM above the firmware's vectors, and its uncached
   alias. DSP local memory 0x9D000000.. lies in neither. */
#define SMP_DRAM_LO 0x60021000u
#define SMP_DRAM_HI 0x80000000u
#define SMP_UNCACHED_OFF 0x40000000u
#define SMP_UNCACHED(a) (((a) & 0x3FFFFFFFu) | 0x80000000u)

extern const struct fenykl_tp_hdr tp_hdr;	/* tp_crt.c */

struct smp {
	const struct fenykl_tp_api *api;
	volatile struct smp_ctl *c;
	volatile uint32_t *ring;
	uint32_t mode, period, nwords, rec_words, cap;
	struct smp_word word[SMP_WORDS_MAX];
	uint32_t rec[SMP_REC_MAX];	/* this pass */
	uint32_t ptr[SMP_WORDS_MAX];	/* the u32 each word read this pass */
	uint32_t share[SMP_WORDS_MAX];	/* smp_share */
	uint32_t last[SMP_WORDS_MAX], last_bad, primed;
	uint32_t pending;		/* SMP_CHANGE: a change dropped, record the next pass with room */
	uint32_t head, wi, lost, loop_max;
};

static struct smp smp;

static uint32_t smp_since(uint32_t from, uint32_t to)
{
	return (to - from) & SMP_TIMER_MASK;
}

/* to - from as a signed difference, both within half the timer range of each other. */
static int32_t smp_diff(uint32_t from, uint32_t to)
{
	uint32_t d = smp_since(from, to);

	return d >= SMP_TIMER_HALF ? (int32_t)(d - SMP_TIMER_MASK - 1) : (int32_t)d;
}

/* DSP local memory 0x9D000000..0x9DFFFFFF and the bases that differ from it only in the top two
   address bits: a CPU0 read outside a DSP job hangs the bus (its clock is gated). */
static bool smp_dsp(uint32_t addr)
{
	return (addr & 0x3F000000u) == 0x1D000000u;
}

static int smp_word_check(const struct smp_word *w, uint32_t mode)
{
	uint32_t n = w->width / 8u;

	if (w->width != 8 && w->width != 16 && w->width != 32)
		return SMP_EARGS;
	if (w->flags & ~(SMP_DEREF | SMP_TRIGGER) ||
	    (w->flags & SMP_TRIGGER && (mode != SMP_CHANGE || !w->mask)))
		return SMP_EARGS;
	if (w->addr & ((w->flags & SMP_DEREF) ? 3u : n - 1))
		return SMP_EALIGN;
	return smp_dsp(w->addr) ? SMP_EDSP : 0;
}

/* [a, a + n) lies in RAM and a is aligned to n. */
static bool smp_ram(uint32_t a, uint32_t n)
{
	if (a & (n - 1))
		return false;
	if (a >= SMP_DRAM_LO + SMP_UNCACHED_OFF && a < SMP_DRAM_HI + SMP_UNCACHED_OFF)
		a -= SMP_UNCACHED_OFF;
	return a >= SMP_DRAM_LO && a < SMP_DRAM_HI && n <= SMP_DRAM_HI - a;
}

static uint32_t smp_load(uint32_t addr, uint32_t width)
{
	if (width == 8)
		return *(const volatile uint8_t *)addr;
	if (width == 16)
		return *(const volatile uint16_t *)addr;
	return *(const volatile uint32_t *)addr;
}

/* Word i's value; false and the pointer in *v when the pointer leads outside RAM. A pointer read
   earlier in the pass (s->share) is used again, so words behind one pointer see one target. */
static bool smp_get(struct smp *s, uint32_t i, uint32_t *v)
{
	const struct smp_word *w = &s->word[i];
	uint32_t a = w->addr, p;

	if (!(w->flags & SMP_DEREF)) {
		*v = s->ptr[i] = smp_load(a, w->width);
		return true;
	}
	p = s->share[i] ? s->ptr[s->share[i] - 1] : *(const volatile uint32_t *)a;
	s->ptr[i] = p;
	a = p + w->offset;
	if (!smp_ram(a, w->width / 8u)) {
		*v = p;
		return false;
	}
	*v = smp_load(a, w->width);
	return true;
}

/* For a pointer word, 1 + the first earlier word that reads the same u32 (a pointer word or a
   32-bit plain word), else 0. */
static uint32_t smp_share(const struct smp *s, uint32_t i)
{
	for (uint32_t j = 0; j < i; j++) {
		const struct smp_word *o = &s->word[j];

		if (o->addr == s->word[i].addr && (o->flags & SMP_DEREF || o->width == 32))
			return j + 1;
	}
	return 0;
}

/* arg lies between the program's end and the job buffer, uncached; else it is not ours to read. */
static bool smp_placed(uint32_t arg)
{
	return !(arg & 3u) && arg >= SMP_UNCACHED(tp_hdr.base + tp_hdr.mem) &&
	       arg <= SMP_RING_END - SMP_CTL_SIZE;
}

static int smp_setup(struct smp *s, uint32_t arg)
{
	volatile struct smp_ctl *c = (volatile struct smp_ctl *)arg;
	uint32_t triggers = 0;
	int r;

	s->c = c;
	s->ring = (volatile uint32_t *)(arg + SMP_CTL_SIZE);
	if (c->magic != SMP_MAGIC)
		return SMP_EMAGIC;
	s->mode = c->mode;
	s->period = c->period_us;
	s->nwords = c->nwords;
	s->cap = c->cap;
	if (s->mode > SMP_CHANGE || !s->period || s->period >= SMP_TIMER_HALF || !s->nwords ||
	    s->nwords > SMP_WORDS_MAX || !c->duration_ms || c->duration_ms > SMP_DURATION_MAX_MS)
		return SMP_EARGS;
	s->rec_words = SMP_REC_HEAD + s->nwords;
	if (s->cap < 2 || s->cap > SMP_RING_MAX / 4 / s->rec_words)
		return SMP_EARGS;
	if (s->cap * s->rec_words * 4u > SMP_RING_END - SMP_CTL_SIZE - arg)
		return SMP_EPLACE;
	for (uint32_t i = 0; i < s->nwords; i++) {
		struct smp_word *w = &s->word[i];

		w->addr = c->word[i].addr;
		w->offset = c->word[i].offset;
		w->mask = c->word[i].mask;
		w->width = c->word[i].width;
		w->flags = c->word[i].flags;
		r = smp_word_check(w, s->mode);
		if (r)
			return r;
		if (w->flags & SMP_DEREF)
			s->share[i] = smp_share(s, i);
		triggers += !!(w->flags & SMP_TRIGGER);
	}
	return s->mode == SMP_CHANGE && !triggers ? SMP_EARGS : 0;
}

/* All words into s->rec, the same instant as far as the loop allows. */
static void smp_fill(struct smp *s, uint32_t t)
{
	uint32_t bad = 0, invalid = 0;

	s->rec[SMP_REC_T] = t;
	s->rec[SMP_REC_LOST] = s->lost;
	for (uint32_t i = 0; i < s->nwords; i++) {
		if (!smp_get(s, i, &s->rec[SMP_REC_HEAD + i])) {
			bad |= 1u << i;
			invalid++;
		}
	}
	s->rec[SMP_REC_BAD] = bad;
	if (invalid)
		s->c->invalid += invalid;
}

/* seq last: the host takes a record only when seq is the index it expects. */
static void smp_copy(struct smp *s, volatile uint32_t *dst)
{
	for (uint32_t i = SMP_REC_SEQ + 1; i < s->rec_words; i++)
		dst[i] = s->rec[i];
	dst[SMP_REC_SEQ] = s->head;
}

/* SMP_CHANGE: a trigger word differs from the last pass (the first pass always does). */
static bool smp_changed(struct smp *s)
{
	uint32_t bad = s->rec[SMP_REC_BAD];
	bool changed = !s->primed;

	for (uint32_t i = 0; i < s->nwords; i++) {
		const struct smp_word *w = &s->word[i];
		uint32_t v = s->rec[SMP_REC_HEAD + i];

		if (!(w->flags & SMP_TRIGGER))
			continue;
		if ((v ^ s->last[i]) & w->mask || ((bad ^ s->last_bad) >> i & 1u))
			changed = true;
		s->last[i] = v;
	}
	s->last_bad = bad;
	s->primed = 1;
	return changed;
}

/* A record, or a drop when the host has not taken enough. head is published after the record. */
static void smp_pass(struct smp *s, uint32_t t)
{
	volatile struct smp_ctl *c = s->c;

	bool changed = true;

	smp_fill(s, t);
	if (s->mode == SMP_CHANGE) {
		changed = smp_changed(s);
		if (!changed && !s->pending)
			return;
	}
	if (s->head - c->tail >= s->cap) {
		/* a change already dropped is counted once, however long the ring stays full */
		if (changed) {
			c->drops++;
			s->lost++;
		}
		s->pending = 1;
		return;
	}
	smp_copy(s, s->ring + s->wi * s->rec_words);
	s->lost = 0;
	s->pending = 0;
	if (++s->wi == s->cap)
		s->wi = 0;
	c->head = ++s->head;
}

/* The second longest of a few full passes (the longest may be an interrupt), copied into the
   first record slot (the run has not started and head stays 0): the shortest period the loop can
   keep. +1 for the timer's resolution. */
static uint32_t smp_calibrate(struct smp *s)
{
	const struct fenykl_tp_api *api = s->api;
	uint32_t worst = 0, second = 0;

	for (uint32_t i = 0; i < SMP_CALIBRATE; i++) {
		uint32_t t0 = api->timer_us(), t;

		(void)s->c->stop;
		(void)s->c->tail;
		smp_fill(s, t0);
		if (s->mode == SMP_CHANGE)
			(void)smp_changed(s);
		smp_copy(s, s->ring);
		t = smp_since(t0, api->timer_us());
		if (t > worst) {
			second = worst;
			worst = t;
		} else if (t > second) {
			second = t;
		}
	}
	s->c->invalid = 0;
	s->primed = 0;
	return second + 1;
}

static void smp_run(struct smp *s, uint32_t duration_us)
{
	const struct fenykl_tp_api *api = s->api;
	volatile struct smp_ctl *c = s->c;
	uint32_t now = api->timer_us();
	uint32_t elapsed = 0, last = now, next = now;

	c->t_start = now;
	for (;;) {
		int32_t left;
		uint32_t cost;

		now = api->timer_us();
		elapsed += smp_since(last, now);
		last = now;
		if (c->stop || elapsed >= duration_us)
			return;
		left = smp_diff(now, next);
		if (left >= SMP_SLEEP_MIN_US) {
			uint32_t ms = (uint32_t)left / 1000 - 1;

			api->sleep_ms(ms < SMP_SLEEP_MAX_MS ? ms : SMP_SLEEP_MAX_MS);
			continue;
		}
		if (left > 0)
			continue;
		if ((uint32_t)-left >= s->period) {
			uint32_t k = (uint32_t)-left / s->period;

			c->overruns += k;
			s->lost += k;
			next += k * s->period;
		}
		next = (next + s->period) & SMP_TIMER_MASK;
		smp_pass(s, elapsed);
		cost = smp_since(now, api->timer_us());
		if (cost > s->loop_max) {
			s->loop_max = cost;
			c->loop_max_us = cost;
		}
	}
}

int tp_main(const struct fenykl_tp_api *api, uint32_t arg)
{
	struct smp *s = &smp;
	int r;

	s->api = api;
	if (!smp_placed(arg))
		return SMP_EPLACE;
	r = smp_setup(s, arg);
	if (r == SMP_EMAGIC)
		return r;	/* not a control block: leave it alone */
	if (r) {
		s->c->state = r;
		return r;
	}
	s->c->loop_us = smp_calibrate(s);
	if (s->period < s->c->loop_us) {
		s->c->state = SMP_EPERIOD;
		return SMP_EPERIOD;
	}
	s->c->state = SMP_RUNNING;
	smp_run(s, s->c->duration_ms * 1000u);
	s->c->t_end = api->timer_us();
	s->c->state = SMP_DONE;
	return 0;
}
