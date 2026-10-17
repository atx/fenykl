/*
 * Settings page (sdk/lib/ui.c) in the GDB simulator: the flat option list (group order, hidden
 * options, more than 20 rejected), the list slot 0xF for 1, 7, 8 and 20 rows (count, visible, page,
 * row states, kinds, borrowed title ids), cursor wrap and page flips, BOOL/ENUM/INT editing and
 * the value text with edit arrows and clamping, the "not saved" header (the real cfg library
 * saving to the mock card of tests/sim/log/fw.S and cfg/fw.S), and the borrowed lang ids on a fake
 * language table: lent at entry, given back on every way out (MENU, system event, exit action,
 * the tick's stray check). gui_data_set is mocked (fw.S here) and records the published slot.
 * plugin_main sets sim_exit_code to 0 or 1 + the number of the first failed check.
 */
#include <fenykl.h>
#include "../../../sdk/lib/ui_int.h"

#define SIM_FAT_MOUNTED (*(volatile uint16_t *)0x6059C978u)
#define SIM_LANG_CUR (*(volatile uint32_t *)0x605FC9C0u)
#define SIM_GUI_SUPER (*(volatile uint8_t *)0x60702278u)
#define SIM_GUI_APP (*(volatile uint32_t *)0x6034E4D8u)
#define SIM_MENU_FLAGS (*(volatile uint32_t *)0x603530C8u)
#define SIM_PSW_IE 0x800u
#define SIM_LANG_COUNT 1341
#define SIM_STRINGS 64
#define SIM_MENULIST_VTABLE 0x6F150894u

#define CHECK(n, cond)					\
	do {						\
		if (!(cond) && !sim.first_fail)		\
			sim.first_fail = (n);		\
	} while (0)

struct sim_lang {
	uint32_t arr, end;
	uint16_t strings[SIM_STRINGS];
	uint32_t ptr[SIM_LANG_COUNT + 1];
};

struct sim_state {
	uint32_t first_fail;
	bool write_fails;
	/* gui_data_set */
	unsigned int sets;
	uint32_t id, size, notify;
	struct fui_slot slot;
	/* GUI objects */
	uint32_t app[0x1A0 / 4];
	uint32_t panel[4];
	uint32_t flags[8];
	struct sim_lang lang;
	/* synthetic option tables */
	int32_t values[FENYKL_UI_OPTS_MAX + 1];
	struct fenykl_cfg_opt opts[FENYKL_UI_OPTS_MAX + 1];
};

static struct sim_state sim;

volatile uint32_t mreg[5];
volatile uint32_t mock_psw = SIM_PSW_IE, mock_masks;
volatile uint32_t mock_usb_key = 1;	/* 1 = no USB cable */
volatile uint32_t mock_purges;
uint32_t sim_exit_code = 99;

/* --- the registered option table: Beta (order 1), Alpha and Gamma (order 2, link order), a
   hidden one; 15 rows over three screens --- */

static const char *const sim_xyz[] = { "x", "y", "z" };

FENYKL_CFG_GROUP(sim_alpha, "Alpha", 2);
FENYKL_CFG_BOOL(a0, "a.0", "A0", &sim_alpha, 0);
FENYKL_CFG_BOOL(a1, "a.1", "A1", &sim_alpha, 0);
FENYKL_CFG_BOOL(a2, "a.2", "A2", &sim_alpha, 0);
FENYKL_CFG_BOOL(a3, "a.3", "A3", &sim_alpha, 0);
FENYKL_CFG_BOOL(a4, "a.4", "A4", &sim_alpha, 0);
FENYKL_CFG_BOOL(a5, "a.5", "A5", &sim_alpha, 0);
FENYKL_CFG_BOOL(a6, "a.6", "A6", &sim_alpha, 0);
FENYKL_CFG_BOOL(a7, "a.7", "A7", &sim_alpha, 0);
FENYKL_CFG_BOOL(a8, "a.8", "Fifteen chars a", &sim_alpha, 0);
FENYKL_CFG_INT(hidden, "hidden", "Hidden", NULL, 0, 0, 9, 1);
FENYKL_CFG_GROUP(sim_beta, "Beta", 1);
FENYKL_CFG_BOOL(b_bool, "b.bool", "Bool", &sim_beta, 0);
FENYKL_CFG_ENUM(b_enum, "b.enum", "Enum", &sim_beta, 0, sim_xyz);
FENYKL_CFG_INT(b_int, "b.int", "Int", &sim_beta, 10, 0, 20, 5);
FENYKL_CFG_INT(b_neg, "b.neg", "Neg", &sim_beta, 0, -3, 3, 2);
FENYKL_CFG_INT(b_wide, "b.wide", "Wide", &sim_beta, 0x7FFFFFF8, 0, 0x7FFFFFFF, 1);
FENYKL_CFG_GROUP(sim_gamma, "Gamma", 2);
FENYKL_CFG_BOOL(g0, "g.0", "G0", &sim_gamma, 1);

