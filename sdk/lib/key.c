/*
 * Key layer (sdk/docs/key.md): one event handler at FENYKL_KEY_EVENT_PRIO turns the key-class
 * APPEVENTs into named keys. A claimed press is consumed and held; its release completes it as SHORT
 * or LONG, or replays the press to the firmware (long_stock). fenykl_key_tick decides LONG while the
 * key is still down. Hook and tick change a handler's state under the CPU0 interrupt mask; a release
 * that arrives while the tick acts on the hold is handed to the tick, so the order stays press,
 * release. Everything hangs off fenykl_key_add/install: a plugin that never calls them links none of
 * it.
 */
#include <key.h>

#define GUI_SUPER 0x60702278u	/* u8 gui_sm super state, u8 state at +1 */
#define MODE_DIAL_VALUE (FENYKL_EVENT_KEY_VALUE | 0x1901u)
#define KEY_ALL ((FENYKL_KEY_BIT(FENYKL_KEY_COUNT) - 1) & ~FENYKL_KEY_BIT(FENYKL_KEY_NONE))

enum {
	KS_IDLE,
	KS_HELD,	/* claimed press held back */
	KS_LONG,	/* the tick delivered LONG; the release is consumed */
	KS_TICK,	/* the tick is delivering LONG or replaying the press */
	KS_TICK_REL,	/* ... and the release came meanwhile: the tick completes it */
};

FENYKL_LOG_MODULE(fenykl_key_log, "key", FENYKL_LOG_INFO);

/* "camera" = seen through the hook on the camera (2026-10-17) */
static const uint16_t key_codes[FENYKL_KEY_COUNT] = {
	[FENYKL_KEY_OK] = 0x0004,	/* camera */
	[FENYKL_KEY_UP] = 0x0005,	/* camera */
	[FENYKL_KEY_DOWN] = 0x0006,	/* camera */
	[FENYKL_KEY_LEFT] = 0x0007,	/* camera */
	[FENYKL_KEY_RIGHT] = 0x0008,	/* camera */
	[FENYKL_KEY_INFO] = 0x1205,	/* camera */
	[FENYKL_KEY_MENU] = 0x0089,	/* camera */
	[FENYKL_KEY_PLAY] = 0x0087,	/* camera */
	[FENYKL_KEY_DEL] = 0x0084,	/* camera */
	[FENYKL_KEY_FN] = 0x000B,	/* camera (also 2026-10-14) */
	[FENYKL_KEY_REC] = 0x1209,	/* camera: R started and stopped recording */
	[FENYKL_KEY_HALF] = 0x0100,	/* camera; a shot: HALF, FULL press, FULL, HALF release */
	[FENYKL_KEY_FULL] = 0x0101,	/* camera */
	[FENYKL_KEY_POWER] = 0x000C,	/* not exercised */
	[FENYKL_KEY_MODE_DIAL] = 0x1901,	/* camera: one VALUE per step, poll agrees */
};

static enum fenykl_event_action key_on_event(struct fenykl_event *ev, void *arg);

static struct {
	struct fenykl_key_handler *head;
	struct fenykl_event_handler ev;
	bool added;
	uint32_t press_us[FENYKL_KEY_COUNT];	/* last press seen, for unclaimed releases */
	volatile struct fenykl_key_stats st;
} keyl = {
	.ev = { .name = "key", .fn = key_on_event, .prio = FENYKL_KEY_EVENT_PRIO, .enabled = true },
};

static uint32_t irq_save(void)
{
	uint32_t psw = fw_psw_get();

	fw_irq_disable_all();
	return psw;
}

static bool key_is_button(enum fenykl_key k)
{
	return k > FENYKL_KEY_NONE && k < FENYKL_KEY_MODE_DIAL;
}

static uint32_t key_long_us(const struct fenykl_key_handler *h)
{
	return h->long_us ? h->long_us : FENYKL_KEY_LONG_US;
}

static enum fenykl_key key_lookup(uint32_t code)
{
	for (unsigned int k = FENYKL_KEY_NONE + 1; k < FENYKL_KEY_COUNT; k++)
		if (key_codes[k] == code)
			return k;
	return FENYKL_KEY_NONE;
}

/* Every enabled handler listing the key. */
static void key_observe(const struct fenykl_key_event *e)
{
	for (struct fenykl_key_handler *h = keyl.head; h; h = h->private.next)
		if (h->enabled && (h->keys & FENYKL_KEY_BIT(e->key)))
			h->on(e, h->arg);
}

/* The first enabled handler listing the key whose claim() says yes. A handler holding a key does
   not claim another press. */
static struct fenykl_key_handler *key_claimer(const struct fenykl_key_event *e, bool press)
{
	for (struct fenykl_key_handler *h = keyl.head; h; h = h->private.next) {
		if (!h->enabled || !h->claim || !(h->keys & FENYKL_KEY_BIT(e->key)))
			continue;
		if (press && h->private.state != KS_IDLE) {
			h->private.busy++;
			if (h->private.held == e->key)
				fenykl_log_warn(&fenykl_key_log, "%s: key %u pressed again, release lost",
						h->name, e->key);
			else
				fenykl_log_dbg(&fenykl_key_log, "%s: key %u busy, %u held", h->name,
						 e->key, h->private.held);
			continue;
		}
		if (h->claim(e->key, e->gui, h->arg))
			return h;
	}
	return NULL;
}

