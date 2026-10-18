/* E-M10 II I-cache probe: proves fenykl_icache_invalidate (arch/em10ii/hooks.c) on CPU0 with a tiny
   function in payload RAM; patches no firmware code. Each round writes the function as the hook
   runtime writes an entry (D-cache write-back, uncached stores, write-back), calls it, rewrites it,
   calls it without and then with an I-cache invalidate. Logs to C:\FENYKL.LOG. */
#include <fenykl.h>
#include <log.h>

#define ICP_ROUNDS 8u
#define ICP_CODE_SIZE 8u
#define ICP_FLUSH_TRIES 50u

static FENYKL_LOG_MODULE(icp_log, "icprobe", FENYKL_LOG_INFO);

/* Its own 64-byte block: no other code or data shares its cache lines. */
static uint8_t icp_code[64] __attribute__((aligned(64)));

typedef uint32_t (*icp_fn)(void);

static struct {
	uint32_t fails;
	uint32_t stale; /* rounds whose call without an invalidate returned the old value */
} icp;

/* `mov value,d0; rets` (fc cc imm32 f0 fc), GCC's encoding of a leaf returning a constant. Returns 1
   if DRAM holds it afterwards. */
static int icp_write(uint32_t value)
{
	const uint8_t code[ICP_CODE_SIZE] = { 0xfc, 0xcc, value, value >> 8, value >> 16, value >> 24,
					      0xf0, 0xfc };
	volatile uint8_t *p = FENYKL_UNCACHED(icp_code);
	int same = 1;

	fenykl_dcache_flush();
	for (unsigned int i = 0; i < ICP_CODE_SIZE; i++)
		p[i] = code[i];
	fenykl_dcache_flush();
	for (unsigned int i = 0; i < ICP_CODE_SIZE; i++)
		same &= p[i] == code[i];
	return same;
}

static uint32_t icp_call(void)
{
	return ((icp_fn)(void *)icp_code)();
}

static void icp_check(const char *what, uint32_t got, uint32_t want)
{
	if (got == want)
		return;
	icp.fails++;
	fenykl_log_err(&icp_log, "FAIL %s: got %08x want %08x", what, got, want);
}

static void icp_log_invalidate(unsigned int round, int result)
{
	struct fenykl_icache_stats st;

	fenykl_icache_get_stats(&st);
	fenykl_log_info(&icp_log, "r%u inv %d polls %u/%u", round, result, st.polls[0], st.polls[1]);
	/* entry, ICEN off | after ICINV, exit */
	fenykl_log_info(&icp_log, "r%u chctr %08x %08x", round, st.chctr[0] << 16 | st.chctr[1],
			st.chctr[2] << 16 | st.chctr[3]);
	if (result) {
		icp.fails++;
		fenykl_log_err(&icp_log, "FAIL r%u invalidate %d", round, result);
	}
}

static void icp_round(unsigned int round)
{
	uint32_t a = 0x1C000A00u | round, b = 0x1C000B00u | round;
	uint32_t got;

	icp_check("dram a", icp_write(a), 1);
	icp_log_invalidate(round, fenykl_icache_invalidate());
	got = icp_call();
	icp_check("call a", got, a);

	icp_check("dram b", icp_write(b), 1);
	got = icp_call();
	if (got == a)
		icp.stale++;
	else
		icp_check("call b, no invalidate", got, b);
	fenykl_log_info(&icp_log, "r%u no invalidate: %08x (%s)", round, got,
			(uint32_t)(got == a ? "stale" : got == b ? "fresh" : "other"));

	icp_log_invalidate(round, fenykl_icache_invalidate());
	got = icp_call();
	icp_check("call b, invalidated", got, b);
	fenykl_log_info(&icp_log, "r%u invalidated: %08x", round, got);
}

static void icp_final_flush(void)
{
	for (unsigned int i = 0; i < ICP_FLUSH_TRIES; i++) {
		if (fenykl_log_flush() == 0)
			return;
		fw_OlyTaskSleep(200);
	}
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_icache_stats st;
	int init = fenykl_log_init(0);
	int task = fenykl_log_task_start();

	(void)load_base;
	(void)image_size;
	fenykl_log_info(&icp_log, "icprobe E-M10II: log init %d task %d core %u", init, task,
			FENYKL_CORE_ID());
	fenykl_log_info(&icp_log, "chctr at entry %04x, code at %08x", *(volatile uint16_t *)FW_CHCTR,
			(uint32_t)icp_code);

	/* The block was never executed in this boot: the first call must see the new bytes. */
	icp_check("dram first", icp_write(0x1C000F00u), 1);
	icp_check("first call", icp_call(), 0x1C000F00u);

	for (unsigned int round = 1; round <= ICP_ROUNDS; round++)
		icp_round(round);

	fenykl_icache_get_stats(&st);
	fenykl_log_info(&icp_log, "invalidates %u timeouts %u polls max %u", st.runs, st.timeouts,
			st.polls_max);
	fenykl_log_info(&icp_log, "icprobe %s: fails %u, stale without invalidate %u of %u",
			(uint32_t)(icp.fails ? "FAIL" : "PASS"), icp.fails, icp.stale, ICP_ROUNDS);
	icp_final_flush();
}
