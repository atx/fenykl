/*
 * Button functions on the E-M10 II (sdk/docs/fnfunc.md): the plugin function is function ID 0x3BF,
 * which every mode rule of Fn1, Fn2, Fn3, REC and L-Fn already allows wherever Preview is allowed,
 * and which nothing else on this body knows: no store list, no label, no dispatch entry. A press
 * of a button holding it resolves to the firmware's "none" function, like Off.
 *
 * Install, data only: the five store records get lists with 0x3BF appended, the function
 * selector's 15 key entries and the five button rows get label tables with a kind-5 entry for it.
 * Every stock word is checked before it is swapped (pointer before count, labels before lists),
 * and every copy is a strict extension of its stock table, so a reader between two swaps sees
 * consistent tables. The copies live in .shared lines; the firmware gets their uncached alias.
 *
 * Press: an event-layer handler that passes every event; it calls the plugin back on the press of
 * a button whose setting holds 0x3BF. The event layer is installed only once a button holds it.
 */
#include <event.h>
#include <fnfunc.h>
#include <body.h>

#define FFN_ID 0x3BFu
#define FFN_KIND_INLINE 5u		/* {u32 value, u16 kind 5, u16 0, u32 ASCII text} */
#define FFN_SECTIONS 0x0Eu		/* section_loaded bits of sec2, sec3, sec4 */
#define FFN_NONE 0xFFFFFFFFu		/* ffn_setting: no property object */

#define FFN_LIST_N 28u			/* Fn1, Fn2, Fn3, REC: the same 28 IDs */
#define FFN_LIST_LFN_N 29u		/* L-Fn: AF Stop, then the 28 */
#define FFN_REC_FLAGS 0x0080u
#define FFN_REC_KIND 0x00000500u
#define FFN_REC_SIZE(n) (0x00020000u | 2u * (n))	/* u16 bytes, u16 width 2 */
#define FFN_HASH_MAX 64u
#define FFN_OVERRIDE_ID(i) (FW_SETTING_COUNT_SLOTS + 0x4Cu * (i) + 0x44u)

#define FFN_KEYS 15u
#define FFN_KEY_STRIDE 0x1Cu
#define FFN_NAMES_N 37u
#define FFN_ICONS_N 21u
#define FFN_HELP_N 37u
#define FFN_ROW_STRIDE 0x24u
#define FFN_CELLS_N 38u

/* Setting 0x217: under some of its values the dispatch may replace REC's and Fn2's stored
   function with one of its own (struct ffn_button skip_modes). */
#define FFN_MODE_SETTING 0x217u
#define FFN_MODE_PROP 0xE1E1u
#define FFN_MODE_ELEM 0u

#define FFN_PICKER_INIT_FN 0x609EE672u	/* the button picker's init */

#define FFN_NWORDS (4u * FFN_KEYS + 4u * FENYKL_FNFUNC_BUTTONS)
#define FFN_YIELD_WORDS 16u	/* swaps between two yields: each flushes the whole D-cache twice */

struct ffn_cell {
	uint32_t value;
	uint16_t kind, id;
	uint32_t aux;
};

_Static_assert(sizeof(struct ffn_cell) == 12, "label entry layout");

struct ffn_shared {
	uint16_t list[FFN_LIST_N + 1u];
	uint16_t list_lfn[FFN_LIST_LFN_N + 1u];
	struct ffn_cell names[FFN_NAMES_N + 1u];
	struct ffn_cell help[FFN_HELP_N + 1u];
	struct ffn_cell cells[FFN_CELLS_N + 1u];
};

struct ffn_button {
	const char *name;
	uint16_t code;		/* key code of its press 0x0200cccc */
	uint16_t setting;
	uint16_t prop, elem;	/* the settings map entry of the setting */
	uint32_t rec, list;	/* store record and its stock list */
	uint8_t n;		/* stock list entries */
	uint8_t row;		/* index in button_rows */
	uint8_t init;		/* index in button_picker_init */
	/* bit v: presses skipped while setting 0x217 is v, where the dispatch overrides the stored
	   function when its object check passes (else it dispatches Off: that press is lost) */
	uint8_t skip_modes;
};

