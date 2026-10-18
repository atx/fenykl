/* Camera probe: live-view colour calibration dumps, one set per shutter press.
 *
 * On each FULL press (event layer, every event passed through) the probe task (priority 12, below the
 * REC tasks) waits for the next display flip and takes, before the still pipeline runs, from the buffer
 * that flip showed:
 *   R: one IPU engine-1 resize of the shown display buffer to 320x240 YUYV (img_exec ucMode 0x15, zero
 *      IMG_PARAM, the job lvrsz measured at ~2.3 ms); skipped if engine 1 is locked (0x8FF00100 bit
 *      value 2), more than 100 ms passed since the press, or after resize errors;
 *   D: the live area of the same buffer (540x480 YUYV of the 800x600 buffer on img1), valid if img1
 *      changed at most once during the copy (a change to 0, display off, counts); one retry on a new
 *      buffer, else D is not written;
 *   F: the face-detection frame (640x480 Y8): the publish word if set, else the last slot the task saw
 *      published within 200 ms before the press (the word was 0 after FULL in every set of the first
 *      run, M mode); valid if at most 3 further publishes happened during the copy (5-slot ring).
 * The header's valid bits (R 1, D 2, F 4) say which files hold a consistent frame; invalid D and F
 * files are not written.
 * The img_exec hook copies, once per press, the IMG_CTRL, IMG_PARAM and the whole IPU register shadow
 * (context, 0x2230 bytes) of the first live-view pass after the press (engine 2, ucMode 7) and of the
 * first 6 engine-1 jobs after it (the still's develop chain), plus the 256-entry Y tone tables the
 * shadow points to (path A ctx+0x5AC, path B ctx+0x11C4). The copies are made after the original job
 * returned, in the calling task, without blocking (~10 KB word copy each).
 * After the capture indicator says the still is on the card (no take handle with ps == 0xFF in the
 * REC take-handle table, plus a settle time) the task writes C:\LC<nn><K>.BIN, K = P (parameters),
 * R, D, F, nn = press number 00..99, from a RECFILE run held for the boot (8 blocks) and from .bss.
 * Presses while a set is pending are skipped. No firmware state is written. Log: C:\FENYKL.LOG.
 */
#include <fenykl.h>
#include "event.h"
#include "log.h"

#define LC_START_MS 5000
#define LC_STACK_SIZE 0x1000u
#define LC_PRIO 12
#define LC_PRESSES 100u
#define LC_STILL_SLOTS 6u
#define LC_SLOTS (1u + LC_STILL_SLOTS)
#define LC_RSZ_LATE_US 100000u
#define LC_SLOW_US 100000u
#define LC_MAX_ERRORS 3u
#define LC_PRESS_HOLD_US 5000000u /* capture assumed from a press until a take handle appears */
#define LC_SETTLE_US 300000u /* after the last busy take handle, before the card writes */
#define LC_CAP_MAX_US 30000000u /* a still not on the card after this: the set is dropped */
#define LC_FLIP_WAIT_MS 40
#define LC_FD_MAX_AGE_US 200000u
#define LC_FD_MAX_CHANGES 3u
#define LC_TRIES 2
#define LC_CHUNK 0x10000u

/* Live-view addresses, verified by earlier probes (framepaths, lvrsz, lvlock). */
#define LC_FD_PUB 0x606067B0u /* u32: FD slot published by mov_fd_publish, once per FD frame */
#define LC_IMG1 0x6034C880u /* u32: display buffer on screen (img1 shadow), 800x600 YUYV */
#define LC_IPU_LOCK 0x8FF00100u
#define LC_LOCK_E1 0x2u
#define LC_LOCK_E2 0x4u

/* REC take-handle table, 25 entries of 0x18 bytes; ps (+0x15) is 0xFF from the still job's start
 * until REC_FILE has written its file. */
#define LC_TH 0x6061062Cu
#define LC_TH_N 25u
#define LC_TH_SIZE 0x18u
#define LC_TH_PS 0x15u
#define LC_PS_BUSY 0xFFu

/* RECFILE block map: one byte per 0x1E3C0-byte block, non-zero = used. */
#define LC_RF_MAP 0x605753F0u
#define LC_RF_BLOCKS 256u
#define LC_RF_BLOCK 0x1E3C0u
#define LC_RF_MARGIN 64u /* blocks that must stay free besides ours (~7.7 MB for the still) */

/* Current task id: TCB = **(kernel block + 0x60), id = u16 TCB + 0x10 (k_ref_tsk 0x6E68432A). */
#define LC_KCB 0x8FF00004u

