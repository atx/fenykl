/*
 * main: battery charge element, e.g. "73%", in the top-left corner of the info screens. Once a
 * second in live view it reads the battery voltage as the firmware's own checks do
 * (fenykl_batt_read), feeds the model (main_batt.c) and renders on change only: green above 50 %,
 * yellow above 25 %, red below.
 * Hidden on the AC adapter or DC coupler. The stock battery icon is left alone (E-PL3: on these
 * screens the GUI shows it only at its own "low" level).
 */
#include "main.h"
#include "main_batt.h"
#include <body.h>

#define FEAT_BATT_GREEN 50u		/* % above which the text is green, then yellow */
#define FEAT_BATT_YELLOW 25u
#define FEAT_BATT_WIDEST "100%"
#define FEAT_BATT_TEXT_SIZE sizeof(FEAT_BATT_WIDEST)

struct feat_batt_state {
	struct fenykl_hud_elem elem;
	struct main_batt model;
	uint32_t src;		/* power source of the last tick */
	uint8_t shown;		/* % on screen, MAIN_BATT_NONE while hidden */
	uint32_t vbat;		/* last read */
	uint32_t reads;
	uint32_t standby;	/* reads skipped: source check off */
	uint32_t busy;		/* reads skipped: the battery check held the A/D */
	uint32_t changes;	/* values drawn */
	uint32_t refused;	/* renders refused by the library */
};

static struct feat_batt_state feat_batt_st = {
	.elem = {
		.x = MAIN_LAYOUT_BATT_X,
		.y = MAIN_LAYOUT_BATT_Y,
		.w = MAIN_LAYOUT_BATT_W,
		.h = FENYKL_HUD_LV_LINE_H,
		.bg = MAIN_LAYOUT_BG,
		.align = MAIN_LAYOUT_ALIGN,
		.style = MAIN_LAYOUT_STYLE,
		.ink = FENYKL_HUD_GREEN,
	},
	.shown = MAIN_BATT_NONE,
};

static const uint8_t feat_batt_inks[][3] = {
	FENYKL_HUD_GREEN, FENYKL_HUD_YELLOW,
#ifdef FENYKL_HUD_RED
	FENYKL_HUD_RED,
#else
	FENYKL_HUD_YELLOW,	/* until feat_batt_red finds one */
#endif
};

static FENYKL_LOG_MODULE(feat_batt_log, "batt", FENYKL_LOG_INFO);

FENYKL_CFG_BOOL(feat_batt_show, "main.batt.show", "Battery %", &main_grp_hud, 1);

static int feat_batt_start(void)
{
	struct feat_batt_state *st = &feat_batt_st;
	int w = fenykl_hud_text_width(MAIN_LAYOUT_STYLE, FEAT_BATT_WIDEST);
	int r;

	main_batt_reset(&st->model);
	if (w < 0) {
		fenykl_log_err(&feat_batt_log, "text width %d: no usable font", w);
		return w;
	}
	if (w + FENYKL_HUD_LV_MARGIN > st->elem.w) {
		fenykl_log_err(&feat_batt_log, "%d px text does not fit the %d px element", w,
			       st->elem.w);
		return FENYKL_HUD_EGLYPH;
	}
	r = fenykl_hud_add(&st->elem);
	fenykl_log_info(&feat_batt_log, "element %dx%d at (%d,%d)", st->elem.w, st->elem.h,
			st->elem.x, st->elem.y);
	if (r)
		fenykl_log_err(&feat_batt_log, "hud add %d", r);
	return r;
}

static void feat_batt_hide(void)
{
	struct feat_batt_state *st = &feat_batt_st;

	if (st->shown == MAIN_BATT_NONE)
		return;
	fenykl_hud_hide(&st->elem);
	st->shown = MAIN_BATT_NONE;
	fenykl_log_dbg(&feat_batt_log, "hidden (source %x, show %d)", st->src, feat_batt_show);
}

