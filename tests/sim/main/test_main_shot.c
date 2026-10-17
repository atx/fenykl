/* The main plugin's screenshot pixel work (sdk/projects/main/main_shot.c) in the GDB simulator,
   with ../hooks/sim_start.S. plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
     1   YCbCr -> RGB: a fixed case (grey, black, white, saturation and clamping) differs
     2   YCbCr -> RGB: more than 1 off the double-precision BT.601 full-range reference
     4   palette: an entry differs from its YCbCr conversion, or alpha is not masked to 0..15
     8   live view: wrong placement, chroma pairing or non-black canvas outside the image
     16  blending: alpha 0 changed the pixel, 15 did not replace it, or a partial alpha is off
     32  4 bpp: the low nibble is not the left pixel, or an index past the entries was drawn
     64  layer order: the last layer is not on top, or a hidden layer was drawn
     128 stretch: identity, constant row, end pixels or monotonicity broken
     256 PPM header text or length */
#include <stddef.h>
#include "../../../sdk/projects/main/main_shot.h"

uint32_t sim_exit_code = 99;

static uint8_t t_lv[2][2 * MAIN_SHOT_W];
static uint8_t t_l8[2][MAIN_SHOT_W];
static uint8_t t_l4[2][MAIN_SHOT_W / 2];
static uint8_t t_rgb[3 * MAIN_SHOT_W];
static uint8_t t_out[3 * MAIN_SHOT_OUT_W];

static bool t_px(const uint8_t *rgb, unsigned int x, unsigned int r, unsigned int g,
		 unsigned int b)
{
	return rgb[3 * x] == r && rgb[3 * x + 1] == g && rgb[3 * x + 2] == b;
}

static uint32_t t_ycc_fixed(void)
{
	static const uint8_t cases[][6] = {
		{ 0, 128, 128, 0, 0, 0 },
		{ 255, 128, 128, 255, 255, 255 },
		{ 128, 128, 128, 128, 128, 128 },
		{ 76, 85, 255, 254, 0, 0 },	/* red: R 254.05, G 0.15, B -0.2 */
		{ 255, 255, 255, 255, 121, 255 },	/* G 120.6, R and B clamp */
		{ 0, 0, 0, 0, 135, 0 },		/* G 135.4 */
		{ 16, 128, 240, 173, 0, 16 },	/* R 173.0, G -64.0 */
	};
	uint8_t rgb[3];

	for (unsigned int i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		const uint8_t *c = cases[i];

		main_shot_ycc_rgb(c[0], c[1], c[2], rgb);
		if (rgb[0] != c[3] || rgb[1] != c[4] || rgb[2] != c[5])
			return 1;
	}
	return 0;
}

static int t_ref(double v)
{
	if (v < 0)
		return 0;
	return v > 255 ? 255 : (int)(v + 0.5);
}

static uint32_t t_ycc_sweep(void)
{
	uint8_t rgb[3];

	for (int y = 0; y < 256; y += 15)
		for (int cb = 0; cb < 256; cb += 15)
			for (int cr = 0; cr < 256; cr += 15) {
				double u = cb - 128, v = cr - 128;
				int ref[3] = {
					t_ref(y + 1.402 * v),
					t_ref(y - 0.344136 * u - 0.714136 * v),
					t_ref(y + 1.772 * u),
				};

				main_shot_ycc_rgb(y, cb, cr, rgb);
				for (int i = 0; i < 3; i++)
					if (rgb[i] - ref[i] > 1 || ref[i] - rgb[i] > 1)
						return 2;
			}
	return 0;
}

static uint32_t t_palette(void)
{
	static const uint8_t ycca[] = { 0, 128, 128, 0x0F, 200, 50, 180, 0x38, 128, 128, 128, 0 };
	uint8_t rgba[12], rgb[3];

	main_shot_palette(ycca, 3, rgba);
	for (unsigned int i = 0; i < 3; i++) {
		main_shot_ycc_rgb(ycca[4 * i], ycca[4 * i + 1], ycca[4 * i + 2], rgb);
		if (rgba[4 * i] != rgb[0] || rgba[4 * i + 1] != rgb[1] || rgba[4 * i + 2] != rgb[2])
			return 4;
	}
	return rgba[3] == 15 && rgba[7] == 8 && rgba[11] == 0 ? 0 : 4;
}

