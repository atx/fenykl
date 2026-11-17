/*
 * E-M10 II part of the HUD library (sdk/lib/hud_em10ii.c) in the GDB simulator: the visibility
 * predicate over GUI state, display request, output device, layer mask and the INFO screen property
 * (registry and override table), the output device, the colour pick from the live layer-1 palette,
 * and the widget backend against fw_em10ii.S's mocks, entered through its slot words with the
 * firmware's calling convention: install all or nothing, the pool's limit, the records given to
 * the factory, attach and the shown image per element at a panel create, one post per tick for
 * every change, the swaps in the GUI task (records decoded and compared with the rendered bitmaps), the empty image while
 * hidden or on another INFO screen, a lost and a refused post, the panel's destroy, the next panel
 * showing the newest content, refused widgets and CPU1 calls. plugin_main sets sim_exit_code to
 * the mask of failed checks below, or 16 + the number of the first failed widget step (0 = pass).
 */
#include "sim_hud.h"

enum {
	SIM_FAIL_VISIBLE = 1,	/* the predicate over GUI state, request, device and mask */
	SIM_FAIL_OUTPUT = 2,	/* device 1 LCD, 6 EVF, anything else none */
	SIM_FAIL_INFO = 4,	/* INFO screen from the registry or the override table */
	SIM_FAIL_COLOR = 8,	/* nearest opaque entry below 0x80 */
};

#define SIM_PROP_OVR (FW_PROP_OVERRIDE + 8u * (0x8321u >> 4) + 4u)

const void *const sim_images[1];
static volatile int8_t sim_info_ovr;

/* fw.S's glyph mock forwards here; no text is rendered in this program. */
uint32_t c_gpi_get_glyph(void)
{
	return 0;
}

static bool sim_visible_in(uint32_t gui, uint32_t req, uint32_t dev, uint16_t hide)
{
	SIM_U8(SIM_SUPER) = (uint8_t)(gui >> 8);
	SIM_U8(SIM_STATE) = (uint8_t)gui;
	SIM_U32(FW_GUI_DISP_REQ) = req;
	SIM_U32(FW_DISP_DEVICE) = dev;
	SIM_U16(SIM_HIDE_MASK) = hide;
	bool v = fenykl_hud_visible();

	SIM_U8(SIM_SUPER) = 0x01;
	SIM_U8(SIM_STATE) = 0x08;
	SIM_U32(FW_GUI_DISP_REQ) = 0xF001;
	SIM_U32(FW_DISP_DEVICE) = 1;
	SIM_U16(SIM_HIDE_MASK) = 0;
	return v;
}

static int sim_check_visible(void)
{
	static const int8_t shown[] = { 1, 0x11 }, hidden[] = { 0, 0x12, 2, -1 };

	if (!sim_visible_in(0x0108, 0xF001, 1, 0) || !sim_visible_in(0x0108, 0xF001, 6, 0))
		return 0;
	if (sim_visible_in(0x1101, 0xF001, 1, 0) || sim_visible_in(0x0403, 0xF001, 1, 0) ||
	    sim_visible_in(0x0108, 0xF003, 1, 0) || sim_visible_in(0x0108, 0xF001, 0, 0) ||
	    sim_visible_in(0x0108, 0xF001, 5, 0) || sim_visible_in(0x0108, 0xF001, 1, 0x10))
		return 0;
	if (!sim_visible_in(0x0108, 0xF001, 1, 0x101))
		return 0;
	for (unsigned int i = 0; i < sizeof(shown); i++) {
		sim_info = shown[i];
		if (!fenykl_hud_visible())
			return 0;
	}
	for (unsigned int i = 0; i < sizeof(hidden); i++) {
		sim_info = hidden[i];
		if (fenykl_hud_visible())
			return 0;
	}
	sim_info = SIM_INFO_BASIC;
	return 1;
}

static int sim_check_output(void)
{
	static const uint32_t dev[] = { 0, 1, 2, 3, 4, 5, 6, 7, 0x12 };
	static const int8_t out[] = { 0, 1, 0, 0, 0, 0, 2, 0, 0 };
	int ok = 1;

	for (unsigned int i = 0; i < sizeof(dev) / sizeof(dev[0]); i++) {
		SIM_U32(FW_DISP_DEVICE) = dev[i];
		ok &= fenykl_hud_output() == out[i];
	}
	SIM_U32(FW_DISP_DEVICE) = 1;
	return ok && FENYKL_HUD_OUT_LCD == 1 && FENYKL_HUD_OUT_EVF == 2;
}

