/*
 * The plugin's own contrast client (afc.h, docs/afc.md): snapshot of fcs's contrast_setting,
 * gates, attach with cam_request, detach by number = 0, callbacks. The firmware layouts come from
 * the body (body.h, afc group).
 */
#include <fenykl.h>
#include <afc.h>
#include <body.h>
#include "afc_int.h"

#define AFC_FCS_IDLE 0u
#define AFC_SNAP_MS 500u

static struct fenykl_afc afc;
/* Whole lines, used uncached. */
static uint32_t afc_dma_buf[FENYKL_CNT_DMA_WORDS] FENYKL_SHARED
	__attribute__((aligned(FENYKL_CNT_DMA_ALIGN)));
/* Callbacks on another core count here, in their own uncached line. */
static FENYKL_SHARED uint32_t afc_other_core;

FENYKL_LOG_MODULE(fenykl_afc_log, "afc", FENYKL_LOG_INFO);

const char *const fenykl_afc_state_name[] = { "idle", "attaching", "attached", "detaching", "off" };
const char *const fenykl_afc_err_name[] = { "none", "req", "noframe", "nodone", "early", "end",
					    "event", "core" };

static volatile uint32_t *afc_dma(void)
{
	return *FENYKL_SHARED_PTR(afc_dma_buf);
}

/* Interrupt nesting byte of the executing core's kernel block: non-zero in an ISR. */
static uint32_t afc_irq_nest(void)
{
	uint32_t k = fenykl_rd32(FW_KERNEL_CORE_CTX + 4u * FENYKL_CORE_ID());

	return fenykl_rd8(fenykl_rd32(k + 0x1Cu));
}

static void afc_set_u16(uint32_t off, uint32_t v)
{
	*(volatile uint16_t *)(afc.setting + off) = (uint16_t)v;
}

/* A callback on another core than CPU0: counted, nothing else. */
static bool afc_foreign_core(void)
{
	if (FENYKL_CORE_ID() == 0)
		return false;
	(*FENYKL_SHARED_PTR(afc_other_core))++;
	return true;
}

/* --- callbacks: firmware stack, interrupt context; counters and the number = 0 write only --- */

static void afc_frame_cb(unsigned int unk, unsigned int events, void *req)
{
	volatile struct fenykl_afc_cb *c = &afc.cb;
	uint32_t now, nest;

	if (afc_foreign_core())
		return;
	now = FENYKL_TIMER_NOW();
	nest = afc_irq_nest();
	if (c->calls) {
		uint32_t gap = FENYKL_TIMER_ELAPSED(c->last_us, now);

		if (gap > c->gap_us)
			c->gap_us = gap;
	}
	c->psw = fw_psw_get();
	if (!c->first_us) {
		c->first_us = now | 1u;
		c->first_nest = nest;
		c->first_psw = c->psw;
		c->first_a0 = (uint32_t)req;
	}
	c->last_us = now;
	c->calls++;
	c->st += !!(events & FENYKL_CNT_EV_ST);
	c->cp += !!(events & FENYKL_CNT_EV_CP);
	c->err += !!(events & FENYKL_CNT_EV_ERR);
	c->irq += nest != 0;
	c->bad_req += req != afc.req;
	c->events = events;
	c->unk = unk;
	/* Fastest detach: the driver reads number right after the frame-start callback. */
	if (c->stop || fenykl_af_half()) {
		afc_set_u16(FENYKL_CNT_SET_NUMBER, 0);
		if (!c->cb_stop_us)
			c->cb_stop_us = now | 1u;
		c->cb_stops++;
	}
}
FENYKL_CALLBACK(cam_cnt_frame_cb, afc_frame_cb);

static void afc_done_cb(unsigned int status, void *req)
{
	volatile struct fenykl_afc_cb *c = &afc.cb;

	if (afc_foreign_core())
		return;
	c->done_status = status;
	c->done_us = FENYKL_TIMER_NOW();
	c->done_nest = afc_irq_nest();
	c->bad_req += req != afc.req;
	c->done++;
}
FENYKL_CALLBACK(cam_req_done_cb, afc_done_cb);

