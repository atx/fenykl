/*
 * main: text formatting for the HUD elements, free of firmware calls so that the simulator
 * test (tests/sim/main/) runs the real code.
 */
#ifndef _MAIN_FMT_H
#define _MAIN_FMT_H

#include <stdint.h>

#define MAIN_FMT_DIST_INF 0xFFFFFFFFu	/* distance "inf" */
#define MAIN_FMT_DIST_SIZE 8		/* buffer size for main_fmt_dist, NUL included */

/*
 * Distance in mm as two HUD lines, the number (3 significant digits, at most 4 characters) over the
 * unit: "35\nmm", "25.0\ncm", "1.40\nm", "12.3\nm", "450\nm", "1.25\nkm", "450\nkm", "4290\nkm";
 * MAIN_FMT_DIST_INF gives "inf" (no unit line), 0 gives "".
 * Rounds half-up, carrying across units (9995 mm -> "10.0 m"). Writes at most
 * MAIN_FMT_DIST_SIZE bytes to buf and returns the string length.
 */
int main_fmt_dist(char *buf, uint32_t mm);

#endif
