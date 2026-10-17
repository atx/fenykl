/*
 * mfstats: the plugin's own contrast client (AF statistics, block 0x99C00000) in manual focus,
 * so the hardware produces per-frame contrast values without the AF running. A probe: it logs the
 * attach/detach cycle, the callback context, the HPF3 values and everything needed to judge whether
 * the firmware's AF still behaves.
 *
 * The client itself is the SDK's (sdk/lib/afc.c, docs/afc.md): a snapshot of fcs's contrast_setting
 * taken in S-AF framing, attached with the library's DMA buffer, number = 0xFFFF, every update bit.
 */
#include <fenykl.h>
#include <stdbool.h>
#include <afc.h>
#include "log.h"

/* firmware data, v1.6 */
#define MFS_EVAL_STATE 0x605A8C38u	/* u8 fcs_af_eval_state: 0 idle, 2 running, 3 stop, 1 last, 4 update */
#define MFS_CTX_PTR 0x605AED1Cu		/* u32 fcs contrast ctx (cached alias), 0 when freed */
#define MFS_DMA_PTR 0x605AED20u		/* u32 g_contrast_dma_addr, 0 when freed */
#define MFS_CLIENT 0x605AEFA0u		/* u32 contrast driver: current client */
#define MFS_QUEUED 0x605AEFA4u		/* u32 contrast driver: queued client */
#define MFS_DRV_DBG 0x605AEFD4u		/* u16 driver debug mode */
#define MFS_CAM_STATUS 0x605AED51u	/* u8 camtop status: 3 HS, 4 HSSC, ... */
#define MFS_TF_PTR 0x60575E50u		/* u32 take_focus */
#define MFS_TRG 0x605519A4u		/* u8 trigger snapshot: bit 0 clear = half or full press */
#define MFS_GUI_SUPER 0x60702278u
#define MFS_GUI_STATE 0x60702279u
#define MFS_PROP_AREA 0x84D1u		/* AF area mode, 2 = all targets */
#define MFS_PROP_TARGET 0x88B2u
#define MFS_MODE2FCS 0x60575F5Du	/* u8 {fcs index, valid}[35] */
#define MFS_TARGETS 35u

/* take_focus offsets (u8) */
#define MFS_TF_MODE 0x05u		/* 0 S-AF, 1 MF, 2 C-AF */
#define MFS_TF_MOVIE 0x07u
#define MFS_TF_RESULT 0x26u
#define MFS_TF_LV_MODE 0xD4u

/* fcs contrast ctx offsets */
#define MFS_CTX_PATTERN 0x2D0u
#define MFS_CTX_CUTOFF 0x2D2u		/* u16[4] HPF1/2/3/V */
#define MFS_CTX_REQ 0x2DCu		/* its cam_request object */

#define MFS_DMA_USED (FENYKL_AFC_CHANNELS * 5u * FENYKL_AFC_ROW_WORDS)

#define MFS_POLL_TICKS 10
#define MFS_RUN_MS 300000u		/* 5 minutes, then detach for good */
#define MFS_END_WAIT_MS 2000u
#define MFS_VAL_MS 200u			/* value lines while attached */
#define MFS_HB_MS 10000u
#define MFS_FLUSH_MS 500u
#define MFS_FLUSH_TRIES 50u
#define MFS_GROUPS 4u
#define MFS_ARGS 4u

struct mfs_plugin {
	const struct fenykl_afc *afc;
	uint32_t snaps;			/* snapshots logged */
	uint32_t val_ms, hb_ms;
	uint32_t prev[MFS_GROUPS][MFS_ARGS];
	bool seen[MFS_GROUPS];
	bool dma_moved;			/* the buffer changed since the attach */
	uint32_t moved_at;		/* attach number dma_moved belongs to */
	uint32_t polls;
};

static struct mfs_plugin mfs;

static FENYKL_LOG_MODULE(mfs_mod, "mfs", FENYKL_LOG_INFO);
static FENYKL_LOG_MODULE(mfs_v_mod, "v", FENYKL_LOG_INFO);

