/*
 * APPEVENT interception (sdk/docs/event.md). The SDK binds the appevent_post hook (target.toml
 * sdk_hooks); plugin code registers handlers in a priority-sorted chain. Linked into every plugin;
 * zero bytes unless fenykl_event_install is called.
 */
#ifndef _FENYKL_EVENT_H
#define _FENYKL_EVENT_H

#include <stdbool.h>
#include <fenykl.h>
#include "log.h"

/* event word = class << 16 | code; the rear dial's class 0x0203 never
   reaches the hook on the camera (key.md 5) */
#define FENYKL_EVENT_CLASS_MASK 0xFFFF0000u
#define FENYKL_EVENT_CODE_MASK 0x0000FFFFu
#define FENYKL_EVENT_KEY_PRESS 0x02000000u
#define FENYKL_EVENT_KEY_RELEASE 0x02010000u
#define FENYKL_EVENT_KEY_VALUE 0x02020000u	/* mode dial */
#define FENYKL_EVENT_IS_KEY(ev) (((ev) & 0xFF000000u) == 0x02000000u)

#define FENYKL_EVENT_EBUSY (-1)		/* handler already registered */
#define FENYKL_EVENT_EARG (-2)		/* no fn */
#define FENYKL_EVENT_EHOOK (-3)		/* fenykl_hook_install/remove failed (its code is logged) */

/* Hook entries while this many dispatches are in progress skip the chain (stats.bypassed). */
#define FENYKL_EVENT_DEPTH_MAX 2u

struct fenykl_event {
	uint32_t event;		/* editable: what later handlers and the firmware see */
	uint32_t param;
	uint32_t flag;		/* low 16 bits go into the queue record */
};

enum fenykl_event_action {
	FENYKL_EVENT_PASS,	/* on to the next handler, then the firmware */
	FENYKL_EVENT_CONSUME,	/* dropped: no later handler, no firmware; the hook returns 0 */
};

struct fenykl_event_handler {
	const char *name;	/* for the log */
	enum fenykl_event_action (*fn)(struct fenykl_event *ev, void *arg);
	void *arg;
	uint8_t prio;		/* 0 runs first; equal prio: registration order */
	volatile bool enabled;	/* false pauses the handler; any context */
	struct {
		struct fenykl_event_handler *next;
		uint32_t seen, consumed;
	} private;		/* SDK-owned */
};

struct fenykl_event_stats {
	uint32_t events;	/* hook entries on CPU0 */
	uint32_t consumed, injected;
	uint32_t nested;	/* entries while another dispatch was in progress (event.md 4) */
	uint32_t bypassed;	/* entries at FENYKL_EVENT_DEPTH_MAX: chain skipped, must stay 0 */
	uint32_t cpu1;		/* must stay 0 (appevent_post is CPU0-only) */
};

/* Module "event": errors, install/remove and the first nested entry. */
extern struct fenykl_log_module fenykl_event_log;

/* Links h into the chain by prio. Any CPU0 context, before or after install; h is static storage
   and stays registered for the boot (pause with enabled = false). 0 or FENYKL_EVENT_E*. */
int fenykl_event_add(struct fenykl_event_handler *h) FENYKL_CPU0_ONLY("fenykl_event_add");
/* Patches appevent_post. CPU0 task, not a handler; idempotent. 0 or FENYKL_EVENT_EHOOK. */
int fenykl_event_install(void) FENYKL_CPU0_ONLY("fenykl_event_install");
/* Restores the stock entry; handlers stay registered. CPU0 task, not a handler. */
int fenykl_event_remove(void) FENYKL_CPU0_ONLY("fenykl_event_remove");
/* Posts to APPEQUE through the stock entry: handlers never see it. CPU0 task or handler, never an
   ISR (the queue write may block); returns the firmware's result. */
int fenykl_event_inject(uint32_t event, uint32_t param, uint32_t flag)
	FENYKL_CPU0_ONLY("fenykl_event_inject");
void fenykl_event_get_stats(struct fenykl_event_stats *st);
/* The hook's body: runs the chain, then the stock function. Tests call it without the patch. */
int fenykl_event_dispatch(uint32_t event, uint32_t param, uint32_t flag);

#endif
