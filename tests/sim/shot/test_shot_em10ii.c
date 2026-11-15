/* The screenshot library's E-M10 II rules and pixel work (sdk/lib/shot_pix.c) in the GDB simulator,
   with ../hooks_em10ii/sim_start.S. plugin_main sets sim_exit_code to the failure bit mask (0 =
   pass):
     1   EVF: the 640 x 480 OSD is not scaled 1.6x to the 1024 x 768 canvas by nearest pixel
         (x * 640 / 1024, y * 480 / 768), or the LCD's 720 x 480 is not 1:1
     2   palette: RGB not copied, alpha not clamped to 0..32, the global alpha not folded in
     4   blending: alpha 0 changed the pixel, 32 did not replace it, or a partial alpha is off
     8   gate: movie, playback, prop 0xE951 or a busy pool over PRIM block 0 not refusing a start
         and aborting a held shot; a PRIM frame in flight or the display not ready not refusing a
         start, or aborting a held shot; an output change not aborting, or refusing a start; a
         free-count rule
     16  layout: the LCD and EVF snapshots are not LV + three 8-bpp layers within PRIM block 0,
         or a strip is not 16 file rows */
#include <stddef.h>
#include "../../../sdk/lib/shot_int.h"

#define T_EVF_W 1024u
#define T_EVF_H 768u
#define T_OSD_W 640u
#define T_OSD_H 480u
#define T_PRIM_BLOCK 0x1895000u

uint32_t sim_exit_code = 99;

/* The 640 x 480 source with a pitch of 1 byte: pixel (x, y) is t_src[y + x], so every row and
   column a canvas pixel maps to shows in its index without a 300 KB buffer. */
static uint8_t t_src[T_OSD_W + T_OSD_H];
static uint8_t t_rgb[3 * T_EVF_W];
static uint8_t t_pal[256 * 4];

static bool t_px(const uint8_t *rgb, unsigned int x, unsigned int r, unsigned int g,
		 unsigned int b)
{
	return rgb[3 * x] == r && rgb[3 * x + 1] == g && rgb[3 * x + 2] == b;
}

/* Opaque palette entry i = {i, 0, 7}; t_src[i] = i % 251. */
static void t_scaled_fill(void)
{
	for (unsigned int i = 0; i < 256; i++) {
		t_pal[4 * i] = (uint8_t)i;
		t_pal[4 * i + 1] = 0;
		t_pal[4 * i + 2] = 7;
		t_pal[4 * i + 3] = FENYKL_SHOT_ALPHA_MAX;
	}
	for (unsigned int i = 0; i < sizeof(t_src); i++)
		t_src[i] = (uint8_t)(i % 251);
}

/* Every canvas pixel of row y against source pixel (x * sw / w, y * sh / h). */
static bool t_scaled_row(const struct fenykl_shot_frame *f, unsigned int y, unsigned int sw,
			 unsigned int sh)
{
	unsigned int sy = y * sh / f->h;

	fenykl_shot_compose(f, y, t_rgb);
	for (unsigned int x = 0; x < f->w; x++)
		if (!t_px(t_rgb, x, t_src[sy + x * sw / f->w], 0, 7))
			return false;
	return true;
}

static uint32_t t_scale(void)
{
	static uint8_t row[720];
	struct fenykl_shot_frame f = { .w = T_EVF_W, .h = T_EVF_H };

	t_scaled_fill();
	f.layer[2] = (struct fenykl_shot_layer){
		t_src, 1, 8, 256, t_pal, T_OSD_W, T_OSD_H
	};
	for (unsigned int y = 0; y < T_EVF_H; y += 7)
		if (!t_scaled_row(&f, y, T_OSD_W, T_OSD_H))
			return 1;
	if (!t_scaled_row(&f, T_EVF_H - 1, T_OSD_W, T_OSD_H))
		return 1;
	/* x 1, 2 and 1023 show source columns 0, 1 and 639; canvas row 2 shows source row 1 */
	fenykl_shot_compose(&f, 1, t_rgb);
	if (!t_px(t_rgb, 1, 0, 0, 7) || !t_px(t_rgb, 2, 1, 0, 7) || !t_px(t_rgb, 1023, 639 % 251, 0, 7))
		return 1;
	fenykl_shot_compose(&f, 2, t_rgb);
	if (!t_px(t_rgb, 0, 1, 0, 7))
		return 1;
	/* the LCD: 720 x 480 at 1:1 */
	f = (struct fenykl_shot_frame){ .w = 720, .h = 480 };
	f.layer[2] = (struct fenykl_shot_layer){ row, 0, 8, 256, t_pal, 720, 480 };
	for (unsigned int i = 0; i < sizeof(row); i++)
		row[i] = (uint8_t)(i % 199);
	fenykl_shot_compose(&f, 479, t_rgb);
	for (unsigned int x = 0; x < 720; x++)
		if (!t_px(t_rgb, x, x % 199, 0, 7))
			return 1;
	return 0;
}

