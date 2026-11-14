/*
 * E-M10 II key map of the key layer (key.c). UI_IN posts the E-PL3's event shapes (press
 * 0x0200cccc, release 0x0201cccc, value 0x0202cccc, param and flag 0). "static" = the v1.4 key
 * kind tables 0x601A90EC (entry type 2 = button) only, no event seen yet.
 */
#include "key_int.h"

/* "camera" = seen through the hook on the camera (2026-10-19/20) */
const uint16_t fenykl_key_codes[FENYKL_KEY_COUNT] = {
	[FENYKL_KEY_OK] = 0x0004,	/* camera */
	[FENYKL_KEY_UP] = 0x0005,	/* camera */
	[FENYKL_KEY_DOWN] = 0x0006,	/* camera */
	[FENYKL_KEY_LEFT] = 0x0007,	/* camera */
	[FENYKL_KEY_RIGHT] = 0x0008,	/* static (kind 5 map entry 4); its key state seen on the camera */
	[FENYKL_KEY_INFO] = 0x1205,	/* camera */
	[FENYKL_KEY_MENU] = 0x0089,	/* camera */
	[FENYKL_KEY_PLAY] = 0x0087,	/* camera */
	[FENYKL_KEY_DEL] = 0x0084,	/* camera */
	[FENYKL_KEY_FN] = 0x000B,	/* camera: Fn1 */
	[FENYKL_KEY_REC] = 0x120A,	/* camera: started and stopped recording */
	[FENYKL_KEY_HALF] = 0x0100,	/* camera */
	[FENYKL_KEY_FULL] = 0x0101,	/* camera */
	/* POWER: the lever is a switch (kind 0 map entry 0, type 1) posting the value 0x0202000C;
	   mapped, a claimer could consume the power-off event. */
	[FENYKL_KEY_FN2] = 0x1209,	/* camera */
	[FENYKL_KEY_FN3] = 0x1224,	/* camera */
	[FENYKL_KEY_LFN] = 0x2202,	/* static (kind 7 map entry 11); level set by the lens driver */
	[FENYKL_KEY_MODE_DIAL] = 0x1901,	/* camera: one VALUE per position */
};
