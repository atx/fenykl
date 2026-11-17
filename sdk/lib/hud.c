/*
 * Live-view HUD elements (sdk/docs/hud.md), the part both cameras share: the elements, the camera's
 * font and the stock images, rendering into the elements' bitmaps. How a bitmap reaches the screen
 * is the camera part's (hud_<camera>.c: hud_show, fenykl_hud_hide, fenykl_hud_tick). Rendering runs
 * in the caller's task.
 */
#include "hud_int.h"

#define HUD_GLYPH_FMT 0x09
#define HUD_IMAGE_FMT 0x03
#define HUD_IMAGE_BITMAP 0x10		/* struct gui_image: -> RLE bitmap */
#define HUD_LV_SPACE 11			/* unmapped character: half the line, as gui_imgstr_layout */
#define HUD_LV_IMAGE_H 22		/* stock live-view value images */
#define HUD_LV_IMAGE_TOP 1		/* line row of the images' row 0 */
#define HUD_LV_WHITE_BODY 0x04		/* the images' white body; 0x05, 0x06 its anti-alias shades */
#define HUD_LV_PAD 3			/* letter cell: bold 1 + outline 2, black between letters as stock */
#define HUD_LV_GLYPH_W 30		/* largest UI glyph cell the letter scratch takes */
#define HUD_LV_GLYPH_H 32
#define HUD_LV_SCALE_N 2		/* letters are the UI font scaled by N/D, smaller than the digits */
#define HUD_LV_SCALE_D 3
#define HUD_LV_SRC_TOP 1		/* glyph row offset: the baseline (row 25) ends a scaled row */
#define HUD_LV_INK_MIN (HUD_LV_SCALE_D * HUD_LV_SCALE_D)	/* code sum of 1/3 coverage and less: no ink */
#define HUD_LV_LETTER_W ((HUD_LV_GLYPH_W * HUD_LV_SCALE_N + HUD_LV_SCALE_D - 1) / HUD_LV_SCALE_D)
#define HUD_LV_LETTER_H \
	(((HUD_LV_GLYPH_H + HUD_LV_SRC_TOP) * HUD_LV_SCALE_N + HUD_LV_SCALE_D - 1) / HUD_LV_SCALE_D)
#define HUD_LV_DROP 3			/* scaled baseline row 17 onto line row 20, the images' row 19 */

struct hud_glyph {
	uint16_t w, h;			/* w = advance cell, h = 32 for Latin */
	const uint8_t *bitmap;
};

enum hud_cell_kind {
	HUD_CELL_GLYPH,			/* FENYKL_HUD_PLAIN: UI font glyph */
	HUD_CELL_LETTER,		/* FENYKL_HUD_LV: UI font glyph, scaled, bold and outlined */
	HUD_CELL_IMAGE,			/* FENYKL_HUD_LV: stock image */
	HUD_CELL_SPACE,			/* FENYKL_HUD_LV: blank */
	HUD_CELL_NEWLINE,		/* '\n': the next line, FENYKL_HUD_*PITCH lower */
};

/* One laid-out character. */
struct hud_cell {
	struct hud_glyph g;
	uint8_t kind;
	int16_t adv;
};

struct hud_state hud;

/* --- camera font --- */

/* Re-read at every render: a language change replaces the resource. */
uint32_t fenykl_hud_font(void)
{
	uint32_t res = fenykl_rd32(FW_GPI_FONT_RES);

	if (!fenykl_dram_ptr(res, 4))
		return 0;
	uint32_t list = fenykl_rd32(res + 4);

	if (!fenykl_dram_ptr(list, 4))
		return 0;
	uint32_t id = fenykl_rd32(list);

	return id >= 10 && id < 0x93 ? id : 0;	/* < 10: legacy icon path; 0x93 ends the list */
}

/* RLE bitmap header {h-1:12, w-1:12, fmt:8} */
static int hud_bmp_h(const uint8_t *s)
{
	return (s[0] | (s[1] & 0x0F) << 8) + 1;
}

static int hud_bmp_w(const uint8_t *s)
{
	return (s[1] >> 4 | s[2] << 4) + 1;
}

static bool hud_get_glyph(uint32_t font, char c, struct hud_glyph *g)
{
	const unsigned short key[3] = { (unsigned short)font, 0, (unsigned char)c };

	fw_gpi_get_glyph(key, g);
	if (g->w == 0 || !g->bitmap)
		return false;
	return g->bitmap[3] == HUD_GLYPH_FMT;
}

