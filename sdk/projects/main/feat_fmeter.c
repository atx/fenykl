/*
 * main: focus meter: a row of ten cells under the green AF dot
 * showing the AF statistics' HPF3 of the metered area on an absolute log2 scale (main_fmeter.c).
 * Read-only polling of the statistics DMA buffer every tick, no hook; in MF the SDK's own contrast
 * client (sdk/lib/afc.c) provides the statistics. Drawn from the stock exposure-row cells
 * decoded at start, in the stock scale's joined black bar.
 */
#include <afc.h>
#include "main.h"
#include "main_fmeter.h"

#define FEAT_FMETER_TF_MODE 0x05u	/* u8 mode_focus: 1 MF [camera] */
#define FEAT_FMETER_TF_MF 1u
#define FEAT_FMETER_PROP_AREA 0x84D1u	/* AF area mode, 2 = all targets */
#define FEAT_FMETER_PROP_TARGET 0x88B2u	/* AF target index */
#define FEAT_FMETER_AREA_ALL 2u
#define FEAT_FMETER_TARGETS 35u
#define FEAT_FMETER_CTX_COLS 0x5Du
#define FEAT_FMETER_CTX_ROWS 0x5Eu
#define FEAT_FMETER_CTX_PATTERN 0x2D0u
#define FEAT_FMETER_CTX_CUT3 0x2D6u	/* u16 HPF3 cut-off: switches between framing and scan */
#define FEAT_FMETER_ROWS_MAX 6u
#define FEAT_FMETER_H3 2u		/* channel: HPF1, HPF2, HPF3, HPFV */
#define FEAT_FMETER_KEY_MF 0xFFFF0000u	/* log key of the own client's values */
/* Own-client gates that hide the bar in MF; the others (half-press, fcs, slots) hold it. */
#define FEAT_FMETER_MF_HIDE (FENYKL_AFC_G_SNAP | FENYKL_AFC_G_LV | FENYKL_AFC_G_MOVIE | \
			     FENYKL_AFC_G_DRV | FENYKL_AFC_G_OFF)

/* Exposure-row cells (8 x 9, 1 px transparent ring) and their kinds in the bar. */
#define FEAT_FMETER_IMG_WHITE 1095u
#define FEAT_FMETER_IMG_YELLOW 1096u
#define FEAT_FMETER_IMG_GREEN 1098u
#define FEAT_FMETER_IMG_FILL 0x04u	/* 1095's white fill */
#define FEAT_FMETER_IMG_BORDER 0x7Eu	/* 1095's translucent border */
#define FEAT_FMETER_CELL_W 8
#define FEAT_FMETER_CELL_H 9
#define FEAT_FMETER_CELL_PX (FEAT_FMETER_CELL_W * FEAT_FMETER_CELL_H)

enum feat_fmeter_kind { FEAT_FMETER_UNLIT, FEAT_FMETER_LIT, FEAT_FMETER_PEAK, FEAT_FMETER_GREEN,
			FEAT_FMETER_KINDS };

struct feat_fmeter_state {
	struct fenykl_hud_elem elem;
	uint8_t cell[FEAT_FMETER_KINDS][FEAT_FMETER_CELL_PX];
	struct main_fmeter norm;
	struct main_fmeter_bar drawn;	/* on screen; shown false while hidden */
	uint32_t now;			/* ms, MAIN_TICK_MS per tick */
	uint32_t last[3];		/* HPF1..3 of the area at the last tick */
	uint32_t key;			/* HPF3 cut-off << 16 | area << 8 | pattern, for the log */
	unsigned int area;
	uint32_t samples;	/* new frames */
	uint32_t renders;	/* bars drawn */
	uint32_t refused;	/* renders refused by the library */
};

static struct feat_fmeter_state feat_fmeter_st = {
	.elem = {
		.x = MAIN_LAYOUT_FMETER_X,
		.y = MAIN_LAYOUT_FMETER_Y,
		.w = MAIN_LAYOUT_FMETER_W,
		.h = MAIN_LAYOUT_FMETER_H,
		.bg = FENYKL_HUD_CLEAR,
	},
};