/*
 * IPU register shadow (img_ctx 0x6E74CA56): 0x60564050 if IMG_CTRL ulFlag bit 2, else
 * 0x6055FBF0 + 0x2230 * (core * 2 + engine2). The LUT loaders 0x6E74C767 / 0x6E74C8DC take the
 * Y tone table pointer from ctx+0x5AC (path A) / ctx+0x11C4 (path B) and the packed RGB gamma from
 * ctx+0x5D8 / ctx+0x11C8.
 */
#define LC_CTX_BASE 0x6055FBF0u
#define LC_CTX_ALT 0x60564050u
#define LC_CTX_SIZE 0x2230u
#define LC_CTX_YA 0x5ACu
#define LC_CTX_YB 0x11C4u
#define LC_YTAB_BYTES 0x200u

/* IMG_CTRL (0xC8 bytes), field names from the firmware's own debug dump (mov_dbg.c). */
#define IC_SIZE 0xC8u
#define IC_R1ADDR 0x00u
#define IC_W1ADDR 0x10u
#define IC_FLAG 0x54u
#define IC_PARAM 0x60u
#define IC_IN_MEM_HW 0x6Au
#define IC_IN_USE_HW 0x6Cu
#define IC_IN_USE_VW 0x6Eu
#define IC_IN_AREA_HW 0x72u
#define IC_IN_AREA_VW 0x74u
#define IC_IN_AREA_HC 0x76u
#define IC_IN_AREA_VC 0x78u
#define IC_OUT_WHOLE_HW 0x8Eu
#define IC_OUT_WHOLE_VW 0x90u
#define IC_OUT_AREA_HW 0x9Cu
#define IC_OUT_AREA_VW 0x9Eu
#define IC_ZOOM 0xB2u
#define IC_MODE 0xC4u
#define IC_MODE_LV 0x07u
#define IC_MODE_RESIZE 0x15u
#define IC_FLAG_ALTCTX 0x4u
#define IC_FLAG_ENGINE2 0x10000000u

/* IMG_PARAM (fields up to +0x5D). */
#define IP_SIZE 0x80u
#define IP_FLAG 0x14u
#define IP_GAIN_R 0x2Au
#define IP_GAIN_B 0x30u
#define IP_SV 0x3Eu
#define IP_BV 0x42u
#define IP_SHARP 0x4Cu
#define IP_CONTRAST 0x4Du
#define IP_TONE 0x4Eu
#define IP_EXPOSE 0x4Fu
#define IP_SAT 0x50u
#define IP_TCP 0x57u
#define IP_TYPE 0x5Cu

#define LC_DISP_STRIDE 1600u /* bytes */
#define LC_DISP_W 540u
#define LC_DISP_H 480u
#define LC_DISP_ROW (LC_DISP_W * 2u)
#define LC_FD_W 640u
#define LC_FD_H 480u
#define LC_RSZ_W 320u
#define LC_RSZ_H 240u

/* RECFILE layout (each part a multiple of 64 bytes). */
#define LC_R_OFF 0u
#define LC_R_BYTES (LC_RSZ_W * LC_RSZ_H * 2u)
#define LC_D_OFF (LC_R_OFF + LC_R_BYTES)
#define LC_D_BYTES (LC_DISP_ROW * LC_DISP_H)
#define LC_F_OFF (LC_D_OFF + LC_D_BYTES)
#define LC_F_BYTES (LC_FD_W * LC_FD_H)
#define LC_BUF_BYTES (LC_F_OFF + LC_F_BYTES)
#define LC_BUF_BLOCKS ((LC_BUF_BYTES + LC_RF_BLOCK - 1u) / LC_RF_BLOCK)

#define LC_HEAD_MAGIC 0x3148434Cu /* "LCH1" */
#define LC_SLOT_MAGIC 0x3153434Cu /* "LCS1" */
#define LC_VERSION 2u
#define LC_VALID_R 0x1u
#define LC_VALID_D 0x2u
#define LC_VALID_F 0x4u
#define LC_FD_SRC_PUB 1u /* publish word at the copy */
#define LC_FD_SRC_LAST 2u /* last publish seen by the task before the press */
#define LC_NOT_RUN 0xFFFFFFFFu

enum lc_kind { LC_KIND_NONE, LC_KIND_LV, LC_KIND_STILL };
enum lc_phase { LC_IDLE, LC_WAIT, LC_DONE };

/* File P: one head, then LC_SLOTS slots (slot 0 = live view, 1.. = engine-1 jobs). Little endian. */
struct lc_head {
	uint32_t magic, version, seq, slots, slot_size;
	uint32_t disp, rsz_disp, fd, lock;
	uint32_t disp_us, disp_flips_tries, fd_us;
	uint32_t rsz_status, rsz_us;
	uint32_t extra_st_n; /* presses during the capture << 16 | still slots filled */
	uint32_t cap_ms;
	uint32_t valid; /* LC_VALID_* */
	uint32_t disp_rows; /* rows of D copied while the buffer could not be rewritten */
	uint32_t fd_src, fd_age_us, fd_changes;
	uint32_t fd_press; /* publish word at the FULL event */
};

