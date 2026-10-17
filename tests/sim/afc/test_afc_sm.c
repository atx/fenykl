/* The contrast client's attach/detach state machine (sdk/lib/afc_sm.c) in the GDB simulator, with
   ../hooks/sim_start.S, driven at a ~10 ms poll: boot delay, settle and cooldown, the stop on a gate,
   the frame callback's own stop, a stop pending while live view is off, and every error path ending
   passive for good. plugin_main sets sim_exit_code to 0
   (pass) or the number of the first failed check. */
#include "../../../sdk/lib/afc_int.h"

#define T_POLL 10u

uint32_t sim_exit_code = 99;

struct t_run {
	struct fenykl_afc_sm sm;
	struct fenykl_afc_in in;
	uint32_t attaches, stops;
	bool frames;		/* live view on: a frame on every poll while the client is in the driver */
};

static void t_start(struct t_run *r, uint32_t gates)
{
	*r = (struct t_run){ .in.gates = gates };
	fenykl_afc_sm_init(&r->sm, &r->in);
}

static enum fenykl_afc_action t_poll(struct t_run *r)
{
	enum fenykl_afc_action a;

	r->in.now += T_POLL;
	if (r->frames && r->sm.state >= FENYKL_AFC_ATTACHING && r->sm.state <= FENYKL_AFC_DETACHING)
		r->in.frames++;
	a = fenykl_afc_sm_step(&r->sm, &r->in);
	r->attaches += a == FENYKL_AFC_ATTACH;
	r->stops += a == FENYKL_AFC_STOP;
	return a;
}

/* Polls until action a (true) or until the time reaches until (false). */
static bool t_until(struct t_run *r, enum fenykl_afc_action a, uint32_t until)
{
	while (r->in.now < until)
		if (t_poll(r) == a)
			return true;
	return false;
}

static void t_wait(struct t_run *r, uint32_t ms)
{
	uint32_t end = r->in.now + ms;

	while (r->in.now < end)
		t_poll(r);
}

static int t_boot_and_settle(void)
{
	struct t_run r;

	t_start(&r, 0);
	if (t_until(&r, FENYKL_AFC_ATTACH, FENYKL_AFC_BOOT_MS - T_POLL))
		return 1;
	if (!t_until(&r, FENYKL_AFC_ATTACH, FENYKL_AFC_BOOT_MS + T_POLL))
		return 2;
	/* gates failing until 6 s: the attach waits for FENYKL_AFC_SETTLE_MS of clear gates */
	t_start(&r, FENYKL_AFC_G_MF);
	t_wait(&r, 6000);
	r.in.gates = 0;
	if (t_until(&r, FENYKL_AFC_ATTACH, 6000 + FENYKL_AFC_SETTLE_MS - T_POLL))
		return 3;
	if (!t_until(&r, FENYKL_AFC_ATTACH, 6000 + FENYKL_AFC_SETTLE_MS + T_POLL))
		return 4;
	/* a gate that blips during the settle restarts it */
	t_start(&r, 0);
	t_wait(&r, FENYKL_AFC_BOOT_MS - 100);
	r.in.gates = FENYKL_AFC_G_HALF;
	t_poll(&r);
	r.in.gates = 0;
	if (t_until(&r, FENYKL_AFC_ATTACH, FENYKL_AFC_BOOT_MS + FENYKL_AFC_SETTLE_MS - 100))
		return 5;
	return 0;
}

static int t_cycle(void)
{
	struct t_run r;
	uint32_t done_at;

	t_start(&r, 0);
	r.frames = true;
	if (!t_until(&r, FENYKL_AFC_ATTACH, 6000))
		return 10;
	t_wait(&r, 100);
	if (r.sm.state != FENYKL_AFC_ATTACHED)
		return 11;
	t_wait(&r, 2000);
	if (r.sm.state != FENYKL_AFC_ATTACHED || r.stops)
		return 12;
	/* half-press: stop at once, with the gate as the reason */
	r.in.gates = FENYKL_AFC_G_HALF | FENYKL_AFC_G_FCS;
	if (t_poll(&r) != FENYKL_AFC_STOP || r.sm.state != FENYKL_AFC_DETACHING || r.sm.why != (FENYKL_AFC_G_HALF | FENYKL_AFC_G_FCS))
		return 13;
	/* gates still failing: no second stop; the done callback returns it to idle */
	t_wait(&r, 50);
	if (r.stops != 1 || r.sm.state != FENYKL_AFC_DETACHING)
		return 14;
	r.in.done++;
	t_poll(&r);
	done_at = r.in.now;
	if (r.sm.state != FENYKL_AFC_IDLE || r.sm.err != FENYKL_AFC_E_NONE)
		return 15;
	/* released long before the cooldown ends: the next attach waits for it */
	r.in.gates = 0;
	if (t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS - T_POLL))
		return 16;
	if (!t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS + T_POLL) || r.attaches != 2)
		return 17;
	return 0;
}

