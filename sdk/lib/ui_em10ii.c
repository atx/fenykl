/*
 * Settings page, E-M10 II part (sdk/docs/ui.md; the option core is ui.c). The Eye-Fi row of Custom
 * menu tab K (Utility) becomes "Fenykl" and opens the page:
 * - The slots of target.toml sdk_slots: condition 113 of the Eye-Fi rule answers "allowed" (the row
 *   is not grayed); OK on the row pushes the page's screen on the main menu app's screen stack;
 *   RIGHT on the row is swallowed. Both row callbacks return 1 ("handled"), so the stock On/Off
 *   pop-up, whose OK writes setting 0x193, never opens.
 * - The row's title descriptor becomes kind 5 (inline ASCII) "Fenykl", drawn in the stock font.
 * - The page draws no pixel: the Custom list view stays on screen and the page publishes its own
 *   model in GUI data slot 0x38, built from the list's own copy (tab column, frame) with the
 *   options as kind-5 title and value cells. The list redraws its slot right after the OK callback
 *   returns, so the page publishes on its first event (the OK release).
 * - The screen gets the keys; MENU pops it (the list re-shows from its own model), every other
 *   event goes to the app's default handler (half press, mode dial: the menu ends, the app
 *   deletes the screen).
 * Everything is installed for the boot. The callbacks run in the GUI task (CPU0). A call on another
 * core is counted and touches nothing: the condition answers "allowed", the row callbacks "handled",
 * the screen drops the event; only the delete still closes the page, as the screen is gone either way.
 */
#include "ui_int.h"

#define FUI_ROW_HANDLED 1			/* row callback: handled, the list redraws, no pop-up */
#define FUI_COND_ALLOWED 0

/* main menu app (data.menu_app) */
#define FUI_APP_WANT 0x4u			/* u16 wanted depth */
#define FUI_APP_DEPTH 0x6u			/* u16 depth */
#define FUI_APP_ENTRY(i) (0x10u + 0x10u * (i))	/* {u32 kind, u32 screen, u32 group, u16 id} */
#define FUI_APP_MAX 7u
#define FUI_PAGE_KIND 0xF0u			/* entry kind and group: no stock push matches, never group 0xF */
#define FUI_POP_CANCEL 2u			/* re-show reason of a stock pop-up left with MENU */

#define FUI_EV_RESHOW 0x971Du

/* Eye-Fi row record (data.custom_row_eyefi): u16 +0 setting id, +8 title {u16 kind, u16 lang, u32 text} */
#define FUI_ROW_SETTING 0x193u
#define FUI_TITLE_W0 (FW_CUSTOM_ROW_EYEFI + 8u)
#define FUI_TITLE_W1 (FW_CUSTOM_ROW_EYEFI + 12u)
#define FUI_TITLE_STOCK0 0x05FA0004u	/* kind 4, lang 0x5FA "Eye-Fi" */
#define FUI_TITLE_STOCK1 0x60D38D30u	/* an empty string */

static const char fui_s_title[] = "Fenykl";

/* --- the page model in slot 0x38 --- */

static void fui_cell_text(uint32_t off, const uint16_t *text)
{
	uint16_t *c = fui.page + off / 2;

	c[0] = FUI_KIND_INLINE;
	for (unsigned int i = 0; i < FUI_TEXT_LEN - 1 && text[i]; i++)
		c[FUI_CELL_TEXT / 2 + i] = text[i];
}

