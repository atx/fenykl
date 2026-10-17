/* The key layer (sdk/lib/key.c) over the event layer in the GDB simulator, through the patched
   appevent_post entry (fw_appevent_post) with the recording mock ./appevent.S, ../hooks/sim_start.S
   and ../hooks/support.S. The test writes the timer word (0x98900074, counts down) and the GUI
   state bytes (0x60702278) between posts. Handlers: a = Fn/OK/mode dial, claims in SS_REC,
   long_stock (main's Fn); b = observer of Fn/OK/Up/mode dial; c = OK/Up, claims when c_claim, long_us 300 ms.
   plugin_main sets sim_exit_code to the first failed section (0 = pass):
     1  add errors (no on, no keys, NONE bit, long_us too large, twice), install idempotent
     2  a non-key event passes unchanged
     3  claimed press consumed and held back; release before long_us: SHORT (held, gui at the press)
     4  unclaimed press and release: passed, PRESS/RELEASE to every handler listing the key
     5  long_stock release after long_us: press replayed with its own param/flag, then the release
     6  timer wrap: SHORT and replay across the 30-bit wrap
     7  !long_stock: LONG + RELEASE at release; first claimer wins, the others get nothing
     8  busy: a handler holding a key does not claim; the next one does, or the press passes
     9  tick, !long_stock: nothing before long_us, LONG once at long_us, the release only RELEASE
     10 tick, long_stock: the press replayed at long_us, the release passes later
     11 tick hand-off: a release posted while the tick delivers LONG comes after it, consumed
     12 mode dial: VALUE claimed (only the claimer hears it) or passed to all
     13 unknown codes and classes pass and are counted
     14 a disabled holder still completes its hold; a disabled handler neither claims nor observes
     15 fenykl_key_inject, stats */
#include <key.h>

#define TIMER_WORD (*(volatile uint32_t *)0x98900074u)
#define GUI_SM ((volatile uint8_t *)0x60702278u)
#define FN 0x000Bu
#define OK 0x0004u
#define PRESS(c) (FENYKL_EVENT_KEY_PRESS | (c))
#define RELEASE(c) (FENYKL_EVENT_KEY_RELEASE | (c))
#define LIVE 0x0203u
#define PLAY 0x0601u

struct post {
	uint32_t event, param, flag;
};

volatile uint32_t mock_args[12];	/* support.S records here */
struct post sim_posts[64];		/* ./appevent.S appends here */
uint32_t sim_nposts;
uint32_t sim_exit_code = 99;

static char trace[16];
static unsigned int ntrace;
static struct fenykl_key_event last[3];
static bool c_claim, c_handoff;
static int handoff_ret = -1;
static uint32_t nposts;		/* sim_nposts at the last post() */

static void set_now(uint32_t us)
{
	TIMER_WORD = FENYKL_LOG_TIMER_MASK - us;
}

static void set_gui(uint16_t gui)
{
	GUI_SM[0] = gui >> 8;
	GUI_SM[1] = gui & 0xFF;
}

static int traced(const char *want)
{
	unsigned int i;

	for (i = 0; want[i]; i++)
		if (i >= ntrace || trace[i] != want[i])
			return 0;
	return i == ntrace;
}

static void record(int id, const struct fenykl_key_event *ev)
{
	static const char kinds[] = "PRSLV";

	last[id] = *ev;
	if (ntrace < sizeof(trace) - 2) {
		trace[ntrace++] = 'a' + id;
		trace[ntrace++] = kinds[ev->kind];
	}
}

static bool claim_live(enum fenykl_key key, uint16_t gui, void *arg)
{
	(void)key;
	(void)arg;
	return FENYKL_GUI_SUPER(gui) == FENYKL_GUI_SS_REC;
}

static bool claim_c(enum fenykl_key key, uint16_t gui, void *arg)
{
	(void)key;
	(void)gui;
	(void)arg;
	return c_claim;
}

static void on_a(const struct fenykl_key_event *ev, void *arg)
{
	(void)arg;
	record(0, ev);
}

