/*
 * nnbench: neural-network kernel costs on CPU1 (and the instruction timings of the former
 * macbench on both cores). A CPU0 control task borrows CPU1 task 0x1002 and has the worker
 * (bench.c) run the stages (cases.c) one per command, most important first. Before every stage it
 * logs a marker and flushes, so a hang shows which stage caused it; it logs the worker's rows as
 * they come and flushes every NB_FLUSH_ROWS. After CPU1 it runs the instruction stages on CPU0.
 *
 * Log: C:\FENYKL.LOG, module "nnb", one line per measurement (nnbench_report.py reads them):
 *   R st=<stage> k=<family> v=<variant> <param>=<value>... m=<mode> it=<calls per rep> reps=<n>
 *     us=<min>/<median> (per rep) cyc=<min>/<median> (per call) ops=<n> <unit> ops2=<n> <unit2>
 *     code=<bytes> chk=OK|BAD|- bad=<mismatching outputs> [live modes: frame=<slot> hash=<x> tries=<n>]
 *     [split generated code: blocks=<n> sizes=<bytes>,<bytes>...]
 *   R st=<stage> k=<family> v=<variant> ci=<case> chk=BAD|SKIP err=<reason>
 *   I c=<core> st=<stage> kind=<kind> us=<min>/<median> ops=<n> got=<x> want=<x> mdr=<x> hi=<x>
 *     chk=OK|BAD|- name=<the rest of the line>
 * Cycles use the CPU1 clock measured by the calib stage (one-cycle add), or 159 MHz before it.
 *
 * Camera: copy FENYKL.BIN to the card root, power on in P mode and leave live view idle on a static
 * scene (no menus, no shooting, no movie, no MF-assist zoom: its FD frame is a constant fill). The
 * run takes about 2 to 3 minutes after the 3 s start delay; wait 5 minutes, power off and keep
 * FENYKL.LOG. Then the same with nnbench2's FENYKL.BIN (about 1 minute; its live stage needs live
 * view, else its rows say SKIP), and `python3 nnbench_report.py nnbench.log nnbench2.log` on the PC.
 * A log ends with "nnbench done"; without it, the last "stage ... next" line names the stage that
 * hung. The plan (stages, families) is the plugin's plan.c; nnbench2 builds this file with its own.
 * While CPU1 runs, the task publishes the newest FD frame slot for the live modes every 10 ms.
 */
#include "log.h"
#include "nnbench.h"
#include "../birddetect/fd_frame.h"

#define NB_STACK_SIZE 0x2000u
#define NB_START_DELAY 3000	/* ticks after boot */
#define NB_ROW_TIMEOUT 60000u	/* ms without a new row */
#define NB_STOP_TIMEOUT 1000	/* ticks */
#define NB_FLUSH_ROWS 16u
#define NB_KHZ_DEFAULT 159000u

struct nb_state {
	struct nb_row rows[NB_ISA_ROWS];
	uint32_t khz;			/* CPU1 clock from the calib stage */
	uint32_t empty_us, empty_ops;	/* the running core's empty block loop */
	uint32_t rows_logged, bad;
	uint32_t fd_last, fd_pubs;	/* FD slot publishes seen in the current stage */
	char line[320];
};

/* Whole lines: the CPU1 data linked next to it must not share one. */
static struct nb_state nb __attribute__((aligned(64)));

static FENYKL_LOG_MODULE(nb_log, "nnb", FENYKL_LOG_INFO);

static const char *const nb_mode_name[NB_MODES] = { "warm", "cold", "uncached", "live", "liveu" };
static const char *const nb_status_name[] = { "OK", "BAD", "-", "SKIP" };

/* Puts everything queued on the card before the caller goes on. */
static void nb_flush(void)
{
	for (int i = 0; i < 200; i++) {
		int r = fenykl_log_flush();

		if (r != FENYKL_LOG_EBUSY && r != FENYKL_LOG_ENOCARD)
			return;
		fw_OlyTaskSleep(10);
	}
}

static unsigned int nb_len(const char *s)
{
	unsigned int n = 0;

	while (s[n])
		n++;
	return n;
}