struct lc_slot {
	uint32_t magic, kind, seq, t_us, dt_us, ra, task, status;
	uint32_t ctx, ctrl_addr, param_addr, ya_ptr, yb_ptr, lock_after, flag, mode;
	uint8_t ctrl[IC_SIZE];
	uint8_t param[IP_SIZE];
	uint8_t ya[LC_YTAB_BYTES];
	uint8_t yb[LC_YTAB_BYTES];
	uint8_t ctx_copy[LC_CTX_SIZE];
};

struct lc_pfile {
	struct lc_head head;
	struct lc_slot slot[LC_SLOTS];
};

struct lc_state {
	struct lc_pfile pf __attribute__((aligned(64)));
	uint8_t ctrl[IC_SIZE] __attribute__((aligned(4)));
	uint8_t param[IP_SIZE] __attribute__((aligned(4)));
	uint32_t buf; /* RECFILE run, uncached alias */
	/* events */
	volatile uint32_t presses, press_t, fd_press;
	/* last FD publish seen by the task loop */
	uint32_t fd_last, fd_last_t;
	/* hook interface: armed by the task, cleared by the hook */
	volatile uint8_t lv_arm, st_arm, grabbing;
	volatile uint32_t st_n;
	uint32_t seq;
	/* task */
	enum lc_phase phase;
	uint32_t handled, last_busy_t, skipped;
	uint8_t seen_busy;
	uint32_t rsz_errors;
	uint8_t rsz_dead;
	uint32_t sets, write_fail;
};

static struct lc_state lc;
FENYKL_SHARED uint32_t lc_cpu1;

FENYKL_LOG_MODULE(lc_log, "lvcal", FENYKL_LOG_DEBUG);

static uint32_t lc_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

static uint8_t lc_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t lc_now(void)
{
	return FENYKL_LOG_TIMER_NOW();
}

static uint32_t lc_since(uint32_t t)
{
	return FENYKL_LOG_ELAPSED(t, FENYKL_LOG_TIMER_NOW());
}

static uint32_t lc_cached(uint32_t a)
{
	return (a & 0x3FFFFFFFu) | 0x40000000u;
}

/* DRAM (cached 0x6..., uncached 0xA...), word aligned, inside the 256 MB window. */
static bool lc_dram(uint32_t a, uint32_t bytes)
{
	uint32_t c = lc_cached(a);

	return (a & 3u) == 0 && ((a >> 28) == 0x6u || (a >> 28) == 0xAu) && c + bytes <= 0x70000000u;
}

/* Word copy; the SDK memcpy is a byte loop. */
static void lc_words(void *dst, uint32_t src, uint32_t bytes)
{
	const volatile uint32_t *s = (const volatile uint32_t *)src;
	uint32_t *d = dst;

	for (uint32_t i = 0; i < bytes / 4u; i++)
		d[i] = s[i];
}

static uint32_t lc_task_id(void)
{
	uint32_t kcb = lc_rd32(LC_KCB + 4u * FENYKL_CORE_ID());
	uint32_t p, tcb;

	if (!kcb)
		return 0xFF;
	p = lc_rd32(kcb + 0x60u);
	if (!p)
		return 0xFF;
	tcb = lc_rd32(p);
	if (!tcb)
		return 0xFF;
	return *(volatile uint16_t *)(tcb + 0x10u) & 0x7Fu;
}

static void lc_ytab(uint8_t *dst, uint32_t p)
{
	if (lc_dram(p, LC_YTAB_BYTES))
		lc_words(dst, lc_cached(p), LC_YTAB_BYTES);
}

