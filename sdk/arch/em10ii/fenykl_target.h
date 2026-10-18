/* E-M10 II part of fenykl.h (included from there). FENYKL_LOAD_BASE and FENYKL_REGION_END come from
   target.toml via the build. Plugin code is started on CPU0 only (no cpu1 capability); hook wrappers
   run on the core of the firmware caller. */
#ifndef FENYKL_TARGET_H
#define FENYKL_TARGET_H

/* Uncached alias of a DRAM address, the rule of to_uncached 0x6007F686: for buffers handed to file or
   DMA code. */
#define FENYKL_UNCACHED(p) ((void *)(((uint32_t)(p) & 0x3FFFFFFFu) | 0x80000000u))

/* FENYKL_TASK(fn) declares `void fn(void)` and defines a task entry `fn_task` to pass to
   fw_OlyTaskCreate. The kernel enters a task with `calls (a2)`, D0 = exinf and SP 4 below the stack
   top (0x60032682..0x600326A1, 0x600329D6..0x600329E3); the entry ignores D0, gives the GCC function
   a stack frame of its own and, when fn returns, sleeps forever (the task is not deleted). */
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

/* Priority of the library tasks (log flusher, cfg saver): PER_WDT's 55. The CPU0 tick ISR 0x600702A4
   rotates the priority-55 ready queue every 5 ticks (irot_rdq(55) at 0x600702D0), so a busy library
   task cannot starve the watchdog kicker; any priority below 55 could. */
#define FENYKL_LIB_TASK_PRIO 55

/* Writes the D-cache back to DRAM and invalidates it, as the firmware's callers of dcache_flush_all do
   (0x60073886..0x60073897): there is no IRQ-safe wrapper on this body. Task context, CPU0. */
static inline void fenykl_dcache_flush(void)
{
	unsigned int cookie = fw_irq_lock(0);

	fw_dcache_flush_all();
	fw_irq_unlock(cookie);
}

/* No vtable clones on this target yet: fenykl.h's prototypes only. */
struct fenykl_vtable;

/* Descriptor of a slot (fenykl_slot_<name>, generated from firmware.toml by fwapi.slot_asm).
   Replaced with FENYKL_SLOT_REPLACE, restored with fenykl_slot_restore (slots.c). */
struct fenykl_slot {
	uint32_t address;  /* the pointer word the firmware calls through */
	uint32_t expected; /* its stock value, also the original to call (0 = empty slot) */
	uint32_t current;  /* the replacement while replaced, else 0 */
};

/* Hooks (hooks.c): fenykl_hook_install/remove patch from CPU0 and invalidate CPU0's I-cache only; a
   hook whose target CPU1/CPU2 may run is safe only under the rules in sdk/README.md (Hooks). */
/* On FENYKL_HOOK_EVERIFY and FENYKL_HOOK_EICACHE the old bytes are written back. */
#define FENYKL_HOOK_EICACHE (-3) /* the I-cache invalidate timed out */

/* I-cache invalidate of the executing core (hooks.c), with interrupts masked: CHCTR ICEN off, wait
   for !ICBUSY, ICINV, wait for !ICBUSY, entry value back; explicit values with the INV bits clear
   except the one ICINV write. Both waits are bounded; ICBUSY (bit 2) is inferred, no stock code of
   this body reads it. 0 or FENYKL_ICACHE_E*. Task context. */
#define FENYKL_ICACHE_EBUSY (-1)    /* busy before ICINV: nothing invalidated */
#define FENYKL_ICACHE_ETIMEOUT (-2) /* still busy after ICINV */
int fenykl_icache_invalidate(void);

struct fenykl_icache_stats {
	uint32_t runs;
	uint32_t timeouts;  /* runs that returned FENYKL_ICACHE_E* */
	uint32_t polls_max; /* most busy polls seen in one wait */
	uint32_t polls[2];  /* last run: busy polls before and after ICINV */
	uint16_t chctr[4];  /* last run: CHCTR at entry, ICEN off, after ICINV, at exit */
};
void fenykl_icache_get_stats(struct fenykl_icache_stats *out);

/* Core id of the executing core: 0 = CPU0, 1 = CPU1, 2 = CPU2. */
#define FENYKL_CORE_ID() (*(volatile uint32_t *)FW_CORE_ID)

/* Data other cores may read (lib/log_rec.c's CPU1 ring, empty here): whole 64-byte lines, accessed only
   through FENYKL_SHARED_PTR, the uncached alias. */
#define FENYKL_SHARED __attribute__((section(".shared"), aligned(64)))
#define FENYKL_SHARED_PTR(var) ((volatile __typeof__(var) *)FENYKL_UNCACHED(&(var)))

#endif
