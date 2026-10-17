/* Camera experiment: live-view frames read on CPU1. A CPU0 control task (priority 12) polls the
   face-detection image the firmware publishes every LV frame (640x480 Y8, 5 slots, complete when
   published), borrows CPU1 task 0x1002 with fenykl_cpu1_start and, every PUB_STEP-th publish, asks
   the worker (worker.c) to sum that frame uncached and cached. After RUN_US it stops the worker and
   logs the timings, bandwidths, sum agreement and frame changes. No hooks: the firmware is only
   read. */
#include "shared.h"

#define STACK_SIZE 0x2000u
#define LOG_SIZE 0x2000u
#define LOG_LINE 64u /* padding line length */
#define WAIT_LV_US 60000000u /* for the first FD publishes, before CPU1 is touched */
#define RUN_US 30000000u
#define REPORT_US 10000000u
#define STOP_TIMEOUT 1000 /* ticks */
#define PUB_STEP 10u /* a request at most every 10th published FD frame */
#define DETAIL 10u /* results logged one by one: the first DETAIL, then mismatches up to DETAIL */
#define MIN_REQUESTS 30u

/* Live-view frame addresses, verified by the framepaths probe (framepaths.c uses the same). */
#define LV_FRAME_COUNTER 0x60576464u /* u32, +1 per sensor frame handed out */
#define FD_PUB 0x606067B0u /* u32: FD slot published by mov_fd_publish (uncached address) */
#define FD_DESC 0x60606024u /* FD descriptor: +0 address, +4 u16 width, +6 u16 height */
#define FD_SLOT0 0xADC56300u /* ROM table 0x6E9EA9EC: 5 slots of 0x4B000 bytes */
#define FD_SLOTS 5u

/* --- log: C:\FENYKL.LOG, always LOG_SIZE bytes padded with blank lines ("w" does not truncate) --- */

static char logbuf[LOG_SIZE];
static unsigned loglen;
static char line[192];

/* Appends fw_sprintf(line, ...) (at most 8 variadic words) as one line; dropped if it does not fit. */
#define LOG(...)                                                                          \
	do {                                                                              \
		fw_sprintf(line, __VA_ARGS__);                                            \
		add_line();                                                               \
	} while (0)

static void add_line(void)
{
	unsigned n = 0;

	while (line[n])
		n++;
	if (loglen + n + 2 > LOG_SIZE)
		return;
	memcpy(logbuf + loglen, line, n);
	loglen += n;
	logbuf[loglen++] = '\r';
	logbuf[loglen++] = '\n';
}

/* Rewrites the whole log; the padding keeps an older, longer file's tail from surviving. */
static void write_log(void)
{
	for (unsigned i = loglen; i < LOG_SIZE; i++) {
		unsigned col = i % LOG_LINE;
		logbuf[i] = col == LOG_LINE - 2 ? '\r' : col == LOG_LINE - 1 ? '\n' : ' ';
	}
	void *fp = fw_rpc_fopen("C:\\FENYKL.LOG", "w");
	if (!fp)
		return;
	fw_rpc_fwrite(logbuf, 1, LOG_SIZE, fp);
	fw_rpc_fclose(fp);
}

FENYKL_SHARED struct shared shared;

void worker_cpu1(void); /* FENYKL_CPU1_WORKER(worker) in worker.c */

static uint32_t rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

/* --- FD publishes, polled once per tick (the MOV task writes FD_PUB on this core) --- */

static uint32_t pub_addr, pubs, bad_addrs, bad_last, slots_seen;

/* Slot index of an FD address, or -1. */
static int fd_slot(uint32_t addr)
{
	uint32_t off = (uint32_t)FENYKL_UNCACHED(addr) - FD_SLOT0;

	if (off % 0x4B000u || off / 0x4B000u >= FD_SLOTS)
		return -1;
	return off / 0x4B000u;
}

