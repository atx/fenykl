/*
 * Live-view HUD elements (sdk/docs/hud.md).
 * The GUI queues its drawing during a frame, executes the batch in gpi_flush and then shows the
 * buffer with cw_updateDisplay(0). The plugin's hook on cw_updateDisplay calls fenykl_hud_frame,
 * which copies each published element into the layer-1 buffer about to be shown, after the GUI's
 * batch. Rendering runs in the caller's task; the hook only reads and copies.
 */
#include <hud.h>

#define VRAM_TABLE 0x6066ED88u		/* gpi_layer_vram: struct osd_vram *[layer * 2 + buf] */
#define LAYER_BPP 0x6F1232C8u		/* u8 bpp[3] */
#define GPI_DRAW_BUF 0x6034D5B4u	/* u8: buffer cw_updateDisplay(0) shows */
/* cw_updateDisplay one-layer override: if MODE == 2 and LAYER == l, layer l shows b[BUF ^ 1] */
#define GPI_ONE_MODE 0x6034D5D2u	/* u8 */
#define GPI_ONE_LAYER 0x6034D5D4u	/* s16 */
#define GPI_ONE_BUF 0x6034D5D6u		/* u8 */
#define GPI_HIDE_MASK 0x6034D5D0u	/* u16, bit 0x10 hides layer 1 */
#define GPI_FONT_RES 0x6066EDACu	/* -> per-language font resource; +4 -> glyph_list, +0 u32 font id */
#define GUI_SUPER 0x60702278u		/* u8 gui_sm super state */
#define GUI_STATE 0x60702279u		/* u8 gui_sm state */
#define SS_REC 0x02
#define CORE_ID 0x8050u			/* 0 on CPU0 */

#define LAYER 1
#define PLANE 2
#define BPP 8
#define PITCH 896
#define ROWS 600
#define GLYPH_FMT 0x09
#define IMAGE_FMT 0x03
#define GUI_IMAGE_TABLE 0xAF966010u	/* common resource +0x10: -> u32 [id] -> struct gui_image */
#define IMAGE_BITMAP 0x10		/* struct gui_image: -> RLE bitmap */
#define LV_SPACE 11			/* unmapped character: half the line, as gui_imgstr_layout */
#define LV_IMAGE_H 22			/* stock live-view value images */
#define LV_IMAGE_TOP 1			/* line row of the images' row 0 */
#define LV_WHITE_BODY 0x04		/* the images' white body; 0x05, 0x06 its anti-alias shades */
#define LV_PAD 3			/* letter cell: bold 1 + outline 2, black between letters as stock */
#define LV_GLYPH_W 30			/* largest UI glyph cell the letter scratch takes */
#define LV_GLYPH_H 32
#define LV_SCALE_N 2			/* letters are the UI font scaled by N/D, smaller than the digits */
#define LV_SCALE_D 3
#define LV_SRC_TOP 1			/* glyph row offset: the baseline (row 25) ends a scaled row */
#define LV_INK_MIN (LV_SCALE_D * LV_SCALE_D)	/* code sum of 1/3 coverage and less: no ink */
#define LV_LETTER_W ((LV_GLYPH_W * LV_SCALE_N + LV_SCALE_D - 1) / LV_SCALE_D)
#define LV_LETTER_H (((LV_GLYPH_H + LV_SRC_TOP) * LV_SCALE_N + LV_SCALE_D - 1) / LV_SCALE_D)
#define LV_DROP 3			/* scaled baseline row 17 onto line row 20, the images' row 19 */

/* Helpers shared by the text path and fenykl_hud_image: inlined into each, so a plugin that only
   draws text keeps the code and size it had before fenykl_hud_image. */
#define INLINE static inline __attribute__((always_inline))

#define PUB_SHOWN 2u
#define PUB_INDEX 1u
#define NOT_WRITTEN 0xFFFFFFFFu		/* never a pub value: the sequence number does not get there */
#define HIDE_CLEARS 2u			/* clears by fenykl_hud_tick after the one in fenykl_hud_hide */

