/* The worker of test_cpu1.c, a cpu1_sources file (FENYKL_CPU1). */
#include <fenykl.h>

volatile uint32_t worker_arg, worker_runs, worker_stop_seen;

FENYKL_CPU1_WORKER(simworker);
void simworker(void *arg)
{
	worker_arg = (uint32_t)arg;
	worker_stop_seen = fenykl_cpu1_stop_requested();
	worker_runs++;
}
