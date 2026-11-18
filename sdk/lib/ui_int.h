/*
 * Internals of the settings page library, shared by its option core (ui.c), the camera's page
 * (ui_<camera>.c) and their simulator tests (tests/sim/ui*): the state, the page model and the
 * page lifecycle.
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
/* Dial events: the posted parameter (gui_action p2) is a signed 16-bit step count, read by the
   stock menu actions with exth (StListMenu 0x6F081D5D). The rear wheel posts CTRL_RING. */
#define FUI_JOG_DIAL 0x9031u		/* EVT_ST_CHANGE_JOG_DIAL */
#define FUI_CTRL_RING 0x9032u		/* EVT_ST_CHANGE_CTRL_RING */
#define FUI_DIAL_STEPS 10

#define FUI_PAGE_ROWS 7
#define FUI_VALUE_LEN 15		/* u16 incl. the NUL */
#define FUI_LABEL_LEN 16		/* cfg labels: at most 15 characters */

/* GUI data store slot 0x38 (data.gui_data_slot38): the model of the Custom menu list. Cells start
   at offsets = 2 mod 4, so the page is built with u16 accesses only. Titles and values are cells
   {u16 kind, u16 image, u16 lang, u16 style, u16 text[33]}; kind 5 draws text verbatim. */
#define FUI_SLOT_ID 0x38u
#define FUI_SLOT_SIZE 0xC6Eu
#define FUI_SCROLL 0x0u			/* u16: scroll bar shown */
#define FUI_PAGES 0x2u			/* u16 */
#define FUI_PAGE 0x4u			/* u16 */
#define FUI_CURSOR 0x6u			/* u16 [7]: 2 = cursor on the row (list focused) */
#define FUI_CURSOR_B 0x14u		/* u16 [7]: the other cursor column, 0 */
#define FUI_MARK 0x22u			/* u16 [7]: 0xB enabled row, 0xC grayed */
#define FUI_TITLES 0x3Eu		/* title cells [7] */
#define FUI_VALUES 0x244u		/* value cells [7]: the stock list's value column */
#define FUI_CELL_SIZE 0x4Au
#define FUI_WIDE_A 0x44Au		/* two more cell sets [7] of 0x94 bytes, cleared on the page */
#define FUI_WIDE_B 0x856u
#define FUI_WIDE_SIZE 0x94u
#define FUI_CELL_TEXT 8u		/* u16 text[33] */
#define FUI_TEXT_LEN 33u
#define FUI_KIND_INLINE 5u
#define FUI_CURSOR_ROW 2u
#define FUI_MARK_ROW 0xBu
#define FUI_ARROW_LEFT 0xA050u		/* icon characters of the camera font */
#define FUI_ARROW_RIGHT 0xA03Fu

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

	bool installed;
	/* the page screen on the menu app's stack (GUI task) */
	uint32_t screen_vt[2];		/* its class: event, delete */
	uint32_t screen;		/* the screen object: its vtable pointer */
	bool open;
	bool fresh;			/* opened, the stock list not yet replaced */
	/* word-aligned: gui_data_set copies them with the firmware memcpy */
	uint16_t base[FUI_SLOT_SIZE / 2] __attribute__((aligned(4)));	/* the list's own slot */
	uint16_t page[FUI_SLOT_SIZE / 2] __attribute__((aligned(4)));
	volatile struct fenykl_ui_stats st;
};

extern struct fui_state fui;

/* A stock word the page relies on or changes: size 1, 2 or 4 bytes at addr. */
struct fui_expect {
	uint32_t addr;
	uint8_t size;
	uint32_t value;
	const char *what;
};

/* Counts a GUI callback running on CPU1: true there, false on CPU0. */
bool fui_count_core(void);
/* Bookkeeping of opening the page: counter, cfg save failures so far, no edit, saved. */
void fui_page_open(void);
/* Bookkeeping of closing it: counter, a pending edit dropped (logged). */
void fui_page_close(void);
/* fenykl_ui_install's start: FENYKL_UI_EBUSY once installed, else the list from the registered
   options (FENYKL_UI_EOPTS logged). */
int fui_prepare(void);
/* Every word of tab[n] holds its value; each mismatch is logged. */
bool fui_check_words(const struct fui_expect *tab, unsigned int n);
/* fenykl_ui_tick's part on every body: fui.unsaved, its change to "not saved" logged. */
void fui_tick_unsaved(void);

/* Fills the list from the options in [begin, end): 0 or FENYKL_UI_EOPTS. */
int fui_collect(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end);
enum fui_result fui_key(unsigned int code);
enum fui_result fui_dial(int n);
/* The value column of option o (the cursor row shows the edit value with arrows). */
void fui_value_text(const struct fenykl_cfg_opt *o, uint16_t out[FUI_VALUE_LEN]);

/* Fills fui.page from fui.base and the model. */
void fui_build_page(void);

#endif