/* Hook side: copies one job's parameters and shadow into a slot (CPU0, after the job returned). */
static void lc_grab(struct lc_slot *s, enum lc_kind kind, uint32_t c, uint32_t flag, uint32_t mode,
		    uint32_t st, uint32_t t0, uint32_t ra)
{
	uint32_t eng2 = (flag & IC_FLAG_ENGINE2) != 0;
	uint32_t ctx = (flag & IC_FLAG_ALTCTX) ? LC_CTX_ALT : LC_CTX_BASE + LC_CTX_SIZE * eng2;
	uint32_t p = lc_rd32(c + IC_PARAM);

	s->magic = LC_SLOT_MAGIC;
	s->seq = lc.seq;
	s->t_us = lc_since(lc.press_t);
	s->dt_us = lc_since(t0);
	s->ra = ra;
	s->task = lc_task_id();
	s->status = st;
	s->ctx = ctx;
	s->ctrl_addr = c;
	s->param_addr = p;
	s->flag = flag;
	s->mode = mode;
	lc_words(s->ctrl, c, IC_SIZE);
	if (lc_dram(p, IP_SIZE))
		lc_words(s->param, lc_cached(p), IP_SIZE);
	lc_words(s->ctx_copy, ctx, LC_CTX_SIZE);
	s->ya_ptr = lc_rd32(ctx + LC_CTX_YA);
	s->yb_ptr = lc_rd32(ctx + LC_CTX_YB);
	lc_ytab(s->ya, s->ya_ptr);
	lc_ytab(s->yb, s->yb_ptr);
	/* the engine's lock bit set again: another job may have rebuilt the shadow during the copy */
	s->lock_after = lc_rd32(LC_IPU_LOCK);
	s->kind = kind;
}

static unsigned int lc_img_exec(void *ctrl, uint32_t ra)
{
	uint32_t c = (uint32_t)ctrl, flag, mode, t0, st, n;

	if (FENYKL_CORE_ID() != 0) {
		(*FENYKL_SHARED_PTR(lc_cpu1))++;
		return fw_orig_img_exec(ctrl);
	}
	if (!ctrl || ctrl == lc.ctrl || !(lc.lv_arm || lc.st_arm) || !lc_dram(c, IC_SIZE))
		return fw_orig_img_exec(ctrl);
	c = lc_cached(c);
	flag = lc_rd32(c + IC_FLAG);
	mode = lc_rd8(c + IC_MODE);
	t0 = lc_now();
	st = fw_orig_img_exec(ctrl);

	lc.grabbing = 1;
	if ((flag & IC_FLAG_ENGINE2) && mode == IC_MODE_LV && lc.lv_arm) {
		lc_grab(&lc.pf.slot[0], LC_KIND_LV, c, flag, mode, st, t0, ra);
		lc.lv_arm = 0;
	} else if (!(flag & IC_FLAG_ENGINE2) && lc.st_arm) {
		n = lc.st_n;
		lc_grab(&lc.pf.slot[1u + n], LC_KIND_STILL, c, flag, mode, st, t0, ra);
		lc.st_n = n + 1u;
		if (n + 1u >= LC_STILL_SLOTS)
			lc.st_arm = 0;
	}
	lc.grabbing = 0;
	return st;
}

/*
 * Bound by hand instead of FENYKL_HOOK to get the caller: the generated entry stub calls
 * fenykl_hookwrap_img_exec(r, s) with r = D0 D1 A0 A1 and s = the caller's (4,SP); the firmware CALL
 * stored the return address at the caller's (0,SP), i.e. s[-1].
 */
unsigned int fenykl_hookwrap_img_exec(const uint32_t *r, const uint32_t *s);
unsigned int fenykl_hookwrap_img_exec(const uint32_t *r, const uint32_t *s)
{
	return lc_img_exec((void *)r[2], s[-1]);
}

static enum fenykl_event_action lc_on_event(struct fenykl_event *ev, void *arg)
{
	(void)arg;
	if (ev->event != (FENYKL_EVENT_KEY_PRESS | 0x0101u))
		return FENYKL_EVENT_PASS;
	lc.press_t = lc_now();
	lc.fd_press = lc_rd32(LC_FD_PUB);
	lc.presses++;
	fenykl_log_info(&lc_log, "FULL %u", lc.presses - 1u);
	return FENYKL_EVENT_PASS;
}

static struct fenykl_event_handler lc_handler = {
	.name = "lvcal", .fn = lc_on_event, .prio = 0, .enabled = true,
};

static uint32_t lc_th_busy(void)
{
	uint32_t busy = 0;

	for (uint32_t i = 0; i < LC_TH_N; i++)
		busy += lc_rd8(LC_TH + i * LC_TH_SIZE + LC_TH_PS) == LC_PS_BUSY;
	return busy;
}

/* --- press-time captures (task) --- */

static uint32_t lc_wait_flip(void)
{
	uint32_t a = lc_rd32(LC_IMG1);

	for (int i = 0; i < LC_FLIP_WAIT_MS; i++) {
		uint32_t now;

		fw_OlyTaskSleep(1);
		now = lc_rd32(LC_IMG1);
		if (now != a)
			return now;
	}
	return a;
}

