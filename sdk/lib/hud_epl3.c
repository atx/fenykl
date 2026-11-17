/*
 * HUD library, E-PL3 part (sdk/docs/hud.md): the elements are copied into OSD layer 1. The GUI
 * queues its drawing during a frame, executes the batch in gpi_flush and then shows the buffer with
 * cw_updateDisplay(0). The plugin's hook on cw_updateDisplay calls fenykl_hud_frame, which copies
 * each published element into the layer-1 buffer about to be shown, after the GUI's batch. The
 * hook only reads and copies; the task paints changes at once and keeps the boxes off hidden
 * screens (fenykl_hud_tick).
 */
#include "hud_int.h"

#define HUD_LAYER 1
#define HUD_PLANE 2
#define HUD_BPP 8
#define HUD_PITCH 896
#define HUD_ROWS 600
#define HUD_NOT_WRITTEN 0xFFFFFFFFu	/* never a pub value: the sequence number does not get there */
#define HUD_HIDE_CLEARS 2u		/* clears by fenykl_hud_tick after the one in fenykl_hud_hide */
#define HUD_WIPE_TICKS 5u		/* fenykl_hud_tick calls (~0.5 s) hidden before the boxes are wiped */

static struct {
	volatile uint8_t *canvas[2];
	unsigned int hidden_ticks;	/* fenykl_hud_tick calls hidden in a row, up to HUD_WIPE_TICKS */
	volatile struct fenykl_hud_stats st;
} ho;

/* --- copies into layer 1 --- */

static volatile uint32_t *hud_row_of(const struct fenykl_hud_elem *e, volatile uint8_t *c, int r)
{
	return (volatile uint32_t *)(c + (e->y + r) * HUD_PITCH + e->x);
}

/* One step of a box checksum; the rotation makes it depend on the word positions. */
static uint32_t hud_mix(uint32_t sum, uint32_t w)
{
	return (sum << 5 | sum >> 27) + w;
}

/* Copies bmp into e's box on canvas c and returns the box's checksum. */
static uint32_t hud_copy_box(const struct fenykl_hud_elem *e, volatile uint8_t *c,
			     const uint32_t *bmp)
{
	unsigned int words = (unsigned int)e->w / 4;
	uint32_t sum = 0;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *dst = hud_row_of(e, c, r);
		const uint32_t *src = bmp + r * words;

		for (unsigned int x = 0; x < words; x++) {
			dst[x] = src[x];
			sum = hud_mix(sum, src[x]);
		}
	}
	return sum;
}

static uint32_t hud_box_sum(const struct fenykl_hud_elem *e, volatile uint8_t *c)
{
	unsigned int words = (unsigned int)e->w / 4;
	uint32_t sum = 0;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *row = hud_row_of(e, c, r);

		for (unsigned int x = 0; x < words; x++)
			sum = hud_mix(sum, row[x]);
	}
	return sum;
}

static bool hud_box_intact(const struct fenykl_hud_elem *e, volatile uint8_t *c,
			   const uint32_t *bmp)
{
	unsigned int words = (unsigned int)e->w / 4;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *row = hud_row_of(e, c, r);
		const uint32_t *src = bmp + r * words;

		for (unsigned int x = 0; x < words; x++)
			if (row[x] != src[x])
				return false;
	}
	return true;
}

static void hud_clear_box(const struct fenykl_hud_elem *e, volatile uint8_t *c)
{
	unsigned int words = (unsigned int)e->w / 4;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *row = hud_row_of(e, c, r);

		for (unsigned int x = 0; x < words; x++)
			row[x] = FENYKL_HUD_CLEAR * 0x01010101u;
	}
}

/* --- visibility --- */

bool fenykl_hud_visible(void)
{
	if (fenykl_rd16(FW_GPI_HIDE_MASK) & 0x10)
		return false;
	if (fenykl_rd8(FW_GUI_SUPER) != HUD_SS_REC)
		return false;
	unsigned int state = fenykl_rd8(FW_GUI_STATE);

	return state < 32 && (hud.states & FENYKL_HUD_STATE(state));
}

/* Copies e's bitmap of pub p into buffer b and records what the buffer holds. */
static void hud_put_box(struct fenykl_hud_elem *e, unsigned int b, uint32_t p)
{
	e->sum[b] = hud_copy_box(e, ho.canvas[b], e->bmp[p & HUD_PUB_INDEX]);
	e->ours[b] = 1;
	e->written[b] = p;
}

