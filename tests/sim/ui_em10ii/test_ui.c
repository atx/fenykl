/*
 * E-M10 II settings page (sdk/lib/ui_em10ii.c) in the GDB simulator, driven the way the firmware
 * drives it: install (all or nothing on a stock mismatch), then the row callbacks and the page
 * screen's methods entered through the slot words and the pushed app entry with the firmware's
 * calling convention (fw_call_* thunks). Checks the push onto a fake menu app, the first publish
 * at the first event, the slot 0x38 image (cells, cursor, marks, scroll bar, untouched head), keys,
 * releases, page flips, an ENUM edit through the real cfg library, MENU popping the screen through
 * the app's pop method, the delete, forwarding to the default handler and the refusals.
 * plugin_main sets sim_exit_code to 0 or 1 + the number of the first failed check.
 */
#include <fenykl.h>
#include "../../../sdk/lib/ui_int.h"

#define SIM_MENU_APP (*(volatile uint32_t *)FW_MENU_APP)
#define SIM_ACTUATOR 0x61568D40u
#define SIM_TITLE_KIND 0x05FA0004u	/* stock title descriptor of the Eye-Fi row */
#define SIM_TITLE_TEXT 0x60D38D30u
#define SIM_ROWS 9

#define CHECK(n, cond)					\
	do {						\
		if (!(cond) && !sim.first_fail)		\
			sim.first_fail = (n);		\
	} while (0)

struct sim_state {
	uint32_t first_fail;
	/* gui_data_set */
	unsigned int sets;
	uint32_t id, size, notify;
	uint16_t page[FUI_SLOT_SIZE / 2];
	/* menu_app_default, the app's pop */
	unsigned int defaults, pops;
	uint32_t default_code, pop_reason;
	/* GUI objects */
	uint32_t app[0x90 / 4];
	uint32_t list_screen[2];
};

static struct sim_state sim;

volatile uint32_t mreg[4];
uint32_t sim_exit_code = 99;

/* --- options: Many (order 1, 8 rows), System (order 9, 1 row): two pages --- */

static const char *const sim_levels[] = { "err", "warn", "info", "debug" };

FENYKL_CFG_GROUP(sim_many, "Many", 1);
FENYKL_CFG_BOOL(m0, "m.0", "Row zero", &sim_many, 0);
FENYKL_CFG_BOOL(m1, "m.1", "M1", &sim_many, 1);
FENYKL_CFG_BOOL(m2, "m.2", "M2", &sim_many, 0);
FENYKL_CFG_BOOL(m3, "m.3", "M3", &sim_many, 0);
FENYKL_CFG_BOOL(m4, "m.4", "M4", &sim_many, 0);
FENYKL_CFG_BOOL(m5, "m.5", "M5", &sim_many, 0);
FENYKL_CFG_BOOL(m6, "m.6", "M6", &sim_many, 0);
FENYKL_CFG_BOOL(m7, "m.7", "M7", &sim_many, 0);
FENYKL_CFG_GROUP(sim_sys, "System", 9);
FENYKL_CFG_ENUM(level, "sim.level", "Log level", &sim_sys, 2, sim_levels);

/* --- mocks (fw.S forwards here; A0, A1, D0, D1 in mreg[0..3]) --- */

uint32_t c_psw_get(void)
{
	return 0;
}

uint32_t c_irq_disable_all(void)
{
	return 0;
}

uint32_t c_irq_mask_restore(void)
{
	return 0;
}

uint32_t c_irq_lock(void)
{
	return 0;
}

uint32_t c_irq_unlock(void)
{
	return 0;
}

uint32_t c_dcache_flush_all(void)
{
	return 0;
}

/* gui_data_set(buf@A0, id@D0, size@D1, notify@A1) */
uint32_t c_gui_data_set(void)
{
	sim.sets++;
	sim.id = mreg[2];
	sim.size = mreg[3];
	sim.notify = mreg[1];
	memcpy(sim.page, (const void *)mreg[0], sizeof(sim.page));
	return 1;
}

/* menu_app_default(app@A0, code@D0, param@D1): the stock default handler's menu-end codes */
uint32_t c_menu_app_default(void)
{
	sim.defaults++;
	sim.default_code = mreg[2];
	return mreg[2] == 0x9000 ? 0x9625 : 0;
}

