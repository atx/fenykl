/*
 * Settings page (sdk/docs/ui.md). All changes are RAM
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

#define GUI_APP_PANEL 0x19Cu
#define SS_FENYKL 0x0D
#define SLOT_0D (FW_SS_DESCS + 4 * (SS_FENYKL - 1))
#define MENU_STATES 15
#define MENU_ROWS 68
#define MENU_NACT 220
#define ST_WIZARD 4
#define ROW_RIGHT 5
#define ROW_OK 6
#define CELL_RIGHT (FW_MENU_MATRIX + ROW_RIGHT * MENU_STATES + ST_WIZARD - 1)
#define CELL_OK (FW_MENU_MATRIX + ROW_OK * MENU_STATES + ST_WIZARD - 1)
#define ACT_RIGHT 12			/* FW_MENU_ACT_RIGHT: on_key(3) */
#define ACT_OK 13			/* FW_MENU_ACT_OK: on_key(4) */
#define ACT_GENIFA 200

#define FENYKL_ROW 70
#define REC (FW_CUSTOM_MENU_ITEMS + 16 * FENYKL_ROW)	/* u32 setting +4; help +0xA */
#define LANG_COUNT 1341u
#define LANG_HELP 0x1DFu
#define LANG_STABLE_TICKS 3u

#define SETTING_NONE 0xFFFFFFF1u
#define AVAIL_ALWAYS 0x0000FFF1u
#define TO_FENYKL 0x0D01u
#define TO_LIST 0x0304u

struct fui_state fui;

/* Titles of custom_menu_items 0..20, checked at install. */
const uint16_t fui_borrowed_ids[FUI_BORROWED] = {
	0x033, 0x21B, 0x031, 0x0E0, 0x04D, 0x08B, 0x0AC, 0x02F, 0x032, 0x082,
	0x02C, 0x02B, 0x060, 0x063, 0x062, 0x064, 0x0DE, 0x0DD, 0x486, 0x526,
	0x15D,
};

static const __CHAR16_TYPE__ fui_s_title[] = u"Fenykl";
static const __CHAR16_TYPE__ fui_s_help[] = u"Fenykl plugin settings";
static const __CHAR16_TYPE__ fui_s_unsaved[] = u"Fenykl: not saved";

FENYKL_LOG_MODULE(fenykl_ui_log, "ui", FENYKL_LOG_INFO);

static uint8_t rd8(uint32_t a)
{
	return *(volatile uint8_t *)(uintptr_t)a;
}

static uint16_t rd16(uint32_t a)
{
	return *(volatile uint16_t *)(uintptr_t)a;
}

static int16_t rd16s(uint32_t a)
{
	return *(volatile int16_t *)(uintptr_t)a;
}

static uint32_t rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

static void wr8(uint32_t a, uint8_t v)
{
	*(volatile uint8_t *)(uintptr_t)a = v;
}

static void wr32(uint32_t a, uint32_t v)
{
	*(volatile uint32_t *)(uintptr_t)a = v;
}

static bool dram_pointer(uint32_t p)
{
	return (p & 3) == 0 && ((p >> 28) == 0x6 || (p >> 28) == 0xA);
}

/* --- page model (GUI task) --- */

int fui_collect(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end)
{
	const struct fenykl_cfg_group *groups[FENYKL_UI_OPTS_MAX];
	const struct fenykl_cfg_opt *o;
	unsigned int n = 0, ng = 0, i;

	for (o = begin; o < end; o++)
		if (o->group)
			n++;
	if (!n || n > FENYKL_UI_OPTS_MAX)
		return FENYKL_UI_EOPTS;
	for (o = begin; o < end; o++) {
		const struct fenykl_cfg_group *g = o->group;

		if (!g)
			continue;
		for (i = 0; i < ng && groups[i] != g; i++)
			;
		if (i < ng)
			continue;
		/* sorted by order; equal orders keep link order */
		for (i = ng; i > 0 && groups[i - 1]->order > g->order; i--)
			groups[i] = groups[i - 1];
		groups[i] = g;
		ng++;
	}
	fui.nopts = 0;
	for (i = 0; i < ng; i++)
		for (o = begin; o < end; o++)
			if (o->group == groups[i])
				fui.opts[fui.nopts++] = o;
	fui.cursor = 0;
	fui.edit = false;
	return 0;
}