#define SIM_ROWS 15

/* --- mocks (tests/sim/log/fw.S, cfg/fw.S, ui/fw.S forward here) --- */

/* gui_data_set(buf@A0, id@D0, size@D1, notify@A1) */
uint32_t c_gui_data_set(void)
{
	sim.sets++;
	sim.id = mreg[2];
	sim.size = mreg[3];
	sim.notify = mreg[1];
	memcpy(&sim.slot, (const void *)mreg[0], sizeof(sim.slot));
	return 1;
}

static bool sim_streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

/* No file exists; "w" opens a sink. */
uint32_t c_rpc_fopen(void)
{
	return sim_streq((const char *)mreg[1], "w") ? 1 : 0;
}

uint32_t c_rpc_fclose(void)
{
	return 0;
}

uint32_t c_rpc_fread(void)
{
	return 0;
}

/* fwrite(buf@A0, size@D0, n@D1, fp@A1) */
uint32_t c_rpc_fwrite(void)
{
	return sim.write_fails ? 0 : mreg[2] * mreg[3];
}

uint32_t c_rpc_fremove(void)
{
	return (uint32_t)-1;
}

uint32_t c_sprintf(void)
{
	return 0;
}

uint32_t c_malloc(void)
{
	return 0;
}

uint32_t c_free(void)
{
	return 0;
}

uint32_t c_OlyTaskSleep(void)
{
	return 0;
}

/* --- helpers --- */

static enum fui_result key(unsigned int code)
{
	return fui_key(code);
}

static void keys(unsigned int code, unsigned int n)
{
	while (n--)
		fui_key(code);
}

static bool u16eq(const uint16_t *a, const char *b)
{
	while (*b && *a == (uint8_t)*b)
		a++, b++;
	return *a == (uint8_t)*b;
}

/* The language table as the firmware has it: header, strings, pointer array, 0. String i is
   strings[i % SIM_STRINGS]. */
static void sim_lang_init(void)
{
	struct sim_lang *l = &sim.lang;

	for (unsigned int i = 0; i < SIM_LANG_COUNT; i++)
		l->ptr[i] = (uint32_t)&l->strings[i % SIM_STRINGS];
	l->ptr[SIM_LANG_COUNT] = 0;
	l->arr = (uint32_t)l->ptr;
	l->end = (uint32_t)&l->ptr[SIM_LANG_COUNT];
	SIM_LANG_CUR = (uint32_t)l;
}

static uint32_t lang(unsigned int id)
{
	return sim.lang.ptr[id];
}

static bool lang_stock(void)
{
	for (unsigned int k = 0; k < FUI_BORROWED; k++) {
		unsigned int id = fui_borrowed_ids[k];

		if (lang(id) != (uint32_t)&sim.lang.strings[id % SIM_STRINGS])
			return false;
	}
	return true;
}

static bool lang_lent(void)
{
	for (unsigned int k = 0; k < FENYKL_UI_OPTS_MAX; k++) {
		uint32_t want = k < fui.nopts ? (uint32_t)fui.labels[k] :
			(uint32_t)&sim.lang.strings[fui_borrowed_ids[k] % SIM_STRINGS];

		if (lang(fui_borrowed_ids[k]) != want)
			return false;
	}
	return u16eq((const uint16_t *)lang(fui_borrowed_ids[FUI_TITLE_UNSAVED]),
		     "Fenykl: not saved");
}

/* Every slot invariant for n rows with the cursor on `cursor`. */
static bool slot_ok(unsigned int n, unsigned int cursor, unsigned int page, unsigned int visible)
{
	const struct fui_slot *s = &fui.slot;
	unsigned int selected = 0;

	if (s->count != (int8_t)n || s->page != page || s->visible != (int8_t)visible ||
	    s->title != FUI_TITLE || s->pad != 0)
		return false;
	for (unsigned int i = 0; i < FENYKL_UI_OPTS_MAX; i++) {
		const struct fui_row *r = &s->row[i];

		if (i >= n) {
			if (r->state || r->kind || r->title || r->help || r->v[0])
				return false;
			continue;
		}
		if (r->kind != FUI_KIND_TEXT || r->title != fui_borrowed_ids[i])
			return false;
		if (r->state == FUI_ROW_SELECTED)
			selected++;
		else if (r->state != FUI_ROW_NORMAL)
			return false;
		if (r->help != (i == cursor ? FUI_HELP_SELECTED : FUI_HELP))
			return false;
	}
	return selected == 1 && s->row[cursor].state == FUI_ROW_SELECTED;
}

