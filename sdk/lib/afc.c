/*
 * The plugin's own contrast client (afc.h, docs/afc.md): snapshot of fcs's contrast_setting,
 * gates, attach with cam_request, detach by number = 0, callbacks. Firmware
 * addresses and layouts are those of v1.6.
 */
#include <fenykl.h>
#include <afc.h>
#include "afc_int.h"

#define AFC_EVAL_STATE 0x605A8C38u	/* u8 fcs_af_eval_state: 0 idle, 2 running, 3 stop, 1 last, 4 update */
#define AFC_CTX_PTR 0x605AED1Cu		/* u32 fcs contrast ctx, 0 when freed */
#define AFC_FCS_DMA 0xACD377C0u		/* fcs's DMA buffer, uncached */
#define AFC_CLIENT 0x605AEFA0u		/* u32 contrast driver: current client */
#define AFC_QUEUED 0x605AEFA4u		/* u32 contrast driver: queued client */
#define AFC_CL_SETTING 0x08u		/* driver client +8: its contrast_setting */
#define AFC_DRV_DBG 0x605AEFD4u		/* u16 driver debug mode: 3 converts windows in cam_cnt_apply */
#define AFC_TF_PTR 0x60575E50u		/* u32 take_focus */
#define AFC_TRG 0x605519A4u		/* u8 trigger snapshot: bit 0 clear = half or full press */
#define AFC_GUI_SUPER 0x60702278u
#define AFC_GUI_STATE 0x60702279u
#define AFC_SS_REC 0x02u
#define AFC_KCTX 0x8FF00004u		/* per-core kernel block; *(*(+0x1C)) != 0: interrupt context */

/* take_focus offsets (u8) */
#define AFC_TF_MODE 0x05u		/* 0 S-AF, 1 MF, 2 C-AF */
#define AFC_TF_MF 1u
#define AFC_TF_MOVIE 0x07u
#define AFC_TF_LV_MODE 0xD4u

/* fcs contrast ctx offsets */
#define AFC_CTX_COLS 0x5Du
#define AFC_CTX_ROWS 0x5Eu
#define AFC_CTX_PATTERN 0x2D0u
#define AFC_CTX_REQ 0x2DCu		/* its cam_request object */
#define AFC_CTX_SETTING 0x378u

/* contrast_setting */
#define AFC_SET_NUMBER 0x00u		/* u16: 0xFFFF endless, 0 = detach at the next frame */
#define AFC_SET_UPDATE 0x08u		/* u16 update bits, cleared by cam_cnt_apply */
#define AFC_SET_DMA 0x88u
#define AFC_SET_ENDLESS 0xFFFFu
#define AFC_SET_ALL 0x1FFFu		/* every setting-driven update bit (0x1..0x1000) */

/* request */
#define AFC_REQ_TYPE 7u
#define AFC_EV_ST 0x400u		/* AF_ST */
#define AFC_EV_CP 0x800u		/* AF_CP */
#define AFC_COLS 7u			/* pattern 0 / 0xD: 7 x 5 */
#define AFC_ROWS 5u
#define AFC_SNAP_MS 500u

struct afc_req {
	uint16_t type;		/* +0x00 */
	uint8_t flag;		/* +0x02 = 1 */
	uint8_t pad;
	uint32_t expo;		/* +0x04: 0, the driver keeps its sensor info */
	uint32_t smode;		/* +0x08 */
	uint32_t r0c, r10;
	uint32_t setting;	/* +0x14 contrast_setting */
	uint32_t r18, r1c;
	uint32_t step;		/* +0x20: 0 selects the async contrast path */
	uint32_t r24;
	uint32_t done_cb;	/* +0x28 */
	uint32_t frame_cb;	/* +0x2C */
	uint32_t events;	/* +0x30 */
	uint32_t arg;		/* +0x34 */
	uint32_t r38, r3c;
};

_Static_assert(sizeof(struct afc_req) == sizeof(((struct fenykl_afc *)0)->req), "request is 0x40 bytes");

static struct fenykl_afc afc;
static uint32_t afc_dma_buf[FENYKL_AFC_DMA_WORDS] FENYKL_SHARED;	/* whole lines, used uncached */