static void fui_set(const struct fenykl_cfg_opt *o, int32_t v)
{
	int r = fenykl_cfg_set(o, v);

	if (r) {
		fui.st.set_fail++;
		fenykl_log_warn(&fenykl_ui_log, "set %s = %d failed: %d", o->name, v, r);
		return;
	}
	fui.st.sets++;
	fenykl_log_info(&fenykl_ui_log, "set %s = %d", o->name, v);
}

static void fui_edit_clamp(const struct fenykl_cfg_opt *o, int64_t v)
{
	fui.edit_val = (int32_t)(v < o->min ? o->min : v > o->max ? o->max : v);
}

static enum fui_result fui_edit_key(const struct fenykl_cfg_opt *o, unsigned int code)
{
	int64_t step = o->step > 0 ? o->step : 1, v = fui.edit_val;

	switch (code) {
	case FUI_KEY_LEFT:
		v -= step;
		break;
	case FUI_KEY_RIGHT:
		v += step;
		break;
	case FUI_KEY_OK:
		fui.edit = false;
		fui_set(o, fui.edit_val);
		return FUI_REDRAW;
	case FUI_KEY_MENU:
		fui.edit = false;
		return FUI_REDRAW;
	default:
		return FUI_NONE;
	}
	fui_edit_clamp(o, v);
	return FUI_REDRAW;
}

/* LEFT, RIGHT or OK on the cursor row. */
static enum fui_result fui_row_key(const struct fenykl_cfg_opt *o, unsigned int code)
{
	int d = code == FUI_KEY_LEFT ? -1 : 1;

	switch (o->type) {
	case FENYKL_CFG_BOOL:
		fui_set(o, !*o->value);
		return FUI_REDRAW;
	case FENYKL_CFG_ENUM:
		fui_set(o, (*o->value + d + o->max + 1) % (o->max + 1));
		return FUI_REDRAW;
	case FENYKL_CFG_INT:
		if (code == FUI_KEY_LEFT)
			return FUI_NONE;
		fui.edit = true;
		fui.edit_val = *o->value;
		return FUI_REDRAW;
	}
	return FUI_NONE;
}

enum fui_result fui_key(unsigned int code)
{
	const struct fenykl_cfg_opt *o = fui.opts[fui.cursor];

	if (fui.edit)
		return fui_edit_key(o, code);
	switch (code) {
	case FUI_KEY_UP:
		fui.cursor = (fui.cursor + fui.nopts - 1) % fui.nopts;
		return FUI_REDRAW;
	case FUI_KEY_DOWN:
		fui.cursor = (fui.cursor + 1) % fui.nopts;
		return FUI_REDRAW;
	case FUI_KEY_MENU:
		return FUI_LEAVE;
	case FUI_KEY_LEFT:
	case FUI_KEY_RIGHT:
	case FUI_KEY_OK:
		return fui_row_key(o, code);
	}
	return FUI_NONE;
}

/* n dial steps: n > 0 moves the cursor down and increases an edited value. */
enum fui_result fui_dial(int n)
{
	const struct fenykl_cfg_opt *o = fui.opts[fui.cursor];
	int64_t step = o->step > 0 ? o->step : 1;
	int c;

	if (!n)
		return FUI_NONE;
	if (fui.edit) {
		fui_edit_clamp(o, fui.edit_val + (int64_t)n * FUI_DIAL_STEPS * step);
		return FUI_REDRAW;
	}
	c = ((int)fui.cursor + n) % (int)fui.nopts;
	fui.cursor = c < 0 ? c + fui.nopts : c;
	return FUI_REDRAW;
}

/* --- the list slot --- */

static uint16_t *fui_put(uint16_t *p, const uint16_t *end, const char *s)
{
	while (*s && p < end)
		*p++ = (uint8_t)*s++;
	return p;
}

static void fui_dec(char *p, int32_t v)
{
	char t[10];
	uint32_t u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v;
	int n = 0;

	if (v < 0)
		*p++ = '-';
	do
		t[n++] = (char)('0' + u % 10);
	while (u /= 10);
	while (n)
		*p++ = t[--n];
	*p = 0;
}

