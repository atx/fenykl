/*
 * The detection the FD hook injects (hook.c), as pure code (also run in the GDB simulator,
 * tests/sim/birddetect6): the fenykl-nn head post-processing of the two logit grids (one fire
 * decision on the sum of the presence grid, a 3x3 centroid around the top cell of the heat map), and
 * the face-detector candidate at that point. The
 * candidate sits on the latest point every live FD cycle; no slew limit.
 */
#ifndef BD6_INJECT_H
#define BD6_INJECT_H

#include <stdbool.h>
#include <stdint.h>

#define BD6_INJ_SIZE 90			/* raw FD size: output 108 < 128, no CPU1 eye detection */
#define BD6_INJ_CONF 1665		/* raw confidence 15 x 111 */
#define BD6_INJ_MAX_AGE_US 300000u	/* an older target clears the list */
/* The 4:3 track window of the FD tracker. */
#define BD6_INJ_Y_MIN 34
#define BD6_INJ_Y_MAX 445

/* Target word: 0 = no bird, else BD6_TGT_VALID | y << 16 | x (FD coordinates 640x480). */
#define BD6_TGT_VALID 0x80000000u
#define BD6_TGT(x, y) (BD6_TGT_VALID | (uint32_t)(y) << 16 | (uint32_t)(x))

/* One frame's post-processing. */
struct bd6_det {
	int32_t score;		/* presence logits summed over all cells */
	bool fire;		/* score >= bd6_threshold */
	uint8_t row, col;	/* top heat cell: the first maximum in raster order */
	uint16_t x_q4, y_q4;	/* centroid in 1/16 input pixels */
};

/* bd6_model.c: bd6_post of each self-test pattern (enum bd6_pattern). */
extern const struct bd6_det bd6_selftest_det[];

/* Post-processing of the logits (net.h: heat map, presence) with the model's threshold and centroid
   table. */
void bd6_post(const int32_t *logit, struct bd6_det *d);
bool bd6_det_eq(const struct bd6_det *a, const struct bd6_det *b);
/* FD coordinates of a point in 1/16 input pixels, y clamped into the track window. */
uint32_t bd6_inj_target(unsigned int x_q4, unsigned int y_q4);
/* The candidate position of target into *x, *y; false if there is none: no target, or the target
   is older than BD6_INJ_MAX_AGE_US. */
bool bd6_inj_pos(uint32_t target, uint32_t age_us, int16_t *x, int16_t *y);

#endif
