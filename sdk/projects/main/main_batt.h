/*
 * main: battery charge from the battery-check A/D value (bchk_ad_read channel 4), free of
 * firmware calls so that the simulator test (tests/sim/main/) runs the real code.
 */
#ifndef _MAIN_BATT_H
#define _MAIN_BATT_H

#include <stdint.h>

/*
 * Pack voltage per A/D count. One multimeter point: 7.999 V open circuit read 819-821 at boot,
 * before live view; the offset is unknown and taken as 0. A second point replaces this by a
 * line through both.
 */
#define MAIN_BATT_UV_PER_COUNT 9770u
#define MAIN_BATT_CELLS 2u
/* Live view reads about 7 counts below open circuit (boot 821-823, live view 812-816 [camera]) */
#define MAIN_BATT_LV_SAG 7u
#define MAIN_BATT_MEDIAN 5u		/* samples: rejects the AF dips (5 more counts, < 2 s) */
#define MAIN_BATT_EMA_SHIFT 3u		/* time constant 8 samples */
#define MAIN_BATT_EMA_FRAC 4u		/* fraction bits of the filtered value */
#define MAIN_BATT_HYST 10u		/* permille the charge must move past the shown % */
#define MAIN_BATT_NONE 0xFFu

struct main_batt {
	uint16_t win[MAIN_BATT_MEDIAN];	/* last samples, counts */
	uint8_t n;			/* samples in win */
	uint8_t next;			/* win index of the next sample */
	uint8_t shown;			/* percentage, MAIN_BATT_NONE before the first sample */
	uint32_t ema;			/* filtered counts << MAIN_BATT_EMA_FRAC */
};

/* Open-circuit cell voltage in mV for a filtered value (counts << MAIN_BATT_EMA_FRAC). */
uint32_t main_batt_cell_mv(uint32_t ema);
/* State of charge in permille for an open-circuit cell voltage in mV, clamped to 0..1000. */
uint32_t main_batt_soc(uint32_t cell_mv);
void main_batt_reset(struct main_batt *b);
/* Adds one live-view sample (raw counts) and returns the percentage to show, 0..100. */
unsigned int main_batt_add(struct main_batt *b, uint32_t counts);

#endif