struct glyph {
	uint16_t w, h;			/* w = advance cell, h = 32 for Latin */
	const uint8_t *bitmap;
};

enum cell_kind {
	CELL_GLYPH,			/* FENYKL_HUD_PLAIN: UI font glyph */
	CELL_LETTER,			/* FENYKL_HUD_LV: UI font glyph, scaled, bold and outlined */
	CELL_IMAGE,			/* FENYKL_HUD_LV: stock image */
	CELL_SPACE,			/* FENYKL_HUD_LV: blank */
	CELL_NEWLINE,			/* '\n': the next line, FENYKL_HUD_*PITCH lower */
};

/* One laid-out character. */
struct cell {
	struct glyph g;
	uint8_t kind;
	int16_t adv;
};

struct osd_vram {
	uint32_t plane;
	uint16_t width, height;
	uint32_t pdata;
};

static struct {
	volatile uint8_t *canvas[2];
	uint32_t states;
	struct fenykl_hud_elem *elem[FENYKL_HUD_ELEMS_MAX];
	volatile unsigned int count;
	volatile struct fenykl_hud_stats st;
} hud;

static uint8_t rd8(uint32_t a)
{
	return *(volatile uint8_t *)(uintptr_t)a;
}

static uint16_t rd16(uint32_t a)
{
	return *(volatile uint16_t *)(uintptr_t)a;
}

static uint32_t rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

/* --- camera font --- */

static bool dram_pointer(uint32_t p)
{
	return (p & 3) == 0 && ((p >> 28) == 0x6 || (p >> 28) == 0xA);
}

/* Re-read at every render: a language change replaces the resource. */
uint32_t fenykl_hud_font(void)
{
	uint32_t res = rd32(GPI_FONT_RES);

	if (!dram_pointer(res))
		return 0;
	uint32_t list = rd32(res + 4);

	if (!dram_pointer(list))
		return 0;
	uint32_t id = rd32(list);

	return id >= 10 && id < 0x93 ? id : 0;	/* < 10: legacy icon path; 0x93 ends the list */
}

/* RLE bitmap header {h-1:12, w-1:12, fmt:8} */
static int bmp_h(const uint8_t *s)
{
	return (s[0] | (s[1] & 0x0F) << 8) + 1;
}

static int bmp_w(const uint8_t *s)
{
	return (s[1] >> 4 | s[2] << 4) + 1;
}

static bool get_glyph(uint32_t font, char c, struct glyph *g)
{
	const unsigned short key[3] = { (unsigned short)font, 0, (unsigned char)c };

	fw_gpi_get_glyph(key, g);
	if (g->w == 0 || !g->bitmap)
		return false;
	return g->bitmap[3] == GLYPH_FMT;
}

static bool is_digit(char c)
{
	return c >= '0' && c <= '9';
}

/* Advance after glyph c of cell width w followed by `next`, as gpi_char_advance_adj 0x6EFDD109 with
   the spacing table of all non-Thai fonts {-3, -1, -2, -3, -3} (font list +0x48) as constants. Its
   callers subtract the adjustment from the pen x (0x6EFDB25D, 0x6EFDD4B8), so the gap grows. */
static int advance(char c, char next, int w)
{
	int adj;

	if (!next || next == '\n')
		adj = 0;
	else if (is_digit(c))
		adj = next == '.' || next == ':' || next == '-' || next == '/' || next == '"' ? -2 : -1;
	else if (c == '+' && is_digit(next))
		adj = -2;
	else if (c == ' ')
		adj = -3 + 3;
	else
		adj = -3;
	return w - adj;
}

/* --- stock live-view images --- */

/* Image font 12 of the GUI (char -> image table 0x6F1B7908, the remaining-time digits), and '.'
   from image font 16 (0x6F1B8908), which is drawn in the same style. 0: not an image. */
