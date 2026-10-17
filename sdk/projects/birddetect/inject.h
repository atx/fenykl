/*
 * The face-detector candidate that stands for the bird (hook.c), as pure code (also run in the
 * GDB simulator, tests/sim/birddetect). The control task publishes the chosen cell as a target
 * word; each live FD cycle moves the candidate towards it by at most BD_INJ_STEP per axis, so the
 * firmware's tracker keeps one track (it matches within +-size/2).
 */
#ifndef BD_INJECT_H
#define BD_INJECT_H

#include <stdbool.h>
#include <stdint.h>

#define BD_INJ_SIZE 90			/* raw FD size: output 108 < 128, no CPU1 eye detection */
#define BD_INJ_STEP (BD_INJ_SIZE / 4)
#define BD_INJ_CONF 1665		/* raw confidence 15 x 111 */
#define BD_INJ_MAX_AGE_US 300000u	/* an older target clears the list */
/* The 4:3 track window of the FD tracker. */
#define BD_INJ_Y_MIN 34
#define BD_INJ_Y_MAX 445

/* Target word: 0 = no bird, else BD_TGT_VALID | y << 16 | x (FD coordinates 640x480). */
#define BD_TGT_VALID 0x80000000u
#define BD_TGT(x, y) (BD_TGT_VALID | (uint32_t)(y) << 16 | (uint32_t)(x))

struct bd_inj {
	int16_t cx, cy;		/* the candidate of the last cycle */
	bool valid;		/* cx, cy hold it */
};

/* FD coordinates of the centre of grid cell (cx, cy), y clamped into the track window. */
uint32_t bd_inj_target(unsigned int cx, unsigned int cy);
/* The candidate for this cycle into s->cx, s->cy; false (and s->valid cleared) if there is none:
   no target, or the target is older than BD_INJ_MAX_AGE_US. */
bool bd_inj_step(struct bd_inj *s, uint32_t target, uint32_t age_us);

#endif
