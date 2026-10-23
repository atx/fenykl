/*
 * Settings page, E-PL3 part (sdk/docs/ui.md; the option core is ui.c). All changes are RAM
 * writes to GUI tables, made only after every stock word was checked:
 * - custom_menu_items[70] (Eye-Fi, the last row of tab J) becomes "Fenykl": lang ids 0x122/0x1DF
 *   point at our strings, its setting is 0xFFFFFFF1 (no value column), its kid is never greyed.
 * - Super state 0x0D (a dead stub slot) gets our one-state descriptor.
 * - SS_MENU_MAIN gets a cloned action table plus the wrapper; the OK and RIGHT cells of state 4
 *   (StWizardMenu) point at it. On the Fenykl row it returns 0x0D01, elsewhere the stock result.
 * - The page draws no pixel: it leaves the stock menu list view on screen and publishes its own
 *   item list in GUI data slot 0xF, which the view draws as it draws the Custom list. Row titles
 *   are lang ids of Custom tabs A..D lent to the option labels while the page is open.
 *   MENU goes back like the end of a stock menu warning (0x0304 with the warning-return flag),
 *   whose rebuild rewrites the slot. System events run StWizardMenu's own action.
 */
#include "ui_int.h"

/* --- firmware layouts (v1.6) --- */

#define FUI_APP_PANEL 0x19Cu
#define FUI_SS 0x0D
#define FUI_SLOT_0D (FW_SS_DESCS + 4 * (FUI_SS - 1))
#define FUI_MENU_STATES 15
#define FUI_MENU_ROWS 68
#define FUI_MENU_NACT 220
#define FUI_ST_WIZARD 4
#define FUI_ROW_RIGHT 5
#define FUI_ROW_OK 6
#define FUI_CELL_RIGHT (FW_MENU_MATRIX + FUI_ROW_RIGHT * FUI_MENU_STATES + FUI_ST_WIZARD - 1)
#define FUI_CELL_OK (FW_MENU_MATRIX + FUI_ROW_OK * FUI_MENU_STATES + FUI_ST_WIZARD - 1)
#define FUI_ACT_RIGHT 12		/* FW_MENU_ACT_RIGHT: on_key(3) */
#define FUI_ACT_OK 13			/* FW_MENU_ACT_OK: on_key(4) */
#define FUI_ACT_GENIFA 200

#define FUI_CUSTOM_ROW 70
#define FUI_REC (FW_CUSTOM_MENU_ITEMS + 16 * FUI_CUSTOM_ROW)	/* u32 setting +4; help +0xA */
#define FUI_LANG_COUNT 1341u
#define FUI_LANG_HELP 0x1DFu
#define FUI_LANG_STABLE_TICKS 3u

#define FUI_SETTING_NONE 0xFFFFFFF1u
#define FUI_AVAIL_ALWAYS 0x0000FFF1u
#define FUI_TO_PAGE 0x0D01u
#define FUI_TO_LIST 0x0304u

/* Titles of custom_menu_items 0..20, checked at install. */
const uint16_t fui_borrowed_ids[FUI_BORROWED] = {
	0x033, 0x21B, 0x031, 0x0E0, 0x04D, 0x08B, 0x0AC, 0x02F, 0x032, 0x082,
	0x02C, 0x02B, 0x060, 0x063, 0x062, 0x064, 0x0DE, 0x0DD, 0x486, 0x526,
	0x15D,
};

static const __CHAR16_TYPE__ fui_s_title[] = u"Fenykl";
static const __CHAR16_TYPE__ fui_s_help[] = u"Fenykl plugin settings";
static const __CHAR16_TYPE__ fui_s_unsaved[] = u"Fenykl: not saved";

/* --- the list slot --- */