/* n synthetic BOOL options, value i & 1, in one group. */
static const struct fenykl_cfg_opt *sim_table(unsigned int n)
{
	static const struct fenykl_cfg_group g = { .label = "G", .order = 1 };

	for (unsigned int i = 0; i < n; i++) {
		sim.values[i] = (int32_t)(i & 1);
		sim.opts[i] = (struct fenykl_cfg_opt){ .name = "s", .label = "S", .group = &g,
						       .value = &sim.values[i], .max = 1,
						       .type = FENYKL_CFG_BOOL };
	}
	return sim.opts;
}

/* --- checks --- */

static void check_collect(void)
{
	const struct fenykl_cfg_opt *t;

	CHECK(1, fenykl_cfg_init() == FENYKL_CFG_ENOFILE);
	CHECK(2, fui_collect(fenykl_cfg_start, fenykl_cfg_end) == 0 && fui.nopts == SIM_ROWS);
	CHECK(3, fui.opts[0] == &fenykl_cfg_b_bool && fui.opts[4] == &fenykl_cfg_b_wide &&
		 fui.opts[5] == &fenykl_cfg_a0 && fui.opts[13] == &fenykl_cfg_a8 &&
		 fui.opts[14] == &fenykl_cfg_g0 && fui.cursor == 0);
	t = sim_table(FENYKL_UI_OPTS_MAX + 1);
	CHECK(4, fui_collect(t, t + FENYKL_UI_OPTS_MAX + 1) == FENYKL_UI_EOPTS);
	CHECK(5, fui_collect(t, t) == FENYKL_UI_EOPTS);
	sim.opts[0].group = NULL;
	CHECK(6, fui_collect(t, t + 1) == FENYKL_UI_EOPTS);
	CHECK(7, fui_collect(t, t + FENYKL_UI_OPTS_MAX + 1) == 0 &&
		 fui.nopts == FENYKL_UI_OPTS_MAX && fui.opts[0] == &sim.opts[1]);
}

/* Slots for N = 1, 7, 8, 20 at the first and last row. */
static void check_slot_sizes(void)
{
	static const uint8_t ns[] = { 1, 7, 8, FENYKL_UI_OPTS_MAX };

	for (unsigned int j = 0; j < sizeof(ns); j++) {
		unsigned int n = ns[j], last = n - 1, lp = last / FUI_PAGE_ROWS;
		const struct fenykl_cfg_opt *t = sim_table(n);

		CHECK(10 + j, fui_collect(t, t + n) == 0 && fui.nopts == n);
		fui_build_slot();
		CHECK(14 + j, slot_ok(n, 0, 0, n < 7 ? n : 7));
		CHECK(18 + j, u16eq(fui.slot.row[0].v, "Off") && u16eq(fui.slot.row[last].v,
									last & 1 ? "On" : "Off"));
		key(FUI_KEY_UP);
		fui_build_slot();
		CHECK(22 + j, fui.cursor == last &&
			      slot_ok(n, last, lp, n - lp * 7 < 7 ? n - lp * 7 : 7));
	}
	/* N = 20: page flips at 6/7 and 13/14, wrap both ways */
	keys(FUI_KEY_DOWN, 7);
	fui_build_slot();
	CHECK(26, fui.cursor == 6 && slot_ok(20, 6, 0, 7));
	key(FUI_KEY_DOWN);
	fui_build_slot();
	CHECK(27, fui.cursor == 7 && slot_ok(20, 7, 1, 7));
	key(FUI_KEY_UP);
	fui_build_slot();
	CHECK(28, slot_ok(20, 6, 0, 7));
	keys(FUI_KEY_DOWN, 8);
	fui_build_slot();
	CHECK(29, slot_ok(20, 14, 2, 6));
	key(FUI_KEY_UP);
	key(FUI_DIAL_DOWN);
	fui_build_slot();
	CHECK(30, slot_ok(20, 12, 1, 7));
	key(FUI_DIAL_UP);
	key(FUI_DIAL_UP);
	fui_build_slot();
	CHECK(31, slot_ok(20, 14, 2, 6));
}

