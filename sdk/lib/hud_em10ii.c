/*
 * HUD library, E-M10 II part (sdk/docs/hud.md 6): each element is a stock image widget (type 0x8003)
 * on the still live-view info panel (panel id 10, class vtable 0x60C9801C), so the GUI draws, clips
 * and repaints it with its own items and deletes it with the panel (movie, menu, playback). Three
 * slots, replaced all or nothing by fenykl_hud_init and then resident:
 * - panel OnCreate: the stock builder, then one widget per element, made by the stock factory from
 *   a copy of the ISO item's record (parent = panel, so the panel's OnDestroy deletes it), appended
 *   to the panel's child list and attached, showing the image the element should show.
 * - panel destroy: drops the widget pointers when the panel is ours (the next panel is created
 *   before the old one is destroyed). Never frees a widget.
 * - GUI app OnMessage: HUD_EVENT, posted by fenykl_hud_tick, sets the widgets' image handles.
 * Each element has three image records: two for its content (the task encodes into the one the GUI
 * side neither holds as newest nor shows, and hands it over with `ready`) and an empty one (all
 * transparent), shown while the element is hidden or fenykl_hud_visible is false. Widget calls run
 * in the GUI task only; the task encodes, flushes and posts. Also here: visibility, the output
 * device, the INFO screen, the GUI panel and colours from the live layer-1 palette.
 */
#include <body.h>
#include "hud_int.h"

#define HUD_LAYER1_HIDDEN 0x10u		/* gpi_hide_mask bit of layer 1 */
#define HUD_DISP_REQ_INFO 0xF001u	/* EVT_REQ_REC_DISP_INFO: the live-view info display */
#define HUD_DEV_LCD 1u			/* disp_device: rear LCD */
#define HUD_DEV_EVF 6u			/* built-in EVF */
#define HUD_PROP_INFO 0x8321u		/* s8 property: the live-view INFO screen */
#define HUD_PAL_ALPHA 3			/* osd_palette entry {R, G, B, alpha >> 3} */
#define HUD_PAL_OPAQUE 0x1Fu
#define HUD_APP_PANEL 0x19Cu		/* gui_app: the panel object on screen */
#define HUD_APP_RECTS 0x4Cu		/* gui_app: s16 rects per record */
#define HUD_APP_RECT 0x4Eu		/* gui_app: s16 rect used */
#define HUD_EVENT 0x7E47u		/* private GUI event code: no operand in the firmware uses it */
#define HUD_WIDGET_ID 0xF0u		/* + element index; >= 0x96: the panel's id lookup never returns it */
#define HUD_TYPE_IMAGE 0x8003u
#define HUD_ISO_ID 0x91u
#define HUD_LIST_IMAGE 1147u		/* stock "P" icon of the image list: shown until a handle is set */
#define HUD_IMG_TYPE 0x0Eu		/* stock image record type, 8 bpp */
#define HUD_REC_SIZE 0x58
#define HUD_RECTS 2			/* rects per record, as the stock records */
#define HUD_R_ID 0x02			/* record fields */
#define HUD_R_RECTS 0x04
#define HUD_R_MODE 0x34
#define HUD_R_COUNT 0x35
#define HUD_R_LIST 0x38
#define HUD_R_EXTRA 0x3C
#define HUD_W_POOL 0x1Cu		/* widget or panel: u8 pool, u8 sub */
#define HUD_VT_ATTACH 0x94u
#define HUD_EMPTY 2u			/* record index of the empty image */
#define HUD_NONE 0xFFu			/* no record */
#define HUD_STUCK_TICKS 5u		/* ticks without the posted event before it is posted again */
/* GUI heap descriptors {u8 pool, u8 sub, .., +4 base, +8 size, +0xC free tree}: pool 6 has two
   subs, one per panel slot; free blocks {.., +4 size, .., +0xC left, +0x10 right} */
#define HUD_HEAP_POOL6 (FW_GUI_HEAP_DESC + 0x50u)
#define HUD_HEAP_NODES 4096u
#define HUD_HEAP_DEPTH 48
/* The elements' bitmaps and image buffers: main's five take 54108 bytes. fw_malloc would give each
   buffer over 0x800 bytes a block of the firmware's large class, which the firmware shares. */
#define HUD_POOL_WORDS (0xD800u / 4)

/* the stock image record */
struct hud_img {
	uint16_t type, w, h, bpp;
	uint32_t zero, size, bitmap, key;
};

