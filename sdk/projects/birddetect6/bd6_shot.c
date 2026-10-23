/*
 * birddetect6: the Fn screenshot. A short Fn press in live view or the menu saves what the panel
 * shows, the face frame on the bird included, through the shot library (sdk/docs/shot.md); a long press gives
 * the stock Fn function back at the release (long_stock). The shot runs in its own task below the
 * control task, which preempts it whenever it wakes, so detection keeps running during the ~9 s
 * write. Presses while a shot is being written are ignored and logged.
 */
#include "birddetect6.h"
#include <key.h>
#include <shot.h>

#define BD6_SHOT_TICK_MS 50
#define BD6_SHOT_PRIO 13		/* below the control task (12) */
#define BD6_SHOT_STACK 0x2000u		/* fw_malloc returns NULL above 0x3FFC */

struct bd6_shot_state {
	volatile uint32_t req;		/* short presses, written by the key handler */
	uint32_t done;			/* presses the task has taken */
	uint32_t ignored;		/* presses while busy */
	bool busy;
};

static struct bd6_shot_state bd6_shot_st;

static FENYKL_LOG_MODULE(bd6_shot_log, "fn", FENYKL_LOG_INFO);

/* key task */
static bool bd6_shot_claim(enum fenykl_key key, uint16_t gui, void *arg)
{
	(void)key;
	(void)arg;
	return FENYKL_GUI_SUPER(gui) == FENYKL_GUI_SS_REC ||
	       FENYKL_GUI_SUPER(gui) == FENYKL_GUI_SS_MENU;
}

static void bd6_shot_on_key(const struct fenykl_key_event *ev, void *arg)
{
	(void)arg;
	if (ev->kind == FENYKL_KEY_SHORT)
		bd6_shot_st.req++;
}

static struct fenykl_key_handler bd6_shot_key = {
	.name = "fn", .keys = FENYKL_KEY_BIT(FENYKL_KEY_FN), .claim = bd6_shot_claim,
	.on = bd6_shot_on_key, .long_stock = true, .enabled = true,
};

static void bd6_shot_tick(struct bd6_shot_state *st)
{
	uint32_t req = st->req;

	if (st->busy) {
		if (req != st->done) {
			st->ignored += req - st->done;
			fenykl_log_info(&bd6_shot_log, "%u Fn presses while a screenshot is written: "
					"ignored (%u in all)", req - st->done, st->ignored);
			st->done = req;
		}
		st->busy = fenykl_shot_step();
		return;
	}
	if (req == st->done)
		return;
	if (req - st->done > 1)
		fenykl_log_info(&bd6_shot_log, "%u Fn presses at once: one taken", req - st->done);
	st->done = req;
	st->busy = fenykl_shot_start();
}

FENYKL_TASK(bd6_shot);
void bd6_shot(void)
{
	struct bd6_shot_state *st = &bd6_shot_st;
	int r = fenykl_key_add(&bd6_shot_key);

	if (!r)
		r = fenykl_key_install();
	if (r) {
		fenykl_log_err(&bd6_shot_log, "key install %d: Fn stays stock, no screenshots", r);
		return;
	}
	st->done = st->req;
	fenykl_log_info(&bd6_shot_log, "key layer installed: short Fn in live view or menu = screenshot");
	for (;;) {
		fenykl_task_sleep(BD6_SHOT_TICK_MS);
		bd6_shot_tick(st);
	}
}

int bd6_shot_start(void)
{
	void *stack = fw_malloc(BD6_SHOT_STACK);

	if (!stack)
		return -1;
	return fw_OlyTaskCreate("BD6SHOT", bd6_shot_task, 2, 0, BD6_SHOT_PRIO, BD6_SHOT_STACK,
				stack);
}
