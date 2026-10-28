/*
 * main: the img_exec hook, shared by the R/B swap (main_rbswap_hook.c), which changes the pass,
 * and peaking (main_peak.c), which follows a live-view pass with a pass of its own.
 */
#include "main.h"
#include "main_peak.h"
#include "main_rbswap.h"

static unsigned int main_img_exec(void *ctrl)
{
	uint32_t t_start = FENYKL_TIMER_NOW();

	return main_peak_after(ctrl, main_rbswap_exec(ctrl), t_start);
}
FENYKL_HOOK(img_exec, main_img_exec);

int main_img_hook_install(void)
{
	return fenykl_hook_install(&fenykl_hook_img_exec);
}