static void on_b(const struct fenykl_key_event *ev, void *arg)
{
	(void)arg;
	record(1, ev);
}

static void on_c(const struct fenykl_key_event *ev, void *arg)
{
	(void)arg;
	record(2, ev);
	/* the key task preempting the plugin task inside the tick */
	if (c_handoff && ev->kind == FENYKL_KEY_LONG) {
		c_handoff = false;
		handoff_ret = fw_appevent_post(RELEASE(OK), 0, 0);
	}
}

static struct fenykl_key_handler ha = {
	.name = "a",
	.keys = FENYKL_KEY_BIT(FENYKL_KEY_FN) | FENYKL_KEY_BIT(FENYKL_KEY_OK) |
		FENYKL_KEY_BIT(FENYKL_KEY_MODE_DIAL),
	.claim = claim_live, .on = on_a, .long_stock = true, .enabled = true,
};
static struct fenykl_key_handler hb = {
	.name = "b",
	.keys = FENYKL_KEY_BIT(FENYKL_KEY_FN) | FENYKL_KEY_BIT(FENYKL_KEY_OK) |
		FENYKL_KEY_BIT(FENYKL_KEY_UP) | FENYKL_KEY_BIT(FENYKL_KEY_MODE_DIAL),
	.on = on_b, .enabled = true,
};
static struct fenykl_key_handler hc = {
	.name = "c", .keys = FENYKL_KEY_BIT(FENYKL_KEY_OK) | FENYKL_KEY_BIT(FENYKL_KEY_UP),
	.claim = claim_c, .on = on_c, .long_us = 300000, .enabled = true,
};
static struct fenykl_key_handler bad_on = { .name = "x", .keys = 2 };
static struct fenykl_key_handler bad_keys = { .name = "x", .on = on_a };
static struct fenykl_key_handler bad_none = { .name = "x", .keys = 1, .on = on_a };
static struct fenykl_key_handler bad_long = {
	.name = "x", .keys = 2, .on = on_a, .long_us = FENYKL_KEY_LONG_MAX,
};

/* Posts through the patched entry at time now; 1 = reached the firmware, 0 = consumed. */
static int post(uint32_t now, uint32_t event, uint32_t param, uint32_t flag)
{
	set_now(now);
	ntrace = 0;
	nposts = sim_nposts;
	return fw_appevent_post(event, param, flag);
}

static void tick(uint32_t now)
{
	set_now(now);
	ntrace = 0;
	nposts = sim_nposts;
	fenykl_key_tick();
}

/* The firmware got exactly these posts since the last post()/tick(). */
static int posted(const struct post *want, unsigned int n)
{
	if (sim_nposts - nposts != n)
		return 0;
	for (unsigned int i = 0; i < n; i++) {
		const struct post *p = &sim_posts[nposts + i];

		if (p->event != want[i].event || p->param != want[i].param ||
		    p->flag != want[i].flag)
			return 0;
	}
	return 1;
}

static int got(int id, enum fenykl_key key, uint32_t held_us, uint16_t gui, bool from_tick)
{
	const struct fenykl_key_event *e = &last[id];

	return e->key == key && e->held_us == held_us && e->gui == gui && e->from_tick == from_tick;
}

