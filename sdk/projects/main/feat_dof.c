/*
 * main: depth-of-field element: the total depth from the focus distance,
 * the focal length and the shooting aperture (main_dof.c), under a "DoF" label below the focus
 * element; rendered on change only. At level debug every change logs its inputs, so the camera run
 * can check them against `exiftool -DOF` of a JPEG.
 */
#include "main.h"
#include "main_dof.h"
#include "main_fmt.h"
#include "feat_focus_filter.h"
#include <body.h>

#define FEAT_DOF_COC_UM 15u		/* ExifTool's c for 4/3: diagonal 21.6 mm / 1440 */
#define FEAT_DOF_PAIR_MS 2
#define FEAT_DOF_LABEL "DoF\n"
#define FEAT_DOF_TEXT_SIZE (sizeof(FEAT_DOF_LABEL) - 1 + MAIN_FMT_DIST_SIZE)

struct feat_dof_state {
	struct fenykl_hud_elem elem;
	struct feat_focus_filter filter;	/* the focus distance, as the focus element's */
	char text[FEAT_DOF_TEXT_SIZE];		/* on screen, "" while hidden */
	uint32_t changes;	/* values drawn */
	uint32_t refused;	/* renders refused by the library (font or glyph) */
};

static struct feat_dof_state feat_dof_st = {
	.elem = {
		.x = MAIN_LAYOUT_X,
		.y = MAIN_LAYOUT_DOF_Y,
		.w = MAIN_LAYOUT_W,
		.h = MAIN_LAYOUT_DOF_H,
		.bg = MAIN_LAYOUT_BG,
		.align = MAIN_LAYOUT_ALIGN,
		.style = MAIN_LAYOUT_STYLE,
		.ink = FENYKL_HUD_WHITE,
	},
};

static FENYKL_LOG_MODULE(feat_dof_log, "dof", FENYKL_LOG_INFO);

FENYKL_CFG_BOOL(feat_dof_show, "main.dof.show", "Depth of field", &main_grp_hud, 1);

static int feat_dof_start(void)
{
	return feat_focus_add(&feat_dof_st.elem, FEAT_DOF_LABEL, &feat_dof_log);
}

static void feat_dof_hide(void)
{
	struct feat_dof_state *st = &feat_dof_st;

	if (!st->text[0])
		return;
	fenykl_hud_hide(&st->elem);
	st->text[0] = 0;
	fenykl_log_dbg(&feat_dof_log, "hidden (distance %u, show %d)", st->filter.shown,
		       feat_dof_show);
}

static bool feat_dof_same(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static void feat_dof_tick(void)
{
	struct feat_dof_state *st = &feat_dof_st;
	char text[FEAT_DOF_TEXT_SIZE] = FEAT_DOF_LABEL;
	struct main_dof d;
	uint32_t a, b, s, f, av;
	int r;

	a = feat_focus_read();
	fenykl_task_sleep(FEAT_DOF_PAIR_MS);
	b = feat_focus_read();
	if (feat_focus_filter(&st->filter, a, b, feat_dof_show, &s))
		st->filter.shown = s;
	s = st->filter.shown;
	if (s == FEAT_FOCUS_HIDDEN) {
		feat_dof_hide();
		return;
	}
	f = fenykl_lens_focal_mm();
	av = fenykl_lens_av();
	/* Av 0 is f/1.0, but also the prop before AE has set it */
	if (!av || !main_dof_calc(&d, s, f, av, FEAT_DOF_COC_UM)) {
		feat_dof_hide();
		return;
	}
	main_fmt_dist(text + sizeof(FEAT_DOF_LABEL) - 1, d.depth);
	if (feat_dof_same(text, st->text))
		return;
	r = fenykl_hud_text(&st->elem, text);
	if (r) {
		/* retried every tick: log the first only */
		if (st->refused++ == 0)
			fenykl_log_warn(&feat_dof_log, "%u mm not drawn: %d", d.depth, r);
		return;
	}
	for (unsigned int i = 0; i < sizeof(text); i++)
		st->text[i] = text[i];
	st->changes++;
	fenykl_log_dbg(&feat_dof_log, "s %u f %u av %u iris %u", s, f, av,
		       fenykl_lens_iris_av());
	fenykl_log_dbg(&feat_dof_log, "  N1000 %u near %u far %u dof %u", d.n1000, d.near, d.far,
		       d.depth);
}

static void feat_dof_stop(void)
{
	feat_dof_hide();
}

static void feat_dof_status(void)
{
	const struct feat_dof_state *st = &feat_dof_st;

	fenykl_log_info(&feat_dof_log, "distance %u changes %u unstable %u held %u",
			st->filter.shown, st->changes, st->filter.unstable, st->filter.held);
	if (st->refused)
		fenykl_log_warn(&feat_dof_log, "refused %u", st->refused);
}

const struct main_feat feat_dof = {
	.name = "dof",
	.log = &feat_dof_log,
	.period = MAIN_SLOW,
	.start = feat_dof_start,
	.tick = feat_dof_tick,
	.stop = feat_dof_stop,
	.status = feat_dof_status,
};