static void t_frame(struct main_shot_frame *f)
{
	*f = (struct main_shot_frame){ 0 };
}

static uint32_t t_live_view(void)
{
	struct main_shot_frame f;
	uint8_t a[3], b[3];

	t_frame(&f);
	f.lv = t_lv[0];
	f.lv_pitch = sizeof(t_lv[0]);
	f.lv_x = 4;
	f.lv_y = 10;
	f.lv_w = 8;
	f.lv_h = 1;
	t_lv[0][0] = 50;	/* Y0 U Y1 V */
	t_lv[0][1] = 100;
	t_lv[0][2] = 200;
	t_lv[0][3] = 150;
	for (unsigned int i = 4; i < 16; i++)
		t_lv[0][i] = 128;
	main_shot_ycc_rgb(50, 100, 150, a);
	main_shot_ycc_rgb(200, 100, 150, b);
	main_shot_compose(&f, 10, t_rgb);
	if (!t_px(t_rgb, 4, a[0], a[1], a[2]) || !t_px(t_rgb, 5, b[0], b[1], b[2]) ||
	    !t_px(t_rgb, 6, 128, 128, 128) || !t_px(t_rgb, 11, 128, 128, 128))
		return 8;
	if (!t_px(t_rgb, 3, 0, 0, 0) || !t_px(t_rgb, 12, 0, 0, 0) ||
	    !t_px(t_rgb, MAIN_SHOT_W - 1, 0, 0, 0))
		return 8;
	main_shot_compose(&f, 11, t_rgb);
	for (unsigned int x = 0; x < MAIN_SHOT_W; x++)
		if (!t_px(t_rgb, x, 0, 0, 0))
			return 8;
	return 0;
}

/* palette: 0 transparent, 1 opaque white, 2 black alpha 5, 3 {200, 0, 50} alpha 8 */
static const uint8_t t_pal[] = {
	9, 9, 9, 0, 255, 255, 255, 15, 0, 0, 0, 5, 200, 0, 50, 8,
};

static uint32_t t_blend(void)
{
	struct main_shot_frame f;

	t_frame(&f);
	f.lv = t_lv[1];
	f.lv_pitch = sizeof(t_lv[1]);
	f.lv_w = MAIN_SHOT_W;
	f.lv_h = 1;
	for (unsigned int x = 0; x < MAIN_SHOT_W; x += 2) {	/* grey 255, then grey 50 */
		t_lv[1][2 * x] = t_lv[1][2 * x + 2] = x < 8 ? 255 : 50;
		t_lv[1][2 * x + 1] = t_lv[1][2 * x + 3] = 128;
	}
	t_l8[0][0] = 0;
	t_l8[0][1] = 1;
	t_l8[0][2] = 2;
	t_l8[0][8] = 2;
	t_l8[0][9] = 3;
	t_l8[0][10] = 7;	/* past the 4 entries */
	f.layer[0] = (struct main_shot_layer){ t_l8[0], sizeof(t_l8[0]), 8, 4, t_pal };
	main_shot_compose(&f, 0, t_rgb);
	if (!t_px(t_rgb, 0, 255, 255, 255) || !t_px(t_rgb, 1, 255, 255, 255))
		return 16;
	if (!t_px(t_rgb, 2, 170, 170, 170))	/* (0 * 5 + 255 * 10) / 15 */
		return 16;
	if (!t_px(t_rgb, 8, 33, 33, 33))	/* (0 * 5 + 50 * 10) / 15 = 33.3 */
		return 16;
	if (!t_px(t_rgb, 9, 130, 23, 50))	/* (c * 8 + 50 * 7) / 15 */
		return 16;
	return t_px(t_rgb, 10, 50, 50, 50) ? 0 : 32;
}