static int sim_check_info(void)
{
	int ok = fenykl_hud_info_screen() == SIM_INFO_BASIC;

	/* the override table wins while valid and its entry is set */
	sim_info_ovr = SIM_INFO_IMAGE;
	SIM_U32(SIM_PROP_OVR) = (uint32_t)&sim_info_ovr;
	ok &= fenykl_hud_info_screen() == SIM_INFO_BASIC;
	SIM_U16(FW_PROP_OVERRIDE_VALID) = 1;
	ok &= fenykl_hud_info_screen() == SIM_INFO_IMAGE && !fenykl_hud_visible();
	SIM_U32(SIM_PROP_OVR) = 0;
	ok &= fenykl_hud_info_screen() == SIM_INFO_BASIC;
	SIM_U16(FW_PROP_OVERRIDE_VALID) = 0;
	/* an uncached object, an unregistered property */
	SIM_U32(SIM_PROP_INFO) = (uint32_t)FENYKL_UNCACHED(&sim_info);
	ok &= fenykl_hud_info_screen() == SIM_INFO_BASIC;
	SIM_U32(SIM_PROP_INFO) = 0;
	ok &= fenykl_hud_info_screen() == -1 && !fenykl_hud_visible();
	SIM_U32(SIM_PROP_INFO) = (uint32_t)&sim_info;
	return ok;
}

static void sim_pal(unsigned int i, uint8_t r, uint8_t g, uint8_t b, uint8_t a)
{
	uint32_t e = FW_OSD_PALETTE + 4 * i;

	SIM_U8(e) = r;
	SIM_U8(e + 1) = g;
	SIM_U8(e + 2) = b;
	SIM_U8(e + 3) = a;
}

static int sim_check_color(void)
{
	for (unsigned int i = 0; i < 256; i++)
		sim_pal(i, 0, 0, 0, 0);
	if (fenykl_hud_color(0xFF0000) != -1)
		return 0;
	sim_pal(0x20, 250, 10, 10, 0x1F);
	sim_pal(0x21, 255, 0, 0, 0x11);		/* not opaque */
	sim_pal(0x7F, 200, 200, 200, 0x1F);
	sim_pal(0x90, 255, 0, 0, 0x1F);		/* beyond the hardware palette */
	return fenykl_hud_color(0xFF0000) == 0x20 && fenykl_hud_color(0xFFFFFF) == 0x7F &&
	       fenykl_hud_color(0x000000) == 0x20;
}

/* --- widgets --- */

#define SIM_EVENT 0x7E47u
#define SIM_ELEMS 3u

/* the stock image record the widgets are given */
struct sim_img {
	uint16_t type, w, h, bpp;
	uint32_t zero, size, bitmap, key;
};

static uint32_t sim_app[0x1A0 / 4];
static uint32_t sim_panels[2][0x20 / 4];
static uint32_t sim_free_blocks[2][0x14 / 4];
static unsigned int sim_first;		/* sim_gui.w index of the newest panel's first widget */

static struct fenykl_hud_elem sim_elems[SIM_ELEMS] = {
	{ .x = 100, .y = 233, .w = 56, .h = 65, .bg = FENYKL_HUD_CLEAR },
	{ .x = 12, .y = 13, .w = 64, .h = 23, .bg = FENYKL_HUD_BLACK },
	{ .x = 12, .y = 40, .w = 64, .h = 9, .bg = FENYKL_HUD_CLEAR },
};

/* fenykl_hud_draw callback: a pattern of palette indices below 0x80 per seed */
static int sim_pattern(uint8_t *box, int w, int h, void *arg)
{
	unsigned int seed = (unsigned int)arg;

	for (int i = 0; i < w * h; i++)
		if ((unsigned int)i * seed % 7 < 3)
			box[i] = (uint8_t)(1 + (i + seed) % 0x7F);
	return 0;
}

/* 1 if the record behind handle decodes to want (NULL: all transparent), w x h, with the stock size
   rule; decoder written from the format (header {h-1:12, w-1:12, fmt:8}, six 10-bit codes
   colour << 2 | run-1 per little-endian u64, no run across a row end) */