void fui_build_page(void)
{
	unsigned int page = fui.cursor / FUI_PAGE_ROWS;
	unsigned int pages = (fui.nopts + FUI_PAGE_ROWS - 1) / FUI_PAGE_ROWS;
	uint16_t *s = fui.page;

	memcpy(fui.page, fui.base, sizeof(fui.page));
	s[FUI_SCROLL / 2] = pages > 1;
	s[FUI_PAGES / 2] = (uint16_t)pages;
	s[FUI_PAGE / 2] = (uint16_t)page;
	for (unsigned int k = 0; k < FUI_PAGE_ROWS; k++) {
		unsigned int i = page * FUI_PAGE_ROWS + k;
		uint32_t title = FUI_TITLES + k * FUI_CELL_SIZE, value = FUI_VALUES + k * FUI_CELL_SIZE;
		uint16_t text[FUI_LABEL_LEN];
		const char *label;
		unsigned int n;

		memset(s + title / 2, 0, FUI_CELL_SIZE);
		memset(s + value / 2, 0, FUI_CELL_SIZE);
		memset(s + (FUI_WIDE_A + k * FUI_WIDE_SIZE) / 2, 0, FUI_WIDE_SIZE);
		memset(s + (FUI_WIDE_B + k * FUI_WIDE_SIZE) / 2, 0, FUI_WIDE_SIZE);
		s[FUI_CURSOR / 2 + k] = i == fui.cursor ? FUI_CURSOR_ROW : 0;
		s[FUI_CURSOR_B / 2 + k] = 0;
		s[FUI_MARK / 2 + k] = i < fui.nopts ? FUI_MARK_ROW : 0;
		if (i >= fui.nopts)
			continue;
		label = fui.opts[i]->label;
		for (n = 0; label[n] && n < FUI_LABEL_LEN - 1; n++)
			text[n] = (uint8_t)label[n];
		text[n] = 0;
		fui_cell_text(title, text);
		fui_value_text(fui.opts[i], text);
		fui_cell_text(value, text);
	}
}

/* Publishes the page; the view redraws on the slot's notification (GUI loop). */
static int fui_publish(void)
{
	int r;

	fui_build_page();
	r = fw_gui_data_set(fui.page, FUI_SLOT_ID, FUI_SLOT_SIZE, 1);
	fui.st.publishes++;
	if (r != 1) {
		fui.st.publish_fail++;
		fenykl_log_err(&fenykl_ui_log, "gui_data_set(0x38) failed: %d", r);
	}
	return r;
}

/* --- the page screen (GUI task, inside the menu app's event method) --- */

static void fui_screen_free(void *screen)
{
	(void)screen;
}
FENYKL_CALLBACK(menu_screen_free, fui_screen_free);

/* Every way the app drops the screen (pop, menu end) passes here; the object is static, so the
   no-op free is returned on every core (0 would make the firmware free it). */
static void *fui_screen_delete(void *screen)
{
	(void)screen;
	fui_count_core();
	fui_page_close();
	fui.open = false;
	fui.fresh = false;
	fenykl_log_info(&fenykl_ui_log, "close (%u)", fui.st.exits);
	return (void *)FENYKL_CB(menu_screen_free, fui_screen_free);
}
FENYKL_CALLBACK(menu_screen_delete, fui_screen_delete);

/* A key or dial on the page: 0, or the app's pop for MENU. */
static unsigned int fui_screen_key(unsigned int c, unsigned int param, void *app)
{
	bool dial = c == FUI_JOG_DIAL || c == FUI_CTRL_RING;
	/* negated as on the E-PL3, where a positive count moves the stock menus up */
	enum fui_result r = dial ? fui_dial(-(int16_t)param) : fui_key(c);
	int d = 0;

	fui.st.keys++;
	if (r == FUI_LEAVE) {
		fui.st.backs++;
		fenykl_log_info(&fenykl_ui_log, "MENU: pop, depth %u",
				fenykl_rd16((uint32_t)app + FUI_APP_DEPTH));
		fw_call_menu_app_pop(FW_MENU_APP_POP_FN, app, FUI_POP_CANCEL);
		return 0;
	}
	if (r == FUI_REDRAW)
		d = fui_publish();
	if (dial)
		fenykl_log_dbg(&fenykl_ui_log, "dial %04x param %08x -> cursor %u value %d", c, param,
			       fui.cursor, fui.edit_val);
	else
		fenykl_log_dbg(&fenykl_ui_log, "key %04x -> %d: cursor %u, data_set %d", c, r,
			       fui.cursor, d);
	return 0;
}

