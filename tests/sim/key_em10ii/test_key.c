/* The key layer (sdk/lib/key.c) with the E-M10 II key map (key_em10ii.c) over the event layer and
   the E-M10 II hook runtime in the GDB simulator, posting through the patched appevent_post entry
   onto the recording mock ./fw.S, with ../hooks_em10ii/sim_start.S. The test writes the timer word
   (0x98900074, counts down) and the GUI super/state bytes (0x619A3740) between posts. Handlers:
   f claims Fn1 in live view and the menu; o observes Fn2, Fn3, REC, L-Fn and RIGHT.
   plugin_main sets sim_exit_code to the first failed section (0 = pass):
     1  add refuses a handler listing POWER (no key on this body); add and install succeed
     2  Fn1 0x000B in live view (0x0108) and the menu (0x1102): consumed, SHORT as FN; in playback
        (0x0601) passed, RELEASE as FN
     3  Fn2 0x1209, Fn3 0x1224, REC 0x120A, L-Fn 0x2202, RIGHT 0x0008: passed, PRESS/RELEASE of
        FN2, FN3, REC, LFN, RIGHT
     4  0x000C (the E-PL3's POWER), the power lever's value 0x0202000C and code 0: passed, unknown
     5  inject refuses POWER and posts Fn2's codes */
#include <key.h>

#define TIMER_WORD (*(volatile uint32_t *)0x98900074u)
#define GUI_SM ((volatile uint8_t *)0x619A3740u)
#define PRESS(c) (FENYKL_EVENT_KEY_PRESS | (c))
#define RELEASE(c) (FENYKL_EVENT_KEY_RELEASE | (c))
#define LIVE 0x0108u
#define MENU 0x1102u
#define PLAY 0x0601u

struct post {
	uint32_t event, param, flag;
};

struct post sim_posts[64];		/* ./fw.S appends here */
uint32_t sim_nposts;
uint32_t sim_exit_code = 99;

static struct fenykl_key_event last;
static unsigned int ndelivered;
static uint32_t nposts;		/* sim_nposts at the last post() */

static void set_gui(uint16_t gui)
{
	GUI_SM[0] = gui >> 8;
	GUI_SM[1] = gui & 0xFF;
}

static bool claim_f(enum fenykl_key key, uint16_t gui, void *arg)
{
	(void)key;
	(void)gui;
	(void)arg;
	return fenykl_gui_live_view() || fenykl_gui_menu();
}

static void on_key(const struct fenykl_key_event *ev, void *arg)
{
	(void)arg;
	last = *ev;
	ndelivered++;
}

static struct fenykl_key_handler hf = {
	.name = "f", .keys = FENYKL_KEY_BIT(FENYKL_KEY_FN), .claim = claim_f, .on = on_key,
	.long_stock = true, .enabled = true,
};
static struct fenykl_key_handler ho = {
	.name = "o",
	.keys = FENYKL_KEY_BIT(FENYKL_KEY_FN2) | FENYKL_KEY_BIT(FENYKL_KEY_FN3) |
		FENYKL_KEY_BIT(FENYKL_KEY_REC) | FENYKL_KEY_BIT(FENYKL_KEY_LFN) |
		FENYKL_KEY_BIT(FENYKL_KEY_RIGHT),
	.on = on_key, .enabled = true,
};
static struct fenykl_key_handler bad_power = {
	.name = "x", .keys = FENYKL_KEY_BIT(FENYKL_KEY_FN) | FENYKL_KEY_BIT(FENYKL_KEY_POWER),
	.on = on_key,
};

/* Posts through the patched entry at time now; 1 = reached the firmware, 0 = consumed. */
static int post(uint32_t now, uint32_t event)
{
	TIMER_WORD = FENYKL_TIMER_MASK - now;
	ndelivered = 0;
	nposts = sim_nposts;
	return fw_appevent_post(event, 0, 0);
}

/* The firmware got exactly event (0 = nothing) since the last post(). */
static int posted(uint32_t event)
{
	if (!event)
		return sim_nposts == nposts;
	return sim_nposts == nposts + 1 && sim_posts[nposts].event == event;
}

static int got(enum fenykl_key key, enum fenykl_key_kind kind)
{
	return ndelivered == 1 && last.key == key && last.kind == kind;
}

/* A short Fn1 press in gui: claimed (consumed, SHORT) or passed. */
static int fn1_short(uint16_t gui, bool claimed)
{
	set_gui(gui);
	if (post(1000, PRESS(0x000B)) != !claimed || !posted(claimed ? 0 : PRESS(0x000B)))
		return 0;
	if (post(2000, RELEASE(0x000B)) != !claimed || !posted(claimed ? 0 : RELEASE(0x000B)))
		return 0;
	if (!claimed)
		return got(FENYKL_KEY_FN, FENYKL_KEY_RELEASE);
	return got(FENYKL_KEY_FN, FENYKL_KEY_SHORT) && last.gui == gui;
}

/* code's press and release pass and reach the observer as key. */
static int observed(uint32_t code, enum fenykl_key key)
{
	return post(0, PRESS(code)) == 1 && posted(PRESS(code)) && got(key, FENYKL_KEY_PRESS) &&
	       post(0, RELEASE(code)) == 1 && posted(RELEASE(code)) &&
	       got(key, FENYKL_KEY_RELEASE);
}

static int run(void)
{
	struct fenykl_key_stats st;

	if (fenykl_key_add(&bad_power) != FENYKL_KEY_EARG || fenykl_key_add(&hf) ||
	    fenykl_key_add(&ho) || fenykl_key_install() || !fenykl_hook_appevent_post.installed)
		return 1;

	if (!fn1_short(LIVE, true) || !fn1_short(MENU, true) || !fn1_short(PLAY, false))
		return 2;

	if (!observed(0x1209, FENYKL_KEY_FN2) || !observed(0x1224, FENYKL_KEY_FN3) ||
	    !observed(0x120A, FENYKL_KEY_REC) || !observed(0x2202, FENYKL_KEY_LFN) ||
	    !observed(0x0008, FENYKL_KEY_RIGHT))
		return 3;

	if (post(0, PRESS(0x000C)) != 1 || ndelivered ||
	    post(0, FENYKL_EVENT_KEY_VALUE | 0x000C) != 1 || ndelivered ||
	    post(0, PRESS(0)) != 1 || ndelivered)
		return 4;
	fenykl_key_get_stats(&st);
	if (st.unknown != 3 || st.last_unknown != PRESS(0) || st.claimed != 2)
		return 4;

	nposts = sim_nposts;
	if (fenykl_key_inject(FENYKL_KEY_POWER) != FENYKL_KEY_EARG || !posted(0))
		return 5;
	if (fenykl_key_inject(FENYKL_KEY_FN2) || sim_nposts != nposts + 2 ||
	    sim_posts[nposts].event != PRESS(0x1209) || sim_posts[nposts + 1].event != RELEASE(0x1209))
		return 5;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = run();
}