void fui_value_text(const struct fenykl_cfg_opt *o, uint16_t out[FUI_VALUE_LEN])
{
	const uint16_t *end = out + FUI_VALUE_LEN - 1;
	bool editing = fui.edit && o == fui.opts[fui.cursor];
	uint16_t *p = out;
	char num[12];

	switch (o->type) {
	case FENYKL_CFG_BOOL:
		p = fui_put(p, end, *o->value ? "On" : "Off");
		break;
	case FENYKL_CFG_ENUM:
		p = fui_put(p, end, o->names[*o->value]);
		break;
	case FENYKL_CFG_INT:
		fui_dec(num, editing ? fui.edit_val : *o->value);
		if (editing)
			*p++ = FUI_ARROW_LEFT;
		p = fui_put(p, end, num);
		if (editing && p < end)
			*p++ = FUI_ARROW_RIGHT;
		break;
	}
	*p = 0;
}

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

/* The current string table if it looks sane: word 0 -> array, word 1 -> its end (LANG_COUNT
   entries and the 0 terminator), the first string right after the header, both overridden
   entries ours or inside it. Its array, or 0. */
static uint32_t fui_lang_table(void)
{
	uint32_t b = rd32(FW_OSD_LANG_CUR), a, e, t, h;

	if (!dram_pointer(b))
		return 0;
	a = rd32(b);
	e = rd32(b + 4);
	if (!dram_pointer(a) || e - a != 4 * LANG_COUNT || rd32(a) != b + 8 || rd32(e) != 0)
		return 0;
	t = rd32(a + 4 * FUI_TITLE);
	h = rd32(a + 4 * LANG_HELP);
	if (t != (uint32_t)fui_s_title && (t < b + 8 || t >= a))
		return 0;
	if (h != (uint32_t)fui_s_help && (h < b + 8 || h >= a))
		return 0;
	return a;
}

static void fui_lang_apply(uint32_t arr)
{
	wr32(arr + 4 * FUI_TITLE, (uint32_t)fui_s_title);
	wr32(arr + 4 * LANG_HELP, (uint32_t)fui_s_help);
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
		fui.lent_orig[k] = rd32(a);
		wr32(a, s);
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

		if (s && rd32(a) == s)
			wr32(a, fui.lent_orig[k]);
	}
	fui.lent_arr = 0;
	fui.st.returns++;
}

/* Re-applies after a language change, once the new table has stayed in place for
   LANG_STABLE_TICKS ticks (a table being loaded must not get the pointers). */
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
	if (rd32(arr + 4 * FUI_TITLE) == (uint32_t)fui_s_title &&
	    rd32(arr + 4 * LANG_HELP) == (uint32_t)fui_s_help)
		return;
	if (fui.lang_same < LANG_STABLE_TICKS)
		return;
	fenykl_log_info(&fenykl_ui_log, "language re-applied, table %08x%s", arr,
			arr != fui.lang_arr ? " (moved)" : "");
	fui_lang_apply(arr);
	fui.st.lang_reapply++;
}

/* --- GUI actions (GUI task) --- */

static uint32_t fui_menu_actions[MENU_NACT + 1];	/* SS_MENU_MAIN's table + the wrapper */

static void fui_count_core(void)
{
	if (FENYKL_CORE_ID() != 0)
		fui.st.cpu1_actions++;
}

/* Class vtable of the panel object on screen, 0 if there is none. */
static uint32_t fui_panel_vtable(void)
{
	uint32_t app = rd32(FW_GUI_APP), panel;

	if (!dram_pointer(app))
		return 0;
	panel = rd32(app + GUI_APP_PANEL);
	return dram_pointer(panel) ? rd32(panel) : 0;
}

/* StWizardMenu's stock action for a system event, 0 if none. */
static unsigned int fui_wizard_action(unsigned int code)
{
	for (unsigned int i = 0; i < MENU_ROWS; i++) {
		if (rd32(FW_MENU_EVMAP + 8 * i) != code)
			continue;
		unsigned int row = rd32(FW_MENU_EVMAP + 8 * i + 4);

		if (row >= MENU_ROWS)
			return 0;
		unsigned int act = rd8(FW_MENU_MATRIX + row * MENU_STATES + ST_WIZARD - 1);

		return act <= 2 || act >= MENU_NACT ? 0 : act;
	}
	return 0;
}

/* Back to StWizardMenu as the end of a stock menu warning does (action 170 0x6F085A35): set the
   warning-return flag, return 0x0304. Its entry 0x6F07DED6 then clears the flag and posts 0x9ABC,
   which rebuilds the Custom list (slots 0xE/0xF) with the cursor unchanged. */
