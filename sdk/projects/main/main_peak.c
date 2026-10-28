/*
 * main: option "Peaking" (Off / Low / Medium / High): red focus peaking edges over the still live
 * view on the rear LCD.
 *
 * Line pass: after a stock live-view pass returns in img_exec (main_img_hook.c, MOV_TASK), the hook
 * runs one more pass on a copy of its IMG_CTRL (fenykl_peak_line_ctrl) into a 320x240 line image;
 * the img_hw_start hook patches that pass's shadow (fenykl_peak_shadow_patch): Y LUTs all 0, edge
 * coring by level, gain and limits at maximum, so the image is the edge component on black. One
 * line pass per live-view pass at 30 fps (two passes span MAIN_PEAK_SPAN2_US or more), every
 * second one at 60 fps (stock 8 ms + line 9 ms fill the 16.7 ms frame); none while the previous
 * line image is not taken. The two-pass span stays on the sensor's rate as long as MOV_TASK keeps
 * up, so it does not follow the delay a line pass adds to the next pass.
 *
 * Overlay: the hook wakes the peaking task, which runs above MOV_TASK, so it marks the line image
 * (main_peak_mark.c) into one of two 320x240 overlay buffers and flips the display's overlay layer
 * to it before MOV_TASK shows the live-view frame the line pass came from. Lit pixels are written
 * uncached and listed per buffer, and cleared from that list before the buffer is drawn again.
 *
 * Shown only in still live view on the rear LCD, not magnified, not during a half press or after a
 * live-view layout change (those two only after MAIN_PEAK_SOFT_US: a short one freezes the edges);
 * the memory is RECFILE's, lent while shown. An error turns peaking off for the boot.
 */
#include "main.h"
#include "main_peak.h"
#include "main_peak_mark.h"

#if !FENYKL_HAS_SHOT || !FENYKL_HAS_AFC || !FENYKL_HAS_IMG_EXEC
#error "peak needs the shot, afc and img_exec groups of body.h"
#endif

_Static_assert(FENYKL_PEAK_LN_W == MAIN_PEAK_PITCH, "line image pitch");

#define MAIN_PEAK_PRIO 3		/* MOV_TASK is 4 */
#define MAIN_PEAK_STACK 0x2000u
#define MAIN_PEAK_TICK_MS 20
#define MAIN_PEAK_STAT_US 2000000u
#define MAIN_PEAK_LV_STALE_US 200000u	/* no live-view pass for this long: hide */
#define MAIN_PEAK_SOFT_US 300000u
#define MAIN_PEAK_GAP_US 2000u		/* a stock pass this soon after the last line pass: skip */
#define MAIN_PEAK_SPAN2_US 50000u	/* two live-view passes this far apart: 30 fps or slower */
#define MAIN_PEAK_HIDE_WAIT_MS 6000	/* a line pass in flight: the IPU timeout is 500 ticks */
#define MAIN_PEAK_WATCH_US 60000u
#define MAIN_PEAK_WATCH_EVERY 8u	/* debug level: frames per latch measurement */
#define MAIN_PEAK_LIT_MAX 8192u
#define MAIN_PEAK_ALIGN 256u
#define MAIN_PEAK_LN_BYTES (FENYKL_PEAK_LN_W * FENYKL_PEAK_LN_H)
#define MAIN_PEAK_OVL_BYTES (4u * MAIN_PEAK_LN_BYTES)
#define MAIN_PEAK_BM_BYTES (4u * MAIN_PEAK_BM_WORDS(FENYKL_PEAK_LN_H))
#define MAIN_PEAK_LIST_BYTES (4u * MAIN_PEAK_LIT_MAX)

enum { MAIN_PEAK_HIDDEN, MAIN_PEAK_SHOWN, MAIN_PEAK_HIDING };

/* Stages of a peaking frame; LV end is the end of the stock pass. */
enum {
	MAIN_PEAK_T_START, MAIN_PEAK_T_LINE, MAIN_PEAK_T_WAKE, MAIN_PEAK_T_CLEAR, MAIN_PEAK_T_MARK,
	MAIN_PEAK_T_FLIP, MAIN_PEAK_T_LV, MAIN_PEAK_T_OVL, MAIN_PEAK_NT
};