static void check_value_text(void)
{
	static const char *const names[] = { "a-name-of-twenty-chr", "b" };
	const struct fenykl_cfg_opt *t = sim_table(1);
	uint16_t v[FUI_VALUE_LEN];

	sim.opts[0].type = FENYKL_CFG_ENUM;
	sim.opts[0].names = names;
	sim.values[0] = 0;
	fui_collect(t, t + 1);
	fui_value_text(t, v);
	CHECK(35, u16eq(v, "a-name-of-twen"));
	sim.opts[0].type = FENYKL_CFG_INT;
	sim.opts[0].min = INT32_MIN;
	sim.opts[0].max = INT32_MAX;
	sim.values[0] = INT32_MIN;
	fui_value_text(t, v);
	CHECK(36, u16eq(v, "-2147483648"));
	key(FUI_KEY_OK);
	fui_value_text(t, v);
	CHECK(37, fui.edit && v[0] == FUI_ARROW_LEFT && v[1] == '-' && v[11] == '8' &&
		  v[12] == FUI_ARROW_RIGHT && v[13] == 0);
	fui.edit = false;
}

static void check_edit(void)
{
	fui_collect(fenykl_cfg_start, fenykl_cfg_end);
	CHECK(40, key(FUI_KEY_OK) == FUI_REDRAW && b_bool == 1 && fenykl_cfg_dirty());
	key(FUI_KEY_RIGHT);
	key(FUI_KEY_LEFT);
	CHECK(41, b_bool == 1);
	key(FUI_KEY_DOWN);
	key(FUI_KEY_OK);
	CHECK(42, fui.cursor == 1 && b_enum == 1);
	key(FUI_KEY_RIGHT);
	key(FUI_KEY_RIGHT);
	CHECK(43, b_enum == 0);
	key(FUI_KEY_LEFT);
	CHECK(44, b_enum == 2 && key(0x9014) == FUI_NONE);

	key(FUI_KEY_DOWN);
	CHECK(45, key(FUI_KEY_LEFT) == FUI_NONE && !fui.edit && b_int == 10);
	key(FUI_KEY_RIGHT);
	CHECK(46, fui.edit && fui.edit_val == 10);
	keys(FUI_KEY_RIGHT, 3);
	fui_build_slot();
	CHECK(47, fui.edit_val == 20 && b_int == 10 && fui.slot.row[2].v[0] == FUI_ARROW_LEFT &&
		  fui.slot.row[2].v[1] == '2' && fui.slot.row[2].v[3] == FUI_ARROW_RIGHT &&
		  fui.slot.row[1].v[0] == 'z');
	CHECK(48, key(FUI_KEY_UP) == FUI_NONE && fui.cursor == 2);
	key(FUI_DIAL_DOWN);
	CHECK(49, fui.edit_val == 0);
	CHECK(50, key(FUI_KEY_MENU) == FUI_REDRAW && !fui.edit && b_int == 10);
	key(FUI_KEY_OK);
	key(FUI_KEY_LEFT);
	key(FUI_KEY_OK);
	fui_build_slot();
	CHECK(51, !fui.edit && b_int == 5 && u16eq(fui.slot.row[2].v, "5"));

	key(FUI_KEY_DOWN);
	key(FUI_KEY_OK);
	keys(FUI_KEY_RIGHT, 2);
	CHECK(52, fui.edit_val == 3);
	key(FUI_KEY_LEFT);
	key(FUI_KEY_OK);
	CHECK(53, b_neg == 1);

	key(FUI_KEY_DOWN);
	key(FUI_KEY_OK);
	key(FUI_DIAL_UP);
	CHECK(54, fui.edit_val == 0x7FFFFFFF);
	key(FUI_KEY_OK);
	CHECK(55, b_wide == 0x7FFFFFFF && fui.st.set_fail == 0);
	CHECK(56, key(FUI_KEY_MENU) == FUI_LEAVE);
}