/* the app's pop(app@A0, reason@D0): wanted depth - 1, re-show reason */
uint32_t c_app_pop(void)
{
	uint32_t app = mreg[0];

	sim.pops++;
	sim.pop_reason = mreg[2];
	*(volatile uint16_t *)(app + 4) -= 1;
	*(volatile uint32_t *)(app + 0xC) = mreg[2];
	return 0;
}

/* --- helpers --- */

static uint32_t rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

static uint16_t depth(void)
{
	return *(volatile uint16_t *)((uint32_t)sim.app + 6);
}

static uint32_t entry(unsigned int i, unsigned int w)
{
	return sim.app[(0x10 + 0x10 * i) / 4 + w];
}

/* Stock firmware words the page checks and replaces, a menu app with the Custom list on top. */
static void sim_stock(void)
{
	*(volatile uint32_t *)fenykl_slot_mode_cond_eyefi.address = fenykl_slot_mode_cond_eyefi.expected;
	*(volatile uint32_t *)fenykl_slot_eyefi_ok_fn.address = fenykl_slot_eyefi_ok_fn.expected;
	*(volatile uint32_t *)fenykl_slot_eyefi_right_fn.address = fenykl_slot_eyefi_right_fn.expected;
	*(volatile uint32_t *)(FW_MENU_APP_VTABLE + 4) = FW_MENU_APP_EVENT_FN;
	*(volatile uint32_t *)(FW_MENU_APP_VTABLE + 12) = FW_MENU_APP_POP_FN;
	*(volatile uint16_t *)FW_CUSTOM_ROW_EYEFI = 0x193;
	*(volatile uint32_t *)(FW_CUSTOM_ROW_EYEFI + 8) = SIM_TITLE_KIND;
	*(volatile uint32_t *)(FW_CUSTOM_ROW_EYEFI + 12) = SIM_TITLE_TEXT;

	sim.list_screen[0] = FW_MENU_SCREEN_VT_LIST0;
	sim.app[0] = FW_MENU_APP_VTABLE;
	*(volatile uint16_t *)((uint32_t)sim.app + 4) = 2;
	*(volatile uint16_t *)((uint32_t)sim.app + 6) = 2;
	sim.app[(0x10 + 0x10 * 2) / 4 + 1] = (uint32_t)sim.list_screen;
	sim.app[(0x10 + 0x10 * 2) / 4 + 2] = 0xF;

	/* the list's own slot: a head word, the K tab's marks and tab icons, a stock title in row 0 */
	for (unsigned int i = 0; i < FUI_SLOT_SIZE / 2; i++)
		((volatile uint16_t *)FW_GUI_DATA_SLOT38)[i] = 0x7700 + (i & 0xFF);
}

static uint16_t slot16(const uint16_t *s, uint32_t off)
{
	return s[off / 2];
}

static bool cell_is(const uint16_t *s, uint32_t off, const char *text)
{
	const uint16_t *t = s + (off + FUI_CELL_TEXT) / 2;

	if (s[off / 2] != FUI_KIND_INLINE || s[off / 2 + 1] || s[off / 2 + 2] || s[off / 2 + 3])
		return false;
	for (; *text; text++, t++)
		if (*t != (uint8_t)*text)
			return false;
	return *t == 0;
}

static bool cell_empty(const uint16_t *s, uint32_t off, uint32_t size)
{
	for (uint32_t i = 0; i < size / 2; i++)
		if (s[off / 2 + i])
			return false;
	return true;
}

