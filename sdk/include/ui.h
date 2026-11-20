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
#include "cfg.h"
#include "log.h"

#define FENYKL_UI_OPTS_MAX 40u		/* option rows: 41 rows, 6 pages (camera-tested) */
#define FENYKL_UI_VALUES_MAX 26u	/* names of one ENUM: the stock style-1 maximum */
#define FENYKL_UI_CELLS_MAX 128u	/* values of all options together (BOOL 2) */

#define FENYKL_UI_EAGAIN (-1)		/* the menu code is not loaded yet, or the menu is open */
#define FENYKL_UI_EOPTS (-2)		/* no option has a group, too many, or one is not shown (logged) */
#define FENYKL_UI_ESTOCK (-4)		/* a stock GUI word differs (logged): nothing patched */
#define FENYKL_UI_EBUSY (-5)		/* already installed */
#define FENYKL_UI_ENOTINST (-6)	/* not installed */

/* Counters (sdk/docs/ui.md 6). */
struct fenykl_ui_stats {
	uint32_t opens;		/* option lists opened from a row */
	uint32_t sets, set_fail;	/* fenykl_cfg_set from an option list */
	uint32_t refused;	/* a picker left stock or not filled, an OK not on ours */
	uint32_t refreshes;	/* value cells rewritten by fenykl_ui_tick */
	uint32_t late;		/* 1: installed after the first MENU (letter row L opens after a rebuild) */
	uint32_t reallocs;	/* picker arrays replaced for an ENUM above the donor's 5 entries */
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
/*
 * fenykl_ui_install with the options in [begin, end) instead of the cfg library's: RAM only, a set
 * stores the value and calls on_change, nothing is saved (tests, experiments).
 */
int fenykl_ui_install_opts(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end)
	FENYKL_CPU0_ONLY("fenykl_ui_install_opts");
/*
 * Puts every stock word back, last installed first. CPU0 task, menu closed. The tables stay
 * resident: a page actuator built while installed still points at them. 0, FENYKL_UI_ENOTINST,
 * FENYKL_UI_EAGAIN (menu open) or FENYKL_UI_ESTOCK (a word no longer ours, left, logged).
 */
int fenykl_ui_remove(void) FENYKL_CPU0_ONLY("fenykl_ui_remove");
/* Every ~100 ms from the task: updates the value column and the saved state. */
void fenykl_ui_tick(void) FENYKL_CPU0_ONLY("fenykl_ui_tick");
void fenykl_ui_get_stats(struct fenykl_ui_stats *st);

#endif
