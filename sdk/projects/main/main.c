/*
 * main: the polished daily-use plugin. Starts logging, configuration and the HUD library,
 * binds the hook (main_hooks.c), installs the settings page (sdk/lib/ui.c) and runs every
 * feature's tick, the Fn override (main_fn.c) and the R/B swap's log (main_rbswap.c)
 * from one task loop. Log: C:\FENYKL.LOG; options: C:\FENYKL.CF0/CF1. A target without the hud
 * or event capability gets neither the HUD nor the Fn override (MAIN_HUD).
 */
#include "main.h"
#include "main_rbswap.h"
#include <afc.h>
#include <body.h>
#include <ui.h>

#define MAIN_GUI_WAIT_TICKS (60000u / MAIN_TICK_MS)	/* for the GUI and its language font */
#define MAIN_STATUS_FIRST (3000u / MAIN_TICK_MS)
#define MAIN_STATUS_TICKS (30000u / MAIN_TICK_MS)

/* Each feature in order, those of the target's capabilities (plugin.toml); NULL-terminated, as a
   target may have none. */
static const struct main_feat *const main_feats[] = {
#if FENYKL_HAS_LENS
	&feat_focus,
	&feat_dof,
#endif
#if FENYKL_HAS_AFC
	&feat_fmeter,
#endif
#if FENYKL_HAS_BATT
	&feat_batt,
#endif
	NULL,
};


struct main_state {
	uint32_t ticks;
	uint16_t gui;			/* super << 8 | state last logged */
	bool drawing;			/* HUD up, hook installed, no CPU1 call seen */
	bool started[sizeof(main_feats) / sizeof(main_feats[0])];
};

static struct main_state main_st = { .gui = 0xFFFF };

static FENYKL_LOG_MODULE(main_log, "main", FENYKL_LOG_INFO);

static const char *const main_log_names[] = { "err", "warn", "info", "debug" };

static void main_log_apply(const struct fenykl_cfg_opt *o, int32_t old);

FENYKL_CFG_GROUP(main_grp_hud, "HUD", 1);
static FENYKL_CFG_GROUP(main_grp_sys, "System", 9);
FENYKL_CFG_ENUM(main_log_level, "main.log.level", "Log level", &main_grp_sys, 2, main_log_names,
		.on_change = main_log_apply);

static void main_log_apply(const struct fenykl_cfg_opt *o, int32_t old)
{
	unsigned int level = FENYKL_LOG_ERROR + (unsigned int)main_log_level;

	fenykl_log_set_level(&main_log, level);
	fenykl_log_set_level(&fenykl_ui_log, level);
#if FENYKL_HAS_AFC
	fenykl_log_set_level(&fenykl_afc_log, level);
#endif
	for (unsigned int i = 0; main_feats[i]; i++)
		fenykl_log_set_level(main_feats[i]->log, level);
	main_fn_set_level(level);
	main_rbswap_set_level(level);
	if (o)
		fenykl_log_info(&main_log, "log level %s (was %s)", main_log_names[main_log_level],
				main_log_names[old]);
}

static void main_sleep(void)
{
	fenykl_task_sleep(MAIN_TICK_MS);
	main_st.ticks++;
}

static void main_start_cfg(void)
{
	struct fenykl_cfg_stats st;
	int r = fenykl_cfg_init();

	fenykl_cfg_get_stats(&st);
	fenykl_log_info(&main_log, "cfg init %d: slot %d seq %u, import %u", r, st.slot, st.seq,
			st.imported);
	if (st.invalid || st.unknown)
		fenykl_log_warn(&main_log, "cfg carried: invalid %u, unknown %u", st.invalid,
				st.unknown);
	main_log_apply(NULL, 0);
	fenykl_log_info(&main_log, "log level %s, saver task %d", main_log_names[main_log_level],
			fenykl_cfg_task_start());
}

#if MAIN_HUD
/* Waits for live view or playback with a loaded language font. */
static bool main_wait_gui(void)
{
	for (unsigned int i = 0; i < MAIN_GUI_WAIT_TICKS; i++) {
		if ((fenykl_gui_live_view() || fenykl_gui_playback()) && fenykl_hud_font())
			return true;
		main_sleep();
	}
	return false;
}

/* GUI, HUD library, hook: true if the features may draw. */
static bool main_start_hud(void)
{
	struct fenykl_hud_buf buf[2] = { 0 };
	int r;

	if (!main_wait_gui()) {
		fenykl_log_err(&main_log, "no GUI after %u ticks (gui %04x, font %u): no HUD",
			       main_st.ticks, fenykl_gui_state(), fenykl_hud_font());
		return false;
	}
	fenykl_log_info(&main_log, "GUI after %u ticks, font id %u", main_st.ticks,
			fenykl_hud_font());
	r = fenykl_hud_init(MAIN_LAYOUT_STATES, buf);
	for (unsigned int b = 0; b < 2; b++)
		fenykl_log_info(&main_log, "L1b%u handle %08x plane %x pData %08x", b, buf[b].handle,
				buf[b].plane, buf[b].pdata);
	if (r) {
		fenykl_log_err(&main_log, "hud init %d: no HUD", r);
		return false;
	}
	r = main_hooks_install();
	fenykl_log_info(&main_log, "hook cw_updateDisplay install %d", r);
	if (r) {
		fenykl_log_err(&main_log, "no hook: no HUD (nothing patched)");
		return false;
	}
	return true;
}
#endif

