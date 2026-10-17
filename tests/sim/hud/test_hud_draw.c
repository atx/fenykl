/*
 * fenykl_hud_image and fenykl_hud_draw (sdk/lib/hud.c) in the GDB simulator: images.c holds the stock
 * firmware's own encoded bytes of the exposure-row cells 1095-1098, a live-view digit and the AF dot
 * (fmt 0x02). Each cell image is decoded and dumped, then a 10-cell row in the focus meter's element
 * is drawn through a callback and its rectangle in buffer 0 dumped for
 * test_hud_sim.py, which compares both with the Python decoder. plugin_main sets sim_exit_code to the
 * mask of failed checks below (0 = pass).
 */
#include "sim_hud.h"

enum {
	SIM_FAIL_SETUP = 1,	/* init, add, an image or the draw did not return 0 */
	SIM_FAIL_REFUSE = 2,	/* missing, too large, not fmt 0x03, no table: EGLYPH */
	SIM_FAIL_ARGS = 4,	/* the callback gets the box cleared to bg, w, h and arg */
	SIM_FAIL_BOTH = 8,	/* the draw paints both buffers */
	SIM_FAIL_ABORT = 16,	/* a callback error is returned, the element stays unchanged */
};

#define SIM_CELL_W 8
#define SIM_CELL_H 9
#define SIM_CELL_PX (SIM_CELL_W * SIM_CELL_H)
#define SIM_CELLS 10
#define SIM_CELL_TOP 1		/* element row of the cells' row 0 */
#define SIM_DIGIT 1136		/* "0", 13 x 22: more than SIM_CELL_PX */
#define SIM_DOT 969		/* AF-confirm dot, fmt 0x02 */
#define SIM_MISSING 1000	/* not in images.c */

/* 'W'hite 1095, 'Y'ellow 1096, 'O'range 1097, 'G'reen 1098, 'f'aint (1095 through sim_faint) */
static const char sim_kinds[] = "WYOG";
static const uint16_t sim_ids[] = { 1095, 1096, 1097, 1098 };
static const char sim_pattern[SIM_CELLS + 1] = "GGGOWWYfff";

/* fw.S forwards it; nothing here draws text */
uint32_t c_gpi_get_glyph(void)
{
	return 0;
}

static struct {
	struct fenykl_hud_elem el;
	uint8_t cell[4][SIM_CELL_PX];
	uint8_t faint[SIM_CELL_PX];
	int bad_args;
} sim = {
	.el = { .x = 620, .y = 42, .w = 80, .h = 12, .bg = FENYKL_HUD_CLEAR },
};

/* Unlit cell: the white fill 0x04 translucent black, the border kept. */
static uint8_t sim_faint(uint8_t v)
{
	return v == 0x04 ? FENYKL_HUD_TRANSLUCENT : v;
}

static const uint8_t *sim_cell(char kind)
{
	if (kind == 'f')
		return sim.faint;
	for (unsigned int i = 0; i < sizeof(sim_ids) / sizeof(sim_ids[0]); i++)
		if (sim_kinds[i] == kind)
			return sim.cell[i];
	return NULL;
}

/* Cell k at x = 8 k, row SIM_CELL_TOP; transparent pixels (0) leave the box's bg. */
static int sim_paint_row(uint8_t *box, int w, int h, void *arg)
{
	if (w != sim.el.w || h != sim.el.h || arg != &sim)
		sim.bad_args = 1;
	for (int i = 0; i < w * h; i++)
		if (box[i] != sim.el.bg)
			sim.bad_args = 1;
	for (int k = 0; k < SIM_CELLS; k++) {
		const uint8_t *px = sim_cell(sim_pattern[k]);

		for (int y = 0; y < SIM_CELL_H; y++)
			for (int x = 0; x < SIM_CELL_W; x++)
				if (px[y * SIM_CELL_W + x])
					box[(SIM_CELL_TOP + y) * w + k * SIM_CELL_W + x] =
						px[y * SIM_CELL_W + x];
	}
	return 0;
}

