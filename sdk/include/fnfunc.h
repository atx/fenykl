/*
 * Button functions (sdk/docs/fnfunc.md): one plugin function as a native entry of the camera's
 * Button Function pickers (Fn1, Fn2, Fn3, REC, L-Fn), with a press callback. Built for bodies with
 * the fnfunc capability; zero bytes unless fenykl_fnfunc_install is called.
 */
#ifndef _FENYKL_FNFUNC_H
#define _FENYKL_FNFUNC_H

#include <stdbool.h>
#include <fenykl.h>
#include "log.h"

#define FENYKL_FNFUNC_TEXT_MAX 32u	/* characters of a name or help text */

enum fenykl_fnfunc_button {
	FENYKL_FNFUNC_FN1,
	FENYKL_FNFUNC_FN2,
	FENYKL_FNFUNC_FN3,
	FENYKL_FNFUNC_REC,
	FENYKL_FNFUNC_LFN,	/* the lens's L-Fn button */
	FENYKL_FNFUNC_BUTTONS,
};

struct fenykl_fnfunc {
	const char *name;	/* picker entry and row value cell; ASCII, <= FENYKL_FNFUNC_TEXT_MAX */
	const char *help;	/* the picker's help line; ASCII, <= FENYKL_FNFUNC_TEXT_MAX */
	/* A press of a button that holds the function. Event hook context (event.md 4): cheap, no
	   blocking calls; hand the work to the plugin task. The press reaches the firmware too, which
	   treats the function like Off. */
	void (*press)(enum fenykl_fnfunc_button b, void *arg);
	void *arg;
};

#define FENYKL_FNFUNC_EAGAIN (-1)	/* sections not loaded or not in live view: call again later */
#define FENYKL_FNFUNC_ESTOCK (-2)	/* a stock word differs from v1.4: nothing (left) written */
#define FENYKL_FNFUNC_EARG (-3)		/* no press, a text missing or too long */
#define FENYKL_FNFUNC_EBUSY (-4)	/* already installed */

/* Module "fnfunc": errors, install and the listener install. */
extern struct fenykl_log_module fenykl_fnfunc_log;

/* Adds f to the five pickers for the boot (f is static storage). Plugin task; retry on EAGAIN, and
   on ESTOCK for a few seconds after the boot (the record hash is built by the boot). 0 or
   FENYKL_FNFUNC_E*. */
int fenykl_fnfunc_install(const struct fenykl_fnfunc *f) FENYKL_CPU0_ONLY("fenykl_fnfunc_install");
/* Plugin task, every ~100 ms: once installed and a button holds the function, installs the event
   layer, and so the press callback. 0, or the event layer's error (logged; not tried again). */
int fenykl_fnfunc_tick(void) FENYKL_CPU0_ONLY("fenykl_fnfunc_tick");

#endif