static bool hud_is_digit(char c)
{
	return c >= '0' && c <= '9';
}

/* Advance after glyph c of cell width w followed by `next`, as gpi_char_advance_adj 0x6EFDD109 with
   the spacing table of all non-Thai fonts {-3, -1, -2, -3, -3} (font list +0x48) as constants. Its
   callers subtract the adjustment from the pen x (0x6EFDB25D, 0x6EFDD4B8), so the gap grows. */
static int hud_advance(char c, char next, int w)
{
	int adj;

	if (!next || next == '\n')
		adj = 0;
	else if (hud_is_digit(c))
		adj = next == '.' || next == ':' || next == '-' || next == '/' || next == '"' ? -2 : -1;
	else if (c == '+' && hud_is_digit(next))
		adj = -2;
	else if (c == ' ')
		adj = 0;
	else
		adj = -3;
	return w - adj;
}

/* --- stock live-view images --- */

/* The stock live-view value image of c (hud_int.h), 0: not an image. */
static uint16_t hud_lv_image_id(char c)
{
	if (c >= '1' && c <= '9')
		return (uint16_t)(HUD_LV_DIGITS + (c - '0'));
	switch (c) {
	case '0':
		return HUD_LV_DIGITS + 10;
	case '-':
		return HUD_LV_DIGITS;
	case ':':
		return HUD_LV_DIGITS + 11;
	case '+':
		return HUD_LV_DIGITS + 12;
	case '.':
		return HUD_LV_DOT;
	default:
		return 0;
	}
}

/* Stock image id from the common resource, which is loaded once at boot and never moves
   (*FW_GUI_IMAGE_TABLE -> id table -> record +0x10); false if missing or not fmt 0x03. */
static bool hud_stock_image(uint16_t id, struct hud_glyph *g)
{
	uint32_t table = fenykl_rd32(FW_GUI_IMAGE_TABLE);

	if (!fenykl_dram_ptr(table, 4))
		return false;
	uint32_t rec = fenykl_rd32(table + 4u * id);

	if (!fenykl_dram_ptr(rec, 4))
		return false;
	uint32_t bmp = fenykl_rd32(rec + HUD_IMAGE_BITMAP);

	if (!fenykl_dram_ptr(bmp, 4))
		return false;
	g->bitmap = (const uint8_t *)(uintptr_t)bmp;
	g->w = (uint16_t)hud_bmp_w(g->bitmap);
	g->h = (uint16_t)hud_bmp_h(g->bitmap);
	return g->bitmap[3] == HUD_IMAGE_FMT;
}

static bool hud_lv_image(uint16_t id, struct hud_glyph *g)
{
	return hud_stock_image(id, g) && g->h == HUD_LV_IMAGE_H;
}

static int hud_lv_scaled(int n)
{
	return (n * HUD_LV_SCALE_N + HUD_LV_SCALE_D - 1) / HUD_LV_SCALE_D;
}

/* Fills k for character s[0] in style; 0 or FENYKL_HUD_E*. */
static int hud_lay_char(uint8_t style, uint32_t font, const char *s, struct hud_cell *k)
{
	uint16_t id = style == FENYKL_HUD_LV ? hud_lv_image_id(*s) : 0;

	if (*s == '\n') {
		k->kind = HUD_CELL_NEWLINE;
		k->adv = 0;
		return 0;
	}
	if (id) {
		if (!hud_lv_image(id, &k->g))
			return FENYKL_HUD_EGLYPH;
		k->kind = HUD_CELL_IMAGE;
		k->adv = (int16_t)k->g.w;
		return 0;
	}
	if (style == FENYKL_HUD_LV && *s == ' ') {
		k->kind = HUD_CELL_SPACE;
		k->adv = HUD_LV_SPACE;
		return 0;
	}
	if (!font)
		return FENYKL_HUD_ENOFONT;
	if (!hud_get_glyph(font, *s, &k->g))
		return FENYKL_HUD_EGLYPH;
	if (style != FENYKL_HUD_LV) {
		k->kind = HUD_CELL_GLYPH;
		k->adv = (int16_t)hud_advance(s[0], s[1], k->g.w);
		return 0;
	}
	if (hud_bmp_w(k->g.bitmap) > HUD_LV_GLYPH_W || hud_bmp_h(k->g.bitmap) > HUD_LV_GLYPH_H)
		return FENYKL_HUD_EGLYPH;
	k->kind = HUD_CELL_LETTER;
	k->adv = (int16_t)(hud_lv_scaled(k->g.w) + HUD_LV_PAD);
	return 0;
}

