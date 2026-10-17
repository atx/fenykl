/* The API contract of the CPU1 worker runtime (sdk/targets/epl3/cpu1.c) in the GDB simulator: start,
   state, stop with its timeout, restart and the refusals, against a fake TCB of task 0x1002 and the
   mocks in fw.S. plugin_main sets sim_exit_code to 0 (pass) or 1 + the number of the
   lowest failed check. */
#include <fenykl.h>

#define TCB 0xA07F0474u
#define TABLE_1002 (*(volatile uint32_t *)FENYKL_UNCACHED(0x6E690A54u))
#define T16(off) (*(volatile uint16_t *)(TCB + (off)))
#define T32(off) (*(volatile uint32_t *)(TCB + (off)))
#define TCB_BYTES 0x60

volatile uint32_t mock_args[8];
uint32_t sim_exit_code = 99;
static uint32_t fail;

extern volatile uint32_t worker_arg, worker_runs, worker_stop_seen;
void simworker_cpu1(void);
void sim_enter_task(uint32_t entry, uint32_t arg);

#define CHECK(n, cond)                                                                      \
	do {                                                                                \
		if (!(cond))                                                                \
			fail |= 1u << (n);                                                  \
	} while (0)

static int stop_flag(void)
{
	return FENYKL_SHARED_PTR(fenykl_cpu1_ctl)->stop;
}

/* The stock SCHEAP_DEBUG TCB, other bytes filled with a pattern. */
static void stock_tcb(void)
{
	for (unsigned i = 0; i < TCB_BYTES; i += 4)
		T32(i) = 0xEE000000u | i;
	TABLE_1002 = TCB;
	T16(0x10) = 0x1002;
	T16(0xC) = 0x10;
	T32(0x48) = 0;
	T32(0x4C) = 0x6E70D51Au;
	T16(0x50) = 12;
	T32(0x54) = 0x800;
	T32(0x58) = 0xA07F1000u;
	for (int i = 0; i < 8; i++)
		mock_args[i] = 0;
}

/* fenykl_cpu1_start returns `want` and writes nothing. */
static void refused(int check, int want)
{
	uint32_t before[TCB_BYTES / 4], starts = mock_args[0], purges = mock_args[7];

	for (unsigned i = 0; i < TCB_BYTES / 4; i++)
		before[i] = T32(4 * i);
	CHECK(check, fenykl_cpu1_start(simworker_cpu1, (void *)0x1234) == want);
	for (unsigned i = 0; i < TCB_BYTES / 4; i++)
		CHECK(check, T32(4 * i) == before[i]);
	CHECK(check, mock_args[0] == starts && mock_args[7] == purges);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;

	/* 1: not the stock TCB of task 0x1002 */
	stock_tcb();
	TABLE_1002 = 0x6E690000u; /* not an uncached TCB pointer */
	refused(1, FENYKL_CPU1_ETCB);
	CHECK(1, fenykl_cpu1_state() == FENYKL_CPU1_ETCB);
	CHECK(1, fenykl_cpu1_stop(10) == FENYKL_CPU1_ETCB && mock_args[2] == 0);

	/* 2: the stock task is not dormant */
	stock_tcb();
	T16(0xC) = 4;
	refused(2, FENYKL_CPU1_EBUSY);

	/* 3: borrow the stock TCB: D-cache purged, OlyTaskStart(0x1002), stop flag cleared */
	stock_tcb();
	FENYKL_SHARED_PTR(fenykl_cpu1_ctl)->stop = 1;
	CHECK(3, fenykl_cpu1_start(simworker_cpu1, (void *)0xC0FFEE00u) == 0);
	CHECK(3, mock_args[0] == 1 && mock_args[1] == 0x1002 && mock_args[7] == 1);
	CHECK(3, stop_flag() == 0);

	/* 4: the entry FENYKL_CPU1_WORKER made runs as the kernel enters it, with the argument */
	sim_enter_task(T32(0x4C), T32(0x48));
	CHECK(4, worker_runs == 1 && worker_arg == 0xC0FFEE00u && worker_stop_seen == 0);

	/* 5: state */
	T16(0xC) = 4;
	CHECK(5, fenykl_cpu1_state() == 4);

	/* 6: stop: the worker returns after 3 sleeps and sees the stop request */
	mock_args[6] = 3;
	CHECK(6, fenykl_cpu1_stop(100) == 0 && mock_args[5] == 3 && mock_args[2] == 0);
	CHECK(6, stop_flag() == 1 && fenykl_cpu1_state() == 0x10);
	sim_enter_task(T32(0x4C), T32(0x48));
	CHECK(6, worker_stop_seen == 1);

	/* 7: restart: the borrowed TCB is accepted again */
	CHECK(7, fenykl_cpu1_start(simworker_cpu1, (void *)0x77) == 0);
	CHECK(7, mock_args[0] == 2 && T32(0x48) == 0x77 && stop_flag() == 0);

	/* 8: not dormant */
	T16(0xC) = 1;
	CHECK(8, fenykl_cpu1_start(simworker_cpu1, (void *)0x88) == FENYKL_CPU1_EBUSY);
	CHECK(8, mock_args[0] == 2 && T32(0x48) == 0x77);

	/* 9: the worker does not return: OlyTaskStop after the timeout */
	mock_args[4] = 1;
	mock_args[5] = 0;
	CHECK(9, fenykl_cpu1_stop(5) == FENYKL_CPU1_ETIMEOUT);
	CHECK(9, mock_args[5] == 5 && mock_args[2] == 1 && mock_args[3] == 0x1002);

	/* 10: not even OlyTaskStop ends it */
	T16(0xC) = 4;
	mock_args[4] = 0;
	CHECK(10, fenykl_cpu1_stop(2) == FENYKL_CPU1_EBUSY && mock_args[2] == 2);

	/* 11: the borrowed TCB with another stack is no longer ours */
	T16(0xC) = 0x10;
	T32(0x58) += 64;
	refused(11, FENYKL_CPU1_ETCB);

	sim_exit_code = fail ? 1 + __builtin_ctz(fail) : 0;
}
