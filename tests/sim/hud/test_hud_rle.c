/* The HUD library's fmt 0x03 encoder (sdk/lib/hud_rle.c) in the GDB simulator, linked with
   ../hooks_em10ii/sim_start.S: images of several shapes are encoded and read back with a decoder written
   from the format (as fenykl.assets and the GUI read it: header {h-1:12, w-1:12, fmt:8}, six 10-bit
   codes colour << 2 | run-1 per little-endian u64), checking the pixels, that no run crosses a row
   end, the stock size rule and the refusal of a short buffer. plugin_main sets sim_exit_code to 0 or
   the number of the first failed check. */
#include <stdint.h>
#include "../../../sdk/lib/hud_int.h"

#define T_MAX_W 61
#define T_MAX_H 23

uint32_t sim_exit_code = 99;

static uint8_t px[T_MAX_W * T_MAX_H], back[T_MAX_W * T_MAX_H];
static uint32_t out[HUD_RLE_WORDS(T_MAX_W, T_MAX_H)];
static uint32_t seed = 12345;

static uint32_t rnd(void)
{
	seed = seed * 1103515245u + 12345u;
	return seed >> 16;
}

/* Decodes out into back; returns the number of codes used, or -1 on a malformed stream. */
static int decode(int w, int h)
{
	int n = 0, col = 0, i = 0;

	if ((out[0] & 0xFFFu) != (uint32_t)(h - 1) || (out[0] >> 12 & 0xFFFu) != (uint32_t)(w - 1) ||
	    out[0] >> 24 != 3u)
		return -1;
	while (i < w * h) {
		const uint32_t *q = out + 1 + 2 * (n / 6);
		uint64_t word = (uint64_t)q[1] << 32 | q[0];
		uint32_t code = (uint32_t)(word >> (10 * (n % 6))) & 0x3FFu;
		int run = (int)(code & 3u) + 1;

		if (col + run > w)
			return -1;
		for (int k = 0; k < run; k++)
			back[i++] = (uint8_t)(code >> 2);
		col = (col + run) % w;
		n++;
	}
	return n;
}

static int check(int w, int h)
{
	int size = hud_rle_encode(px, w, h, out, HUD_RLE_WORDS(w, h));
	int n = decode(w, h);

	if (n < 0)
		return 1;
	for (int i = 0; i < w * h; i++)
		if (back[i] != px[i])
			return 2;
	if (size != 4 + 8 * (n / 6) + (n % 6 ? (10 * (n % 6) + 7) / 8 : 0))
		return 3;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	int fail = 0;
	/* noise, two-colour stripes, one colour, single pixel, every colour value */
	for (int t = 0; t < 5 && !fail; t++) {
		for (int i = 0; i < T_MAX_W * T_MAX_H; i++)
			px[i] = t == 0 ? (uint8_t)(rnd() % 3) :
				t == 1 ? (uint8_t)(i / 7 % 2 ? 0x11 : 0x04) :
				t == 2 ? 0x10 : t == 3 ? 0 : (uint8_t)i;
		int w = t == 3 ? 1 : T_MAX_W - t, h = t == 3 ? 1 : T_MAX_H - t;

		fail = check(w, h);
		if (fail)
			fail += 10 * t;
	}
	/* the stock 50 x 38 ISO icon size, all one colour: 13 codes per row, 494 codes */
	if (!fail) {
		for (int i = 0; i < 50 * 38; i++)
			px[i] = 5;
		if (hud_rle_encode(px, 50, 38, out, HUD_RLE_WORDS(50, 38)) != 4 + 8 * 82 + 3)
			fail = 60;
	}
	if (!fail && hud_rle_encode(px, 50, 38, out, 10) != -1)
		fail = 61;
	/* one colour fits the flat bound exactly */
	if (!fail && hud_rle_encode(px, 50, 38, out, HUD_RLE_FLAT_WORDS(50, 38)) < 0)
		fail = 62;
	if (!fail && hud_rle_encode(px, 50, 38, out, HUD_RLE_FLAT_WORDS(50, 38) - 2) != -1)
		fail = 63;
	sim_exit_code = (uint32_t)fail;
}