static uint16_t lv_image_id(char c)
{
	if (c >= '1' && c <= '9')
		return (uint16_t)(1126 + (c - '0'));
	switch (c) {
	case '0':
		return 1136;
	case '-':
		return 1126;
	case ':':
		return 1137;
	case '+':
		return 1138;
	case '.':
		return 1933;
	default:
		return 0;
	}
}

/* Stock image id from the common resource, which is loaded once at boot and never moves
   (*0xAF966010 -> id table -> record +0x10); false if missing or not fmt 0x03. */
INLINE bool stock_image(uint16_t id, struct glyph *g)
{
	uint32_t table = rd32(GUI_IMAGE_TABLE);

	if (!dram_pointer(table))
		return false;
	uint32_t rec = rd32(table + 4u * id);

	if (!dram_pointer(rec))
		return false;
	uint32_t bmp = rd32(rec + IMAGE_BITMAP);

	if (!dram_pointer(bmp))
		return false;
	g->bitmap = (const uint8_t *)(uintptr_t)bmp;
	g->w = (uint16_t)bmp_w(g->bitmap);
	g->h = (uint16_t)bmp_h(g->bitmap);
	return g->bitmap[3] == IMAGE_FMT;
}

static bool lv_image(uint16_t id, struct glyph *g)
{
	return stock_image(id, g) && g->h == LV_IMAGE_H;
}

static int lv_scaled(int n)
{
	return (n * LV_SCALE_N + LV_SCALE_D - 1) / LV_SCALE_D;
}

/* Fills k for character s[0] in style; 0 or FENYKL_HUD_E*. */
static int lay_char(uint8_t style, uint32_t font, const char *s, struct cell *k)
{
	uint16_t id = style == FENYKL_HUD_LV ? lv_image_id(*s) : 0;

	if (*s == '\n') {
		k->kind = CELL_NEWLINE;
		k->adv = 0;
		return 0;
	}
	if (id) {
		if (!lv_image(id, &k->g))
			return FENYKL_HUD_EGLYPH;
		k->kind = CELL_IMAGE;
		k->adv = (int16_t)k->g.w;
		return 0;
	}
	if (style == FENYKL_HUD_LV && *s == ' ') {
		k->kind = CELL_SPACE;
		k->adv = LV_SPACE;
		return 0;
	}
	if (!font)
		return FENYKL_HUD_ENOFONT;
	if (!get_glyph(font, *s, &k->g))
		return FENYKL_HUD_EGLYPH;
	if (style != FENYKL_HUD_LV) {
		k->kind = CELL_GLYPH;
		k->adv = (int16_t)advance(s[0], s[1], k->g.w);
		return 0;
	}
	if (bmp_w(k->g.bitmap) > LV_GLYPH_W || bmp_h(k->g.bitmap) > LV_GLYPH_H)
		return FENYKL_HUD_EGLYPH;
	k->kind = CELL_LETTER;
	k->adv = (int16_t)(lv_scaled(k->g.w) + LV_PAD);
	return 0;
}

/* Lays out s into k[] and returns the width of its widest line or FENYKL_HUD_E*. */
static int layout(uint8_t style, const char *s, struct cell *k)
{
	uint32_t font = fenykl_hud_font();
	int width = 0, line = 0;

	for (int i = 0; s[i]; i++) {
		if (i == FENYKL_HUD_TEXT_MAX)
			return FENYKL_HUD_EGLYPH;
		int r = lay_char(style, font, &s[i], &k[i]);

		if (r)
			return r;
		line = k[i].kind == CELL_NEWLINE ? 0 : line + k[i].adv;
		if (line > width)
			width = line;
	}
	return width;
}

int fenykl_hud_text_width(uint8_t style, const char *s)
{
	struct cell k[FENYKL_HUD_TEXT_MAX];

	return layout(style, s, k);
}