/* IMG_CTRL like the stock YC resize job of REC_IMAGE (0x6E8DF67C), engine 1 (ulFlag 0). */
static void lc_build(uint32_t disp)
{
	uint8_t *c = lc.ctrl;

	memset(c, 0, sizeof(lc.ctrl));
	*(uint32_t *)(c + IC_R1ADDR) = (uint32_t)FENYKL_UNCACHED(disp);
	*(uint32_t *)(c + IC_W1ADDR) = lc.buf + LC_R_OFF;
	*(uint32_t *)(c + IC_PARAM) = (uint32_t)lc.param;
	*(uint16_t *)(c + IC_IN_MEM_HW) = LC_DISP_STRIDE / 2u;
	*(uint16_t *)(c + IC_IN_USE_HW) = LC_DISP_W;
	*(uint16_t *)(c + IC_IN_USE_VW) = LC_DISP_H;
	*(uint16_t *)(c + IC_IN_AREA_HW) = LC_DISP_W;
	*(uint16_t *)(c + IC_IN_AREA_VW) = LC_DISP_H;
	*(uint16_t *)(c + IC_IN_AREA_HC) = LC_DISP_W / 2u;
	*(uint16_t *)(c + IC_IN_AREA_VC) = LC_DISP_H / 2u;
	*(uint16_t *)(c + IC_OUT_WHOLE_HW) = LC_RSZ_W;
	*(uint16_t *)(c + IC_OUT_WHOLE_VW) = LC_RSZ_H;
	*(uint16_t *)(c + IC_OUT_AREA_HW) = LC_RSZ_W;
	*(uint16_t *)(c + IC_OUT_AREA_VW) = LC_RSZ_H;
	*(uint16_t *)(c + IC_ZOOM) = 100;
	c[IC_MODE] = IC_MODE_RESIZE;
}

static void lc_resize(struct lc_head *h, uint32_t disp)
{
	uint32_t t0, st;

	h->rsz_status = LC_NOT_RUN;
	h->rsz_disp = disp;
	if (!lc_dram(disp, LC_DISP_STRIDE * LC_DISP_H)) {
		fenykl_log_warn(&lc_log, "RSZ %u skipped: img1 %x not in DRAM", lc.seq, disp);
		return;
	}
	if (lc.rsz_dead || (lc_rd32(LC_IPU_LOCK) & LC_LOCK_E1) ||
	    lc_since(lc.press_t) > LC_RSZ_LATE_US) {
		fenykl_log_info(&lc_log, "RSZ %u skipped: dead %u lock %x after %u us", lc.seq,
				lc.rsz_dead, lc_rd32(LC_IPU_LOCK), lc_since(lc.press_t));
		return;
	}
	lc_build(disp);
	t0 = lc_now();
	st = fw_img_exec(lc.ctrl);
	h->rsz_us = lc_since(t0);
	h->rsz_status = st;
	if (!st) {
		h->valid |= LC_VALID_R;
		return;
	}
	lc.rsz_errors++;
	fenykl_log_err(&lc_log, "resize status %x after %u us", st, h->rsz_us);
	if (h->rsz_us > LC_SLOW_US || lc.rsz_errors >= LC_MAX_ERRORS) {
		fenykl_log_err(&lc_log, "resize stopped (errors %u)", lc.rsz_errors);
		lc.rsz_dead = 1;
	}
}

/*
 * Copies rows of words from src (cached alias, between two purges) to dst (cached alias of the
 * RECFILE run; the second purge writes it back) and returns how often the word at watch changed,
 * checked per row.
 */
static uint32_t lc_copy_rows(uint32_t dst, uint32_t src, uint32_t pitch, uint32_t row, uint32_t rows,
			     uint32_t watch)
{
	uint32_t last = lc_rd32(watch), flips = 0;

	fw_dcache_purge_all_irqsafe();
	for (uint32_t y = 0; y < rows; y++) {
		uint32_t now;

		lc_words((void *)(dst + y * row), src + y * pitch, row);
		now = lc_rd32(watch);
		flips += now != last;
		last = now;
	}
	fw_dcache_purge_all_irqsafe();
	return flips;
}

/* The shown buffer is next written by the pass after the second flip: whole if <= 1 flip. */
static void lc_copy_disp(struct lc_head *h, uint32_t disp)
{
	uint32_t flips = 0, t = 0, tries;

	for (tries = 1; tries <= LC_TRIES; tries++) {
		if (tries > 1)
			disp = lc_wait_flip();
		if (!lc_dram(disp, LC_DISP_STRIDE * LC_DISP_H)) {
			fenykl_log_warn(&lc_log, "D %u: img1 %x not in DRAM (try %u)", lc.seq, disp, tries);
			break;
		}
		h->disp = disp;
		t = lc_now();
		flips = lc_copy_rows(lc_cached(lc.buf + LC_D_OFF), lc_cached(disp), LC_DISP_STRIDE,
				     LC_DISP_ROW, LC_DISP_H, LC_IMG1);
		t = lc_since(t);
		if (flips <= 1) {
			h->valid |= LC_VALID_D;
			h->disp_rows = LC_DISP_H;
			break;
		}
	}
	h->disp_us = t;
	h->disp_flips_tries = flips << 16 | (tries > LC_TRIES ? LC_TRIES : tries);
}

