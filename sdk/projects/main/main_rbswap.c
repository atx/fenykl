/*
 * main: option "R/B swap". The task side is main_rbswap_wb.c, which follows the option from its
 * tick.
 */
#include "main.h"
#include "main_rbswap.h"

#if MAIN_RBSWAP
FENYKL_LOG_MODULE(main_rbswap_log, "rbswap", FENYKL_LOG_INFO);

static void main_rbswap_apply(const struct fenykl_cfg_opt *o, int32_t old);

FENYKL_CFG_GROUP(main_grp_image, "Image", 3);
FENYKL_CFG_BOOL(main_rbswap_on, "main.rbswap.on", "R/B swap", &main_grp_image, 0,
		.on_change = main_rbswap_apply);

/* Any task (the cfg task, the settings tab): the E-M10 II's registry work waits for the tick. */
static void main_rbswap_apply(const struct fenykl_cfg_opt *o, int32_t old)
{
	(void)o;
	(void)old;
	fenykl_log_info(&main_rbswap_log, "R/B swap %s", main_rbswap_on ? "on" : "off");
}

void main_rbswap_set_level(unsigned int level)
{
	fenykl_log_set_level(&main_rbswap_log, level);
}
#endif