void fui_build_slot(void)
{
	struct fui_slot *s = &fui.slot;
	unsigned int page = fui.cursor / FUI_PAGE_ROWS, left = fui.nopts - page * FUI_PAGE_ROWS;

	memset(s, 0, sizeof(*s));
	s->title = fui.unsaved && fui.lent_arr ? fui_borrowed_ids[FUI_TITLE_UNSAVED] : FUI_TITLE;
	s->count = (int8_t)fui.nopts;
	s->visible = (int8_t)(left < FUI_PAGE_ROWS ? left : FUI_PAGE_ROWS);
	s->page = (uint16_t)page;
	for (unsigned int i = 0; i < fui.nopts; i++) {
		struct fui_row *r = &s->row[i];
		bool sel = i == fui.cursor;

		r->state = sel ? FUI_ROW_SELECTED : FUI_ROW_NORMAL;
		r->kind = FUI_KIND_TEXT;
		r->title = fui_borrowed_ids[i];
		r->help = sel ? FUI_HELP_SELECTED : FUI_HELP;
		fui_value_text(fui.opts[i], r->v);
	}
}

/* Publishes the slot; the view redraws when it handles the posted 0x1900 (GUI loop). */
static int fui_publish(void)
{
	int r;

	fui_build_slot();
	r = fw_gui_data_set(&fui.slot, FUI_SLOT_ID, sizeof(fui.slot), 1);
	fui.st.publishes++;
	if (!r) {
		fui.st.publish_fail++;
		fenykl_log_err(&fenykl_ui_log, "gui_data_set(0xF) failed");
	}
	return r;
}

/* --- language --- */

/* The current string table if it looks sane: word 0 -> array, word 1 -> its end (FUI_LANG_COUNT
   entries and the 0 terminator), the first string right after the header, both overridden
   entries ours or inside it. Its array, or 0. */
static uint32_t fui_lang_table(void)
{
	uint32_t b = fenykl_rd32(FW_OSD_LANG_CUR), a, e, t, h;

	if (!fenykl_dram_ptr(b, 4))
		return 0;
	a = fenykl_rd32(b);
	e = fenykl_rd32(b + 4);
	if (!fenykl_dram_ptr(a, 4) || e - a != 4 * FUI_LANG_COUNT || fenykl_rd32(a) != b + 8 ||
	    fenykl_rd32(e) != 0)
		return 0;
	t = fenykl_rd32(a + 4 * FUI_TITLE);
	h = fenykl_rd32(a + 4 * FUI_LANG_HELP);
	if (t != (uint32_t)fui_s_title && (t < b + 8 || t >= a))
		return 0;
	if (h != (uint32_t)fui_s_help && (h < b + 8 || h >= a))
		return 0;
	return a;
}

static void fui_lang_apply(uint32_t arr)
{
	fenykl_wr32(arr + 4 * FUI_TITLE, (uint32_t)fui_s_title);
	fenykl_wr32(arr + 4 * FUI_LANG_HELP, (uint32_t)fui_s_help);
	fui.lang_arr = arr;
}

/* Borrowed id k's string while lent, 0 if k is not lent. */
static uint32_t fui_lent_string(unsigned int k)
{
	if (k == FUI_TITLE_UNSAVED)
		return (uint32_t)fui_s_unsaved;
	return k < fui.nopts ? (uint32_t)fui.labels[k] : 0;
}

void fui_lang_lend(uint32_t arr)
{
	for (unsigned int k = 0; k < fui.nopts; k++) {
		const char *s = fui.opts[k]->label;
		unsigned int i;

		for (i = 0; s[i] && i < FUI_LABEL_LEN - 1; i++)
			fui.labels[k][i] = (uint8_t)s[i];
		fui.labels[k][i] = 0;
	}
	for (unsigned int k = 0; k < FUI_BORROWED; k++) {
		uint32_t a = arr + 4 * fui_borrowed_ids[k], s = fui_lent_string(k);

		if (!s)
			continue;
		fui.lent_orig[k] = fenykl_rd32(a);
		fenykl_wr32(a, s);
	}
	fui.lent_arr = arr;
	fui.st.lends++;
}