/* --- rendering --- */

static void put(const struct fenykl_hud_elem *e, uint8_t *box, int x, int y, uint8_t v)
{
	if (x >= 0 && x < e->w && y >= 0 && y < e->h)
		box[y * e->w + x] = v;
}

/* Decodes a fmt 0x09 glyph (per byte colour = bits 7..6, run-1 = bits 5..0, rows top to bottom)
   into e's bitmap at x. Background codes are skipped, because the negative spacing makes
   neighbouring cells overlap. */
static void draw_glyph(const struct fenykl_hud_elem *e, const uint8_t *lut, uint8_t *box, int x,
		       int y, const struct glyph *g)
{
	const uint8_t *s = g->bitmap;
	int h = bmp_h(s), w = bmp_w(s);

	s += 4;
	for (int row = 0, col = 0; row < h;) {
		unsigned int c = *s >> 6, run = (*s & 0x3Fu) + 1;

		s++;
		for (; run && row < h; run--) {
			if (c)
				put(e, box, x + col, y + FENYKL_HUD_MARGIN + row, lut[c]);
			if (++col == w)
				col = 0, row++;
		}
	}
}

/* 10-bit code k of a fmt 0x03 u64 {lo, hi} */
INLINE unsigned int code10(uint32_t lo, uint32_t hi, int k)
{
	int b = 10 * k;

	if (b >= 32)
		return hi >> (b - 32) & 0x3FFu;
	if (b > 22)
		return (lo >> b | hi << (32 - b)) & 0x3FFu;
	return lo >> b & 0x3FFu;
}

/* Decodes a fmt 0x03 image (six 10-bit codes per little-endian u64, colour = bits 9..2, run-1 =
   bits 1..0) into e's bitmap at x, row y + LV_IMAGE_TOP. The colours are layer-1 palette indices as
   the GUI draws these images; 0 is the transparent key. With ink, the white ramp of the LV images
   (LV_WHITE_BODY..+2) takes ink[2], ink[1], ink[0], as glyph codes 3, 2, 1. */
INLINE void draw_image(const struct fenykl_hud_elem *e, const uint8_t *ink, uint8_t *box, int x,
		       int y, const struct glyph *g)
{
	const uint32_t *q = (const uint32_t *)(g->bitmap + 4);
	int h = bmp_h(g->bitmap), w = bmp_w(g->bitmap);
	uint32_t lo = 0, hi = 0;

	for (int row = 0, col = 0, k = 6; row < h; k++) {
		if (k == 6) {
			lo = q[0];
			hi = q[1];
			q += 2;
			k = 0;
		}
		unsigned int code = code10(lo, hi, k), c = code >> 2, run = (code & 3u) + 1;

		if (ink && c >= LV_WHITE_BODY && c <= LV_WHITE_BODY + 2u)
			c = ink[LV_WHITE_BODY + 2u - c];
		for (; run && row < h; run--) {
			if (c)
				put(e, box, x + col, y + LV_IMAGE_TOP + row, (uint8_t)c);
			if (++col == w)
				col = 0, row++;
		}
	}
}

int fenykl_hud_image(uint16_t id, uint8_t *px, int max, int *w, int *h)
{
	struct glyph g;

	if (!stock_image(id, &g) || g.w * g.h > max)
		return FENYKL_HUD_EGLYPH;
	/* draw_image's clipping box: the image itself */
	const struct fenykl_hud_elem box = { .w = (int16_t)g.w, .h = (int16_t)g.h };

	memset(px, 0, (size_t)(g.w * g.h));
	draw_image(&box, NULL, px, 0, -LV_IMAGE_TOP, &g);
	*w = g.w;
	*h = g.h;
	return 0;
}

/* Glyph codes of the letter being drawn, and its scaled bold codes with a spare column for the
   bold. */
static uint8_t lv_src[LV_GLYPH_H][LV_GLYPH_W];
static uint8_t lv_code[LV_LETTER_H][LV_LETTER_W + 1];