/* FD slot: the publish word, else the last one the task loop saw shortly before the press. */
static void lc_copy_fd(struct lc_head *h)
{
	uint32_t fd = lc_rd32(LC_FD_PUB), t;

	h->fd_press = lc.fd_press;
	h->fd_src = LC_FD_SRC_PUB;
	if (!lc_dram(fd, LC_F_BYTES) && lc.fd_last &&
	    lc_since(lc.fd_last_t) <= lc_since(lc.press_t) + LC_FD_MAX_AGE_US) {
		fd = lc.fd_last;
		h->fd_src = LC_FD_SRC_LAST;
		h->fd_age_us = lc_since(lc.fd_last_t);
	}
	h->fd = fd;
	if (!lc_dram(fd, LC_F_BYTES)) {
		h->fd_src = 0;
		fenykl_log_warn(&lc_log, "F %u: FD slot %x not in DRAM, last %x", lc.seq, fd, lc.fd_last);
		return;
	}
	t = lc_now();
	h->fd_changes = lc_copy_rows(lc_cached(lc.buf + LC_F_OFF), lc_cached(fd), LC_FD_W, LC_FD_W, LC_FD_H,
				     LC_FD_PUB);
	h->fd_us = lc_since(t);
	if (h->fd_changes <= LC_FD_MAX_CHANGES)
		h->valid |= LC_VALID_F;
}

/* Task loop: remembers the last FD publish (the word read 0 at every press of the first run). */
static void lc_track_fd(void)
{
	uint32_t fd = lc_rd32(LC_FD_PUB);

	if (fd && fd != lc.fd_last) {
		lc.fd_last = fd;
		lc.fd_last_t = lc_now();
	}
}

static void lc_press(uint32_t seq)
{
	struct lc_head *h = &lc.pf.head;
	uint32_t disp;

	memset(h, 0, sizeof(*h));
	for (uint32_t i = 0; i < LC_SLOTS; i++)
		lc.pf.slot[i].kind = LC_KIND_NONE;
	h->magic = LC_HEAD_MAGIC;
	h->version = LC_VERSION;
	h->seq = seq;
	h->slots = LC_SLOTS;
	h->slot_size = sizeof(struct lc_slot);
	h->lock = lc_rd32(LC_IPU_LOCK);
	lc.seq = seq;
	lc.st_n = 0;
	lc.lv_arm = 1;
	lc.st_arm = 1;

	disp = lc_wait_flip();
	lc_resize(h, disp);
	lc_copy_disp(h, disp);
	lc_copy_fd(h);
	fenykl_log_info(&lc_log, "P %u disp %x fd %x lock %x", seq, h->disp, h->fd, h->lock);
	fenykl_log_info(&lc_log, "P %u rsz st %x us %u", seq, h->rsz_status, h->rsz_us);
	fenykl_log_info(&lc_log, "P %u D us %u flips/tries %x rows %u", seq, h->disp_us,
			h->disp_flips_tries, h->disp_rows);
	fenykl_log_info(&lc_log, "P %u F src %u age %u us chg %u", seq, h->fd_src, h->fd_age_us,
			h->fd_changes);
	fenykl_log_info(&lc_log, "P %u F us %u press %x valid %x", seq, h->fd_us, h->fd_press, h->valid);
}

/* --- card writes (task, after the still is on the card) --- */

/* Waits while a still is in progress; 0 = free. */
static int lc_wait_idle(void)
{
	for (uint32_t t = lc_now(); lc_th_busy(); fw_OlyTaskSleep(10)) {
		if (lc_since(t) > LC_CAP_MAX_US)
			return -4;
	}
	return 0;
}

static int lc_write(char kind, uint32_t src, uint32_t n)
{
	char name[16] = "C:\\LC___.BIN";
	void *fp;
	int r = 0;

	name[5] = (char)('0' + lc.seq / 10u);
	name[6] = (char)('0' + lc.seq % 10u);
	name[7] = kind;
	if (!fenykl_card_io_allowed())
		return -5;
	if (lc_wait_idle())
		return -4;
	fp = fw_rpc_fopen(name, "w");
	if (!fp)
		return -1;
	for (uint32_t o = 0; !r && o < n; o += LC_CHUNK) {
		uint32_t c = n - o < LC_CHUNK ? n - o : LC_CHUNK;

		if (lc_wait_idle())
			r = -4;
		else if (fw_rpc_fwrite((const void *)(src + o), 1, c, fp) != (int)c)
			r = -2;
	}
	if (fw_rpc_fclose(fp) == -1 && !r)
		r = -3;
	return r;
}