void fui_lang_return(void)
{
	uint32_t arr = fui.lent_arr;

	if (!arr)
		return;
	for (unsigned int k = 0; k < FUI_BORROWED; k++) {
		uint32_t a = arr + 4 * fui_borrowed_ids[k], s = fui_lent_string(k);

		if (s && fenykl_rd32(a) == s)
			fenykl_wr32(a, fui.lent_orig[k]);
	}
	fui.lent_arr = 0;
	fui.st.returns++;
}

/* Re-applies after a language change, once the new table has stayed in place for
   FUI_LANG_STABLE_TICKS ticks (a table being loaded must not get the pointers). */
static void fui_lang_poll(void)
{
	uint32_t arr = fui_lang_table();

	if (!arr) {
		fui.st.lang_unstable++;
		fui.lang_last_arr = 0;
		fui.lang_same = 0;
		return;
	}
	fui.lang_same = arr == fui.lang_last_arr ? fui.lang_same + 1 : 0;
	fui.lang_last_arr = arr;
	if (fenykl_rd32(arr + 4 * FUI_TITLE) == (uint32_t)fui_s_title &&
	    fenykl_rd32(arr + 4 * FUI_LANG_HELP) == (uint32_t)fui_s_help)
		return;
	if (fui.lang_same < FUI_LANG_STABLE_TICKS)
		return;
	fenykl_log_info(&fenykl_ui_log, "language re-applied, table %08x%s", arr,
			arr != fui.lang_arr ? " (moved)" : "");
	fui_lang_apply(arr);
	fui.st.lang_reapply++;
}

/* --- GUI actions (GUI task) --- */

static uint32_t fui_menu_actions[FUI_MENU_NACT + 1];	/* SS_MENU_MAIN's table + the wrapper */

/* Class vtable of the panel object on screen, 0 if there is none. */
static uint32_t fui_panel_vtable(void)
{
	uint32_t app = fenykl_rd32(FW_GUI_APP), panel;

	if (!fenykl_dram_ptr(app, 4))
		return 0;
	panel = fenykl_rd32(app + FUI_APP_PANEL);
	return fenykl_dram_ptr(panel, 4) ? fenykl_rd32(panel) : 0;
}

/* StWizardMenu's stock action for a system event, 0 if none. */
static unsigned int fui_wizard_action(unsigned int code)
{
	for (unsigned int i = 0; i < FUI_MENU_ROWS; i++) {
		if (fenykl_rd32(FW_MENU_EVMAP + 8 * i) != code)
			continue;
		unsigned int row = fenykl_rd32(FW_MENU_EVMAP + 8 * i + 4);

		if (row >= FUI_MENU_ROWS)
			return 0;
		unsigned int act =
			fenykl_rd8(FW_MENU_MATRIX + row * FUI_MENU_STATES + FUI_ST_WIZARD - 1);

		return act <= 2 || act >= FUI_MENU_NACT ? 0 : act;
	}
	return 0;
}

/* Back to StWizardMenu as the end of a stock menu warning does (action 170 0x6F085A35): set the
   warning-return flag, return 0x0304. Its entry 0x6F07DED6 then clears the flag and posts 0x9ABC,
   which rebuilds the Custom list (slots 0xE/0xF) with the cursor unchanged. */
static unsigned int fui_back_to_list(void)
{
	uint32_t flags = fenykl_rd32(FW_MENU_FLAGS_PTR);

	if (flags)
		fenykl_wr32(flags + 0x14, 1);
	else
		fui.st.flag_missing++;
	return FUI_TO_LIST;
}