static void nb_emit(void)
{
	fenykl_log_text(&nb_log, FENYKL_LOG_INFO, nb.line, nb_len(nb.line));
	if (++nb.rows_logged % NB_FLUSH_ROWS == 0)
		nb_flush();
}

/* Cycles per call of a rep of iters calls (32-bit: reps up to ~2.5 s). */
static uint32_t nb_cyc(uint32_t us, uint32_t iters)
{
	return us * (nb.khz / 100u) / (10u * iters);
}

static void nb_log_case(const struct nb_row *r)
{
	const struct nb_stage *st = &nb_stages[r->stage];
	const struct nb_case *k = &st->cases[r->ci];
	const struct nb_fam *f = nb_fams[k->fam];
	char *p = nb.line;

	if (r->name) {
		fw_sprintf(p, "R st=%s k=%s v=%s ci=%u chk=%s err=%s", st->name, f->name,
			   f->var[r->var].name, r->ci, nb_status_name[r->status], r->name);
		nb.bad += r->status == NB_BAD;
		nb_emit();
		return;
	}
	fw_sprintf(p, "R st=%s k=%s v=%s", st->name, f->name, f->var[r->var].name);
	p += nb_len(p);
	for (unsigned int i = 0; i < NB_NP && f->pname[i]; i++) {
		fw_sprintf(p, " %s=%u", f->pname[i], k->p[i]);
		p += nb_len(p);
	}
	fw_sprintf(p, " m=%s it=%u reps=%u us=%u/%u cyc=%u/%u", nb_mode_name[k->mode], r->iters,
		   r->reps, r->us_min, r->us_med, nb_cyc(r->us_min, r->iters),
		   nb_cyc(r->us_med, r->iters));
	p += nb_len(p);
	fw_sprintf(p, " ops=%u %s ops2=%u %s code=%u chk=%s bad=%u", r->ops, f->unit, r->ops2,
		   f->unit2, r->code, nb_status_name[r->status], r->bad);
	if (k->mode >= NB_LIVE) {
		p += nb_len(p);
		fw_sprintf(p, " frame=%08x hash=%08x tries=%u", r->got, r->want, r->got2);
	}
	if (r->nblk) {
		p += nb_len(p);
		fw_sprintf(p, " blocks=%u sizes=", r->nblk);
		for (unsigned int i = 0; i < r->nblk; i++) {
			p += nb_len(p);
			fw_sprintf(p, i ? ",%u" : "%u", r->blk[i]);
		}
	}
	nb.bad += r->status == NB_BAD;
	nb_emit();
}

static void nb_log_isa(unsigned int core, unsigned int stage, const struct nb_row *r)
{
	if (r->kind == NB_K_EMPTY) {
		nb.empty_us = r->us_min;
		nb.empty_ops = r->ops;
	}
	fw_sprintf(nb.line, "I c=%u st=%s kind=%u us=%u/%u ops=%u got=%x want=%x", core,
		   nb_stages[stage].name, r->kind, r->us_min, r->us_med, r->ops, r->got, r->want);
	fw_sprintf(nb.line + nb_len(nb.line), " mdr=%x hi=%x chk=%s name=%s", r->got2, r->want2,
		   nb_status_name[r->status], r->name);
	nb.bad += r->status == NB_BAD;
	nb_emit();
	/* calib: the clock if add is one cycle (row 1), less the empty loop */
	if (r->kind == NB_K_OP && nb.empty_ops && r->us_min > nb.empty_us * r->ops / nb.empty_ops) {
		uint32_t khz = r->ops * 1000u / (r->us_min - nb.empty_us * r->ops / nb.empty_ops);

		fenykl_log_info(&nb_log, "clock c=%u khz=%u (add = 1 cycle)", core, khz);
		if (core)
			nb.khz = khz;
	}
}

static void nb_marker(unsigned int core, unsigned int stage)
{
	fenykl_log_info(&nb_log, "stage c=%u s=%s next", core, nb_stages[stage].name);
	nb_flush();
}