static void lv_decode(const struct glyph *g)
{
	const uint8_t *s = g->bitmap;
	int h = bmp_h(s), w = bmp_w(s);

	s += 4;
	for (int row = 0, col = 0; row < h;) {
		uint8_t c = *s >> 6;
		unsigned int run = (*s & 0x3Fu) + 1;

		s++;
		for (; run && row < h; run--) {
			lv_src[row][col] = c;
			if (++col == w)
				col = 0, row++;
		}
	}
}

/* Code sum of scaled pixel (x, y): the glyph on a grid N times finer, D x D cells of it. */
static unsigned int lv_cover(int x, int y, int w, int h)
{
	unsigned int sum = 0;

	for (int sy = y * LV_SCALE_D; sy < (y + 1) * LV_SCALE_D; sy++) {
		int row = sy / LV_SCALE_N - LV_SRC_TOP;

		if (row < 0 || row >= h)
			continue;
		for (int sx = x * LV_SCALE_D; sx < (x + 1) * LV_SCALE_D && sx / LV_SCALE_N < w; sx++)
			sum += lv_src[row][sx / LV_SCALE_N];
	}
	return sum;
}

/* Scales the decoded glyph by N/D (box filter), drops pixels of 1/3 coverage or less so that a
   one-row gap, as under the dot of i, stays open, and makes it bold: each pixel also inks the
   pixel right of it. */
static void lv_scale(const struct glyph *g)
{
	int h = bmp_h(g->bitmap), w = bmp_w(g->bitmap);

	memset(lv_code, 0, sizeof(lv_code));
	for (int y = 0; y < LV_LETTER_H; y++) {
		for (int x = 0; x < lv_scaled(w); x++) {
			unsigned int sum = lv_cover(x, y, w, h);
			uint8_t c = (uint8_t)((sum + LV_SCALE_D * LV_SCALE_D / 2) /
					      (LV_SCALE_D * LV_SCALE_D));

			if (sum <= LV_INK_MIN)
				continue;
			if (c > lv_code[y][x])
				lv_code[y][x] = c;
			lv_code[y][x + 1] = c;
		}
	}
}

static unsigned int lv_at(int x, int y)
{
	if (x < 0 || y < 0 || x > LV_LETTER_W || y >= LV_LETTER_H)
		return 0;
	return lv_code[y][x];
}

/* The stock items' outline around ink [camera]: black beside it, translucent
   diagonal to it, faint up to two pixels right of and below it, else 0 (nothing). */
static uint8_t lv_edge(int x, int y)
{
	if (lv_at(x - 1, y) || lv_at(x + 1, y) || lv_at(x, y - 1) || lv_at(x, y + 1))
		return FENYKL_HUD_BLACK;
	if (lv_at(x - 1, y - 1) || lv_at(x + 1, y - 1) || lv_at(x - 1, y + 1) || lv_at(x + 1, y + 1))
		return FENYKL_HUD_TRANSLUCENT;
	if (lv_at(x - 2, y - 1) || lv_at(x - 1, y - 2) || lv_at(x - 2, y - 2))
		return FENYKL_HUD_FAINT;
	return 0;
}

/* A UI font glyph in the LV style: scaled, bold, outlined, its baseline on the images' baseline,
   ink one pixel right of the cell start x. */
static void draw_letter(const struct fenykl_hud_elem *e, const uint8_t *lut, uint8_t *box, int x,
			int y, const struct glyph *g)
{
	int w = lv_scaled(bmp_w(g->bitmap)) + 1;

	lv_decode(g);
	lv_scale(g);
	for (int cy = -1; cy < LV_LETTER_H + 2; cy++) {
		for (int cx = -1; cx < w + 2; cx++) {
			unsigned int c = lv_at(cx, cy);
			uint8_t v = c ? lut[c] : lv_edge(cx, cy);

			if (v)
				put(e, box, x + 1 + cx, y + cy + LV_DROP, v);
		}
	}
}