static unsigned int fui_on_wrap(void *sm, unsigned int code, unsigned int p, void *p2)
{
	unsigned int c = code & 0xFFFF;
	uint32_t stock = c == FUI_KEY_RIGHT ? fui.stock_right : fui.stock_ok;
	uint32_t panel, vt, arr;
	unsigned int r;

	if (fui_count_core())
		return fw_call_gui_action(stock, sm, code, p, p2);
	panel = fenykl_rd32(FW_CUSTOM_PANEL);
	if (!panel || fenykl_rd32(FW_MENU_CUR_PANEL) != panel ||
	    (int16_t)fenykl_rd16(FW_CUSTOM_CUR_ITEM) != FUI_CUSTOM_ROW) {
		r = fw_call_gui_action(stock, sm, code, p, p2);
		fui.st.wrap_pass++;
		fenykl_log_dbg(&fenykl_ui_log, "wrapper pass %04x -> %04x, cur %d", c, r & 0xFFFF,
			       (int16_t)fenykl_rd16(FW_CUSTOM_CUR_ITEM));
		return r;
	}
	vt = fui_panel_vtable();
	arr = fui_lang_table();
	if (vt != FW_MENULIST_VTABLE || !arr) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "NOT opened: panel %d vtable %08x (want %08x), lang %08x",
			       (int16_t)fenykl_rd16(FW_GUI_CUR_PANEL), vt, FW_MENULIST_VTABLE, arr);
		return 0;
	}
	fui.st.wrap_in++;
	fenykl_log_info(&fenykl_ui_log, "open (key %04x, panel %d, vtable %08x)", c,
			(int16_t)fenykl_rd16(FW_GUI_CUR_PANEL), vt);
	return FUI_TO_PAGE;
}
FENYKL_CALLBACK(gui_action, fui_on_wrap);

unsigned int fui_on_entry(void *sm, unsigned int code, unsigned int p, void *p2)
{
	uint32_t arr = fui_lang_table();
	int r;

	fui_count_core();
	fui_page_open();
	if (arr)
		fui_lang_lend(arr);
	else
		fenykl_log_err(&fenykl_ui_log, "entry: no language table, titles NOT lent");
	r = fui_publish();
	fenykl_log_info(&fenykl_ui_log, "entry %04x: panel %d vtable %08x, lang %08x", code & 0xFFFF,
			(int16_t)fenykl_rd16(FW_GUI_CUR_PANEL), fui_panel_vtable(), arr);
	fenykl_log_info(&fenykl_ui_log, "entry: %u rows, cursor %u, data_set %d", fui.nopts,
			fui.cursor, r);
	return 0;
}
FENYKL_CALLBACK(gui_action, fui_on_entry);

/* Every way out of super 0x0D passes here: a pending edit is dropped, the ids go back. */
unsigned int fui_on_exit(void *sm, unsigned int code, unsigned int p, void *p2)
{
	fui_count_core();
	fui_page_close();
	fui_lang_return();
	fenykl_log_info(&fenykl_ui_log, "exit %04x", code & 0xFFFF);
	return 0;
}
FENYKL_CALLBACK(gui_action, fui_on_exit);

unsigned int fui_on_key(void *sm, unsigned int code, unsigned int p, void *p2)
{
	unsigned int c = code & 0xFFFF;
	bool dial = c == FUI_JOG_DIAL || c == FUI_CTRL_RING;
	/* negated: a positive count moves the stock menus up (checked on the camera) */
	int n = -(int16_t)(uintptr_t)p2;
	enum fui_result r;
	int d = 0;

	fui_count_core();
	fui.st.keys++;
	r = dial ? fui_dial(n) : fui_key(c);
	if (r == FUI_LEAVE) {
		fui.st.backs++;
		fui_lang_return();
		fenykl_log_dbg(&fenykl_ui_log, "key %04x: back", c);
		return fui_back_to_list();
	}
	if (r == FUI_REDRAW)
		d = fui_publish();
	if (!dial)
		fenykl_log_dbg(&fenykl_ui_log, "key %04x -> %d: cursor %u, data_set %d", c, r,
			       fui.cursor, d);
	else if (fui.edit)
		fenykl_log_dbg(&fenykl_ui_log, "dial %04x p2 %08x step %d -> value %d", c,
			       (unsigned int)(uintptr_t)p2, n, fui.edit_val);
	else
		fenykl_log_dbg(&fenykl_ui_log, "dial %04x p2 %08x step %d -> cursor %u", c,
			       (unsigned int)(uintptr_t)p2, n, fui.cursor);
	return 0;
}
FENYKL_CALLBACK(gui_action, fui_on_key);