struct ffn_word {
	uint32_t addr, stock, ours;
};

struct ffn_expect {
	uint32_t addr, value;
};

struct ffn_state {
	const struct fenykl_fnfunc *f;
	bool installed, listening, failed;
	unsigned int nwords, done;
	struct ffn_word w[FFN_NWORDS];
};

FENYKL_LOG_MODULE(fenykl_fnfunc_log, "fnfunc", FENYKL_LOG_INFO);

static FENYKL_SHARED struct ffn_shared ffn_sh;
static struct ffn_state ffn;

/* Key codes: Fn1, Fn2, Fn3 and REC seen through the event hook on the camera; L-Fn 0x2202 static
   only (a button in the key map), not seen on the camera. */
static const struct ffn_button ffn_buttons[FENYKL_FNFUNC_BUTTONS] = {
	[FENYKL_FNFUNC_FN1] = { "Fn1", 0x000B, 0x2F, 0xECC2, 0, FW_SETTING_REC_FN1, 0x6038EEB4u,
				FFN_LIST_N, 0, 0, 0 },
	[FENYKL_FNFUNC_FN2] = { "Fn2", 0x1209, 0x31, 0xECC2, 2, FW_SETTING_REC_FN2, 0x6038EF24u,
				FFN_LIST_N, 1, 1, 1u << 5 | 1u << 6 },
	[FENYKL_FNFUNC_FN3] = { "Fn3", 0x1224, 0x23B, 0xECC2, 4, FW_SETTING_REC_FN3, 0x6038FB40u,
				FFN_LIST_N, 3, 3, 0 },
	[FENYKL_FNFUNC_REC] = { "REC", 0x120A, 0x30, 0xECC2, 1, FW_SETTING_REC_REC, 0x6038EEECu,
				FFN_LIST_N, 2, 2, 1u << 3 | 1u << 5 | 1u << 6 },
	[FENYKL_FNFUNC_LFN] = { "L-Fn", 0x2202, 0x38, 0xECE2, 0, FW_SETTING_REC_LFN, 0x6038EF80u,
				FFN_LIST_LFN_N, 7, 10, 0 },
};

/* idx of each key entry, in table order */
static const uint8_t ffn_key_idx[FFN_KEYS] = {
	0, 1, 6, 7, 8, 9, 2, 0xA, 0xB, 0xC, 3, 4, 0xD, 0xE, 5,
};

/* first and last entries {value, kind | id << 16} of the label tables */
static const struct ffn_expect ffn_labels[] = {
	{ FW_FUNC_NAMES, 0x7D9 }, { FW_FUNC_NAMES + 4u, 0x058D0004u },
	{ FW_FUNC_NAMES + 12u * (FFN_NAMES_N - 1u), 0x306 },
	{ FW_FUNC_NAMES + 12u * FFN_NAMES_N - 8u, 0x0A5D0004u },
	{ FW_FUNC_HELP, 0x7D9 }, { FW_FUNC_HELP + 4u, 0x09590004u },
	{ FW_FUNC_HELP + 12u * (FFN_HELP_N - 1u), 0x306 },
	{ FW_FUNC_HELP + 12u * FFN_HELP_N - 8u, 0x0A580004u },
	{ FW_BUTTON_CELLS, 0x7D9 }, { FW_BUTTON_CELLS + 4u, 0x058D0004u },
	{ FW_BUTTON_CELLS + 12u * (FFN_CELLS_N - 1u), 0x306 },
	{ FW_BUTTON_CELLS + 12u * FFN_CELLS_N - 8u, 0x10290003u },
};

#define FFN_N(a) (sizeof(a) / sizeof((a)[0]))

static volatile struct ffn_shared *ffn_shared(void)
{
	return FENYKL_SHARED_PTR(ffn_sh);
}