/* Counts a new publish; returns 1 if the published address is a valid FD slot. */
static int poll_pub(void)
{
	uint32_t a = rd32(FD_PUB);

	if (a == pub_addr)
		return 0;
	pub_addr = a;
	pubs++;
	int slot = fd_slot(a);
	if (slot < 0) {
		bad_addrs++;
		bad_last = a;
		return 0;
	}
	slots_seen |= 1u << slot;
	return 1;
}

/* --- results --- */

struct mode_stats {
	uint32_t min, max, total;
};

static struct mode_stats st_u, st_c;
static uint32_t results, agree, mismatch, changes, late, pubs_max, logged_mismatch;
static uint32_t last_sum, mean_min = 0xFFFFFFFFu, mean_max;

static void add_time(struct mode_stats *m, uint32_t us)
{
	if (results == 1 || us < m->min)
		m->min = us;
	if (us > m->max)
		m->max = us;
	m->total += us;
}

/* One finished request: pubs_during = FD publishes between the request and its result. */
static void take_result(volatile struct shared *s, uint32_t slot, uint32_t pubs_during)
{
	uint32_t sum_u = s->sum_u, sum_c = s->sum_c, us_u = s->us_u, us_c = s->us_c;
	uint32_t mean10 = sum_u * 10u / FD_BYTES; /* luma mean x10 */

	results++;
	add_time(&st_u, us_u);
	add_time(&st_c, us_c);
	int same = sum_u == sum_c;
	if (same)
		agree++;
	else
		mismatch++;
	if (results > 1 && sum_u != last_sum)
		changes++;
	last_sum = sum_u;
	if (mean10 < mean_min)
		mean_min = mean10;
	if (mean10 > mean_max)
		mean_max = mean10;
	if (pubs_during >= 2) /* conservative: the 5-slot ring may skip slots held by FD */
		late++;
	if (pubs_during > pubs_max)
		pubs_max = pubs_during;
	if (results <= DETAIL || (!same && logged_mismatch++ < DETAIL))
		LOG("R%u s%u u=%u c=%u us_u=%u us_c=%u mean=%u p=%u", results, slot, sum_u, sum_c, us_u,
		    us_c, mean10, pubs_during);
}

/* MB/s x 10 for FD_BYTES in `us` microseconds (1 byte/us = 1 MB/s). */
static uint32_t mbps10(uint32_t us)
{
	return us ? FD_BYTES * 10u / us : 0;
}

static void log_mode(const char *name, const struct mode_stats *m)
{
	uint32_t avg = results ? m->total / results : 0;
	uint32_t a = mbps10(avg), b = mbps10(m->min);

	LOG("%s: min=%uus avg=%uus max=%uus  avg %u.%u MB/s, best %u.%u MB/s", name, m->min, avg, m->max,
	    a / 10, a % 10, b / 10, b % 10);
}

static void log_progress(volatile struct shared *s, uint32_t t_us)
{
	LOG("t=%us pubs=%u requests=%u results=%u worker beats=%u state=%x", t_us / 1000000u, pubs,
	    s->req_seq, results, s->beats, fenykl_cpu1_state());
	write_log();
}

