/* Camera probe: small live-view frames from the IPU resizer.
 *
 * A CPU0 task runs standalone img_exec jobs with ucMode 0x15 (the YC resize/crop mode that the stock
 * REC_IMAGE and MOV code use, e.g. 640x480 -> 640x360 for the movie frame buffer) on two live-view
 * sources and checks the results:
 *   C/D: the shown display buffer (800x600 YUYV, live image in x 0..539, y 0..479 on the rear LCD)
 *        -> YUYV 160x120 / 320x240 (stock-like job: YC in, YC out);
 *   A/B: the published face-detection frame (640x480 Y8) -> Y8 160x120 / 320x240, with ulFlag 8
 *        (single 8-bit plane in) and 4 (Y-only out), read from the calculator code. On the camera A
 *        timed out on engine 1 (status 0x2D30 after 500 ms), so A/B run last.
 * Stage 1: C/D on engine 1 (idle in still live view); stage 2: C/D on engine 2 (shared with the
 * live-view pass, serialised by the firmware's IPU lock); stage 3: A/B on engine 1. Per stage:
 * SAMPLES frames are converted, checked and dumped to the card as raw files
 * (LR<stage><job><sample>.BIN, plus the FD frame as job F), then each job is timed TIMING_RUNS times. Any non-zero img_exec status disables that job; a status that took
 * longer than SLOW_US (an IPU timeout) stops the probe. Log: C:\FENYKL.LOG. */
#include <fenykl.h>
#include "log.h"

#define LR_STACK_SIZE 0x2000u
#define LR_START_MS 15000
#define LR_SAMPLES 3u
#define LR_TIMING_RUNS 20u
#define LR_SLOW_US 100000u
#define LR_PUB_WAIT 300 /* ticks for a new FD publish */
#define LR_CHUNK 0x10000u
#define LR_GUARD 0x100u
#define LR_FILL 0xA5u
#define LR_GUARD_BYTE 0x5Au

/* Live-view addresses, verified by earlier probes (framepaths, fdgrab, cpu1frame). */
#define LR_FD_PUB 0x606067B0u /* u32: FD slot published by mov_fd_publish (uncached) */
#define LR_IMG1 0x6034C880u /* u32: display buffer on screen (img1 shadow), stable for 2 flips */
#define LR_CAMTOP 0x605AED51u /* u8: 8 = magnified view, FD frame is a constant fill */
#define LR_OUT_TYPE 0x60606205u /* u8: output type byte (1 seen on the rear LCD) */
#define LR_IPU_LOCK 0x8FF00100u /* bits 2/4: engine 1/2 busy */

#define LR_FD_W 640u
#define LR_FD_H 480u
#define LR_DISP_STRIDE 800u /* pixels, 2 bytes each */
#define LR_DISP_W 540u
#define LR_DISP_H 480u

/* IMG_CTRL (0xC8 bytes), field names from the firmware's own debug dump (mov_dbg.c). */
#define IC_SIZE 0xC8u
#define IC_R1ADDR 0x00u
#define IC_W1ADDR 0x10u
#define IC_FLAG 0x54u
#define IC_ABORT 0x5Cu
#define IC_PARAM 0x60u
#define IC_STATUS 0x68u
#define IC_IN_MEM_HW 0x6Au
#define IC_IN_USE_HW 0x6Cu
#define IC_IN_USE_VW 0x6Eu
#define IC_IN_USE_HS 0x70u
#define IC_IN_AREA_HW 0x72u
#define IC_IN_AREA_VW 0x74u
#define IC_IN_AREA_HC 0x76u
#define IC_IN_AREA_VC 0x78u
#define IC_OUT_WHOLE_HW 0x8Eu
#define IC_OUT_WHOLE_VW 0x90u
#define IC_OUT_AREA_HS 0x98u
#define IC_OUT_AREA_VS 0x9Au
#define IC_OUT_AREA_HW 0x9Cu
#define IC_OUT_AREA_VW 0x9Eu
#define IC_ZOOM 0xB2u
#define IC_FUNC 0xC2u
#define IC_MODE 0xC4u

#define IC_MODE_RESIZE 0x15u
#define IC_FLAG_Y_OUT 0x4u /* output format 1: one 8-bit plane */
#define IC_FLAG_Y_IN 0x8u /* input: one 8-bit plane */
#define IC_FLAG_ENGINE2 0x10000000u

