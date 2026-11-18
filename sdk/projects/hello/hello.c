/* First visible proof that the loader works: writes the arguments the loader passed and the FENYKL
   task's stack use to C:\FENYKL.LOG through the log library (card gate, append, flusher task), then
   returns (the task ends). */
#include <fenykl.h>

#define STACK_SIZE 0x4000u /* FENYKL task stack above FENYKL_REGION_END, 0xDEADBEEF-filled by the kernel */

static unsigned stack_untouched(void)
{
	const uint32_t *p = (const uint32_t *)FENYKL_REGION_END;
	while (p < (const uint32_t *)(FENYKL_REGION_END + STACK_SIZE) && *p == 0xDEADBEEFu)
		p++;
	return (uint32_t)p - FENYKL_REGION_END;
}

#include <log.h>

#define HELLO_WAIT_TICKS 5000 /* the flusher writes the first lines meanwhile */

static FENYKL_LOG_MODULE(hello_mod, "hello", FENYKL_LOG_INFO);

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;

	fenykl_log_info(&hello_mod, "fenykl hello E-M10II: load base %08x, image size %u, core %u",
			load_base, image_size, FENYKL_CORE_ID());
	fenykl_log_info(&hello_mod, "log init %d, flusher task %d", init, task);
	fw_OlyTaskSleep(HELLO_WAIT_TICKS);
	fenykl_log_get_stats(&st);
	fenykl_log_info(&hello_mod, "flushes %u, bytes %u, open fails %u, gate skipped %u", st.flushes,
			st.bytes, st.open_fail, st.gate_skipped);
	fenykl_log_info(&hello_mod, "stack untouched %u of %u", stack_untouched(), STACK_SIZE);
}
