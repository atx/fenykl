/*
 * Screenshot (sdk/docs/shot.md): what the panel shows, the live view and the GUI's OSD layers, to
 * C:\SHOTnnnn.PPM at square pixels (E-PL3 853 x 480; E-M10 II 720 x 480 on the LCD, 1024 x 768
 * on the EVF). fenykl_shot_start takes the snapshot into firmware memory lent for the write (E-PL3
 * a RECFILE run, E-M10 II block 0 of the PRIM still-image pool); each fenykl_shot_step composes
 * and writes a strip, so the calling task keeps working during the write. Bodies with the shot
 * capability (body.h).
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

/* CPU0 task, in live view or the menu: file open, snapshot and header (up to ~1.5 s: flip and
   commit waits, retries). true = busy: call fenykl_shot_step until it returns false; false = no
   screenshot (logged). */
bool fenykl_shot_start(void) FENYKL_CPU0_ONLY("fenykl_shot_start");
/* The same task, every ~50 ms while busy: one strip or raw chunk; false once the files are done,
   failed or aborted (the camera's state changed, body.h fenykl_shot_cond, or the buffer was lent
   for 30 s). */
bool fenykl_shot_step(void) FENYKL_CPU0_ONLY("fenykl_shot_step");
/* The first `first` snapshots of the boot also write the raw snapshot after the PPM: SHOTnnnn.LV
   (the live view as copied, 2 * w bytes per row), .L0/.L1/.L2 (each OSD layer's copied rows), .PAL
   (the firmware's palette block) and .REG (geometry, sources, free blocks and display registers as
   text). 0 by default. */
void fenykl_shot_raw(unsigned int first);
/* Logs the counters: shots, failed, aborted, overwritten, torn copies, shots without live view. */
void fenykl_shot_status(void);

#endif