static FENYKL_LOG_MODULE(feat_fmeter_log, "fmeter", FENYKL_LOG_INFO);

FENYKL_CFG_BOOL(feat_fmeter_show, "main.fmeter.show", "Focus meter", &main_grp_hud, 1);

static uint32_t feat_fmeter_rd8(uint32_t a)
{
	return *(volatile uint8_t *)(uintptr_t)a;
}

static uint32_t feat_fmeter_rd16(uint32_t a)
{
	return *(volatile uint16_t *)(uintptr_t)a;
}

static uint32_t feat_fmeter_rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

static uint32_t feat_fmeter_prop_u8(unsigned int prop)
{
	const volatile uint8_t *p = fw_prop_get_obj(prop);

	return p ? p[0] : 0xFFu;
}

/* Decodes image id into kind k's cell, the borders made black as in the stock scale's bar. */
static int feat_fmeter_cell(unsigned int k, uint16_t id, uint8_t fill)
{
	uint8_t *px = feat_fmeter_st.cell[k];
	int w, h, r = fenykl_hud_image(id, px, FEAT_FMETER_CELL_PX, &w, &h);

	if (r || w != FEAT_FMETER_CELL_W || h != FEAT_FMETER_CELL_H) {
		fenykl_log_err(&feat_fmeter_log, "image %u: %d, %dx%d", id, r, w, h);
		return r ? r : FENYKL_HUD_EGLYPH;
	}
	for (int i = 0; i < FEAT_FMETER_CELL_PX; i++) {
		if (px[i] == FEAT_FMETER_IMG_BORDER)
			px[i] = FENYKL_HUD_BLACK;
		else if (px[i] == FEAT_FMETER_IMG_FILL)
			px[i] = fill;
	}
	return 0;
}

static int feat_fmeter_start(void)
{
	struct fenykl_hud_elem *e = &feat_fmeter_st.elem;
	int r;

	fenykl_afc_init();
	r = feat_fmeter_cell(FEAT_FMETER_UNLIT, FEAT_FMETER_IMG_WHITE, MAIN_LAYOUT_FMETER_UNLIT);
	if (!r)
		r = feat_fmeter_cell(FEAT_FMETER_LIT, FEAT_FMETER_IMG_WHITE, FEAT_FMETER_IMG_FILL);
	if (!r)
		r = feat_fmeter_cell(FEAT_FMETER_PEAK, FEAT_FMETER_IMG_YELLOW, FEAT_FMETER_IMG_FILL);
	if (!r)
		r = feat_fmeter_cell(FEAT_FMETER_GREEN, FEAT_FMETER_IMG_GREEN, FEAT_FMETER_IMG_FILL);
	if (r)
		return r;
	r = fenykl_hud_add(e);
	fenykl_log_info(&feat_fmeter_log, "element %dx%d at (%d,%d)", e->w, e->h, e->x, e->y);
	if (r)
		fenykl_log_err(&feat_fmeter_log, "hud add %d", r);
	return r;
}

/* Cells at the stock scale's pitch: neighbouring borders form one 2 px black line. */
static int feat_fmeter_paint(uint8_t *box, int w, int h, void *arg)
{
	const struct main_fmeter_bar *b = arg;

	(void)h;
	for (unsigned int k = 0; k < MAIN_FMETER_CELLS; k++) {
		unsigned int kind = FEAT_FMETER_UNLIT;
		const uint8_t *c;

		if (k < b->lit)
			kind = b->green ? FEAT_FMETER_GREEN : FEAT_FMETER_LIT;
		else if (k + 1 == b->peak)
			kind = FEAT_FMETER_PEAK;
		c = feat_fmeter_st.cell[kind];
		for (int y = 0; y < FEAT_FMETER_CELL_H; y++)
			for (int x = 0; x < FEAT_FMETER_CELL_W; x++)
				if (c[y * FEAT_FMETER_CELL_W + x])
					box[y * w + MAIN_LAYOUT_FMETER_PITCH * k + x] =
						c[y * FEAT_FMETER_CELL_W + x];
	}
	return 0;
}