/* One element's widget. Written by the task: rec..pub, ready (to a record); by the GUI task: cur,
   ready (to HUD_NONE), shown, obj. */
struct hud_widget {
	uint32_t rec[HUD_REC_SIZE / 4];
	uint16_t rects[HUD_RECTS][4];
	uint16_t list[8];
	uint16_t extra[1];
	struct hud_img img[3];		/* content 0 and 1, HUD_EMPTY */
	uint32_t handle[3];		/* -> img[i], what gui_image_set_handle takes */
	uint32_t *bits[3];		/* their bitmaps */
	uint32_t pub;			/* e->pub last encoded */
	volatile uint8_t ready;		/* content record encoded for the GUI task to take, HUD_NONE */
	volatile uint8_t cur;		/* content record with the newest content taken */
	volatile uint8_t shown;		/* record the widget shows, HUD_EMPTY without a widget */
	void *volatile obj;		/* the widget, NULL while the panel has none */
};

static struct {
	struct hud_widget w[FENYKL_HUD_ELEMS_MAX];
	void *volatile panel;		/* the panel holding the widgets */
	volatile bool posted;		/* HUD_EVENT posted, not yet received */
	unsigned int waits;		/* ticks since the post */
	uint32_t heap_stack[HUD_HEAP_DEPTH];	/* GUI task */
	unsigned int pool_used;		/* words of fenykl_hud_pool given out */
	volatile struct fenykl_hud_stats st;
} hn;

/* Weak, so that a simulator test can link a larger pool (both symbols). */
uint32_t fenykl_hud_pool[HUD_POOL_WORDS] __attribute__((weak));
uint32_t fenykl_hud_pool_words __attribute__((weak)) = HUD_POOL_WORDS;	/* not const: GCC folds a weak const */

int fenykl_hud_output(void)
{
	uint32_t dev = fenykl_rd32(FW_DISP_DEVICE);

	if (dev == HUD_DEV_LCD)
		return FENYKL_HUD_OUT_LCD;
	if (dev == HUD_DEV_EVF)
		return FENYKL_HUD_OUT_EVF;
	return FENYKL_HUD_OUT_NONE;
}

int fenykl_hud_prop(unsigned int prop)
{
	uint32_t obj = fbody_prop_obj(prop);

	if (!fenykl_dram_ptr(obj, 1))
		return -1;
	return (int8_t)fenykl_rd8(obj);
}

int fenykl_hud_info_screen(void)
{
	return fenykl_hud_prop(HUD_PROP_INFO);
}

void fenykl_hud_panel(struct fenykl_hud_panel *p)
{
	uint32_t at = fenykl_rd32(FW_GUI_APP) + HUD_APP_PANEL;

	p->id = (int16_t)fenykl_rd16(FW_GUI_CUR_PANEL);
	p->lv_id = (int32_t)fenykl_rd32(FW_GUI_LV_PANEL);
	p->obj = fenykl_dram_ptr(at, 4) ? fenykl_rd32(at) : 0;
	p->vtable = fenykl_dram_ptr(p->obj, 4) ? fenykl_rd32(p->obj) : 0;
}

bool fenykl_hud_visible(void)
{
	if (fenykl_rd16(FW_GPI_HIDE_MASK) & HUD_LAYER1_HIDDEN)
		return false;
	if (!fenykl_gui_live_view() || fenykl_rd32(FW_GUI_DISP_REQ) != HUD_DISP_REQ_INFO)
		return false;
	if (fenykl_hud_output() == FENYKL_HUD_OUT_NONE)
		return false;
	int info = fenykl_hud_info_screen();

	return info >= 0 && info < 32 && (hud.states & FENYKL_HUD_INFO(info));
}

int fenykl_hud_color(uint32_t rgb)
{
	const volatile uint8_t *pal = (const volatile uint8_t *)FW_OSD_PALETTE;
	uint32_t best = 0xFFFFFFFFu;
	int pick = -1;

	for (int i = 0; i < FENYKL_HUD_PAL_ENTRIES; i++) {
		const volatile uint8_t *e = pal + 4 * i;

		if (e[HUD_PAL_ALPHA] < HUD_PAL_OPAQUE)
			continue;
		int dr = e[0] - (int)(rgb >> 16 & 0xFF);
		int dg = e[1] - (int)(rgb >> 8 & 0xFF);
		int db = e[2] - (int)(rgb & 0xFF);
		uint32_t d = (uint32_t)(dr * dr + dg * dg + db * db);

		if (d < best) {
			best = d;
			pick = i;
		}
	}
	return pick;
}

