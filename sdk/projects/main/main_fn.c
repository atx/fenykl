/*
 * main: the Button Function entry (sdk/docs/fnfunc.md). The action (act_shot) appears as
 * "Screenshot" (help "(Fenykl) ...") in the stock function pickers of Fn1, Fn2, Fn3, REC and L-Fn; a press of a
 * button holding it runs the action in the plugin task. The press still reaches the firmware,
 * which treats the entry like Off. Presses while the action is busy are ignored and logged.
 */
#include "main.h"
#include <event.h>
#include <fnfunc.h>

#define MAIN_FN_STOCK_TRIES 5u
#define MAIN_FN_STOCK_TICKS (1000u / MAIN_TICK_MS)	/* between two tries after ESTOCK */

/* The entry's one action; a body without it gets no entry (MAIN_FN). */
#if MAIN_FN
static const struct main_fn_act *const main_fn_act = &act_shot;

struct main_fn_state {
	volatile uint32_t req;		/* presses, written by the press callback */
	volatile uint32_t press_us;
	volatile uint32_t refused;	/* presses outside the action's GUI states */
	volatile uint8_t button;	/* of the last press */
	uint32_t done;			/* presses the task has taken */
	uint32_t ignored;		/* presses while busy */
	unsigned int stock_tries;	/* installs refused with ESTOCK */
	unsigned int wait;		/* ticks until the next try */
	bool busy, installed, failed;
};

static struct main_fn_state main_fn_st;

static FENYKL_LOG_MODULE(main_fn_log, "fn", FENYKL_LOG_INFO);

static const char *const main_fn_buttons[FENYKL_FNFUNC_BUTTONS] = {
	[FENYKL_FNFUNC_FN1] = "Fn1", [FENYKL_FNFUNC_FN2] = "Fn2", [FENYKL_FNFUNC_FN3] = "Fn3",
	[FENYKL_FNFUNC_REC] = "REC", [FENYKL_FNFUNC_LFN] = "L-Fn",
};

/* event hook context */
static void main_fn_press(enum fenykl_fnfunc_button b, void *arg)
{
	struct main_fn_state *st = &main_fn_st;

	(void)arg;
	if (!main_fn_act->claim()) {
		st->refused++;
		return;
	}
	st->press_us = FENYKL_TIMER_NOW();
	st->button = (uint8_t)b;
	st->req++;
}

static struct fenykl_fnfunc main_fn_func = { .press = main_fn_press };

static void main_fn_install(void)
{
	struct main_fn_state *st = &main_fn_st;
	int r;

	main_fn_func.name = main_fn_act->name;
	main_fn_func.help = main_fn_act->help;
	if (st->wait) {
		st->wait--;
		return;
	}
	r = fenykl_fnfunc_install(&main_fn_func);
	if (r == FENYKL_FNFUNC_EAGAIN)
		return;
	/* the boot may still be building what the check reads */
	if (r == FENYKL_FNFUNC_ESTOCK && ++st->stock_tries < MAIN_FN_STOCK_TRIES) {
		fenykl_log_warn(&main_fn_log, "button function install refused, try %u of %u",
				st->stock_tries, MAIN_FN_STOCK_TRIES);
		st->wait = MAIN_FN_STOCK_TICKS;
		return;
	}
	if (r) {
		st->failed = true;
		fenykl_log_err(&main_fn_log, "button function install %d: not in the pickers", r);
		return;
	}
	st->installed = true;
}

void main_fn_tick(void)
{
	struct main_fn_state *st = &main_fn_st;
	uint32_t req = st->req;

	if (!st->installed) {
		if (!st->failed)
			main_fn_install();
		return;
	}
	fenykl_fnfunc_tick();
	if (st->busy) {
		if (req != st->done) {
			st->ignored += req - st->done;
			fenykl_log_info(&main_fn_log, "%u presses while %s is busy: ignored",
					req - st->done, main_fn_act->name);
			st->done = req;
		}
		st->busy = main_fn_act->step();
		return;
	}
	if (req == st->done)
		return;
	if (req - st->done > 1)
		fenykl_log_info(&main_fn_log, "%u presses at once: one taken", req - st->done);
	st->done = req;
	fenykl_log_info(&main_fn_log, "%s press", main_fn_buttons[st->button]);
	st->busy = !main_fn_act->start(st->press_us);
}

void main_fn_set_level(unsigned int level)
{
	fenykl_log_set_level(&main_fn_log, level);
	fenykl_log_set_level(&fenykl_fnfunc_log, level);
	fenykl_log_set_level(main_fn_act->log, level);
}

void main_fn_status(void)
{
	struct main_fn_state *st = &main_fn_st;
	struct fenykl_event_stats es;

	if (!st->installed)
		return;
	fenykl_log_info(&main_fn_log, "%s: presses %u ignored %u refused %u", main_fn_act->name,
			st->done, st->ignored, st->refused);
	fenykl_event_get_stats(&es);
	fenykl_log_dbg(&main_fn_log, "  events %u consumed %u injected %u nested %u", es.events,
		       es.consumed, es.injected, es.nested);
	fenykl_log_dbg(&main_fn_log, "  events bypassed %u cpu1 %u", es.bypassed, es.cpu1);
	main_fn_act->status();
}
#endif
