/*
 * Internals of the settings page library (ui.c), shared with its simulator test
 * (tests/sim/ui/test_ui.c): the state, the stock list slot and the page model.
 */
#ifndef _FENYKL_UI_INT_H
#define _FENYKL_UI_INT_H

#include "cfg.h"
#include "ui.h"

#define FUI_KEY_UP 0x9004u
#define FUI_KEY_DOWN 0x9006u
#define FUI_KEY_LEFT 0x9008u
#define FUI_KEY_RIGHT 0x900Au
#define FUI_KEY_OK 0x900Cu
#define FUI_KEY_MENU 0x900Eu
#define FUI_DIAL_DOWN 0x9031u		/* direction [open]: the camera run logs it */
#define FUI_DIAL_UP 0x9032u
#define FUI_DIAL_STEPS 10

/* GUI data store slot 0xF: the item list the stock menu list view draws, written the way
   custom_menu_build writes it. */
#define FUI_SLOT_ID 0xFu
#define FUI_PAGE_ROWS 7
#define FUI_VALUE_LEN 15		/* u16 incl. the NUL */
#define FUI_LABEL_LEN 16		/* cfg labels: at most 15 characters */
#define FUI_ROW_NORMAL 1
#define FUI_ROW_SELECTED 2
#define FUI_KIND_TEXT 9			/* v[] is a NUL-terminated UTF-16 string */
#define FUI_HELP 0x455u			/* row help ids of the stock J list */
#define FUI_HELP_SELECTED 0x45Bu
#define FUI_TITLE 0x122u		/* "Fenykl" (the overridden Eye-Fi title) */
#define FUI_ARROW_LEFT 0xA05Fu		/* icon characters of the camera font */
#define FUI_ARROW_RIGHT 0xA029u

struct fui_row {
	uint8_t state;
	uint8_t kind;
	uint16_t title;			/* lang id; 0 hides the title */
	uint16_t help;
	uint16_t v[FUI_VALUE_LEN];
};

struct fui_slot {
	uint16_t title;			/* lang id of the header */
	int8_t count;
	int8_t visible;			/* min(7, count - 7 * page) */
	uint16_t page;			/* cursor / 7 */
	uint16_t pad;
	struct fui_row row[FENYKL_UI_OPTS_MAX];
};
_Static_assert(sizeof(struct fui_row) == 0x24, "row layout");
_Static_assert(sizeof(struct fui_slot) == 0x2D8, "slot 0xF layout");

/* Lang ids lent to the page while it is open: the titles of custom_menu_items 0..20 (tabs A..D,
   never on screen with the page). Row k uses id k; the last one is the "not saved" header. */
#define FUI_BORROWED (FENYKL_UI_OPTS_MAX + 1)
#define FUI_TITLE_UNSAVED FENYKL_UI_OPTS_MAX

enum fui_result {
	FUI_NONE,			/* nothing changed */
	FUI_REDRAW,
	FUI_LEAVE,			/* MENU: back to the Custom list */
};

struct fui_state {
	/* page model (GUI task) */
	const struct fenykl_cfg_opt *opts[FENYKL_UI_OPTS_MAX];
	unsigned int nopts, cursor;
	bool edit;
	int32_t edit_val;
	uint32_t fails_at_open;		/* cfg save_fail when the page opened */
	volatile bool unsaved;		/* fenykl_ui_tick: dirty and the card gate closed or a save failed */
	struct fui_slot slot;

	/* language (GUI task while open; the tick otherwise) */
	bool installed;
	uint32_t lang_arr, lang_last_arr, lang_same;
	volatile uint32_t lent_arr;	/* table the borrowed ids are set in, 0 if none */
	uint32_t lent_orig[FUI_BORROWED];
	uint8_t stray_ticks;
	uint16_t labels[FENYKL_UI_OPTS_MAX][FUI_LABEL_LEN];

	uint32_t stock_ok, stock_right;
	volatile struct fenykl_ui_stats st;
};

extern struct fui_state fui;
extern const uint16_t fui_borrowed_ids[FUI_BORROWED];

/* Fills the list from the options in [begin, end): 0 or FENYKL_UI_EOPTS. */
int fui_collect(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end);
enum fui_result fui_key(unsigned int code);
/* The value column of option o (the cursor row shows the edit value with arrows). */
void fui_value_text(const struct fenykl_cfg_opt *o, uint16_t out[FUI_VALUE_LEN]);
/* Fills fui.slot from the model. */
void fui_build_slot(void);
/* Lends the borrowed ids of the language array arr to the page (labels from fui.opts). */
void fui_lang_lend(uint32_t arr);
/* Gives them back; does nothing if none are lent. */
void fui_lang_return(void);

/* GUI actions of super state 0x0D */
unsigned int fui_on_entry(void *sm, unsigned int code, unsigned int p, void *p2);
unsigned int fui_on_exit(void *sm, unsigned int code, unsigned int p, void *p2);
unsigned int fui_on_key(void *sm, unsigned int code, unsigned int p, void *p2);
unsigned int fui_on_sys(void *sm, unsigned int code, unsigned int p, void *p2);

#endif