/* Pen x of the line that starts at s, k: its width placed by e's alignment. */
static int line_x(const struct fenykl_hud_elem *e, const char *s, const struct cell *k, int margin)
{
	int width = 0;

	for (int i = 0; s[i] && s[i] != '\n'; i++)
		width += k[i].adv;
	if (e->align == FENYKL_HUD_CENTER)
		return (e->w - width) / 2;
	if (e->align == FENYKL_HUD_RIGHT)
		return e->w - margin - width;
	return margin;
}

static int render(const struct fenykl_hud_elem *e, uint32_t *bmp, const char *s)
{
	struct cell k[FENYKL_HUD_TEXT_MAX];
	int width = layout(e->style, s, k);

	if (width < 0)
		return width;
	const uint8_t lut[4] = { e->bg, e->ink[0], e->ink[1], e->ink[2] };
	bool lv = e->style == FENYKL_HUD_LV;
	int margin = lv ? FENYKL_HUD_LV_MARGIN : FENYKL_HUD_MARGIN;
	uint8_t *box = (uint8_t *)bmp;
	int x = line_x(e, s, k, margin), y = 0;

	memset(box, e->bg, (size_t)e->w * e->h);
	for (int i = 0; s[i]; i++) {
		if (k[i].kind == CELL_NEWLINE) {
			y += lv ? FENYKL_HUD_LV_PITCH : FENYKL_HUD_PITCH;
			x = line_x(e, &s[i + 1], &k[i + 1], margin);
			continue;
		}
		if (k[i].kind == CELL_GLYPH)
			draw_glyph(e, lut, box, x, y, &k[i].g);
		else if (k[i].kind == CELL_LETTER)
			draw_letter(e, lut, box, x, y, &k[i].g);
		else if (k[i].kind == CELL_IMAGE)
			draw_image(e, e->ink, box, x, y, &k[i].g);
		x += k[i].adv;
	}
	return 0;
}

/* --- copies into layer 1 --- */

static volatile uint32_t *row_of(const struct fenykl_hud_elem *e, volatile uint8_t *c, int r)
{
	return (volatile uint32_t *)(c + (e->y + r) * PITCH + e->x);
}

static void copy_box(const struct fenykl_hud_elem *e, volatile uint8_t *c, const uint32_t *bmp)
{
	unsigned int words = (unsigned int)e->w / 4;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *dst = row_of(e, c, r);
		const uint32_t *src = bmp + r * words;

		for (unsigned int x = 0; x < words; x++)
			dst[x] = src[x];
	}
}

static bool box_intact(const struct fenykl_hud_elem *e, volatile uint8_t *c, const uint32_t *bmp)
{
	unsigned int words = (unsigned int)e->w / 4;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *row = row_of(e, c, r);
		const uint32_t *src = bmp + r * words;

		for (unsigned int x = 0; x < words; x++)
			if (row[x] != src[x])
				return false;
	}
	return true;
}

static void clear_box(const struct fenykl_hud_elem *e, volatile uint8_t *c)
{
	unsigned int words = (unsigned int)e->w / 4;

	for (int r = 0; r < e->h; r++) {
		volatile uint32_t *row = row_of(e, c, r);

		for (unsigned int x = 0; x < words; x++)
			row[x] = FENYKL_HUD_CLEAR * 0x01010101u;
	}
}

/* --- visibility --- */

bool fenykl_hud_visible(void)
{
	if (rd16(GPI_HIDE_MASK) & 0x10)
		return false;
	if (rd8(GUI_SUPER) != SS_REC)
		return false;
	unsigned int state = rd8(GUI_STATE);

	return state < 32 && (hud.states & FENYKL_HUD_STATE(state));
}

/* --- the hook side (GUI task, CPU0) --- */