/* Settings page from a Custom menu row (sdk/docs/ui.md); does not need the HUD. */
static void main_start_ui(void)
{
	unsigned int i;
	int r;

	for (i = 0; i < MAIN_GUI_WAIT_TICKS; i++) {
		r = fenykl_ui_install();
		if (r != FENYKL_UI_EAGAIN)
			break;
		main_sleep();
	}
	if (r)
		fenykl_log_err(&main_log, "ui install %d after %u tries: no settings page", r, i);
	else
		fenykl_log_info(&main_log, "ui install 0 after %u tries", i);
}

#if MAIN_HUD
static void main_start_feats(void)
{
	for (unsigned int i = 0; main_feats[i]; i++) {
		int r = main_feats[i]->start();

		main_st.started[i] = r == 0;
		fenykl_log_info(&main_log, "feature %s start %d", main_feats[i]->name, r);
	}
}

/* A hook call on CPU1 stops all drawing for this boot. */
static void main_check_cpu1(const struct fenykl_hud_stats *hs)
{
	if (!main_st.drawing || hs->cpu1 == 0)
		return;
	main_st.drawing = false;
	main_hooks_draw_off();
	for (unsigned int i = 0; main_feats[i]; i++)
		if (main_st.started[i])
			main_feats[i]->stop();
	fenykl_log_err(&main_log, "cw_updateDisplay ran on CPU1 (%u calls): drawing stopped",
		       hs->cpu1);
	fenykl_log_flush();
}
#endif

static void main_log_gui(void)
{
	uint16_t gui = fenykl_gui_state();

	if (gui == main_st.gui)
		return;
	main_st.gui = gui;
#if MAIN_HUD
	fenykl_log_info(&main_log, "gui %02x:%02x %s", gui >> 8, gui & 0xFF,
			fenykl_hud_visible() ? "visible" : "hidden");
#else
	fenykl_log_info(&main_log, "gui %02x:%02x", gui >> 8, gui & 0xFF);
#endif
}

/* hs: the HUD's counters, NULL without the HUD */
static void main_status(const struct fenykl_hud_stats *hs)
{
	struct fenykl_ui_stats us;

	fenykl_ui_get_stats(&us);
#if MAIN_HUD
	fenykl_log_info(&main_log, "status t %u drawing %d updates %u shows %u", main_st.ticks,
			main_st.drawing, hs->updates, hs->shows);
	fenykl_log_info(&main_log, "  drawn %u hidden %u erased %u cpu1 %u", hs->drawn, hs->hidden,
			hs->erased, hs->cpu1);
	fenykl_log_info(&main_log, "  stale %u override %u nofont %u miss %u", hs->stale,
			hs->overrides, hs->nofont, hs->glyph_miss);
#else
	(void)hs;
	fenykl_log_info(&main_log, "status t %u", main_st.ticks);
#endif
	fenykl_log_info(&main_log, "  ui entry %u exit %u set %u fail %u", us.entries, us.exits,
			us.sets, us.set_fail);
	fenykl_log_info(&main_log, "  ui refused %u stray %u cpu1 %u", us.refused, us.lang_stray,
			us.cpu1_actions);
	for (unsigned int i = 0; main_feats[i]; i++)
		if (main_st.started[i])
			main_feats[i]->status();
	main_fn_status();
	main_rbswap_status();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);

	fenykl_log_task_start();
	fenykl_log_info(&main_log, "main, load %08x size %x, log init %d", load_base,
			image_size, r);
	main_start_cfg();
	main_rbswap_start();
#if MAIN_HUD
	main_st.drawing = main_start_hud();
	if (main_st.drawing)
		main_start_feats();
#endif
	main_start_ui();

	for (;;) {
#if MAIN_HUD
		struct fenykl_hud_stats hs;
#endif

		bool slow;

		main_sleep();
		slow = main_st.ticks % MAIN_SLOW == 0;
#if MAIN_HUD
		fenykl_hud_get_stats(&hs);
		main_check_cpu1(&hs);
#endif
		main_log_gui();
		main_fn_tick();
		main_rbswap_tick();
		if (slow)
			fenykl_ui_tick();
		for (unsigned int i = 0; main_st.drawing && main_feats[i]; i++)
			if (main_st.started[i] && main_st.ticks % main_feats[i]->period == 0)
				main_feats[i]->tick();
#if MAIN_HUD
		if (slow)
			fenykl_hud_tick();
		if (main_st.ticks % MAIN_STATUS_TICKS == MAIN_STATUS_FIRST)
			main_status(&hs);
#else
		if (main_st.ticks % MAIN_STATUS_TICKS == MAIN_STATUS_FIRST)
			main_status(NULL);
#endif
	}
}