static int16_t lc_s16(const uint8_t *p, uint32_t off)
{
	return (int16_t)(p[off] | p[off + 1] << 8);
}

static void lc_log_param(void)
{
	const struct lc_slot *s = &lc.pf.slot[0];
	const uint8_t *p = s->param;

	if (s->kind != LC_KIND_LV) {
		fenykl_log_warn(&lc_log, "S %u: no live-view pass captured", lc.seq);
		return;
	}
	fenykl_log_info(&lc_log, "LV %u t %u ctx %x ya %x", lc.seq, s->t_us, s->ctx, s->ya_ptr);
	fenykl_log_info(&lc_log, "LV %u yb %x flag %x lk %x", lc.seq, s->yb_ptr, s->flag,
			s->lock_after);
	fenykl_log_info(&lc_log, "LV pflag %x ex %x tcp %x type %x", *(const uint32_t *)(p + IP_FLAG),
			p[IP_EXPOSE], p[IP_TCP], p[IP_TYPE]);
	fenykl_log_info(&lc_log, "LV shp %d con %d tone %d sat %d", (int8_t)p[IP_SHARP],
			(int8_t)p[IP_CONTRAST], (int8_t)p[IP_TONE], (int8_t)p[IP_SAT]);
	fenykl_log_info(&lc_log, "LV gain R %u B %u sv %d bv %d", lc_s16(p, IP_GAIN_R),
			lc_s16(p, IP_GAIN_B), lc_s16(p, IP_SV), lc_s16(p, IP_BV));
}

static void lc_log_still(void)
{
	for (uint32_t i = 1; i < LC_SLOTS; i++) {
		const struct lc_slot *s = &lc.pf.slot[i];

		if (s->kind != LC_KIND_STILL)
			continue;
		fenykl_log_info(&lc_log, "S%u mode %x task %x st %x", i, s->mode, s->task, s->status);
		fenykl_log_info(&lc_log, "S%u t %u dt %u ra %x", i, s->t_us, s->dt_us, s->ra);
		fenykl_log_info(&lc_log, "S%u ya %x yb %x lk %x", i, s->ya_ptr, s->yb_ptr,
				s->lock_after);
	}
}

static void lc_finish(void)
{
	struct lc_head *h = &lc.pf.head;
	int rp, rr = 0, rd = 0, rf = 0;

	lc.lv_arm = 0;
	lc.st_arm = 0;
	while (lc.grabbing)
		fw_OlyTaskSleep(1);
	h->extra_st_n = (lc.presses - lc.seq - 1u) << 16 | lc.st_n;
	h->cap_ms = lc_since(lc.press_t) / 1000u;
	lc_log_param();
	lc_log_still();
	fw_dcache_purge_all_irqsafe();
	rp = lc_write('P', (uint32_t)FENYKL_UNCACHED(&lc.pf), sizeof(lc.pf));
	if (h->valid & LC_VALID_R)
		rr = lc_write('R', lc.buf + LC_R_OFF, LC_R_BYTES);
	if (h->valid & LC_VALID_D)
		rd = lc_write('D', lc.buf + LC_D_OFF, LC_D_BYTES);
	if (h->valid & LC_VALID_F)
		rf = lc_write('F', lc.buf + LC_F_OFF, LC_F_BYTES);
	lc.sets++;
	if (rp || rr || rd || rf)
		lc.write_fail++;
	fenykl_log_info(&lc_log, "W %u P %d R %d D %d", lc.seq, rp, rr, rd);
	fenykl_log_info(&lc_log, "W %u F %d cap %u ms", lc.seq, rf, h->cap_ms);
	fenykl_log_info(&lc_log, "W %u st_n %u extra %u", lc.seq, lc.st_n, lc.presses - lc.seq - 1u);
}

/* Capture indicator: a still is in progress while a take handle is busy, and from the press until
 * one appears (at most LC_PRESS_HOLD_US). Returns 1 when the set may be written. */
static int lc_capture_done(void)
{
	uint32_t busy = lc_th_busy();

	if (busy) {
		lc.seen_busy = 1;
		lc.last_busy_t = lc_now();
		return 0;
	}
	if (!lc.seen_busy && lc_since(lc.press_t) < LC_PRESS_HOLD_US)
		return 0;
	return lc_since(lc.last_busy_t) >= LC_SETTLE_US;
}