static const char *const main_peak_t_name[MAIN_PEAK_NT] = {
	"LV end -> line", "line pass", "line end -> task", "purge+clear", "mark", "LV end -> flip",
	"LV end -> LV shown", "LV end -> edges shown",
};

struct main_peak_lvl {
	uint8_t coring;
	uint8_t thr;			/* 1..128 */
};

/* Off, Low, Medium, High */
static const struct main_peak_lvl main_peak_lvls[] = {
	{ 0, 0 }, { 50, 56 }, { 40, 40 }, { 40, 24 },
};

/* Hook counters; the task takes them every MAIN_PEAK_STAT_US. */
struct main_peak_hk {
	uint32_t lv, lv_us_sum, lv_us_max, line, wake_err, stock_err;
	uint32_t skip_layout, skip_af, skip_clip, skip_rate, skip_busy;
};

/* The line pass the task has not taken, written by the hook before fresh. */
struct main_peak_frame {
	uint32_t t_lv, t_start, t_end;
	uint32_t out1;			/* the stock pass's display output, physical */
};

struct main_peak_acc {
	uint32_t n, sum, max;
};

/* Task side, per MAIN_PEAK_STAT_US. */
struct main_peak_stats {
	struct main_peak_acc t[MAIN_PEAK_NT];
	uint32_t frames, lit_sum, lit_max, capped, lv_missed, ovl_missed;
};

struct main_peak_state {
	int task;
	volatile uint8_t coring;	/* read by the hooks */
	volatile uint8_t state;
	volatile bool dead;		/* an error: off for the boot */
	volatile uint32_t err_status;

	/* hook side */
	volatile bool in_pass;		/* the hook runs a line pass on the overlay's buffers */
	volatile bool line;		/* the line pass is in img_exec: patch at img_hw_start */
	volatile bool fresh;		/* frame and the line image hold a pass the task has not taken */
	volatile bool have_lv;
	volatile bool layout_ok;	/* the last live-view pass has the shown layout */
	volatile uint32_t lv_last;	/* timer at the end of the last live-view pass */
	uint32_t lv_prev[2];		/* timer at the start of the last two live-view passes */
	uint32_t last_end;		/* timer at the end of the last line pass */
	uint32_t lv_div;		/* live-view passes since the last line pass */
	struct main_peak_frame frame;
	struct main_peak_hk hk;
	uint32_t lv_copy[FENYKL_PEAK_CTRL_SIZE / 4];	/* the last live-view pass while hidden */
	uint32_t ctrl[FENYKL_PEAK_CTRL_SIZE / 4];	/* the line pass */
	uint32_t saved_shadow[FENYKL_PEAK_SHADOW_N];
	uint16_t lut_zero[FENYKL_PEAK_LUT_N];

	/* task side */
	uint32_t stock[FENYKL_PEAK_CTRL_SIZE / 4];	/* the live-view pass of the shown layout */
	struct fenykl_peak_layout lay;
	struct fenykl_ovl ovl;
	uint32_t alloc, out1, ln, disp, lum, lists, bm;
	unsigned int back;		/* the overlay buffer drawn next */
	uint32_t n_lit[2];
	unsigned int level;		/* shown level */
	bool soft;			/* a soft block holds since soft_t */
	uint32_t soft_t;
	uint32_t stat_t;
	uint32_t watch_div;
	uint32_t shows, frames, lv_total, line_total, wake_err_total;
	struct main_peak_stats ps;
};

static struct main_peak_state main_peak;

static FENYKL_LOG_MODULE(main_peak_log, "peak", FENYKL_LOG_INFO);

static const char *const main_peak_names[] = { "Off", "Low", "Medium", "High" };

static void main_peak_apply(const struct fenykl_cfg_opt *o, int32_t old);

FENYKL_CFG_ENUM(main_peak_level, "main.peak.level", "Peaking", &main_grp_image, 0, main_peak_names,
		.on_change = main_peak_apply);

static void main_peak_apply(const struct fenykl_cfg_opt *o, int32_t old)
{
	(void)o;
	fenykl_log_info(&main_peak_log, "peaking %s (was %s)", main_peak_names[main_peak_level],
			main_peak_names[old]);
}

static void main_peak_barrier(void)
{
	__asm__ volatile("" ::: "memory");
}

/* --- hooks (MOV_TASK and every other img_exec caller) --- */

