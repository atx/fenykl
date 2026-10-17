/* The main plugin's focus show/hide and outlier rules (sdk/projects/main/feat_focus_filter.c) in
   the GDB simulator, with ../hooks/sim_start.S. One filter runs through t_steps in order;
   plugin_main sets sim_exit_code to 0 (pass), 1 + the index of the first step whose result differs,
   or 200 if the counters at the end differ. */
#include "../../../sdk/projects/main/feat_focus_filter.h"

#define T_INF 0xFFFFFFFFu
#define T_KEEP 0xEEEEEEEEu	/* no change expected */

uint32_t sim_exit_code = 99;

struct t_step {
	uint32_t a, b;		/* the two reads */
	uint8_t show;		/* main.focus.show */
	uint8_t drawn;		/* the caller commits a change (0: the render was refused) */
	uint32_t out;		/* expected new value, or T_KEEP */
};

static const struct t_step t_steps[] = {
	{ 0, 0, 1, 1, T_KEEP },			/* no lens at start: stays hidden */
	{ 1000, 1000, 1, 1, 1000 },		/* first value: no jump rule from hidden */
	{ 1000, 1000, 1, 1, T_KEEP },
	{ 1000, 1200, 1, 1, T_KEEP },		/* reads differ: unstable */
	{ 1200, 1200, 1, 1, 1200 },		/* below 5x: at once */
	{ 240, 240, 1, 1, T_KEEP },		/* raw le16 store, 1/5: held */
	{ 1200, 1200, 1, 1, T_KEEP },		/* ... and gone: never shown */
	{ 241, 241, 1, 1, 241 },		/* just under 5x: at once */
	{ 1205, 1205, 1, 1, T_KEEP },		/* exactly 5x up: held */
	{ 1205, 1205, 1, 1, 1205 },		/* second tick: shown */
	{ T_INF, T_INF, 1, 1, T_KEEP },		/* to infinity: held */
	{ T_INF, T_INF, 1, 1, T_INF },
	{ 3000, 3000, 1, 1, T_KEEP },		/* from infinity: held */
	{ 3000, 3001, 1, 1, T_KEEP },		/* unstable tick breaks the pair */
	{ 3000, 3000, 1, 1, T_KEEP },		/* held again */
	{ 40000, 40000, 1, 1, T_KEEP },		/* another jump replaces the pending one */
	{ 3000, 3000, 1, 1, T_KEEP },		/* not consecutive: held again */
	{ 3000, 3000, 1, 0, 3000 },		/* confirmed, render refused */
	{ 3000, 3000, 1, 1, 3000 },		/* retried as confirmed, drawn */
	{ 3000, 3000, 1, 1, T_KEEP },
	{ 3500, 3500, 1, 0, 3500 },		/* refused small change ... */
	{ 3500, 3500, 1, 1, 3500 },		/* ... retried */
	{ 3500, 3500, 0, 1, 0 },		/* option off: hidden at once */
	{ 3500, 3600, 0, 1, T_KEEP },		/* off: unstable reads do not matter */
	{ T_INF, T_INF, 1, 1, T_INF },		/* option on: from hidden, at once */
	{ 0, 0, 1, 1, 0 },			/* lens removed: hidden at once */
	{ 0, 0, 1, 1, T_KEEP },
	{ 20, 20, 1, 1, 20 },			/* lens back, macro distance */
	{ 2000, 2000, 1, 1, T_KEEP },		/* 100x: held */
	{ 0, 0, 1, 1, 0 },			/* obj_dst 0 hides even with a jump pending */
	{ 2000, 2000, 1, 1, 2000 },		/* from hidden, at once */
};

/* After t_steps: unstable and held counts. */
#define T_UNSTABLE 2u
#define T_HELD 8u

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct feat_focus_filter f = { 0 };
	unsigned int i;

	(void)load_base;
	(void)image_size;
	for (i = 0; i < sizeof(t_steps) / sizeof(t_steps[0]); i++) {
		const struct t_step *s = &t_steps[i];
		uint32_t out = T_KEEP;
		bool changed = feat_focus_filter(&f, s->a, s->b, s->show, &out);

		if (changed != (s->out != T_KEEP) || out != s->out) {
			sim_exit_code = 1 + i;
			return;
		}
		if (changed && s->drawn)
			f.shown = out;
	}
	sim_exit_code = f.unstable == T_UNSTABLE && f.held == T_HELD ? 0 : 200;
}
