/*
 * Settings page (sdk/docs/ui.md): a Custom menu row becomes "Fenykl" (the Eye-Fi row of
 * tab K) and opens a page that edits the cfg options (cfg.h) in one
 * scrolling list, drawn by the camera's own menu list view. Linked into every plugin. The plugin
 * calls fenykl_ui_install and fenykl_ui_tick from its task; the page itself runs in the GUI task
 * (CPU0) and calls fenykl_cfg_set there.
 */
#ifndef _FENYKL_UI_H
#define _FENYKL_UI_H

#include <stdbool.h>
#include <fenykl.h>
#include "log.h"

#define FENYKL_UI_OPTS_MAX 20u		/* rows of the stock list's data slot */

#define FENYKL_UI_EAGAIN (-1)		/* GUI not in live view or playback, or no language yet */
#define FENYKL_UI_EOPTS (-2)		/* no option has a group, or more than FENYKL_UI_OPTS_MAX do */
#define FENYKL_UI_ESTOCK (-4)		/* a stock GUI word differs (logged): nothing patched */
#define FENYKL_UI_EBUSY (-5)		/* already installed */

/* Counters (sdk/docs/ui.md 7). */
struct fenykl_ui_stats {
	uint32_t wrap_in;	/* not counted on this body, always 0 */
	uint32_t wrap_pass;	/* RIGHT on the row */
	uint32_t refused;	/* OK on the Fenykl row, but the page is open or was not pushed */
	uint32_t entries, exits, keys, backs;
	uint32_t sys;		/* events passed to the menu app's default handler */
	uint32_t ignored;	/* key releases */
	uint32_t sets, set_fail;	/* fenykl_cfg_set from the page */
	uint32_t publishes, publish_fail;	/* gui_data_set of the list slot; fail = did not return 1 */
	/* not counted on this body, always 0 */
	uint32_t lends, returns, lang_stray;
	uint32_t lang_reapply, lang_unstable, flag_missing;
	uint32_t cpu1_actions;	/* must stay 0 */
};

/* Module "ui": INFO logs entry, exit, sets and install; DEBUG adds every key. */
extern struct fenykl_log_module fenykl_ui_log;

/*
 * Checks the options and every stock word, then installs the row and the page. All or nothing. CPU0 task, after
 * fenykl_cfg_init; retry every ~100 ms while it returns FENYKL_UI_EAGAIN. 0 or FENYKL_UI_E*.
 */
int fenykl_ui_install(void) FENYKL_CPU0_ONLY("fenykl_ui_install");
/* Every ~100 ms from the task: updates the saved state. */
void fenykl_ui_tick(void) FENYKL_CPU0_ONLY("fenykl_ui_tick");
void fenykl_ui_get_stats(struct fenykl_ui_stats *st);

#endif
