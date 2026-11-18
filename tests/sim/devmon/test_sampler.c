/*
 * The memory sampler sdk/tp/sampler/, built by `fenykl tp build` for slot 2 and placed there by the
 * test (.tpimg), in the GDB simulator. The function table is the world: each timer_us call moves
 * time on and applies the scenario's events (memory changes, a preemption, the host's stop and
 * tail). The first scenario runs the control block py/fenykl/sampler.py wrote (.smpcfg).
 * plugin_main sets sim_exit_code to 0 or the first failed check.
 */
#include <fenykl.h>
#include "../../../sdk/lib/devmon_int.h"
#include "../../../sdk/tp/sampler/sampler.h"

uint32_t sim_exit_code = 99;

#define T_SLOT 2
#define T_DATA 0x6EB00000u	/* words of the host's config (tests/test_sampler.py) */
#define T_CFG 0x6EB01000u	/* the host's control block */
#define T_MASK 0x3FFFFFFFu
#define T_EVENTS 4
#define T_AREA (SMP_CTL_SIZE + 0x2000u)
#define T_CTL FDM_SLOT_ADDR(T_SLOT + 1)	/* the block where the host puts it */
#define T_SEQ SMP_REC_SEQ
#define T_T SMP_REC_T
#define T_LOST SMP_REC_LOST
#define T_BAD SMP_REC_BAD
#define T_V(i) (SMP_REC_HEAD + (i))

struct t_event {
	uint32_t at, addr, val;
};

struct t_world {
	uint32_t us, base, step;
	uint32_t jump_at, jump;		/* one preemption */
	struct t_event ev[T_EVENTS];
	uint32_t nev;
	uint32_t stop_at;
	bool follow;			/* the host takes each record at once */
	uint32_t take_at;		/* the host takes everything once */
	uint32_t sleeps;
};

static struct t_world w;
static volatile struct smp_ctl *ctl;

static uint32_t t_timer(void)
{
	uint32_t t = (w.base + w.us) & T_MASK;

	w.us += w.step;
	if (w.jump && w.us >= w.jump_at) {
		w.us += w.jump;
		w.jump = 0;
	}
	for (uint32_t i = 0; i < w.nev; i++)
		if (w.ev[i].addr && w.us >= w.ev[i].at) {
			*(volatile uint32_t *)w.ev[i].addr = w.ev[i].val;
			w.ev[i].addr = 0;
		}
	if (w.stop_at && w.us >= w.stop_at)
		ctl->stop = 1;
	if (w.follow || (w.take_at && w.us >= w.take_at)) {
		ctl->tail = ctl->head;
		w.take_at = 0;
	}
	return t;
}

static void t_sleep(uint32_t ms)
{
	w.sleeps++;
	w.us += ms * 1000u + 300u;
}

static void t_log(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2)
{
	(void)fmt;
	(void)a0;
	(void)a1;
	(void)a2;
}

static uint32_t t_rd32(uint32_t addr)
{
	return *(volatile uint32_t *)addr;
}

static void t_wr32(uint32_t addr, uint32_t v)
{
	*(volatile uint32_t *)addr = v;
}

static const struct fenykl_tp_api api = {
	.version = FENYKL_TP_API,
	.size = sizeof(struct fenykl_tp_api),
	.log = t_log,
	.sleep_ms = t_sleep,
	.timer_us = t_timer,
	.rd32 = t_rd32,
	.wr32 = t_wr32,
};

/* A fresh world and control block (mode, period, 1 ms, a ring of cap records). */
static void t_reset(uint32_t mode, uint32_t period, uint32_t cap)
{
	w = (struct t_world){ .step = 1 };
	ctl = FENYKL_UNCACHED(T_CTL);
	for (uint32_t i = 0; i < T_AREA / 4; i++)
		((volatile uint32_t *)ctl)[i] = 0;
	ctl->magic = SMP_MAGIC;
	ctl->mode = mode;
	ctl->period_us = period;
	ctl->duration_ms = 1;
	ctl->cap = cap;
}

static void t_word(uint32_t i, uint32_t addr, uint32_t width, uint32_t flags, uint32_t mask)
{
	ctl->word[i].addr = addr;
	ctl->word[i].width = (uint16_t)width;
	ctl->word[i].flags = (uint16_t)flags;
	ctl->word[i].mask = mask;
	ctl->nwords = i + 1;
}

