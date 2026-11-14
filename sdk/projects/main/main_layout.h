/*
 * main: everything that depends on the target's screen and GUI resources, one section per target
 * with the HUD: element positions and the stock images and palette indices they are drawn with.
 */
#ifndef _MAIN_LAYOUT_H
#define _MAIN_LAYOUT_H

#include <hud.h>

/* main draws the HUD on every target with it; each has a layout section below. */
#define MAIN_HUD FENYKL_HAS_HUD

#if FENYKL_TARGET_EPL3

/*
 * HUD layout: blocks in the stock
 * info items' style on a transparent background, in the left column below the ISO item, as narrow
 * as it: a label ("FD", "DoF") over the number over its unit, like the ISO item's "ISO-A" over its
 * value. x puts the digits' ink at x 23 like the ISO item's. Each block's first ink starts 19 blank
 * rows below the ink above it, the stock rhythm. Left of the grid (x >= 96); free on Basic,
 * Histogram and Highlight&Shadow (camera OSD dumps).
 */
#define MAIN_LAYOUT_STATES FENYKL_HUD_INFO_SCREENS
#define MAIN_LAYOUT_BG FENYKL_HUD_CLEAR
#define MAIN_LAYOUT_ALIGN FENYKL_HUD_LEFT
#define MAIN_LAYOUT_STYLE FENYKL_HUD_LV
#define MAIN_LAYOUT_LINES_H(n) (((n) - 1) * FENYKL_HUD_LV_PITCH + FENYKL_HUD_LV_LINE_H)
#define MAIN_LAYOUT_X 20
#define MAIN_LAYOUT_W 56
#define MAIN_LAYOUT_FOCUS_Y 253
#define MAIN_LAYOUT_FOCUS_H MAIN_LAYOUT_LINES_H(3)
#define MAIN_LAYOUT_DOF_Y 327
#define MAIN_LAYOUT_DOF_H MAIN_LAYOUT_LINES_H(3)

/*
 * Focus meter: ten exposure-row cells 6 px apart, so that their 1 px
 * black borders join into the stock exposure scale's bar (1 px outside, 2 px between cells).
 * The bar's ink is x 641..700, y 42..48: right-aligned with the green AF dot (L2 x 678..700,
 * y 14..36 [camera]) and 5 rows below it, clear of the flash icon's inferred bottom
 * (y 41). Free in every camera OSD dump of a visible state. Unlit cells take the stock scale's
 * unmarked-cell colour.
 */
#define MAIN_LAYOUT_FMETER_X 640
#define MAIN_LAYOUT_FMETER_Y 41
#define MAIN_LAYOUT_FMETER_W 64
#define MAIN_LAYOUT_FMETER_H 9
#define MAIN_LAYOUT_FMETER_PITCH 6
#define MAIN_LAYOUT_FMETER_UNLIT 0x35	/* grey, alpha 170 [camera] */
/* Exposure-row cells (8 x 9, 1 px transparent ring) the bar is drawn from. */
#define MAIN_LAYOUT_FMETER_IMG_WHITE 1095u
#define MAIN_LAYOUT_FMETER_IMG_YELLOW 1096u
#define MAIN_LAYOUT_FMETER_IMG_GREEN 1098u
#define MAIN_LAYOUT_FMETER_IMG_FILL 0x04u	/* 1095's white fill */
#define MAIN_LAYOUT_FMETER_IMG_BORDER 0x7Eu	/* 1095's translucent border */
#define MAIN_LAYOUT_FMETER_CELL_W 8
#define MAIN_LAYOUT_FMETER_CELL_H 9

/*
 * Battery percentage: one LV line in the top-left corner, on the left column's x, its digits'
 * ink (rows 17..33) level with the green AF dot (y 14..36). Free of layer-1 items in every
 * camera OSD dump of a visible state.
 */
#define MAIN_LAYOUT_BATT_X MAIN_LAYOUT_X
#define MAIN_LAYOUT_BATT_Y 13
#define MAIN_LAYOUT_BATT_W 64