static uint32_t t_palette(void)
{
	static const uint8_t src[] = { 10, 20, 30, 32, 40, 50, 60, 31, 70, 80, 90, 40, 1, 2, 3, 0 };
	uint8_t rgba[sizeof(src)];

	fenykl_shot_palette(src, 4, 0x100, rgba);
	for (unsigned int i = 0; i < 4; i++)
		if (rgba[4 * i] != src[4 * i] || rgba[4 * i + 1] != src[4 * i + 1] ||
		    rgba[4 * i + 2] != src[4 * i + 2])
			return 2;
	if (rgba[3] != 32 || rgba[7] != 31 || rgba[11] != 32 || rgba[15] != 0)
		return 2;
	fenykl_shot_palette(src, 4, 0xCC, rgba);	/* the EVF's 80 % */
	return rgba[3] == 25 && rgba[7] == 24 && rgba[11] == 25 && rgba[15] == 0 ? 0 : 2;
}

static uint32_t t_blend(void)
{
	/* 0 transparent, 1 opaque white, 2 black alpha 8, 3 {200, 0, 64} alpha 16 */
	static const uint8_t pal[] = { 9, 9, 9, 0, 255, 255, 255, 32, 0, 0, 0, 8, 200, 0, 64, 16 };
	static uint8_t lv[4 * 4];
	struct fenykl_shot_frame f = { .w = 8, .h = 1 };

	for (unsigned int i = 0; i < sizeof(lv); i += 2) {	/* grey 100 */
		lv[i] = 100;
		lv[i + 1] = 128;
	}
	f.lv = lv;
	f.lv_w = 8;
	f.lv_h = 1;
	for (unsigned int i = 0; i < 4; i++)
		t_src[i] = (uint8_t)i;
	f.layer[1] = (struct fenykl_shot_layer){ t_src, 0, 8, 4, pal, 8, 1 };
	fenykl_shot_compose(&f, 0, t_rgb);
	if (!t_px(t_rgb, 0, 100, 100, 100) || !t_px(t_rgb, 1, 255, 255, 255))
		return 4;
	if (!t_px(t_rgb, 2, 75, 75, 75))	/* (0 * 8 + 100 * 24) / 32 */
		return 4;
	return t_px(t_rgb, 3, 150, 50, 82) ? 0 : 4;	/* (c * 16 + 100 * 16) / 32 */
}

static bool t_refused(const struct fenykl_shot_cond *c, const struct fenykl_shot_cond *start)
{
	return fenykl_shot_gate(c, start) != NULL;
}

/* A rule that refuses a start (start NULL) and/or aborts a held shot. */
struct t_rule {
	struct fenykl_shot_cond c;
	bool start, held;
};

static uint32_t t_gate(void)
{
	const struct fenykl_shot_cond ok = { .ready = true, .mode = 1, .buf_free = 12 };
	static const struct t_rule rules[] = {
		{ { .ready = true, .mode = 1, .movie = true }, true, true },
		{ { .ready = true, .mode = 1, .playback = true }, true, true },
		{ { .ready = true, .mode = 1, .still_shared = true }, true, true },
		{ { .ready = true, .mode = 1, .other_used = 0x80 }, true, true },
		/* a PRIM frame in flight only before the lend: a still while held uses the next block */
		{ { .ready = true, .mode = 1, .pool_used = 0x2 }, true, false },
		{ { .mode = 1 }, true, false },
		{ { .ready = true, .mode = 6 }, false, true },
		/* no free-count rule */
		{ { .ready = true, .mode = 1, .buf_free = 0 }, false, false },
	};

	if (t_refused(&ok, NULL) || t_refused(&ok, &ok))
		return 8;
	for (unsigned int i = 0; i < sizeof(rules) / sizeof(rules[0]); i++)
		if (t_refused(&rules[i].c, NULL) != rules[i].start ||
		    t_refused(&rules[i].c, &ok) != rules[i].held)
			return 8;
	return 0;
}

static uint32_t t_layout(void)
{
	struct fenykl_disp_geom lcd = { .w = 720, .h = 480, .square_w = 720, .osd_w = 720,
					.osd_h = 480 };
	struct fenykl_disp_geom evf = { .w = 1024, .h = 768, .square_w = 1024, .osd_w = 640,
					.osd_h = 480 };
	struct fenykl_shot_layout lay;

	fenykl_shot_layout(&lcd, &lay);
	if (lay.lv_row != 1440 || lay.osd[0] != 691200 || lay.osd_row[1] != 720 ||
	    lay.snap != 1728000 || lay.bytes != lay.snap || lay.strip != 3 * 720 * 16)
		return 16;
	fenykl_shot_layout(&evf, &lay);
	if (lay.lv_row != 2048 || lay.osd[0] != 1572864 || lay.osd_row[2] != 640 ||
	    lay.snap != 2494464 || lay.bytes > T_PRIM_BLOCK)
		return 16;
	return lay.strip == 3 * T_EVF_W * FENYKL_SHOT_STRIP_ROWS && FENYKL_SHOT_STRIP_ROWS == 16 ?
	       0 : 16;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = t_scale() | t_palette() | t_blend() | t_gate() | t_layout();
}
