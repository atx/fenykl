/*
 * main: the "screenshot" Fn action, the shot library (sdk/docs/shot.md) in live view and the menu,
 * where the live view keeps running behind it. The release of a short Fn takes the snapshot; each
 * later tick writes a strip.
 */
#include "main.h"
#include <shot.h>

static bool act_shot_claim(void)
{
	uint32_t ss = FENYKL_GUI_SUPER(fenykl_gui_state());

	return ss == FENYKL_GUI_SS_REC || ss == FENYKL_GUI_SS_MENU;
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