/* The layer-1 buffer cw_updateDisplay(0) shows (0x6EFDB6E3). */
static unsigned int shown_buffer(void)
{
	if (rd8(GPI_ONE_MODE) == 2 && (int16_t)rd16(GPI_ONE_LAYER) == LAYER) {
		hud.st.overrides++;
		return (rd8(GPI_ONE_BUF) ^ 1u) & 1u;
	}
	return rd8(GPI_DRAW_BUF) & 1u;
}

static void draw_shown(struct fenykl_hud_elem *e)
{
	uint32_t p = e->pub;

	if (!(p & PUB_SHOWN))
		return;
	if (!fenykl_hud_visible()) {
		hud.st.hidden++;
		e->written[0] = NOT_WRITTEN;
		e->written[1] = NOT_WRITTEN;
		return;
	}
	unsigned int b = shown_buffer();
	const uint32_t *bmp = e->bmp[p & PUB_INDEX];

	if (e->written[b] == p && !box_intact(e, hud.canvas[b], bmp))
		hud.st.erased++;
	copy_box(e, hud.canvas[b], bmp);
	e->written[b] = p;
	hud.st.drawn++;
	if (e->pub != p)
		e->stale = 1;
}

void fenykl_hud_frame(int show_front)
{
	hud.st.updates++;
	if (rd32(CORE_ID) != 0) {
		hud.st.cpu1++;
		return;
	}
	/* the firmware tests D0 after exth */
	if ((int16_t)show_front != 0)
		return;
	hud.st.shows++;
	for (unsigned int i = 0; i < hud.count; i++)
		draw_shown(hud.elem[i]);
}

/* --- the task side --- */

/* Draws e into both buffers while visible: the front shows the change at once, the hook redraws at
   every flip anyway. Visibility is re-checked per buffer to narrow the race with a GUI screen change
   (the task does not run in the GUI task). An unpublished element is cleared. */
static void paint(struct fenykl_hud_elem *e)
{
	uint32_t p = e->pub;

	for (unsigned int b = 0; b < 2; b++) {
		if (!fenykl_hud_visible())
			return;
		if (p & PUB_SHOWN)
			copy_box(e, hud.canvas[b], e->bmp[p & PUB_INDEX]);
		else
			clear_box(e, hud.canvas[b]);
		e->written[b] = p & PUB_SHOWN ? p : NOT_WRITTEN;
	}
}

static void publish(struct fenykl_hud_elem *e, uint32_t flags)
{
	uint32_t p = e->pub;

	e->pub = ((p >> 2) + 1) << 2 | flags;
}

/* Publishes e's spare bitmap idx, just rendered, and paints it. */
static void show(struct fenykl_hud_elem *e, uint32_t idx)
{
	publish(e, PUB_SHOWN | idx);
	e->clears = 0;
	paint(e);
}

int fenykl_hud_text(struct fenykl_hud_elem *e, const char *s)
{
	uint32_t idx = (e->pub & PUB_INDEX) ^ 1u;
	int r = render(e, e->bmp[idx], s);

	if (r == FENYKL_HUD_ENOFONT)
		hud.st.nofont++;
	if (r == FENYKL_HUD_EGLYPH)
		hud.st.glyph_miss++;
	if (r)
		return r;
	show(e, idx);
	return 0;
}

int fenykl_hud_draw(struct fenykl_hud_elem *e,
		    int (*fn)(uint8_t *box, int w, int h, void *arg), void *arg)
{
	uint32_t idx = (e->pub & PUB_INDEX) ^ 1u;
	uint8_t *box = (uint8_t *)e->bmp[idx];

	memset(box, e->bg, (size_t)e->w * e->h);
	int r = fn(box, e->w, e->h, arg);

	if (r)
		return r;
	show(e, idx);
	return 0;
}

void fenykl_hud_hide(struct fenykl_hud_elem *e)
{
	publish(e, e->pub & PUB_INDEX);
	e->clears = HIDE_CLEARS;
	paint(e);
}

