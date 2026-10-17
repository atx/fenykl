/*
 * The CPU1 producers of test_log.c (a cpu1_sources file, so the plugin has ring 1). The test
 * runs them with the core id at 0x8050 set to 1.
 */
#include <fenykl.h>
#include "log.h"

extern struct fenykl_log_module sim_mod;
static unsigned int sim_spsc_next;

void sim_burst1(unsigned int i)
{
	fenykl_log_info(&sim_mod, "burst1 %u", i);
}

void sim_spsc(unsigned int i)
{
	(void)i;
	fenykl_log_info(&sim_mod, "spsc %u", sim_spsc_next++);
}