/* --- the hook side (GUI task, CPU0) --- */

/* The layer-1 buffer cw_updateDisplay(0) shows (0x6EFDB6E3). */
static unsigned int hud_shown_buffer(void)
{
	if (fenykl_rd8(FW_GPI_ONE_MODE) == 2 &&
	    (int16_t)fenykl_rd16(FW_GPI_ONE_LAYER) == HUD_LAYER) {
		ho.st.overrides++;
		return (fenykl_rd8(FW_GPI_ONE_BUF) ^ 1u) & 1u;
	}
	return fenykl_rd8(FW_GPI_DRAW_BUF) & 1u;
}

static void hud_draw_shown(struct fenykl_hud_elem *e)
{
	uint32_t p = e->pub;

	if (!(p & HUD_PUB_SHOWN))
		return;
	/* the first copy afterwards finds the GUI's repaint: no erase; ours[] and sum[] stay */
	if (!fenykl_hud_visible()) {
		ho.st.hidden++;
		e->written[0] = HUD_NOT_WRITTEN;
		e->written[1] = HUD_NOT_WRITTEN;
		return;
	}
	unsigned int b = hud_shown_buffer();

	if (e->written[b] == p && !hud_box_intact(e, ho.canvas[b], e->bmp[p & HUD_PUB_INDEX]))
		ho.st.erased++;
	hud_put_box(e, b, p);
	ho.st.drawn++;
	if (e->pub != p)
		e->stale = 1;
}

void fenykl_hud_frame(int show_front)
{
	ho.st.updates++;
	if (FENYKL_CORE_ID() != 0) {
		ho.st.cpu1++;
		return;
	}
	/* the firmware tests D0 after exth */
	if ((int16_t)show_front != 0)
		return;
	ho.st.shows++;
	for (unsigned int i = 0; i < hud.count; i++)
		hud_draw_shown(hud.elem[i]);
}

/* --- the task side --- */

/* Draws e into both buffers while visible: the front shows the change at once, the hook redraws at
   every flip anyway. Visibility is re-checked per buffer to narrow the race with a GUI screen change
   (the task does not run in the GUI task). An unpublished element is cleared. */
static void hud_paint(struct fenykl_hud_elem *e)
{
	uint32_t p = e->pub;

	for (unsigned int b = 0; b < 2; b++) {
		if (!fenykl_hud_visible())
			return;
		if (p & HUD_PUB_SHOWN) {
			hud_put_box(e, b, p);
			continue;
		}
		hud_clear_box(e, ho.canvas[b]);
		e->ours[b] = 0;
		e->written[b] = HUD_NOT_WRITTEN;
	}
}

/* While hidden: the GUI clears only its own items on a screen change, so this clears e's box in
   each buffer that still holds it as copied. A box the GUI painted over (menu, Super Control
   Panel), even in part, is left alone. Either way the buffer no longer holds e. The check and the
   clear run with interrupts masked, so that the GUI task cannot paint between them. */
static void hud_wipe(struct fenykl_hud_elem *e)
{
	for (unsigned int b = 0; b < 2; b++) {
		if (!e->ours[b])
			continue;
		if (fenykl_hud_visible())
			return;
		uint32_t psw = fenykl_irq_save();
		bool intact = hud_box_sum(e, ho.canvas[b]) == e->sum[b];

		if (intact)
			hud_clear_box(e, ho.canvas[b]);
		fenykl_irq_restore(psw);
		if (intact)
			ho.st.wiped++;
		else
			ho.st.covered++;
		e->ours[b] = 0;
		e->written[b] = HUD_NOT_WRITTEN;
	}
}

/* The buffers do not hold what e's pub says: a box left from a hide, or none after a wipe. */
static bool hud_out_of_date(const struct fenykl_hud_elem *e)
{
	uint8_t shown = e->pub & HUD_PUB_SHOWN ? 1 : 0;

	return e->ours[0] != shown || e->ours[1] != shown;
}

/* Publishes e's spare bitmap idx, just rendered, and paints it. */
void hud_show(struct fenykl_hud_elem *e, uint32_t idx)
{
	hud_publish(e, HUD_PUB_SHOWN | idx);
	e->clears = 0;
	hud_paint(e);
}

void fenykl_hud_hide(struct fenykl_hud_elem *e)
{
	hud_publish(e, e->pub & HUD_PUB_INDEX);
	e->clears = HUD_HIDE_CLEARS;
	hud_paint(e);
}

