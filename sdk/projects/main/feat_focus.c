/*
 * main: focus distance element. Reads obj_dst twice per tick, filters
 * (feat_focus_filter.c), formats (main_fmt.c) and renders on change only, under an "FD" label
 * like the DoF element's.
 */
#include "main.h"
#include "main_fmt.h"
#include "feat_focus_filter.h"

#define FEAT_FOCUS_PAIR_MS 2		/* between the two reads */
#define FEAT_FOCUS_LABEL "FD\n"
#define FEAT_FOCUS_TEXT_SIZE (sizeof(FEAT_FOCUS_LABEL) - 1 + MAIN_FMT_DIST_SIZE)

struct feat_focus_state {
	struct fenykl_hud_elem elem;
	struct feat_focus_filter filter;
	char text[FEAT_FOCUS_TEXT_SIZE];
	uint32_t changes;	/* values drawn */
	uint32_t refused;	/* renders refused by the library (font or glyph) */
};

static struct feat_focus_state feat_focus_st = {
	.elem = {
		.x = MAIN_LAYOUT_X,
		.y = MAIN_LAYOUT_FOCUS_Y,
		.w = MAIN_LAYOUT_W,
		.h = MAIN_LAYOUT_FOCUS_H,
		.bg = MAIN_LAYOUT_BG,
		.align = MAIN_LAYOUT_ALIGN,
		.style = MAIN_LAYOUT_STYLE,
		.ink = FENYKL_HUD_WHITE,
	},
	.text = FEAT_FOCUS_LABEL,
};

static FENYKL_LOG_MODULE(feat_focus_log, "focus", FENYKL_LOG_INFO);

FENYKL_CFG_BOOL(feat_focus_show, "main.focus.show", "Focus distance", &main_grp_hud, 1);

/* obj_dst in mm, FEAT_FOCUS_HIDDEN without a lens */
uint32_t feat_focus_read(void)
{
	if (*(volatile uint16_t *)FW_LENS_ATTACHED == 0)
		return FEAT_FOCUS_HIDDEN;
	return *(volatile uint32_t *)FW_LENS_OBJ_DST;
}

/* Width of the widest line of any main_fmt_dist text in the layout style, or FENYKL_HUD_E*.
   Digits are drawn with the widest digit. */
static int feat_focus_widest(void)
{
	static const char *const forms[] = {
		"##\nmm", "##.#\ncm", "#.##\nm", "##.#\nm", "###\nm",
		"#.##\nkm", "##.#\nkm", "####\nkm", "inf",
	};
	char digit = '0';
	int digit_w = 0, widest = 0;

	for (char c = '0'; c <= '9'; c++) {
		const char s[2] = { c, 0 };
		int w = fenykl_hud_text_width(MAIN_LAYOUT_STYLE, s);

		if (w < 0)
			return w;
		if (w > digit_w)
			digit = c, digit_w = w;
	}
	for (unsigned int f = 0; f < sizeof(forms) / sizeof(forms[0]); f++) {
		char s[FENYKL_HUD_TEXT_MAX + 1];
		unsigned int i;
		int w;

		for (i = 0; forms[f][i]; i++)
			s[i] = forms[f][i] == '#' ? digit : forms[f][i];
		s[i] = 0;
		w = fenykl_hud_text_width(MAIN_LAYOUT_STYLE, s);
		if (w < 0)
			return w;
		if (w > widest)
			widest = w;
	}
	return widest;
}

/* Adds e for a label line over any main_fmt_dist text, after checking that both fit */
int feat_focus_add(struct fenykl_hud_elem *e, const char *label,
		   const struct fenykl_log_module *log)
{
	int w = feat_focus_widest(), lw = fenykl_hud_text_width(MAIN_LAYOUT_STYLE, label);
	int r;

	if (w < 0 || lw < 0) {
		fenykl_log_err(log, "text width %d/%d: no usable font", w, lw);
		return w < 0 ? w : lw;
	}
	if (lw > w)
		w = lw;
	if (w + FENYKL_HUD_LV_MARGIN > e->w) {
		fenykl_log_err(log, "widest text %d px does not fit the %d px element", w, e->w);
		return FENYKL_HUD_EGLYPH;
	}
	r = fenykl_hud_add(e);
	fenykl_log_info(log, "element %dx%d at (%d,%d)", e->w, e->h, e->x, e->y);
	if (r)
		fenykl_log_err(log, "hud add %d", r);
	return r;
}

static int feat_focus_start(void)
{
	return feat_focus_add(&feat_focus_st.elem, FEAT_FOCUS_LABEL, &feat_focus_log);
}

static void feat_focus_tick(void)
{
	struct feat_focus_state *st = &feat_focus_st;
	uint32_t a, b, v;
	int r;

	a = feat_focus_read();
	fw_OlyTaskSleep(FEAT_FOCUS_PAIR_MS);
	b = feat_focus_read();
	if (!feat_focus_filter(&st->filter, a, b, feat_focus_show, &v))
		return;
	if (v == FEAT_FOCUS_HIDDEN) {
		fenykl_hud_hide(&st->elem);
		st->filter.shown = v;
		fenykl_log_dbg(&feat_focus_log, "hidden (lens %u, show %d)",
			       *(volatile uint16_t *)FW_LENS_ATTACHED, feat_focus_show);
		return;
	}
	main_fmt_dist(st->text + sizeof(FEAT_FOCUS_LABEL) - 1, v);
	r = fenykl_hud_text(&st->elem, st->text);
	if (r) {
		/* retried every tick: log the first only */
		if (st->refused++ == 0)
			fenykl_log_warn(&feat_focus_log, "%u mm not drawn: %d", v, r);
		return;
	}
	st->filter.shown = v;
	st->changes++;
	fenykl_log_dbg(&feat_focus_log, "%u mm", v);
}

static void feat_focus_stop(void)
{
	fenykl_hud_hide(&feat_focus_st.elem);
	feat_focus_st.filter.shown = FEAT_FOCUS_HIDDEN;
}

static void feat_focus_status(void)
{
	const struct feat_focus_state *st = &feat_focus_st;

	fenykl_log_info(&feat_focus_log, "shown %u changes %u unstable %u held %u", st->filter.shown,
			st->changes, st->filter.unstable, st->filter.held);
	if (st->refused)
		fenykl_log_warn(&feat_focus_log, "refused %u", st->refused);
}

const struct main_feat feat_focus = {
	.name = "focus",
	.log = &feat_focus_log,
	.period = MAIN_SLOW,
	.start = feat_focus_start,
	.tick = feat_focus_tick,
	.stop = feat_focus_stop,
	.status = feat_focus_status,
};
