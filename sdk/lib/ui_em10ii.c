/*
 * Settings tab, E-M10 II part (sdk/docs/ui.md; the option core is ui.c): Custom menu tab
 * "L. Fenykl" with an info row and one stock row per option, data writes only, no code hook.
 *
 * - The tab borrows tab id 0x8F1 (Accessory Port C, whose menu page this body never shows): it has
 *   an entry in every fixed-count by-id table a tab needs. The install retitles its tab record,
 *   points it and its config 0x40 at our rows and key tables, re-keys one dead row-style entry per
 *   option row to style 1 (vertical option list), extends the letter list's title/help page record
 *   and open records by one, and appends 0x8F1 to the Custom tab list last. An id with no ModeCheck
 *   rule and no setting-rule record is enabled, so nothing grays the tab or the fresh row ids.
 * - Every swap is a checked word store of a pointer + count pair, pointer first, so no count ever
 *   exceeds the table behind its pointer; a failed store rolls the install back. Installed for the
 *   boot. The main menu's page actuator copies the letter list's open records when the first MENU
 *   builds it, so the install belongs before that (else letter row L opens only after LEFT to the
 *   top tabs and RIGHT, which rebuilds it).
 * - Option rows: OK or RIGHT finds the row's L1 record, whose callback the stock code calls instead
 *   of creating the level-2 picker, then goes to level 2 whatever it did. The callback creates the
 *   stock option picker of the donor setting 0x115 (Warning Level, plain stock init, 5 entries),
 *   clears its wrap and dial flags, puts our init in +0x2C and our handler in +0x48, refills its
 *   arrays in place (the destructor frees them) and hands it to the sub-actuator. Our handler calls
 *   fenykl_cfg_set on OK and passes every other event to the stock handler, so no setting is
 *   written. A picker that does not look stock is refused (logged, counted): with the stock handler
 *   in +0x48 it gets a handler that cancels every event, so its OK never writes Warning Level.
 * - The value column shows one cell per row, matched by the value 0 that the controller reads for
 *   an id it does not know; the handler and fenykl_ui_tick rewrite the cell's label word.
 * - The info row has no options; RIGHT and OK on it find its dispatch records, whose callback
 *   returns "handled".
 * All callbacks run in the GUI task (CPU0); a call on another core is counted.
 */
#include <body.h>
#include "ui_int.h"

#define FUI_SECTIONS 0x0Eu		/* section_loaded bits of sec2..sec4 */
#define FUI_TAB 0x8F1u
#define FUI_ROW_INFO 0xF40u		/* fresh row ids: the info row, then the options */
#define FUI_ROW_OPT0 0xF41u
#define FUI_DONOR 0x115u		/* Warning Level: the picker the rows borrow */
#define FUI_KIND_LANG 4u
#define FUI_KIND_TEXT 5u
#define FUI_LANG_OFF 0x5AEu		/* stock "Off" / "On" (the Eye-Fi row's value cells) */
#define FUI_LANG_ON 0xC6u
#define FUI_EMPTY_TEXT 0x60D38D30u	/* the empty string of the stock kind-4 descriptors */
#define FUI_MARK_GREEN 0xBu		/* the Custom tabs' row mark */
#define FUI_STYLE_LIST 1u
#define FUI_ROW_HANDLED 1		/* dispatch callback: handled, no controller opened */
#define FUI_YIELD_WORDS 8u		/* swaps between two yields: each one flushes the D-cache twice */
#define FUI_REC_STRIDE 24u
#define FUI_CELL_STRIDE 12u

