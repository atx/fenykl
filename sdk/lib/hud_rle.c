/*
 * HUD library: the fmt 0x03 encoder (hud_int.h), the GUI's 8-bpp run-length format, for the E-M10
 * II's image widgets.
 */
#include "hud_int.h"

#define HUD_RLE_FMT 0x03u
#define HUD_RLE_RUN_MAX 4
#define HUD_RLE_CODES 6		/* per u64 */

/* Puts 10-bit code k (0..5) into the u64 {lo, hi}. */
static void hud_rle_put(uint32_t *q, int k, uint32_t code)
{
	int b = 10 * k;

	if (b >= 32) {
		q[1] |= code << (b - 32);
		return;
	}
	q[0] |= code << b;
	if (b > 22)
		q[1] |= code >> (32 - b);
}

int hud_rle_encode(const uint8_t *px, int w, int h, uint32_t *out, int max_words)
{
	uint32_t *q = out + 1;
	int n = 0;

	if (max_words < 1)
		return -1;
	out[0] = (uint32_t)(h - 1) | (uint32_t)(w - 1) << 12 | HUD_RLE_FMT << 24;
	for (int y = 0; y < h; y++) {
		const uint8_t *row = px + y * w;

		for (int x = 0; x < w;) {
			int run = 1;

			while (run < HUD_RLE_RUN_MAX && x + run < w && row[x + run] == row[x])
				run++;
			int k = n % HUD_RLE_CODES;

			if (k == 0) {
				if (q + 2 > out + max_words)
					return -1;
				q[0] = 0;
				q[1] = 0;
			}
			hud_rle_put(q, k, (uint32_t)row[x] << 2 | (uint32_t)(run - 1));
			if (k == HUD_RLE_CODES - 1)
				q += 2;
			n++;
			x += run;
		}
	}
	int tail = n % HUD_RLE_CODES;

	return 4 + 8 * (n / HUD_RLE_CODES) + (tail ? (10 * tail + 7) / 8 : 0);
}
