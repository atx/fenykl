/*
 * main: the peaking mark. Pass 1 thresholds the line image a word (4 pixels) at a time into a
 * bitmap; pass 2 counts each set pixel's 8 neighbours 32 pixels at a time with bit operations and
 * writes only the lit pixels. Its cost is the line image read plus one store per lit pixel; nothing
 * per pixel that is below the threshold beyond the word test.
 */
#include "main_peak_mark.h"

static void main_peak_bitmap(const struct main_peak_mark *m)
{
	const uint32_t k = (0x80u - m->thr) * 0x01010101u;
	const uint32_t words = m->w / 4;

	for (uint32_t y = 0; y < m->h; y++) {
		const uint32_t *q = (const uint32_t *)(m->ln + y * MAIN_PEAK_PITCH);
		uint32_t *b = m->bm + y * MAIN_PEAK_BM_PITCH;

		for (uint32_t i = 0; i < MAIN_PEAK_BM_PITCH; i++)
			b[i] = 0;
		b++;
		for (const uint32_t *p = q, *e = q + words; p < e; p++) {
			/* bit 7 of byte i: byte i >= thr, with no carry between the bytes */
			uint32_t v = *p, j;

			v = (v | ((v & 0x7F7F7F7Fu) + k)) & 0x80808080u;
			if (!v)
				continue;
			j = (uint32_t)(p - q);
			v >>= 7;
			v |= v >> 7;
			b[j / 8] |= ((v | v >> 14) & 0xFu) << (j % 8 * 4);
		}
	}
}

static void main_peak_lit(struct main_peak_mark *m, uint32_t idx, uint32_t lit)
{
	for (; lit; lit >>= 1, idx++) {
		while (!(lit & 0xFu)) {
			lit >>= 4;
			idx += 4;
		}
		if (!(lit & 1u))
			continue;
		if (m->n == m->max) {
			m->capped = true;
			return;
		}
		m->dst[idx] = m->edge;
		m->list[m->n++] = idx;
	}
}

/* Pixels with at least two of the inputs set: carry-save count of 8 bit vectors. */
#define MAIN_PEAK_ADD(v)                \
	do {                            \
		uint32_t _v = (v);      \
		twos |= ones & _v;      \
		ones |= _v;             \
	} while (0)

void main_peak_mark(struct main_peak_mark *m)
{
	uint32_t last, last_bit;

	m->n = 0;
	m->capped = false;
	if (m->w < 3 || m->h < 3)
		return;
	last = (m->w - 1) / 32;
	last_bit = 1u << (m->w - 1) % 32;
	main_peak_bitmap(m);
	for (uint32_t y = 1; y + 1 < m->h; y++) {
		const uint32_t *u = m->bm + (y - 1) * MAIN_PEAK_BM_PITCH + 1;
		const uint32_t *c = u + MAIN_PEAK_BM_PITCH, *d = c + MAIN_PEAK_BM_PITCH;

		for (uint32_t k = 0; k <= last; k++) {
			uint32_t x = c[k], ones = 0, twos = 0, lit;

			if (!x)
				continue;
			/* bit i of a neighbour vector: pixel 32 k + i's neighbour on that side */
			MAIN_PEAK_ADD(u[k] << 1 | u[k - 1] >> 31);
			MAIN_PEAK_ADD(u[k]);
			MAIN_PEAK_ADD(u[k] >> 1 | u[k + 1] << 31);
			MAIN_PEAK_ADD(x << 1 | c[k - 1] >> 31);
			MAIN_PEAK_ADD(x >> 1 | c[k + 1] << 31);
			MAIN_PEAK_ADD(d[k] << 1 | d[k - 1] >> 31);
			MAIN_PEAK_ADD(d[k]);
			MAIN_PEAK_ADD(d[k] >> 1 | d[k + 1] << 31);
			lit = x & twos;
			if (k == 0)
				lit &= ~1u;
			if (k == last)
				lit &= ~last_bit;
			if (!lit)
				continue;
			main_peak_lit(m, y * MAIN_PEAK_PITCH + 32 * k, lit);
			if (m->capped)
				return;
		}
	}
}