/* Stock words of the tables the install copies or changes */
#define FUI_TAB_IDS 0x60D33E9Eu		/* the tab list's ids 0x846..0x850 */
#define FUI_TAB_FIRST 0x846u
#define FUI_C30_RIGHT 0x60D2E2ACu
#define FUI_C30_OK 0x60D2E3B4u
#define FUI_PAGE_TITLES 0x60D4F020u
#define FUI_PAGE_HELPS 0x60D4F230u
#define FUI_TABREC_ROWS 0x60D3B528u
#define FUI_TABREC_TITLE 0x03FF0004u	/* kind 4, lang 0x3FF "C. Electronic Viewfinder" */
#define FUI_TABREC_LAST 0x60D3C554u
#define FUI_C40_RIGHT 0x60D2E81Cu
#define FUI_C40_OK 0x60D2E84Cu
#define FUI_ROWS_IDS 0x60D33F3Au
#define FUI_TAB_CFG_ENTRY (0x60D36A5Cu + 13u * 8u)	/* {tab, config} */
#define FUI_ROW_LIST_ENTRY (0x60D33FB0u + 13u * 8u)	/* {u16 1, u16 list, variant*} */
#define FUI_STYLES_N 0x50u
#define FUI_RULES_N 0x220u
#define FUI_RULE_SIZE 12u

/* Picker fields */
#define FUI_PK_INIT 0x2Cu		/* per-id init, run by the handler on re-init events */
#define FUI_PK_HANDLER 0x48u		/* OK / MENU / re-init handler */
#define FUI_PK_ID 0x50u			/* u16 setting id */
#define FUI_PK_N 0x54u			/* u16 entries */
#define FUI_PK_CUR 0x56u		/* u16 cursor */
#define FUI_PK_DIS 0x5Cu		/* u16 [n]: 0 = selectable */
#define FUI_PK_VALS 0x60u		/* u32 [n] */
#define FUI_PK_NOWRAP 0x64u
#define FUI_PK_FIRST 0x68u		/* first and last selectable entry */
#define FUI_PK_LAST 0x6Cu
#define FUI_PK_DIAL 0x80u
#define FUI_SUB_KIND 0x50u		/* sub-actuator: level-2 kind, picker */
#define FUI_SUB_PICKER 0x58u

#define FUI_EV_OK 0x900Cu
#define FUI_EV_SET 0x972Bu
#define FUI_EV_MENU 0x900Eu
#define FUI_EV_RESHOW 0x971Du
#define FUI_PK_DONE 2u			/* the stock handler's result after a set */
#define FUI_PK_CANCEL 3u		/* its result for MENU: the list closes */

#define FUI_N(a) (sizeof(a) / sizeof((a)[0]))

static const char fui_s_tab[] = "L. Fenykl";
static const char fui_s_letter[] = "Fenykl";
static const char fui_s_letter_help[] = "Fenykl plugin settings.";
static const char fui_s_info[] = "Fenykl";
static const char fui_s_info_value[] = "Loaded";
static const char fui_s_info_help[] = "The Fenykl plugin is running.";

/* The row-style entries no listed row can match, in the order the option rows take them:
   entry 79 (a second 0x28C, entry 60 matches first), then those of ids in no row-id list. */
static const struct fui_expect fui_styles[FENYKL_UI_OPTS_MAX] = {
	{ FW_CUSTOM_ROW_STYLES + 4u * 79u, 4, 0x0001028Cu, "style 79" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 8u, 4, 0x00010256u, "style 8" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 12u, 4, 0x00010181u, "style 12" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 43u, 4, 0x000500A7u, "style 43" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 64u, 4, 0x00010242u, "style 64" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 69u, 4, 0x0001028Au, "style 69" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 70u, 4, 0x0001028Bu, "style 70" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 71u, 4, 0x00060315u, "style 71" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 72u, 4, 0x0001030Au, "style 72" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 76u, 4, 0x00010309u, "style 76" },
	{ FW_CUSTOM_ROW_STYLES + 4u * 77u, 4, 0x00010340u, "style 77" },
};