static enum fenykl_event_action key_press(struct fenykl_key_event *e, const struct fenykl_event *ev)
{
	struct fenykl_key_handler *h;

	keyl.st.presses++;
	keyl.press_us[e->key] = e->now_us;
	h = key_claimer(e, true);
	if (!h) {
		e->kind = FENYKL_KEY_PRESS;
		key_observe(e);
		return FENYKL_EVENT_PASS;
	}
	h->private.held = e->key;
	h->private.press_us = e->now_us;
	h->private.param = ev->param;
	h->private.flag = ev->flag;
	h->private.gui = e->gui;
	__asm__ volatile("" ::: "memory");	/* the tick sees the hold complete */
	h->private.state = KS_HELD;
	keyl.st.claimed++;
	fenykl_log_dbg(&fenykl_key_log, "%s: claim %08x gui %04x", h->name, ev->event, e->gui);
	return FENYKL_EVENT_CONSUME;
}

/* Completes the hold of h at its release; true = the release is consumed. */
static bool key_complete(struct fenykl_key_handler *h, struct fenykl_key_event *e,
			 const struct fenykl_event *ev)
{
	uint32_t psw = irq_save();
	uint8_t state = h->private.state;

	if (state == KS_TICK) {
		h->private.rel_param = ev->param;
		h->private.rel_flag = ev->flag;
		h->private.rel_us = e->now_us;
		h->private.state = KS_TICK_REL;
		fw_irq_mask_restore(psw);
		return true;
	}
	h->private.state = KS_IDLE;
	fw_irq_mask_restore(psw);
	e->held_us = FENYKL_LOG_ELAPSED(h->private.press_us, e->now_us);
	e->gui = h->private.gui;
	if (state == KS_LONG) {
		e->kind = FENYKL_KEY_RELEASE;
		h->on(e, h->arg);
		return true;
	}
	if (e->held_us < key_long_us(h)) {
		h->private.shorts++;
		fenykl_log_dbg(&fenykl_key_log, "%s: short %08x held %u us", h->name, ev->event,
				 e->held_us);
		e->kind = FENYKL_KEY_SHORT;
		h->on(e, h->arg);
		return true;
	}
	if (h->long_stock) {
		h->private.replays++;
		fenykl_log_dbg(&fenykl_key_log, "%s: replay %08x held %u us", h->name, ev->event,
				 e->held_us);
		fenykl_event_inject(FENYKL_EVENT_KEY_PRESS | key_codes[e->key], h->private.param,
				    h->private.flag);
		return false;
	}
	h->private.longs++;
	fenykl_log_dbg(&fenykl_key_log, "%s: long %08x held %u us", h->name, ev->event,
			 e->held_us);
	e->kind = FENYKL_KEY_LONG;
	h->on(e, h->arg);
	e->kind = FENYKL_KEY_RELEASE;
	h->on(e, h->arg);
	return true;
}

static enum fenykl_event_action key_release(struct fenykl_key_event *e,
					    const struct fenykl_event *ev)
{
	struct fenykl_key_handler *h;

	keyl.st.releases++;
	for (h = keyl.head; h; h = h->private.next)
		if (h->private.state != KS_IDLE && h->private.held == e->key)
			break;
	if (h && key_complete(h, e, ev))
		return FENYKL_EVENT_CONSUME;
	e->held_us = FENYKL_LOG_ELAPSED(keyl.press_us[e->key], e->now_us);
	e->kind = FENYKL_KEY_RELEASE;
	key_observe(e);
	return FENYKL_EVENT_PASS;
}

static enum fenykl_event_action key_on_event(struct fenykl_event *ev, void *arg)
{
	uint32_t class = ev->event & FENYKL_EVENT_CLASS_MASK;
	struct fenykl_key_event e = { 0 };
	struct fenykl_key_handler *h;

	(void)arg;
	if (!FENYKL_EVENT_IS_KEY(ev->event))
		return FENYKL_EVENT_PASS;
	e.key = key_lookup(ev->event & FENYKL_EVENT_CODE_MASK);
	if (e.key == FENYKL_KEY_NONE || class > FENYKL_EVENT_KEY_VALUE) {
		keyl.st.unknown++;
		keyl.st.last_unknown = ev->event;
		return FENYKL_EVENT_PASS;
	}
	e.now_us = FENYKL_LOG_TIMER_NOW();
	e.gui = fenykl_gui_state();
	if (class == FENYKL_EVENT_KEY_PRESS)
		return key_press(&e, ev);
	if (class == FENYKL_EVENT_KEY_RELEASE)
		return key_release(&e, ev);
	keyl.st.values++;
	e.kind = FENYKL_KEY_VALUE;
	h = key_claimer(&e, false);
	if (!h) {
		key_observe(&e);
		return FENYKL_EVENT_PASS;
	}
	keyl.st.claimed++;
	h->on(&e, h->arg);
	return FENYKL_EVENT_CONSUME;
}