#define IP_SIZE 0x80u /* IMG_PARAM: fields up to +0x5D; zero = no picture processing */

enum lr_src { LR_SRC_FD, LR_SRC_DISP };

#define LR_JOBS_FD 0x3u /* A, B */
#define LR_JOBS_DISP 0xCu /* C, D */

struct lr_job {
	char letter;
	uint8_t src;
	uint16_t out_w, out_h;
	uint8_t bpp; /* output bytes per pixel: 1 Y8, 2 YUYV */
	uint32_t off; /* in the RECFILE buffer */
	uint8_t disabled;
	uint8_t ran; /* run in this sample */
	uint32_t t_min, t_max, t_sum, t_n;
};

struct lr_state {
	uint32_t buf; /* recfile_alloc result (uncached), 0 = none */
	uint32_t buf_size;
	uint32_t fd_off; /* FD frame copy */
	struct lr_job job[4];
	uint8_t ctrl[IC_SIZE] __attribute__((aligned(4)));
	uint8_t param[IP_SIZE] __attribute__((aligned(4)));
	uint32_t fd_addr, disp_addr;
	uint32_t errors;
	uint8_t stop;
};

static struct lr_state lr;

FENYKL_LOG_MODULE(lr_log, "lvrsz", FENYKL_LOG_DEBUG);

static uint32_t lr_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

static uint8_t lr_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t lr_now(void)
{
	return FENYKL_LOG_TIMER_NOW();
}

static uint32_t lr_since(uint32_t t)
{
	return FENYKL_LOG_ELAPSED(t, FENYKL_LOG_TIMER_NOW());
}

static volatile uint8_t *lr_ptr(uint32_t off)
{
	return (volatile uint8_t *)(lr.buf + off);
}

static void lr_flush(void)
{
	for (int i = 0; i < 200; i++) {
		int r = fenykl_log_flush();

		if (r != FENYKL_LOG_EBUSY && r != FENYKL_LOG_ENOCARD)
			return;
		fw_OlyTaskSleep(10);
	}
}

static void ic_w32(unsigned off, uint32_t v)
{
	*(uint32_t *)(lr.ctrl + off) = v;
}

static void ic_w16(unsigned off, uint32_t v)
{
	*(uint16_t *)(lr.ctrl + off) = (uint16_t)v;
}

/* Fills IMG_CTRL like the stock YC resize job of REC_IMAGE (0x6E8DF67C), plus engine/plane flags. */
static void lr_build(const struct lr_job *j, uint32_t flags)
{
	uint32_t in, mem_w, w, h;

	if (j->src == LR_SRC_FD) {
		in = lr.fd_addr;
		mem_w = LR_FD_W;
		w = LR_FD_W;
		h = LR_FD_H;
		flags |= IC_FLAG_Y_IN | IC_FLAG_Y_OUT;
	} else {
		in = lr.disp_addr;
		mem_w = LR_DISP_STRIDE;
		w = LR_DISP_W;
		h = LR_DISP_H;
	}
	memset(lr.ctrl, 0, sizeof(lr.ctrl));
	ic_w32(IC_R1ADDR, (uint32_t)FENYKL_UNCACHED(in));
	ic_w32(IC_W1ADDR, lr.buf + j->off);
	ic_w32(IC_FLAG, flags);
	ic_w32(IC_ABORT, 0);
	ic_w32(IC_PARAM, (uint32_t)lr.param);
	ic_w16(IC_IN_MEM_HW, mem_w);
	ic_w16(IC_IN_USE_HW, w);
	ic_w16(IC_IN_USE_VW, h);
	ic_w16(IC_IN_USE_HS, 0);
	ic_w16(IC_IN_AREA_HW, w);
	ic_w16(IC_IN_AREA_VW, h);
	ic_w16(IC_IN_AREA_HC, w / 2);
	ic_w16(IC_IN_AREA_VC, h / 2);
	ic_w16(IC_OUT_WHOLE_HW, j->out_w);
	ic_w16(IC_OUT_WHOLE_VW, j->out_h);
	ic_w16(IC_OUT_AREA_HS, 0);
	ic_w16(IC_OUT_AREA_VS, 0);
	ic_w16(IC_OUT_AREA_HW, j->out_w);
	ic_w16(IC_OUT_AREA_VW, j->out_h);
	ic_w16(IC_ZOOM, 100);
	lr.ctrl[IC_FUNC] = 0;
	lr.ctrl[IC_MODE] = IC_MODE_RESIZE;
}