static uint32_t ffn_row(const struct ffn_button *b)
{
	return FW_BUTTON_ROWS + FFN_ROW_STRIDE * b->row;
}

/* A setting's value read like the firmware's getter: element elem of prop's object, of the width
   prop & 7 gives (4: u32, 2: u16, else u8). FFN_NONE if the object is not there. */
static uint32_t ffn_setting(unsigned int prop, unsigned int elem)
{
	unsigned int kind = prop & 7u, width = kind == 4 ? 4 : kind == 2 ? 2 : 1;
	uint32_t obj = fbody_prop_obj(prop), a = obj + width * elem;

	if (!obj || !fenykl_dram_ptr(a, width))
		return FFN_NONE;
	if (width == 4)
		return fenykl_rd32(a);
	return width == 2 ? fenykl_rd16(a) : fenykl_rd8(a);
}

static bool ffn_held(const struct ffn_button *b)
{
	uint32_t mode;

	if (ffn_setting(b->prop, b->elem) != FFN_ID)
		return false;
	if (!b->skip_modes)
		return true;
	mode = ffn_setting(FFN_MODE_PROP, FFN_MODE_ELEM);
	return mode >= 8 || !(b->skip_modes >> mode & 1u);
}

/* --- press --- */

static enum fenykl_event_action ffn_on_event(struct fenykl_event *ev, void *arg)
{
	uint32_t code = ev->event & FENYKL_EVENT_CODE_MASK;

	(void)arg;
	if ((ev->event & FENYKL_EVENT_CLASS_MASK) != FENYKL_EVENT_KEY_PRESS)
		return FENYKL_EVENT_PASS;
	for (unsigned int i = 0; i < FENYKL_FNFUNC_BUTTONS; i++) {
		if (ffn_buttons[i].code != code)
			continue;
		if (ffn_held(&ffn_buttons[i]))
			ffn.f->press((enum fenykl_fnfunc_button)i, ffn.f->arg);
		break;
	}
	return FENYKL_EVENT_PASS;
}

static struct fenykl_event_handler ffn_handler = {
	.name = "fnfunc", .fn = ffn_on_event, .prio = 0, .enabled = true,
};

/* --- checks --- */

static bool ffn_expect_words(const char *what, const struct ffn_expect *e, unsigned int n)
{
	bool ok = true;

	for (unsigned int i = 0; i < n; i++) {
		uint32_t v = fenykl_rd32(e[i].addr);

		if (v == e[i].value)
			continue;
		fenykl_log_err(&fenykl_fnfunc_log, "%s: %08x holds %08x, want %08x", what, e[i].addr,
			       v, e[i].value);
		ok = false;
	}
	return ok;
}

/* The record the firmware's hash lookup finds for id: the first node {u32 record, u32 next} of
   bucket id & 0x7F whose record's u16 +0 is id. 0 if none or a pointer is not DRAM. */
static uint32_t ffn_hash_find(unsigned int id)
{
	uint32_t n = fenykl_rd32(FW_SETTING_REC_HASH + 4u * (id & 0x7Fu));

	for (unsigned int i = 0; n && i < FFN_HASH_MAX; i++) {
		uint32_t rec;

		if (!fenykl_dram_ptr(n, 4))
			return 0;
		rec = fenykl_rd32(n);
		if (!fenykl_dram_ptr(rec, 4))
			return 0;
		if (fenykl_rd16(rec) == id)
			return rec;
		n = fenykl_rd32(n + 4u);
	}
	return 0;
}