static void main_peak_hw_start(void *ctrl)
{
	struct main_peak_state *st = &main_peak;

	if (FENYKL_CORE_ID() != 0 || ctrl != (void *)st->ctrl || !st->line) {
		FENYKL_ORIG(img_hw_start)(ctrl);
		return;
	}
	fenykl_peak_shadow_patch(st->saved_shadow, st->coring, st->lut_zero);
	FENYKL_ORIG(img_hw_start)(ctrl);
	fenykl_peak_shadow_restore(st->saved_shadow);
}
FENYKL_HOOK(img_hw_start, main_peak_hw_start);

/* Takes the overlay's buffers for a line pass; false if it is not shown. */
static bool main_peak_enter(struct main_peak_state *st)
{
	uint32_t psw = fenykl_irq_save();
	bool ok = st->state == MAIN_PEAK_SHOWN && !st->in_pass;

	if (ok)
		st->in_pass = true;
	fenykl_irq_restore(psw);
	return ok;
}

static void main_peak_line_pass(struct main_peak_state *st, const void *ctrl, uint32_t t_lv)
{
	uint32_t t_start, t_end, status;

	memcpy(st->ctrl, ctrl, FENYKL_PEAK_CTRL_SIZE);
	fenykl_peak_line_ctrl(st->ctrl, &st->lay, st->out1, st->ln, st->lum);
	st->line = true;
	t_start = FENYKL_TIMER_NOW();
	status = FENYKL_ORIG(img_exec)(st->ctrl);
	t_end = FENYKL_TIMER_NOW();
	st->line = false;
	st->last_end = t_end;
	if (status) {
		st->err_status = status;
		st->dead = true;
		st->in_pass = false;
		return;
	}
	st->hk.line++;
	st->frame = (struct main_peak_frame){
		.t_lv = t_lv, .t_start = t_start, .t_end = t_end, .out1 = fenykl_peak_out1(ctrl),
	};
	main_peak_barrier();
	st->fresh = true;
	st->in_pass = false;
	/* The task runs above MOV_TASK: it marks from here on. */
	if (fenykl_task_wake(st->task) < 0)
		st->hk.wake_err++;
}

/* Records the live-view pass; true if a line pass may follow it. */
static bool main_peak_lv(struct main_peak_state *st, const void *ctrl, uint32_t t_start,
			 uint32_t t_lv)
{
	struct main_peak_hk *s = &st->hk;
	uint32_t us = FENYKL_TIMER_ELAPSED(t_start, t_lv), psw;

	s->lv++;
	s->lv_us_sum += us;
	s->lv_us_max = us > s->lv_us_max ? us : s->lv_us_max;
	st->lv_last = t_lv;
	if (st->state == MAIN_PEAK_HIDDEN) {
		psw = fenykl_irq_save();
		memcpy(st->lv_copy, ctrl, FENYKL_PEAK_CTRL_SIZE);
		st->have_lv = true;
		fenykl_irq_restore(psw);
		return false;
	}
	st->layout_ok = fenykl_peak_same_layout(ctrl, st->stock);
	if (st->state != MAIN_PEAK_SHOWN || st->dead)
		return false;
	if (!st->layout_ok) {
		s->skip_layout++;
		return false;
	}
	if (fenykl_af_half()) {
		s->skip_af++;
		return false;
	}
	if (fenykl_lv_magnified()) {
		s->skip_clip++;
		return false;
	}
	return true;
}

unsigned int main_peak_after(void *ctrl, unsigned int r, uint32_t t_start)
{
	struct main_peak_state *st = &main_peak;
	uint32_t t_lv, span;

	if (FENYKL_CORE_ID() != 0 || !ctrl || !fenykl_peak_lv_pass(ctrl))
		return r;
	t_lv = FENYKL_TIMER_NOW();
	span = FENYKL_TIMER_ELAPSED(st->lv_prev[1], t_start);
	st->lv_prev[1] = st->lv_prev[0];
	st->lv_prev[0] = t_start;
	if (r) {
		st->hk.stock_err++;
		return r;
	}
	if (!main_peak_lv(st, ctrl, t_start, t_lv))
		return r;
	st->lv_div++;
	if (st->fresh || (span < MAIN_PEAK_SPAN2_US && st->lv_div < 2)) {
		st->hk.skip_rate++;
		return r;
	}
	if (FENYKL_TIMER_ELAPSED(st->last_end, t_start) < MAIN_PEAK_GAP_US || !main_peak_enter(st)) {
		st->hk.skip_busy++;
		return r;
	}
	st->lv_div = 0;
	main_peak_line_pass(st, ctrl, t_lv);
	return r;
}