static void key_tick_one(struct fenykl_key_handler *h, uint32_t now)
{
	struct fenykl_key_event e = { .now_us = now, .from_tick = true };
	uint32_t psw = irq_save();
	uint8_t state = h->private.state;

	e.key = h->private.held;
	e.gui = h->private.gui;
	e.held_us = FENYKL_LOG_ELAPSED(h->private.press_us, now);
	if (state != KS_HELD || e.held_us < key_long_us(h)) {
		fw_irq_mask_restore(psw);
		return;
	}
	h->private.state = KS_TICK;
	fw_irq_mask_restore(psw);

	keyl.st.ticks_long++;
	if (h->long_stock) {
		h->private.replays++;
		fenykl_log_dbg(&fenykl_key_log, "%s: replay key %u held %u us (tick)", h->name,
				 e.key, e.held_us);
		fenykl_event_inject(FENYKL_EVENT_KEY_PRESS | key_codes[e.key], h->private.param,
				    h->private.flag);
	} else {
		h->private.longs++;
		fenykl_log_dbg(&fenykl_key_log, "%s: long key %u held %u us (tick)", h->name, e.key,
				 e.held_us);
		e.kind = FENYKL_KEY_LONG;
		h->on(&e, h->arg);
	}

	psw = irq_save();
	state = h->private.state;
	h->private.state = state == KS_TICK && !h->long_stock ? KS_LONG : KS_IDLE;
	fw_irq_mask_restore(psw);
	if (state != KS_TICK_REL)
		return;
	e.held_us = FENYKL_LOG_ELAPSED(h->private.press_us, h->private.rel_us);
	e.kind = FENYKL_KEY_RELEASE;
	if (!h->long_stock) {
		h->on(&e, h->arg);
		return;
	}
	fenykl_event_inject(FENYKL_EVENT_KEY_RELEASE | key_codes[e.key], h->private.rel_param,
			    h->private.rel_flag);
	key_observe(&e);
}

void fenykl_key_tick(void)
{
	uint32_t now = FENYKL_LOG_TIMER_NOW();

	for (struct fenykl_key_handler *h = keyl.head; h; h = h->private.next)
		key_tick_one(h, now);
}

int fenykl_key_add(struct fenykl_key_handler *h)
{
	struct fenykl_key_handler **pp;
	uint32_t psw;

	if (!h->on || !h->keys || (h->keys & ~KEY_ALL) || h->long_us >= FENYKL_KEY_LONG_MAX) {
		fenykl_log_err(&fenykl_key_log, "add %s: on %p keys %x long_us %u", h->name, h->on,
			       h->keys, h->long_us);
		return FENYKL_KEY_EARG;
	}
	psw = irq_save();
	for (pp = &keyl.head; *pp && *pp != h; pp = &(*pp)->private.next)
		;
	if (*pp) {
		fw_irq_mask_restore(psw);
		fenykl_log_err(&fenykl_key_log, "add %s: already registered", h->name);
		return FENYKL_KEY_EBUSY;
	}
	h->private.next = NULL;
	h->private.state = KS_IDLE;
	h->private.held = FENYKL_KEY_NONE;
	h->private.shorts = 0;
	h->private.longs = 0;
	h->private.replays = 0;
	h->private.busy = 0;
	__asm__ volatile("" ::: "memory");	/* a preempting hook sees the node complete */
	*pp = h;
	fw_irq_mask_restore(psw);
	return 0;
}

int fenykl_key_install(void)
{
	int r;

	if (!keyl.added) {
		r = fenykl_event_add(&keyl.ev);
		if (r)
			return r;
		keyl.added = true;
	}
	return fenykl_event_install();
}

void fenykl_key_get_stats(struct fenykl_key_stats *st)
{
	*st = keyl.st;
}

int fenykl_key_inject(enum fenykl_key k)
{
	if (!key_is_button(k)) {
		fenykl_log_err(&fenykl_key_log, "inject: key %u is not a button", k);
		return FENYKL_KEY_EARG;
	}
	fenykl_event_inject(FENYKL_EVENT_KEY_PRESS | key_codes[k], 0, 0);
	fenykl_event_inject(FENYKL_EVENT_KEY_RELEASE | key_codes[k], 0, 0);
	return 0;
}

bool fenykl_key_pressed(enum fenykl_key k)
{
	return key_is_button(k) && fw_ui_key_get_state(key_codes[k]) == 1;
}

unsigned int fenykl_key_mode_dial(void)
{
	return fw_ui_key_get_state(MODE_DIAL_VALUE);
}

uint16_t fenykl_gui_state(void)
{
	const volatile uint8_t *sm = (const volatile uint8_t *)GUI_SUPER;

	return sm[0] << 8 | sm[1];
}
