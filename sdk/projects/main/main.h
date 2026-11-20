/*
 * main: the plugin's shared declarations.
 */
#ifndef _MAIN_H
#define _MAIN_H

#include <stdbool.h>
#include <cfg.h>
#include <hud.h>
#include <log.h>
#include "main_layout.h"

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
extern const struct main_feat feat_precap;

/* feat_focus.c, for feat_dof.c */
uint32_t feat_focus_read(void);
int feat_focus_add(struct fenykl_hud_elem *e, const char *label,
		   const struct fenykl_log_module *log);

/*
 * The action of the Button Function entry (main_fn.c): claim() at the press, in the posting
 * task, decides per GUI state; start() runs in the plugin task, 0 = busy until step() returns
 * false. step() runs once per tick while busy.
 */
struct main_fn_act {
	const char *name;		/* the entry's name and help in the stock pickers */
	const char *help;
	struct fenykl_log_module *log;	/* level follows main.log.level */
	bool (*claim)(void);
	int (*start)(uint32_t press_us);
	bool (*step)(void);
	void (*status)(void);
};

extern const struct main_fn_act act_shot;

/* main_fn.c, plugin task; no Button Function entry without the fnfunc and shot capabilities */
#define MAIN_FN (FENYKL_HAS_FNFUNC && FENYKL_HAS_SHOT)
#if MAIN_FN
void main_fn_tick(void);
void main_fn_set_level(unsigned int level);
void main_fn_status(void);
#else
static inline void main_fn_tick(void)
{
}

static inline void main_fn_set_level(unsigned int level)
{
	(void)level;
}

static inline void main_fn_status(void)
{
}
#endif

/* The "Image" page of the R/B swap option (main_rbswap.c). */
#if FENYKL_HAS_WB_BASE
extern const struct fenykl_cfg_group main_grp_image;
#endif

#endif
