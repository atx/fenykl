/*
 * macbench: multiply and int8 dot-product throughput on both cores. A CPU0 control task
 * runs every stage on CPU0, then borrows CPU1 task 0x1002 and has the worker (bench.c) run the
 * same stages there, one per command. Before every stage it logs a marker and flushes the log, so
 * a hang or reset shows which stage caused it. Log: C:\FENYKL.LOG, modules "mbc0"/"mbc1".
 */
#include "log.h"
#include "macbench.h"

#define MB_STACK_SIZE 0x2000u
#define MB_START_DELAY 3000	/* ticks after boot */
#define MB_STAGE_TIMEOUT 20000u	/* ms */
#define MB_STOP_TIMEOUT 1000	/* ticks */
#define MB_MHZ 162u		/* assumed core clock */

struct mb_state {
	struct mb_row rows[MB_ROWS_MAX];
	uint32_t empty_us, empty_ops;	/* the running core's empty block loop */
	uint32_t bad[2];
	uint32_t checked[2];
};

static struct mb_state mb;

static FENYKL_LOG_MODULE(mb_log0, "mbc0", FENYKL_LOG_INFO);
static FENYKL_LOG_MODULE(mb_log1, "mbc1", FENYKL_LOG_INFO);

static struct fenykl_log_module *mb_mod(unsigned int core)
{
	return core ? &mb_log1 : &mb_log0;
}

/* Puts everything queued on the card before the caller goes on. */
static void mb_flush(void)
{
	for (int i = 0; i < 200; i++) {
		int r = fenykl_log_flush();

		if (r != FENYKL_LOG_EBUSY && r != FENYKL_LOG_ENOCARD)
			return;
		fw_OlyTaskSleep(10);
	}
}

/* Cycles x100 per op at MB_MHZ, the empty block loop subtracted, the mov of a pair too. */
static int32_t mb_op_cyc100(const struct mb_row *r)
{
	int32_t net = (int32_t)r->us - (int32_t)(mb.empty_us * r->ops / mb.empty_ops);
	int32_t cyc = net * (int32_t)(MB_MHZ * 100) / (int32_t)r->ops;

	return r->kind == MB_K_PAIR ? cyc - 100 : cyc;
}

static void mb_log_row(unsigned int core, const struct mb_row *r)
{
	struct fenykl_log_module *mod = mb_mod(core);

	switch (r->kind) {
	case MB_K_EMPTY:
		mb.empty_us = r->us;
		mb.empty_ops = r->ops;
		fenykl_log_info(mod, "%s: %u us / %u ops", r->name, r->us, r->ops);
		break;
	case MB_K_OP:
	case MB_K_PAIR:
		fenykl_log_info(mod, "%s: %u us / %u ops = %d cyc x100", r->name, r->us, r->ops,
				mb_op_cyc100(r));
		break;
	case MB_K_PROBE:
		fenykl_log_info(mod, "%s: Dn %x MDR %x (64-bit high %x)", r->name, r->got, r->got2,
				r->want2);
		break;
	case MB_K_KERNEL:
		fenykl_log_info(mod, "%s: %u us, %u cyc/MAC x100, %u MMAC/s x100", r->name, r->us,
				r->us * MB_MHZ * 100 / r->ops, r->us ? r->ops * 100 / r->us : 0);
		break;
	}
	if (!r->check)
		return;
	mb.checked[core]++;
	/* A probe's MDR is information (udf semantics), shown in its line. */
	if (r->got == r->want && (r->kind == MB_K_PROBE || r->got2 == r->want2))
		return;
	mb.bad[core]++;
	fenykl_log_err(mod, "%s: BAD got %x want %x", r->name, r->got, r->want);
	if (r->kind != MB_K_PROBE && r->got2 != r->want2)
		fenykl_log_err(mod, "%s: BAD lane 2 got %x want %x", r->name, r->got2, r->want2);
}

static void mb_log_stage(unsigned int core, unsigned int stage, const struct mb_row *rows,
			 unsigned int n)
{
	for (unsigned int i = 0; i < n; i++)
		mb_log_row(core, &rows[i]);
	if (stage == MB_ST_CALIB && rows[1].us > mb.empty_us)
		fenykl_log_info(mb_mod(core), "clock: %u kHz if add is 1 cycle",
				rows[1].ops * 1000 / (rows[1].us - mb.empty_us * rows[1].ops / mb.empty_ops));
	fenykl_log_info(mb_mod(core), "stage %s done: %u rows, %u checked, %u BAD so far",
			mb_stage_name(stage), n, mb.checked[core], mb.bad[core]);
}

static void mb_marker(unsigned int core, unsigned int stage)
{
	fenykl_log_info(mb_mod(core), "stage %s next", mb_stage_name(stage));
	mb_flush();
}

/* The CPU1 pass: 0, or <0 if the worker failed to start or answer. */
static int mb_cpu1_pass(void)
{
	volatile struct mb_shared *sh = FENYKL_SHARED_PTR(mb_sh);
	int r = fenykl_cpu1_start(mb_worker_cpu1, 0);

	fenykl_log_info(&mb_log1, "start %d (expect 0)", r);
	if (r)
		return r;
	for (unsigned int s = 0; s < MB_STAGES; s++) {
		uint32_t waited = 0;

		mb_marker(1, s);
		sh->cmd = s + 1;
		while (sh->done != s + 1) {
			if (waited >= MB_STAGE_TIMEOUT) {
				fenykl_log_err(&mb_log1, "stage %s: no answer after %u ms, worker core %u",
					       mb_stage_name(s), waited, sh->core);
				return -1;
			}
			fw_OlyTaskSleep(10);
			waited += 10;
		}
		unsigned int n = sh->count;
		for (unsigned int i = 0; i < n && i < MB_ROWS_MAX; i++)
			mb.rows[i] = *(const struct mb_row *)&sh->rows[i];
		mb_log_stage(1, s, mb.rows, n);
	}
	r = fenykl_cpu1_stop(MB_STOP_TIMEOUT);
	fenykl_log_info(&mb_log1, "stop %d (expect 0), state %x (expect 10), worker core %u", r,
			fenykl_cpu1_state(), sh->core);
	return 0;
}

FENYKL_TASK(mb_control);
void mb_control(void)
{
	fw_OlyTaskSleep(MB_START_DELAY);
	uint32_t t0 = FENYKL_LOG_TIMER_NOW();
	fenykl_log_info(&mb_log0, "macbench: all stages on CPU0, then CPU1; %u MHz assumed",
			MB_MHZ);
	for (unsigned int s = 0; s < MB_STAGES; s++) {
		mb_marker(0, s);
		mb_log_stage(0, s, mb.rows, mb_stage(s, mb.rows));
	}
	int r = mb_cpu1_pass();
	fenykl_log_info(&mb_log0, "macbench done %d: BAD cpu0 %u, cpu1 %u, %u ms", r, mb.bad[0],
			mb.bad[1], FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW()) / 1000);
	mb_flush();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);

	fenykl_log_task_start();
	fenykl_log_info(&mb_log0, "macbench, load %08x size %x, log init %d", load_base, image_size,
			r);
	void *stack = fw_malloc(MB_STACK_SIZE);
	if (!stack) {
		fenykl_log_err(&mb_log0, "no stack");
		return;
	}
	/* Priority 12, below PER_WDT (11): fenykl_cpu1_start/stop spin on CPU0 until CPU1 answers. */
	fw_OlyTaskCreate("MBCTL", mb_control_task, 2, 0, 12, MB_STACK_SIZE, stack);
}
