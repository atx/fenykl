/*
 * Key layer (sdk/docs/key.md): one event handler (event.h, prio FENYKL_KEY_EVENT_PRIO) that turns
 * the key-class APPEVENTs into named keys for key handlers. A handler claims a press (held back,
 * completed as SHORT or LONG, or replayed to the firmware) or observes it. Linked into every plugin;
 * zero bytes unless fenykl_key_add/install is called.
 */
#ifndef _FENYKL_KEY_H
#define _FENYKL_KEY_H

#include <stdbool.h>
#include <fenykl.h>
#include "body.h"
#include "event.h"
#include "log.h"

/* Named keys; key.c maps them to the firmware key codes. All but POWER are camera-seen
   through the hook. No rear dial: it posts no APPEVENT on the camera (key.md 5). */
enum fenykl_key {
	FENYKL_KEY_NONE,
	FENYKL_KEY_OK,
	FENYKL_KEY_UP,
	FENYKL_KEY_DOWN,
	FENYKL_KEY_LEFT,
	FENYKL_KEY_RIGHT,
	FENYKL_KEY_INFO,
	FENYKL_KEY_MENU,
	FENYKL_KEY_PLAY,
	FENYKL_KEY_DEL,
	FENYKL_KEY_FN,
	FENYKL_KEY_REC,		/* R (movie) */
	FENYKL_KEY_HALF,	/* shutter half press (Trg1) */
	FENYKL_KEY_FULL,	/* shutter full press (Trg2) */
	FENYKL_KEY_POWER,	/* not exercised on the camera */
	FENYKL_KEY_MODE_DIAL,	/* VALUE only */
	FENYKL_KEY_COUNT,
};
#define FENYKL_KEY_BIT(k) (1u << (k))

enum fenykl_key_kind {
	FENYKL_KEY_PRESS,	/* unclaimed press, passed to the firmware */
	FENYKL_KEY_RELEASE,	/* release of an unclaimed (or replayed) press, passed; after a LONG */
	FENYKL_KEY_SHORT,	/* claimed press released before long_us */
	FENYKL_KEY_LONG,	/* claimed press held long_us (not with long_stock) */
	FENYKL_KEY_VALUE,	/* mode dial moved */
};

struct fenykl_key_event {
	enum fenykl_key key;
	enum fenykl_key_kind kind;
	uint32_t held_us;	/* RELEASE/SHORT/LONG: time since the press */
	uint32_t now_us;	/* FENYKL_TIMER_NOW() */
	uint16_t gui;		/* fenykl_gui_state(), at the press for SHORT/LONG/RELEASE */
	bool from_tick;		/* delivered by fenykl_key_tick (plugin task), not the hook */
};

struct fenykl_key_handler {
	const char *name;	/* for the log */
	uint32_t keys;		/* FENYKL_KEY_BIT mask it wants */
	/* At a press (or mode dial value) of a key in `keys`: true = claim it. Hook context, cheap (a
	   GUI state test). NULL = never claim. A claim consumes the events only: stock hold behaviour
	   (playback scroll, menu repeat) follows the physical key and still runs (key.md 3.1). */
	bool (*claim)(enum fenykl_key key, uint16_t gui, void *arg);
	void (*on)(const struct fenykl_key_event *ev, void *arg);	/* handler rules (key.md 4) */
	void *arg;
	uint32_t long_us;	/* 0 = FENYKL_KEY_LONG_US; < FENYKL_KEY_LONG_MAX */
	/* A long hold replays the press to the firmware instead of LONG. The firmware then sees ONE
	   late press, not the hold: right for a key whose stock function is one action (Fn), wrong
	   for arrows in menus and playback (key.md 3.1). */
	bool long_stock;
	volatile bool enabled;	/* false: no new claims or deliveries; a held key still completes */
	struct {
		struct fenykl_key_handler *next;
		volatile uint8_t state;		/* key.c */
		enum fenykl_key held;
		uint32_t press_us, param, flag;	/* the held press */
		uint16_t gui;
		uint32_t rel_param, rel_flag, rel_us;	/* a release handed to the tick */
		uint32_t shorts, longs, replays;
		uint32_t busy;	/* presses of another key while one was held */
	} private;		/* SDK-owned */
};

#define FENYKL_KEY_LONG_US 800000u
#define FENYKL_KEY_LONG_MAX (1u << 29)
#define FENYKL_KEY_EVENT_PRIO 128u
#define FENYKL_KEY_EBUSY (-1)	/* handler already registered */
#define FENYKL_KEY_EARG (-2)	/* no on, keys empty or unknown bits, long_us too large, bad key */

struct fenykl_key_stats {
	uint32_t presses, releases, values;	/* key-class events in the table (values: mode dial) */
	uint32_t claimed;			/* presses and mode dial values a handler claimed */
	uint32_t unknown;			/* key-class events not in the table, passed */
	uint32_t last_unknown;			/* that event word */
	uint32_t ticks_long;			/* LONGs and replays decided by fenykl_key_tick */
};

/* Module "key": errors at ERR; claim, short, long, replay and busy at DEBUG (fenykl_log_dbg). */
extern struct fenykl_log_module fenykl_key_log;

/* Appends h (registration order decides between claims). Any CPU0 context, before or after install;
   h is static storage and stays registered for the boot. 0 or FENYKL_KEY_E*. */
int fenykl_key_add(struct fenykl_key_handler *h) FENYKL_CPU0_ONLY("fenykl_key_add");
/* Registers the key layer's event handler and installs the event layer. CPU0 task, not a handler;
   idempotent. 0 or FENYKL_EVENT_E*. */
int fenykl_key_install(void) FENYKL_CPU0_ONLY("fenykl_key_install");
/* Optional, plugin task, every ~50 ms: LONG (or the long_stock replay) at long_us while held. */
void fenykl_key_tick(void) FENYKL_CPU0_ONLY("fenykl_key_tick");
void fenykl_key_get_stats(struct fenykl_key_stats *st);

/* Press+release of k through the stock entry: the firmware acts, ui_key_get_state does not see it.
   CPU0 task or handler. 0 or FENYKL_KEY_EARG (NONE, the mode dial). */
int fenykl_key_inject(enum fenykl_key k) FENYKL_CPU0_ONLY("fenykl_key_inject");

#endif