static void lc_step(void)
{
	uint32_t presses = lc.presses;

	switch (lc.phase) {
	case LC_IDLE:
		if (presses == lc.handled)
			return;
		if (presses - lc.handled > 1u) {
			lc.skipped += presses - lc.handled - 1u;
			fenykl_log_warn(&lc_log, "presses %u..%u skipped", lc.handled, presses - 2u);
		}
		if (presses - 1u >= LC_PRESSES) {
			fenykl_log_info(&lc_log, "press limit reached: no more dumps");
			lc.phase = LC_DONE;
			return;
		}
		lc.seen_busy = 0;
		lc.last_busy_t = lc.press_t;
		lc_press(presses - 1u);
		lc.phase = LC_WAIT;
		return;
	case LC_WAIT:
		if (lc_capture_done()) {
			lc_finish();
			lc.handled = lc.presses;
			if (lc.handled - lc.seq - 1u)
				lc.skipped += lc.handled - lc.seq - 1u;
			lc.phase = LC_IDLE;
			return;
		}
		if (lc_since(lc.press_t) > LC_CAP_MAX_US) {
			lc.lv_arm = 0;
			lc.st_arm = 0;
			fenykl_log_err(&lc_log, "set %u dropped: still not on the card after %u s", lc.seq,
				       LC_CAP_MAX_US / 1000000u);
			lc.handled = lc.presses;
			lc.phase = LC_IDLE;
		}
		return;
	case LC_DONE:
		return;
	}
}

FENYKL_TASK(lc_main);
void lc_main(void)
{
	uint32_t run = 0, free = 0, sum_t;
	int r;

	fw_OlyTaskSleep(LC_START_MS);
	memset(lc.param, 0, sizeof(lc.param));
	for (uint32_t i = 0; i < LC_RF_BLOCKS && run < LC_BUF_BLOCKS; i++)
		run = lc_rd8(LC_RF_MAP + i) ? 0 : run + 1u;
	for (uint32_t i = 0; i < LC_RF_BLOCKS; i++)
		free += !lc_rd8(LC_RF_MAP + i);
	if (run < LC_BUF_BLOCKS || free < LC_BUF_BLOCKS + LC_RF_MARGIN) {
		fenykl_log_err(&lc_log, "RECFILE: run %u free %u, need %u + %u", run, free,
			       LC_BUF_BLOCKS, LC_RF_MARGIN);
		return;
	}
	lc.buf = fw_recfile_alloc(LC_BUF_BYTES);
	if (!lc_dram(lc.buf, LC_BUF_BYTES)) {
		fenykl_log_err(&lc_log, "recfile_alloc returned %x", lc.buf);
		return;
	}
	lc.buf = (uint32_t)FENYKL_UNCACHED(lc.buf);
	r = fenykl_hook_install(&fenykl_hook_img_exec);
	if (r) {
		fenykl_log_err(&lc_log, "img_exec hook install failed %d", r);
		return;
	}
	r = fenykl_event_add(&lc_handler);
	if (!r)
		r = fenykl_event_install();
	if (r) {
		fenykl_log_err(&lc_log, "event layer failed %d", r);
		return;
	}
	lc.handled = lc.presses;
	fenykl_log_info(&lc_log, "lvcal ready: buf %x blocks %u free %u slot %u", lc.buf,
			LC_BUF_BLOCKS, free, sizeof(struct lc_slot));
	sum_t = lc_now();
	for (;;) {
		lc_track_fd();
		lc_step();
		if (lc_since(sum_t) > 60000000u) {
			fenykl_log_info(&lc_log, "SUM sets %u wfail %u skipped %u cpu1 %u", lc.sets,
					lc.write_fail, lc.skipped, *FENYKL_SHARED_PTR(lc_cpu1));
			fenykl_log_info(&lc_log, "SUM fd %x last %x age %u ms img1 %x", lc_rd32(LC_FD_PUB),
					lc.fd_last, lc_since(lc.fd_last_t) / 1000u, lc_rd32(LC_IMG1));
			sum_t = lc_now();
		}
		fw_OlyTaskSleep(1);
	}
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config lcfg = { 0 };
	int r = fenykl_log_init(&lcfg);
	void *stack;

	fenykl_log_task_start();
	fenykl_log_info(&lc_log, "lvcal start, load %x size %x, log init %d", load_base, image_size, r);
	stack = fw_malloc(LC_STACK_SIZE);
	if (!stack) {
		fenykl_log_err(&lc_log, "no stack");
		return;
	}
	if (fw_OlyTaskCreate("LVCAL", lc_main_task, 2, 0, LC_PRIO, LC_STACK_SIZE, stack) < 0)
		fenykl_log_err(&lc_log, "task not created");
}