static int t_run_at(uint32_t arg)
{
	const struct fenykl_tp_hdr *h = (const struct fenykl_tp_hdr *)FDM_SLOT_ADDR(T_SLOT);
	int (*entry)(const struct fenykl_tp_api *, uint32_t);

	for (uint32_t a = h->base + h->size; a < h->base + h->mem; a++)
		*(volatile uint8_t *)a = 0;
	entry = (int (*)(const struct fenykl_tp_api *, uint32_t))h->entry;
	return entry(&api, arg);
}

static int t_run(void)
{
	return t_run_at((uint32_t)ctl);
}

static const volatile uint32_t *t_rec(uint32_t i)
{
	return (const volatile uint32_t *)(ctl + 1) + (i % ctl->cap) * (SMP_REC_HEAD + ctl->nwords);
}

static uint32_t t_gap(uint32_t i)
{
	return t_rec(i)[T_T] - t_rec(i - 1)[T_T];
}

/* A record's time on the timer of the world (its microseconds since t_start, plus t_start). */
static uint32_t t_at(uint32_t i)
{
	return (ctl->t_start + t_rec(i)[T_T] - w.base) & T_MASK;
}

/* The host's block: periodic, 10 us, 1 ms; a u32, a u16, a u8, a word behind a pointer, and a
   pointer into DSP memory. The timer wraps during the run; the records' times do not. */
static uint32_t t_host_config(void)
{
	volatile uint32_t *d = (volatile uint32_t *)T_DATA;

	t_reset(0, 0, 0);
	for (uint32_t i = 0; i < SMP_CTL_SIZE / 4; i++)
		((volatile uint32_t *)ctl)[i] = ((const uint32_t *)T_CFG)[i];
	d[0] = 0x9234ABCDu;
	d[1] = 0x5678EF01u;	/* +6: 0x5678 */
	d[2] = 0x0000AB00u;	/* +9: 0xAB */
	d[4] = T_DATA + 0x20;	/* pointer: +4 is d[9] */
	d[5] = 0x9D000000u;	/* pointer into DSP memory */
	d[9] = 0x13572468u;
	w.base = T_MASK - 300;
	w.follow = true;
	if (ctl->nwords != 5 || ctl->period_us != 10 || ctl->word[3].flags != SMP_DEREF ||
	    ctl->word[3].offset != 4 || ctl->word[2].width != 8)
		return 1;
	if (t_run() || ctl->state != SMP_DONE)
		return 2;
	if (ctl->head < 95 || ctl->head > 101 || ctl->drops || ctl->overruns)
		return 3;
	for (uint32_t i = 0; i < ctl->head; i++) {
		const volatile uint32_t *r = t_rec(i);

		if (i > 1 && t_gap(i) != 10)	/* the first pass is a microsecond late */
			return 4;
		if (r[T_SEQ] != i || r[T_LOST] || r[T_BAD] != 1u << 4 || r[T_V(0)] != 0x9234ABCDu ||
		    r[T_V(1)] != 0x5678u || r[T_V(2)] != 0xABu || r[T_V(3)] != 0x13572468u ||
		    r[T_V(4)] != 0x9D000000u)
			return 5;
	}
	if (((ctl->t_end - ctl->t_start) & T_MASK) < 1000 || ctl->t_end > ctl->t_start ||
	    ctl->invalid != ctl->head)
		return 6;
	return 0;
}

/* A ring of 4 the host empties once at 500 us: drops, and the lost count in the record after
   them. Records 4..7 are left in the ring. */
static uint32_t t_drops(void)
{
	const volatile uint32_t *r;

	t_reset(SMP_PERIODIC, 10, 4);
	t_word(0, T_DATA, 32, 0, 0);
	w.take_at = 500;
	if (t_run() || ctl->state != SMP_DONE)
		return 1;
	if (ctl->head != 8 || ctl->tail != 4 || ctl->overruns)
		return 2;
	if (ctl->head + ctl->drops < 95 || ctl->head + ctl->drops > 101)
		return 3;	/* every pass is a record or a drop */
	r = t_rec(4);
	if (t_at(4) < 500 || !r[T_LOST] || r[T_SEQ] != 4 || ctl->drops <= r[T_LOST])
		/* the second fill dropped too */
		return 4;
	for (uint32_t i = 5; i < 8; i++)
		if (t_rec(i)[T_LOST] || t_gap(i) != 10)
			return 5;
	return 0;
}