/* --- snapshot of fcs's contrast_setting (S-AF/C-AF framing, through AF running) --- */

/* Two copies must agree on the parameter part (fcs may be updating it). */
static void afc_snap_poll(void)
{
	const volatile uint8_t *set;
	uint8_t pat, cols, rows;

	if (afc.ms - afc.snap_ms < AFC_SNAP_MS)
		return;
	set = (const volatile uint8_t *)fenykl_cnt_fcs_setting(&pat, &cols, &rows);
	if (!set)
		return;
	afc.snap_ms = afc.ms;
	for (uint32_t k = 0; k < 2; k++)
		for (uint32_t i = 0; i < FENYKL_CNT_SET_SIZE; i++)
			afc.tmp[k][i] = set[i];
	if (memcmp(afc.tmp[0], afc.tmp[1], FENYKL_CNT_SET_INFO)) {
		afc.torn++;
		return;
	}
	if (afc.snapped && !memcmp(afc.tmp[0], afc.snap, FENYKL_CNT_SET_INFO))
		return;
	memcpy(afc.snap, afc.tmp[0], FENYKL_CNT_SET_SIZE);
	afc.snap_pat = pat;
	afc.snap_cols = cols;
	afc.snap_rows = rows;
	afc.snaps++;
	if (!afc.snapped)
		fenykl_log_info(&fenykl_afc_log, "snap #%u: pat %02x, %ux%u", afc.snaps, pat, cols,
				rows);
	else
		fenykl_log_dbg(&fenykl_afc_log, "snap #%u: pat %02x, %ux%u", afc.snaps, pat, cols,
			       rows);
	afc.snapped = true;
}

/* --- gates --- */

static uint32_t afc_gates(bool enable)
{
	uint32_t g = 0;

	if (!afc.snapped)
		g |= FENYKL_AFC_G_SNAP;
	if (!fenykl_af_mf())
		g |= FENYKL_AFC_G_MF;
	if (!fenykl_cnt_lv())
		g |= FENYKL_AFC_G_LV;
	if (fenykl_cnt_movie())
		g |= FENYKL_AFC_G_MOVIE;
	if (fenykl_af_half())
		g |= FENYKL_AFC_G_HALF;
	if (fenykl_rd8(FW_FCS_AF_EVAL_STATE) != AFC_FCS_IDLE)
		g |= FENYKL_AFC_G_FCS;
	if (fenykl_cnt_foreign((uint32_t)afc.setting))
		g |= FENYKL_AFC_G_SLOT;
	if (fenykl_cnt_drv_dbg())
		g |= FENYKL_AFC_G_DRV;
	if (!enable)
		g |= FENYKL_AFC_G_OFF;
	return g;
}

/* --- actions --- */

static int afc_attach(void)
{
	volatile uint32_t *dma = afc_dma();
	uint32_t r;

	memcpy(afc.setting, afc.snap, FENYKL_CNT_SET_SIZE);
	memset(afc.setting + FENYKL_CNT_SET_INFO, 0, FENYKL_CNT_SET_SIZE - FENYKL_CNT_SET_INFO);
	fenykl_cnt_setting_prep(afc.setting, (uint32_t)dma);
	for (uint32_t i = 0; i < FENYKL_CNT_DMA_WORDS; i++)
		dma[i] = FENYKL_AFC_DMA_FILL;

	memset(afc.req, 0, sizeof(afc.req));
	fenykl_cnt_req_prep(afc.req, (uint32_t)afc.setting, FENYKL_CB(cam_cnt_frame_cb, afc_frame_cb),
			    FENYKL_CB(cam_req_done_cb, afc_done_cb), (uint32_t)&afc);

	afc.cb.stop = 0;
	afc.cb.first_us = 0;
	afc.cb.cb_stop_us = 0;
	afc.cb.gap_us = 0;
	afc.attach_calls = afc.cb.calls;
	afc.attach_st = afc.cb.st;
	afc.attaches++;
	afc.req_us = FENYKL_TIMER_NOW();
	afc.attach_ms = afc.ms;
	r = fw_cam_request(afc.req);
	fenykl_log_info(&fenykl_afc_log, "attach #%u: cam_request %x, pat %02x", afc.attaches, r,
			afc.snap_pat);
	return (int)r;
}

