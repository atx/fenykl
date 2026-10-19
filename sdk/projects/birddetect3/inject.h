/*
 * The detection the FD hook injects (hook.c), as pure code (also run in the GDB simulator,
 * tests/sim/birddetect3): the fenykl-nn head post-processing of a logit grid (one fire decision on
 * the top cell, a 3x3 centroid around it), and the face-detector candidate at that point. The
 * candidate sits on the latest point every live FD cycle; no slew limit.
 */
#ifndef BD3_INJECT_H
#define BD3_INJECT_H

#include <stdbool.h>
#include <stdint.h>

#define BD3_INJ_SIZE 90			/* raw FD size: output 108 < 128, no CPU1 eye detection */
#define BD3_INJ_CONF 1665		/* raw confidence 15 x 111 */
#define BD3_INJ_MAX_AGE_US 300000u	/* an older target clears the list */
/* The 4:3 track window of the FD tracker. */
#define BD3_INJ_Y_MIN 34
#define BD3_INJ_Y_MAX 445

/* Target word: 0 = no bird, else BD3_TGT_VALID | y << 16 | x (FD coordinates 640x480). */
#define BD3_TGT_VALID 0x80000000u
#define BD3_TGT(x, y) (BD3_TGT_VALID | (uint32_t)(y) << 16 | (uint32_t)(x))

/* One frame's post-processing. */
struct bd3_det {
	int32_t score;		/* logit of the top cell */
	bool fire;		/* score >= bd3_threshold */
	uint8_t row, col;	/* top cell: the first maximum in raster order */
	uint16_t x_q4, y_q4;	/* centroid in 1/16 input pixels */
};

/* Post-processing of the 12x9 logit grid with the model's threshold and centroid table. */
void bd3_post(const int32_t *logit, struct bd3_det *d);
/* FD coordinates of a point in 1/16 input pixels, y clamped into the track window. */
uint32_t bd3_inj_target(unsigned int x_q4, unsigned int y_q4);
/* The candidate position of target into *x, *y; false if there is none: no target, or the target
   is older than BD3_INJ_MAX_AGE_US. */
bool bd3_inj_pos(uint32_t target, uint32_t age_us, int16_t *x, int16_t *y);

#endif