static int sim_decodes(uint32_t handle, const uint8_t *want, int w, int h)
{
	const struct sim_img *r = (const struct sim_img *)(handle ? SIM_U32(handle) : 0);

	if (!r || r->type != 0x0E || r->w != w || r->h != h || r->bpp != 8 || r->key)
		return 0;
	const uint32_t *out = (const uint32_t *)r->bitmap;
	int n = 0, col = 0, i = 0;

	if (out[0] != ((uint32_t)(h - 1) | (uint32_t)(w - 1) << 12 | 3u << 24))
		return 0;
	while (i < w * h) {
		const uint32_t *q = out + 1 + 2 * (n / 6);
		uint64_t word = (uint64_t)q[1] << 32 | q[0];
		uint32_t code = (uint32_t)(word >> (10 * (n % 6))) & 0x3FFu;
		int run = (int)(code & 3u) + 1;

		if (col + run > w)
			return 0;
		for (int k = 0; k < run; k++, i++)
			if ((want ? want[i] : 0) != code >> 2)
				return 0;
		col = (col + run) % w;
		n++;
	}
	return r->size == (uint32_t)(4 + 8 * (n / 6) + (n % 6 ? (10 * (n % 6) + 7) / 8 : 0));
}

/* 1 if element i's widget in the newest panel shows its content (else: the empty image) */
static int sim_shows(unsigned int i, bool content)
{
	const struct fenykl_hud_elem *e = &sim_elems[i];
	const uint8_t *px = content ? (const uint8_t *)e->bmp[e->pub & 1u] : NULL;

	return sim_decodes(sim_gui.w[sim_first + i].handle, px, e->w, e->h);
}

static int sim_shows_all(bool content)
{
	return sim_shows(0, content) && sim_shows(1, content) && sim_shows(2, content);
}

/* the GUI's calls through the three slots */
static int sim_create(unsigned int p)
{
	sim_first = sim_gui.widgets;
	return fw_call_gui_panel_create(SIM_U32(fenykl_slot_info_panel_create.address), sim_panels[p]);
}

static void sim_destroy(unsigned int p)
{
	fw_call_gui_obj_method(SIM_U32(fenykl_slot_info_panel_destroy.address), sim_panels[p]);
}

static int sim_message(unsigned int code)
{
	return fw_call_gui_on_message(SIM_U32(fenykl_slot_gui_app_message.address), sim_app, code, 0,
				      NULL);
}

static void sim_screen(unsigned int p)
{
	sim_app[0x19C / 4] = (uint32_t)sim_panels[p];
}

static void sim_setup_app(void)
{
	uint32_t desc = FW_GUI_HEAP_DESC + 0x60;	/* pool 6, sub 1 */

	SIM_U32(FW_GUI_APP) = (uint32_t)sim_app;
	SIM_U16((uint32_t)sim_app + 0x4C) = 2;
	SIM_U16((uint32_t)sim_app + 0x4E) = 1;
	for (unsigned int p = 0; p < 2; p++)
		SIM_U16((uint32_t)sim_panels[p] + 0x1C) = (uint16_t)(6 | (1 - p) << 8);
	/* panel 0's pool: two free blocks of 200 and 16 bytes */
	SIM_U32(desc + 4) = (uint32_t)sim_free_blocks;
	SIM_U32(desc + 8) = sizeof(sim_free_blocks);
	SIM_U32(desc + 0xC) = (uint32_t)sim_free_blocks[0];
	sim_free_blocks[0][1] = 200;
	sim_free_blocks[0][3] = (uint32_t)sim_free_blocks[1];
	sim_free_blocks[1][1] = 16;
}