static void afc_stop(void)
{
	afc.stop_us = FENYKL_TIMER_NOW();
	afc.cb.stop = 1;
	afc_set_u16(FENYKL_CNT_SET_NUMBER, 0);
}

static void afc_do(enum fenykl_afc_action a, const struct fenykl_afc_in *in)
{
	if (a == FENYKL_AFC_ATTACH && afc_attach())
		a = fenykl_afc_sm_fail(&afc.sm, in, FENYKL_AFC_E_REQ);
	if (a == FENYKL_AFC_STOP)
		afc_stop();
}

/* --- log --- */

static const char *afc_stop_by(void)
{
	if (afc.sm.by_cb)
		return "cb";
	return afc.sm.why == FENYKL_AFC_W_STALL ? "stall" : "task";
}

static void afc_log(enum fenykl_afc_state from, bool done)
{
	volatile struct fenykl_afc_cb *c = &afc.cb;
	enum fenykl_afc_state to = afc.sm.state;
	bool live = from == FENYKL_AFC_ATTACHING || from == FENYKL_AFC_ATTACHED;
	uint32_t stop_us = afc.sm.by_cb ? c->cb_stop_us : afc.stop_us;

	if (to == from) {
		/* A stop with live view off waits for the frames to resume (no timeout meanwhile). */
		if (to == FENYKL_AFC_DETACHING && !afc.sm.flowing && !afc.pend_logged &&
		    afc.ms - afc.sm.since >= FENYKL_AFC_DONE_MS) {
			afc.pend_logged = true;
			fenykl_log_info(&fenykl_afc_log, "stop #%u pending: no frames", afc.attaches);
		}
		return;
	}
	if (to == FENYKL_AFC_ATTACHED) {
		fenykl_log_info(&fenykl_afc_log, "attached #%u: first cb %u us after the request",
				afc.attaches, FENYKL_TIMER_ELAPSED(afc.req_us, c->first_us));
		fenykl_log_info(&fenykl_afc_log, "  cb nest %u psw %x a0 %x req %x", c->first_nest,
				c->first_psw, c->first_a0, (uint32_t)afc.req);
	} else if (live && to == FENYKL_AFC_OFF)
		fenykl_log_err(&fenykl_afc_log, "done while attached, no stop requested, status %x: off",
			       c->done_status);
	else if (live)
		fenykl_log_info(&fenykl_afc_log, "stop #%u: %s, gates %03x, %u frames", afc.attaches,
				afc_stop_by(), afc.sm.why, c->st - afc.attach_st);
	if (to == FENYKL_AFC_DETACHING)
		afc.pend_logged = false;
	if ((to == FENYKL_AFC_IDLE || to == FENYKL_AFC_OFF) && done && !(live && to == FENYKL_AFC_OFF))
		fenykl_log_info(&fenykl_afc_log, "done #%u: %u us after the stop, status %x, nest %u",
				afc.attaches, FENYKL_TIMER_ELAPSED(stop_us, c->done_us), c->done_status,
				c->done_nest);
}

/* The first error, once: the client stays off for the boot. */
static void afc_log_err(void)
{
	if (!fenykl_afc_failed() || afc.err_logged)
		return;
	afc.err_logged = true;
	if (afc.sm.err == FENYKL_AFC_E_NODONE)
		fenykl_log_err(&fenykl_afc_log, "no done within %u ms of the frames after the stop",
			       FENYKL_AFC_DONE_MS);
	fenykl_log_err(&fenykl_afc_log, "off for the boot: %s (attach #%u, sm %s)",
		       fenykl_afc_err_name[afc.sm.err], afc.attaches,
		       fenykl_afc_state_name[afc.sm.state]);
}