/* vbat16 into *v; false if the read was skipped. */
static bool feat_batt_read(uint32_t *v)
{
	struct feat_batt_state *st = &feat_batt_st;
	int r = fenykl_batt_read(v);

	if (r == FENYKL_BATT_EBUSY) {
		st->busy++;
		return false;
	}
	if (r == FENYKL_BATT_ESTANDBY) {
		st->standby++;
		return false;
	}
	st->reads++;
	return true;
}

#ifndef FENYKL_HUD_RED
#define FEAT_BATT_RED_RGB 0xFF0000u

/* No fixed red index (hud.h): the live palette's nearest opaque red, outlined in black like the
   E-PL3's red ink; ink is left alone without one. */
static void feat_batt_red(uint8_t *ink)
{
	int red = fenykl_hud_color(FEAT_BATT_RED_RGB);

	if (red < 0)
		return;
	ink[0] = FENYKL_HUD_BLACK;
	ink[1] = (uint8_t)red;
	ink[2] = (uint8_t)red;
}
#endif

static void feat_batt_text(char *s, unsigned int pct)
{
	char d[3];
	unsigned int n = 0;

	do {
		d[n++] = (char)('0' + pct % 10);
		pct /= 10;
	} while (pct);
	while (n)
		*s++ = d[--n];
	*s++ = '%';
	*s = 0;
}

static void feat_batt_tick(void)
{
	struct feat_batt_state *st = &feat_batt_st;
	uint32_t src = fenykl_power_src();
	char text[FEAT_BATT_TEXT_SIZE];
	unsigned int pct, ink;
	int r;

	if (src != st->src) {
		fenykl_log_info(&feat_batt_log, "source %x (was %x)", src, st->src);
		st->src = src;
		main_batt_reset(&st->model);
	}
	if (!feat_batt_show || !fenykl_power_src_batt(src)) {
		feat_batt_hide();
		return;
	}
	/* the model's load compensation is the live view's */
	if (!fenykl_gui_live_view() || !feat_batt_read(&st->vbat))
		return;
	pct = main_batt_add(&st->model, st->vbat);
	if (pct == st->shown)
		return;
	ink = pct > FEAT_BATT_GREEN ? 0 : pct > FEAT_BATT_YELLOW ? 1 : 2;
	for (unsigned int i = 0; i < 3; i++)
		st->elem.ink[i] = feat_batt_inks[ink][i];
#ifndef FENYKL_HUD_RED
	if (ink == 2)
		feat_batt_red(st->elem.ink);
#endif
	feat_batt_text(text, pct);
	r = fenykl_hud_text(&st->elem, text);
	if (r) {
		/* retried every tick: log the first only */
		if (st->refused++ == 0)
			fenykl_log_warn(&feat_batt_log, "%u%% not drawn: %d", pct, r);
		return;
	}
	st->shown = (uint8_t)pct;
	st->changes++;
	fenykl_log_dbg(&feat_batt_log, "%u%%: vbat %u filtered %u/16 cell %u mV", pct, st->vbat,
		       st->model.ema, main_batt_cell_mv(st->model.ema));
}

static void feat_batt_stop(void)
{
	feat_batt_hide();
}

static void feat_batt_status(void)
{
	const struct feat_batt_state *st = &feat_batt_st;

	fenykl_log_info(&feat_batt_log, "shown %u vbat %u cell %u mV source %x", st->shown, st->vbat,
			main_batt_cell_mv(st->model.ema), st->src);
	fenykl_log_info(&feat_batt_log, "  changes %u reads %u standby %u busy %u", st->changes,
			st->reads, st->standby, st->busy);
	if (st->refused)
		fenykl_log_warn(&feat_batt_log, "refused %u", st->refused);
}

const struct main_feat feat_batt = {
	.name = "batt",
	.log = &feat_batt_log,
	.period = 1000u / MAIN_TICK_MS,
	.start = feat_batt_start,
	.tick = feat_batt_tick,
	.stop = feat_batt_stop,
	.status = feat_batt_status,
};