static int sim_paint_fail(uint8_t *box, int w, int h, void *arg)
{
	(void)arg;
	for (int i = 0; i < w * h; i++)
		box[i] = SIM_GUI;
	return -42;
}

/* "#img id w h\n", then the pixels */
static void sim_dump_image(uint16_t id, const uint8_t *px, int w, int h)
{
	char line[32] = "#img ", *o = line + 5;

	o = sim_dec(o, id);
	o = sim_dec(o, w);
	o = sim_dec(o, h);
	o[-1] = '\n';
	sim_write(line, (unsigned int)(o - line));
	sim_write(px, (unsigned int)(w * h));
}

/* "#row x y w h pattern\n", then the rectangle of buffer 0, row by row */
static void sim_dump_row(void)
{
	const struct fenykl_hud_elem *e = &sim.el;
	char line[48] = "#row ", *o = line + 5;

	o = sim_dec(o, e->x);
	o = sim_dec(o, e->y);
	o = sim_dec(o, e->w);
	o = sim_dec(o, e->h);
	for (const char *s = sim_pattern; *s; s++)
		*o++ = *s;
	*o++ = '\n';
	sim_write(line, (unsigned int)(o - line));
	for (int r = 0; r < e->h; r++)
		sim_write((const void *)sim_px(SIM_BUF0, e, r, 0), (unsigned int)e->w);
}

static int sim_decode_cells(void)
{
	int w, h;

	for (unsigned int i = 0; i < sizeof(sim_ids) / sizeof(sim_ids[0]); i++) {
		if (fenykl_hud_image(sim_ids[i], sim.cell[i], SIM_CELL_PX, &w, &h) != 0 ||
		    w != SIM_CELL_W || h != SIM_CELL_H)
			return 0;
		sim_dump_image(sim_ids[i], sim.cell[i], w, h);
	}
	for (int i = 0; i < SIM_CELL_PX; i++)
		sim.faint[i] = sim_faint(sim.cell[0][i]);
	return 1;
}

static int sim_refusals(void)
{
	uint8_t px[SIM_CELL_PX];
	int w = -1, h = -1;

	if (fenykl_hud_image(SIM_DIGIT, px, sizeof(px), &w, &h) != FENYKL_HUD_EGLYPH ||
	    fenykl_hud_image(SIM_DOT, px, sizeof(px), &w, &h) != FENYKL_HUD_EGLYPH ||
	    fenykl_hud_image(SIM_MISSING, px, sizeof(px), &w, &h) != FENYKL_HUD_EGLYPH)
		return 0;
	SIM_U32(SIM_IMAGE_TABLE) = 0;
	int r = fenykl_hud_image(sim_ids[0], px, sizeof(px), &w, &h);

	SIM_U32(SIM_IMAGE_TABLE) = (uint32_t)sim_images;
	return r == FENYKL_HUD_EGLYPH && w == -1 && h == -1;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	struct fenykl_hud_buf buf[2];
	struct fenykl_hud_elem *e = &sim.el;
	int fail = 0;

	sim_setup_memory();
	sim_fill(SIM_BUF0, e, SIM_GUI);
	sim_fill(SIM_BUF1, e, SIM_GUI);
	if (fenykl_hud_init(FENYKL_HUD_INFO_SCREENS, buf) != 0 || fenykl_hud_add(e) != 0 ||
	    !sim_decode_cells()) {
		sim_exit_code = SIM_FAIL_SETUP;
		return;
	}
	if (!sim_refusals())
		fail |= SIM_FAIL_REFUSE;
	if (fenykl_hud_draw(e, sim_paint_row, &sim) != 0)
		fail |= SIM_FAIL_SETUP;
	if (sim.bad_args)
		fail |= SIM_FAIL_ARGS;
	if (!sim_same(e))
		fail |= SIM_FAIL_BOTH;
	sim_dump_row();

	uint32_t pub = e->pub;

	if (fenykl_hud_draw(e, sim_paint_fail, NULL) != -42 || e->pub != pub || !sim_same(e) ||
	    sim_all(SIM_BUF0, e, SIM_GUI))
		fail |= SIM_FAIL_ABORT;
	sim_exit_code = (uint32_t)fail;
}