/* The published page is page pg with n option rows, the cursor on row cur. */
static bool page_ok(unsigned int pg, unsigned int n, unsigned int cur)
{
	const uint16_t *s = sim.page;
	static const char *const titles[SIM_ROWS] = {
		"Row zero", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "Log level",
	};

	if (sim.id != FUI_SLOT_ID || sim.size != FUI_SLOT_SIZE || sim.notify != 1)
		return false;
	if (slot16(s, FUI_SCROLL) != 1 || slot16(s, FUI_PAGES) != 2 || slot16(s, FUI_PAGE) != pg)
		return false;
	/* tab icons and everything before the cursor arrays stay the list's own */
	if (slot16(s, FUI_MARK + 14) != 0x7700 + (FUI_MARK + 14) / 2)
		return false;
	for (unsigned int k = 0; k < FUI_PAGE_ROWS; k++) {
		unsigned int i = FUI_PAGE_ROWS * pg + k;
		uint32_t t = FUI_TITLES + k * FUI_CELL_SIZE, v = FUI_VALUES + k * FUI_CELL_SIZE;

		if (slot16(s, FUI_CURSOR + 2 * k) != (k == cur ? FUI_CURSOR_ROW : 0) ||
		    slot16(s, FUI_CURSOR_B + 2 * k) != 0)
			return false;
		if (!cell_empty(s, FUI_WIDE_A + k * FUI_WIDE_SIZE, FUI_WIDE_SIZE) ||
		    !cell_empty(s, FUI_WIDE_B + k * FUI_WIDE_SIZE, FUI_WIDE_SIZE))
			return false;
		if (k >= n) {
			if (slot16(s, FUI_MARK + 2 * k) != 0 || !cell_empty(s, t, FUI_CELL_SIZE) ||
			    !cell_empty(s, v, FUI_CELL_SIZE))
				return false;
			continue;
		}
		if (slot16(s, FUI_MARK + 2 * k) != FUI_MARK_ROW || !cell_is(s, t, titles[i]))
			return false;
	}
	return true;
}

static int row_ok(void)
{
	return fw_call_menu_row(rd32(fenykl_slot_eyefi_ok_fn.address), (void *)SIM_ACTUATOR);
}

/* An event to the top screen, as the app's event method sends it. */
static unsigned int event(unsigned int code, unsigned int param)
{
	uint32_t screen = entry(depth(), 1);

	return fw_call_menu_screen(rd32(rd32(screen)), (void *)screen, code, param, sim.app);
}

/* The app drops the top screen: delete, then the function it returns; depth - 1. */
static bool drop(void)
{
	uint32_t screen = entry(depth(), 1);
	void *free_fn = fw_call_menu_screen_delete(rd32(rd32(screen) + 4), (void *)screen);

	if (!free_fn)
		return false;
	fw_call_menu_screen_free((uint32_t)free_fn, (void *)screen);
	memset(&sim.app[(0x10 + 0x10 * depth()) / 4], 0, 0x10);
	*(volatile uint16_t *)((uint32_t)sim.app + 6) -= 1;
	return true;
}

/* --- checks --- */

static void check_install(void)
{
	sim_stock();
	*(volatile uint32_t *)(FW_CUSTOM_ROW_EYEFI + 8) = 0x05FA0005;
	CHECK(1, fenykl_ui_install() == FENYKL_UI_ESTOCK && !fui.installed);
	CHECK(2, rd32(fenykl_slot_eyefi_ok_fn.address) == 0 &&
		 rd32(fenykl_slot_mode_cond_eyefi.address) == fenykl_slot_mode_cond_eyefi.expected);
	/* a slot that holds something else: nothing stays replaced */
	sim_stock();
	*(volatile uint32_t *)fenykl_slot_mode_cond_eyefi.address = 0x60123456;
	CHECK(3, fenykl_ui_install() == FENYKL_UI_ESTOCK && !fui.installed);
	CHECK(4, rd32(fenykl_slot_eyefi_ok_fn.address) == 0 &&
		 rd32(fenykl_slot_eyefi_right_fn.address) == 0 &&
		 rd32(FW_CUSTOM_ROW_EYEFI + 8) == SIM_TITLE_KIND);
	sim_stock();
	CHECK(5, fenykl_ui_install() == 0 && fui.installed && fui.nopts == SIM_ROWS);
	CHECK(6, fenykl_ui_install() == FENYKL_UI_EBUSY);
	CHECK(7, rd32(fenykl_slot_eyefi_ok_fn.address) != 0 &&
		 rd32(fenykl_slot_eyefi_right_fn.address) != 0 &&
		 rd32(FW_CUSTOM_ROW_EYEFI + 8) == FUI_KIND_INLINE &&
		 !memcmp((const char *)rd32(FW_CUSTOM_ROW_EYEFI + 12), "Fenykl", 7));
	CHECK(8, fw_call_mode_cond(rd32(fenykl_slot_mode_cond_eyefi.address)) == 0);
}

