/*
 * main: the plugin's shared declarations.
 */
#ifndef _MAIN_H
#define _MAIN_H

#include <stdbool.h>
#include <cfg.h>
#include <hud.h>
#include <log.h>

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

/*
 * Battery percentage: one LV line in the top-left corner, on the left column's x, its digits'
 * ink (rows 17..33) level with the green AF dot (y 14..36). Free of layer-1 items in every
 * camera OSD dump of a visible state.
 */
#define MAIN_LAYOUT_BATT_X MAIN_LAYOUT_X
#define MAIN_LAYOUT_BATT_Y 13
#define MAIN_LAYOUT_BATT_W 64

/* A feature: one module with its own state struct, options and log module. */
struct main_feat {
	const char *name;
	struct fenykl_log_module *log;	/* level follows main.log.level */
	unsigned int period;		/* ticks between tick calls */
	int (*start)(void);		/* after the HUD is up: adds its elements; 0 ok */
	void (*tick)(void);		/* plugin task, every period * MAIN_TICK_MS */
	void (*stop)(void);		/* hides its elements for good */
	void (*status)(void);		/* logs its counters */
};

#define MAIN_TICK_MS 50
#define MAIN_SLOW 2u			/* ticks: the 100 ms period of the libraries and text blocks */

extern const struct fenykl_cfg_group main_grp_hud;
extern const struct main_feat feat_focus;
extern const struct main_feat feat_dof;
extern const struct main_feat feat_fmeter;
extern const struct main_feat feat_batt;

/* feat_focus.c, for feat_dof.c */
uint32_t feat_focus_read(void);
int feat_focus_add(struct fenykl_hud_elem *e, const char *label,
		   const struct fenykl_log_module *log);

/*
 * An Fn override action (main_fn.c): claim() in the key task decides per GUI
 * state; start() runs in the plugin task at the release of a short press, 0 = busy until step()
 * returns false. step() runs once per tick while busy.
 */
struct main_fn_act {
	const char *name;
	struct fenykl_log_module *log;	/* level follows main.log.level */
	bool (*claim)(uint16_t gui);
	int (*start)(uint32_t press_us);
	bool (*step)(void);
	void (*status)(void);
};

extern const struct main_fn_act act_shot;

/* main_fn.c, plugin task */
void main_fn_tick(void);
void main_fn_set_level(unsigned int level);
void main_fn_status(void);

/* main_hooks.c */
int main_hooks_install(void);
void main_hooks_draw_off(void);

#endif