FENYKL_TASK(control);
void control(void)
{
	volatile struct shared *s = FENYKL_SHARED_PTR(shared);

	LOG("fenykl cpu1frame: CPU1 sums live-view FD frames for ~30 s");
	LOG("before: state=%x (expect 10)", fenykl_cpu1_state());
	write_log();

	/* Wait for live view: three publishes (so the address really moves) before CPU1 is borrowed. */
	pub_addr = rd32(FD_PUB);
	uint32_t t0 = TIMER_NOW();
	while (pubs < 3 && ELAPSED(t0, TIMER_NOW()) < WAIT_LV_US) {
		poll_pub();
		fw_OlyTaskSleep(1);
	}
	LOG("fd: pub=%08x desc addr=%08x w=%u h=%u (expect 640 480) pubs=%u", pub_addr, rd32(FD_DESC),
	    *(volatile uint16_t *)(FD_DESC + 4), *(volatile uint16_t *)(FD_DESC + 6), pubs);
	if (pubs < 3) {
		LOG("no FD frames published in %us: live view not running", WAIT_LV_US / 1000000u);
		LOG("verdict: FAIL");
		write_log();
		return;
	}

	int start = fenykl_cpu1_start(worker_cpu1, 0);
	LOG("start=%d (expect 0)", start);
	if (start)
		LOG("verdict: FAIL");
	write_log();
	if (start)
		return;

	uint32_t lv0 = rd32(LV_FRAME_COUNTER), pubs0 = pubs, slot = 0, pubs_at_req = 0, busy = 0;
	uint32_t next_pub = pubs + PUB_STEP;
	uint32_t report = REPORT_US;
	t0 = TIMER_NOW();
	for (;;) {
		uint32_t t = ELAPSED(t0, TIMER_NOW());
		if (t >= RUN_US)
			break;
		int fresh = poll_pub();
		if (busy && s->res_seq == s->req_seq) {
			take_result(s, slot, pubs - pubs_at_req);
			busy = 0;
		}
		if (!busy && fresh && pubs >= next_pub && s->phase == 2) {
			slot = fd_slot(pub_addr);
			s->req_addr = (uint32_t)FENYKL_UNCACHED(pub_addr);
			s->req_seq = s->req_seq + 1;
			pubs_at_req = pubs;
			next_pub = pubs + PUB_STEP;
			busy = 1;
		}
		if (t >= report) {
			log_progress(s, t);
			report += REPORT_US;
		}
		fw_OlyTaskSleep(1);
	}
	uint32_t run_us = ELAPSED(t0, TIMER_NOW());
	uint32_t lv_frames = rd32(LV_FRAME_COUNTER) - lv0, run_pubs = pubs - pubs0;

	/* The worker finishes a pending request before it sees the stop flag. */
	int stop = fenykl_cpu1_stop(STOP_TIMEOUT);
	int state = fenykl_cpu1_state();
	if (busy && s->res_seq == s->req_seq)
		take_result(s, slot, pubs - pubs_at_req);
	LOG("stop=%d (expect 0) state=%x (expect 10) phase=%u (expect 3)", stop, state, s->phase);
	LOG("worker core=%u (expect 1) beats=%u chunk_max=%uus (expect < 10000)", s->core_id, s->beats,
	    s->chunk_max);
	LOG("frames in %ums: lv=%u fd pubs=%u slots seen=%x (expect 1f) bad=%u last bad=%08x",
	    run_us / 1000u, lv_frames, run_pubs, slots_seen, bad_addrs, bad_last);
	LOG("requests=%u results=%u (expect >= %u) every %u-th FD frame", s->req_seq, results,
	    MIN_REQUESTS, PUB_STEP);
	log_mode("uncached", &st_u);
	log_mode("cached+purge", &st_c);
	uint32_t hot = mbps10(s->hot_us);
	LOG("hot loop (cache-resident data): %uus  %u.%u MB/s", s->hot_us, hot / 10, hot % 10);
	LOG("sums: cached==uncached %u, differ %u (late %u, max %u pubs during a request)", agree,
	    mismatch, late, pubs_max);
	LOG("frame changes: %u of %u; mean luma x10 min=%u max=%u", changes, results ? results - 1 : 0,
	    mean_min, mean_max);
	int pass = stop == 0 && state == FENYKL_CPU1_DORMANT && s->phase == 3 && s->core_id == 1 &&
		   bad_addrs == 0 && results >= MIN_REQUESTS && changes >= 1;
	LOG("verdict: %s", pass ? "PASS" : "FAIL");
	write_log();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	void *stack = fw_malloc(STACK_SIZE);
	if (!stack)
		return;
	/* Priority 12, below PER_WDT (11): OlyTaskStart/Stop of a CPU1 task spin on CPU0 until CPU1's idle
	   loop answers, which must never starve the watchdog task. */
	fw_OlyTaskCreate("C1FRM", control_task, 2, 0, 12, STACK_SIZE, stack);
}
