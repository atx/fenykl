/*
 * main: the "screenshot" Fn action, the shot library (sdk/docs/shot.md) in live view and the menu,
 * where the live view keeps running behind it. The release of a short Fn takes the snapshot; each
 * later tick writes a strip.
 */
#include "main.h"
#include <shot.h>

static bool act_shot_claim(void)
{
	return fenykl_gui_live_view() || fenykl_gui_menu();
}

static int act_shot_start(uint32_t press_us)
{
	(void)press_us;
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