static bool ffn_check_button(const struct ffn_button *b)
{
	uint32_t row = ffn_row(b), first = FW_SETTING_REC_FN1;
	const struct ffn_expect e[] = {
		{ FW_SETTING_MAP + 4u * b->setting, (uint32_t)b->elem << 16 | b->prop },
		{ b->rec, (uint32_t)FFN_REC_FLAGS << 16 | b->setting }, { b->rec + 4u, b->list },
		{ b->rec + 8u, 0 }, { b->rec + 12u, FFN_REC_SIZE(b->n) },
		{ b->rec + 16u, FFN_REC_KIND },
		{ row, b->setting }, { row + 12u, FW_BUTTON_CELLS }, { row + 16u, FFN_CELLS_N },
		{ FW_BUTTON_PICKER_INIT + 8u * b->init, b->setting },
		{ FW_BUTTON_PICKER_INIT + 8u * b->init + 4u, FFN_PICKER_INIT_FN },
	};
	bool ok = ffn_expect_words(b->name, e, FFN_N(e));
	uint32_t rec = ffn_hash_find(b->setting);

	if (rec != b->rec) {
		fenykl_log_err(&fenykl_fnfunc_log, "%s: lookup finds record %08x, want %08x", b->name,
			       rec, b->rec);
		ok = false;
	}
	for (unsigned int i = 0; i < 2; i++) {
		if (fenykl_rd16(FFN_OVERRIDE_ID(i)) != b->setting)
			continue;
		fenykl_log_err(&fenykl_fnfunc_log, "%s: count override %u holds it", b->name, i);
		ok = false;
	}
	for (unsigned int i = 0; i < b->n; i++) {
		uint32_t v = fenykl_rd16(b->list + 2u * i);
		/* the four 28-lists are one shared copy: each must equal Fn1's */
		uint32_t want = b->n == FFN_LIST_N ? fenykl_rd16(fenykl_rd32(first + 4u) + 2u * i) : v;

		if (v != FFN_ID && v == want)
			continue;
		fenykl_log_err(&fenykl_fnfunc_log, "%s: list %u holds %x", b->name, i, v);
		ok = false;
	}
	return ok;
}

static bool ffn_check_keys(void)
{
	bool ok = true;

	for (unsigned int k = 0; k < FFN_KEYS; k++) {
		uint32_t a = FW_FUNC_SELECTOR_KEYS + FFN_KEY_STRIDE * k;
		const struct ffn_expect e[] = {
			{ a, FFN_NAMES_N << 16 | ffn_key_idx[k] },
			{ a + 4u, FFN_HELP_N << 16 | FFN_ICONS_N },
			{ a + 12u, FW_FUNC_NAMES }, { a + 16u, FW_FUNC_ICONS }, { a + 20u, FW_FUNC_HELP },
			{ a + 24u, FW_FUNC_FOOTER },
		};

		ok = ffn_expect_words("key entry", e, FFN_N(e)) && ok;
	}
	return ok;
}

/* No entry of a label table holds 0x3BF yet. */
static bool ffn_check_free(const char *what, uint32_t table, unsigned int n)
{
	for (unsigned int i = 0; i < n; i++) {
		if (fenykl_rd32(table + 12u * i) != FFN_ID)
			continue;
		fenykl_log_err(&fenykl_fnfunc_log, "%s %u already holds %x", what, i, FFN_ID);
		return false;
	}
	return true;
}

static bool ffn_check(void)
{
	const struct ffn_expect mode = {
		FW_SETTING_MAP + 4u * FFN_MODE_SETTING, FFN_MODE_ELEM << 16 | FFN_MODE_PROP,
	};
	bool ok = ffn_check_keys();

	ok = ffn_expect_words("mode", &mode, 1) && ok;

	for (unsigned int i = 0; i < FENYKL_FNFUNC_BUTTONS; i++)
		ok = ffn_check_button(&ffn_buttons[i]) && ok;
	ok = ffn_expect_words("labels", ffn_labels, FFN_N(ffn_labels)) && ok;
	ok = ffn_check_free("name", FW_FUNC_NAMES, FFN_NAMES_N) && ok;
	ok = ffn_check_free("help", FW_FUNC_HELP, FFN_HELP_N) && ok;
	ok = ffn_check_free("icon", FW_FUNC_ICONS, FFN_ICONS_N) && ok;
	return ffn_check_free("cell", FW_BUTTON_CELLS, FFN_CELLS_N) && ok;
}

/* --- install --- */

