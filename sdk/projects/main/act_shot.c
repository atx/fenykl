/*
 * main: the "screenshot" Fn action, the shot library (sdk/docs/shot.md) in live view and the menu,
 * where the live view keeps running behind it. The release of a short Fn takes the snapshot; each
 * later tick writes a strip.
 */
#include "main.h"
#include <shot.h>

/* On the E-M10 II the first two shots of a boot also keep the raw snapshot (SHOTnnnn.LV, .L0..L2,
   .PAL, .REG), so that the colour order, palettes, alpha and layout can be checked against the PPM
   off the camera; later shots lend the block only for the PPM. */
#if FENYKL_TARGET_EM10II
#define ACT_SHOT_RAW_FIRST 2u
#else
#define ACT_SHOT_RAW_FIRST 0u
#endif

static bool act_shot_claim(void)
{
	return fenykl_gui_live_view() || fenykl_gui_menu();
}

static int act_shot_start(uint32_t press_us)
{
	(void)press_us;
	fenykl_shot_raw(ACT_SHOT_RAW_FIRST);
	return fenykl_shot_start() ? 0 : -1;
}

const struct main_fn_act act_shot = {
	.name = "screenshot",
	.log = &fenykl_shot_log,
	.claim = act_shot_claim,
	.start = act_shot_start,
	.step = fenykl_shot_step,
	.status = fenykl_shot_status,
};
