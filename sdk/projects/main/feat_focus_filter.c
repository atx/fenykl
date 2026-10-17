/*
 * main: the focus feature's show/hide and outlier rules.
 */
#include "feat_focus_filter.h"

/* The parser stores the raw le16 before the x5 value: a one-read outlier at exactly 1/5. */
static bool feat_focus_jump(uint32_t from, uint32_t to)
{
	if (from == FEAT_FOCUS_HIDDEN)
		return false;
	return to / FEAT_FOCUS_JUMP >= from || from / FEAT_FOCUS_JUMP >= to;
}

bool feat_focus_filter(struct feat_focus_filter *f, uint32_t a, uint32_t b, bool show,
		       uint32_t *out)
{
	uint32_t v = show ? a : FEAT_FOCUS_HIDDEN;

	if (show && a != b) {
		f->unstable++;
		f->pending = 0;
		return false;
	}
	if (v == f->shown) {
		f->pending = 0;
		return false;
	}
	if (v != FEAT_FOCUS_HIDDEN && feat_focus_jump(f->shown, v) && v != f->pending) {
		f->held++;
		f->pending = v;
		return false;
	}
	/* a confirmed jump stays pending until drawn, so a refused render is retried as confirmed */
	if (v != f->pending)
		f->pending = 0;
	*out = v;
	return true;
}