/* Slots all or nothing, the records given to the factory, attach, the shown images. */
static int sim_check_install(void)
{
	SIM_U16(FW_GUI_INFO_ISO_RECORD + 2) = 0x92;
	if (fenykl_hud_init(FENYKL_HUD_INFO_SCREENS) != FENYKL_HUD_ESTOCK ||
	    fenykl_hud_add(&sim_elems[0]) != FENYKL_HUD_EINIT)
		return 1;
	sim_setup_widgets();
	SIM_U32(fenykl_slot_gui_app_message.address) = 0x60123456;
	if (fenykl_hud_init(FENYKL_HUD_INFO_SCREENS) != FENYKL_HUD_ESLOT ||
	    SIM_U32(fenykl_slot_info_panel_destroy.address) != fenykl_slot_info_panel_destroy.expected ||
	    SIM_U32(fenykl_slot_info_panel_create.address) != fenykl_slot_info_panel_create.expected)
		return 2;
	sim_setup_widgets();
	if (fenykl_hud_init(FENYKL_HUD_INFO_SCREENS) != 0 ||
	    SIM_U32(fenykl_slot_info_panel_destroy.address) == fenykl_slot_info_panel_destroy.expected ||
	    SIM_U32(fenykl_slot_gui_app_message.address) == fenykl_slot_gui_app_message.expected ||
	    SIM_U32(fenykl_slot_info_panel_create.address) == fenykl_slot_info_panel_create.expected)
		return 3;
	for (unsigned int i = 0; i < SIM_ELEMS; i++)
		if (fenykl_hud_add(&sim_elems[i]) != 0)
			return 4;
	/* more than the pool has left */
	struct fenykl_hud_elem big = { .x = 0, .y = 0, .w = 640, .h = 25 };

	if (fenykl_hud_add(&big) != FENYKL_HUD_ENOMEM)
		return 4;
	/* encoded without a panel, nothing posted */
	if (fenykl_hud_draw(&sim_elems[0], sim_pattern, (void *)3) != 0)
		return 4;
	fenykl_hud_tick();
	if (sim_gui.posts)
		return 4;
	if (sim_create(0) != 1 || sim_gui.stock_create != 1 || sim_gui.widgets != SIM_ELEMS)
		return 5;
	for (unsigned int i = 0; i < SIM_ELEMS; i++) {
		const struct sim_widget *w = &sim_gui.w[i];
		const struct fenykl_hud_elem *e = &sim_elems[i];
		const uint8_t *rec = w->rec;
		const uint16_t *rects = (const uint16_t *)SIM_U32((uint32_t)rec + 4);
		const uint16_t *list = (const uint16_t *)SIM_U32((uint32_t)rec + 0x38);

		if (w->parent != (uint32_t)sim_panels[0] || w->attached != (uint32_t)sim_panels[0] ||
		    SIM_U16((uint32_t)rec) != 0x8003 || SIM_U16((uint32_t)rec + 2) != 0xF0 + i ||
		    rec[0x34] != 2 || rec[0x35] != 1 || list[0] != 0xFFFF || !list[6])
			return 6;
		for (unsigned int r = 0; r < 2; r++)
			if (rects[4 * r] != e->x || rects[4 * r + 1] != e->y ||
			    rects[4 * r + 2] != e->w || rects[4 * r + 3] != e->h)
				return 6;
		if (!sim_shows(i, i == 0))
			return 7;
	}
	struct fenykl_hud_stats st;

	fenykl_hud_get_stats(&st);
	return st.creates == 1 && st.made == SIM_ELEMS && st.heap_free == 216 ? 0 : 8;
}

/* One post per tick for all changes, swaps in the GUI task, visibility, hide, lost and refused
   posts, the panel's destroy and the next create, refused widgets, CPU1. */
