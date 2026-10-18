/*
 * main: the hook adapter. FENYKL_HOOK_* and the originals are generated from plugin.toml, so
 * the hooks are bound here and only call library entry points. Exception: img_exec is bound in
 * main_rbswap_hook.c, which the simulator test runs on its own.
 */
#include "main.h"

struct main_hooks_state {
	volatile bool draw_off;		/* set after a CPU1 call */
};

static struct main_hooks_state main_hooks;

/* GUI task: copies the HUD elements into the buffer about to be shown. */
static void main_hooks_update(int show_front)
{
	if (!main_hooks.draw_off)
		fenykl_hud_frame(show_front);
	FENYKL_ORIG(cw_updateDisplay)(show_front);
}
FENYKL_HOOK(cw_updateDisplay, main_hooks_update);

int main_hooks_install(void)
{
	return fenykl_hook_install(&fenykl_hook_cw_updateDisplay);
}

/* The hook keeps calling the original but no longer draws. */
void main_hooks_draw_off(void)
{
	main_hooks.draw_off = true;
}