/* The frame callback writes number = 0 on a half-press; the done may come before the task looks. */
static int t_cb_stop(void)
{
	struct t_run r;
	uint32_t done_at;

	/* stop and done in the same poll (camera run 2026-10-16): a normal detach, not early */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 1000);
	r.in.cb_stops++;
	r.in.done++;
	r.in.gates = FENYKL_AFC_G_HALF;
	if (t_poll(&r) != FENYKL_AFC_NONE || r.sm.state != FENYKL_AFC_IDLE || r.sm.err != FENYKL_AFC_E_NONE || !r.sm.by_cb ||
	    r.sm.why != FENYKL_AFC_G_HALF)
		return 30;
	done_at = r.in.now;
	/* half-press held 200 ms: the cooldown still decides the re-attach */
	t_wait(&r, 200);
	r.in.gates = 0;
	if (t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS - T_POLL))
		return 31;
	if (!t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS + T_POLL) || r.attaches != 2 || r.stops)
		return 32;
	/* stop seen first (gates still clear to the task), done later: detaching, no task stop */
	t_wait(&r, 1000);
	if (r.sm.state != FENYKL_AFC_ATTACHED)
		return 33;
	r.in.cb_stops++;
	if (t_poll(&r) != FENYKL_AFC_NONE || r.sm.state != FENYKL_AFC_DETACHING || !r.sm.by_cb || r.sm.why)
		return 34;
	r.in.cb_stops++;
	r.in.gates = FENYKL_AFC_G_HALF;
	t_wait(&r, 30);
	if (r.sm.state != FENYKL_AFC_DETACHING || r.stops)
		return 35;
	r.in.done++;
	t_poll(&r);
	done_at = r.in.now;
	if (r.sm.state != FENYKL_AFC_IDLE || r.sm.err != FENYKL_AFC_E_NONE)
		return 36;
	/* released 800 ms after the done: the settle decides */
	t_wait(&r, 800);
	r.in.gates = 0;
	if (t_until(&r, FENYKL_AFC_ATTACH, done_at + 800 + FENYKL_AFC_SETTLE_MS - T_POLL))
		return 37;
	if (!t_until(&r, FENYKL_AFC_ATTACH, done_at + 800 + FENYKL_AFC_SETTLE_MS + T_POLL) || r.attaches != 3)
		return 38;
	/* a later task stop is no longer counted as the callback's */
	t_wait(&r, 1000);
	r.in.gates = FENYKL_AFC_G_LV;
	if (t_poll(&r) != FENYKL_AFC_STOP || r.sm.by_cb)
		return 39;
	/* callback stop without a done callback: nodone, off */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 100);
	r.in.cb_stops++;
	t_poll(&r);
	t_wait(&r, FENYKL_AFC_DONE_MS - T_POLL);
	if (r.sm.state != FENYKL_AFC_DETACHING)
		return 40;
	t_wait(&r, 2 * T_POLL);
	if (r.sm.state != FENYKL_AFC_OFF || r.sm.err != FENYKL_AFC_E_NODONE)
		return 41;
	return 0;
}

/* An error stops the client and leaves the machine off after the done callback. */
static int t_off_for_good(struct t_run *r, enum fenykl_afc_err err)
{
	if (r->sm.state != FENYKL_AFC_DETACHING || r->sm.err != err)
		return 1;
	r->in.done++;
	t_poll(r);
	if (r->sm.state != FENYKL_AFC_OFF)
		return 2;
	r->in.gates = 0;
	r->frames = false;
	t_wait(r, 10000);
	return r->attaches != 1 || r->sm.state != FENYKL_AFC_OFF;
}