/* The camera framed only in MF so far: nothing to copy. */
static void afc_log_nosnap(void)
{
	if (afc.gates != FENYKL_AFC_G_SNAP || afc.nosnap_logged || afc.ms < FENYKL_AFC_BOOT_MS)
		return;
	afc.nosnap_logged = true;
	fenykl_log_info(&fenykl_afc_log, "MF without a snapshot: frame in S-AF or C-AF first");
}

/* --- API --- */

static void afc_clock(void)
{
	uint32_t now = FENYKL_TIMER_NOW();

	afc.rem_us += FENYKL_TIMER_ELAPSED(afc.last_us, now);
	afc.last_us = now;
	afc.ms += afc.rem_us / 1000u;
	afc.rem_us %= 1000u;
}

static struct fenykl_afc_in afc_input(void)
{
	return (struct fenykl_afc_in){ .now = afc.ms, .gates = afc.gates, .frames = afc.cb.calls,
				       .done = afc.cb.done, .cb_stops = afc.cb.cb_stops };
}

void fenykl_afc_init(void)
{
	struct fenykl_afc_in in = { 0 };

	afc.last_us = FENYKL_TIMER_NOW();
	afc.snap_ms = -AFC_SNAP_MS;
	fenykl_afc_sm_init(&afc.sm, &in);
}

/* The first error the callbacks reported: a call on another core, or the error event. */
static enum fenykl_afc_err afc_cb_err(void)
{
	if (afc.sm.err != FENYKL_AFC_E_NONE)
		return FENYKL_AFC_E_NONE;
	if (fenykl_afc_other_core())
		return FENYKL_AFC_E_CORE;
	if (afc.cb.err)
		return FENYKL_AFC_E_EVENT;
	return FENYKL_AFC_E_NONE;
}

void fenykl_afc_poll(bool enable)
{
	enum fenykl_afc_state from = afc.sm.state;
	uint32_t done = afc.sm.done;
	enum fenykl_afc_err err;
	struct fenykl_afc_in in;

	afc_clock();
	afc_snap_poll();
	afc.gates = afc_gates(enable);
	in = afc_input();
	err = afc_cb_err();
	if (err != FENYKL_AFC_E_NONE)
		afc_do(fenykl_afc_sm_fail(&afc.sm, &in, err), &in);
	else
		afc_do(fenykl_afc_sm_step(&afc.sm, &in), &in);
	/* An early done ends a live client in OFF without a stop: number = 0 anyway. */
	if (afc.sm.state == FENYKL_AFC_OFF &&
	    (from == FENYKL_AFC_ATTACHING || from == FENYKL_AFC_ATTACHED))
		afc_stop();
	afc_log(from, done != afc.sm.done);
	afc_log_err();
	afc_log_nosnap();
}

void fenykl_afc_end(void)
{
	enum fenykl_afc_state from = afc.sm.state;
	struct fenykl_afc_in in;

	afc_clock();
	in = afc_input();
	afc_do(fenykl_afc_sm_fail(&afc.sm, &in, FENYKL_AFC_E_END), &in);
	afc_log(from, false);
}

const struct fenykl_afc *fenykl_afc_get(void)
{
	return &afc;
}

const volatile uint32_t *fenykl_afc_dma(void)
{
	return afc_dma() + FENYKL_CNT_DMA_STATS;
}

uint32_t fenykl_afc_other_core(void)
{
	return *FENYKL_SHARED_PTR(afc_other_core);
}

bool fenykl_afc_failed(void)
{
	return afc.sm.err != FENYKL_AFC_E_NONE && afc.sm.err != FENYKL_AFC_E_END;
}