static uint32_t lr_out_bytes(const struct lr_job *j)
{
	return (uint32_t)j->out_w * j->out_h * j->bpp;
}

static void lr_prepare_out(const struct lr_job *j)
{
	volatile uint8_t *p = lr_ptr(j->off);
	uint32_t n = lr_out_bytes(j);

	for (uint32_t i = 0; i < n; i++)
		p[i] = LR_FILL;
	for (uint32_t i = 0; i < LR_GUARD; i++)
		p[n + i] = LR_GUARD_BYTE;
}

/* Runs one job; returns the img_exec status. Disables the job on a non-zero status. */
static unsigned lr_run(struct lr_job *j, uint32_t flags, uint32_t *us)
{
	lr_build(j, flags);
	uint32_t t0 = lr_now();
	unsigned st = fw_img_exec(lr.ctrl);

	*us = lr_since(t0);
	if (!st)
		return 0;
	j->disabled = 1;
	lr.errors++;
	fenykl_log_err(&lr_log, "job %c flags %x: status %x after %u us", j->letter, flags, st, *us);
	if (*us > LR_SLOW_US) {
		fenykl_log_err(&lr_log, "slow error status: stopping the probe");
		lr.stop = 1;
	}
	return st;
}

/* Waits for the next FD publish; takes the FD slot and the display buffer on screen. */
static int lr_take_sources(void)
{
	uint32_t old = lr_rd32(LR_FD_PUB);

	for (int i = 0; i < LR_PUB_WAIT; i++) {
		uint32_t a = lr_rd32(LR_FD_PUB);

		if (a && a != old) {
			lr.fd_addr = a;
			lr.disp_addr = lr_rd32(LR_IMG1);
			return lr.disp_addr ? 0 : -1;
		}
		fw_OlyTaskSleep(1);
	}
	return -1;
}

static void lr_copy_fd(void)
{
	const volatile uint32_t *s = FENYKL_UNCACHED(lr.fd_addr);
	volatile uint32_t *d = (volatile uint32_t *)lr_ptr(lr.fd_off);

	for (uint32_t i = 0; i < LR_FD_W * LR_FD_H / 4; i++)
		d[i] = s[i];
}

/* Output checks: unwritten fill bytes, guard intact, mean of Y (and of U, V for YUYV). */
static void lr_check_out(const struct lr_job *j, char stage, unsigned sample)
{
	volatile uint8_t *p = lr_ptr(j->off);
	uint32_t n = lr_out_bytes(j);
	uint32_t fill = 0, guard = 0, ysum = 0, usum = 0, vsum = 0;

	for (uint32_t i = 0; i < n; i++) {
		uint8_t v = p[i];

		fill += v == LR_FILL;
		if (j->bpp == 1 || !(i & 1))
			ysum += v;
		else if (i & 2)
			vsum += v;
		else
			usum += v;
	}
	for (uint32_t i = 0; i < LR_GUARD; i++)
		guard += p[n + i] != LR_GUARD_BYTE;
	uint32_t px = (uint32_t)j->out_w * j->out_h;

	fenykl_log_info(&lr_log, "chk %c%c%u fill %u", stage, j->letter, sample, fill);
	fenykl_log_info(&lr_log, "chk %c%c guard-bad %u meanY %u", stage, j->letter, guard,
			ysum / px);
	if (j->bpp == 2)
		fenykl_log_info(&lr_log, "chk %c%c meanU %u meanV %u", stage, j->letter,
				usum / (px / 2), vsum / (px / 2));
}

/* Mean absolute difference of a Y8 output against a CPU box filter of the FD copy (k x k). */
static void lr_compare_box(const struct lr_job *j, char stage)
{
	volatile uint8_t *f = lr_ptr(lr.fd_off);
	volatile uint8_t *o = lr_ptr(j->off);
	unsigned k = LR_FD_W / j->out_w;
	uint32_t rows = (LR_FD_H - 4) / k; /* FD row 479 is garbage */
	uint32_t sum = 0, max = 0, n = 0, fsum = 0;

	for (uint32_t y = 0; y < rows; y++) {
		for (uint32_t x = 0; x < j->out_w; x++) {
			uint32_t acc = 0;

			for (unsigned dy = 0; dy < k; dy++)
				for (unsigned dx = 0; dx < k; dx++)
					acc += f[(y * k + dy) * LR_FD_W + x * k + dx];
			acc = (acc + k * k / 2) / (k * k);
			uint32_t v = o[y * j->out_w + x];
			uint32_t d = v > acc ? v - acc : acc - v;

			sum += d;
			fsum += acc;
			max = d > max ? d : max;
			n++;
		}
	}
	fenykl_log_info(&lr_log, "box %c%c k%u mad x100 %u", stage, j->letter, k, sum * 100 / n);
	fenykl_log_info(&lr_log, "box %c%c max %u boxmean %u", stage, j->letter, max, fsum / n);
}

