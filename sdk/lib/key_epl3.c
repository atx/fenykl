/*
 * E-PL3 key map of the key layer (key.c).
 */
#include "key_int.h"

/* "camera" = seen through the hook on the camera (2026-10-17) */
const uint16_t fenykl_key_codes[FENYKL_KEY_COUNT] = {
	[FENYKL_KEY_OK] = 0x0004,	/* camera */
	[FENYKL_KEY_UP] = 0x0005,	/* camera */
	[FENYKL_KEY_DOWN] = 0x0006,	/* camera */
	[FENYKL_KEY_LEFT] = 0x0007,	/* camera */
	[FENYKL_KEY_RIGHT] = 0x0008,	/* camera */
	[FENYKL_KEY_INFO] = 0x1205,	/* camera */
	[FENYKL_KEY_MENU] = 0x0089,	/* camera */
	[FENYKL_KEY_PLAY] = 0x0087,	/* camera */
	[FENYKL_KEY_DEL] = 0x0084,	/* camera */
	[FENYKL_KEY_FN] = 0x000B,	/* camera (also 2026-10-14) */
	[FENYKL_KEY_REC] = 0x1209,	/* camera: R started and stopped recording */
	[FENYKL_KEY_HALF] = 0x0100,	/* camera; a shot: HALF, FULL press, FULL, HALF release */
	[FENYKL_KEY_FULL] = 0x0101,	/* camera */
	[FENYKL_KEY_POWER] = 0x000C,	/* not exercised */
	[FENYKL_KEY_MODE_DIAL] = 0x1901,	/* camera: one VALUE per step, poll agrees */
};