FENYKL_LOG_MODULE(fenykl_afc_log, "afc", FENYKL_LOG_INFO);

const char *const fenykl_afc_state_name[] = { "idle", "attaching", "attached", "detaching", "off" };
const char *const fenykl_afc_err_name[] = { "none", "req", "noframe", "nodone", "early", "end" };

static uint32_t afc_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t afc_rd16(uint32_t a)
{
	return *(volatile uint16_t *)a;
}

static uint32_t afc_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

static volatile uint32_t *afc_dma(void)
{
	return *FENYKL_SHARED_PTR(afc_dma_buf);
}

static struct afc_req *afc_req(void)
{
	return (struct afc_req *)afc.req;
}

static bool afc_half(void)
{
	return !(afc_rd8(AFC_TRG) & 1u);
}

/* As 0x6E6896DF, which the firmware's contrast callbacks use to pick iset_flg. */
static uint32_t afc_in_irq(void)
{
	uint32_t k = afc_rd32(AFC_KCTX + 4u * FENYKL_CORE_ID());

	return afc_rd8(afc_rd32(k + 0x1Cu)) != 0;
}

static void afc_set_u16(uint32_t off, uint32_t v)
{
	*(volatile uint16_t *)(afc.setting + off) = (uint16_t)v;
}

/* --- callbacks: firmware stack, interrupt context; counters and the number = 0 write only --- */

static void afc_frame_cb(unsigned int unk, unsigned int events, void *req)
{
	volatile struct fenykl_afc_cb *c = &afc.cb;
	uint32_t now = FENYKL_LOG_TIMER_NOW();
	uint32_t irq = afc_in_irq();

	if (c->calls) {
		uint32_t gap = FENYKL_LOG_ELAPSED(c->last_us, now);

		if (gap > c->gap_us)
			c->gap_us = gap;
	}
	if (!c->first_us) {
		c->first_us = now | 1u;
		c->first_irq = irq;
		c->first_core = FENYKL_CORE_ID();
	}
	c->last_us = now;
	c->calls++;
	c->st += !!(events & AFC_EV_ST);
	c->cp += !!(events & AFC_EV_CP);
	c->irq += irq;
	c->cpu1 += FENYKL_CORE_ID() != 0;
	c->bad_req += req != afc.req;
	c->events = events;
	c->unk = unk;
	c->psw = fw_psw_get();
	/* Fastest detach: the driver reads number right after the AF_ST callback (cam_cnt_frame). */
	if (c->stop || afc_half()) {
		afc_set_u16(AFC_SET_NUMBER, 0);
		if (!c->cb_stop_us)
			c->cb_stop_us = now | 1u;
		c->cb_stops++;
	}
}
FENYKL_CALLBACK(cam_cnt_frame_cb, afc_frame_cb);

static void afc_done_cb(unsigned int status, void *req)
{
	volatile struct fenykl_afc_cb *c = &afc.cb;

	c->done_status = status;
	c->done_us = FENYKL_LOG_TIMER_NOW();
	c->done_irq = afc_in_irq();
	c->done_core = FENYKL_CORE_ID();
	c->bad_req += req != afc.req;
	c->done++;
}
FENYKL_CALLBACK(cam_req_done_cb, afc_done_cb);

/* --- snapshot of fcs's contrast_setting (S-AF/C-AF framing, through AF running) --- */

static bool afc_snap_ready(uint32_t tf, uint32_t ctx)
{
	uint32_t set = ctx + AFC_CTX_SETTING;
	uint32_t pat;

	if (!tf || !ctx || afc_rd8(AFC_EVAL_STATE) != 2 || afc_half())
		return false;
	if (afc_rd8(tf + AFC_TF_MODE) == AFC_TF_MF || afc_rd8(tf + AFC_TF_MOVIE))
		return false;
	pat = afc_rd8(ctx + AFC_CTX_PATTERN);
	if ((pat != 0 && pat != 0xD) || afc_rd8(ctx + AFC_CTX_COLS) != AFC_COLS ||
	    afc_rd8(ctx + AFC_CTX_ROWS) != AFC_ROWS)
		return false;
	return afc_rd16(set + AFC_SET_NUMBER) == AFC_SET_ENDLESS &&
	       afc_rd32(set + AFC_SET_DMA) == AFC_FCS_DMA &&
	       afc_rd32(ctx + AFC_CTX_REQ + 0x14u) == set;
}

