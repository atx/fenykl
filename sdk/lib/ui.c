/*
 * Settings page, option core (sdk/docs/ui.md): the flat option list, the key and dial semantics,
 * the value text, the cfg calls and the page lifecycle every body shares (open and close, the
 * install's start and stock words, the saved state). The camera's part (ui_<camera>.c) opens the
 * page from a menu row, draws it with the stock menu list view and feeds it the keys.
 */
#include "ui_int.h"

struct fui_state fui;

FENYKL_LOG_MODULE(fenykl_ui_log, "ui", FENYKL_LOG_INFO);

/* --- page model (GUI task) --- */

int fui_collect(const struct fenykl_cfg_opt *begin, const struct fenykl_cfg_opt *end)
{
	const struct fenykl_cfg_group *groups[FENYKL_UI_OPTS_MAX];
	const struct fenykl_cfg_opt *o;
	unsigned int n = 0, ng = 0, i;

	for (o = begin; o < end; o++)
		if (o->group)
			n++;
	if (!n || n > FENYKL_UI_OPTS_MAX)
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
	fui.cursor = 0;
	fui.edit = false;
	return 0;
}

static void fui_set(const struct fenykl_cfg_opt *o, int32_t v)
{
	int r = fenykl_cfg_set(o, v);

	if (r) {
		fui.st.set_fail++;
		fenykl_log_warn(&fenykl_ui_log, "set %s = %d failed: %d", o->name, v, r);
		return;
	}
	fui.st.sets++;
	fenykl_log_info(&fenykl_ui_log, "set %s = %d", o->name, v);
}

static void fui_edit_clamp(const struct fenykl_cfg_opt *o, int64_t v)
{
	fui.edit_val = (int32_t)(v < o->min ? o->min : v > o->max ? o->max : v);
}

static enum fui_result fui_edit_key(const struct fenykl_cfg_opt *o, unsigned int code)
{
	int64_t step = o->step > 0 ? o->step : 1, v = fui.edit_val;

	switch (code) {
	case FUI_KEY_LEFT:
		v -= step;
		break;
	case FUI_KEY_RIGHT:
		v += step;
		break;
	case FUI_KEY_OK:
		fui.edit = false;
		fui_set(o, fui.edit_val);
		return FUI_REDRAW;
	case FUI_KEY_MENU:
		fui.edit = false;
		return FUI_REDRAW;
	default:
		return FUI_NONE;
	}
	fui_edit_clamp(o, v);
	return FUI_REDRAW;
}

/* LEFT, RIGHT or OK on the cursor row. */
static enum fui_result fui_row_key(const struct fenykl_cfg_opt *o, unsigned int code)
{
	int d = code == FUI_KEY_LEFT ? -1 : 1;

	switch (o->type) {
	case FENYKL_CFG_BOOL:
		fui_set(o, !*o->value);
		return FUI_REDRAW;
	case FENYKL_CFG_ENUM:
		fui_set(o, (*o->value + d + o->max + 1) % (o->max + 1));
		return FUI_REDRAW;
	case FENYKL_CFG_INT:
		if (code == FUI_KEY_LEFT)
			return FUI_NONE;
		fui.edit = true;
		fui.edit_val = *o->value;
		return FUI_REDRAW;
	}
	return FUI_NONE;
}

enum fui_result fui_key(unsigned int code)
{
	const struct fenykl_cfg_opt *o = fui.opts[fui.cursor];

	if (fui.edit)
		return fui_edit_key(o, code);
	switch (code) {
	case FUI_KEY_UP:
		fui.cursor = (fui.cursor + fui.nopts - 1) % fui.nopts;
		return FUI_REDRAW;
	case FUI_KEY_DOWN:
		fui.cursor = (fui.cursor + 1) % fui.nopts;
		return FUI_REDRAW;
	case FUI_KEY_MENU:
		return FUI_LEAVE;
	case FUI_KEY_LEFT:
	case FUI_KEY_RIGHT:
	case FUI_KEY_OK:
		return fui_row_key(o, code);
	}
	return FUI_NONE;
}

/* n dial steps: n > 0 moves the cursor down and increases an edited value. */
enum fui_result fui_dial(int n)
{
	const struct fenykl_cfg_opt *o = fui.opts[fui.cursor];
	int64_t step = o->step > 0 ? o->step : 1;
	int c;

	if (!n)
		return FUI_NONE;
	if (fui.edit) {
		fui_edit_clamp(o, fui.edit_val + (int64_t)n * FUI_DIAL_STEPS * step);
		return FUI_REDRAW;
	}
	c = ((int)fui.cursor + n) % (int)fui.nopts;
	fui.cursor = c < 0 ? c + fui.nopts : c;
	return FUI_REDRAW;
}

/* --- value text --- */

static uint16_t *fui_put(uint16_t *p, const uint16_t *end, const char *s)
{
	while (*s && p < end)
		*p++ = (uint8_t)*s++;
	return p;
}

static void fui_dec(char *p, int32_t v)
{
	char t[10];
	uint32_t u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v;
	int n = 0;

	if (v < 0)
		*p++ = '-';
	do
		t[n++] = (char)('0' + u % 10);
	while (u /= 10);
	while (n)
		*p++ = t[--n];
	*p = 0;
}

void fui_value_text(const struct fenykl_cfg_opt *o, uint16_t out[FUI_VALUE_LEN])
{
	const uint16_t *end = out + FUI_VALUE_LEN - 1;
	bool editing = fui.edit && o == fui.opts[fui.cursor];
	uint16_t *p = out;
	char num[12];

	switch (o->type) {
	case FENYKL_CFG_BOOL:
		p = fui_put(p, end, *o->value ? "On" : "Off");
		break;
	case FENYKL_CFG_ENUM:
		p = fui_put(p, end, o->names[*o->value]);
		break;
	case FENYKL_CFG_INT:
		fui_dec(num, editing ? fui.edit_val : *o->value);
		if (editing)
			*p++ = FUI_ARROW_LEFT;
		p = fui_put(p, end, num);
		if (editing && p < end)
			*p++ = FUI_ARROW_RIGHT;
		break;
	}
	*p = 0;
}

/* --- page lifecycle, called by the camera's part --- */

bool fui_count_core(void)
{
	if (FENYKL_CORE_ID() == 0)
		return false;
	fui.st.cpu1_actions++;
	return true;
}

void fui_page_open(void)
{
	struct fenykl_cfg_stats cs;

	fenykl_cfg_get_stats(&cs);
	fui.fails_at_open = cs.save_fail;
	fui.unsaved = false;
	fui.edit = false;
	fui.st.entries++;
}

void fui_page_close(void)
{
	fui.st.exits++;
	if (!fui.edit)
		return;
	fui.edit = false;
	fenykl_log_info(&fenykl_ui_log, "edit of %s dropped", fui.opts[fui.cursor]->name);
}

int fui_prepare(void)
{
	int r;

	if (fui.installed)
		return FENYKL_UI_EBUSY;
	r = fui_collect(fenykl_cfg_start, fenykl_cfg_end);
	if (r)
		fenykl_log_err(&fenykl_ui_log, "no option with a group, or more than %u",
			       FENYKL_UI_OPTS_MAX);
	return r;
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