static unsigned int fui_back_to_list(void)
{
	uint32_t flags = rd32(FW_MENU_FLAGS_PTR);

	if (flags)
		wr32(flags + 0x14, 1);
	else
		fui.st.flag_missing++;
	return TO_LIST;
}

static unsigned int fui_on_wrap(void *sm, unsigned int code, unsigned int p, void *p2)
{
	unsigned int c = code & 0xFFFF;
	uint32_t stock = c == FUI_KEY_RIGHT ? fui.stock_right : fui.stock_ok;
	uint32_t panel, vt, arr;
	unsigned int r;

	if (FENYKL_CORE_ID() != 0) {
		fui.st.cpu1_actions++;
		return fw_call_gui_action(stock, sm, code, p, p2);
	}
	panel = rd32(FW_CUSTOM_PANEL);
	if (!panel || rd32(FW_MENU_CUR_PANEL) != panel || rd16s(FW_CUSTOM_CUR_ITEM) != FENYKL_ROW) {
		r = fw_call_gui_action(stock, sm, code, p, p2);
		fui.st.wrap_pass++;
		fenykl_log_dbg(&fenykl_ui_log, "wrapper pass %04x -> %04x, cur %d", c, r & 0xFFFF,
			       rd16s(FW_CUSTOM_CUR_ITEM));
		return r;
	}
	vt = fui_panel_vtable();
	arr = fui_lang_table();
	if (vt != FW_MENULIST_VTABLE || !arr) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "NOT opened: panel %d vtable %08x (want %08x), lang %08x",
			       rd16s(FW_GUI_CUR_PANEL), vt, FW_MENULIST_VTABLE, arr);
		return 0;
	}
	fui.st.wrap_in++;
	fenykl_log_info(&fenykl_ui_log, "open (key %04x, panel %d, vtable %08x)", c,
			rd16s(FW_GUI_CUR_PANEL), vt);
	return TO_FENYKL;
}
FENYKL_CALLBACK(gui_action, fui_on_wrap);

unsigned int fui_on_entry(void *sm, unsigned int code, unsigned int p, void *p2)
{
	struct fenykl_cfg_stats cs;
	uint32_t arr = fui_lang_table();
	int r;

	fui_count_core();
	fui.st.entries++;
	fenykl_cfg_get_stats(&cs);
	fui.fails_at_open = cs.save_fail;
	fui.unsaved = false;
	fui.edit = false;
	if (arr)
		fui_lang_lend(arr);
	else
		fenykl_log_err(&fenykl_ui_log, "entry: no language table, titles NOT lent");
	r = fui_publish();
	fenykl_log_info(&fenykl_ui_log, "entry %04x: panel %d vtable %08x, lang %08x", code & 0xFFFF,
			rd16s(FW_GUI_CUR_PANEL), fui_panel_vtable(), arr);
	fenykl_log_info(&fenykl_ui_log, "entry: %u rows, cursor %u, data_set %d", fui.nopts,
			fui.cursor, r);
	return 0;
}
FENYKL_CALLBACK(gui_action, fui_on_entry);