/* --- peaking task --- */

static uint32_t main_peak_round(uint32_t n)
{
	return (n + MAIN_PEAK_ALIGN - 1) & ~(MAIN_PEAK_ALIGN - 1);
}

static void main_peak_off(struct main_peak_state *st, const char *why)
{
	fenykl_log_err(&main_peak_log, "%s: peaking off for this boot", why);
	st->dead = true;
}

/* Why the overlay must not be shown, NULL if it may; *soft: only if it lasts MAIN_PEAK_SOFT_US. */
static const char *main_peak_block(const struct main_peak_state *st, bool *soft)
{
	*soft = false;
	if (!main_peak_level)
		return "off";
	if (st->dead)
		return "error";
	if (!st->have_lv)
		return "no live-view pass yet";
	if (FENYKL_TIMER_ELAPSED(st->lv_last, FENYKL_TIMER_NOW()) > MAIN_PEAK_LV_STALE_US)
		return "no live-view pass";
	if (!fenykl_disp_rear())
		return "not the rear LCD";
	if (fenykl_lv_magnified())
		return "magnified";
	if (!fenykl_gui_still_lv())
		return "not a still live-view screen";
	if (st->state == MAIN_PEAK_HIDDEN && !fenykl_ovl_free())
		return "overlay layer in use";
	*soft = true;
	if (st->state == MAIN_PEAK_SHOWN && !st->layout_ok)
		return "live-view layout changed";
	if (fenykl_af_half())
		return "half press";
	return NULL;
}

/* Lends the buffers from RECFILE: display output, line image, overlays, LUM, lists, bitmap. */
static bool main_peak_alloc(struct main_peak_state *st)
{
	uint32_t out1_size = main_peak_round(st->lay.out1_bytes);
	uint32_t size = out1_size + MAIN_PEAK_LN_BYTES + 2 * MAIN_PEAK_OVL_BYTES +
			FENYKL_PEAK_LUM_BYTES + 2 * MAIN_PEAK_LIST_BYTES + MAIN_PEAK_BM_BYTES +
			MAIN_PEAK_ALIGN;
	uint32_t a;

	if (!fenykl_recfile_fits(size)) {
		fenykl_log_err(&main_peak_log, "RECFILE has no free run of %u bytes", size);
		return false;
	}
	st->alloc = fenykl_recfile_alloc(size);
	if (!fenykl_dram(st->alloc, size)) {
		fenykl_log_err(&main_peak_log, "recfile_alloc(%u) returned %08x", size, st->alloc);
		st->alloc = 0;
		return false;
	}
	a = (uint32_t)FENYKL_UNCACHED(main_peak_round(st->alloc));
	st->out1 = a;
	st->ln = a + out1_size;
	st->disp = st->ln + MAIN_PEAK_LN_BYTES;
	st->lum = st->disp + 2 * MAIN_PEAK_OVL_BYTES;
	st->lists = fenykl_cached(st->lum + FENYKL_PEAK_LUM_BYTES);
	st->bm = st->lists + 2 * MAIN_PEAK_LIST_BYTES;
	return true;
}

static void main_peak_free(struct main_peak_state *st)
{
	fenykl_recfile_free(st->alloc);
	st->alloc = 0;
}