/* --- GUI task --- */

static bool hud_on_cpu1(void)
{
	if (FENYKL_CORE_ID() == 0)
		return false;
	hn.st.cpu1++;
	return true;
}

/* Free bytes of a GUI heap descriptor, the sizes in its free tree (alloc and free take no lock). */
static uint32_t hud_heap_free(uint32_t desc)
{
	uint32_t base = fenykl_rd32(desc + 4), end = base + fenykl_rd32(desc + 8);
	uint32_t free = 0, blocks = 0;
	int sp = 0;

	if (fenykl_rd32(desc + 0xC))
		hn.heap_stack[sp++] = fenykl_rd32(desc + 0xC);
	while (sp) {
		uint32_t b = hn.heap_stack[--sp];

		if (b < base || b >= end || b & 3u || ++blocks > HUD_HEAP_NODES)
			continue;
		free += fenykl_rd32(b + 4);
		for (uint32_t off = 0xC; off <= 0x10; off += 4) {
			uint32_t next = fenykl_rd32(b + off);

			if (next && sp < HUD_HEAP_DEPTH)
				hn.heap_stack[sp++] = next;
		}
	}
	return free;
}

/* Takes element i's newly encoded content and returns the record it should show. */
static uint8_t hud_want(unsigned int i, bool visible)
{
	struct hud_widget *w = &hn.w[i];
	uint8_t r = w->ready;

	if (r != HUD_NONE) {
		w->cur = r;
		__asm__ volatile("" ::: "memory");	/* the task sees cur before ready */
		w->ready = HUD_NONE;
	}
	return visible && (hud.elem[i]->pub & HUD_PUB_SHOWN) ? w->cur : HUD_EMPTY;
}

static void hud_make_widget(void *app, void *panel, unsigned int i, bool visible)
{
	struct hud_widget *w = &hn.w[i];
	void *obj = fw_gui_widget_create(app, HUD_TYPE_IMAGE, (uint32_t)panel, w->rec);

	/* a widget not in the child list is never deleted: one 300-byte block lost */
	if (!obj || !fw_gui_container_append(panel, obj)) {
		hn.st.failed++;
		return;
	}
	fw_call_gui_attach(fenykl_rd32(fenykl_rd32((uint32_t)obj) + HUD_VT_ATTACH), obj, panel);
	uint8_t want = hud_want(i, visible);

	fw_gui_image_set_handle(obj, &w->handle[want], 0);
	w->shown = want;
	w->obj = obj;
	hn.st.made++;
}

static int hud_panel_create(void *panel)
{
	int r = fw_orig_info_panel_create(panel);

	if (hud_on_cpu1() || !r)
		return r;
	uint32_t app = fenykl_rd32(FW_GUI_APP);
	int rects = (int16_t)fenykl_rd16(app + HUD_APP_RECTS);
	int rect = (int16_t)fenykl_rd16(app + HUD_APP_RECT);
	unsigned int n = hud.count;
	bool visible = fenykl_hud_visible();

	hn.st.creates++;
	hn.panel = panel;
	for (unsigned int i = 0; i < n; i++) {
		hn.w[i].obj = NULL;
		hn.w[i].shown = HUD_EMPTY;
	}
	/* the records hold HUD_RECTS rects */
	if (rects > HUD_RECTS || rect < 0 || rect >= HUD_RECTS) {
		hn.st.failed += n;
		return r;
	}
	for (unsigned int i = 0; i < n; i++)
		hud_make_widget((void *)app, panel, i, visible);
	hn.st.heap_free = hud_heap_free(HUD_HEAP_POOL6 +
					0x10u * (fenykl_rd16((uint32_t)panel + HUD_W_POOL) >> 8 & 1u));
	return r;
}
FENYKL_CALLBACK(gui_panel_create, hud_panel_create);

static void hud_panel_destroy(void *panel)
{
	/* also on CPU1: the panel objects reuse two addresses, a kept pointer could match again */
	hud_on_cpu1();
	if (panel == hn.panel) {
		hn.st.destroys++;
		for (unsigned int i = 0; i < FENYKL_HUD_ELEMS_MAX; i++) {
			hn.w[i].obj = NULL;
			hn.w[i].shown = HUD_EMPTY;
		}
		hn.panel = NULL;
	}
	fw_orig_info_panel_destroy(panel);
}
FENYKL_CALLBACK(gui_obj_method, hud_panel_destroy);