/* Donor and neighbour words the install relies on but does not write. */
static const struct fui_expect fui_fixed[] = {
	{ FUI_TAB_CFG_ENTRY, 4, FUI_TAB, "tab config id" },
	{ FUI_TAB_CFG_ENTRY + 4u, 4, 0x40, "tab config" },
	{ FUI_ROW_LIST_ENTRY, 4, 0x0001004Du, "row list" },
	{ FUI_ROW_LIST_ENTRY + 4u, 4, FW_CUSTOM_ROWS_L, "row list variant" },
	{ FW_CUSTOM_TAB_CFG_L, 4, 0x40, "cfg 40 id" },
	{ FW_CUSTOM_TAB_CFG_L + 4u, 4, 4, "cfg 40 kind" },
	{ FW_CUSTOM_TAB_CFG_L + 8u, 4, 0x4D, "cfg 40 list" },
	{ FW_CUSTOM_LETTER_CFG, 4, 0x30, "cfg 30 id" },
	{ FW_CUSTOM_LETTER_CFG + 8u, 4, 0x3D, "cfg 30 list" },
	{ FW_CUSTOM_TAB_ICON_L, 4, 0x000E0000u | FUI_TAB, "tab icon" },
	{ FW_CUSTOM_TAB_REC_L + 0x10u, 4, FUI_TABREC_LAST, "tab record" },
	{ FW_MENU_PICKER_HANDLERS + 0x18u, 4, FW_MENU_PICKER_STOCK_FN, "picker handler" },
};

/* --- labels and value cells --- */

static struct fui_desc fui_text(const char *s)
{
	return (struct fui_desc){ FUI_KIND_TEXT, 0, (uint32_t)s };
}

static struct fui_desc fui_lang(unsigned int lang)
{
	return (struct fui_desc){ FUI_KIND_LANG, (uint16_t)lang, FUI_EMPTY_TEXT };
}

/* Value v of option o: BOOL the stock Off/On, ENUM its name. */
static struct fui_desc fui_value_desc(const struct fenykl_cfg_opt *o, unsigned int v)
{
	if (o->type == FENYKL_CFG_BOOL)
		return fui_lang(v ? FUI_LANG_ON : FUI_LANG_OFF);
	return fui_text(o->names[v]);
}

/* Rewrites a live cell word by word (a row keeps its kind, so one word changes): true if changed. */
static bool fui_cell_put(struct fui_cell *c, struct fui_desc d)
{
	uint32_t kl = (uint32_t)d.lang << 16 | d.kind;
	bool changed = false;

	if (fenykl_rd32((uint32_t)&c->d.text) != d.text) {
		fenykl_wr32((uint32_t)&c->d.text, d.text);
		changed = true;
	}
	if (fenykl_rd32((uint32_t)&c->d) != kl) {
		fenykl_wr32((uint32_t)&c->d, kl);
		changed = true;
	}
	return changed;
}

/* --- option lists (GUI task) --- */

static void fui_picker_fill(uint32_t pk)
{
	const struct fenykl_cfg_opt *o = fui.opts[fui.row];
	uint32_t vals = fenykl_rd32(pk + FUI_PK_VALS), dis = fenykl_rd32(pk + FUI_PK_DIS);
	unsigned int n = fui_nvalues(o);

	if (pk != fui.pk || n > fui.cap) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "init: picker %08x (ours %08x, room %u): not filled",
			       pk, fui.pk, fui.cap);
		return;
	}
	for (unsigned int k = 0; k < n; k++) {
		fenykl_wr32(vals + 4u * k, k);
		fenykl_wr16(dis + 2u * k, 0);
	}
	fenykl_wr16(pk + FUI_PK_N, n);
	fenykl_wr32(pk + FUI_PK_FIRST, 0);
	fenykl_wr32(pk + FUI_PK_LAST, n - 1u);
	/* the re-init after the set (0x971D, 0x214) does not restore the cursor itself */
	fenykl_wr16(pk + FUI_PK_CUR, fui_value(o));
}

static unsigned int fui_picker_ok(uint32_t pk)
{
	const struct fenykl_cfg_opt *o = fui.opts[fui.row];
	unsigned int v = fenykl_rd16(pk + FUI_PK_CUR);

	if (pk != fui.pk || v >= fui_nvalues(o)) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "OK: picker %08x (ours %08x) at %u: nothing set", pk,
			       fui.pk, v);
		return FUI_PK_DONE;
	}
	fui_set(o, (int32_t)v);
	fui_cell_put(&fui.t.vals[fui.row], fui_value_desc(o, fui_value(o)));
	return FUI_PK_DONE;
}