/* The newest FD frame slot for the live modes (0: none), and the publishes counted. */
static void nb_fd_poll(volatile struct nb_shared *sh)
{
	uint32_t a = *(volatile uint32_t *)BD_FD_PUB;

	if (a != nb.fd_last) {
		nb.fd_last = a;
		nb.fd_pubs++;
	}
	sh->fd_addr = bd_fd_is_slot(a) ? (uint32_t)FENYKL_UNCACHED(a) : 0;
}

/* The CPU1 pass: 0, or <0 if the worker failed to start or answer. */
static int nb_cpu1_pass(void)
{
	volatile struct nb_shared *sh = FENYKL_SHARED_PTR(nb_sh);
	int r = fenykl_cpu1_start(nb_worker_cpu1, 0);

	fenykl_log_info(&nb_log, "start %d (expect 0)", r);
	if (r)
		return r;
	for (unsigned int s = 0; s < nb_nstages; s++) {
		uint32_t waited = 0;

		nb_marker(1, s);
		nb.fd_pubs = 0;
		nb_fd_poll(sh);
		sh->cmd = s + 1;
		for (;;) {
			int last = sh->done == s + 1;

			while (sh->tail != sh->head) {
				struct nb_row row = *(const struct nb_row *)&sh->ring[sh->tail % NB_RING];

				__asm__ volatile("" ::: "memory");
				sh->tail++;
				waited = 0;
				if (row.var == NB_ISA_VAR)
					nb_log_isa(1, s, &row);
				else
					nb_log_case(&row);
			}
			if (last)
				break;
			if (waited >= NB_ROW_TIMEOUT) {
				fenykl_log_err(&nb_log, "stage %s: no row after %u ms, case %u, worker core %u",
					       nb_stages[s].name, waited, sh->ci, sh->core);
				return -1;
			}
			fw_OlyTaskSleep(10);
			waited += 10;
			nb_fd_poll(sh);
		}
		fenykl_log_info(&nb_log, "stage c=1 s=%s done, %u rows, %u BAD so far, %u FD publishes",
				nb_stages[s].name, nb.rows_logged, nb.bad, nb.fd_pubs);
	}
	r = fenykl_cpu1_stop(NB_STOP_TIMEOUT);
	fenykl_log_info(&nb_log, "stop %d (expect 0), state %x (expect 10), worker core %u", r,
			fenykl_cpu1_state(), sh->core);
	return 0;
}

/* macbench's instruction stages on CPU0, for the comparison with CPU1. */
static void nb_cpu0_pass(void)
{
	nb.empty_ops = 0;
	for (unsigned int s = 0; s < nb_nstages; s++) {
		unsigned int n;

		if (!nb_stages[s].isa)
			continue;
		nb_marker(0, s);
		n = nb_isa_stage(nb_stages[s].isa - 1, nb.rows);
		for (unsigned int i = 0; i < n; i++)
			nb_log_isa(0, s, &nb.rows[i]);
	}
}

FENYKL_TASK(nb_control);
void nb_control(void)
{
	fw_OlyTaskSleep(NB_START_DELAY);
	uint32_t t0 = FENYKL_LOG_TIMER_NOW();
	int r;

	nb.khz = NB_KHZ_DEFAULT;
	fenykl_log_info(&nb_log, "%s: %u stages on CPU1, then the instruction stages on CPU0",
			nb_plan_name, nb_nstages);
	r = nb_cpu1_pass();
	nb_cpu0_pass();
	fenykl_log_info(&nb_log, "nnbench done %d: %u rows, %u BAD, %u ms", r, nb.rows_logged, nb.bad,
			FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW()) / 1000);
	nb_flush();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);

	fenykl_log_task_start();
	fenykl_log_info(&nb_log, "%s, load %08x size %x, log init %d", nb_plan_name, load_base, image_size,
			r);
	void *stack = fw_malloc(NB_STACK_SIZE);
	if (!stack) {
		fenykl_log_err(&nb_log, "no stack");
		return;
	}
	/* Priority 12, below PER_WDT (11): fenykl_cpu1_start/stop spin on CPU0 until CPU1 answers. */
	fw_OlyTaskCreate("NBCTL", nb_control_task, 2, 0, 12, NB_STACK_SIZE, stack);
}