/* Lays out s into k[] and returns the width of its widest line or FENYKL_HUD_E*. */
static int hud_layout(uint8_t style, const char *s, struct hud_cell *k)
{
	uint32_t font = fenykl_hud_font();
	int width = 0, line = 0;

	for (int i = 0; s[i]; i++) {
		if (i == FENYKL_HUD_TEXT_MAX)
			return FENYKL_HUD_EGLYPH;
		int r = hud_lay_char(style, font, &s[i], &k[i]);

		if (r)
			return r;
		line = k[i].kind == HUD_CELL_NEWLINE ? 0 : line + k[i].adv;
		if (line > width)
			width = line;
	}
	return width;
}

int fenykl_hud_text_width(uint8_t style, const char *s)
{
	struct hud_cell k[FENYKL_HUD_TEXT_MAX];

	return hud_layout(style, s, k);
}

/* --- rendering --- */

static void hud_put(const struct fenykl_hud_elem *e, uint8_t *box, int x, int y, uint8_t v)
{
	if (x >= 0 && x < e->w && y >= 0 && y < e->h)
		box[y * e->w + x] = v;
}

/* Decodes a fmt 0x09 glyph (per byte colour = bits 7..6, run-1 = bits 5..0, rows top to bottom)
   into e's bitmap at x. Background codes are skipped, because the negative spacing makes
   neighbouring cells overlap. */
static void hud_draw_glyph(const struct fenykl_hud_elem *e, const uint8_t *lut, uint8_t *box,
			   int x, int y, const struct hud_glyph *g)
{
	const uint8_t *s = g->bitmap;
	int h = hud_bmp_h(s), w = hud_bmp_w(s);

	s += 4;
	for (int row = 0, col = 0; row < h;) {
		unsigned int c = *s >> 6, run = (*s & 0x3Fu) + 1;

		s++;
		for (; run && row < h; run--) {
			if (c)
				hud_put(e, box, x + col, y + FENYKL_HUD_MARGIN + row, lut[c]);
			if (++col == w)
				col = 0, row++;
		}
	}
}

/* 10-bit code k of a fmt 0x03 u64 {lo, hi} */
static unsigned int hud_code10(uint32_t lo, uint32_t hi, int k)
{
	int b = 10 * k;

	if (b >= 32)
		return hi >> (b - 32) & 0x3FFu;
	if (b > 22)
		return (lo >> b | hi << (32 - b)) & 0x3FFu;
	return lo >> b & 0x3FFu;
}

/* Decodes a fmt 0x03 image (six 10-bit codes per little-endian u64, colour = bits 9..2, run-1 =
   bits 1..0) into e's bitmap at x, row y + HUD_LV_IMAGE_TOP. The colours are layer-1 palette
   indices as the GUI draws these images; 0 is the transparent key. With ink, the white ramp of the
   LV images (HUD_LV_WHITE_BODY..+2) takes ink[2], ink[1], ink[0], as glyph codes 3, 2, 1. */
static void hud_draw_image(const struct fenykl_hud_elem *e, const uint8_t *ink, uint8_t *box,
			   int x, int y, const struct hud_glyph *g)
{
	const uint32_t *q = (const uint32_t *)(g->bitmap + 4);
	int h = hud_bmp_h(g->bitmap), w = hud_bmp_w(g->bitmap);
	uint32_t lo = 0, hi = 0;

	for (int row = 0, col = 0, k = 6; row < h; k++) {
		if (k == 6) {
			lo = q[0];
			hi = q[1];
			q += 2;
			k = 0;
		}
		unsigned int code = hud_code10(lo, hi, k), c = code >> 2, run = (code & 3u) + 1;

		if (ink && c >= HUD_LV_WHITE_BODY && c <= HUD_LV_WHITE_BODY + 2u)
			c = ink[HUD_LV_WHITE_BODY + 2u - c];
		for (; run && row < h; run--) {
			if (c)
				hud_put(e, box, x + col, y + HUD_LV_IMAGE_TOP + row, (uint8_t)c);
			if (++col == w)
				col = 0, row++;
		}
	}
}

int fenykl_hud_image(uint16_t id, uint8_t *px, int max, int *w, int *h)
{
	struct hud_glyph g;

	if (!hud_stock_image(id, &g) || g.w * g.h > max)
		return FENYKL_HUD_EGLYPH;
	/* hud_draw_image's clipping box: the image itself */
	const struct fenykl_hud_elem box = { .w = (int16_t)g.w, .h = (int16_t)g.h };

	memset(px, 0, (size_t)(g.w * g.h));
	hud_draw_image(&box, NULL, px, 0, -HUD_LV_IMAGE_TOP, &g);
	*w = g.w;
	*h = g.h;
	return 0;
}