/* The area's HPF1..3 from buf (channels cols x rows, rows * ROW_WORDS apart); in->fresh if changed. */
static void feat_fmeter_sample(struct main_fmeter_in *in, const volatile uint32_t *buf,
			       uint32_t cols, uint32_t rows, uint8_t pattern)
{
	struct feat_fmeter_state *st = &feat_fmeter_st;
	uint32_t cw = rows * MAIN_FMETER_ROW_WORDS;
	uint32_t t, word;
	int area = MAIN_FMETER_AREA_NONE;
	bool fresh = false, all;

	t = feat_fmeter_prop_u8(FEAT_FMETER_PROP_TARGET);
	if (t < FEAT_FMETER_TARGETS && feat_fmeter_rd8(FW_TAKE_FCS_AF_MODE2FCS + 2 * t + 1))
		area = (int)feat_fmeter_rd8(FW_TAKE_FCS_AF_MODE2FCS + 2 * t);
	all = feat_fmeter_prop_u8(FEAT_FMETER_PROP_AREA) == FEAT_FMETER_AREA_ALL;
	st->area = main_fmeter_area((const uint32_t *)(buf + FEAT_FMETER_H3 * cw), cols, rows, all,
				    area);
	word = main_fmeter_word(st->area, cols);
	for (unsigned int c = 0; c <= FEAT_FMETER_H3; c++) {
		uint32_t x = buf[c * cw + word];

		fresh |= x != st->last[c];
		st->last[c] = x;
	}
	in->fresh = fresh;
	in->v = st->last[FEAT_FMETER_H3];
	in->pattern = pattern;
}

/* AF: the firmware's client, fcs's buffer. */
static void feat_fmeter_read_af(struct main_fmeter_in *in)
{
	uint32_t ctx = feat_fmeter_rd32(FW_FCS_CONTRAST_CTX);
	uint32_t cw = feat_fmeter_rd32(FW_FCS_AREA_SIZE);
	uint32_t cols, rows;
	uint8_t pattern;

	if (!ctx)
		return;
	cols = feat_fmeter_rd8(ctx + FEAT_FMETER_CTX_COLS);
	rows = feat_fmeter_rd8(ctx + FEAT_FMETER_CTX_ROWS);
	if (!cols || cols > MAIN_FMETER_ROW_WORDS || !rows || rows > FEAT_FMETER_ROWS_MAX ||
	    cw != rows * MAIN_FMETER_ROW_WORDS)
		return;
	pattern = (uint8_t)feat_fmeter_rd8(ctx + FEAT_FMETER_CTX_PATTERN);
	feat_fmeter_sample(in, (const volatile uint32_t *)FW_FCS_DMA_BUF, cols, rows, pattern);
	feat_fmeter_st.key = feat_fmeter_rd16(ctx + FEAT_FMETER_CTX_CUT3) << 16 |
			     feat_fmeter_st.area << 8 | pattern;
}

/*
 * MF: the own client (sdk/lib/afc.c) with the snapshot's grid and pattern. Hidden at once when it
 * cannot run here (no snapshot, not stills LV, movie, debug mode, error); a half-press (its detach)
 * or fcs busy holds the bar like the S-AF lock.
 */
static void feat_fmeter_read_mf(struct main_fmeter_in *in)
{
	const struct fenykl_afc *a = fenykl_afc_get();
	const volatile uint32_t *buf = fenykl_afc_dma();

	if (fenykl_afc_failed() || a->gates & FEAT_FMETER_MF_HIDE) {
		in->hide = true;
		return;
	}
	if (a->sm.state != FENYKL_AFC_ATTACHED ||
	    buf[FEAT_FMETER_H3 * a->snap_rows * MAIN_FMETER_ROW_WORDS] == FENYKL_AFC_DMA_FILL)
		return;
	feat_fmeter_sample(in, buf, a->snap_cols, a->snap_rows, a->snap_pat);
	feat_fmeter_st.key = FEAT_FMETER_KEY_MF | feat_fmeter_st.area << 8 | a->snap_pat;
}

