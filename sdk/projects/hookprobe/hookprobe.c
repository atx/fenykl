/* E-M10 II hook probe and input map: pass-through hooks on cw_updateDisplay and appevent_post that only
   count and log, plus a 20 ms poll of the input state (hp_input.c). Installs both hooks at start,
   reports every 5 s, removes both after 15 minutes and reports 30 s more. CPU0 events are queued by
   the hook and logged by the poll (hp_input.c); calls on CPU1/CPU2 are only counted. Run only after
   icprobe passed. Logs to C:\FENYKL.LOG. */
#include <fenykl.h>
#include <log.h>

#include "hookprobe.h"

#define HP_POLL_MS 20
#define HP_POLLS_PER_PERIOD 250u /* 5 s */
#define HP_HOOKED_PERIODS 180u   /* 15 minutes */
#define HP_AFTER_PERIODS 6u
#define HP_SEC4_LOADED 0x08u /* section_loaded bit 3 */
#define HP_SEC4_POLLS 300u   /* x 100 ms */
#define HP_FLUSH_TRIES 50u

FENYKL_LOG_MODULE(hp_log, "hookp", FENYKL_LOG_INFO);

/* Written by the hooks on the core whose id is the index (3 is never expected); read by the reporter. */
struct hp_core {
	uint32_t cw_calls;
	uint32_t cw_zero;  /* (int16_t)show_front == 0 */
	uint32_t cw_one;   /* == 1 */
	uint32_t cw_other; /* anything else */
	uint32_t cw_other_last;
	uint32_t ev_calls;
	uint32_t ev_last;
	uint32_t ev_last_param;
	uint32_t ev_last_flag;
};
static FENYKL_SHARED struct hp_core hp_cores[4];

static struct {
	uint32_t cw_prev;   /* reporter: CPU0 cw_calls at the last report */
	uint32_t cw_max;    /* most CPU0 cw_updateDisplay calls in one period */
	uint32_t ev_prev;
} hp;

static volatile struct hp_core *hp_core(uint32_t id)
{
	return &(*FENYKL_SHARED_PTR(hp_cores))[id & 3];
}

static void hp_on_update(int show_front)
{
	uint32_t psw = fw_psw_get();
	volatile struct hp_core *c;

	fw_irq_disable_all();
	c = hp_core(FENYKL_CORE_ID());
	c->cw_calls++;
	if ((int16_t)show_front == 0) {
		c->cw_zero++;
	} else if ((int16_t)show_front == 1) {
		c->cw_one++;
	} else {
		c->cw_other++;
		c->cw_other_last = show_front;
	}
	fw_irq_mask_restore(psw);
	fw_orig_cw_updateDisplay(show_front);
}
FENYKL_HOOK(cw_updateDisplay, hp_on_update);

static void hp_count_event(uint32_t core, unsigned int event, unsigned int param, unsigned int flag)
{
	uint32_t psw = fw_psw_get();
	volatile struct hp_core *c;

	fw_irq_disable_all();
	c = hp_core(core);
	c->ev_calls++;
	c->ev_last = event;
	c->ev_last_param = param;
	c->ev_last_flag = flag;
	fw_irq_mask_restore(psw);
}

/* CPU1/CPU2 calls are counted and passed through; CPU0 calls are also queued for the log. */
static int hp_on_event(unsigned int event, unsigned int param, unsigned int flag)
{
	uint32_t core = FENYKL_CORE_ID();

	hp_count_event(core, event, param, flag);
	if (core == 0)
		hp_input_event(event, param, flag);
	return fw_orig_appevent_post(event, param, flag);
}
FENYKL_HOOK(appevent_post, hp_on_event);

static void hp_log_entry(const char *what, uint32_t addr)
{
	const volatile uint8_t *p = FENYKL_UNCACHED(addr);

	fenykl_log_info(&hp_log, "%s %08x: %06x %04x", (uint32_t)what, addr, p[0] << 16 | p[1] << 8 | p[2],
			p[3] << 8 | p[4]);
}

static void hp_log_icache(const char *what, int result)
{
	struct fenykl_icache_stats st;

	fenykl_icache_get_stats(&st);
	fenykl_log_info(&hp_log, "%s %d: icache runs %u timeouts %u", (uint32_t)what, result, st.runs,
			st.timeouts);
	fenykl_log_info(&hp_log, "%s: polls %u/%u chctr %08x", (uint32_t)what, st.polls[0], st.polls[1],
			st.chctr[0] << 16 | st.chctr[3]);
}