static const char *const mfs_snap_fmt[FENYKL_AFC_SET_INFO / 16] = {
	"s00 %08x %08x %08x %08x",
	"s10 %08x %08x %08x %08x",
	"s20 %08x %08x %08x %08x",
	"s30 %08x %08x %08x %08x",
	"s40 %08x %08x %08x %08x",
	"s50 %08x %08x %08x %08x",
	"s60 %08x %08x %08x %08x",
	"s70 %08x %08x %08x %08x",
	"s80 %08x %08x %08x %08x",
};

static uint32_t mfs_rd8(uint32_t a)
{
	return *(volatile uint8_t *)a;
}

static uint32_t mfs_rd16(uint32_t a)
{
	return *(volatile uint16_t *)a;
}

static uint32_t mfs_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

static uint32_t mfs_prop_u8(unsigned int prop)
{
	const volatile uint8_t *p = fw_prop_get_obj(prop);

	return p ? p[0] : 0xFFu;
}

/* --- snapshot --- */

/* A new snapshot in the library: its words and fcs's cut-offs and request. */
static void mfs_snap_log(void)
{
	const uint32_t *w = (const uint32_t *)mfs.afc->snap;
	uint32_t ctx = mfs_rd32(MFS_CTX_PTR);

	if (mfs.afc->snaps == mfs.snaps)
		return;
	mfs.snaps = mfs.afc->snaps;
	fenykl_log_info(&mfs_mod, "snap #%u: pat %02x hpf %u %u", mfs.snaps, mfs.afc->snap_pat,
			ctx ? mfs_rd16(ctx + MFS_CTX_CUTOFF) : 0, ctx ? mfs_rd16(ctx + MFS_CTX_CUTOFF + 2) : 0);
	if (ctx)
		fenykl_log_info(&mfs_mod, "snap: hpf3 %u hpfv %u req4 %08x req8 %08x",
				mfs_rd16(ctx + MFS_CTX_CUTOFF + 4), mfs_rd16(ctx + MFS_CTX_CUTOFF + 6),
				mfs_rd32(ctx + MFS_CTX_REQ + 4), mfs_rd32(ctx + MFS_CTX_REQ + 8));
	for (uint32_t i = 0; i < FENYKL_AFC_SET_INFO / 16; i++)
		fenykl_log_info(&mfs_mod, mfs_snap_fmt[i], w[4 * i], w[4 * i + 1], w[4 * i + 2], w[4 * i + 3]);
}

/* --- logging --- */

/* Callback counts: all so far, and since the last attach (frames = AF_ST callbacks). */
static void mfs_log_cbs(const char *fmt)
{
	const volatile struct fenykl_afc_cb *c = &mfs.afc->cb;

	fenykl_log_info(&mfs_mod, fmt, c->calls, c->cb_stops, c->calls - mfs.afc->attach_calls,
			c->st - mfs.afc->attach_st);
}

static const char *mfs_stop_by(void)
{
	if (mfs.afc->sm.by_cb)
		return "cb";
	return mfs.afc->sm.why == FENYKL_AFC_W_STALL ? "stall" : "task";
}

/* Back to idle or off: the done callback that ended a stop, or its absence. */
static void mfs_log_detached(bool stop_done, bool no_done)
{
	const struct fenykl_afc *a = mfs.afc;
	const volatile struct fenykl_afc_cb *c = &a->cb;

	if (stop_done) {
		fenykl_log_info(&mfs_mod, "done #%u: %u us after the %s stop, status %x", a->attaches,
				FENYKL_LOG_ELAPSED(a->sm.by_cb ? c->cb_stop_us : a->stop_us, c->done_us),
				mfs_stop_by(), c->done_status);
		mfs_log_cbs("  cb %u cbstop %u, this attach: cb %u frames %u");
	} else if (no_done) {
		fenykl_log_err(&mfs_mod, "no done callback within %u ms of the frames after the stop",
			       FENYKL_AFC_DONE_MS);
	}
	fenykl_log_info(&mfs_mod, "  done %u irq %u core %u, cl %08x", c->done, c->done_irq, c->done_core,
			mfs_rd32(MFS_CLIENT));
	fenykl_log_info(&mfs_mod, "  q %08x, cb irq %u cpu1 %u bad %u", mfs_rd32(MFS_QUEUED), c->irq, c->cpu1,
			c->bad_req);
}