/* The statistics of this tick; in->fresh false if none. */
static void feat_fmeter_read(struct main_fmeter_in *in)
{
	uint32_t tf = feat_fmeter_rd32(FW_TAKE_FOCUS_PTR);

	in->hide = false;
	in->half = !(feat_fmeter_rd8(FW_TRG_SNAPSHOT) & 1u);
	in->fresh = false;
	if (tf && feat_fmeter_rd8(tf + FEAT_FMETER_TF_MODE) == FEAT_FMETER_TF_MF)
		feat_fmeter_read_mf(in);
	else
		feat_fmeter_read_af(in);
}

static bool feat_fmeter_same(const struct main_fmeter_bar *a, const struct main_fmeter_bar *b)
{
	return a->shown == b->shown && a->lit == b->lit && a->peak == b->peak &&
	       a->green == b->green;
}

static void feat_fmeter_hide(void)
{
	struct feat_fmeter_state *st = &feat_fmeter_st;

	if (!st->drawn.shown)
		return;
	fenykl_hud_hide(&st->elem);
	st->drawn.shown = false;
	fenykl_log_dbg(&feat_fmeter_log, "hidden (show %d)", feat_fmeter_show);
}

static void feat_fmeter_tick(void)
{
	struct feat_fmeter_state *st = &feat_fmeter_st;
	struct main_fmeter_bar bar;
	struct main_fmeter_in in;
	int r;

	st->now += MAIN_TICK_MS;
	fenykl_afc_poll(feat_fmeter_show);
	if (!feat_fmeter_show) {
		feat_fmeter_hide();
		return;
	}
	feat_fmeter_read(&in);
	st->samples += in.fresh;
	main_fmeter_step(&st->norm, &in, st->now);
	bar = st->norm.bar;
	if (!bar.shown) {
		feat_fmeter_hide();
		return;
	}
	if (feat_fmeter_same(&bar, &st->drawn))
		return;
	r = fenykl_hud_draw(&st->elem, feat_fmeter_paint, &bar);
	if (r) {
		if (st->refused++ == 0)
			fenykl_log_warn(&feat_fmeter_log, "bar not drawn: %d", r);
		return;
	}
	st->drawn = bar;
	st->renders++;
	fenykl_log_dbg(&feat_fmeter_log, "v %u l2 %d.%02d key %08x", in.v,
		       st->norm.level / MAIN_FMETER_LOG2_ONE,
		       st->norm.level % MAIN_FMETER_LOG2_ONE * 100 / MAIN_FMETER_LOG2_ONE, st->key);
	fenykl_log_dbg(&feat_fmeter_log, "  lit %u peak %u area %u", bar.lit, bar.peak, st->area);
}

static void feat_fmeter_stop(void)
{
	fenykl_afc_end();
	feat_fmeter_hide();
}

static void feat_fmeter_status(void)
{
	const struct feat_fmeter_state *st = &feat_fmeter_st;
	const struct fenykl_afc *a = fenykl_afc_get();

	fenykl_log_info(&feat_fmeter_log, "samples %u renders %u area %u lit %u", st->samples,
			st->renders, st->area, st->drawn.lit);
	if (st->refused)
		fenykl_log_warn(&feat_fmeter_log, "refused %u", st->refused);
	fenykl_log_info(&feat_fmeter_log, "mf %s gates %03x attaches %u err %s",
			fenykl_afc_state_name[a->sm.state], a->gates, a->attaches,
			fenykl_afc_err_name[a->sm.err]);
}

const struct main_feat feat_fmeter = {
	.name = "fmeter",
	.log = &feat_fmeter_log,
	.period = 1,
	.start = feat_fmeter_start,
	.tick = feat_fmeter_tick,
	.stop = feat_fmeter_stop,
	.status = feat_fmeter_status,
};