static bool ffn_text_ok(const char *s)
{
	unsigned int n = 0;

	if (!s)
		return false;
	while (s[n] && n <= FENYKL_FNFUNC_TEXT_MAX)
		n++;
	return n && n <= FENYKL_FNFUNC_TEXT_MAX;
}

/* Stores to in the word at a if it holds from, as a slot swap: D-cache written back around a
   store under masked interrupts. True if the word reads back as to. */
static bool ffn_cas(uint32_t a, uint32_t from, uint32_t to)
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

/* Copies a stock label table and appends {0x3BF, kind 5, text}. */
static void ffn_build_cells(volatile struct ffn_cell *dst, uint32_t src, unsigned int n,
			    const char *text)
{
	for (unsigned int i = 0; i < n; i++) {
		uint32_t a = src + 12u * i;

		dst[i].value = fenykl_rd32(a);
		dst[i].kind = (uint16_t)fenykl_rd16(a + 4u);
		dst[i].id = (uint16_t)fenykl_rd16(a + 6u);
		dst[i].aux = fenykl_rd32(a + 8u);
	}
	dst[n].value = FFN_ID;
	dst[n].kind = FFN_KIND_INLINE;
	dst[n].id = 0;
	dst[n].aux = (uint32_t)text;
}

static void ffn_build_list(volatile uint16_t *dst, uint32_t src, unsigned int n)
{
	for (unsigned int i = 0; i < n; i++)
		dst[i] = (uint16_t)fenykl_rd16(src + 2u * i);
	dst[n] = FFN_ID;
}

static void ffn_add(uint32_t addr, uint32_t stock, uint32_t ours)
{
	struct ffn_word *w = &ffn.w[ffn.nwords++];

	w->addr = addr;
	w->stock = stock;
	w->ours = ours;
}

/* The copies, then the words in install order: label pointers, label counts, list pointers and
   sizes. */
static void ffn_build(const struct fenykl_fnfunc *f)
{
	volatile struct ffn_shared *s = ffn_shared();

	ffn_build_list(s->list, ffn_buttons[FENYKL_FNFUNC_FN1].list, FFN_LIST_N);
	ffn_build_list(s->list_lfn, ffn_buttons[FENYKL_FNFUNC_LFN].list, FFN_LIST_LFN_N);
	ffn_build_cells(s->names, FW_FUNC_NAMES, FFN_NAMES_N, f->name);
	ffn_build_cells(s->help, FW_FUNC_HELP, FFN_HELP_N, f->help);
	ffn_build_cells(s->cells, FW_BUTTON_CELLS, FFN_CELLS_N, f->name);

	ffn.nwords = 0;
	for (unsigned int k = 0; k < FFN_KEYS; k++) {
		uint32_t a = FW_FUNC_SELECTOR_KEYS + FFN_KEY_STRIDE * k;

		ffn_add(a + 12u, FW_FUNC_NAMES, (uint32_t)s->names);
		ffn_add(a + 20u, FW_FUNC_HELP, (uint32_t)s->help);
	}
	for (unsigned int i = 0; i < FENYKL_FNFUNC_BUTTONS; i++)
		ffn_add(ffn_row(&ffn_buttons[i]) + 12u, FW_BUTTON_CELLS, (uint32_t)s->cells);
	for (unsigned int k = 0; k < FFN_KEYS; k++) {
		uint32_t a = FW_FUNC_SELECTOR_KEYS + FFN_KEY_STRIDE * k;

		ffn_add(a, FFN_NAMES_N << 16 | ffn_key_idx[k],
			(FFN_NAMES_N + 1u) << 16 | ffn_key_idx[k]);
		ffn_add(a + 4u, FFN_HELP_N << 16 | FFN_ICONS_N,
			(FFN_HELP_N + 1u) << 16 | FFN_ICONS_N);
	}
	for (unsigned int i = 0; i < FENYKL_FNFUNC_BUTTONS; i++)
		ffn_add(ffn_row(&ffn_buttons[i]) + 16u, FFN_CELLS_N, FFN_CELLS_N + 1u);
	for (unsigned int i = 0; i < FENYKL_FNFUNC_BUTTONS; i++) {
		const struct ffn_button *b = &ffn_buttons[i];
		uint32_t list = b->n == FFN_LIST_N ? (uint32_t)s->list : (uint32_t)s->list_lfn;

		ffn_add(b->rec + 4u, b->list, list);
		ffn_add(b->rec + 12u, FFN_REC_SIZE(b->n), FFN_REC_SIZE(b->n + 1u));
	}
}