/* Entry lends, every way out gives back. */
static void check_lang(void)
{
	fui_collect(fenykl_cfg_start, fenykl_cfg_end);
	sim_lang_init();
	SIM_GUI_SUPER = 0x0D;

	sim.sets = 0;
	CHECK(60, fui_on_entry(0, 0x0D01, 0, 0) == 0 && lang_lent() && fui.lent_arr == sim.lang.arr);
	CHECK(61, u16eq(fui.labels[0], "Bool") && u16eq(fui.labels[13], "Fifteen chars a"));
	CHECK(62, sim.sets == 1 && sim.id == FUI_SLOT_ID && sim.size == sizeof(struct fui_slot) &&
		  sim.notify == 1 && !memcmp(&sim.slot, &fui.slot, sizeof(sim.slot)));
	CHECK(63, sim.slot.count == SIM_ROWS && sim.slot.row[14].title == fui_borrowed_ids[14]);
	fui_on_key(0, FUI_KEY_DOWN, 0, 0);
	CHECK(64, sim.sets == 2 && sim.slot.row[1].state == FUI_ROW_SELECTED);
	fui_on_key(0, 0x9014, 0, 0);
	CHECK(65, sim.sets == 2 && lang_lent());

	/* MENU */
	SIM_MENU_FLAGS = (uint32_t)sim.flags;
	CHECK(66, fui_on_key(0, FUI_KEY_MENU, 0, 0) == 0x0304 && lang_stock() && !fui.lent_arr);
	CHECK(67, sim.flags[5] == 1 && fui.st.backs == 1);
	fui_on_exit(0, 0x0304, 0, 0);
	CHECK(68, lang_stock() && fui.st.returns == 1);

	/* a system event (no stock action in the empty event map: back to the list) */
	fui_on_entry(0, 0x0D01, 0, 0);
	SIM_MENU_FLAGS = 0;
	CHECK(70, lang_lent() && fui_on_sys(0, 0x9010, 0, 0) == 0x0304 && lang_stock());
	CHECK(71, fui.st.flag_missing == 1);
	fui_on_exit(0, 0x0304, 0, 0);

	/* the exit action alone, with an edit pending */
	fui_on_entry(0, 0x0D01, 0, 0);
	fui.cursor = 2;
	fui_on_key(0, FUI_KEY_OK, 0, 0);
	fui_on_key(0, FUI_KEY_RIGHT, 0, 0);
	CHECK(72, fui.edit && lang_lent());
	fui_on_exit(0, 0x0D01, 0, 0);
	CHECK(73, !fui.edit && b_int == 5 && lang_stock() && fui.st.returns == 3);

	/* no exit action at all: the tick gives the ids back after three ticks outside 0x0D */
	fui.installed = true;
	fui_on_entry(0, 0x0D01, 0, 0);
	fenykl_ui_tick();
	CHECK(74, lang_lent());
	SIM_GUI_SUPER = 0x03;
	fenykl_ui_tick();
	fenykl_ui_tick();
	CHECK(75, lang_lent());
	fenykl_ui_tick();
	CHECK(76, lang_stock() && fui.st.lang_stray == 1);
	fui_on_exit(0, 0x0304, 0, 0);
	CHECK(77, lang_stock() && fui.st.returns == 4 && fui.st.cpu1_actions == 0);
}

/* The header says "not saved" while a change cannot reach the card. */
static void check_unsaved(void)
{
	SIM_GUI_SUPER = 0x0D;
	fenykl_cfg_save();
	fui_on_entry(0, 0x0D01, 0, 0);
	fui.cursor = 0;
	SIM_FAT_MOUNTED = 0;
	fui_on_key(0, FUI_KEY_OK, 0, 0);
	fenykl_ui_tick();
	CHECK(80, fui.unsaved && sim.slot.title == FUI_TITLE);
	fui_on_key(0, FUI_KEY_DOWN, 0, 0);
	CHECK(81, sim.slot.title == fui_borrowed_ids[FUI_TITLE_UNSAVED]);
	SIM_FAT_MOUNTED = 1;
	sim.write_fails = true;
	fenykl_ui_tick();
	CHECK(82, !fui.unsaved);
	CHECK(83, fenykl_cfg_save() == FENYKL_CFG_EIO);
	fenykl_ui_tick();
	CHECK(84, fui.unsaved);
	sim.write_fails = false;
	CHECK(85, fenykl_cfg_save() == 0);
	fenykl_ui_tick();
	fui_on_key(0, FUI_KEY_UP, 0, 0);
	CHECK(86, !fui.unsaved && sim.slot.title == FUI_TITLE);
	fui_on_exit(0, 0x0304, 0, 0);
	CHECK(87, lang_stock());
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	SIM_FAT_MOUNTED = 1;
	sim.panel[0] = SIM_MENULIST_VTABLE;
	sim.app[0x19C / 4] = (uint32_t)sim.panel;
	SIM_GUI_APP = (uint32_t)sim.app;
	check_collect();
	check_slot_sizes();
	check_value_text();
	check_edit();
	check_lang();
	check_unsaved();
	sim_exit_code = sim.first_fail ? 1 + sim.first_fail : 0;
}