/* Mean Y of the display source area, every 4th pixel of every 4th row. */
static uint32_t lr_disp_mean(void)
{
	const volatile uint8_t *s = FENYKL_UNCACHED(lr.disp_addr);
	uint32_t sum = 0, n = 0;

	for (uint32_t y = 0; y < LR_DISP_H; y += 4)
		for (uint32_t x = 0; x < LR_DISP_W; x += 4, n++)
			sum += s[(y * LR_DISP_STRIDE + x) * 2];
	return sum / n;
}

static int lr_write_file(char stage, char letter, unsigned sample, uint32_t off, uint32_t n)
{
	char name[16] = "C:\\LR___.BIN";

	name[5] = stage;
	name[6] = letter;
	name[7] = (char)('0' + sample);
	if (!fenykl_card_io_allowed())
		return -5;
	void *fp = fw_rpc_fopen(name, "w");

	if (!fp)
		return -1;
	int r = 0;
	const uint8_t *src = (const uint8_t *)(lr.buf + off);

	for (uint32_t o = 0; !r && o < n; o += LR_CHUNK) {
		uint32_t c = n - o < LR_CHUNK ? n - o : LR_CHUNK;

		if (fw_rpc_fwrite(src + o, 1, c, fp) != (int)c)
			r = -2;
	}
	if (fw_rpc_fclose(fp) == -1 && !r)
		r = -3;
	return r;
}

static void lr_sample(char stage, uint32_t flags, unsigned sample)
{
	uint32_t us;

	if (lr_take_sources()) {
		fenykl_log_warn(&lr_log, "%c%u: no FD publish / display buffer", stage, sample);
		return;
	}
	fenykl_log_info(&lr_log, "%c%u: fd %x disp %x", stage, sample, lr.fd_addr, lr.disp_addr);
	fenykl_log_info(&lr_log, "camtop %u out %u lock %x", lr_rd8(LR_CAMTOP), lr_rd8(LR_OUT_TYPE),
			lr_rd32(LR_IPU_LOCK));
	/* Display jobs first: the display buffer stays on screen for two flips (>= 33 ms). */
	for (unsigned i = 0; i < 4 && !lr.stop; i++) {
		struct lr_job *j = &lr.job[(i + 2) % 4];

		j->ran = 0;
		if (j->disabled)
			continue;
		lr_prepare_out(j);
		j->ran = 1;
		unsigned st = lr_run(j, flags, &us);

		fenykl_log_info(&lr_log, "job %c%c%u status %x", stage, j->letter, sample, st);
		fenykl_log_info(&lr_log, "job %c%c us %u", stage, j->letter, us);
	}
	uint32_t t0 = lr_now();

	lr_copy_fd();
	fenykl_log_info(&lr_log, "%c%u: fd copy %u us, pub now %x", stage, sample, lr_since(t0),
			lr_rd32(LR_FD_PUB));
	fenykl_log_info(&lr_log, "%c%u: display meanY %u", stage, sample, lr_disp_mean());
	for (unsigned i = 0; i < 4; i++) {
		struct lr_job *j = &lr.job[i];

		if (j->disabled || !j->ran)
			continue;
		lr_check_out(j, stage, sample);
		if (j->src == LR_SRC_FD)
			lr_compare_box(j, stage);
	}
	lr_flush();
	int r = lr_write_file(stage, 'F', sample, lr.fd_off, LR_FD_W * LR_FD_H);

	for (unsigned i = 0; i < 4 && !r; i++) {
		struct lr_job *j = &lr.job[i];

		if (!j->disabled && j->ran)
			r = lr_write_file(stage, j->letter, sample, j->off, lr_out_bytes(j));
	}
	fenykl_log_info(&lr_log, "%c%u: dump %d", stage, sample, r);
	lr_flush();
}

