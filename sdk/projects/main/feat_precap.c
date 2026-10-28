/*
 * main: pre-capture (sdk/docs/precap.md), one option on the "Shooting" page. The library's task does
 * the work; this feature starts it, follows the option and shows "PRE" while a half press starts a
 * pre-capture burst, "PRE!" after a fail-safe. The HUD draws only on the INFO screens, so nothing is
 * shown during the burst itself.
 */
#include "main.h"
#include <precap.h>

#define FEAT_PRECAP_WIDEST "PRE!"

enum feat_precap_tag { FEAT_PRECAP_NONE, FEAT_PRECAP_ARMED, FEAT_PRECAP_FAILED };

struct feat_precap_state {
	struct fenykl_hud_elem elem;
	enum feat_precap_tag shown;
	uint32_t changes;	/* tags drawn or hidden */
	uint32_t refused;	/* renders refused by the library */
};

static struct feat_precap_state feat_precap_st = {
	.elem = {
		.x = MAIN_LAYOUT_PRECAP_X,
		.y = MAIN_LAYOUT_PRECAP_Y,
		.w = MAIN_LAYOUT_PRECAP_W,
		.h = FENYKL_HUD_LV_LINE_H,
		.bg = MAIN_LAYOUT_BG,
		.align = MAIN_LAYOUT_ALIGN,
		.style = MAIN_LAYOUT_STYLE,
		.ink = FENYKL_HUD_WHITE,
	},
};

static const uint8_t feat_precap_inks[][3] = { FENYKL_HUD_WHITE, FENYKL_HUD_YELLOW };
static const char *const feat_precap_texts[] = { "PRE", "PRE!" };

static void feat_precap_apply(const struct fenykl_cfg_opt *o, int32_t old);

static FENYKL_CFG_GROUP(feat_precap_grp, "Shooting", 4);
FENYKL_CFG_BOOL(feat_precap_on, "main.precap.on", "Pre-capture", &feat_precap_grp, 0,
		.on_change = feat_precap_apply);

static void feat_precap_apply(const struct fenykl_cfg_opt *o, int32_t old)
{
	(void)o;
	(void)old;
	fenykl_precap_enable(feat_precap_on);
}

static int feat_precap_start(void)
{
	struct feat_precap_state *st = &feat_precap_st;
	int w = fenykl_hud_text_width(MAIN_LAYOUT_STYLE, FEAT_PRECAP_WIDEST);
	int r = fenykl_precap_start();

	if (r)
		return r;
	fenykl_precap_enable(feat_precap_on);
	if (w < 0) {
		fenykl_log_err(&fenykl_precap_log, "text width %d: no usable font", w);
		return w;
	}
	if (w + FENYKL_HUD_LV_MARGIN > st->elem.w) {
		fenykl_log_err(&fenykl_precap_log, "%d px tag does not fit the %d px element", w,
			       st->elem.w);
		return FENYKL_HUD_EGLYPH;
	}
	r = fenykl_hud_add(&st->elem);
	if (r)
		fenykl_log_err(&fenykl_precap_log, "hud add %d", r);
	return r;
}

static enum feat_precap_tag feat_precap_tag(void)
{
	switch (fenykl_precap_state()) {
	case FENYKL_PRECAP_ARMED:
	case FENYKL_PRECAP_RING:
	case FENYKL_PRECAP_SAVE:
		return FEAT_PRECAP_ARMED;
	case FENYKL_PRECAP_FAILED:
		return FEAT_PRECAP_FAILED;
	default:
		return FEAT_PRECAP_NONE;
	}
}

static void feat_precap_tick(void)
{
	struct feat_precap_state *st = &feat_precap_st;
	enum feat_precap_tag tag = feat_precap_tag();
	int r;

	if (tag == st->shown)
		return;
	if (tag == FEAT_PRECAP_NONE) {
		fenykl_hud_hide(&st->elem);
		st->shown = tag;
		st->changes++;
		return;
	}
	for (unsigned int i = 0; i < 3; i++)
		st->elem.ink[i] = feat_precap_inks[tag - 1][i];
	r = fenykl_hud_text(&st->elem, feat_precap_texts[tag - 1]);
	if (r) {
		/* retried every tick: log the first only */
		if (st->refused++ == 0)
			fenykl_log_warn(&fenykl_precap_log, "tag not drawn: %d", r);
		return;
	}
	st->shown = tag;
	st->changes++;
}

static void feat_precap_stop(void)
{
	struct feat_precap_state *st = &feat_precap_st;

	if (st->shown != FEAT_PRECAP_NONE)
		fenykl_hud_hide(&st->elem);
	st->shown = FEAT_PRECAP_NONE;
}

static void feat_precap_status(void)
{
	const struct feat_precap_state *st = &feat_precap_st;

	fenykl_precap_status();
	fenykl_log_info(&fenykl_precap_log, "  tag %u changes %u, HUD hook calls in bursts %u",
			st->shown, st->changes, main_hooks_precap_calls());
	if (st->refused)
		fenykl_log_warn(&fenykl_precap_log, "  tag refused %u", st->refused);
}

const struct main_feat feat_precap = {
	.name = "precap",
	.log = &fenykl_precap_log,
	.period = MAIN_SLOW,
	.start = feat_precap_start,
	.tick = feat_precap_tick,
	.stop = feat_precap_stop,
	.status = feat_precap_status,
};