static int fui_cb_init(void *pk)
{
	fui_count_core();
	fui_picker_fill((uint32_t)pk);
	return 0;
}
FENYKL_CALLBACK(menu_row, fui_cb_init);

static unsigned int fui_cb_picker(void *pk, unsigned int code, unsigned int param)
{
	unsigned int r;

	fui_count_core();
	if (code == FUI_EV_OK || code == FUI_EV_SET)
		return fui_picker_ok((uint32_t)pk);
	r = fw_call_menu_picker(FW_MENU_PICKER_STOCK_FN, pk, code, param);
	if (code == FUI_EV_MENU || code == FUI_EV_RESHOW)
		fenykl_log_info(&fenykl_ui_log, "%s: event %04x param %x -> stock %u",
				fui.opts[fui.row]->name, code, param, r);
	else
		fenykl_log_dbg(&fenykl_ui_log, "picker event %04x param %x -> stock %u", code, param,
			       r);
	return r;
}
FENYKL_CALLBACK(menu_picker, fui_cb_picker);

/* The handler of a picker that failed the check: closes the list (the stock MENU result). */
static unsigned int fui_cb_cancel(void *pk, unsigned int code, unsigned int param)
{
	fui_count_core();
	fenykl_log_warn(&fenykl_ui_log, "refused picker %08x: event %04x param %x -> cancel",
			(uint32_t)pk, code, param);
	return FUI_PK_CANCEL;
}
FENYKL_CALLBACK(menu_picker, fui_cb_cancel);

/* The L1 callback's work for option i, sub = the tab's sub-actuator: always leaves a picker. */
static void fui_row_open(uint32_t sub, unsigned int i)
{
	const struct fenykl_cfg_opt *o = fui.opts[i];
	const uint32_t key[2] = { FUI_DONOR, 0 };
	uint32_t pk = (uint32_t)fw_menu_picker_create(key, 0);
	uint32_t h, id, cap;

	fui_count_core();
	/* the stock code goes to level 2 after the callback: the sub-actuator always gets the picker */
	fenykl_wr32(sub + FUI_SUB_PICKER, pk);
	fenykl_wr32(sub + FUI_SUB_KIND, 0);
	if (!fenykl_dram_ptr(pk, 4)) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "%s: no picker (%08x)", o->name, pk);
		return;
	}
	h = fenykl_rd32(pk + FUI_PK_HANDLER);
	id = fenykl_rd16(pk + FUI_PK_ID);
	cap = fenykl_rd16(pk + FUI_PK_N);
	if (h != FW_MENU_PICKER_STOCK_FN || id != FUI_DONOR || cap < fui_nvalues(o) ||
	    !fenykl_dram_ptr(fenykl_rd32(pk + FUI_PK_DIS), 2) ||
	    !fenykl_dram_ptr(fenykl_rd32(pk + FUI_PK_VALS), 4)) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "%s: picker %08x refused", o->name, pk);
		fenykl_log_err(&fenykl_ui_log, "%s: handler %08x, id %x, n %u", o->name, h, id, cap);
		/* its stock OK would write setting 0x115: every event cancels instead */
		if (h == FW_MENU_PICKER_STOCK_FN)
			fenykl_wr32(pk + FUI_PK_HANDLER, FENYKL_CB(menu_picker, fui_cb_cancel));
		return;
	}
	fui.row = i;
	fui.pk = pk;
	fui.cap = cap;
	fui_list_open();
	fenykl_wr32(pk + FUI_PK_NOWRAP, 0);
	fenykl_wr32(pk + FUI_PK_DIAL, 0);
	fenykl_wr32(pk + FUI_PK_INIT, FENYKL_CB(menu_row, fui_cb_init));
	fenykl_wr32(pk + FUI_PK_HANDLER, FENYKL_CB(menu_picker, fui_cb_picker));
	fui_picker_fill(pk);
	fenykl_log_info(&fenykl_ui_log, "%s: picker %08x, donor n %u, cur %u", o->name, pk, cap,
			fenykl_rd16(pk + FUI_PK_CUR));
}