/* Glyph codes of the letter being drawn, and its scaled bold codes with a spare column for the
   bold. */
static uint8_t hud_lv_src[HUD_LV_GLYPH_H][HUD_LV_GLYPH_W];
static uint8_t hud_lv_code[HUD_LV_LETTER_H][HUD_LV_LETTER_W + 1];

static void hud_lv_decode(const struct hud_glyph *g)
{
	const uint8_t *s = g->bitmap;
	int h = hud_bmp_h(s), w = hud_bmp_w(s);

	s += 4;
	for (int row = 0, col = 0; row < h;) {
		uint8_t c = *s >> 6;
		unsigned int run = (*s & 0x3Fu) + 1;

		s++;
		for (; run && row < h; run--) {
			hud_lv_src[row][col] = c;
			if (++col == w)
				col = 0, row++;
		}
	}
}

/* Code sum of scaled pixel (x, y): the glyph on a grid N times finer, D x D cells of it. */
static unsigned int hud_lv_cover(int x, int y, int w, int h)
{
	unsigned int sum = 0;

	for (int sy = y * HUD_LV_SCALE_D; sy < (y + 1) * HUD_LV_SCALE_D; sy++) {
		int row = sy / HUD_LV_SCALE_N - HUD_LV_SRC_TOP;

		if (row < 0 || row >= h)
			continue;
		for (int sx = x * HUD_LV_SCALE_D;
		     sx < (x + 1) * HUD_LV_SCALE_D && sx / HUD_LV_SCALE_N < w; sx++)
			sum += hud_lv_src[row][sx / HUD_LV_SCALE_N];
	}
	return sum;
}

/* Scales the decoded glyph by N/D (box filter), drops pixels of 1/3 coverage or less so that a
   one-row gap, as under the dot of i, stays open, and makes it bold: each pixel also inks the
   pixel right of it. */
static void hud_lv_scale(const struct hud_glyph *g)
{
	int h = hud_bmp_h(g->bitmap), w = hud_bmp_w(g->bitmap);

	memset(hud_lv_code, 0, sizeof(hud_lv_code));
	for (int y = 0; y < HUD_LV_LETTER_H; y++) {
		for (int x = 0; x < hud_lv_scaled(w); x++) {
			unsigned int sum = hud_lv_cover(x, y, w, h);
			uint8_t c = (uint8_t)((sum + HUD_LV_SCALE_D * HUD_LV_SCALE_D / 2) /
					      (HUD_LV_SCALE_D * HUD_LV_SCALE_D));

			if (sum <= HUD_LV_INK_MIN)
				continue;
			if (c > hud_lv_code[y][x])
				hud_lv_code[y][x] = c;
			hud_lv_code[y][x + 1] = c;
		}
	}
}

static unsigned int hud_lv_at(int x, int y)
{
	if (x < 0 || y < 0 || x > HUD_LV_LETTER_W || y >= HUD_LV_LETTER_H)
		return 0;
	return hud_lv_code[y][x];
}

/* The stock items' outline around ink [camera]: black beside it, translucent
   diagonal to it, faint up to two pixels right of and below it, else 0 (nothing). */
static uint8_t hud_lv_edge(int x, int y)
{
	if (hud_lv_at(x - 1, y) || hud_lv_at(x + 1, y) || hud_lv_at(x, y - 1) ||
	    hud_lv_at(x, y + 1))
		return FENYKL_HUD_BLACK;
	if (hud_lv_at(x - 1, y - 1) || hud_lv_at(x + 1, y - 1) || hud_lv_at(x - 1, y + 1) ||
	    hud_lv_at(x + 1, y + 1))
		return FENYKL_HUD_TRANSLUCENT;
	if (hud_lv_at(x - 2, y - 1) || hud_lv_at(x - 1, y - 2) || hud_lv_at(x - 2, y - 2))
		return FENYKL_HUD_FAINT;
	return 0;
}

/* A UI font glyph in the LV style: scaled, bold, outlined, its baseline on the images' baseline,
   ink one pixel right of the cell start x. */