/* The panel holding the widgets is the one on screen: only then are they touched (a widget freed
   by another path must not be). */
static bool hud_live(void)
{
	void *screen = (void *)fenykl_rd32(fenykl_rd32(FW_GUI_APP) + HUD_APP_PANEL);

	return hn.panel && hn.panel == screen;
}

static int hud_app_message(void *app, unsigned int code, unsigned int p1, void *p2)
{
	if ((code & 0xFFFFu) != HUD_EVENT || hud_on_cpu1())
		return fw_orig_gui_app_message(app, code, p1, p2);
	bool live = hud_live(), visible = fenykl_hud_visible();

	hn.posted = false;
	hn.st.messages++;
	/* hud_want takes new records also without a widget on screen, so the task can encode the next
	   ones; the next create shows the newest */
	for (unsigned int i = 0; i < hud.count; i++) {
		struct hud_widget *w = &hn.w[i];
		uint8_t want = hud_want(i, visible);

		if (!live || !w->obj || w->shown == want)
			continue;
		fw_gui_image_set_handle(w->obj, &w->handle[want], 0);
		w->shown = want;
		hn.st.swaps++;
	}
	return 0;
}
FENYKL_CALLBACK(gui_on_message, hud_app_message);

/* --- task --- */

/* Encodes e's bitmap of pub p into the content record element i's GUI side neither holds as its
   newest nor shows; false while there is none (the last one not yet taken). */
static bool hud_encode(unsigned int i, const struct fenykl_hud_elem *e, uint32_t p)
{
	struct hud_widget *w = &hn.w[i];

	if (w->ready != HUD_NONE)
		return false;
	uint8_t back = w->cur ^ 1u;

	if (back == w->shown)
		return false;
	int size = hud_rle_encode((const uint8_t *)e->bmp[p & HUD_PUB_INDEX], e->w, e->h,
				  w->bits[back], HUD_RLE_WORDS(e->w, e->h));

	if (size < 0)
		return false;
	w->img[back].size = (uint32_t)size;
	w->pub = p;
	return true;
}

void fenykl_hud_tick(void)
{
	bool visible = fenykl_hud_visible(), want_post = false;
	uint8_t encoded[FENYKL_HUD_ELEMS_MAX];
	unsigned int n = hud.count, k = 0;

	for (unsigned int i = 0; i < n; i++) {
		const struct fenykl_hud_elem *e = hud.elem[i];
		struct hud_widget *w = &hn.w[i];
		uint32_t p = e->pub;
		bool content = visible && (p & HUD_PUB_SHOWN);

		if (content && p != w->pub && hud_encode(i, e, p))
			encoded[k++] = (uint8_t)i;
		else if (w->obj && (w->ready != HUD_NONE ||
				    w->shown != (content ? w->cur : HUD_EMPTY)))
			want_post = true;
	}
	if (k) {
		/* the GUI reads the bitmaps through the uncached alias, as the stock ones */
		fenykl_dcache_flush();
		for (unsigned int j = 0; j < k; j++) {
			struct hud_widget *w = &hn.w[encoded[j]];

			w->ready = w->cur ^ 1u;
			want_post = want_post || w->obj;
		}
	}
	if (!want_post || !hud_live()) {
		hn.waits = 0;
		return;
	}
	/* GESetEvent can report a post it did not queue */
	if (hn.posted && ++hn.waits < HUD_STUCK_TICKS)
		return;
	if (hn.posted)
		hn.st.unstuck++;
	hn.waits = 0;
	hn.posted = true;
	hn.st.posts++;
	if (fw_gui_post(HUD_EVENT))
		return;
	hn.st.post_fail++;
	hn.posted = false;
}

void hud_show(struct fenykl_hud_elem *e, uint32_t idx)
{
	hud_publish(e, HUD_PUB_SHOWN | idx);
}

void fenykl_hud_hide(struct fenykl_hud_elem *e)
{
	hud_publish(e, e->pub & HUD_PUB_INDEX);
}

/* --- setup (task) --- */

