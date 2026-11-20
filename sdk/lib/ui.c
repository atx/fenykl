/*
 * Settings tab, option core (sdk/docs/ui.md): the option list and its limits, the values, the cfg
 * calls and the install's start, stock words and saved state every body shares. The camera's part
 * (ui_<camera>.c) builds the menu tab from the list and edits the values with the stock pickers.
 */
#include "ui_int.h"

struct fui_state fui;

FENYKL_LOG_MODULE(fenykl_ui_log, "ui", FENYKL_LOG_INFO);

/* --- option list --- */

static bool fui_text_ok(const char *s)
{
	unsigned int n = 0;

	while (s[n] && n <= FUI_TEXT_MAX)
		n++;
	return n <= FUI_TEXT_MAX;
}

unsigned int fui_nvalues(const struct fenykl_cfg_opt *o)
{
	return o->type == FENYKL_CFG_BOOL ? 2u : (unsigned int)o->max + 1u;
}

unsigned int fui_value(const struct fenykl_cfg_opt *o)
{
	int32_t v = *o->value;

	return v >= 0 && (uint32_t)v < fui_nvalues(o) ? (unsigned int)v : 0u;
}

/* An option the tab can show: BOOL, or ENUM with few enough names; labels short enough. */
static bool fui_opt_ok(const struct fenykl_cfg_opt *o)
{
	const char *why = NULL;

	if (o->type != FENYKL_CFG_BOOL && o->type != FENYKL_CFG_ENUM)
		why = "not BOOL or ENUM";
	else if (fui_nvalues(o) > FENYKL_UI_VALUES_MAX)
		why = "too many values";
	else if (!fui_text_ok(o->label) || !fui_text_ok(o->group->label))
		why = "label or group name too long";
	for (unsigned int i = 0; !why && o->type == FENYKL_CFG_ENUM && i < fui_nvalues(o); i++)
		if (!fui_text_ok(o->names[i]))
			why = "value name too long";
	if (why)
		fenykl_log_err(&fenykl_ui_log, "option %s: %s", o->name, why);
	return !why;
}

int fui_collect(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end)
{
	const struct fenykl_cfg_group *groups[FENYKL_UI_OPTS_MAX];
	const struct fenykl_cfg_opt *o;
	unsigned int n = 0, ng = 0, cells = 0, i;
	bool ok = true;

	for (o = begin; o < end; o++) {
		if (!o->group)
			continue;
		n++;
		cells += fui_nvalues(o);
		ok = fui_opt_ok(o) && ok;
	}
	if (!n || n > FENYKL_UI_OPTS_MAX) {
		fenykl_log_err(&fenykl_ui_log, "%u options with a group, 1..%u shown", n,
			       FENYKL_UI_OPTS_MAX);
		return FENYKL_UI_EOPTS;
	}
	if (cells > FENYKL_UI_CELLS_MAX) {
		fenykl_log_err(&fenykl_ui_log, "%u values in all, at most %u", cells,
			       FENYKL_UI_CELLS_MAX);
		return FENYKL_UI_EOPTS;
	}
	if (!ok)
		return FENYKL_UI_EOPTS;
	for (o = begin; o < end; o++) {
		const struct fenykl_cfg_group *g = o->group;

		if (!g)
			continue;
		for (i = 0; i < ng && groups[i] != g; i++)
			;
		if (i < ng)
			continue;
		/* sorted by order; equal orders keep link order */
		for (i = ng; i > 0 && groups[i - 1]->order > g->order; i--)
			groups[i] = groups[i - 1];
		groups[i] = g;
		ng++;
	}
	fui.nopts = 0;
	for (i = 0; i < ng; i++)
		for (o = begin; o < end; o++)
			if (o->group == groups[i])
				fui.opts[fui.nopts++] = o;
	return 0;
}

/* An option of the caller's table (fenykl_ui_install_opts): stored, never saved. */
static int fui_set_ram(const struct fenykl_cfg_opt *o, int32_t v)
{
	int32_t old = *o->value;

	if (v < o->min || v > o->max)
		return FENYKL_CFG_EINVAL;
	*o->value = v;
	if (old != v && o->on_change)
		o->on_change(o, old);
	return 0;
}

void fui_set(const struct fenykl_cfg_opt *o, int32_t v)
{
	bool cfg = o >= fenykl_cfg_start && o < fenykl_cfg_end;
	int r = cfg ? fenykl_cfg_set(o, v) : fui_set_ram(o, v);

	if (r) {
		fui.st.set_fail++;
		fenykl_log_warn(&fenykl_ui_log, "set %s = %d failed: %d", o->name, v, r);
		return;
	}
	fui.st.sets++;
	fenykl_log_info(&fenykl_ui_log, "set %s = %d", o->name, v);
}

/* --- lifecycle, called by the camera's part --- */

bool fui_count_core(void)
{
	if (FENYKL_CORE_ID() == 0)
		return false;
	fui.st.cpu1_actions++;
	return true;
}

void fui_list_open(void)
{
	struct fenykl_cfg_stats cs;

	fenykl_cfg_get_stats(&cs);
	fui.fails_at_open = cs.save_fail;
	fui.unsaved = false;
	fui.st.opens++;
}

int fui_prepare(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end)
{
	if (fui.installed)
		return FENYKL_UI_EBUSY;
	return fui_collect(begin, end);
}

int fenykl_ui_install(void)
{
	return fenykl_ui_install_opts(fenykl_cfg_start, fenykl_cfg_end);
}

bool fui_check_words(const struct fui_expect *tab, unsigned int n)
{
	bool ok = true;

	for (unsigned int i = 0; i < n; i++) {
		const struct fui_expect *e = &tab[i];
		uint32_t v = e->size == 1 ? fenykl_rd8(e->addr) :
			     e->size == 2 ? fenykl_rd16(e->addr) : fenykl_rd32(e->addr);

		if (v == e->value)
			continue;
		ok = false;
		fenykl_log_err(&fenykl_ui_log, "stock MISMATCH %s %08x = %x, expected %x", e->what,
			       e->addr, v, e->value);
	}
	return ok;
}

void fui_tick_unsaved(void)
{
	struct fenykl_cfg_stats st;
	bool dirty = fenykl_cfg_dirty(), unsaved;

	fenykl_cfg_get_stats(&st);
	unsaved = dirty && (!fenykl_card_io_allowed() || st.save_fail != fui.fails_at_open);
	if (unsaved && !fui.unsaved)
		fenykl_log_warn(&fenykl_ui_log, "settings not saved: card io %d, save fails %u",
				fenykl_card_io_allowed(), st.save_fail);
	fui.unsaved = unsaved;
}

void fenykl_ui_get_stats(struct fenykl_ui_stats *st)
{
	*st = fui.st;
}