static void hud_draw_letter(const struct fenykl_hud_elem *e, const uint8_t *lut, uint8_t *box,
			    int x, int y, const struct hud_glyph *g)
{
	int w = hud_lv_scaled(hud_bmp_w(g->bitmap)) + 1;

	hud_lv_decode(g);
	hud_lv_scale(g);
	for (int cy = -1; cy < HUD_LV_LETTER_H + 2; cy++) {
		for (int cx = -1; cx < w + 2; cx++) {
			unsigned int c = hud_lv_at(cx, cy);
			uint8_t v = c ? lut[c] : hud_lv_edge(cx, cy);

			if (v)
				hud_put(e, box, x + 1 + cx, y + cy + HUD_LV_DROP, v);
		}
	}
}

/* Pen x of the line that starts at s, k: its width placed by e's alignment. */
static int hud_line_x(const struct fenykl_hud_elem *e, const char *s, const struct hud_cell *k,
		      int margin)
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

static int hud_render(const struct fenykl_hud_elem *e, uint32_t *bmp, const char *s)
{
	struct hud_cell k[FENYKL_HUD_TEXT_MAX];
	int width = hud_layout(e->style, s, k);

	if (width < 0)
		return width;
	const uint8_t lut[4] = { e->bg, e->ink[0], e->ink[1], e->ink[2] };
	bool lv = e->style == FENYKL_HUD_LV;
	int margin = lv ? FENYKL_HUD_LV_MARGIN : FENYKL_HUD_MARGIN;
	uint8_t *box = (uint8_t *)bmp;
	int x = hud_line_x(e, s, k, margin), y = 0;

	memset(box, e->bg, (size_t)e->w * e->h);
	for (int i = 0; s[i]; i++) {
		if (k[i].kind == HUD_CELL_NEWLINE) {
			y += lv ? FENYKL_HUD_LV_PITCH : FENYKL_HUD_PITCH;
			x = hud_line_x(e, &s[i + 1], &k[i + 1], margin);
			continue;
		}
		if (k[i].kind == HUD_CELL_GLYPH)
			hud_draw_glyph(e, lut, box, x, y, &k[i].g);
		else if (k[i].kind == HUD_CELL_LETTER)
			hud_draw_letter(e, lut, box, x, y, &k[i].g);
		else if (k[i].kind == HUD_CELL_IMAGE)
			hud_draw_image(e, e->ink, box, x, y, &k[i].g);
		x += k[i].adv;
	}
	return 0;
}

/* --- the task side --- */

void hud_publish(struct fenykl_hud_elem *e, uint32_t flags)
{
	uint32_t p = e->pub;

	e->pub = ((p >> 2) + 1) << 2 | flags;
}

int fenykl_hud_text(struct fenykl_hud_elem *e, const char *s)
{
	uint32_t idx = (e->pub & HUD_PUB_INDEX) ^ 1u;
	int r = hud_render(e, e->bmp[idx], s);

	if (r == FENYKL_HUD_ENOFONT)
		hud.nofont++;
	if (r == FENYKL_HUD_EGLYPH)
		hud.glyph_miss++;
	if (r)
		return r;
	hud_show(e, idx);
	return 0;
}

int fenykl_hud_draw(struct fenykl_hud_elem *e,
		    int (*fn)(uint8_t *box, int w, int h, void *arg), void *arg)
{
	uint32_t idx = (e->pub & HUD_PUB_INDEX) ^ 1u;
	uint8_t *box = (uint8_t *)e->bmp[idx];

	memset(box, e->bg, (size_t)e->w * e->h);
	int r = fn(box, e->w, e->h, arg);

	if (r)
		return r;
	hud_show(e, idx);
	return 0;
}

/* --- setup --- */

int fenykl_hud_add(struct fenykl_hud_elem *e)
{
	if (e->x < 0 || e->y < 0 || e->w <= 0 || e->h <= 0 || e->x % 4 || e->w % 4 ||
	    e->x + e->w > FENYKL_HUD_CANVAS_W || e->y + e->h > FENYKL_HUD_CANVAS_H ||
	    e->w * e->h > FENYKL_HUD_BITMAP_MAX)
		return FENYKL_HUD_ERECT;
	if (!hud.ready)
		return FENYKL_HUD_EINIT;
	if (hud.count == FENYKL_HUD_ELEMS_MAX)
		return FENYKL_HUD_EFULL;
	int r = hud_add(e, hud.count);

	if (r)
		return r;
	e->pub = 0;
	hud.elem[hud.count] = e;
	__asm__ volatile("" ::: "memory");	/* the GUI task or hook sees the element before the count */
	hud.count++;
	return 0;
}