/* One L1 callback per option row: the callback gets only the sub-actuator. */
#define FUI_ROW_CB(n)						\
	static int fui_cb_row##n(void *sub)			\
	{							\
		fui_row_open((uint32_t)sub, n);			\
		return 0;					\
	}							\
	FENYKL_CALLBACK(menu_row, fui_cb_row##n)

FUI_ROW_CB(0);
FUI_ROW_CB(1);
FUI_ROW_CB(2);
FUI_ROW_CB(3);
FUI_ROW_CB(4);
FUI_ROW_CB(5);
FUI_ROW_CB(6);
FUI_ROW_CB(7);
FUI_ROW_CB(8);
FUI_ROW_CB(9);
FUI_ROW_CB(10);

static uint32_t fui_row_cb(unsigned int i)
{
	static const uint32_t cb[FENYKL_UI_OPTS_MAX] = {
		FENYKL_CB(menu_row, fui_cb_row0), FENYKL_CB(menu_row, fui_cb_row1),
		FENYKL_CB(menu_row, fui_cb_row2), FENYKL_CB(menu_row, fui_cb_row3),
		FENYKL_CB(menu_row, fui_cb_row4), FENYKL_CB(menu_row, fui_cb_row5),
		FENYKL_CB(menu_row, fui_cb_row6), FENYKL_CB(menu_row, fui_cb_row7),
		FENYKL_CB(menu_row, fui_cb_row8), FENYKL_CB(menu_row, fui_cb_row9),
		FENYKL_CB(menu_row, fui_cb_row10),
	};

	return cb[i];
}

/* RIGHT and OK on the info row. */
static int fui_cb_swallow(void *sub)
{
	(void)sub;
	fui_count_core();
	return FUI_ROW_HANDLED;
}
FENYKL_CALLBACK(menu_row, fui_cb_swallow);

/* --- tables (task) --- */

static struct fui_rec fui_open_rec(unsigned int tab)
{
	return (struct fui_rec){ 1, (uint16_t)tab, 0, 0, 0xF, 1, 0 };
}

static void fui_build_rows(void)
{
	struct fui_tables *t = &fui.t;
	uint32_t swallow = FENYKL_CB(menu_row, fui_cb_swallow);

	t->info = (struct fui_cell){ 0, fui_text(fui_s_info_value) };
	t->rows[0] = (struct fui_row){
		FUI_ROW_INFO, 1, 0, fui_text(fui_s_info), fui_text(fui_s_info_help),
		(uint32_t)&t->info, 0, 0,
	};
	t->ids[0] = FUI_ROW_INFO;
	for (unsigned int k = 0; k < 2; k++) {
		t->right[k] = (struct fui_rec){ 1, FUI_ROW_INFO, 0, swallow, 0, 0, 0 };
		t->ok[k] = t->right[k];
	}
	for (unsigned int i = 0; i < fui.nopts; i++) {
		const struct fenykl_cfg_opt *o = fui.opts[i];
		unsigned int n = fui_nvalues(o), id = FUI_ROW_OPT0 + i;

		for (unsigned int k = 0; k < n; k++)
			t->opts[i][k] = (struct fui_cell){ k, fui_value_desc(o, k) };
		t->vals[i] = (struct fui_cell){ 0, fui_value_desc(o, fui_value(o)) };
		t->rows[1 + i] = (struct fui_row){
			(uint16_t)id, 1, n, fui_text(o->label), fui_text(o->group->label),
			(uint32_t)&t->vals[i], 0, (uint32_t)t->opts[i],
		};
		t->l1[i] = (struct fui_l1){ 0, (uint16_t)id, 0, fui_row_cb(i) };
		t->ids[1 + i] = (uint16_t)id;
	}
}

static void fui_build_lists(void)
{
	struct fui_tables *t = &fui.t;

	for (unsigned int i = 0; i < FUI_TABS; i++)
		t->tabs[i] = (uint16_t)fenykl_rd16(FUI_TAB_IDS + 2u * i);
	t->tabs[FUI_TABS] = FUI_TAB;
	memcpy(t->titles, (const void *)FUI_PAGE_TITLES, FUI_CELL_STRIDE * FUI_TABS);
	memcpy(t->helps, (const void *)FUI_PAGE_HELPS, FUI_CELL_STRIDE * FUI_TABS);
	t->titles[FUI_TABS] = (struct fui_cell){ FUI_TAB, fui_text(fui_s_letter) };
	t->helps[FUI_TABS] = (struct fui_cell){ FUI_TAB, fui_text(fui_s_letter_help) };
	memcpy(t->c30_right, (const void *)FUI_C30_RIGHT, FUI_REC_STRIDE * FUI_TABS);
	memcpy(t->c30_ok, (const void *)FUI_C30_OK, FUI_REC_STRIDE * FUI_TABS);
	t->c30_right[FUI_TABS] = fui_open_rec(FUI_TAB);
	t->c30_ok[FUI_TABS] = fui_open_rec(FUI_TAB);
}

static void fui_add(uint32_t addr, uint32_t stock, uint32_t ours)
{
	fui.w[fui.nwords++] = (struct fui_word){ addr, stock, ours };
}

/* The words in install order; a failed install unwinds it backwards. */
static void fui_build_words(void)
{
	struct fui_tables *t = &fui.t;
	uint32_t rows = fui.nopts + 1u;

	fui.nwords = 0;
	for (unsigned int i = 0; i < fui.nopts; i++)
		fui_add(fui_styles[i].addr, fui_styles[i].value,
			FUI_STYLE_LIST << 16 | (FUI_ROW_OPT0 + i));
	/* config 0x40: L1 grows from 0; RIGHT and OK keep their count 2 */
	fui_add(FW_CUSTOM_TAB_CFG_L + 0xCu, 0, (uint32_t)t->l1);
	fui_add(FW_CUSTOM_TAB_CFG_L + 0x14u, FUI_C40_RIGHT, (uint32_t)t->right);
	fui_add(FW_CUSTOM_TAB_CFG_L + 0x1Cu, FUI_C40_OK, (uint32_t)t->ok);
	fui_add(FW_CUSTOM_TAB_CFG_L + 0x10u, 0, fui.nopts);
	/* row-id list 0x4D */
	fui_add(FW_CUSTOM_ROWS_L + 4u, FUI_ROWS_IDS, (uint32_t)t->ids);
	fui_add(FW_CUSTOM_ROWS_L, 0x00020000u, rows << 16);
	/* tab record: rows, then the count; the title text, then its kind */
	fui_add(FW_CUSTOM_TAB_REC_L + 0xCu, FUI_TABREC_ROWS, (uint32_t)t->rows);
	fui_add(FW_CUSTOM_TAB_REC_L, 0x00020000u | FUI_TAB, rows << 16 | FUI_TAB);
	fui_add(FW_CUSTOM_TAB_REC_L + 8u, FUI_EMPTY_TEXT, (uint32_t)fui_s_tab);
	fui_add(FW_CUSTOM_TAB_REC_L + 4u, FUI_TABREC_TITLE, FUI_KIND_TEXT);
	/* the tab column's row mark */
	fui_add(FW_CUSTOM_TAB_ICON_L + 4u, 0x0000001Cu, FUI_MARK_GREEN << 16 | 0x1Cu);
	/* letter list: titles and helps, then the count both share */
	fui_add(FW_CUSTOM_LETTER_PAGE + 4u, FUI_PAGE_TITLES, (uint32_t)t->titles);
	fui_add(FW_CUSTOM_LETTER_PAGE + 8u, FUI_PAGE_HELPS, (uint32_t)t->helps);
	fui_add(FW_CUSTOM_LETTER_PAGE, FUI_TABS << 16 | 3u, (FUI_TABS + 1u) << 16 | 3u);
	/* letter list open records */
	fui_add(FW_CUSTOM_LETTER_CFG + 0x14u, FUI_C30_RIGHT, (uint32_t)t->c30_right);
	fui_add(FW_CUSTOM_LETTER_CFG + 0x1Cu, FUI_C30_OK, (uint32_t)t->c30_ok);
	fui_add(FW_CUSTOM_LETTER_CFG + 0x18u, FUI_TABS, FUI_TABS + 1u);
	fui_add(FW_CUSTOM_LETTER_CFG + 0x20u, FUI_TABS, FUI_TABS + 1u);
	/* the tab list last: the tab appears only with everything behind it in place */
	fui_add(FW_CUSTOM_TAB_LIST + 4u, FUI_TAB_IDS, (uint32_t)t->tabs);
	fui_add(FW_CUSTOM_TAB_LIST, FUI_TABS << 16, (FUI_TABS + 1u) << 16);
}

/* --- checks (task) --- */

static bool fui_check_swapped(void)
{
	bool ok = true;

	for (unsigned int i = 0; i < fui.nwords; i++) {
		uint32_t v = fenykl_rd32(fui.w[i].addr);

		if (v == fui.w[i].stock)
			continue;
		fenykl_log_err(&fenykl_ui_log, "stock MISMATCH word %u %08x = %08x, expected %08x",
			       i, fui.w[i].addr, v, fui.w[i].stock);
		ok = false;
	}
	return ok;
}

/* The stock lists the install copies: tab ids A..K with their letter titles, helps and open
   records. */
static bool fui_check_lists(void)
{
	bool ok = true;

	for (unsigned int i = 0; i < FUI_TABS; i++) {
		unsigned int id = FUI_TAB_FIRST + i;
		uint32_t r = FUI_C30_RIGHT + FUI_REC_STRIDE * i, o = FUI_C30_OK + FUI_REC_STRIDE * i;
		const struct fui_expect e[] = {
			{ FUI_TAB_IDS + 2u * i, 2, id, "tab id" },
			{ FUI_PAGE_TITLES + FUI_CELL_STRIDE * i, 4, id, "letter title" },
			{ FUI_PAGE_HELPS + FUI_CELL_STRIDE * i, 4, id, "letter help" },
			{ r, 4, 1, "letter RIGHT" }, { r + 4u, 4, id, "letter RIGHT row" },
			{ r + 8u, 4, 0, "letter RIGHT fn" }, { r + 12u, 4, 0xF, "letter RIGHT group" },
			{ r + 16u, 4, 1, "letter RIGHT target" },
			{ o, 4, 1, "letter OK" }, { o + 4u, 4, id, "letter OK row" },
			{ o + 8u, 4, 0, "letter OK fn" }, { o + 12u, 4, 0xF, "letter OK group" },
			{ o + 16u, 4, 1, "letter OK target" },
		};

		ok = fui_check_words(e, FUI_N(e)) && ok;
	}
	return ok;
}

static bool fui_fresh(unsigned int id)
{
	return id >= FUI_ROW_INFO && id <= FUI_ROW_OPT0 + fui.nopts;
}

/* No style entry or ModeCheck rule knows the fresh ids or the donor tab. */
static bool fui_check_free(void)
{
	bool ok = true;

	for (unsigned int i = 0; i < FUI_STYLES_N; i++) {
		unsigned int id = fenykl_rd16(FW_CUSTOM_ROW_STYLES + 4u * i);

		if (!fui_fresh(id))
			continue;
		fenykl_log_err(&fenykl_ui_log, "style %u already holds %x", i, id);
		ok = false;
	}
	for (unsigned int i = 0; i < FUI_RULES_N; i++) {
		unsigned int id = fenykl_rd16(FW_MODE_RULES + FUI_RULE_SIZE * i);

		if (!fui_fresh(id) && id != FUI_TAB)
			continue;
		fenykl_log_err(&fenykl_ui_log, "mode rule %u for %x", i, id);
		ok = false;
	}
	return ok;
}

static bool fui_check(void)
{
	bool ok = fui_check_swapped();

	ok = fui_check_words(fui_fixed, FUI_N(fui_fixed)) && ok;
	ok = fui_check_lists() && ok;
	return fui_check_free() && ok;
}

/* --- install (task) --- */

/* Stores to in the word at a if it holds from, as a slot swap: D-cache written back around a
   store under masked interrupts. True if the word reads back as to. */
static bool fui_cas(uint32_t a, uint32_t from, uint32_t to)
{
	uint32_t psw;
	bool ok;

	fenykl_dcache_flush();
	psw = fenykl_irq_save();
	ok = fenykl_rd32(a) == from;
	if (ok) {
		fenykl_wr32(a, to);
		ok = fenykl_rd32(a) == to;
	}
	fenykl_irq_restore(psw);
	fenykl_dcache_flush();
	return ok;
}

/* Puts the stock words back, last swapped first; a word that no longer holds ours is left. */
static void fui_unwind(void)
{
	while (fui.done) {
		const struct fui_word *w = &fui.w[--fui.done];

		if (fui.done % FUI_YIELD_WORDS == 0)
			fenykl_task_sleep(1);
		if (!fui_cas(w->addr, w->ours, w->stock))
			fenykl_log_err(&fenykl_ui_log, "unwind: %08x holds %08x, not ours: left",
				       w->addr, fenykl_rd32(w->addr));
	}
}

static int fui_swap(void)
{
	fenykl_dcache_flush();
	for (fui.done = 0; fui.done < fui.nwords; fui.done++) {
		const struct fui_word *w = &fui.w[fui.done];

		if (fui.done && fui.done % FUI_YIELD_WORDS == 0)
			fenykl_task_sleep(1);
		if (fui_cas(w->addr, w->stock, w->ours))
			continue;
		fenykl_log_err(&fenykl_ui_log, "install: %08x holds %08x, expected %08x: rolled back",
			       w->addr, fenykl_rd32(w->addr), w->stock);
		fui_unwind();
		return FENYKL_UI_ESTOCK;
	}
	return 0;
}

int fenykl_ui_install(void)
{
	uint32_t t0 = FENYKL_TIMER_NOW(), obj;
	int r = fui_prepare();

	if (r)
		return r;
	if ((fenykl_rd8(FW_SECTION_LOADED) & FUI_SECTIONS) != FUI_SECTIONS || fenykl_gui_menu())
		return FENYKL_UI_EAGAIN;
	fui_build_rows();
	fui_build_lists();
	fui_build_words();
	if (!fui_check())
		return FENYKL_UI_ESTOCK;
	r = fui_swap();
	if (r)
		return r;
	/* after the swap: a MENU during it built the tree from a partial state */
	obj = fenykl_rd32(FW_CUSTOM_TAB_OBJ);
	fui.installed = true;
	fenykl_log_info(&fenykl_ui_log, "installed: tab L, %u option rows, %u words in %u us",
			fui.nopts, fui.done, FENYKL_TIMER_ELAPSED(t0, FENYKL_TIMER_NOW()));
	if (obj) {
		fui.st.late = 1;
		fenykl_log_warn(&fenykl_ui_log,
				"menu built before the install (%08x): letter L opens after LEFT, RIGHT",
				obj);
	}
	return 0;
}

void fenykl_ui_tick(void)
{
	if (!fui.installed)
		return;
	/* a late cfg load or another writer changes values outside the option lists */
	for (unsigned int i = 0; i < fui.nopts; i++)
		if (fui_cell_put(&fui.t.vals[i], fui_value_desc(fui.opts[i], fui_value(fui.opts[i]))))
			fui.st.refreshes++;
	fui_tick_unsaved();
}