static unsigned int fui_screen_event(void *screen, unsigned int code, unsigned int param,
				     void *app)
{
	unsigned int c = code & 0xFFFF, r;

	(void)screen;
	if (fui_count_core())
		return 0;
	if (fui.fresh) {
		fui.fresh = false;
		memcpy(fui.base, (const void *)FW_GUI_DATA_SLOT38, sizeof(fui.base));
		r = fui_publish();
		fenykl_log_info(&fenykl_ui_log, "page: %u rows, cursor %u, data_set %d (event %04x)",
				fui.nopts, fui.cursor, r, c);
	}
	if (c >= FUI_KEY_UP && c <= FUI_KEY_MENU + 1 && (c & 1)) {
		fui.st.ignored++;
		return 0;	/* key releases */
	}
	if ((c >= FUI_KEY_UP && c <= FUI_KEY_MENU) || c == FUI_JOG_DIAL || c == FUI_CTRL_RING)
		return fui_screen_key(c, param, app);
	if (c == FUI_EV_RESHOW) {
		fui_publish();
		return 0;
	}
	r = fw_menu_app_default(app, code, param);
	fui.st.sys++;
	fenykl_log_info(&fenykl_ui_log, "event %04x param %08x -> default %x", c, param, r);
	return r;
}
FENYKL_CALLBACK(menu_screen, fui_screen_event);

/* Pushes the screen as the app's push method would, without its factory: 0 or why not. */
static int fui_push(void)
{
	uint32_t app = fenykl_rd32(FW_MENU_APP), top, vt, e;
	unsigned int want, depth;

	if (!fenykl_dram_ptr(app, 4))
		return -1;
	if (fenykl_rd32(app) != FW_MENU_APP_VTABLE)
		return -2;
	want = fenykl_rd16(app + FUI_APP_WANT);
	depth = fenykl_rd16(app + FUI_APP_DEPTH);
	if (want != depth || depth < 1 || depth >= FUI_APP_MAX)
		return -3;
	top = fenykl_rd32(app + FUI_APP_ENTRY(depth) + 4);
	vt = fenykl_dram_ptr(top, 4) ? fenykl_rd32(top) : 0;
	if (vt != FW_MENU_SCREEN_VT_LIST0 && vt != FW_MENU_SCREEN_VT_LIST5) {
		fenykl_log_err(&fenykl_ui_log, "top screen %08x has vtable %08x", top, vt);
		return -4;
	}
	e = app + FUI_APP_ENTRY(depth + 1);
	memset((void *)e, 0, 0x10);
	fenykl_wr32(e, FUI_PAGE_KIND);
	fenykl_wr32(e + 4, (uint32_t)&fui.screen);
	fenykl_wr32(e + 8, FUI_PAGE_KIND);
	fenykl_wr16(app + FUI_APP_DEPTH, depth + 1);
	fenykl_wr16(app + FUI_APP_WANT, depth + 1);
	return 0;
}

/* --- the Eye-Fi row --- */

static unsigned int fui_cond(void)
{
	fui_count_core();
	return FUI_COND_ALLOWED;
}
FENYKL_CALLBACK(mode_cond, fui_cond);

static int fui_row_ok(void *act)
{
	int r;

	if (fui_count_core())
		return FUI_ROW_HANDLED;
	if (fui.open) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "OK on the row: page already open");
		return FUI_ROW_HANDLED;
	}
	r = fui_push();
	if (r) {
		fui.st.refused++;
		fenykl_log_err(&fenykl_ui_log, "NOT opened (%d): app %08x", r,
			       fenykl_rd32(FW_MENU_APP));
		return FUI_ROW_HANDLED;
	}
	fui_page_open();
	fui.open = true;
	fui.fresh = true;
	fenykl_log_info(&fenykl_ui_log, "open: actuator %08x, depth %u", (uint32_t)act,
			fenykl_rd16(fenykl_rd32(FW_MENU_APP) + FUI_APP_DEPTH));
	return FUI_ROW_HANDLED;
}
FENYKL_CALLBACK(menu_row, fui_row_ok);

