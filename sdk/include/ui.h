/*
 * Settings tab (sdk/docs/ui.md): a native Custom menu tab "L. Fenykl" with one stock row per cfg
 * option (cfg.h) and an info row, drawn and edited by the camera's own menu code. Linked into every
 * plugin. The plugin calls fenykl_ui_install and fenykl_ui_tick from its task; the rows' pickers
 * run in the GUI task (CPU0) and call fenykl_cfg_set there.
 */
#ifndef _FENYKL_UI_H
#define _FENYKL_UI_H

#include <stdbool.h>
#include <fenykl.h>
#include "log.h"

#define FENYKL_UI_OPTS_MAX 11u		/* the row-style entries a tab can borrow */
#define FENYKL_UI_VALUES_MAX 5u		/* the entries of the borrowed option picker */

#define FENYKL_UI_EAGAIN (-1)		/* the menu code is not loaded yet, or the menu is open */
#define FENYKL_UI_EOPTS (-2)		/* no option has a group, too many, or one is not shown (logged) */
#define FENYKL_UI_ESTOCK (-4)		/* a stock GUI word differs (logged): nothing patched */
#define FENYKL_UI_EBUSY (-5)		/* already installed */

/* Counters (sdk/docs/ui.md 6). */
struct fenykl_ui_stats {
	uint32_t opens;		/* option lists opened from a row */
	uint32_t sets, set_fail;	/* fenykl_cfg_set from an option list */
	uint32_t refused;	/* a picker left stock or not filled, an OK not on ours */
	uint32_t refreshes;	/* value cells rewritten by fenykl_ui_tick */
	uint32_t late;		/* 1: installed after the first MENU (letter row L opens after a rebuild) */
	uint32_t cpu1_actions;	/* must stay 0 */
};

/* Module "ui": INFO logs install, option lists and sets; DEBUG adds every picker event. */
extern struct fenykl_log_module fenykl_ui_log;

/*
 * Checks the options and every stock word, then installs the tab. All or nothing. CPU0 task, after
 * fenykl_cfg_init and before the first MENU; retry every ~100 ms while it returns FENYKL_UI_EAGAIN.
 * 0 or FENYKL_UI_E*.
 */
int fenykl_ui_install(void) FENYKL_CPU0_ONLY("fenykl_ui_install");
/* Every ~100 ms from the task: updates the value column and the saved state. */
void fenykl_ui_tick(void) FENYKL_CPU0_ONLY("fenykl_ui_tick");
void fenykl_ui_get_stats(struct fenykl_ui_stats *st);

#endif
