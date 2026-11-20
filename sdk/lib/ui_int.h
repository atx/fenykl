/*
 * Internals of the settings tab library, shared by its option core (ui.c), the camera's tab
 * (ui_<camera>.c) and their simulator test (tests/sim/ui_em10ii): the state, the tab's tables and
 * the option list.
 */
#ifndef _FENYKL_UI_INT_H
#define _FENYKL_UI_INT_H

#include "cfg.h"
#include "ui.h"

#define FUI_TEXT_MAX 32u		/* inline ASCII labels (kind 5) */
#define FUI_NROWS (FENYKL_UI_OPTS_MAX + 1u)	/* the info row, then the options */
#define FUI_TABS 11u			/* stock Custom tabs A..K */
#define FUI_NWORDS (FENYKL_UI_OPTS_MAX + 20u)

/* Label descriptor: kind 4 = lang id, kind 5 = inline ASCII. */
struct fui_desc {
	uint16_t kind, lang;
	uint32_t text;
};

/* Value cell, option cell, letter title or help: matched by value (or tab id). */
struct fui_cell {
	uint32_t value;
	struct fui_desc d;
};

/* Row record: value cell count, option count, title, help, value cells, the two-setting record
   (stock only), options. */
struct fui_row {
	uint16_t id, nvals;
	uint32_t nopts;
	struct fui_desc title, help;
	uint32_t vals, pair, opts;
};

/* L1 record: fn is called instead of creating the level-2 picker. */
struct fui_l1 {
	uint32_t kind;
	uint16_t row, zero;
	uint32_t fn;
};

/* Dispatch record of a RIGHT or OK table. */
struct fui_rec {
	uint32_t level;
	uint16_t row, sub;
	uint32_t fn, group, target, fn2;
};

_Static_assert(sizeof(struct fui_cell) == 12, "cell layout");
_Static_assert(sizeof(struct fui_row) == 36, "row record layout");
_Static_assert(sizeof(struct fui_l1) == 12, "L1 record layout");
_Static_assert(sizeof(struct fui_rec) == 24, "dispatch record layout");

struct fui_tables {
	struct fui_row rows[FUI_NROWS];
	struct fui_cell info;
	struct fui_cell vals[FENYKL_UI_OPTS_MAX];
	struct fui_cell opts[FENYKL_UI_OPTS_MAX][FENYKL_UI_VALUES_MAX];
	struct fui_l1 l1[FENYKL_UI_OPTS_MAX];
	struct fui_rec right[2], ok[2];		/* the info row's record twice: the stock count */
	uint16_t ids[FUI_NROWS];
	uint16_t tabs[FUI_TABS + 1];
	struct fui_cell titles[FUI_TABS + 1], helps[FUI_TABS + 1];
	struct fui_rec c30_right[FUI_TABS + 1], c30_ok[FUI_TABS + 1];
};

/* A stock word the install swaps: stock -> ours. */
struct fui_word {
	uint32_t addr, stock, ours;
};

struct fui_state {
	const struct fenykl_cfg_opt *opts[FENYKL_UI_OPTS_MAX];
	unsigned int nopts;
	bool installed;
	uint32_t fails_at_open;		/* cfg save_fail when the last option list opened */
	volatile bool unsaved;		/* fenykl_ui_tick: dirty and the card gate closed or a save failed */

	/* the open option list (GUI task) */
	unsigned int row;		/* option index */
	uint32_t pk;			/* the picker we patched */
	unsigned int cap;		/* entries of its blocks */

	unsigned int nwords, done;	/* words to swap, swapped */
	struct fui_word w[FUI_NWORDS];
	struct fui_tables t;
	volatile struct fenykl_ui_stats st;
};

extern struct fui_state fui;

/* A stock word the tab relies on: size 1, 2 or 4 bytes at addr. */
struct fui_expect {
	uint32_t addr;
	uint8_t size;
	uint32_t value;
	const char *what;
};

/* Counts a GUI callback running on CPU1: true there, false on CPU0. */
bool fui_count_core(void);
/* fenykl_ui_install's start: FENYKL_UI_EBUSY once installed, else the list from the registered
   options (FENYKL_UI_EOPTS logged). */
int fui_prepare(void);
/* Every word of tab[n] holds its value; each mismatch is logged. */
bool fui_check_words(const struct fui_expect *tab, unsigned int n);
/* fenykl_ui_tick's part on every body: fui.unsaved, its change to "not saved" logged. */
void fui_tick_unsaved(void);

/* Fills the list from the options with a group in [begin, end), groups sorted by order: 0, or
   FENYKL_UI_EOPTS (logged) for none, more than FENYKL_UI_OPTS_MAX, an INT, an ENUM with more than
   FENYKL_UI_VALUES_MAX names or a label, name or group name over FUI_TEXT_MAX characters. */
int fui_collect(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end);
/* The values of option o (BOOL 2, ENUM its names) and its current one (0 if out of range). */
unsigned int fui_nvalues(const struct fenykl_cfg_opt *o);
unsigned int fui_value(const struct fenykl_cfg_opt *o);
/* fenykl_cfg_set, counted and logged. */
void fui_set(const struct fenykl_cfg_opt *o, int32_t v);
/* Bookkeeping of an option list opening: counter, cfg save failures so far. */
void fui_list_open(void);

#endif