static void main_peak_show(struct main_peak_state *st)
{
	uint32_t psw, x = 0, y = 0;
	const char *why;
	unsigned int r;

	psw = fenykl_irq_save();
	memcpy(st->stock, st->lv_copy, FENYKL_PEAK_CTRL_SIZE);
	fenykl_irq_restore(psw);
	why = fenykl_peak_layout(st->stock, &st->lay);
	if (why) {
		main_peak_off(st, why);
		return;
	}
	if (!main_peak_alloc(st)) {
		main_peak_off(st, "no buffers");
		return;
	}
	if (!fenykl_disp_lv_pos(fenykl_disp_lv(), &x, &y))
		fenykl_log_warn(&main_peak_log, "live-view buffer %08x unknown: placed at 0,0",
				fenykl_disp_lv());
	memset((void *)fenykl_cached(st->disp), 0, 2 * MAIN_PEAK_OVL_BYTES);
	fenykl_dcache_flush();
	st->back = 1;
	st->n_lit[0] = 0;
	st->n_lit[1] = 0;
	st->fresh = false;
	st->lv_div = 0;
	st->layout_ok = true;
	st->soft = false;
	st->level = main_peak_level;
	st->coring = main_peak_lvls[st->level].coring;
	r = fenykl_ovl_show(&st->ovl, st->disp, st->lay.ln_w, st->lay.ln_h, (uint16_t)(x + st->lay.x),
			    (uint16_t)(y + st->lay.y), st->lay.w, st->lay.h);
	if (r) {
		unsigned int h = fenykl_ovl_hide(&st->ovl);

		fenykl_log_err(&main_peak_log, "overlay show: status %x, hide %x", r, h);
		main_peak_free(st);
		main_peak_off(st, "overlay setup failed");
		return;
	}
	st->shows++;
	memset(&st->ps, 0, sizeof(st->ps));
	st->state = MAIN_PEAK_SHOWN;
	fenykl_log_info(&main_peak_log, "show %u: %s, line %ux%u", st->shows,
			main_peak_names[st->level], st->lay.ln_w, st->lay.ln_h);
	fenykl_log_dbg(&main_peak_log, "show: %ux%u at %u,%u", st->lay.w, st->lay.h, x + st->lay.x,
		       y + st->lay.y);
}

static void main_peak_hide(struct main_peak_state *st, const char *why)
{
	uint32_t psw = fenykl_irq_save();
	unsigned int r;

	st->state = MAIN_PEAK_HIDING;
	fenykl_irq_restore(psw);
	for (int t = 0; st->in_pass && t < MAIN_PEAK_HIDE_WAIT_MS; t += MAIN_PEAK_TICK_MS)
		fenykl_task_sleep(MAIN_PEAK_TICK_MS);
	if (st->in_pass) {
		/* MOV_TASK is stuck in img_exec with our buffers: leave them lent and the layer up. */
		main_peak_off(st, "line pass did not finish");
		return;
	}
	st->fresh = false;
	st->soft = false;
	st->have_lv = false;		/* the next show takes the layout of a new pass */
	r = fenykl_ovl_hide(&st->ovl);
	main_peak_free(st);
	st->state = MAIN_PEAK_HIDDEN;
	fenykl_log_info(&main_peak_log, "hide (%s)", why);
	if (r) {
		fenykl_log_err(&main_peak_log, "overlay hide: status %x", r);
		main_peak_off(st, "overlay teardown failed");
	}
}

/* Shows or hides by main_peak_block; a soft block hides only after MAIN_PEAK_SOFT_US. */
static void main_peak_gate(struct main_peak_state *st)
{
	bool soft;
	const char *why = main_peak_block(st, &soft);

	if (!why) {
		st->soft = false;
		if (st->state == MAIN_PEAK_HIDDEN)
			main_peak_show(st);
		return;
	}
	if (st->state != MAIN_PEAK_SHOWN)
		return;
	if (soft) {
		if (!st->soft) {
			st->soft = true;
			st->soft_t = FENYKL_TIMER_NOW();
		}
		if (FENYKL_TIMER_ELAPSED(st->soft_t, FENYKL_TIMER_NOW()) < MAIN_PEAK_SOFT_US)
			return;
	}
	main_peak_hide(st, why);
}

static void main_peak_add(struct main_peak_acc *a, uint32_t us)
{
	a->n++;
	a->sum += us;
	a->max = us > a->max ? us : a->max;
}

/* Flips the overlay to buf; false if it is being hidden (nothing written). */
static bool main_peak_flip(struct main_peak_state *st, uint32_t buf)
{
	uint32_t psw = fenykl_irq_save();
	bool ok = st->state == MAIN_PEAK_SHOWN;

	if (ok)
		fenykl_ovl_flip(buf);
	fenykl_irq_restore(psw);
	return ok;
}