static int fui_row_right(void *act)
{
	if (fui_count_core())
		return FUI_ROW_HANDLED;
	fui.st.wrap_pass++;
	fenykl_log_dbg(&fenykl_ui_log, "RIGHT on the row: swallowed, actuator %08x", (uint32_t)act);
	return FUI_ROW_HANDLED;
}
FENYKL_CALLBACK(menu_row, fui_row_right);

/* --- setup (task) --- */

static const struct fui_expect fui_stock[] = {
	{ FW_MENU_APP_VTABLE + 4, 4, FW_MENU_APP_EVENT_FN, "app vtable event" },
	{ FW_MENU_APP_VTABLE + 12, 4, FW_MENU_APP_POP_FN, "app vtable pop" },
	{ FW_CUSTOM_ROW_EYEFI, 2, FUI_ROW_SETTING, "row setting" },
	{ FUI_TITLE_W0, 4, FUI_TITLE_STOCK0, "row title kind" },
	{ FUI_TITLE_W1, 4, FUI_TITLE_STOCK1, "row title text" },
};
#define FUI_STOCK (sizeof(fui_stock) / sizeof(fui_stock[0]))

/* The row callbacks before the condition: the row is never enabled with the stock pop-up behind it. */
static int fui_take_row(void)
{
	int ok = FENYKL_SLOT_REPLACE(eyefi_ok_fn, fui_row_ok);
	int right = ok ? 0 : FENYKL_SLOT_REPLACE(eyefi_right_fn, fui_row_right);
	int cond = ok || right ? 0 : FENYKL_SLOT_REPLACE(mode_cond_eyefi, fui_cond);

	if (!ok && !right && !cond)
		return 0;
	fenykl_log_err(&fenykl_ui_log, "slots NOT replaced: ok %d, right %d, cond %d", ok, right,
		       cond);
	cond = fenykl_slot_restore(&fenykl_slot_mode_cond_eyefi);
	right = fenykl_slot_restore(&fenykl_slot_eyefi_right_fn);
	ok = fenykl_slot_restore(&fenykl_slot_eyefi_ok_fn);
	fenykl_log_err(&fenykl_ui_log, "restored: cond %d, right %d, ok %d", cond, right, ok);
	return FENYKL_UI_ESTOCK;
}

int fenykl_ui_install(void)
{
	int r = fui_prepare();

	if (r)
		return r;
	if (!fui_check_words(fui_stock, FUI_STOCK))
		return FENYKL_UI_ESTOCK;
	fui.screen_vt[0] = FENYKL_CB(menu_screen, fui_screen_event);
	fui.screen_vt[1] = FENYKL_CB(menu_screen_delete, fui_screen_delete);
	fui.screen = (uint32_t)fui.screen_vt;
	r = fui_take_row();
	if (r)
		return r;
	/* pointer first: a torn read gives one list build an empty title (the stock text) */
	fenykl_wr32(FUI_TITLE_W1, (uint32_t)fui_s_title);
	fenykl_wr32(FUI_TITLE_W0, FUI_KIND_INLINE);
	fui.installed = true;
	fenykl_log_info(&fenykl_ui_log, "installed: Custom K Eye-Fi row, %u rows, title %08x %08x",
			fui.nopts, fenykl_rd32(FUI_TITLE_W0), fenykl_rd32(FUI_TITLE_W1));
	return 0;
}

void fenykl_ui_tick(void)
{
	if (!fui.installed)
		return;
	fui_tick_unsaved();
}
