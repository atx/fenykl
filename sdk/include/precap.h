/*
 * Pre-capture (sdk/docs/precap.md), E-M10 II only: it holds frames of the stock silent sequential H
 * burst, which needs the fully electronic shutter, and the E-PL3 has none.
 * A half press held 300 ms past AF ready starts the burst with an injected full press; the last 6
 * frames are held unrecorded, the oldest dropped. A real full press saves the held frames and every
 * later one of the burst; a half release cancels it without a file. Outside its settings gate the
 * camera is stock. Targets with the precap capability.
 */
#ifndef _FENYKL_PRECAP_H
#define _FENYKL_PRECAP_H

#include <stdbool.h>
#include <fenykl.h>
#include "log.h"

enum fenykl_precap_state {
	FENYKL_PRECAP_OFF,	/* option off, settings outside the gate or not installed */
	FENYKL_PRECAP_ARMED,	/* a half press will start a pre-capture burst */
	FENYKL_PRECAP_RING,	/* a pre-capture burst holds frames */
	FENYKL_PRECAP_SAVE,	/* the burst was committed: frames are recorded */
	FENYKL_PRECAP_FAILED,	/* a fail-safe tripped: off until disabled and enabled again */
};

/* Module "precap": one line per burst at INFO, refusals and fail-safes at WARN and ERR, the key
   control at DEBUG. */
extern struct fenykl_log_module fenykl_precap_log;

/* Starts the pre-capture task (CPU0, below the stock HFW and REC tasks); 0 or < 0. Disabled until
   fenykl_precap_enable(true). */
int fenykl_precap_start(void) FENYKL_CPU0_ONLY("fenykl_precap_start");
/* Turns pre-capture on or off; any task. Turning it off clears a tripped fail-safe. */
void fenykl_precap_enable(bool on);
/* What the camera does on the next half press, or the burst running; any task. */
enum fenykl_precap_state fenykl_precap_state(void);
/* Logs the counters. */
void fenykl_precap_status(void);

#endif