/* Debug level: polls until the display latched the frame's live view and buf. */
static void main_peak_watch(struct main_peak_state *st, const struct main_peak_frame *f,
			    uint32_t buf)
{
	struct main_peak_stats *p = &st->ps;
	bool lv = false, ovl = false;

	for (;;) {
		uint32_t el = FENYKL_TIMER_ELAPSED(f->t_lv, FENYKL_TIMER_NOW());

		if (!lv && fenykl_disp_lv_latched() - f->out1 < st->lay.out1_bytes) {
			lv = true;
			main_peak_add(&p->t[MAIN_PEAK_T_LV], el);
		}
		if (!ovl && fenykl_ovl_latched() == (buf & 0x3FFFFFFFu)) {
			ovl = true;
			main_peak_add(&p->t[MAIN_PEAK_T_OVL], el);
		}
		if ((lv && ovl) || el > MAIN_PEAK_WATCH_US)
			break;
		fenykl_task_sleep(1);
	}
	p->lv_missed += !lv;
	p->ovl_missed += !ovl;
}

/* Marks the line pass the hook left and flips the overlay to it. */
static void main_peak_frame(struct main_peak_state *st)
{
	struct main_peak_stats *p = &st->ps;
	unsigned int b = st->back;
	uint32_t buf = st->disp + b * MAIN_PEAK_OVL_BYTES, t[4];
	struct main_peak_mark m = {
		.ln = (const uint8_t *)fenykl_cached(st->ln), .bm = (uint32_t *)st->bm,
		.dst = (volatile uint32_t *)buf, .list = (uint32_t *)st->lists + b * MAIN_PEAK_LIT_MAX,
		.w = st->lay.ln_w, .h = st->lay.ln_h, .thr = main_peak_lvls[st->level].thr,
		.max = MAIN_PEAK_LIT_MAX, .edge = FENYKL_OVL_RED,
	};
	struct main_peak_frame f;

	main_peak_barrier();
	f = st->frame;
	t[0] = FENYKL_TIMER_NOW();
	fenykl_dcache_flush();
	for (uint32_t i = 0; i < st->n_lit[b]; i++)
		m.dst[m.list[i]] = 0;
	t[1] = FENYKL_TIMER_NOW();
	main_peak_mark(&m);
	main_peak_barrier();
	st->fresh = false;
	t[2] = FENYKL_TIMER_NOW();
	st->n_lit[b] = m.n;
	if (!main_peak_flip(st, buf))
		return;
	t[3] = FENYKL_TIMER_NOW();
	st->back ^= 1u;
	st->frames++;
	main_peak_add(&p->t[MAIN_PEAK_T_START], FENYKL_TIMER_ELAPSED(f.t_lv, f.t_start));
	main_peak_add(&p->t[MAIN_PEAK_T_LINE], FENYKL_TIMER_ELAPSED(f.t_start, f.t_end));
	main_peak_add(&p->t[MAIN_PEAK_T_WAKE], FENYKL_TIMER_ELAPSED(f.t_end, t[0]));
	main_peak_add(&p->t[MAIN_PEAK_T_CLEAR], FENYKL_TIMER_ELAPSED(t[0], t[1]));
	main_peak_add(&p->t[MAIN_PEAK_T_MARK], FENYKL_TIMER_ELAPSED(t[1], t[2]));
	main_peak_add(&p->t[MAIN_PEAK_T_FLIP], FENYKL_TIMER_ELAPSED(f.t_lv, t[3]));
	p->frames++;
	p->lit_sum += m.n;
	p->lit_max = m.n > p->lit_max ? m.n : p->lit_max;
	p->capped += m.capped;
	if (main_peak_log.level >= FENYKL_LOG_DEBUG && ++st->watch_div % MAIN_PEAK_WATCH_EVERY == 0)
		main_peak_watch(st, &f, buf);
}