/* Two copies must agree on the parameter part (fcs may be updating it). */
static void afc_snap_poll(uint32_t tf, uint32_t ctx)
{
	const volatile uint8_t *set = (const volatile uint8_t *)(ctx + AFC_CTX_SETTING);

	if (afc.ms - afc.snap_ms < AFC_SNAP_MS || !afc_snap_ready(tf, ctx))
		return;
	afc.snap_ms = afc.ms;
	for (uint32_t k = 0; k < 2; k++)
		for (uint32_t i = 0; i < FENYKL_AFC_SET_SIZE; i++)
			afc.tmp[k][i] = set[i];
	if (memcmp(afc.tmp[0], afc.tmp[1], FENYKL_AFC_SET_INFO)) {
		afc.torn++;
		return;
	}
	if (afc.snapped && !memcmp(afc.tmp[0], afc.snap, FENYKL_AFC_SET_INFO))
		return;
	memcpy(afc.snap, afc.tmp[0], FENYKL_AFC_SET_SIZE);
	afc.snap_pat = (uint8_t)afc_rd8(ctx + AFC_CTX_PATTERN);
	afc.snap_cols = AFC_COLS;
	afc.snap_rows = AFC_ROWS;
	afc.snaps++;
	if (!afc.snapped)
		fenykl_log_info(&fenykl_afc_log, "snap #%u: pat %02x", afc.snaps, afc.snap_pat);
	else
		fenykl_log_dbg(&fenykl_afc_log, "snap #%u: pat %02x", afc.snaps, afc.snap_pat);
	afc.snapped = true;
}

/* --- gates --- */

/* GUI super 0x02 (SS_REC), stills screens: Basic, Histogram, H&S, STILL_OFF, STILL_OVER, level gauge. */
static bool afc_lv_screen(void)
{
	uint32_t st = afc_rd8(AFC_GUI_STATE);

	if (afc_rd8(AFC_GUI_SUPER) != AFC_SS_REC)
		return false;
	return (st >= 0x03 && st <= 0x05) || (st >= 0x09 && st <= 0x0B);
}

uint32_t fenykl_afc_slot_owner(uint32_t slot)
{
	uint32_t cl = afc_rd32(slot);

	if (!cl)
		return 0;
	return afc_rd32(cl + AFC_CL_SETTING) == (uint32_t)afc.setting ? 1 : 2;
}

static uint32_t afc_gates(uint32_t tf, bool enable)
{
	uint32_t g = 0;

	if (!afc.snapped)
		g |= FENYKL_AFC_G_SNAP;
	if (!tf || afc_rd8(tf + AFC_TF_MODE) != AFC_TF_MF)
		g |= FENYKL_AFC_G_MF;
	if (!afc_lv_screen())
		g |= FENYKL_AFC_G_LV;
	if (!tf || afc_rd8(tf + AFC_TF_MOVIE) || afc_rd8(tf + AFC_TF_LV_MODE))
		g |= FENYKL_AFC_G_MOVIE;
	if (afc_half())
		g |= FENYKL_AFC_G_HALF;
	if (afc_rd8(AFC_EVAL_STATE))
		g |= FENYKL_AFC_G_FCS;
	if (fenykl_afc_slot_owner(AFC_CLIENT) == 2 || fenykl_afc_slot_owner(AFC_QUEUED) == 2)
		g |= FENYKL_AFC_G_SLOT;
	if (afc_rd16(AFC_DRV_DBG))
		g |= FENYKL_AFC_G_DRV;
	if (!enable)
		g |= FENYKL_AFC_G_OFF;
	return g;
}

/* --- actions --- */