/* A system event: run what StWizardMenu (the Custom list's state) runs for it. Its "stay" becomes
   the way back to the list; a result within SS_MENU_MAIN (super byte 0 or 3) gets 0 bytes filled
   in as state 4 would have them; any other super leaves the menu. */
unsigned int fui_on_sys(void *sm, unsigned int code, unsigned int p, void *p2)
{
	unsigned int c = code & 0xFFFF, act = fui_wizard_action(c), r = 0, t;

	fui_count_core();
	fui.st.sys++;
	fui_lang_return();
	if (act)
		r = fw_call_gui_action(fui_menu_actions[act], sm, code, p, p2) & 0xFFFF;
	if (r == 0) {
		t = fui_back_to_list();
	} else {
		unsigned int super = r >> 8 ? r >> 8 : 0x03;
		unsigned int state = r & 0xFF ? r & 0xFF : (super == 0x03 ? FUI_ST_WIZARD : 0);

		t = super << 8 | state;
	}
	fenykl_log_info(&fenykl_ui_log, "sys %04x: action %u -> %04x, to %04x", c, act, r, t);
	return t;
}
FENYKL_CALLBACK(gui_action, fui_on_sys);

static unsigned int fui_on_ignore(void *sm, unsigned int code, unsigned int p, void *p2)
{
	fui_count_core();
	fui.st.ignored++;
	fenykl_log_dbg(&fenykl_ui_log, "ignored %04x %08x", code & 0xFFFF, p);
	return 0;
}
FENYKL_CALLBACK(gui_action, fui_on_ignore);

/* --- super state 0x0D: one state; action indices 0..2 are the shared no-ops --- */

struct fui_ss_desc {
	uint8_t n_states, pad;
	uint16_t n_rows, n_actions, n_map;
	const uint8_t *matrix;
	const uint32_t *actions;
	const uint32_t *evmap;		/* {u32 code, u32 row} */
	const uint32_t *entry;
	const uint32_t *exit;
	uint8_t wide, pad2[3];
};
_Static_assert(sizeof(struct fui_ss_desc) == 0x20, "descriptor layout");

enum { FUI_A_KEY = 3, FUI_A_SYS, FUI_A_IGNORE, FUI_A_GENIFA, FUI_N_ACTIONS };