static void check_open(void)
{
	/* RIGHT is swallowed; OK without a menu app or over another screen is refused, handled */
	CHECK(10, fw_call_menu_row(rd32(fenykl_slot_eyefi_right_fn.address), (void *)SIM_ACTUATOR) == 1 &&
		  depth() == 2);
	SIM_MENU_APP = 0;
	CHECK(11, row_ok() == 1 && depth() == 2 && fui.st.refused == 1);
	SIM_MENU_APP = (uint32_t)sim.app;
	sim.list_screen[0] = FW_MENU_SCREEN_VT_LIST0 + 4;
	CHECK(12, row_ok() == 1 && depth() == 2 && fui.st.refused == 2);
	sim.list_screen[0] = FW_MENU_SCREEN_VT_LIST5;

	CHECK(13, row_ok() == 1 && depth() == 3 && sim.app[1] == (3u << 16 | 3u));
	CHECK(14, entry(3, 0) == 0xF0 && entry(3, 1) == (uint32_t)&fui.screen && entry(3, 2) == 0xF0 &&
		  fui.open && sim.sets == 0);
	CHECK(15, row_ok() == 1 && depth() == 3 && fui.st.refused == 3);

	/* the first event (the OK release) replaces the list's model; releases do nothing else */
	CHECK(16, event(FUI_KEY_OK + 1, 0) == 0 && sim.sets == 1 && page_ok(0, 7, 0));
	CHECK(17, cell_is(sim.page, FUI_VALUES, "Off") &&
		  cell_is(sim.page, FUI_VALUES + FUI_CELL_SIZE, "On"));
	CHECK(18, event(FUI_KEY_DOWN + 1, 0) == 0 && sim.sets == 1);
}

static void check_keys(void)
{
	CHECK(20, event(FUI_KEY_OK, 0) == 0 && m0 == 1 && sim.sets == 2 &&
		  cell_is(sim.page, FUI_VALUES, "On"));
	CHECK(21, event(FUI_KEY_UP, 0) == 0 && fui.cursor == 8 && page_ok(1, 2, 1));
	CHECK(22, cell_is(sim.page, FUI_TITLES + FUI_CELL_SIZE, "Log level") &&
		  cell_is(sim.page, FUI_VALUES + FUI_CELL_SIZE, "info"));
	event(FUI_KEY_RIGHT, 0);
	CHECK(23, level == 3 && cell_is(sim.page, FUI_VALUES + FUI_CELL_SIZE, "debug"));
	/* a rear-dial step of +1 moves the cursor up, as on the E-PL3 */
	CHECK(24, event(FUI_CTRL_RING, 1) == 0 && fui.cursor == 7 && page_ok(1, 2, 0));
	CHECK(25, event(0x971D, 2) == 0 && sim.sets == 6 && sim.defaults == 0);
	/* anything else goes to the app's default handler */
	CHECK(26, event(0x9601, 0) == 0 && sim.defaults == 1 && sim.default_code == 0x9601);
}

static void check_close(void)
{
	unsigned int sets = sim.sets;

	/* MENU: the app's pop with reason 2; the app deletes the screen afterwards */
	CHECK(30, event(FUI_KEY_MENU, 0) == 0 && sim.pops == 1 && sim.pop_reason == 2 &&
		  sim.app[1] == (3u << 16 | 2u) && sim.sets == sets);
	CHECK(31, drop() && !fui.open && fui.st.exits == 1 && depth() == 2);

	/* reopened; half press ends the menu through the default handler */
	*(volatile uint16_t *)((uint32_t)sim.app + 4) = 2;
	CHECK(32, row_ok() == 1 && depth() == 3 && fui.open);
	CHECK(33, event(0x9000, 0) == 0x9625 && sim.default_code == 0x9000 && sim.sets == sets + 1);
	CHECK(34, event(0xAF00, 0) == 0 && sim.default_code == 0xAF00);
	CHECK(35, drop() && !fui.open && fui.st.exits == 2);
	CHECK(36, fui.st.cpu1_actions == 0 && fui.st.publish_fail == 0 && fui.st.entries == 2);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	SIM_MENU_APP = (uint32_t)sim.app;
	check_install();
	check_open();
	check_keys();
	check_close();
	sim_exit_code = sim.first_fail ? 1 + sim.first_fail : 0;
}