void fenykl_hud_tick(void)
{
	for (unsigned int i = 0; i < hud.count; i++) {
		struct fenykl_hud_elem *e = hud.elem[i];

		if (e->stale) {
			e->stale = 0;
			hud.st.stale++;
			paint(e);
		} else if (e->clears) {
			e->clears--;
			paint(e);
		}
	}
}

/* --- setup (as focushud) --- */

static int check_buf(unsigned int b, struct fenykl_hud_buf *out)
{
	uint32_t p = rd32(VRAM_TABLE + 4 * (LAYER * 2 + b));

	*out = (struct fenykl_hud_buf){ .handle = p };
	if (p < 0x60000000u || p >= 0x6E600000u || (p & 3))
		return FENYKL_HUD_EHANDLE;
	const volatile struct osd_vram *v = (const volatile struct osd_vram *)(uintptr_t)p;
	uint32_t a = v->pdata;

	out->plane = v->plane;
	out->width = v->width;
	out->height = v->height;
	out->pdata = a;
	if (v->plane != PLANE || v->width != PITCH || v->height != ROWS)
		return FENYKL_HUD_EPLANE;
	/* cached or uncached DRAM outside the firmware image and the payload */
	uint32_t off = a & 0x0FFFFFFFu, end = off + PITCH * ROWS;

	if (!dram_pointer(a) || (end > 0x0E600000u && (off < 0x0F381400u || end > 0x10000000u)))
		return FENYKL_HUD_EPDATA;
	return 0;
}

int fenykl_hud_init(uint32_t states, struct fenykl_hud_buf buf[2])
{
	int r = check_buf(0, &buf[0]);

	if (r || (r = check_buf(1, &buf[1])))
		return r;
	uint32_t a = buf[0].pdata & 0x3FFFFFFFu, b = buf[1].pdata & 0x3FFFFFFFu;

	if (a < b + PITCH * ROWS && b < a + PITCH * ROWS)
		return FENYKL_HUD_EOVERLAP;
	if (rd8(LAYER_BPP + LAYER) != BPP)
		return FENYKL_HUD_EBPP;
	hud.canvas[0] = FENYKL_UNCACHED(buf[0].pdata);
	hud.canvas[1] = FENYKL_UNCACHED(buf[1].pdata);
	hud.states = states;
	return 0;
}

int fenykl_hud_add(struct fenykl_hud_elem *e)
{
	if (e->x < 0 || e->y < 0 || e->w <= 0 || e->h <= 0 || e->x % 4 || e->w % 4 ||
	    e->x + e->w > FENYKL_HUD_CANVAS_W || e->y + e->h > FENYKL_HUD_CANVAS_H ||
	    e->w * e->h > FENYKL_HUD_BITMAP_MAX)
		return FENYKL_HUD_ERECT;
	if (!hud.canvas[0])
		return FENYKL_HUD_EINIT;
	if (hud.count == FENYKL_HUD_ELEMS_MAX)
		return FENYKL_HUD_EFULL;
	e->bmp[0] = fw_malloc((unsigned int)(e->w * e->h));
	e->bmp[1] = fw_malloc((unsigned int)(e->w * e->h));
	if (!e->bmp[0] || !e->bmp[1]) {
		fw_free(e->bmp[0]);
		fw_free(e->bmp[1]);
		return FENYKL_HUD_ENOMEM;
	}
	e->pub = 0;
	e->written[0] = NOT_WRITTEN;
	e->written[1] = NOT_WRITTEN;
	e->stale = 0;
	e->clears = 0;
	hud.elem[hud.count] = e;
	__asm__ volatile("" ::: "memory");	/* the hook sees the element before the count */
	hud.count++;
	return 0;
}

void fenykl_hud_get_stats(struct fenykl_hud_stats *st)
{
	*st = hud.st;
}
