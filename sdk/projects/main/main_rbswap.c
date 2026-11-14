/*
 * main: option "R/B swap" and, on the E-PL3, the plugin task's side. The img_exec hook
 * (main_img_hook.c) is installed at start whatever the option and passes through while it is off;
 * the option only sets the hook's enable word. Each swapped still logs its input matrix, so the
 * camera run can check the coefficient range. The E-M10 II's task side is main_rbswap_wb.c, which
 * follows the option from its tick.
 */
#include "main.h"
#include "main_rbswap.h"

#if MAIN_RBSWAP
FENYKL_LOG_MODULE(main_rbswap_log, "rbswap", FENYKL_LOG_INFO);

static void main_rbswap_apply(const struct fenykl_cfg_opt *o, int32_t old);

FENYKL_CFG_GROUP(main_grp_image, "Image", 3);
FENYKL_CFG_BOOL(main_rbswap_on, "main.rbswap.on", "R/B swap", &main_grp_image, 0,
		.on_change = main_rbswap_apply);

/* Any task (the cfg task, the settings page): the E-M10 II's registry work waits for the tick. */
static void main_rbswap_apply(const struct fenykl_cfg_opt *o, int32_t old)
{
	(void)o;
	(void)old;
#if FENYKL_HAS_IMG_EXEC
	FENYKL_SHARED_PTR(main_rbswap_hk)->enable = main_rbswap_on != 0;
#endif
	fenykl_log_info(&main_rbswap_log, "R/B swap %s", main_rbswap_on ? "on" : "off");
}

void main_rbswap_set_level(unsigned int level)
{
	fenykl_log_set_level(&main_rbswap_log, level);
}
#endif

#if FENYKL_HAS_IMG_EXEC
struct main_rbswap_state {
	bool installed;
	bool cpu1_seen;		/* the hook ran on CPU1 (logged once) */
	uint32_t m_seq;		/* hook m_seq at the last logged matrix */
};

static struct main_rbswap_state main_rbswap_st;

/* After the img_exec hook is installed. */
void main_rbswap_start(void)
{
	volatile struct main_rbswap_hook_state *s = FENYKL_SHARED_PTR(main_rbswap_hk);

	s->enable = main_rbswap_on != 0;
	main_rbswap_st.installed = true;
	main_rbswap_st.m_seq = s->m_seq;
	fenykl_log_info(&main_rbswap_log, "R/B swap %s", main_rbswap_on ? "on" : "off");
}

/*
 * Logs the input matrix of the last swapped still pass (a copy torn by a new pass waits a tick) and
 * the first CPU1 pass (passed through unchanged).
 */
void main_rbswap_tick(void)
{
	volatile struct main_rbswap_hook_state *s = FENYKL_SHARED_PTR(main_rbswap_hk);
	struct main_rbswap_state *st = &main_rbswap_st;
	uint16_t m[FENYKL_IMG_MAT_N];
	uint32_t seq = s->m_seq, mode = s->m_mode;

	if (!st->installed)
		return;
	if (s->cpu1 && !st->cpu1_seen) {
		st->cpu1_seen = true;
		fenykl_log_err(&main_rbswap_log, "img_exec ran on CPU1 (%u passes, not swapped)",
			       s->cpu1);
	}
	if (seq == st->m_seq)
		return;
	for (unsigned int i = 0; i < FENYKL_IMG_MAT_N; i++)
		m[i] = (uint16_t)s->m[i];
	if (s->m_seq != seq)
		return;
	fenykl_log_info(&main_rbswap_log, "still ucMode %02x: %u swapped passes, input M", mode,
			seq - st->m_seq);
	for (unsigned int r = 0; r < 3; r++)
		fenykl_log_info(&main_rbswap_log, "  M%u %04x %04x %04x", r, m[3 * r], m[3 * r + 1],
				m[3 * r + 2]);
	st->m_seq = seq;
}

void main_rbswap_status(void)
{
	const volatile struct main_rbswap_hook_state *s = FENYKL_SHARED_PTR(main_rbswap_hk);

	if (!main_rbswap_st.installed)
		return;
	fenykl_log_info(&main_rbswap_log, "rbswap %s: cls %u/%u/%u", s->enable ? "on" : "off",
			s->cls[0], s->cls[1], s->cls[2]);
	fenykl_log_info(&main_rbswap_log, "  cls3 %u e1 %u swapped e1 %u e2 %u", s->cls[3], s->eng1,
			s->swaps[0], s->swaps[1]);
	fenykl_log_info(&main_rbswap_log, "  identity %u no_param %u kept %u cpu1 %u", s->identity,
			s->no_param, s->kept, s->cpu1);
}
#endif