/* Time leaps 35 us at 300 us: overruns, and the record after the gap says how many. */
static uint32_t t_overrun(void)
{
	uint32_t i;

	t_reset(SMP_PERIODIC, 10, 200);
	t_word(0, T_DATA, 32, 0, 0);
	w.follow = true;
	w.jump_at = 300;
	w.jump = 35;
	if (t_run() || ctl->state != SMP_DONE)
		return 1;
	if (ctl->overruns < 2 || ctl->drops)
		return 2;
	for (i = 1; i < ctl->head && !t_rec(i)[T_LOST]; i++)
		;
	if (i == ctl->head || t_rec(i)[T_LOST] != ctl->overruns)
		return 3;
	if (t_gap(i) < (ctl->overruns + 1) * 10 || t_gap(i) >= (ctl->overruns + 2) * 10)
		return 4;
	return ctl->loop_max_us && ctl->loop_us ? 0 : 5;
}

/* Change mode: a masked trigger word, a trigger word behind a pointer and the pointer; one more
   word behind the same pointer. */
static uint32_t t_change(void)
{
	volatile uint32_t *d = (volatile uint32_t *)T_DATA;
	static const uint32_t at[] = { 0, 400, 600, 700 };

	t_reset(SMP_CHANGE, 20, 200);
	d[0] = 0x1100;
	d[4] = T_DATA + 0x20;
	d[8] = 7;
	d[12] = 9;
	t_word(0, T_DATA, 32, SMP_TRIGGER, 0xFF00);
	t_word(1, T_DATA + 0x10, 32, SMP_DEREF | SMP_TRIGGER, 0xFFFFFFFFu);
	ctl->word[1].offset = 0;
	t_word(2, T_DATA + 0x10, 32, SMP_TRIGGER, 0xFFFFFFFFu);	/* the pointer itself */
	t_word(3, T_DATA + 0x10, 32, SMP_DEREF, 0);	/* shares word 1's pointer read */
	ctl->word[3].offset = 4;
	d[9] = 0x99;
	d[13] = 0xDD;
	w.follow = true;
	w.ev[0] = (struct t_event){ 200, T_DATA, 0x11FF };	/* masked out */
	w.ev[1] = (struct t_event){ 400, T_DATA, 0x12FF };
	w.ev[2] = (struct t_event){ 600, T_DATA + 0x20, 8 };	/* the record behind the pointer */
	w.ev[3] = (struct t_event){ 700, T_DATA + 0x10, T_DATA + 0x30 };	/* the pointer */
	w.nev = 4;
	if (t_run() || ctl->state != SMP_DONE)
		return 1;
	if (ctl->head != 4 || ctl->drops || ctl->invalid)
		return 2;
	for (uint32_t i = 1; i < 4; i++) {
		uint32_t t = t_at(i);

		if (t < at[i] || t > at[i] + 25)
			return 10 + i;
	}
	if (t_rec(0)[T_V(0)] != 0x1100 || t_rec(0)[T_V(1)] != 7 || t_rec(1)[T_V(0)] != 0x12FF ||
	    t_rec(2)[T_V(1)] != 8 || t_rec(3)[T_V(1)] != 9 || t_rec(3)[T_V(2)] != T_DATA + 0x30 ||
	    t_rec(0)[T_V(3)] != 0x99 || t_rec(3)[T_V(3)] != 0xDD)
		return 3;
	return 0;
}

/* Change mode with a full ring: two changes dropped (counted once each, not per pass), then the
   first pass with room records the state as it is now. */
static uint32_t t_change_full(void)
{
	volatile uint32_t *d = (volatile uint32_t *)T_DATA;
	const volatile uint32_t *r;

	t_reset(SMP_CHANGE, 10, 2);
	d[0] = 1;
	t_word(0, T_DATA, 32, SMP_TRIGGER, 0xFFFFFFFFu);
	w.ev[0] = (struct t_event){ 100, T_DATA, 2 };	/* recorded: the ring is then full */
	w.ev[1] = (struct t_event){ 200, T_DATA, 3 };	/* dropped */
	w.ev[2] = (struct t_event){ 300, T_DATA, 4 };	/* dropped */
	w.nev = 3;
	w.take_at = 400;
	if (t_run() || ctl->state != SMP_DONE)
		return 1;
	if (ctl->head != 3 || ctl->drops != 2)
		return 2;
	r = t_rec(2);
	if (r[T_V(0)] != 4 || r[T_LOST] != 2 || r[T_SEQ] != 2 || t_at(2) < 400 || t_at(2) > 420)
		return 3;
	return 0;
}