static void mfs_log_transition(enum fenykl_afc_state from, uint32_t done_before, uint32_t now)
{
	const struct fenykl_afc *a = mfs.afc;
	const volatile struct fenykl_afc_cb *c = &a->cb;
	enum fenykl_afc_state to = a->sm.state;
	bool live = from == FENYKL_AFC_ATTACHING || from == FENYKL_AFC_ATTACHED;
	bool done = c->done != done_before;

	if (to == from)
		return;
	if (to == FENYKL_AFC_ATTACHED) {
		fenykl_log_info(&mfs_mod, "attached #%u: first cb %u us after the request, irq %u core %u",
				a->attaches, FENYKL_LOG_ELAPSED(a->req_us, c->first_us), c->first_irq,
				c->first_core);
		fenykl_log_info(&mfs_mod, "  cb ev %x d0 %x psw %04x", c->events, c->unk, c->psw);
	} else if (live && to == FENYKL_AFC_OFF) {
		fenykl_log_err(&mfs_mod, "done callback while attached, no stop requested, status %x",
			       c->done_status);
		mfs_log_cbs("  cb %u cbstop %u, this attach: cb %u frames %u");
	} else if (live) {
		fenykl_log_info(&mfs_mod, "stop #%u: %s stop, gates %03x err %s", a->attaches, mfs_stop_by(),
				a->sm.why, fenykl_afc_err_name[a->sm.err]);
		fenykl_log_info(&mfs_mod, "  attached %u ms, st %u cp %u", now - a->attach_ms, c->st, c->cp);
		mfs_log_cbs("  cb %u cbstop %u, this attach: cb %u frames %u");
	}
	if (to == FENYKL_AFC_IDLE || to == FENYKL_AFC_OFF)
		mfs_log_detached(done && !(live && to == FENYKL_AFC_OFF), from == FENYKL_AFC_DETACHING && !done);
	fenykl_log_info(&mfs_mod, "sm %s -> %s at %u ms, err %s", fenykl_afc_state_name[from],
			fenykl_afc_state_name[to], now, fenykl_afc_err_name[a->sm.err]);
}

static void mfs_read_drv(uint32_t tf, uint32_t *v)
{
	(void)tf;
	v[0] = mfs_rd8(MFS_EVAL_STATE);
	v[1] = mfs_rd32(MFS_CLIENT);
	v[2] = mfs_rd32(MFS_QUEUED);
	v[3] = fenykl_afc_slot_owner(MFS_CLIENT) | fenykl_afc_slot_owner(MFS_QUEUED) << 4;
}

static void mfs_read_tf(uint32_t tf, uint32_t *v)
{
	v[0] = tf ? mfs_rd8(tf + MFS_TF_MODE) : 0xFF;
	v[1] = tf ? mfs_rd8(tf + MFS_TF_MOVIE) : 0xFF;
	v[2] = tf ? mfs_rd8(tf + MFS_TF_LV_MODE) : 0xFF;
	v[3] = tf ? mfs_rd8(tf + MFS_TF_RESULT) : 0xFF;
}

static void mfs_read_gui(uint32_t tf, uint32_t *v)
{
	(void)tf;
	v[0] = mfs_rd8(MFS_GUI_SUPER);
	v[1] = mfs_rd8(MFS_GUI_STATE);
	v[2] = mfs_rd8(MFS_TRG);
	v[3] = mfs_rd8(MFS_CAM_STATUS);
}

static void mfs_read_fcs(uint32_t tf, uint32_t *v)
{
	uint32_t ctx = mfs_rd32(MFS_CTX_PTR);

	(void)tf;
	v[0] = ctx;
	v[1] = mfs_rd32(MFS_DMA_PTR);
	v[2] = ctx ? mfs_rd8(ctx + MFS_CTX_PATTERN) : 0xFF;
	v[3] = ctx ? mfs_rd16(ctx + MFS_CTX_CUTOFF + 4) : 0;
}

struct mfs_group {
	const char *fmt;
	void (*read)(uint32_t tf, uint32_t *v);
};

static const struct mfs_group mfs_groups[MFS_GROUPS] = {
	{ "drv st %u cl %08x q %08x own %02x", mfs_read_drv },
	{ "tf m %u mv %u lvm %u res %u", mfs_read_tf },
	{ "gui %02x %02x trg %02x cam %u", mfs_read_gui },
	{ "fcs ctx %08x dma %08x pat %02x h3 %u", mfs_read_fcs },
};