/* Debug level, every MAIN_PEAK_STAT_US: the hook's counters and the stage times. */
static void main_peak_stats(struct main_peak_state *st)
{
	struct main_peak_stats *p = &st->ps;
	struct main_peak_hk s;
	uint32_t psw = fenykl_irq_save(), now = FENYKL_TIMER_NOW();
	uint32_t ms = FENYKL_TIMER_ELAPSED(st->stat_t, now) / 1000;

	s = st->hk;
	memset(&st->hk, 0, sizeof(st->hk));
	fenykl_irq_restore(psw);
	st->stat_t = now;
	st->lv_total += s.lv;
	st->line_total += s.line;
	st->wake_err_total += s.wake_err;
	if (s.stock_err || s.wake_err || p->capped)
		fenykl_log_warn(&main_peak_log, "stock errors %u, wake errors %u, capped %u",
				s.stock_err, s.wake_err, p->capped);
	if (st->state != MAIN_PEAK_SHOWN)
		return;
	fenykl_log_dbg(&main_peak_log, "2s: LV %u/s, line %u, stock pass avg %u max %u us",
		       ms ? s.lv * 1000 / ms : 0, s.line, s.lv ? s.lv_us_sum / s.lv : 0, s.lv_us_max);
	fenykl_log_dbg(&main_peak_log, "2s: skip rate %u busy %u half %u other %u", s.skip_rate,
		       s.skip_busy, s.skip_af, s.skip_layout + s.skip_clip);
	fenykl_log_dbg(&main_peak_log, "2s: %u frames, lit avg %u max %u", p->frames,
		       p->frames ? p->lit_sum / p->frames : 0, p->lit_max);
	for (unsigned int i = 0; i < MAIN_PEAK_NT; i++) {
		const struct main_peak_acc *a = &p->t[i];

		if (a->n)
			fenykl_log_dbg(&main_peak_log, "2s: %s avg %u max %u us (%u)",
				       main_peak_t_name[i], a->sum / a->n, a->max, a->n);
	}
	if (p->lv_missed || p->ovl_missed)
		fenykl_log_dbg(&main_peak_log, "2s: latch missed LV %u edges %u", p->lv_missed,
			       p->ovl_missed);
	memset(p, 0, sizeof(*p));
}

FENYKL_TASK(main_peak_main);
void main_peak_main(void)
{
	struct main_peak_state *st = &main_peak;

	for (;;) {
		/* The hook's wakeup ends the wait after a line pass. */
		if (!(st->state == MAIN_PEAK_SHOWN && st->fresh))
			fenykl_task_wait(MAIN_PEAK_TICK_MS);
		if (st->err_status) {
			main_peak_off(st, "line pass failed");
			fenykl_log_err(&main_peak_log, "line pass status %x", st->err_status);
			st->err_status = 0;
		}
		main_peak_gate(st);
		if (st->state == MAIN_PEAK_SHOWN && main_peak_level && main_peak_level != st->level) {
			st->level = main_peak_level;
			st->coring = main_peak_lvls[st->level].coring;
		}
		if (st->state == MAIN_PEAK_SHOWN && st->fresh)
			main_peak_frame(st);
		if (FENYKL_TIMER_ELAPSED(st->stat_t, FENYKL_TIMER_NOW()) >= MAIN_PEAK_STAT_US)
			main_peak_stats(st);
	}
}

int main_peak_start(void)
{
	struct main_peak_state *st = &main_peak;
	int r;

	st->stat_t = FENYKL_TIMER_NOW();
	/* The hooks wake the task by id: set before the first show. */
	st->task = fenykl_task_create("PEAK", main_peak_main_task, MAIN_PEAK_PRIO, MAIN_PEAK_STACK);
	if (st->task < 0) {
		fenykl_log_err(&main_peak_log, "task: %d, no peaking", st->task);
		return st->task;
	}
	r = fenykl_hook_install(&fenykl_hook_img_hw_start);
	if (r) {
		fenykl_log_err(&main_peak_log, "hook img_hw_start install %d: no peaking", r);
		st->dead = true;
		return r;
	}
	fenykl_log_info(&main_peak_log, "task %d pri %u, peaking %s", st->task, MAIN_PEAK_PRIO,
			main_peak_names[main_peak_level]);
	return 0;
}

void main_peak_set_level(unsigned int level)
{
	fenykl_log_set_level(&main_peak_log, level);
}

void main_peak_status(void)
{
	const struct main_peak_state *st = &main_peak;

	fenykl_log_info(&main_peak_log, "peak %s state %u%s: shows %u", main_peak_names[main_peak_level],
			st->state, st->dead ? " dead" : "", st->shows);
	fenykl_log_info(&main_peak_log, "  LV %u line %u frames %u wake errors %u", st->lv_total,
			st->line_total, st->frames, st->wake_err_total);
}