/* Every way out of super 0x0D passes here: a pending edit is dropped, the ids go back. */
unsigned int fui_on_exit(void *sm, unsigned int code, unsigned int p, void *p2)
{
	fui_count_core();
	fui.st.exits++;
	if (fui.edit) {
		fui.edit = false;
		fenykl_log_info(&fenykl_ui_log, "edit of %s dropped", fui.opts[fui.cursor]->name);
	}
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
		unsigned int state = r & 0xFF ? r & 0xFF : (super == 0x03 ? ST_WIZARD : 0);

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

struct gui_ss_desc {
	uint8_t n_states, pad;
	uint16_t n_rows, n_actions, n_map;
	const uint8_t *matrix;
	const uint32_t *actions;
	const uint32_t *evmap;		/* {u32 code, u32 row} */
	const uint32_t *entry;
	const uint32_t *exit;
	uint8_t wide, pad2[3];
};
_Static_assert(sizeof(struct gui_ss_desc) == 0x20, "descriptor layout");

enum { A_KEY = 3, A_SYS, A_IGNORE, A_GENIFA, N_ACTIONS };

static const uint16_t fui_events[][2] = {
	{ FUI_KEY_UP, A_KEY }, { FUI_KEY_DOWN, A_KEY }, { FUI_KEY_LEFT, A_KEY },
	{ FUI_KEY_RIGHT, A_KEY }, { FUI_KEY_OK, A_KEY }, { FUI_KEY_MENU, A_KEY },
	{ FUI_JOG_DIAL, A_KEY }, { FUI_CTRL_RING, A_KEY },
	/* system events: StWizardMenu's own action, see fui_on_sys */
	{ 0x9000, A_SYS },	/* shutter half press */
	{ 0x9002, A_SYS },	/* full press */
	{ 0x9010, A_SYS },	/* PLAY */
	{ 0x9012, A_SYS },	/* MOVIE */
	{ 0x9030, A_SYS },	/* mode dial */
	{ 0x9033, A_SYS },	/* POWER */
	{ 0x9300, A_SYS },	/* USB */
	{ 0x9303, A_SYS },	/* EVF state */
	{ 0x9304, A_SYS },	/* PENPAL */
	{ 0x9306, A_SYS },	/* EVF key on */
	{ 0x9307, A_SYS },	/* EVF key off */
	{ 0x9601, A_SYS },	/* REQ_CHG_OUTPUT_DISPLAY */
	{ 0x9620, A_SYS },	/* REQ_START_REC */
	{ 0x9622, A_SYS },	/* REQ_START_PLAY */
	{ 0x9623, A_SYS },	/* REQ_END_PLAY */
	{ 0x9624, A_SYS },	/* REQ_START_MENU */
	{ 0x9625, A_SYS },	/* REQ_END_MENU */
	{ 0x9626, A_SYS },	/* REQ_DISP_BLACK_SCREEN */
	{ 0x9627, A_SYS },	/* WARNING */
	{ 0x962C, A_SYS },	/* not in SS_MENU_MAIN's map: back to the list */
	{ 0x9ABB, A_SYS },	/* INF_WARNING -> 0x030E */
	/* logged, ignored */
	{ 0x900D, A_IGNORE }, { 0x9014, A_IGNORE }, { 0x9016, A_IGNORE }, { 0x9018, A_IGNORE },
	{ 0x901A, A_IGNORE }, { 0x901C, A_IGNORE }, { 0x903E, A_IGNORE }, { 0x9040, A_IGNORE },
	{ 0x9404, A_IGNORE }, { 0x9B01, A_IGNORE },
	/* EVT_GUI_END_GENIFA_DRAW: the stock action every SS_MENU_MAIN state maps it to */
	{ 0x9A41, A_GENIFA },
};
#define FUI_EVENTS (sizeof(fui_events) / sizeof(fui_events[0]))

static uint32_t fui_actions[N_ACTIONS];
static uint32_t fui_evmap[FUI_EVENTS * 2];
static uint8_t fui_matrix[FUI_EVENTS];
static uint32_t fui_entry_tab[1], fui_exit_tab[1];
static struct gui_ss_desc fui_desc;

static void fui_build_tables(void)
{
	memcpy(fui_menu_actions, (const void *)FW_MENU_ACTIONS, MENU_NACT * 4);
	fui_menu_actions[MENU_NACT] = FENYKL_CB(gui_action, fui_on_wrap);
	fui.stock_ok = fui_menu_actions[ACT_OK];
	fui.stock_right = fui_menu_actions[ACT_RIGHT];

	fui_actions[0] = FW_MENU_NOOP0;
	fui_actions[1] = FW_MENU_NOOP1;
	fui_actions[2] = FW_MENU_NOOP2;
	fui_actions[A_KEY] = FENYKL_CB(gui_action, fui_on_key);
	fui_actions[A_SYS] = FENYKL_CB(gui_action, fui_on_sys);
	fui_actions[A_IGNORE] = FENYKL_CB(gui_action, fui_on_ignore);
	fui_actions[A_GENIFA] = fui_menu_actions[ACT_GENIFA];
	for (unsigned int i = 0; i < FUI_EVENTS; i++) {
		fui_evmap[2 * i] = fui_events[i][0];
		fui_evmap[2 * i + 1] = i;
		fui_matrix[i] = (uint8_t)fui_events[i][1];
	}
	fui_entry_tab[0] = FENYKL_CB(gui_action, fui_on_entry);
	fui_exit_tab[0] = FENYKL_CB(gui_action, fui_on_exit);
	fui_desc = (struct gui_ss_desc){
		.n_states = 1,
		.n_rows = FUI_EVENTS,
		.n_actions = N_ACTIONS,
		.n_map = FUI_EVENTS,
		.matrix = fui_matrix,
		.actions = fui_actions,
		.evmap = fui_evmap,
		.entry = fui_entry_tab,
		.exit = fui_exit_tab,
	};
}

/* --- preconditions: every stock word changed or relied on --- */

struct fui_expect {
	uint32_t addr;
	uint8_t size;
	uint32_t value;
	const char *what;
};

static const struct fui_expect fui_stock[] = {
	{ FW_GUI_SUPER + 4, 4, FW_SS_DESCS, "gui_sm descs" },
	{ FW_SS_DESCS + 4 * 2, 4, FW_SS_DESC_MENU, "desc SS_MENU_MAIN" },
	{ SLOT_0D, 4, FW_SS_DESC_0D_STUB, "desc slot 0x0D" },
	{ FW_SS_DESC_MENU + 0x0, 1, MENU_STATES, "menu n_states" },
	{ FW_SS_DESC_MENU + 0x2, 2, MENU_ROWS, "menu n_rows" },
	{ FW_SS_DESC_MENU + 0x4, 2, MENU_NACT, "menu n_actions" },
	{ FW_SS_DESC_MENU + 0x6, 2, MENU_ROWS, "menu n_map" },
	{ FW_SS_DESC_MENU + 0x8, 4, FW_MENU_MATRIX, "menu matrix" },
	{ FW_SS_DESC_MENU + 0xC, 4, FW_MENU_ACTIONS, "menu actions" },
	{ FW_SS_DESC_MENU + 0x10, 4, FW_MENU_EVMAP, "menu evmap" },
	{ FW_SS_DESC_MENU + 0x1C, 1, 0, "menu wide" },
	{ FW_MENU_EVMAP + 8 * ROW_RIGHT, 4, FUI_KEY_RIGHT, "evmap RIGHT" },
	{ FW_MENU_EVMAP + 8 * ROW_RIGHT + 4, 4, ROW_RIGHT, "evmap RIGHT row" },
	{ FW_MENU_EVMAP + 8 * ROW_OK, 4, FUI_KEY_OK, "evmap OK" },
	{ FW_MENU_EVMAP + 8 * ROW_OK + 4, 4, ROW_OK, "evmap OK row" },
	{ CELL_RIGHT, 1, ACT_RIGHT, "cell RIGHT/WIZARD" },
	{ CELL_OK, 1, ACT_OK, "cell OK/WIZARD" },
	{ FW_MENU_ACTIONS + 4 * 0, 4, FW_MENU_NOOP0, "action 0" },
	{ FW_MENU_ACTIONS + 4 * 1, 4, FW_MENU_NOOP1, "action 1" },
	{ FW_MENU_ACTIONS + 4 * 2, 4, FW_MENU_NOOP2, "action 2" },
	{ FW_MENU_ACTIONS + 4 * ACT_RIGHT, 4, FW_MENU_ACT_RIGHT, "action RIGHT" },
	{ FW_MENU_ACTIONS + 4 * ACT_OK, 4, FW_MENU_ACT_OK, "action OK" },
	{ FW_MENU_ACTIONS + 4 * ACT_GENIFA, 4, FW_MENU_ACT_GENIFA, "action GENIFA" },
	{ REC + 0x0, 2, 9, "rec tab" },
	{ REC + 0x2, 2, FUI_TITLE, "rec title" },
	{ REC + 0x4, 4, 0x223, "rec setting" },
	{ REC + 0xA, 2, LANG_HELP, "rec help" },
	{ REC + 0xC, 2, 0, "rec fmt" },
	{ REC + 0x10, 2, 0x92, "end of records" },
	{ FW_CUSTOM_MENU_KID70 + 0, 4, 0xA9, "kid node" },
	{ FW_CUSTOM_MENU_KID70 + 4, 4, 0x223, "kid avail" },
};
#define FUI_STOCK (sizeof(fui_stock) / sizeof(fui_stock[0]))

static bool fui_check_stock(void)
{
	bool ok = true;

	for (unsigned int i = 0; i < FUI_STOCK; i++) {
		const struct fui_expect *e = &fui_stock[i];
		uint32_t v = e->size == 1 ? rd8(e->addr) : e->size == 2 ? rd16(e->addr) : rd32(e->addr);

		if (v == e->value)
			continue;
		ok = false;
		fenykl_log_err(&fenykl_ui_log, "stock MISMATCH %s %08x = %x, expected %x", e->what,
			       e->addr, v, e->value);
	}
	for (unsigned int k = 0; k < FUI_BORROWED; k++) {
		uint32_t a = FW_CUSTOM_MENU_ITEMS + 16 * k + 2;

		if (rd16(a) == fui_borrowed_ids[k])
			continue;
		ok = false;
		fenykl_log_err(&fenykl_ui_log, "stock MISMATCH item %u title %08x = %x, expected %x",
			       k, a, rd16(a), fui_borrowed_ids[k]);
	}
	return ok;
}

/* --- setup (task) --- */

static void fui_write32(const char *what, uint32_t addr, uint32_t v)
{
	fenykl_log_info(&fenykl_ui_log, "write %s %08x %08x -> %08x", what, addr, rd32(addr), v);
	wr32(addr, v);
}

int fenykl_ui_install(void)
{
	unsigned int super = rd8(FW_GUI_SUPER);
	uint32_t arr;
	int r;

	if (fui.installed)
		return FENYKL_UI_EBUSY;
	arr = fui_lang_table();
	if ((super != 2 && (super < 6 || super > 8)) || !arr)
		return FENYKL_UI_EAGAIN;
	r = fui_collect(fenykl_cfg_start, fenykl_cfg_end);
	if (r) {
		fenykl_log_err(&fenykl_ui_log, "no option with a group, or more than %u",
			       FENYKL_UI_OPTS_MAX);
		return r;
	}
	if (!fui_check_stock())
		return FENYKL_UI_ESTOCK;
	fenykl_log_info(&fenykl_ui_log, "%u stock words, %u borrowed ids ok, %u rows, lang %08x",
			FUI_STOCK, FUI_BORROWED, fui.nopts, arr);
	fui_build_tables();
	/* the clone before the cells that index past the stock table */
	fui_write32("slot 0x0D", SLOT_0D, (uint32_t)&fui_desc);
	fui_write32("menu actions", FW_SS_DESC_MENU + 0xC, (uint32_t)fui_menu_actions);
	fenykl_log_info(&fenykl_ui_log, "write cells OK %u RIGHT %u -> %u", rd8(CELL_OK),
			rd8(CELL_RIGHT), MENU_NACT);
	wr8(CELL_OK, MENU_NACT);
	wr8(CELL_RIGHT, MENU_NACT);
	fui_write32("rec setting", REC + 4, SETTING_NONE);
	fui_write32("kid avail", FW_CUSTOM_MENU_KID70 + 4, AVAIL_ALWAYS);
	fui_write32("lang title", arr + 4 * FUI_TITLE, (uint32_t)fui_s_title);
	fui_write32("lang help", arr + 4 * LANG_HELP, (uint32_t)fui_s_help);
	fui.lang_arr = arr;
	fui.installed = true;
	fenykl_log_info(&fenykl_ui_log, "installed: Custom J row 70 -> super 0x0D");
	return 0;
}

/* Ids still lent LANG_STABLE_TICKS ticks after the GUI left super 0x0D (an exit path the page
   did not see): given back here and logged as an error. */
static void fui_check_lent(void)
{
	if (!fui.lent_arr || rd8(FW_GUI_SUPER) == SS_FENYKL) {
		fui.stray_ticks = 0;
		return;
	}
	if (++fui.stray_ticks < LANG_STABLE_TICKS)
		return;
	fenykl_log_err(&fenykl_ui_log, "lang ids still lent in GUI %02x: returned by the tick",
		       rd8(FW_GUI_SUPER));
	fui_lang_return();
	fui.st.lang_stray++;
}

void fenykl_ui_tick(void)
{
	struct fenykl_cfg_stats st;
	bool dirty = fenykl_cfg_dirty();

	if (!fui.installed)
		return;
	fui_check_lent();
	fui_lang_poll();
	fenykl_cfg_get_stats(&st);
	fui.unsaved = dirty && (!fenykl_card_io_allowed() || st.save_fail != fui.fails_at_open);
}

void fenykl_ui_get_stats(struct fenykl_ui_stats *st)
{
	*st = fui.st;
}
