/* E-PL3 part of fenykl.h (included from there). FENYKL_LOAD_BASE and FENYKL_REGION_END come from
   target.toml via the build. */
#ifndef FENYKL_TARGET_H
#define FENYKL_TARGET_H

/* Uncached alias of a DRAM address: for buffers handed to file/DMA code. */
#define FENYKL_UNCACHED(p) ((void *)(((uint32_t)(p) & 0x3FFFFFFFu) | 0x80000000u))

/* FENYKL_TASK(fn) declares `void fn(void)` and defines a task entry `fn_task` to pass to
   fw_OlyTaskCreate. The entry gives the GCC function a stack frame of its own, ignores whatever the
   kernel passes in registers and, when fn returns, sleeps forever (the task is not deleted). */
#define FENYKL_TASK(fn)                                                                   \
	void fn(void);                                                                    \
	void fn##_task(void);                                                             \
	__asm__("\t.section .text.fenykl_task_" #fn ",\"ax\"\n"                           \
		"\t.global _" #fn "_task\n"                                               \
		"_" #fn "_task:\n"                                                        \
		"\tadd -12,sp\n"                                                          \
		"\tcall _" #fn ",[],0\n"                                                  \
		"1:\tmov 1000,d0\n"                                                       \
		"\tcall _fw_OlyTaskSleep,[],0\n"                                          \
		"\tbra 1b\n"                                                              \
		"\t.previous\n")

/* Priority of the library tasks (log flusher, cfg saver). */
#define FENYKL_LIB_TASK_PRIO 12

/* Writes the D-cache back to DRAM and invalidates it. */
static inline void fenykl_dcache_flush(void)
{
	fw_dcache_purge_all_irqsafe();
}

#define FENYKL_TASK_ETIMEOUT (-50)

static inline uint32_t fenykl_irq_save(void)
{
	uint32_t psw = fw_psw_get();

	fw_irq_disable_all();
	return psw;
}

static inline void fenykl_irq_restore(uint32_t psw)
{
	fw_irq_mask_restore(psw);
}

static inline int fenykl_task_create(const char *name, void (*entry)(void), int prio,
				     uint32_t stack_bytes)
{
	void *stack = fw_malloc(stack_bytes);

	if (!stack)
		return -1;
	return fw_OlyTaskCreate(name, entry, 2, 0, prio, stack_bytes, stack);
}

static inline int fenykl_task_wait(int ticks)
{
	return fw_tslp_tsk(ticks);
}

static inline int fenykl_task_wake(int id)
{
	return fw_OlyTaskWakeup(id);
}

/* Descriptor of a slot (fenykl_slot_<name>, generated from firmware.toml by fwapi.slot_asm).
   Replaced with FENYKL_SLOT_REPLACE, restored with fenykl_slot_restore (lib/slots.c). */
struct fenykl_slot {
	uint32_t address;  /* the pointer word the firmware calls through */
	uint32_t expected; /* its stock value, also the original to call (0 = empty slot) */
	uint32_t current;  /* the replacement while replaced, else 0 */
};


/* --- CPU1 (README, CPU1) --- */

/* Core id of the executing core: 0 = CPU0, 1 = CPU1. */
#define FENYKL_CORE_ID() (*(volatile uint32_t *)FW_CORE_ID)

/* Data both cores use: in .shared (whole 64-byte lines, zeroed by the loader). Access it only through
   FENYKL_SHARED_PTR(var), the uncached alias, on both cores: there is no cache coherency. */
#define FENYKL_SHARED __attribute__((section(".shared"), aligned(64)))
#define FENYKL_SHARED_PTR(var) ((volatile __typeof__(var) *)FENYKL_UNCACHED(&(var)))

#ifdef FENYKL_CPU1
#define FENYKL_CPU1_TU 1
#else
#define FENYKL_CPU1_TU 0
#endif

/* FENYKL_CPU1_WORKER(fn), in a cpu1_sources file, declares `void fn(void *arg)` and defines the task
   entry `fn_cpu1` to pass to fenykl_cpu1_start. The kernel enters it with `calls` and D0 = arg; when fn
   returns, the task ends and is dormant again. fn runs at priority 15 and MUST block (one
   fw_OlyTaskSleep(1) per work chunk) at least every ~10 ms: CPU1's idle loop, the only server of
   CPU0's task start/stop requests, runs only while it blocks. fn calls CPU1-safe functions only (the
   build enforces it), shares data only through FENYKL_SHARED_PTR, does no file I/O, and returns once
   fenykl_cpu1_stop_requested(). */
#define FENYKL_CPU1_WORKER(fn)                                                            \
	_Static_assert(FENYKL_CPU1_TU, "FENYKL_CPU1_WORKER(" #fn "): list its file in "       \
				       "plugin.toml cpu1_sources");                           \
	void fn(void *arg);                                                               \
	void fn##_cpu1(void);                                                             \
	__asm__("\t.section .text.fenykl_cpu1_" #fn ",\"ax\"\n"                           \
		"\t.global _" #fn "_cpu1\n"                                               \
		"_" #fn "_cpu1:\n"                                                        \
		"\tadd -12,sp\n"                                                          \
		"\tcall _" #fn ",[],0\n"                                                  \
		"\tret [],12\n"                                                           \
		"\t.previous\n")

#define FENYKL_CPU1_ETCB (-1)     /* task 0x1002's TCB is not the stock or our borrowed one: nothing written */
#define FENYKL_CPU1_EBUSY (-2)    /* start: the task is not dormant; stop: still not dormant after OlyTaskStop */
#define FENYKL_CPU1_ETIMEOUT (-3) /* stop: the worker did not return in time, OlyTaskStop ended it */

/* CPU0 side (cpu1.c). Call them from a task of priority 12 or lower (higher number), never from
   plugin_main: OlyTaskStart/Stop spin on CPU0 until CPU1's idle loop serves the mailbox.
   fenykl_cpu1_start borrows the dormant CPU1 task 0x1002 (SCHEAP_DEBUG) after checking its TCB, gives
   it `entry` (FENYKL_CPU1_WORKER's fn_cpu1), `arg`, priority 15 and the runtime's 8 KiB stack, and
   starts it: 0 or FENYKL_CPU1_E*.
   fenykl_cpu1_stop sets the stop flag, waits up to timeout_ticks for the worker to return, else ends
   it with OlyTaskStop: 0 (returned) or FENYKL_CPU1_E*. */
int fenykl_cpu1_start(void (*entry)(void), void *arg) FENYKL_CPU0_ONLY("fenykl_cpu1_start");
int fenykl_cpu1_stop(int timeout_ticks) FENYKL_CPU0_ONLY("fenykl_cpu1_stop");

/* CPU1 side: the worker polls this between work chunks and returns when it is set. */
struct fenykl_cpu1_ctl {
	uint32_t stop;
};
extern struct fenykl_cpu1_ctl fenykl_cpu1_ctl;
static inline int fenykl_cpu1_stop_requested(void)
{
	return FENYKL_SHARED_PTR(fenykl_cpu1_ctl)->stop != 0;
}

#endif