#elif FENYKL_TARGET_EM10II

/*
 * HUD layout, on the stock live-view info panel's 640 x 480 layout (the EVF's screen positions):
 * every block lies at x < 165, left of every stock item the LCD (720 wide) moves right, so one
 * layout serves both outputs. The left column (x 0..96) is taken from y 56 down (flash mode,
 * metering, AF mode, ISO, flash compensation, the LCD's touch button at x 0..86, y 301..365,
 * Myset/AEL from y 376, the mode icon); the top row from the Wi-Fi touch button at x 77. Blocks
 * in the stock info items' style on a transparent background; FD and DoF in the E-PL3's three-line
 * form, in the free second column right of the touch button, above the Myset/AEL row. Not yet
 * checked on the camera.
 */
#define MAIN_LAYOUT_STATES FENYKL_HUD_INFO_SCREENS
#define MAIN_LAYOUT_BG FENYKL_HUD_CLEAR
#define MAIN_LAYOUT_ALIGN FENYKL_HUD_LEFT
#define MAIN_LAYOUT_STYLE FENYKL_HUD_LV
#define MAIN_LAYOUT_LINES_H(n) (((n) - 1) * FENYKL_HUD_LV_PITCH + FENYKL_HUD_LV_LINE_H)
#define MAIN_LAYOUT_X 100
#define MAIN_LAYOUT_W 56
#define MAIN_LAYOUT_FOCUS_Y 233		/* to 297 */
#define MAIN_LAYOUT_FOCUS_H MAIN_LAYOUT_LINES_H(3)
#define MAIN_LAYOUT_DOF_Y 307		/* to 371 */
#define MAIN_LAYOUT_DOF_H MAIN_LAYOUT_LINES_H(3)

/* Battery percentage: one LV line in the free top-left corner, left of the Wi-Fi button. */
#define MAIN_LAYOUT_BATT_X 12
#define MAIN_LAYOUT_BATT_Y 13
#define MAIN_LAYOUT_BATT_W 64

/*
 * Focus meter: the E-PL3's bar of ten exposure-row cells 6 px apart, on the battery's x, 4 rows
 * below its line (y 13..35), above the flash mode item (y 56). Images 1089/1090/1092 are the
 * E-PL3's 1095/1096/1098 pixel for pixel, with this body's palette indices (stock image bytes).
 * Unlit cells: live-view layer-1 entry 0x2E, grey 0xA7A7A7 at alpha 21/31 [camera palette dump],
 * the E-PL3 unlit's alpha.
 */
#define MAIN_LAYOUT_FMETER_X MAIN_LAYOUT_BATT_X
#define MAIN_LAYOUT_FMETER_Y 40
#define MAIN_LAYOUT_FMETER_W 64
#define MAIN_LAYOUT_FMETER_H 9
#define MAIN_LAYOUT_FMETER_PITCH 6
#define MAIN_LAYOUT_FMETER_UNLIT 0x2E
#define MAIN_LAYOUT_FMETER_IMG_WHITE 1089u
#define MAIN_LAYOUT_FMETER_IMG_YELLOW 1090u
#define MAIN_LAYOUT_FMETER_IMG_GREEN 1092u
#define MAIN_LAYOUT_FMETER_IMG_FILL 0x04u	/* 1089's white fill */
#define MAIN_LAYOUT_FMETER_IMG_BORDER 0x11u	/* 1089's translucent border */
#define MAIN_LAYOUT_FMETER_CELL_W 8
#define MAIN_LAYOUT_FMETER_CELL_H 9

/*
 * Pre-capture tag ("PRE", "PRE!"): one LV line at the top of the second column, which holds no
 * fixed panel item. In movie live view the timecode row (x 1..185, y 53..75) lies under it.
 */
#define MAIN_LAYOUT_PRECAP_X MAIN_LAYOUT_X
#define MAIN_LAYOUT_PRECAP_Y 52
#define MAIN_LAYOUT_PRECAP_W 64

#elif MAIN_HUD
#error "main: no layout for this target"
#endif

#endif
