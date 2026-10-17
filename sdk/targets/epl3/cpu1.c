/* CPU1 worker runtime for the E-PL3: borrows
   the dormant CPU1 task 0x1002 SCHEAP_DEBUG, whose id nothing in the firmware references. Runs on
   CPU0; the worker itself only reads the shared stop flag (fenykl_cpu1_stop_requested). */
#include <fenykl.h>

#define TASK_ID 0x1002u
/* k_tcb_table_cpu1 (KCB1+0xCC): TCB pointers by (id & 0xFFF) - 1, the TCBs are in uncached DRAM. */
#define TCB_TABLE 0x6E690A50u
#define STOCK_ENTRY 0x6E70D51Au /* static record 9 at 0x6E690420 */
#define STOCK_PRIORITY 12u
#define STOCK_STACK_SIZE 0x800u
#define WORKER_PRIORITY 15u /* above BOOT_CPU1's idle loop (16), below every firmware task */
#define STACK_SIZE 0x2000u

/* TCB fields read by k_tsk_make_ready 0x6E684E12 at every activation. */
#define TCB_STATE 0x0C /* u16 */
#define TCB_ID 0x10    /* u16 */
#define TCB_EXINF 0x48 /* D0 of the entry */
#define TCB_ENTRY 0x4C
#define TCB_PRIORITY 0x50 /* u16 */
#define TCB_STACK_SIZE 0x54
#define TCB_STACK_BASE 0x58

#define U16(tcb, off) (*(volatile uint16_t *)((tcb) + (off)))
#define U32(tcb, off) (*(volatile uint32_t *)((tcb) + (off)))

FENYKL_SHARED struct fenykl_cpu1_ctl fenykl_cpu1_ctl;

/* CPU1-private once started: whole cache lines, so no CPU0 data shares a line with it. */
static uint8_t stack[STACK_SIZE] __attribute__((aligned(64)));
static uint32_t borrowed_entry; /* the entry fenykl_cpu1_start wrote, 0 before */

/* Task 0x1002's TCB if it is the stock one or the one this runtime filled, else 0. */
static uint8_t *worker_tcb(void)
{
	uint32_t tcb = *(volatile uint32_t *)FENYKL_UNCACHED(TCB_TABLE + 4 * ((TASK_ID & 0xFFF) - 1));

	if ((tcb & 0xC0000003u) != 0x80000000u) /* a word-aligned uncached alias */
		return 0;
	uint8_t *t = (uint8_t *)tcb;
	if (U16(t, TCB_ID) != TASK_ID)
		return 0;
	uint32_t entry = U32(t, TCB_ENTRY), priority = U16(t, TCB_PRIORITY);
	uint32_t size = U32(t, TCB_STACK_SIZE);
	if (entry == STOCK_ENTRY && priority == STOCK_PRIORITY && size == STOCK_STACK_SIZE)
		return t;
	if (borrowed_entry && entry == borrowed_entry && priority == WORKER_PRIORITY &&
	    size == STACK_SIZE && U32(t, TCB_STACK_BASE) == (uint32_t)stack)
		return t;
	return 0;
}

int fenykl_cpu1_start(void (*entry)(void), void *arg)
{
	uint8_t *t = worker_tcb();

	if (!t)
		return FENYKL_CPU1_ETCB;
	if (U16(t, TCB_STATE) != FENYKL_CPU1_DORMANT)
		return FENYKL_CPU1_EBUSY;
	FENYKL_SHARED_PTR(fenykl_cpu1_ctl)->stop = 0;
	/* No dirty CPU0 line of the stack (from the .bss clear) may be written back over CPU1's frames. */
	fw_dcache_purge_all_irqsafe();
	U32(t, TCB_ENTRY) = (uint32_t)entry;
	U32(t, TCB_EXINF) = (uint32_t)arg;
	U16(t, TCB_PRIORITY) = WORKER_PRIORITY;
	U32(t, TCB_STACK_BASE) = (uint32_t)stack;
	U32(t, TCB_STACK_SIZE) = STACK_SIZE;
	borrowed_entry = (uint32_t)entry;
	fw_OlyTaskStart(TASK_ID); /* returns 0 for CPU1 ids once CPU1 took the request */
	return 0;
}

int fenykl_cpu1_state(void)
{
	uint8_t *t = worker_tcb();

	return t ? U16(t, TCB_STATE) : FENYKL_CPU1_ETCB;
}

int fenykl_cpu1_stop(int timeout_ticks)
{
	uint8_t *t = worker_tcb();

	if (!t)
		return FENYKL_CPU1_ETCB;
	FENYKL_SHARED_PTR(fenykl_cpu1_ctl)->stop = 1;
	for (int i = 0; U16(t, TCB_STATE) != FENYKL_CPU1_DORMANT; i++) {
		if (i >= timeout_ticks) {
			fw_OlyTaskStop(TASK_ID);
			return U16(t, TCB_STATE) == FENYKL_CPU1_DORMANT ? FENYKL_CPU1_ETIMEOUT
									: FENYKL_CPU1_EBUSY;
		}
		fw_OlyTaskSleep(1);
	}
	return 0;
}
