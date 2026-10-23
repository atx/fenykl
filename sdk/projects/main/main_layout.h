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
 * HUD layout: the battery percentage only, one line in the stock info items' style on a
 * transparent background, at the E-PL3's top-left spot, inside the EVF's left 640 columns.
 * Not yet checked against this body's stock items: no OSD dump of its info screens.
 */
#define MAIN_LAYOUT_STATES FENYKL_HUD_INFO_SCREENS
#define MAIN_LAYOUT_BG FENYKL_HUD_CLEAR
#define MAIN_LAYOUT_ALIGN FENYKL_HUD_LEFT
#define MAIN_LAYOUT_STYLE FENYKL_HUD_LV
#define MAIN_LAYOUT_BATT_X 20
#define MAIN_LAYOUT_BATT_Y 13
#define MAIN_LAYOUT_BATT_W 64

#elif MAIN_HUD
#error "main: no layout for this target"
#endif

#endif
