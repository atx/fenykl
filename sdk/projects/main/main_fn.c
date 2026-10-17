/*
 * main: option "Fn override". A short Fn press runs the chosen action (main_fn_acts[]) in
 * the plugin task; a long press gives the stock Fn function back at the release (long_stock).
 * With "none" nothing is claimed, and the key layer (sdk/docs/key.md) is installed only once an
 * action is chosen. Presses while an action is busy are ignored and logged.
 */
#include "main.h"
#include <key.h>

enum main_fn_action {
	MAIN_FN_NONE,
	MAIN_FN_SHOT,
	MAIN_FN_ACTIONS,
};

static const char *const main_fn_names[] = { "none", "screenshot" };

static const struct main_fn_act *const main_fn_acts[] = {
	[MAIN_FN_NONE] = NULL,
	[MAIN_FN_SHOT] = &act_shot,
};

_Static_assert(sizeof(main_fn_names) / sizeof(main_fn_names[0]) == MAIN_FN_ACTIONS, "names");
_Static_assert(sizeof(main_fn_acts) / sizeof(main_fn_acts[0]) == MAIN_FN_ACTIONS, "actions");

struct main_fn_state {
	volatile uint32_t req;		/* short presses, written by the key handler */
	volatile uint32_t press_us;
	uint32_t done;			/* presses the task has taken */
	uint32_t ignored;		/* presses while busy */
	const struct main_fn_act *busy;
	bool installed, failed;
};

static struct main_fn_state main_fn_st;

static FENYKL_LOG_MODULE(main_fn_log, "fn", FENYKL_LOG_INFO);

static FENYKL_CFG_GROUP(main_grp_keys, "Buttons", 5);
FENYKL_CFG_ENUM(main_fn_action, "main.fn.action", "Fn override", &main_grp_keys, MAIN_FN_NONE,
		main_fn_names);

static const struct main_fn_act *main_fn_cur(void)
{
	int32_t a = main_fn_action;

	return a > MAIN_FN_NONE && a < MAIN_FN_ACTIONS ? main_fn_acts[a] : NULL;
}

/* key task */
static bool main_fn_claim(enum fenykl_key key, uint16_t gui, void *arg)
{
	const struct main_fn_act *act = main_fn_cur();

	(void)key;
	(void)arg;
	return act && act->claim(gui);
}

static void main_fn_on_key(const struct fenykl_key_event *ev, void *arg)
{
	(void)arg;
	if (ev->kind != FENYKL_KEY_SHORT)
		return;
	main_fn_st.press_us = ev->now_us;
	main_fn_st.req++;
}

static struct fenykl_key_handler main_fn_key = {
	.name = "fn", .keys = FENYKL_KEY_BIT(FENYKL_KEY_FN), .claim = main_fn_claim,
	.on = main_fn_on_key, .long_stock = true, .enabled = true,
};

static void main_fn_install(void)
{
	struct main_fn_state *st = &main_fn_st;
	int r = fenykl_key_add(&main_fn_key);

	if (!r)
		r = fenykl_key_install();
	if (r) {
		st->failed = true;
		fenykl_log_err(&main_fn_log, "key install %d: Fn stays stock", r);
		return;
	}
	st->installed = true;
	st->done = st->req;
	fenykl_log_info(&main_fn_log, "key layer installed for %s", main_fn_names[main_fn_action]);
}

void main_fn_tick(void)
{
	struct main_fn_state *st = &main_fn_st;
	const struct main_fn_act *act;
	uint32_t req = st->req;

	if (!st->installed) {
		if (st->failed || !main_fn_cur())
			return;
		main_fn_install();
		return;
	}
	if (st->busy) {
		if (req != st->done) {
			st->ignored += req - st->done;
			fenykl_log_info(&main_fn_log, "%u Fn presses while %s is busy: ignored",
					req - st->done, st->busy->name);
			st->done = req;
		}
		if (!st->busy->step())
			st->busy = NULL;
		return;
	}
	if (req == st->done)
		return;
	if (req - st->done > 1)
		fenykl_log_info(&main_fn_log, "%u Fn presses at once: one taken", req - st->done);
	st->done = req;
	act = main_fn_cur();
	if (!act) {
		fenykl_log_info(&main_fn_log, "Fn press after the option went to none: ignored");
		return;
	}
	if (!act->start(st->press_us))
		st->busy = act;
}

void main_fn_set_level(unsigned int level)
{
	fenykl_log_set_level(&main_fn_log, level);
	fenykl_log_set_level(&fenykl_key_log, level);
	for (unsigned int i = 0; i < MAIN_FN_ACTIONS; i++)
		if (main_fn_acts[i])
			fenykl_log_set_level(main_fn_acts[i]->log, level);
}

void main_fn_status(void)
{
	struct main_fn_state *st = &main_fn_st;

	if (!st->installed)
		return;
	fenykl_log_info(&main_fn_log, "fn %s: presses %u ignored %u, replays %u",
			main_fn_names[main_fn_action], st->done, st->ignored,
			main_fn_key.private.replays);
	for (unsigned int i = 0; i < MAIN_FN_ACTIONS; i++)
		if (main_fn_acts[i])
			main_fn_acts[i]->status();
}