static int t_errors(void)
{
	struct t_run r;

	/* no frame after the attach */
	t_start(&r, 0);
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	if (t_until(&r, FENYKL_AFC_STOP, r.in.now + FENYKL_AFC_FRAME_MS - T_POLL))
		return 20;
	if (!t_until(&r, FENYKL_AFC_STOP, r.in.now + 2 * T_POLL) || t_off_for_good(&r, FENYKL_AFC_E_NOFRAME))
		return 21;
	/* no done callback after a stop */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 100);
	r.in.gates = FENYKL_AFC_G_LV;
	t_poll(&r);
	t_wait(&r, FENYKL_AFC_DONE_MS - T_POLL);
	if (r.sm.state != FENYKL_AFC_DETACHING)
		return 24;
	t_wait(&r, 2 * T_POLL);
	if (r.sm.state != FENYKL_AFC_OFF || r.sm.err != FENYKL_AFC_E_NODONE)
		return 25;
	/* done callback while attached, no stop requested by anyone: the client is gone, off for good */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 100);
	r.in.done++;
	if (t_poll(&r) != FENYKL_AFC_NONE || r.sm.state != FENYKL_AFC_OFF || r.sm.err != FENYKL_AFC_E_EARLY)
		return 26;
	r.in.gates = 0;
	t_wait(&r, 10000);
	if (r.attaches != 1 || r.sm.state != FENYKL_AFC_OFF)
		return 29;
	/* cam_request error while attaching; end of run while idle */
	t_start(&r, 0);
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	if (fenykl_afc_sm_fail(&r.sm, &r.in, FENYKL_AFC_E_REQ) != FENYKL_AFC_STOP || t_off_for_good(&r, FENYKL_AFC_E_REQ))
		return 27;
	t_start(&r, 0);
	if (fenykl_afc_sm_fail(&r.sm, &r.in, FENYKL_AFC_E_END) != FENYKL_AFC_NONE || r.sm.state != FENYKL_AFC_OFF)
		return 28;
	return 0;
}

/* Live view off (MENU): the stop waits for the frames to resume, without a timeout and without a new
   attach meanwhile ([camera]: done 6.5 s after the stop). */
static int t_lv_off(void)
{
	struct t_run r;
	uint32_t done_at;

	/* frames stop while attached, the GUI gate not (yet) failing: a stall stop, not an error */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 1000);
	r.frames = false;
	if (t_until(&r, FENYKL_AFC_STOP, r.in.now + FENYKL_AFC_FRAME_MS - 2 * T_POLL))
		return 50;
	if (!t_until(&r, FENYKL_AFC_STOP, r.in.now + 3 * T_POLL) || r.sm.why != FENYKL_AFC_W_STALL ||
	    r.sm.err != FENYKL_AFC_E_NONE)
		return 51;
	/* pending for 10 s with clear gates: no error, no attach */
	t_wait(&r, 10000);
	if (r.sm.state != FENYKL_AFC_DETACHING || r.sm.err != FENYKL_AFC_E_NONE || r.attaches != 1)
		return 52;
	/* frames resume: the driver detaches at the first one */
	r.frames = true;
	t_wait(&r, 20);
	r.in.done++;
	t_poll(&r);
	done_at = r.in.now;
	if (r.sm.state != FENYKL_AFC_IDLE || r.sm.err != FENYKL_AFC_E_NONE)
		return 53;
	if (!t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS + T_POLL) || r.attaches != 2)
		return 54;

	/* MENU: the GUI gate fires as the frames stop; back after 7 s, gates clear 300 ms later */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 1000);
	r.frames = false;
	r.in.gates = FENYKL_AFC_G_LV;
	if (t_poll(&r) != FENYKL_AFC_STOP || r.sm.why != FENYKL_AFC_G_LV)
		return 55;
	t_wait(&r, 7000);
	if (r.sm.state != FENYKL_AFC_DETACHING || r.sm.err != FENYKL_AFC_E_NONE || r.stops != 1)
		return 56;
	r.frames = true;
	t_wait(&r, 10);
	r.in.done++;
	t_poll(&r);
	done_at = r.in.now;
	if (r.sm.state != FENYKL_AFC_IDLE)
		return 57;
	t_wait(&r, 300);
	r.in.gates = 0;
	if (t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS - T_POLL))
		return 58;
	if (!t_until(&r, FENYKL_AFC_ATTACH, done_at + FENYKL_AFC_COOLDOWN_MS + T_POLL) || r.attaches != 2)
		return 59;

	/* frames resume but no done callback follows: nodone, off for good */
	t_start(&r, 0);
	r.frames = true;
	t_until(&r, FENYKL_AFC_ATTACH, 6000);
	t_wait(&r, 1000);
	r.frames = false;
	r.in.gates = FENYKL_AFC_G_LV;
	t_poll(&r);
	t_wait(&r, 3000);
	r.frames = true;
	t_poll(&r);
	t_wait(&r, FENYKL_AFC_DONE_MS - 2 * T_POLL);
	if (r.sm.state != FENYKL_AFC_DETACHING || r.sm.err != FENYKL_AFC_E_NONE)
		return 60;
	t_wait(&r, 2 * T_POLL);
	if (r.sm.state != FENYKL_AFC_OFF || r.sm.err != FENYKL_AFC_E_NODONE)
		return 61;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int (*const tests[])(void) = { t_boot_and_settle, t_cycle, t_cb_stop, t_errors, t_lv_off };

	(void)load_base;
	(void)image_size;
	for (unsigned int i = 0; i < sizeof(tests) / sizeof(tests[0]); i++) {
		int r = tests[i]();

		if (r) {
			sim_exit_code = (uint32_t)r;
			return;
		}
	}
	sim_exit_code = 0;
}
