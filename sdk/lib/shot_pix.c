/*
 * The screenshot's pixel work and rules (shot_int.h).
 */
#include "shot_int.h"

/* BT.601 full range, 16.16 fixed point */
#define SHOT_K_RV 91881		/* 1.402 */
#define SHOT_K_GU 22554		/* 0.344136 */
#define SHOT_K_GV 46802		/* 0.714136 */
#define SHOT_K_BU 116130		/* 1.772 */
#define SHOT_HALF 32768

static uint8_t shot_clamp(int32_t v)
{
	v >>= 16;
	if (v < 0)
		return 0;
	return v > 255 ? 255 : (uint8_t)v;
}

void fenykl_shot_ycc_rgb(unsigned int y, unsigned int cb, unsigned int cr, uint8_t *rgb)
{
	int32_t l = (int32_t)(y << 16) + SHOT_HALF;
	int32_t u = (int32_t)cb - 128, v = (int32_t)cr - 128;

	rgb[0] = shot_clamp(l + SHOT_K_RV * v);
	rgb[1] = shot_clamp(l - SHOT_K_GU * u - SHOT_K_GV * v);
	rgb[2] = shot_clamp(l + SHOT_K_BU * u);
}

/* E-PL3: {Y, Cb, Cr, alpha 0..15 in the low nibble}; E-M10 II: {R, G, B, alpha 0..32}. */
void fenykl_shot_palette(const uint8_t *src, unsigned int n, unsigned int global, uint8_t *rgba)
{
	if (global > 0x100)
		global = 0x100;
	for (unsigned int i = 0; i < n; i++, src += 4, rgba += 4) {
		unsigned int a;

		if (FENYKL_OSD_PAL_YCC) {
			fenykl_shot_ycc_rgb(src[0], src[1], src[2], rgba);
			a = src[3] & FENYKL_SHOT_ALPHA_MAX;
		} else {
			rgba[0] = src[0];
			rgba[1] = src[1];
			rgba[2] = src[2];
			a = src[3] < FENYKL_SHOT_ALPHA_MAX ? src[3] : FENYKL_SHOT_ALPHA_MAX;
		}
		rgba[3] = (uint8_t)(a * global >> 8);
	}
}

static void shot_lv(const struct fenykl_shot_frame *f, unsigned int y, uint8_t *rgb)
{
	const uint8_t *s = f->lv + (y - f->lv_y) * f->lv_pitch;
	uint8_t *d = rgb + 3 * f->lv_x;

	for (unsigned int x = 0; x < f->lv_w; x += 2, s += 4, d += 6) {
		fenykl_shot_ycc_rgb(s[0], s[1], s[3], d);
		fenykl_shot_ycc_rgb(s[2], s[1], s[3], d + 3);
	}
}

static void shot_blend(uint8_t *d, const uint8_t *c)
{
	unsigned int a = c[3];

	if (!a)
		return;
	if (a == FENYKL_SHOT_ALPHA_MAX) {
		d[0] = c[0];
		d[1] = c[1];
		d[2] = c[2];
		return;
	}
	for (unsigned int i = 0; i < 3; i++)
		d[i] = (uint8_t)((c[i] * a + d[i] * (FENYKL_SHOT_ALPHA_MAX - a)) / FENYKL_SHOT_ALPHA_MAX);
}

/* Canvas row y of w pixels from the nearest source pixels: sx = x * l->w / w, without a divide. */
static void shot_layer(const struct fenykl_shot_layer *l, unsigned int y, unsigned int w,
		       unsigned int h, uint8_t *rgb)
{
	const uint8_t *s = l->pix + y * l->h / h * l->pitch;
	unsigned int sx = 0, err = 0;

	for (unsigned int x = 0; x < w; x++, rgb += 3) {
		unsigned int i = s[sx * l->bpp / 8];

		if (l->bpp == 4)
			i = sx & 1 ? i >> 4 : i & 0x0F;
		if (i < l->entries)
			shot_blend(rgb, l->pal + 4 * i);
		for (err += l->w; err >= w; err -= w)
			sx++;
	}
}