static void lr_timing(char stage, uint32_t flags)
{
	if (lr_take_sources())
		return;
	for (unsigned i = 0; i < 4 && !lr.stop; i++) {
		struct lr_job *j = &lr.job[i];

		j->t_min = 0xFFFFFFFFu;
		j->t_max = 0;
		j->t_sum = 0;
		j->t_n = 0;
		for (unsigned k = 0; k < LR_TIMING_RUNS && !j->disabled && !lr.stop; k++) {
			uint32_t us;

			if (lr_run(j, flags, &us))
				break;
			j->t_min = us < j->t_min ? us : j->t_min;
			j->t_max = us > j->t_max ? us : j->t_max;
			j->t_sum += us;
			j->t_n++;
			fw_OlyTaskSleep(5);
		}
		if (!j->t_n)
			continue;
		fenykl_log_info(&lr_log, "time %c%c n %u min %u", stage, j->letter, j->t_n, j->t_min);
		fenykl_log_info(&lr_log, "time %c%c avg %u max %u", stage, j->letter, j->t_sum / j->t_n,
				j->t_max);
	}
	lr_flush();
}

/* Runs the jobs in `mask` (bit i = job[i]). */
static void lr_stage(char stage, uint32_t flags, unsigned mask)
{
	for (unsigned i = 0; i < 4; i++)
		lr.job[i].disabled = !(mask & (1u << i));
	fenykl_log_info(&lr_log, "stage %c: ulFlag %x jobs %x", stage, flags, mask);
	for (unsigned s = 0; s < LR_SAMPLES && !lr.stop; s++) {
		lr_sample(stage, flags, s);
		fw_OlyTaskSleep(1000);
	}
	if (!lr.stop)
		lr_timing(stage, flags);
}

/* Claims a contiguous free run in RECFILE first: recfile_alloc would block until one is free. */
static int lr_alloc(void)
{
	uint32_t blocks = (lr.buf_size + 0x1E3C0u - 1) / 0x1E3C0u, run = 0;

	for (uint32_t i = 0; i < 256 && run < blocks; i++)
		run = lr_rd8(0x605753F0u + i) ? 0 : run + 1;
	if (run < blocks)
		return -1;
	lr.buf = fw_recfile_alloc(lr.buf_size);
	return lr.buf ? 0 : -1;
}

static void lr_layout(void)
{
	static const struct lr_job jobs[4] = {
		{ 'A', LR_SRC_FD, 160, 120, 1 },
		{ 'B', LR_SRC_FD, 320, 240, 1 },
		{ 'C', LR_SRC_DISP, 160, 120, 2 },
		{ 'D', LR_SRC_DISP, 320, 240, 2 },
	};
	uint32_t off = LR_FD_W * LR_FD_H + LR_GUARD;

	lr.fd_off = 0;
	for (unsigned i = 0; i < 4; i++) {
		lr.job[i] = jobs[i];
		lr.job[i].off = off;
		off += lr_out_bytes(&lr.job[i]) + LR_GUARD;
	}
	lr.buf_size = off;
}

FENYKL_TASK(lr_main);
void lr_main(void)
{
	fw_OlyTaskSleep(LR_START_MS);
	memset(lr.param, 0, sizeof(lr.param));
	lr_layout();
	if (lr_alloc()) {
		fenykl_log_err(&lr_log, "no RECFILE run for %u bytes", lr.buf_size);
		lr_flush();
		return;
	}
	fenykl_log_info(&lr_log, "buffer %x size %x lock %x", lr.buf, lr.buf_size,
			lr_rd32(LR_IPU_LOCK));
	lr_stage('1', 0, LR_JOBS_DISP);
	if (!lr.stop)
		lr_stage('2', IC_FLAG_ENGINE2, LR_JOBS_DISP);
	if (!lr.stop)
		lr_stage('3', 0, LR_JOBS_FD);
	fw_recfile_free(lr.buf);
	fenykl_log_info(&lr_log, "lvrsz done: errors %u stop %u", lr.errors, lr.stop);
	lr_flush();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);

	fenykl_log_task_start();
	fenykl_log_info(&lr_log, "lvrsz start, load %x size %x, log init %d", load_base, image_size, r);
	void *stack = fw_malloc(LR_STACK_SIZE);

	if (!stack) {
		fenykl_log_err(&lr_log, "no stack");
		return;
	}
	fw_OlyTaskCreate("LVRSZ", lr_main_task, 2, 0, 12, LR_STACK_SIZE, stack);
}
