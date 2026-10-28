/*
 * main: the peaking mark, free of firmware calls (simulator test tests/sim/main/).
 */
#ifndef _MAIN_PEAK_MARK_H
#define _MAIN_PEAK_MARK_H

#include <stdbool.h>
#include <stdint.h>

#define MAIN_PEAK_PITCH 320u		/* line image and overlay pixels per row */
#define MAIN_PEAK_BM_PITCH 12u		/* bitmap words per row: a zero word, 10 x 32 pixels, a zero word */
#define MAIN_PEAK_BM_WORDS(h) (MAIN_PEAK_BM_PITCH * (h))

/* One mark of a line image into an overlay buffer. */
struct main_peak_mark {
	const uint8_t *ln;		/* line image, MAIN_PEAK_PITCH bytes per row, word aligned */
	uint32_t *bm;			/* scratch, MAIN_PEAK_BM_WORDS(h) */
	volatile uint32_t *dst;		/* overlay, MAIN_PEAK_PITCH pixels per row, all 0 */
	uint32_t *list;			/* out: the lit pixels' indices into dst */
	uint32_t w, h;			/* area from 0,0: w a multiple of 4, <= MAIN_PEAK_PITCH */
	uint32_t thr;			/* 1..128 */
	uint32_t max;			/* entries of list */
	uint32_t edge;			/* lit pixel value */
	uint32_t n;			/* out: lit pixels */
	bool capped;			/* out: more than max would have lit */
};

/*
 * Lights a pixel where it and at least two of its 8 neighbours reach thr; the border rows and
 * columns stay dark.
 */
void main_peak_mark(struct main_peak_mark *m);

#endif