static const uint16_t fui_events[][2] = {
	{ FUI_KEY_UP, FUI_A_KEY }, { FUI_KEY_DOWN, FUI_A_KEY }, { FUI_KEY_LEFT, FUI_A_KEY },
	{ FUI_KEY_RIGHT, FUI_A_KEY }, { FUI_KEY_OK, FUI_A_KEY }, { FUI_KEY_MENU, FUI_A_KEY },
	{ FUI_JOG_DIAL, FUI_A_KEY }, { FUI_CTRL_RING, FUI_A_KEY },
	/* system events: StWizardMenu's own action, see fui_on_sys */
	{ 0x9000, FUI_A_SYS },	/* shutter half press */
	{ 0x9002, FUI_A_SYS },	/* full press */
	{ 0x9010, FUI_A_SYS },	/* PLAY */
	{ 0x9012, FUI_A_SYS },	/* MOVIE */
	{ 0x9030, FUI_A_SYS },	/* mode dial */
	{ 0x9033, FUI_A_SYS },	/* POWER */
	{ 0x9300, FUI_A_SYS },	/* USB */
	{ 0x9303, FUI_A_SYS },	/* EVF state */
	{ 0x9304, FUI_A_SYS },	/* PENPAL */
	{ 0x9306, FUI_A_SYS },	/* EVF key on */
	{ 0x9307, FUI_A_SYS },	/* EVF key off */
	{ 0x9601, FUI_A_SYS },	/* REQ_CHG_OUTPUT_DISPLAY */
	{ 0x9620, FUI_A_SYS },	/* REQ_START_REC */
	{ 0x9622, FUI_A_SYS },	/* REQ_START_PLAY */
	{ 0x9623, FUI_A_SYS },	/* REQ_END_PLAY */
	{ 0x9624, FUI_A_SYS },	/* REQ_START_MENU */
	{ 0x9625, FUI_A_SYS },	/* REQ_END_MENU */
	{ 0x9626, FUI_A_SYS },	/* REQ_DISP_BLACK_SCREEN */
	{ 0x9627, FUI_A_SYS },	/* WARNING */
	{ 0x962C, FUI_A_SYS },	/* not in SS_MENU_MAIN's map: back to the list */
	{ 0x9ABB, FUI_A_SYS },	/* INF_WARNING -> 0x030E */
	/* logged, ignored */
	{ 0x900D, FUI_A_IGNORE }, { 0x9014, FUI_A_IGNORE }, { 0x9016, FUI_A_IGNORE },
	{ 0x9018, FUI_A_IGNORE }, { 0x901A, FUI_A_IGNORE }, { 0x901C, FUI_A_IGNORE },
	{ 0x903E, FUI_A_IGNORE }, { 0x9040, FUI_A_IGNORE }, { 0x9404, FUI_A_IGNORE },
	{ 0x9B01, FUI_A_IGNORE },
	/* EVT_GUI_END_GENIFA_DRAW: the stock action every SS_MENU_MAIN state maps it to */
	{ 0x9A41, FUI_A_GENIFA },
};
#define FUI_EVENTS (sizeof(fui_events) / sizeof(fui_events[0]))

static uint32_t fui_actions[FUI_N_ACTIONS];
static uint32_t fui_evmap[FUI_EVENTS * 2];
static uint8_t fui_matrix[FUI_EVENTS];
static uint32_t fui_entry_tab[1], fui_exit_tab[1];
static struct fui_ss_desc fui_desc;

static void fui_build_tables(void)
{
	memcpy(fui_menu_actions, (const void *)FW_MENU_ACTIONS, FUI_MENU_NACT * 4);
	fui_menu_actions[FUI_MENU_NACT] = FENYKL_CB(gui_action, fui_on_wrap);
	fui.stock_ok = fui_menu_actions[FUI_ACT_OK];
	fui.stock_right = fui_menu_actions[FUI_ACT_RIGHT];

	fui_actions[0] = FW_MENU_NOOP0;
	fui_actions[1] = FW_MENU_NOOP1;
	fui_actions[2] = FW_MENU_NOOP2;
	fui_actions[FUI_A_KEY] = FENYKL_CB(gui_action, fui_on_key);
	fui_actions[FUI_A_SYS] = FENYKL_CB(gui_action, fui_on_sys);
	fui_actions[FUI_A_IGNORE] = FENYKL_CB(gui_action, fui_on_ignore);
	fui_actions[FUI_A_GENIFA] = fui_menu_actions[FUI_ACT_GENIFA];
	for (unsigned int i = 0; i < FUI_EVENTS; i++) {
		fui_evmap[2 * i] = fui_events[i][0];
		fui_evmap[2 * i + 1] = i;
		fui_matrix[i] = (uint8_t)fui_events[i][1];
	}
	fui_entry_tab[0] = FENYKL_CB(gui_action, fui_on_entry);
	fui_exit_tab[0] = FENYKL_CB(gui_action, fui_on_exit);
	fui_desc = (struct fui_ss_desc){
		.n_states = 1,
		.n_rows = FUI_EVENTS,
		.n_actions = FUI_N_ACTIONS,
		.n_map = FUI_EVENTS,
		.matrix = fui_matrix,
		.actions = fui_actions,
		.evmap = fui_evmap,
		.entry = fui_entry_tab,
		.exit = fui_exit_tab,
	};
}