/* The widget record: the ISO item's with our id, rects, image list and mode. */
static void hud_prepare_record(struct hud_widget *w, const struct fenykl_hud_elem *e,
			       unsigned int index)
{
	uint8_t *rec = (uint8_t *)w->rec;

	memcpy(rec, (const void *)FW_GUI_INFO_ISO_RECORD, HUD_REC_SIZE);
	for (unsigned int i = 0; i < HUD_RECTS; i++) {
		w->rects[i][0] = (uint16_t)e->x;
		w->rects[i][1] = (uint16_t)e->y;
		w->rects[i][2] = (uint16_t)e->w;
		w->rects[i][3] = (uint16_t)e->h;
	}
	/* {no background, .., image id, 0} as the ISO item's entry */
	memset(w->list, 0, sizeof(w->list));
	w->list[0] = 0xFFFF;
	w->list[6] = HUD_LIST_IMAGE;
	w->extra[0] = 0;
	*(uint16_t *)(rec + HUD_R_ID) = (uint16_t)(HUD_WIDGET_ID + index);
	*(uint32_t *)(rec + HUD_R_RECTS) = (uint32_t)w->rects;
	rec[HUD_R_MODE] = 2;
	rec[HUD_R_COUNT] = 1;
	*(uint32_t *)(rec + HUD_R_LIST) = (uint32_t)w->list;
	*(uint32_t *)(rec + HUD_R_EXTRA) = (uint32_t)w->extra;
}

/* words from the pool; the caller checked that they fit */
static uint32_t *hud_take(unsigned int words)
{
	uint32_t *p = &fenykl_hud_pool[hn.pool_used];

	hn.pool_used += words;
	return p;
}

int hud_add(struct fenykl_hud_elem *e, unsigned int index)
{
	struct hud_widget *w = &hn.w[index];
	const int words[3] = { HUD_RLE_WORDS(e->w, e->h), HUD_RLE_WORDS(e->w, e->h),
			       HUD_RLE_FLAT_WORDS(e->w, e->h) };
	unsigned int bmp_words = ((unsigned int)(e->w * e->h) + 3u) / 4u;

	if (2u * bmp_words + (unsigned int)(words[0] + words[1] + words[2]) >
	    fenykl_hud_pool_words - hn.pool_used)
		return FENYKL_HUD_ENOMEM;
	e->bmp[0] = hud_take(bmp_words);
	e->bmp[1] = hud_take(bmp_words);
	for (unsigned int i = 0; i < 3; i++)
		w->bits[i] = hud_take((unsigned int)words[i]);
	hud_prepare_record(w, e, index);
	/* every record starts empty; bmp[0] is not published yet */
	memset(e->bmp[0], FENYKL_HUD_CLEAR, (size_t)e->w * e->h);
	for (unsigned int i = 0; i < 3; i++) {
		int size = hud_rle_encode((const uint8_t *)e->bmp[0], e->w, e->h, w->bits[i], words[i]);

		if (size < 0)
			return FENYKL_HUD_ENOMEM;
		w->img[i] = (struct hud_img){ HUD_IMG_TYPE, (uint16_t)e->w, (uint16_t)e->h, 8, 0,
					      (uint32_t)size, (uint32_t)FENYKL_UNCACHED(w->bits[i]), 0 };
		w->handle[i] = (uint32_t)&w->img[i];
	}
	fenykl_dcache_flush();
	w->pub = 0;
	w->ready = HUD_NONE;
	w->cur = 0;
	w->shown = HUD_EMPTY;
	w->obj = NULL;
	return 0;
}

int fenykl_hud_init(uint32_t states)
{
	if (fenykl_rd16(FW_GUI_INFO_ISO_RECORD) != HUD_TYPE_IMAGE ||
	    fenykl_rd16(FW_GUI_INFO_ISO_RECORD + HUD_R_ID) != HUD_ISO_ID)
		return FENYKL_HUD_ESTOCK;
	hud.states = states;
	/* OnCreate last: no widget before the other two are in place */
	int d = FENYKL_SLOT_REPLACE(info_panel_destroy, hud_panel_destroy);
	int m = d ? 0 : FENYKL_SLOT_REPLACE(gui_app_message, hud_app_message);
	int c = d || m ? 0 : FENYKL_SLOT_REPLACE(info_panel_create, hud_panel_create);

	if (d || m || c) {
		fenykl_slot_restore(&fenykl_slot_info_panel_create);
		fenykl_slot_restore(&fenykl_slot_gui_app_message);
		fenykl_slot_restore(&fenykl_slot_info_panel_destroy);
		return FENYKL_HUD_ESLOT;
	}
	hud.ready = true;
	return 0;
}

void fenykl_hud_get_stats(struct fenykl_hud_stats *st)
{
	*st = hn.st;
	st->nofont = hud.nofont;
	st->glyph_miss = hud.glyph_miss;
}
