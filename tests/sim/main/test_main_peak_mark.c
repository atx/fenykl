/* The main plugin's peaking mark (sdk/projects/main/main_peak_mark.c) in the GDB simulator, with
   ../hooks/sim_start.S, against a per-pixel count of the 8 neighbours. plugin_main sets
   sim_exit_code to the failure bit mask (0 = pass):
     1  random images (widths 320, 316, 36; thresholds 1, 24, 56, 128): the lit list, its order
        (row-major) or the overlay differs from the per-pixel reference
     2  capped: not flagged, or the list is not the reference's first max entries
     4  a pattern case (isolated pixel, line, 2x2 block, border column) differs from the reference
     8  the mark wrote outside the lit pixels, or left the scratch overlay dirty elsewhere */
#include <stdbool.h>
#include <stdint.h>
#include "../../../sdk/projects/main/main_peak_mark.h"

#define T_H 12u
#define T_PITCH MAIN_PEAK_PITCH
#define T_EDGE 0xFF0000FFu
#define T_MAX (T_PITCH * T_H)

uint32_t sim_exit_code = 99;

static uint32_t t_ln_words[T_PITCH * T_H / 4];
static uint8_t *const t_ln = (uint8_t *)t_ln_words;
static uint32_t t_bm[MAIN_PEAK_BM_WORDS(T_H)];
static uint32_t t_dst[T_PITCH * T_H];
static uint32_t t_list[T_MAX];
static uint32_t t_ref[T_MAX];
static uint32_t t_seed = 12345;

static uint32_t t_rand(void)
{
	t_seed = t_seed * 1103515245u + 12345u;
	return t_seed >> 16;
}

/* Row-major lit pixels by counting each pixel's neighbours; returns their number. */
static uint32_t t_reference(uint32_t w, uint32_t h, uint32_t thr)
{
	uint32_t n = 0;

	for (uint32_t y = 1; y + 1 < h; y++) {
		for (uint32_t x = 1; x + 1 < w; x++) {
			uint32_t k = 0;

			if (t_ln[y * T_PITCH + x] < thr)
				continue;
			for (int dy = -1; dy <= 1; dy++)
				for (int dx = -1; dx <= 1; dx++)
					if ((dy || dx) &&
					    t_ln[(y + dy) * T_PITCH + x + dx] >= thr)
						k++;
			if (k >= 2)
				t_ref[n++] = y * T_PITCH + x;
		}
	}
	return n;
}

/* Runs the mark on a clear overlay; bit 8 if it wrote anything but its list. */
static uint32_t t_run(struct main_peak_mark *m, uint32_t w, uint32_t h, uint32_t thr, uint32_t max)
{
	uint32_t fail = 0;

	for (uint32_t i = 0; i < T_PITCH * T_H; i++)
		t_dst[i] = 0;
	*m = (struct main_peak_mark){
		.ln = t_ln, .bm = t_bm, .dst = t_dst, .list = t_list, .w = w, .h = h, .thr = thr,
		.max = max, .edge = T_EDGE,
	};
	main_peak_mark(m);
	for (uint32_t i = 0; i < m->n; i++) {
		if (t_dst[t_list[i]] != T_EDGE)
			fail |= 8;
		t_dst[t_list[i]] = 0;
	}
	for (uint32_t i = 0; i < T_PITCH * T_H; i++)
		if (t_dst[i])
			fail |= 8;
	return fail;
}

/* The mark matches the reference; bad is the failure bit otherwise. */
static uint32_t t_check(uint32_t w, uint32_t h, uint32_t thr, uint32_t bad)
{
	struct main_peak_mark m;
	uint32_t n = t_reference(w, h, thr), fail = t_run(&m, w, h, thr, T_MAX);

	if (m.n != n || m.capped)
		return fail | bad;
	for (uint32_t i = 0; i < n; i++)
		if (t_list[i] != t_ref[i])
			return fail | bad;
	return fail;
}

static void t_fill(uint32_t level_mask)
{
	for (uint32_t i = 0; i < T_PITCH * T_H; i++)
		t_ln[i] = (uint8_t)(t_rand() & level_mask);
}

static void t_clear(void)
{
	for (uint32_t i = 0; i < T_PITCH * T_H; i++)
		t_ln[i] = 0;
}

static uint32_t t_patterns(void)
{
	uint32_t fail = 0;

	t_clear();
	t_ln[5 * T_PITCH + 100] = 200;			/* isolated: dark */
	for (uint32_t x = 30; x < 60; x++)		/* line: lit but its ends */
		t_ln[3 * T_PITCH + x] = 90;
	for (uint32_t y = 7; y < 9; y++)		/* 2x2 block: all four lit */
		for (uint32_t x = 200; x < 202; x++)
			t_ln[y * T_PITCH + x] = 128;
	for (uint32_t y = 0; y < T_H; y++)		/* border columns and the word edges */
		for (uint32_t x = 0; x < T_PITCH; x += 31)
			t_ln[y * T_PITCH + x] = 255;
	for (uint32_t y = 0; y < T_H; y++)
		t_ln[y * T_PITCH + T_PITCH - 1] = 255;
	fail |= t_check(T_PITCH, T_H, 40, 4);
	fail |= t_check(T_PITCH, T_H, 128, 4);
	fail |= t_check(64, 6, 40, 4);
	return fail;
}

static uint32_t t_random(void)
{
	static const uint32_t widths[] = { 320, 316, 36 };
	static const uint32_t thrs[] = { 1, 24, 56, 128 };
	uint32_t fail = 0;

	for (unsigned int r = 0; r < 4; r++) {
		t_fill(r & 1 ? 0xFF : 0x3F);
		for (unsigned int i = 0; i < sizeof(widths) / sizeof(widths[0]); i++)
			for (unsigned int j = 0; j < sizeof(thrs) / sizeof(thrs[0]); j++)
				fail |= t_check(widths[i], T_H - r, thrs[j], 1);
	}
	return fail;
}

static uint32_t t_capped(void)
{
	struct main_peak_mark m;
	uint32_t n, fail;

	t_fill(0xFF);
	n = t_reference(T_PITCH, T_H, 64);
	fail = t_run(&m, T_PITCH, T_H, 64, n / 2);
	if (n < 4 || !m.capped || m.n != n / 2)
		return fail | 2;
	for (uint32_t i = 0; i < m.n; i++)
		if (t_list[i] != t_ref[i])
			return fail | 2;
	return fail;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = t_random() | t_capped() | t_patterns();
}
