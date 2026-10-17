/*
 * main: the focus feature's show/hide and outlier rules, free of
 * firmware calls so that the simulator test (tests/sim/main/) runs the real code.
 */
#ifndef _FEAT_FOCUS_FILTER_H
#define _FEAT_FOCUS_FILTER_H

#include <stdbool.h>
#include <stdint.h>

#define FEAT_FOCUS_HIDDEN 0u		/* sample without a lens or without a value */
#define FEAT_FOCUS_JUMP 5u		/* a change by this factor or more needs a second tick */

struct feat_focus_filter {
	uint32_t shown;		/* value on screen, FEAT_FOCUS_HIDDEN if none; set by the caller */
	uint32_t pending;	/* a jump seen once, waiting for its second tick; 0 if none */
	uint32_t unstable;	/* ticks whose two reads differed */
	uint32_t held;		/* jumps held back for a tick */
};

/*
 * One tick: a and b are the two reads of obj_dst in mm (FEAT_FOCUS_HIDDEN without a lens), show
 * the option. Returns true with *out set when the element is to change from f->shown; the caller
 * sets f->shown = *out once it is drawn.
 */
bool feat_focus_filter(struct feat_focus_filter *f, uint32_t a, uint32_t b, bool show,
		       uint32_t *out);

#endif
