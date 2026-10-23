/*
 * Screenshot (sdk/docs/shot.md): what the panel shows, the live view and the GUI's OSD layers, to
 * C:\SHOTnnnn.PPM (FENYKL_DISP_SQUARE_W x FENYKL_DISP_H, 16:9). fenykl_shot_start takes the
 * snapshot into a lent RECFILE block run; each fenykl_shot_step composes and writes a strip of 24
 * rows, so the calling task keeps working during the ~3 s write. Bodies with the shot capability
 * (body.h).
 */
#ifndef _FENYKL_SHOT_H
#define _FENYKL_SHOT_H

#include <stdbool.h>
#include <fenykl.h>
#include "body.h"
#include "log.h"

/* Module "shot": the written file at INFO, refusals at WARN, errors at ERR, copy details at
   DEBUG. */
extern struct fenykl_log_module fenykl_shot_log;

/* CPU0 task, in live view: snapshot, file open and header (up to ~0.3 s). true = busy: call
   fenykl_shot_step until it returns false; false = no screenshot (logged). */
bool fenykl_shot_start(void) FENYKL_CPU0_ONLY("fenykl_shot_start");
/* The same task, every ~50 ms while busy: one strip; false once the file is done or failed. */
bool fenykl_shot_step(void) FENYKL_CPU0_ONLY("fenykl_shot_step");
/* Logs the counters: shots, failed, torn copies, shots without live view. */
void fenykl_shot_status(void);

#endif