/* Puts the stock words back, last swapped first; a word that does not hold ours is left (logged). */
static void ffn_unwind(void)
{
	while (ffn.done) {
		const struct ffn_word *w = &ffn.w[--ffn.done];

		if (ffn.done % FFN_YIELD_WORDS == 0)
			fenykl_task_sleep(1);
		if (ffn_cas(w->addr, w->ours, w->stock))
			continue;
		fenykl_log_err(&fenykl_fnfunc_log, "rollback: %08x holds %08x, not ours: left",
			       w->addr, fenykl_rd32(w->addr));
	}
}

int fenykl_fnfunc_install(const struct fenykl_fnfunc *f)
{
	uint32_t t0 = FENYKL_TIMER_NOW();

	if (ffn.installed)
		return FENYKL_FNFUNC_EBUSY;
	if (!f || !f->press || !ffn_text_ok(f->name) || !ffn_text_ok(f->help)) {
		fenykl_log_err(&fenykl_fnfunc_log, "install: no press, or a text missing or over %u",
			       FENYKL_FNFUNC_TEXT_MAX);
		return FENYKL_FNFUNC_EARG;
	}
	if ((fenykl_rd8(FW_SECTION_LOADED) & FFN_SECTIONS) != FFN_SECTIONS || !fenykl_gui_live_view())
		return FENYKL_FNFUNC_EAGAIN;
	if (!ffn_check()) {
		fenykl_log_err(&fenykl_fnfunc_log, "install: stock check failed, nothing written");
		return FENYKL_FNFUNC_ESTOCK;
	}
	ffn_build(f);
	/* every state between two swaps is a strict extension of the stock tables: yielding is safe */
	for (ffn.done = 0; ffn.done < ffn.nwords; ffn.done++) {
		const struct ffn_word *w = &ffn.w[ffn.done];

		if (ffn.done && ffn.done % FFN_YIELD_WORDS == 0)
			fenykl_task_sleep(1);
		if (ffn_cas(w->addr, w->stock, w->ours))
			continue;
		fenykl_log_err(&fenykl_fnfunc_log, "install: %08x holds %08x, want %08x: rolled back",
			       w->addr, fenykl_rd32(w->addr), w->stock);
		ffn_unwind();
		return FENYKL_FNFUNC_ESTOCK;
	}
	ffn.f = f;
	ffn.installed = true;
	fenykl_log_info(&fenykl_fnfunc_log, "installed \"%s\" as %x: %u words in %u us", f->name,
			FFN_ID, ffn.done, FENYKL_TIMER_ELAPSED(t0, FENYKL_TIMER_NOW()));
	return 0;
}

int fenykl_fnfunc_tick(void)
{
	unsigned int i;
	int r;

	if (!ffn.installed || ffn.listening || ffn.failed)
		return 0;
	for (i = 0; i < FENYKL_FNFUNC_BUTTONS; i++)
		if (ffn_setting(ffn_buttons[i].prop, ffn_buttons[i].elem) == FFN_ID)
			break;
	if (i == FENYKL_FNFUNC_BUTTONS)
		return 0;
	r = fenykl_event_add(&ffn_handler);
	if (!r)
		r = fenykl_event_install();
	if (r) {
		ffn.failed = true;
		fenykl_log_err(&fenykl_fnfunc_log, "event layer %d: no presses", r);
		return r;
	}
	ffn.listening = true;
	fenykl_log_info(&fenykl_fnfunc_log, "%s holds it: listening", ffn_buttons[i].name);
	return 0;
}