static int afc_attach(void)
{
	volatile uint32_t *dma = afc_dma();
	struct afc_req *rq = afc_req();
	uint32_t r;

	memcpy(afc.setting, afc.snap, FENYKL_AFC_SET_SIZE);
	memset(afc.setting + FENYKL_AFC_SET_INFO, 0, FENYKL_AFC_SET_SIZE - FENYKL_AFC_SET_INFO);
	afc_set_u16(AFC_SET_NUMBER, AFC_SET_ENDLESS);
	afc_set_u16(AFC_SET_UPDATE, AFC_SET_ALL);
	*(uint32_t *)(afc.setting + AFC_SET_DMA) = (uint32_t)dma;
	for (uint32_t i = 0; i < FENYKL_AFC_DMA_WORDS; i++)
		dma[i] = FENYKL_AFC_DMA_FILL;

	memset(rq, 0, sizeof(*rq));
	rq->type = AFC_REQ_TYPE;
	rq->flag = 1;
	rq->setting = (uint32_t)afc.setting;
	rq->done_cb = FENYKL_CB(cam_req_done_cb, afc_done_cb);
	rq->frame_cb = FENYKL_CB(cam_cnt_frame_cb, afc_frame_cb);
	rq->events = AFC_EV_ST | AFC_EV_CP;
	rq->arg = (uint32_t)&afc;

	afc.cb.stop = 0;
	afc.cb.first_us = 0;
	afc.cb.cb_stop_us = 0;
	afc.cb.gap_us = 0;
	afc.attach_calls = afc.cb.calls;
	afc.attach_st = afc.cb.st;
	afc.attaches++;
	afc.req_us = FENYKL_LOG_TIMER_NOW();
	afc.attach_ms = afc.ms;
	r = fw_cam_request(rq);
	fenykl_log_info(&fenykl_afc_log, "attach #%u: cam_request %x, pat %02x", afc.attaches, r,
			afc.snap_pat);
	return (int)r;
}

static void afc_stop(void)
{
	afc.stop_us = FENYKL_LOG_TIMER_NOW();
	afc.cb.stop = 1;
	afc_set_u16(AFC_SET_NUMBER, 0);
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
	if (to == FENYKL_AFC_ATTACHED)
		fenykl_log_info(&fenykl_afc_log, "attached #%u: first cb %u us after the request",
				afc.attaches, FENYKL_LOG_ELAPSED(afc.req_us, c->first_us));
	else if (live && to == FENYKL_AFC_OFF)
		fenykl_log_err(&fenykl_afc_log, "done while attached, no stop requested, status %x: off",
			       c->done_status);
	else if (live)
		fenykl_log_info(&fenykl_afc_log, "stop #%u: %s, gates %03x, %u frames", afc.attaches,
				afc_stop_by(), afc.sm.why, c->st - afc.attach_st);
	if (to == FENYKL_AFC_DETACHING)
		afc.pend_logged = false;
	if ((to == FENYKL_AFC_IDLE || to == FENYKL_AFC_OFF) && done && !(live && to == FENYKL_AFC_OFF))
		fenykl_log_info(&fenykl_afc_log, "done #%u: %u us after the stop, status %x",
				afc.attaches, FENYKL_LOG_ELAPSED(stop_us, c->done_us), c->done_status);
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
	uint32_t now = FENYKL_LOG_TIMER_NOW();

	afc.rem_us += FENYKL_LOG_ELAPSED(afc.last_us, now);
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

	afc.last_us = FENYKL_LOG_TIMER_NOW();
	afc.snap_cols = AFC_COLS;
	afc.snap_rows = AFC_ROWS;
	afc.snap_ms = -AFC_SNAP_MS;
	fenykl_afc_sm_init(&afc.sm, &in);
}

void fenykl_afc_poll(bool enable)
{
	uint32_t tf = afc_rd32(AFC_TF_PTR);
	enum fenykl_afc_state from = afc.sm.state;
	uint32_t done = afc.sm.done;
	struct fenykl_afc_in in;

	afc_clock();
	afc_snap_poll(tf, afc_rd32(AFC_CTX_PTR));
	afc.gates = afc_gates(tf, enable);
	in = afc_input();
	afc_do(fenykl_afc_sm_step(&afc.sm, &in), &in);
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
	return afc_dma();
}

uint32_t fenykl_afc_take_gap(void)
{
	uint32_t gap = afc.cb.gap_us;

	afc.cb.gap_us = 0;
	return gap;
}

bool fenykl_afc_failed(void)
{
	return afc.sm.err != FENYKL_AFC_E_NONE && afc.sm.err != FENYKL_AFC_E_END;
}
