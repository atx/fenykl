/* The main plugin's focus meter logic (sdk/projects/main/main_fmeter.c) in the GDB simulator, with
   ../hooks/sim_start.S: area choice, the absolute log2 scale and the bar driven by synthetic values at
   the plugin's 50 ms tick. plugin_main sets sim_exit_code to 0 (pass) or the number of the first failed check. */
#include "../../../sdk/projects/main/main_fmeter.h"

#define T_TICK 50u

uint32_t sim_exit_code = 99;

struct t_run {
	struct main_fmeter m;
	struct main_fmeter_in in;
	uint32_t now;
};

static void t_fresh(struct t_run *r, uint32_t v)
{
	r->now += T_TICK;
	r->in.fresh = true;
	r->in.v = v;
	main_fmeter_step(&r->m, &r->in, r->now);
}

static void t_idle(struct t_run *r, unsigned int ticks)
{
	for (unsigned int i = 0; i < ticks; i++) {
		r->now += T_TICK;
		r->in.fresh = false;
		main_fmeter_step(&r->m, &r->in, r->now);
	}
}

static void t_hold(struct t_run *r, uint32_t v, unsigned int ticks)
{
	for (unsigned int i = 0; i < ticks; i++)
		t_fresh(r, v);
}

static int t_area(void)
{
	uint32_t h3[5 * MAIN_FMETER_ROW_WORDS] = { 0 };

	if (main_fmeter_word(17, 7) != 2 * MAIN_FMETER_ROW_WORDS + 3)
		return 1;
	if (main_fmeter_area(h3, 7, 5, false, 3) != 3 || main_fmeter_area(h3, 7, 5, false, 34) != 34)
		return 2;
	/* no or an off-grid target: the centre */
	if (main_fmeter_area(h3, 7, 5, false, MAIN_FMETER_AREA_NONE) != 17 ||
	    main_fmeter_area(h3, 7, 5, false, 35) != 17 || main_fmeter_area(h3, 3, 3, false, 20) != 4)
		return 3;
	/* all targets: the largest value, rows 16 words apart */
	h3[main_fmeter_word(30, 7)] = 900;
	h3[main_fmeter_word(6, 7)] = 899;
	h3[7] = 5000;		/* word 7 of row 0 is off the 7-column grid */
	if (main_fmeter_area(h3, 7, 5, true, 3) != 30)
		return 4;
	return 0;
}

/* log2 in 1/256 units: exact at powers of two, the fraction linear in the mantissa. */
static int t_log2(void)
{
	if (main_fmeter_log2(1) != 0 || main_fmeter_log2(1u << 15) != 15 * 256 ||
	    main_fmeter_log2(0xFFFFFFFFu) != 31 * 256 + 255)
		return 10;
	/* 3 * 2^14: 15 + 0.5 by the linear fraction (log2 1.5 = 0.585) */
	if (main_fmeter_log2(3u << 14) != 15 * 256 + 128 || main_fmeter_log2(3) != 256 + 128)
		return 11;
	return 0;
}

/* Cells at the scale's ends and around a cell boundary; the half-height offset. */
static int t_cells(void)
{
	const uint8_t p = 0x0D;

	if (main_fmeter_cells(0, p) || main_fmeter_cells(1u << 10, p) || main_fmeter_cells(1u << 11, p))
		return 20;
	/* one cell is 0.8 log2 (204.8 units): 11 + 102/256 rounds to 0, 11 + 103/256 to 1 */
	if (main_fmeter_cells(2048 + 8 * 102, p) != 0 || main_fmeter_cells(2048 + 8 * 103, p) != 1)
		return 21;
	/* the blank wall of the camera run (33-38k) is mid-bar */
	if (main_fmeter_cells(33000, p) != 5 || main_fmeter_cells(38000, p) != 5)
		return 22;
	if (main_fmeter_cells(1u << 19, p) != MAIN_FMETER_CELLS ||
	    main_fmeter_cells(0xFFFFFFFFu, p) != MAIN_FMETER_CELLS)
		return 23;
	/* 9.5 cells from 18 + 154/256: 18 + 153/256 is 9 */
	if (main_fmeter_cells((1u << 18) + 1024 * 153, p) != 9 ||
	    main_fmeter_cells((1u << 18) + 1024 * 154, p) != MAIN_FMETER_CELLS)
		return 24;
	/* the S-AF scan's half-height patterns read 1.5 log2 low: the same cells */
	if (main_fmeter_cells(1u << 15, 0x13) != main_fmeter_cells(3u << 15, p) ||
	    main_fmeter_cells(1u << 15, 0x13) != 7 || main_fmeter_cells(1u << 15, 0x04) != 7 ||
	    main_fmeter_cells(1u << 15, 0x0D) != 5)
		return 25;
	return 0;
}

/* Lit, green when full, the peak cell held 1.5 s after a drop. */
static int t_bar(void)
{
	struct t_run r = { .in.pattern = 0x0D };

	t_hold(&r, 1u << 15, 3);
	if (!r.m.bar.shown || r.m.bar.lit != 5 || r.m.bar.green || r.m.bar.peak)
		return 30;
	t_fresh(&r, 1u << 20);
	if (r.m.bar.lit != MAIN_FMETER_CELLS || !r.m.bar.green || r.m.bar.peak)
		return 31;
	/* the half-press switches to a half-height pattern: no jump */
	t_fresh(&r, 1u << 15);
	r.in.pattern = 0x13;
	t_fresh(&r, 3u << 12);
	if (r.m.bar.lit != 5 || r.m.bar.green || r.m.bar.peak != 10)
		return 32;
	t_hold(&r, 3u << 12, 27);
	if (r.m.bar.peak != 10)
		return 33;
	t_fresh(&r, 3u << 12);
	if (r.m.bar.peak)
		return 34;	/* 1.5 s after the top: gone */
	return 0;
}

/* The hold through the S-AF lock gap (and the MF client's detach at a half-press); no source. */
static int t_hold_and_mf(void)
{
	struct t_run r = { .in.pattern = 0x0D };

	t_hold(&r, 1u << 17, 10);
	/* lock: the statistics stop while half-pressed for 5 s, the bar stays */
	r.in.half = true;
	t_idle(&r, 100);
	if (!r.m.bar.shown || r.m.bar.lit != 8)
		return 40;
	/* release: kept until 3 s after it */
	r.in.half = false;
	t_idle(&r, 59);
	if (!r.m.bar.shown)
		return 41;
	t_idle(&r, 1);
	if (r.m.bar.shown)
		return 42;
	t_fresh(&r, 50000);
	if (!r.m.bar.shown)
		return 43;
	/* a short pause (< 500 ms) keeps it live */
	t_idle(&r, 9);
	t_fresh(&r, 50000);
	/* no data and no half-press: hidden 3 s after the last frame */
	t_idle(&r, 59);
	if (!r.m.bar.shown)
		return 44;
	t_idle(&r, 1);
	if (r.m.bar.shown)
		return 45;
	/* no source (MF without the own client) hides at once, even with frames */
	t_hold(&r, 80000, 3);
	r.in.hide = true;
	t_fresh(&r, 80000);
	if (r.m.bar.shown)
		return 46;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int (*const tests[])(void) = { t_area, t_log2, t_cells, t_bar, t_hold_and_mf };

	(void)load_base;
	(void)image_size;
	for (unsigned int i = 0; i < sizeof(tests) / sizeof(tests[0]); i++) {
		int r = tests[i]();

		if (r) {
			sim_exit_code = (uint32_t)r;
			return;
		}
	}
	sim_exit_code = 0;
}