static uint32_t t_4bpp(void)
{
	struct main_shot_frame f;

	t_frame(&f);
	t_l4[0][0] = 0x10;	/* x 0: index 0, x 1: index 1 */
	t_l4[0][1] = 0x03;	/* x 2: index 3, x 3: index 0 */
	t_l4[0][2] = 0x1F;	/* x 4: 15 (past 4 entries), x 5: 1 */
	f.layer[1] = (struct main_shot_layer){ t_l4[0], sizeof(t_l4[0]), 4, 4, t_pal };
	main_shot_compose(&f, 0, t_rgb);
	if (!t_px(t_rgb, 0, 0, 0, 0) || !t_px(t_rgb, 1, 255, 255, 255) ||
	    !t_px(t_rgb, 2, 106, 0, 26) || !t_px(t_rgb, 3, 0, 0, 0))	/* (c * 8) / 15 */
		return 32;
	return t_px(t_rgb, 4, 0, 0, 0) && t_px(t_rgb, 5, 255, 255, 255) ? 0 : 32;
}

static uint32_t t_order(void)
{
	static const uint8_t red[] = { 255, 0, 0, 15 }, blue[] = { 0, 0, 255, 15 };
	struct main_shot_frame f;

	t_frame(&f);
	t_l8[1][0] = 0;
	t_l8[1][1] = 0;
	t_l4[1][0] = 0x00;	/* x 0 and 1 */
	f.layer[0] = (struct main_shot_layer){ t_l8[1], sizeof(t_l8[1]), 8, 1, red };
	f.layer[2] = (struct main_shot_layer){ t_l4[1], sizeof(t_l4[1]), 4, 1, blue };
	main_shot_compose(&f, 0, t_rgb);
	if (!t_px(t_rgb, 0, 0, 0, 255))
		return 64;
	f.layer[2].pix = NULL;
	main_shot_compose(&f, 0, t_rgb);
	return t_px(t_rgb, 1, 255, 0, 0) ? 0 : 64;
}

static uint32_t t_stretch(void)
{
	for (unsigned int i = 0; i < 3 * MAIN_SHOT_W; i++)
		t_rgb[i] = (uint8_t)(i / 3 * 250 / (MAIN_SHOT_W - 1) + i % 3);
	main_shot_stretch(t_rgb, MAIN_SHOT_W, t_out, MAIN_SHOT_W);
	for (unsigned int i = 0; i < 3 * MAIN_SHOT_W; i++)
		if (t_out[i] != t_rgb[i])
			return 128;
	main_shot_stretch(t_rgb, MAIN_SHOT_W, t_out, MAIN_SHOT_OUT_W);
	for (unsigned int c = 0; c < 3; c++) {
		if (t_out[c] != t_rgb[c] ||
		    t_out[3 * (MAIN_SHOT_OUT_W - 1) + c] != t_rgb[3 * (MAIN_SHOT_W - 1) + c])
			return 128;
		for (unsigned int x = 1; x < MAIN_SHOT_OUT_W; x++)
			if (t_out[3 * x + c] < t_out[3 * (x - 1) + c])
				return 128;
	}
	for (unsigned int i = 0; i < 3 * MAIN_SHOT_W; i++)
		t_rgb[i] = (uint8_t)(77 + i % 3);
	main_shot_stretch(t_rgb, MAIN_SHOT_W, t_out, MAIN_SHOT_OUT_W);
	for (unsigned int i = 0; i < 3 * MAIN_SHOT_OUT_W; i++)
		if (t_out[i] != 77 + i % 3)
			return 128;
	return 0;
}

static bool t_same(const char *a, const char *b, unsigned int n)
{
	for (unsigned int i = 0; i < n; i++)
		if (a[i] != b[i])
			return false;
	return !b[n];
}

static uint32_t t_ppm(void)
{
	char h[MAIN_SHOT_HDR_MAX];
	unsigned int n = main_shot_ppm_header(h, MAIN_SHOT_OUT_W, MAIN_SHOT_H);

	if (n != 15 || !t_same(h, "P6\n853 480\n255\n", n))
		return 256;
	n = main_shot_ppm_header(h, 7, 1);
	return n == 11 && t_same(h, "P6\n7 1\n255\n", n) ? 0 : 256;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = t_ycc_fixed() | t_ycc_sweep() | t_palette() | t_live_view() | t_blend() |
			t_4bpp() | t_order() | t_stretch() | t_ppm();
}