static void hp_report(unsigned int period)
{
	volatile struct hp_core *c0 = hp_core(0);
	uint32_t cw = c0->cw_calls, ev = c0->ev_calls;

	if (cw - hp.cw_prev > hp.cw_max)
		hp.cw_max = cw - hp.cw_prev;
	fenykl_log_info(&hp_log, "p%u cw cores %u/%u/%u", period, cw, hp_core(1)->cw_calls,
			hp_core(2)->cw_calls);
	fenykl_log_info(&hp_log, "p%u ev cores %u/%u/%u", period, ev, hp_core(1)->ev_calls,
			hp_core(2)->ev_calls);
	fenykl_log_info(&hp_log, "p%u cw +%u (max %u), ev +%u", period, cw - hp.cw_prev, hp.cw_max,
			ev - hp.ev_prev);
	fenykl_log_info(&hp_log, "p%u cw d0 0:%u 1:%u other:%u", period, c0->cw_zero, c0->cw_one,
			c0->cw_other);
	hp.cw_prev = cw;
	hp.ev_prev = ev;
	if (c0->cw_other)
		fenykl_log_info(&hp_log, "p%u cw last other d0 %08x", period, c0->cw_other_last);
	for (uint32_t id = 1; id < 4; id++) {
		volatile struct hp_core *c = hp_core(id);

		if (c->cw_calls || c->ev_calls)
			fenykl_log_warn(&hp_log, "core %u: last ev %08x param %08x flag %04x", id, c->ev_last,
					c->ev_last_param, c->ev_last_flag & 0xFFFF);
	}
	hp_input_report(period);
}

/* Polls the inputs every 20 ms and reports every 5 s. */
static void hp_periods(unsigned int first, unsigned int count)
{
	for (unsigned int p = first; p < first + count; p++) {
		for (unsigned int i = 0; i < HP_POLLS_PER_PERIOD; i++) {
			fw_OlyTaskSleep(HP_POLL_MS);
			hp_input_poll();
		}
		hp_report(p);
	}
}

static int hp_wait_sec4(void)
{
	for (unsigned int i = 0; i < HP_SEC4_POLLS; i++) {
		if (*(volatile uint8_t *)FW_SECTION_LOADED & HP_SEC4_LOADED)
			return 0;
		fw_OlyTaskSleep(100);
	}
	return -1;
}

static void hp_final_flush(void)
{
	for (unsigned int i = 0; i < HP_FLUSH_TRIES; i++) {
		if (fenykl_log_flush() == 0)
			return;
		fw_OlyTaskSleep(200);
	}
}

/* Removing a hook that is not installed does nothing. */
static void hp_remove_all(void)
{
	hp_log_icache("remove cw_updateDisplay", fenykl_hook_remove(&fenykl_hook_cw_updateDisplay));
	hp_log_icache("remove appevent_post", fenykl_hook_remove(&fenykl_hook_appevent_post));
	hp_log_entry("cw_updateDisplay", fenykl_hook_cw_updateDisplay.entry);
	hp_log_entry("appevent_post", fenykl_hook_appevent_post.entry);
}

static void hp_run(void)
{
	struct fenykl_log_stats ls;
	int pre, cw, ev = 0;

	fenykl_log_info(&hp_log, "chctr %04x loaded mask %02x", *(volatile uint16_t *)FW_CHCTR,
			*(volatile uint8_t *)FW_SECTION_LOADED);
	if (hp_wait_sec4()) {
		fenykl_log_err(&hp_log, "sec4 not loaded: no hooks");
		return;
	}
	/* The invalidate must work before any entry is patched. */
	pre = fenykl_icache_invalidate();
	hp_log_icache("pre-check", pre);
	if (pre) {
		fenykl_log_err(&hp_log, "I-cache invalidate failed: no hooks");
		return;
	}
	hp_log_entry("cw_updateDisplay", fenykl_hook_cw_updateDisplay.entry);
	hp_log_entry("appevent_post", fenykl_hook_appevent_post.entry);

	cw = fenykl_hook_install(&fenykl_hook_cw_updateDisplay);
	hp_log_icache("install cw_updateDisplay", cw);
	if (cw == 0) {
		ev = fenykl_hook_install(&fenykl_hook_appevent_post);
		hp_log_icache("install appevent_post", ev);
	}
	hp_log_entry("cw_updateDisplay", fenykl_hook_cw_updateDisplay.entry);
	hp_log_entry("appevent_post", fenykl_hook_appevent_post.entry);
	/* One failed install is enough evidence: nothing else is patched. */
	if (cw || ev) {
		fenykl_log_err(&hp_log, "install failed (%d, %d): removing, probe stopped", cw, ev);
		hp_remove_all();
		return;
	}

	hp_periods(1, HP_HOOKED_PERIODS);
	hp_remove_all();
	hp_periods(HP_HOOKED_PERIODS + 1, HP_AFTER_PERIODS);
	fenykl_log_get_stats(&ls);
	fenykl_log_info(&hp_log, "log produced %u written %u overwritten %u lost %u", ls.produced[0],
			ls.written[0], ls.overwritten0, ls.lost[0]);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(0);
	int task = fenykl_log_task_start();

	(void)load_base;
	(void)image_size;
	fenykl_log_info(&hp_log, "hookprobe E-M10II: log init %d task %d core %u", init, task,
			FENYKL_CORE_ID());
	hp_run();
	fenykl_log_info(&hp_log, "hookprobe end");
	hp_final_flush();
}