static int run(void)
{
	struct fenykl_key_stats st;
	struct fenykl_event_stats est;
	uint32_t t;

	if (fenykl_key_add(&bad_on) != FENYKL_KEY_EARG || fenykl_key_add(&bad_keys) != FENYKL_KEY_EARG ||
	    fenykl_key_add(&bad_none) != FENYKL_KEY_EARG ||
	    fenykl_key_add(&bad_long) != FENYKL_KEY_EARG || fenykl_key_add(&ha) ||
	    fenykl_key_add(&hb) || fenykl_key_install() || fenykl_key_add(&hc) ||
	    fenykl_key_add(&ha) != FENYKL_KEY_EBUSY || fenykl_key_install() ||
	    *(volatile uint8_t *)FENYKL_UNCACHED(fenykl_hook_appevent_post.entry) != 0xdc)
		return 1;

	if (post(0, 0x04060053, 7, 1) != 1 || !posted(&(struct post){ 0x04060053, 7, 1 }, 1) ||
	    !traced(""))
		return 2;

	set_gui(LIVE);
	if (post(1000, PRESS(FN), 5, 9) != 0 || !posted(NULL, 0) || !traced(""))
		return 3;
	set_gui(PLAY);
	if (post(1000 + 799999, RELEASE(FN), 0, 0) != 0 || !posted(NULL, 0) || !traced("aS") ||
	    !got(0, FENYKL_KEY_FN, 799999, LIVE, false) || last[0].now_us != 1000 + 799999 ||
	    ha.private.shorts != 1)
		return 3;

	if (post(2000000, PRESS(FN), 0, 0) != 1 || !posted(&(struct post){ PRESS(FN), 0, 0 }, 1) ||
	    !traced("aPbP") || !got(1, FENYKL_KEY_FN, 0, PLAY, false))
		return 4;
	if (post(2000100, RELEASE(FN), 0, 0) != 1 || !traced("aRbR") ||
	    !got(1, FENYKL_KEY_FN, 100, PLAY, false))
		return 4;

	set_gui(LIVE);
	if (post(3000000, PRESS(FN), 5, 9) != 0)
		return 5;
	if (post(3800000, RELEASE(FN), 0, 1) != 1 ||
	    !posted((struct post[]){ { PRESS(FN), 5, 9 }, { RELEASE(FN), 0, 1 } }, 2) ||
	    !traced("aRbR") || !got(0, FENYKL_KEY_FN, 800000, LIVE, false) ||
	    ha.private.replays != 1)
		return 5;

	if (post(0x3FFFFF00, PRESS(FN), 0, 0) != 0 || post(0x100, RELEASE(FN), 0, 0) != 0 ||
	    !traced("aS") || last[0].held_us != 0x200)
		return 6;
	if (post(0x3FFF0000, PRESS(FN), 0, 0) != 0 ||
	    post((0x3FFF0000 + 800000) & FENYKL_LOG_TIMER_MASK, RELEASE(FN), 0, 0) != 1 ||
	    !posted((struct post[]){ { PRESS(FN), 0, 0 }, { RELEASE(FN), 0, 0 } }, 2) ||
	    last[0].held_us != 800000)
		return 6;

	set_gui(PLAY);
	c_claim = true;
	if (post(4000000, PRESS(OK), 0, 0) != 0 || !traced("") || hc.private.held != FENYKL_KEY_OK)
		return 7;
	if (post(4299999, RELEASE(OK), 0, 0) != 0 || !traced("cS"))
		return 7;
	if (post(5000000, PRESS(OK), 0, 0) != 0 || post(5300000, RELEASE(OK), 0, 0) != 0 ||
	    !traced("cLcR") || !got(2, FENYKL_KEY_OK, 300000, PLAY, false) || hc.private.longs != 1)
		return 7;
	set_gui(LIVE);
	if (post(5400000, PRESS(OK), 0, 0) != 0 || ha.private.held != FENYKL_KEY_OK ||
	    post(5400010, RELEASE(OK), 0, 0) != 0 || !traced("aS"))
		return 7;

	if (post(6000000, PRESS(FN), 0, 0) != 0 || post(6000001, PRESS(OK), 0, 0) != 0 ||
	    ha.private.busy != 1 || hc.private.held != FENYKL_KEY_OK ||
	    post(6000002, RELEASE(OK), 0, 0) != 0 || !traced("cS"))
		return 8;
	c_claim = false;
	if (post(6000003, PRESS(OK), 0, 0) != 1 || ha.private.busy != 2 || !traced("aPbPcP") ||
	    post(6000004, RELEASE(FN), 0, 0) != 0 || !traced("aS") ||
	    post(6000005, RELEASE(OK), 0, 0) != 1 || !traced("aRbRcR") ||
	    !got(2, FENYKL_KEY_OK, 2, LIVE, false))
		return 8;

	set_gui(PLAY);
	c_claim = true;
	t = 7000000;
	if (post(t, PRESS(OK), 0, 0) != 0)
		return 9;
	tick(t + 299999);
	if (!traced("") || hc.private.longs != 1)
		return 9;
	tick(t + 300000);
	if (!traced("cL") || !got(2, FENYKL_KEY_OK, 300000, PLAY, true) || hc.private.longs != 2)
		return 9;
	tick(t + 350000);
	if (!traced(""))
		return 9;
	if (post(t + 400000, RELEASE(OK), 0, 0) != 0 || !traced("cR") ||
	    !got(2, FENYKL_KEY_OK, 400000, PLAY, false))
		return 9;

	set_gui(LIVE);
	t = 8000000;
	if (post(t, PRESS(FN), 5, 9) != 0)
		return 10;
	tick(t + 800000);
	if (!traced("") || !posted(&(struct post){ PRESS(FN), 5, 9 }, 1) ||
	    ha.private.state != 0 || ha.private.replays != 3)
		return 10;
	if (post(t + 900000, RELEASE(FN), 0, 0) != 1 ||
	    !posted(&(struct post){ RELEASE(FN), 0, 0 }, 1) || !traced("aRbR") ||
	    last[0].held_us != 900000)
		return 10;

	set_gui(PLAY);
	t = 9000000;
	if (post(t, PRESS(OK), 0, 0) != 0)
		return 11;
	c_handoff = true;
	tick(t + 300000);
	if (handoff_ret != 0 || !traced("cLcR") || !got(2, FENYKL_KEY_OK, 300000, PLAY, true) ||
	    !posted(NULL, 0) || post(t + 400000, PRESS(OK), 0, 0) != 0 ||
	    post(t + 400001, RELEASE(OK), 0, 0) != 0 || !traced("cS"))
		return 11;

	set_gui(LIVE);
	if (post(0, FENYKL_EVENT_KEY_VALUE | 0x1901, 0, 0) != 0 || !traced("aV"))
		return 12;
	set_gui(PLAY);
	if (post(0, FENYKL_EVENT_KEY_VALUE | 0x1901, 0, 0) != 1 || !traced("aVbV"))
		return 12;

	if (post(0, PRESS(0x1234), 0, 0) != 1 || !traced("") ||
	    post(0, 0x02031902, 0, 0) != 1 || !traced("") ||
	    post(0, 0x02040004, 3, 0) != 1 || !posted(&(struct post){ 0x02040004, 3, 0 }, 1))
		return 13;
	fenykl_key_get_stats(&st);
	if (st.unknown != 3 || st.last_unknown != 0x02040004)
		return 13;

	set_gui(LIVE);
	if (post(10000000, PRESS(FN), 0, 0) != 0)
		return 14;
	ha.enabled = false;
	if (post(10000001, RELEASE(FN), 0, 0) != 0 || !traced("aS") ||
	    post(10000002, PRESS(FN), 0, 0) != 1 || !traced("bP") ||
	    post(10000003, RELEASE(FN), 0, 0) != 1 || !traced("bR"))
		return 14;

	nposts = sim_nposts;
	if (fenykl_key_inject(FENYKL_KEY_FN) ||
	    !posted((struct post[]){ { PRESS(FN), 0, 0 }, { RELEASE(FN), 0, 0 } }, 2) ||
	    fenykl_key_inject(FENYKL_KEY_MODE_DIAL) != FENYKL_KEY_EARG ||
	    fenykl_key_inject(FENYKL_KEY_NONE) != FENYKL_KEY_EARG)
		return 15;
	fenykl_key_get_stats(&st);
	fenykl_event_get_stats(&est);
	if (st.presses != 17 || st.releases != 17 || st.values != 2 || st.claimed != 15 ||
	    st.ticks_long != 3 || est.injected != 5 || est.nested != 0)
		return 15;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = run();
}
