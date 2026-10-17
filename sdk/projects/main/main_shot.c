/*
 * main: the screenshot's pixel work (main_shot.h).
 */
#include "main_shot.h"

/* BT.601 full range, 16.16 fixed point */
#define MAIN_SHOT_K_RV 91881		/* 1.402 */
#define MAIN_SHOT_K_GU 22554		/* 0.344136 */
#define MAIN_SHOT_K_GV 46802		/* 0.714136 */
#define MAIN_SHOT_K_BU 116130		/* 1.772 */
#define MAIN_SHOT_HALF 32768

static uint8_t main_shot_clamp(int32_t v)
{
	v >>= 16;
	if (v < 0)
		return 0;
	return v > 255 ? 255 : (uint8_t)v;
}

void main_shot_ycc_rgb(unsigned int y, unsigned int cb, unsigned int cr, uint8_t *rgb)
{
	int32_t l = (int32_t)(y << 16) + MAIN_SHOT_HALF;
	int32_t u = (int32_t)cb - 128, v = (int32_t)cr - 128;

	rgb[0] = main_shot_clamp(l + MAIN_SHOT_K_RV * v);
	rgb[1] = main_shot_clamp(l - MAIN_SHOT_K_GU * u - MAIN_SHOT_K_GV * v);
	rgb[2] = main_shot_clamp(l + MAIN_SHOT_K_BU * u);
}

void main_shot_palette(const uint8_t *ycca, unsigned int n, uint8_t *rgba)
{
	for (unsigned int i = 0; i < n; i++, ycca += 4, rgba += 4) {
		main_shot_ycc_rgb(ycca[0], ycca[1], ycca[2], rgba);
		rgba[3] = ycca[3] & MAIN_SHOT_ALPHA_MAX;
	}
}

static void main_shot_lv(const struct main_shot_frame *f, unsigned int y, uint8_t *rgb)
{
	const uint8_t *s = f->lv + (y - f->lv_y) * f->lv_pitch;
	uint8_t *d = rgb + 3 * f->lv_x;

	for (unsigned int x = 0; x < f->lv_w; x += 2, s += 4, d += 6) {
		main_shot_ycc_rgb(s[0], s[1], s[3], d);
		main_shot_ycc_rgb(s[2], s[1], s[3], d + 3);
	}
}

static void main_shot_blend(uint8_t *d, const uint8_t *c)
{
	unsigned int a = c[3];

	if (!a)
		return;
	if (a == MAIN_SHOT_ALPHA_MAX) {
		d[0] = c[0];
		d[1] = c[1];
		d[2] = c[2];
		return;
	}
	for (unsigned int i = 0; i < 3; i++)
		d[i] = (uint8_t)((c[i] * a + d[i] * (MAIN_SHOT_ALPHA_MAX - a)) / MAIN_SHOT_ALPHA_MAX);
}

static void main_shot_layer(const struct main_shot_layer *l, unsigned int y, uint8_t *rgb)
{
	const uint8_t *s = l->pix + y * l->pitch;

	for (unsigned int x = 0; x < MAIN_SHOT_W; x++, rgb += 3) {
		unsigned int i = s[x * l->bpp / 8];

		if (l->bpp == 4)
			i = x & 1 ? i >> 4 : i & 0x0F;
		if (i < l->entries)
			main_shot_blend(rgb, l->pal + 4 * i);
	}
}

void main_shot_compose(const struct main_shot_frame *f, unsigned int y, uint8_t *rgb)
{
	for (unsigned int i = 0; i < 3 * MAIN_SHOT_W; i++)
		rgb[i] = 0;
	if (f->lv && y >= f->lv_y && y < f->lv_y + f->lv_h)
		main_shot_lv(f, y, rgb);
	for (unsigned int l = 0; l < MAIN_SHOT_LAYERS; l++)
		if (f->layer[l].pix)
			main_shot_layer(&f->layer[l], y, rgb);
}

void main_shot_stretch(const uint8_t *in, unsigned int in_w, uint8_t *out, unsigned int out_w)
{
	/* source position of the output pixel centre, 16.16 */
	uint32_t step = (in_w << 16) / out_w;
	int32_t pos = (int32_t)(step / 2) - MAIN_SHOT_HALF;

	for (unsigned int x = 0; x < out_w; x++, pos += (int32_t)step, out += 3) {
		int32_t p = pos < 0 ? 0 : pos;
		unsigned int i = (unsigned int)p >> 16, w = (unsigned int)p & 0xFFFF;
		const uint8_t *a = in + 3 * i;
		const uint8_t *b = i + 1 < in_w ? a + 3 : a;

		for (unsigned int c = 0; c < 3; c++)
			out[c] = (uint8_t)((a[c] * (65536 - w) + b[c] * w + MAIN_SHOT_HALF) >> 16);
	}
}

static char *main_shot_dec(char *p, unsigned int v)
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

unsigned int main_shot_ppm_header(char *out, unsigned int w, unsigned int h)
{
	char *p = out;

	*p++ = 'P';
	*p++ = '6';
	*p++ = '\n';
	p = main_shot_dec(p, w);
	*p++ = ' ';
	p = main_shot_dec(p, h);
	*p++ = '\n';
	p = main_shot_dec(p, 255);
	*p++ = '\n';
	return (unsigned int)(p - out);
}