/* --- preconditions: every stock word changed or relied on --- */

static const struct fui_expect fui_stock[] = {
	{ FW_GUI_SUPER + 4, 4, FW_SS_DESCS, "gui_sm descs" },
	{ FW_SS_DESCS + 4 * 2, 4, FW_SS_DESC_MENU, "desc SS_MENU_MAIN" },
	{ FUI_SLOT_0D, 4, FW_SS_DESC_0D_STUB, "desc slot 0x0D" },
	{ FW_SS_DESC_MENU + 0x0, 1, FUI_MENU_STATES, "menu n_states" },
	{ FW_SS_DESC_MENU + 0x2, 2, FUI_MENU_ROWS, "menu n_rows" },
	{ FW_SS_DESC_MENU + 0x4, 2, FUI_MENU_NACT, "menu n_actions" },
	{ FW_SS_DESC_MENU + 0x6, 2, FUI_MENU_ROWS, "menu n_map" },
	{ FW_SS_DESC_MENU + 0x8, 4, FW_MENU_MATRIX, "menu matrix" },
	{ FW_SS_DESC_MENU + 0xC, 4, FW_MENU_ACTIONS, "menu actions" },
	{ FW_SS_DESC_MENU + 0x10, 4, FW_MENU_EVMAP, "menu evmap" },
	{ FW_SS_DESC_MENU + 0x1C, 1, 0, "menu wide" },
	{ FW_MENU_EVMAP + 8 * FUI_ROW_RIGHT, 4, FUI_KEY_RIGHT, "evmap RIGHT" },
	{ FW_MENU_EVMAP + 8 * FUI_ROW_RIGHT + 4, 4, FUI_ROW_RIGHT, "evmap RIGHT row" },
	{ FW_MENU_EVMAP + 8 * FUI_ROW_OK, 4, FUI_KEY_OK, "evmap OK" },
	{ FW_MENU_EVMAP + 8 * FUI_ROW_OK + 4, 4, FUI_ROW_OK, "evmap OK row" },
	{ FUI_CELL_RIGHT, 1, FUI_ACT_RIGHT, "cell RIGHT/WIZARD" },
	{ FUI_CELL_OK, 1, FUI_ACT_OK, "cell OK/WIZARD" },
	{ FW_MENU_ACTIONS + 4 * 0, 4, FW_MENU_NOOP0, "action 0" },
	{ FW_MENU_ACTIONS + 4 * 1, 4, FW_MENU_NOOP1, "action 1" },
	{ FW_MENU_ACTIONS + 4 * 2, 4, FW_MENU_NOOP2, "action 2" },
	{ FW_MENU_ACTIONS + 4 * FUI_ACT_RIGHT, 4, FW_MENU_ACT_RIGHT, "action RIGHT" },
	{ FW_MENU_ACTIONS + 4 * FUI_ACT_OK, 4, FW_MENU_ACT_OK, "action OK" },
	{ FW_MENU_ACTIONS + 4 * FUI_ACT_GENIFA, 4, FW_MENU_ACT_GENIFA, "action GENIFA" },
	{ FUI_REC + 0x0, 2, 9, "rec tab" },
	{ FUI_REC + 0x2, 2, FUI_TITLE, "rec title" },
	{ FUI_REC + 0x4, 4, 0x223, "rec setting" },
	{ FUI_REC + 0xA, 2, FUI_LANG_HELP, "rec help" },
	{ FUI_REC + 0xC, 2, 0, "rec fmt" },
	{ FUI_REC + 0x10, 2, 0x92, "end of records" },
	{ FW_CUSTOM_MENU_KID70 + 0, 4, 0xA9, "kid node" },
	{ FW_CUSTOM_MENU_KID70 + 4, 4, 0x223, "kid avail" },
};
#define FUI_STOCK (sizeof(fui_stock) / sizeof(fui_stock[0]))