static int sim_check_updates(void)
{
	struct fenykl_hud_stats st;
	unsigned int flushes, posts, handles;

	sim_screen(0);
	fenykl_hud_draw(&sim_elems[1], sim_pattern, (void *)5);
	flushes = sim_gui.flushes;
	fenykl_hud_tick();
	if (sim_gui.posts != 1 || sim_gui.post_code != SIM_EVENT || sim_gui.flushes != flushes + 1)
		return 9;
	fenykl_hud_draw(&sim_elems[2], sim_pattern, (void *)6);
	fenykl_hud_tick();
	if (sim_gui.posts != 1)
		return 10;
	if (sim_message(SIM_EVENT) != 0 || !sim_shows_all(true))
		return 11;
	if (sim_message(0x1100) != SIM_STOCK_MSG || sim_gui.stock_msg != 1)
		return 12;
	sim_info = SIM_INFO_IMAGE;
	fenykl_hud_tick();
	sim_message(SIM_EVENT);
	if (sim_gui.posts != 2 || !sim_shows_all(false))
		return 13;
	sim_info = SIM_INFO_BASIC;
	flushes = sim_gui.flushes;
	fenykl_hud_tick();
	sim_message(SIM_EVENT);
	if (sim_gui.posts != 3 || sim_gui.flushes != flushes || !sim_shows_all(true))
		return 14;
	fenykl_hud_hide(&sim_elems[0]);
	fenykl_hud_tick();
	sim_message(SIM_EVENT);
	if (!sim_shows(0, false) || !sim_shows(1, true))
		return 15;
	/* an event that does not arrive: posted again after 5 ticks */
	fenykl_hud_draw(&sim_elems[1], sim_pattern, (void *)9);
	posts = sim_gui.posts;
	for (int t = 0; t < 6; t++) {
		fenykl_hud_tick();
		if (sim_gui.posts != posts + 1 + (t == 5))
			return 16;
	}
	sim_message(SIM_EVENT);
	fenykl_hud_get_stats(&st);
	if (st.unstuck != 1 || !sim_shows(1, true))
		return 17;
	sim_gui.refuse_post = true;
	fenykl_hud_draw(&sim_elems[2], sim_pattern, (void *)11);
	fenykl_hud_tick();
	sim_gui.refuse_post = false;
	posts = sim_gui.posts;
	fenykl_hud_tick();
	sim_message(SIM_EVENT);
	fenykl_hud_get_stats(&st);
	if (st.post_fail != 1 || sim_gui.posts != posts + 1 || !sim_shows(2, true))
		return 18;
	sim_destroy(1);
	fenykl_hud_get_stats(&st);
	if (st.destroys != 0 || sim_gui.stock_destroy != 1)
		return 19;
	sim_destroy(0);
	fenykl_hud_get_stats(&st);
	if (st.destroys != 1 || sim_gui.stock_destroy != 2)
		return 19;
	/* no panel: encoded, not posted; a stray event touches no widget */
	handles = sim_gui.handles;
	posts = sim_gui.posts;
	fenykl_hud_draw(&sim_elems[0], sim_pattern, (void *)13);
	fenykl_hud_tick();
	sim_message(SIM_EVENT);
	if (sim_gui.posts != posts || sim_gui.handles != handles)
		return 20;
	/* the next panel shows the newest content before it is on screen */
	if (sim_create(1) != 1 || !sim_shows_all(true))
		return 21;
	fenykl_hud_draw(&sim_elems[1], sim_pattern, (void *)15);
	posts = sim_gui.posts;
	fenykl_hud_tick();
	if (sim_gui.posts != posts)
		return 22;
	sim_screen(1);
	fenykl_hud_tick();
	sim_message(SIM_EVENT);
	if (sim_gui.posts != posts + 1 || !sim_shows_all(true))
		return 22;
	sim_gui.refuse_append = true;
	sim_create(0);
	sim_gui.refuse_append = false;
	fenykl_hud_get_stats(&st);
	if (st.failed != SIM_ELEMS)
		return 23;
	handles = sim_gui.widgets;
	SIM_U16((uint32_t)sim_app + 0x4C) = 3;
	sim_create(1);
	SIM_U16((uint32_t)sim_app + 0x4C) = 2;
	fenykl_hud_get_stats(&st);
	if (st.failed != 2 * SIM_ELEMS || sim_gui.widgets != handles)
		return 23;
	/* CPU1: the event goes to the stock handler; a destroy still drops the panel */
	SIM_U32(FW_CORE_ID) = 1;
	int r = sim_message(SIM_EVENT);

	sim_destroy(1);
	SIM_U32(FW_CORE_ID) = 0;
	fenykl_hud_get_stats(&st);
	return r == SIM_STOCK_MSG && st.cpu1 == 2 && sim_gui.stock_msg == 2 && st.destroys == 2 &&
	       sim_gui.stock_destroy == 3 ? 0 : 24;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	int fail = 0;

	sim_setup_memory();
	sim_setup_app();
	sim_setup_widgets();
	/* fenykl_hud_init sets the INFO screens the predicate checks */
	int step = sim_check_install();

	if (!sim_check_visible())
		fail |= SIM_FAIL_VISIBLE;
	if (!sim_check_output())
		fail |= SIM_FAIL_OUTPUT;
	if (!sim_check_info())
		fail |= SIM_FAIL_INFO;
	if (!sim_check_color())
		fail |= SIM_FAIL_COLOR;
	if (fail) {
		sim_exit_code = (uint32_t)fail;
		return;
	}
	if (!step)
		step = sim_check_updates();
	sim_exit_code = step ? 16u + (uint32_t)step : 0;
}
