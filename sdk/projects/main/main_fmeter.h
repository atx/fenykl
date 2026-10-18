/*
 * main: the focus meter's area choice and scale, free of firmware calls so that the
 * simulator test (tests/sim/main/) runs the real code. Constants from the camera runs of
 * 2026-10-16.
 */
#ifndef _MAIN_FMETER_H
#define _MAIN_FMETER_H

#include <stdbool.h>
#include <stdint.h>

#define MAIN_FMETER_CELLS 10u
#define MAIN_FMETER_ROW_WORDS 16u	/* u32 per grid row in a channel (g_area_H_size) */
#define MAIN_FMETER_AREA_NONE (-1)	/* no valid target: the centre area */

/*
 * Absolute scale: lit = (log2(v) + offset - LO) * 10 / (HI - LO), rounded, clamped to 0..10; log2 in
 * 1/256 units (MAIN_FMETER_LOG2_ONE). Camera runs (2026-10-16), HPF3 of
 * the area: framing log2 ~10.5-18.5, mostly 13-16, blank wall 15.1, sharp framing after S-AF lock
 * 18.8-19.0: the wall shows 5 cells, a sharp contrasty subject 10.
 */
#define MAIN_FMETER_LOG2_ONE 256
#define MAIN_FMETER_LO (11 * MAIN_FMETER_LOG2_ONE)
#define MAIN_FMETER_HI (19 * MAIN_FMETER_LOG2_ONE)
/*
 * Half-height grid patterns (4..7, 0x13..0x16: 23 instead of 45 lines per area), used by the S-AF
 * scan, read ~1.5 log2 lower than framing (pattern 0xD) on the same scene; the HPF3 cut-off alone
 * (1600 vs 2400 at pattern 0xD, blank wall) changed it by 0.1.
 */
#define MAIN_FMETER_HALF_OFFSET (3 * MAIN_FMETER_LOG2_ONE / 2)
#define MAIN_FMETER_PEAK_MS 1500u	/* the peak cell stays this long */
#define MAIN_FMETER_STALE_MS 500u	/* no new frame this long: the statistics stopped */
#define MAIN_FMETER_HOLD_MS 3000u	/* last bar kept after the data and the half-press end */

struct main_fmeter_in {
	bool fresh;		/* a new frame since the last step; v and key are valid */
	uint32_t v;		/* the area's value */
	uint8_t pattern;	/* grid pattern */
	bool half;		/* shutter half-pressed */
	bool hide;		/* no statistics source (MF without the own client): hidden at once */
};

struct main_fmeter_bar {
	bool shown;
	uint8_t lit;		/* cells lit */
	uint8_t peak;		/* peak-hold cell number (1..10) above lit, 0 if none */
	bool green;		/* all cells lit: drawn green */
};

struct main_fmeter {
	int32_t level;		/* last value's log2 with the pattern offset, 1/256 units */
	uint32_t last_fresh;	/* ms of the last new frame */
	uint32_t last_busy;	/* ms of the last new frame or half-press */
	uint32_t hold_at;	/* ms at which hold was reached */
	uint8_t hold;		/* highest cell count within MAIN_FMETER_PEAK_MS */
	bool live;		/* frames arrive */
	struct main_fmeter_bar bar;
};

/*
 * The grid area to meter: all targets -> the area with the largest h3, else
 * area (the AF target's area from the mode2fcs table); MAIN_FMETER_AREA_NONE or off the grid -> the
 * centre. h3 is a channel, rows MAIN_FMETER_ROW_WORDS apart.
 */
unsigned int main_fmeter_area(const uint32_t *h3, unsigned int cols, unsigned int rows, bool all,
			      int area);

/* log2(v) in 1/256 units, the fraction linear in the mantissa (error < 0.09); 0 for v 0. */
int32_t main_fmeter_log2(uint32_t v);

/* Cells lit for v read with grid pattern. */
uint8_t main_fmeter_cells(uint32_t v, uint8_t pattern);

/* Word offset of area in a channel. */
unsigned int main_fmeter_word(unsigned int area, unsigned int cols);

/* One step at now ms; m->bar is the bar to show. */
void main_fmeter_step(struct main_fmeter *m, const struct main_fmeter_in *in, uint32_t now);

#endif