static void mfs_poll_groups(uint32_t tf)
{
	for (uint32_t i = 0; i < MFS_GROUPS; i++) {
		uint32_t v[MFS_ARGS];

		mfs_groups[i].read(tf, v);
		if (mfs.seen[i] && !memcmp(v, mfs.prev[i], sizeof(v)))
			continue;
		fenykl_log_info(&mfs_mod, mfs_groups[i].fmt, v[0], v[1], v[2], v[3]);
		memcpy(mfs.prev[i], v, sizeof(v));
		mfs.seen[i] = true;
	}
}

/* log2 in 1/100: integer part and the mantissa's linear fraction. */
static void mfs_log2(uint32_t v, uint32_t *ip, uint32_t *fp)
{
	uint32_t e = 0;

	*ip = 0;
	*fp = 0;
	if (!v)
		return;
	for (uint32_t x = v; x > 1; x >>= 1)
		e++;
	*ip = e;
	*fp = ((e >= 8 ? v >> (e - 8) : v << (8 - e)) & 0xFFu) * 100u >> 8;
}

static uint32_t mfs_area_word(uint32_t idx)
{
	return idx / mfs.afc->snap_cols * FENYKL_AFC_ROW_WORDS + idx % mfs.afc->snap_cols;
}

/* The meter's area: all targets -> max HPF3, else prop 0x88B2 via mode2fcs. */
static uint32_t mfs_select(uint32_t max_idx)
{
	uint32_t cols = mfs.afc->snap_cols, rows = mfs.afc->snap_rows;
	uint32_t t = mfs_prop_u8(MFS_PROP_TARGET);
	uint32_t idx = cols * rows;

	if (mfs_prop_u8(MFS_PROP_AREA) == 2)
		return max_idx;
	if (t < MFS_TARGETS && mfs_rd8(MFS_MODE2FCS + 2 * t + 1))
		idx = mfs_rd8(MFS_MODE2FCS + 2 * t);
	if (idx >= cols * rows)
		idx = rows / 2 * cols + cols / 2;
	return idx;
}

static void mfs_log_values(void)
{
	const volatile uint32_t *b = fenykl_afc_dma();
	const volatile struct fenykl_afc_cb *c = &mfs.afc->cb;
	uint32_t cw = mfs.afc->snap_rows * FENYKL_AFC_ROW_WORDS;
	uint32_t areas = mfs.afc->snap_cols * mfs.afc->snap_rows;
	uint32_t max = 0, max_idx = 0, sel, w, ip, fp, fill = 0;

	if (mfs.moved_at != mfs.afc->attaches) {
		mfs.moved_at = mfs.afc->attaches;
		mfs.dma_moved = false;
	}
	for (uint32_t i = 0; i < areas; i++) {
		uint32_t x = b[2 * cw + mfs_area_word(i)];

		fill += x == FENYKL_AFC_DMA_FILL;
		if (x > max) {
			max = x;
			max_idx = i;
		}
	}
	if (!mfs.dma_moved && fill < areas) {
		mfs.dma_moved = true;
		fenykl_log_info(&mfs_mod, "dma: first write seen, %u of %u HPF3 areas still fill", fill, areas);
	}
	sel = mfs_select(max_idx);
	w = mfs_area_word(sel);
	mfs_log2(b[2 * cw + w], &ip, &fp);
	fenykl_log_info(&mfs_v_mod, "f %u cp %u cb %u gap %u", c->st, c->cp, c->calls, fenykl_afc_take_gap());
	fenykl_log_info(&mfs_v_mod, "a%u h3 %u l2 %u.%02u", sel, b[2 * cw + w], ip, fp);
	mfs_log2(max, &ip, &fp);
	fenykl_log_info(&mfs_v_mod, "mx%u h3 %u l2 %u.%02u", max_idx, max, ip, fp);
	fenykl_log_info(&mfs_v_mod, "a h1 %u h2 %u hv %u ev %x", b[w], b[cw + w], b[3 * cw + w], c->events);
	fenykl_log_info(&mfs_v_mod, "s st %u cl %08x q %08x info %u", mfs_rd8(MFS_EVAL_STATE),
			mfs_rd32(MFS_CLIENT), mfs_rd32(MFS_QUEUED),
			*(const uint32_t *)(mfs.afc->setting + FENYKL_AFC_SET_INFO + 8));
}