/* The wipe starts after HUD_WIPE_TICKS hidden so that short hidden windows (~0.3 s after a
   screenshot) do not blink, and then runs at every hidden tick: a visible blip between two ticks
   may let the hook copy a box again. A visible tick repaints an element out of date, as no GUI
   flip may follow. */
void fenykl_hud_tick(void)
{
	bool visible = fenykl_hud_visible();

	if (visible)
		ho.hidden_ticks = 0;
	else if (ho.hidden_ticks < HUD_WIPE_TICKS)
		ho.hidden_ticks++;
	for (unsigned int i = 0; i < hud.count; i++) {
		struct fenykl_hud_elem *e = hud.elem[i];

		if (ho.hidden_ticks == HUD_WIPE_TICKS)
			hud_wipe(e);
		if (e->stale) {
			e->stale = 0;
			ho.st.stale++;
			hud_paint(e);
		} else if (e->clears) {
			e->clears--;
			hud_paint(e);
		} else if (visible && hud_out_of_date(e)) {
			hud_paint(e);
		}
	}
}

/* --- setup --- */

static int hud_check_buf(unsigned int b, struct fenykl_hud_buf *out)
{
	uint32_t p = fenykl_rd32(FW_GPI_LAYER_VRAM + 4 * (HUD_LAYER * 2 + b));

	*out = (struct fenykl_hud_buf){ .handle = p };
	if (HUD_HANDLE_BAD(p) || (p & 3))
		return FENYKL_HUD_EHANDLE;
	const volatile struct hud_osd_vram *v = (const volatile struct hud_osd_vram *)(uintptr_t)p;
	uint32_t a = v->pdata;

	out->plane = v->plane;
	out->width = v->width;
	out->height = v->height;
	out->pdata = a;
	if (v->plane != HUD_PLANE || HUD_SIZE_BAD(v->width, v->height))
		return FENYKL_HUD_EPLANE;
	/* cached or uncached DRAM outside the firmware image and the payload */
	uint32_t off = HUD_DRAM_OFF(a), end = off + HUD_PITCH * HUD_ROWS;

	if (!fenykl_dram_ptr(a, 4) ||
	    (end > HUD_DRAM_OFF(FW_IMAGE_START) &&
	     (off < HUD_DRAM_OFF(FW_IMAGE_END) || end > FW_DRAM_END - FW_DRAM_START)))
		return FENYKL_HUD_EPDATA;
	return 0;
}

int fenykl_hud_init(uint32_t states, struct fenykl_hud_buf buf[2])
{
	int r = hud_check_buf(0, &buf[0]);

	if (r || (r = hud_check_buf(1, &buf[1])))
		return r;
	uint32_t a = HUD_DRAM_OFF(buf[0].pdata), b = HUD_DRAM_OFF(buf[1].pdata);

	if (a < b + HUD_PITCH * HUD_ROWS && b < a + HUD_PITCH * HUD_ROWS)
		return FENYKL_HUD_EOVERLAP;
	if (fenykl_rd8(FW_GPI_LAYER_BPP + HUD_LAYER) != HUD_BPP)
		return FENYKL_HUD_EBPP;
	ho.canvas[0] = FENYKL_UNCACHED(buf[0].pdata);
	ho.canvas[1] = FENYKL_UNCACHED(buf[1].pdata);
	hud.states = states;
	hud.ready = true;
	return 0;
}

int hud_add(struct fenykl_hud_elem *e, unsigned int index)
{
	(void)index;
	e->bmp[0] = fw_malloc((unsigned int)(e->w * e->h));
	e->bmp[1] = fw_malloc((unsigned int)(e->w * e->h));
	if (!e->bmp[0] || !e->bmp[1]) {
		if (e->bmp[0])
			fw_free(e->bmp[0]);
		if (e->bmp[1])
			fw_free(e->bmp[1]);
		return FENYKL_HUD_ENOMEM;
	}
	e->written[0] = HUD_NOT_WRITTEN;
	e->written[1] = HUD_NOT_WRITTEN;
	e->ours[0] = 0;
	e->ours[1] = 0;
	e->stale = 0;
	e->clears = 0;
	return 0;
}

void fenykl_hud_get_stats(struct fenykl_hud_stats *st)
{
	*st = ho.st;
	st->nofont = hud.nofont;
	st->glyph_miss = hud.glyph_miss;
}
