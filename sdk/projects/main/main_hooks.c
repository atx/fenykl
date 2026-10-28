/*
 * main: the hook adapter. FENYKL_HOOK_* and the originals are generated from plugin.toml, so
 * the hooks are bound here and only call library entry points. Exceptions: img_exec is bound in
 * main_img_hook.c (the R/B swap and peaking), img_hw_start in main_peak.c.
 */
#include "main.h"
#if FENYKL_HAS_PRECAP
#include <precap.h>
#endif

struct main_hooks_state {
	volatile bool draw_off;		/* set after a CPU1 call */
#if FENYKL_HAS_PRECAP
	volatile uint32_t precap_calls;	/* calls while a pre-capture burst runs */
#endif
};

static struct main_hooks_state main_hooks;

/* GUI task: copies the HUD elements into the buffer about to be shown. */
static void main_hooks_update(int show_front)
{
#if FENYKL_HAS_PRECAP
	enum fenykl_precap_state pc = fenykl_precap_state();

	if (pc == FENYKL_PRECAP_RING || pc == FENYKL_PRECAP_SAVE)
		main_hooks.precap_calls++;
#endif
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

#if FENYKL_HAS_PRECAP
uint32_t main_hooks_precap_calls(void)
{
	return main_hooks.precap_calls;
}
#endif