/* Words past the used part should keep the fill: the DMA stays inside its 4 x rows x 16 words. */
static uint32_t mfs_dma_overrun(void)
{
	const volatile uint32_t *b = fenykl_afc_dma();
	uint32_t n = 0;

	for (uint32_t i = MFS_DMA_USED; i < FENYKL_AFC_DMA_WORDS; i++)
		n += b[i] != FENYKL_AFC_DMA_FILL;
	return n;
}

static void mfs_heartbeat(uint32_t now)
{
	const struct fenykl_afc *a = mfs.afc;
	struct fenykl_log_stats st;

	fenykl_log_get_stats(&st);
	fenykl_log_info(&mfs_mod, "hb %u ms: sm %s gates %03x err %s", now, fenykl_afc_state_name[a->sm.state],
			a->gates, fenykl_afc_err_name[a->sm.err]);
	fenykl_log_info(&mfs_mod, "hb: attaches %u done %u irq %u", a->attaches, a->cb.done, a->cb.irq);
	mfs_log_cbs("hb: cb %u cbstop %u, this attach: cb %u frames %u");
	/* The buffer holds the fill pattern only from the first attach on. */
	if (a->attaches)
		fenykl_log_info(&mfs_mod, "hb: snaps %u torn %u overrun %u lost %u", a->snaps, a->torn,
				mfs_dma_overrun(), st.overwritten0);
	else
		fenykl_log_info(&mfs_mod, "hb: snaps %u torn %u overrun n/a lost %u", a->snaps, a->torn,
				st.overwritten0);
}

static void mfs_final_flush(void)
{
	for (uint32_t i = 0; i < MFS_FLUSH_TRIES; i++) {
		if (fenykl_log_flush() != FENYKL_LOG_EBUSY)
			return;
		fw_OlyTaskSleep(MFS_POLL_TICKS);
	}
}

/* One poll: the library's step (or its end), then the logs. */
static void mfs_poll(bool end)
{
	enum fenykl_afc_state from = mfs.afc->sm.state;
	uint32_t done_before = mfs.afc->sm.done;
	uint32_t now;

	if (end)
		fenykl_afc_end();
	else
		fenykl_afc_poll(true);
	now = mfs.afc->ms;
	mfs_snap_log();
	mfs_log_transition(from, done_before, now);
	mfs_poll_groups(mfs_rd32(MFS_TF_PTR));
	if (mfs.afc->sm.state == FENYKL_AFC_ATTACHED && now - mfs.val_ms >= MFS_VAL_MS) {
		mfs.val_ms = now;
		mfs_log_values();
	}
	if (now - mfs.hb_ms >= MFS_HB_MS) {
		mfs.hb_ms = now;
		mfs_heartbeat(now);
	}
	mfs.polls++;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config cfg = { .flush_ms = MFS_FLUSH_MS, .tee = true };
	int init = fenykl_log_init(&cfg);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;
	uint32_t end;

	(void)load_base;
	(void)image_size;
	fenykl_afc_init();
	mfs.afc = fenykl_afc_get();
	fenykl_log_info(&mfs_mod, "mfstats: log %d task %d, %u s, dma %08x", init, task, MFS_RUN_MS / 1000u,
			(uint32_t)fenykl_afc_dma());
	fenykl_log_info(&mfs_mod, "drv dbg %u, trg %02x", mfs_rd16(MFS_DRV_DBG), mfs_rd8(MFS_TRG));

	while (mfs.afc->ms < MFS_RUN_MS) {
		mfs_poll(false);
		fw_OlyTaskSleep(MFS_POLL_TICKS);
	}
	mfs_poll(true);
	for (end = mfs.afc->ms; mfs.afc->sm.state == FENYKL_AFC_DETACHING && mfs.afc->ms - end < MFS_END_WAIT_MS;) {
		fw_OlyTaskSleep(MFS_POLL_TICKS);
		mfs_poll(false);
	}
	mfs_heartbeat(mfs.afc->ms);
	fenykl_log_get_stats(&st);
	fenykl_log_info(&mfs_mod, "end: sm %s err %s, log lost %u bad %u", fenykl_afc_state_name[mfs.afc->sm.state],
			fenykl_afc_err_name[mfs.afc->sm.err], st.overwritten0, st.bad);
	mfs_final_flush();
}