/* Refusals before any pass: the state says why. */
static uint32_t t_refusals(void)
{
	static const struct {
		uint32_t mode, period, addr, width, flags;
		int32_t want;
	} cases[] = {
		{ SMP_PERIODIC, 100, 0x9D000100u, 32, 0, SMP_EDSP },
		{ SMP_PERIODIC, 100, 0xDD000000u, 8, 0, SMP_EDSP },
		{ SMP_PERIODIC, 100, 0x9D000000u, 8, SMP_DEREF, SMP_EDSP },
		{ SMP_PERIODIC, 100, T_DATA + 1, 16, 0, SMP_EALIGN },
		{ SMP_PERIODIC, 100, T_DATA + 2, 8, SMP_DEREF, SMP_EALIGN },
		{ SMP_PERIODIC, 100, T_DATA, 12, 0, SMP_EARGS },
		{ SMP_PERIODIC, 100, T_DATA, 32, SMP_TRIGGER, SMP_EARGS },
		{ SMP_CHANGE, 100, T_DATA, 32, 0, SMP_EARGS },
		{ SMP_PERIODIC, 1, T_DATA, 32, 0, SMP_EPERIOD },
	};

	for (uint32_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		t_reset(cases[i].mode, cases[i].period, 16);
		t_word(0, cases[i].addr, cases[i].width, cases[i].flags, 1);
		if (t_run() != cases[i].want || ctl->state != cases[i].want || ctl->head)
			return 1 + i;
	}
	if (ctl->loop_us <= 1)
		return 20;
	t_reset(SMP_PERIODIC, 100, 16);
	t_word(0, T_DATA, 32, 0, 0);
	ctl->magic = 0;
	if (t_run() != SMP_EMAGIC || ctl->state)
		return 21;
	/* a block not between the program and the job buffer is not read, nor written */
	ctl->magic = SMP_MAGIC;
	if (t_run_at((uint32_t)FENYKL_UNCACHED(FDM_SLOT_ADDR(T_SLOT)) + 0x100) != SMP_EPLACE ||
	    t_run_at((uint32_t)FENYKL_UNCACHED(FDM_IO_ADDR)) != SMP_EPLACE ||
	    t_run_at((uint32_t)ctl + 2) != SMP_EPLACE || ctl->state)
		return 22;
	/* a ring past the job buffer at 0xA100A800: 13 slots less the block hold 21267 records */
	ctl->cap = 21268;
	if (t_run() != SMP_EPLACE || ctl->state != SMP_EPLACE)
		return 23;
	ctl->cap = 21267;
	ctl->state = 0;
	return t_run() || ctl->state != SMP_DONE ? 24 : 0;
}

/* The host's stop word ends the run early; long periods sleep instead of spinning, and a long
   sleep is cut into pieces that check stop and the duration. */
static uint32_t t_stop_and_sleep(void)
{
	t_reset(SMP_PERIODIC, 10, 200);
	t_word(0, T_DATA, 32, 0, 0);
	ctl->duration_ms = 100;
	w.follow = true;
	w.stop_at = 300;
	if (t_run() || ctl->state != SMP_DONE || ctl->head < 25 || ctl->head > 31 || w.us > 400)
		return 1;
	t_reset(SMP_PERIODIC, 5000, 200);
	t_word(0, T_DATA, 32, 0, 0);
	ctl->duration_ms = 30;
	w.follow = true;
	if (t_run() || ctl->state != SMP_DONE || ctl->head != 6 || !w.sleeps || ctl->overruns)
		return 2;
	for (uint32_t i = 2; i < ctl->head; i++)
		if (t_gap(i) != 5000)
			return 3;
	/* a period far longer than the run: sleeps of 100 ms at most, then the duration ends it */
	t_reset(SMP_PERIODIC, 30000000, 200);
	t_word(0, T_DATA, 32, 0, 0);
	w.follow = true;
	if (t_run() || ctl->state != SMP_DONE || ctl->head != 1 || w.sleeps != 1 || w.us > 101000)
		return 4;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	static uint32_t (*const scenario[])(void) = {
		t_host_config, t_drops, t_overrun, t_change, t_change_full, t_refusals,
		t_stop_and_sleep,
	};

	(void)load_base;
	(void)image_size;
	if (fdm_tp_check((const struct fenykl_tp_hdr *)FDM_SLOT_ADDR(T_SLOT), T_SLOT)) {
		sim_exit_code = 1;
		return;
	}
	for (uint32_t i = 0; i < sizeof(scenario) / sizeof(scenario[0]); i++) {
		uint32_t r = scenario[i]();

		if (r) {
			sim_exit_code = 10 * (i + 1) + (r < 10 ? r : 9);
			return;
		}
	}
	sim_exit_code = 0;
}