/* Nearest source pixel, like the display's scaler stretching w x h onto to_w x to_h. */
static void shot_frames(const struct fenykl_shot_frames *fr, unsigned int y, uint8_t *rgb)
{
	const uint8_t *s = fr->pix + y * fr->h / fr->to_h * fr->pitch;

	for (unsigned int x = 0; x < fr->to_w; x++, rgb += 3) {
		unsigned int i = s[x * fr->w / fr->to_w];

		if (!i || i >= fr->entries)
			continue;
		rgb[0] = fr->pal[4 * i];
		rgb[1] = fr->pal[4 * i + 1];
		rgb[2] = fr->pal[4 * i + 2];
	}
}

void fenykl_shot_compose(const struct fenykl_shot_frame *f, unsigned int y, uint8_t *rgb)
{
	for (unsigned int i = 0; i < 3u * f->w; i++)
		rgb[i] = 0;
	if (f->lv && y >= f->lv_y && y < f->lv_y + f->lv_h)
		shot_lv(f, y, rgb);
	if (f->frames.pix && y < f->frames.to_h)
		shot_frames(&f->frames, y, rgb);
	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++)
		if (f->layer[l].pix)
			shot_layer(&f->layer[l], y, f->w, f->h, rgb);
}

void fenykl_shot_stretch(const uint8_t *in, unsigned int in_w, uint8_t *out, unsigned int out_w)
{
	/* source position of the output pixel centre, 16.16 */
	uint32_t step = (in_w << 16) / out_w;
	int32_t pos = (int32_t)(step / 2) - SHOT_HALF;

	for (unsigned int x = 0; x < out_w; x++, pos += (int32_t)step, out += 3) {
		int32_t p = pos < 0 ? 0 : pos;
		unsigned int i = (unsigned int)p >> 16, w = (unsigned int)p & 0xFFFF;
		const uint8_t *a = in + 3 * i;
		const uint8_t *b = i + 1 < in_w ? a + 3 : a;

		for (unsigned int c = 0; c < 3; c++)
			out[c] = (uint8_t)((a[c] * (65536 - w) + b[c] * w + SHOT_HALF) >> 16);
	}
}

static char *shot_dec(char *p, unsigned int v)
{
	char tmp[10];
	unsigned int n = 0;

	do
		tmp[n++] = (char)('0' + v % 10);
	while (v /= 10);
	while (n)
		*p++ = tmp[--n];
	return p;
}

unsigned int fenykl_shot_ppm_header(char *out, unsigned int w, unsigned int h)
{
	char *p = out;

	*p++ = 'P';
	*p++ = '6';
	*p++ = '\n';
	p = shot_dec(p, w);
	*p++ = ' ';
	p = shot_dec(p, h);
	*p++ = '\n';
	p = shot_dec(p, 255);
	*p++ = '\n';
	return (unsigned int)(p - out);
}

void fenykl_shot_layout(const struct fenykl_disp_geom *g, struct fenykl_shot_layout *lay)
{
	static const uint8_t bpp[FENYKL_SHOT_LAYERS] = FENYKL_OSD_BPP;
	uint32_t off;

	lay->lv = 0;
	lay->lv_row = 2u * g->w;
	off = lay->lv_row * g->h;
	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++) {
		lay->osd[l] = off;
		lay->osd_row[l] = (uint32_t)g->osd_w * bpp[l] / 8;
		off += lay->osd_row[l] * g->osd_h;
	}
	lay->frames = off;
	lay->snap = off + FENYKL_FRAMES_PITCH * FENYKL_FRAMES_H;
	lay->strip = 3u * g->square_w * FENYKL_SHOT_STRIP_ROWS;
	lay->bytes = lay->snap + (FENYKL_SHOT_STRIP_LENT ? lay->strip : 0);
}

const char *fenykl_shot_gate(const struct fenykl_shot_cond *c, const struct fenykl_shot_cond *start)
{
	if (c->movie)
		return "movie mode";
	if (c->playback)
		return "playback";
	if (c->still_shared)
		return "stills go to the lent memory";
	if (c->other_used)
		return "a pool over the lent memory in use";
	if (start)
		return c->mode != start->mode ? "output changed" : NULL;
	if (!c->ready)
		return "display not ready";
	return c->pool_used ? "a picture in the lent pool" : NULL;
}