static bool fui_check_stock(void)
{
	bool ok = fui_check_words(fui_stock, FUI_STOCK);

	for (unsigned int k = 0; k < FUI_BORROWED; k++) {
		uint32_t a = FW_CUSTOM_MENU_ITEMS + 16 * k + 2;

		if (fenykl_rd16(a) == fui_borrowed_ids[k])
			continue;
		ok = false;
		fenykl_log_err(&fenykl_ui_log, "stock MISMATCH item %u title %08x = %x, expected %x",
			       k, a, fenykl_rd16(a), fui_borrowed_ids[k]);
	}
	return ok;
}

/* --- setup (task) --- */

static void fui_write32(const char *what, uint32_t addr, uint32_t v)
{
	fenykl_log_info(&fenykl_ui_log, "write %s %08x %08x -> %08x", what, addr, fenykl_rd32(addr),
			v);
	fenykl_wr32(addr, v);
}

int fenykl_ui_install(void)
{
	unsigned int super;
	uint32_t arr;
	int r = fui_prepare();

	if (r)
		return r;
	super = fenykl_rd8(FW_GUI_SUPER);
	arr = fui_lang_table();
	if ((super != 2 && (super < 6 || super > 8)) || !arr)
		return FENYKL_UI_EAGAIN;
	if (!fui_check_stock())
		return FENYKL_UI_ESTOCK;
	fenykl_log_info(&fenykl_ui_log, "%u stock words, %u borrowed ids ok, %u rows, lang %08x",
			FUI_STOCK, FUI_BORROWED, fui.nopts, arr);
	fui_build_tables();
	/* the clone before the cells that index past the stock table */
	fui_write32("slot 0x0D", FUI_SLOT_0D, (uint32_t)&fui_desc);
	fui_write32("menu actions", FW_SS_DESC_MENU + 0xC, (uint32_t)fui_menu_actions);
	fenykl_log_info(&fenykl_ui_log, "write cells OK %u RIGHT %u -> %u", fenykl_rd8(FUI_CELL_OK),
			fenykl_rd8(FUI_CELL_RIGHT), FUI_MENU_NACT);
	fenykl_wr8(FUI_CELL_OK, FUI_MENU_NACT);
	fenykl_wr8(FUI_CELL_RIGHT, FUI_MENU_NACT);
	fui_write32("rec setting", FUI_REC + 4, FUI_SETTING_NONE);
	fui_write32("kid avail", FW_CUSTOM_MENU_KID70 + 4, FUI_AVAIL_ALWAYS);
	fui_write32("lang title", arr + 4 * FUI_TITLE, (uint32_t)fui_s_title);
	fui_write32("lang help", arr + 4 * FUI_LANG_HELP, (uint32_t)fui_s_help);
	fui.lang_arr = arr;
	fui.installed = true;
	fenykl_log_info(&fenykl_ui_log, "installed: Custom J row 70 -> super 0x0D");
	return 0;
}

/* Ids still lent FUI_LANG_STABLE_TICKS ticks after the GUI left super 0x0D (an exit path the page
   did not see): given back here and logged as an error. */
static void fui_check_lent(void)
{
	if (!fui.lent_arr || fenykl_rd8(FW_GUI_SUPER) == FUI_SS) {
		fui.stray_ticks = 0;
		return;
	}
	if (++fui.stray_ticks < FUI_LANG_STABLE_TICKS)
		return;
	fenykl_log_err(&fenykl_ui_log, "lang ids still lent in GUI %02x: returned by the tick",
		       fenykl_rd8(FW_GUI_SUPER));
	fui_lang_return();
	fui.st.lang_stray++;
}

void fenykl_ui_tick(void)
{
	if (!fui.installed)
		return;
	fui_check_lent();
	fui_lang_poll();
	fui_tick_unsaved();
}
