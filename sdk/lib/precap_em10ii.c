/*
 * Pre-capture (sdk/docs/precap.md), E-M10 II part: the whole mechanism.
 * Key control (CPU0 task, PRECAP_PRIO): a half press held PRECAP_READY_MS past AF ready (TAKE state
 * RdyTrig2Wait) injects the shutter full press (ui_key_inject_level), so the stock silent H burst
 * starts; a half release injects the release. The task follows TAKE through its history ring.
 * Wrappers on three Base sequence slots (HIGHLEVEL_MAIN, no log or blocking call):
 * - frame done holds the last PRECAP_N finished frames unrecorded in the buffer manager's list 1
 *   (flag 0x200 of the sequence flag word around the stock method skips only the record handoff)
 *   and frees the oldest with the stock frame discard once TAKE has released its handle;
 * - commit: the first frame done after a real full press records the held frames oldest first with
 *   seq_record_frame, then the stock method records its own; every later frame is stock;
 * - cancel: the frame done with the continuous stop bit, and every later one, drops all frames;
 *   the take end wrapper then calls endPaintBlack, which a burst without a record needs;
 * - take fail drops the held frames before its stock body.
 * Any fail-safe disarms: the next wrapper call drops the held frames, our press is released once
 * the real one is up, the slots go back once nothing is in flight, and nothing is injected until
 * fenykl_precap_enable(false) and (true).
 */
#include <precap.h>
#include <body.h>

#define PRECAP_N 6u			/* frames held; one PRIM/THUM block must stay free */
#define PRECAP_DEEP (PRECAP_N + 1u)	/* deeper than this trips the fail-safe */
#define PRECAP_PRIO 60			/* below UI_IN, HIGHLEVEL_MAIN, REC and the library tasks */
#define PRECAP_STACK 0x1000u
#define PRECAP_POLL_MS 10

/* key control timing, ms */
#define PRECAP_READY_MS 300u		/* after RdyTrig2Wait, before the injected press */
#define PRECAP_TAKE1_MS 1000u		/* injected press to the first HsContTake1 */
#define PRECAP_SYNC_MS 200u		/* injected release to snapshot == shadow */
#define PRECAP_KRING_MS 200u		/* key ring non-empty this long: not draining */
#define PRECAP_END_MS 2000u		/* release to the end of the take */
/* burst checks, ms */
#define PRECAP_STALL_MS 2000u		/* HsContWait with PRIM free 0 while frames are held */
#define PRECAP_SUM_MS 2000u		/* burst end to the summary without a take end call */
#define PRECAP_BACK_COMMIT_MS 30000u	/* pools, handles, +0x190, shooting flag back after a commit */
#define PRECAP_BACK_CANCEL_MS 5000u

/* keys: 0 bit = active */
#define PRECAP_TRG2 0x01u		/* key_state1 / key_shadow1: full press */
#define PRECAP_TRG1 0x02u		/* key_state0: half press */
#define PRECAP_KRING_BYTES 60u
#define PRECAP_KRING_REC 6u
#define PRECAP_KRING_FULL 8u		/* of 9 usable records */
#define PRECAP_KEY_FULL 0x101u
#define PRECAP_LVL_PRESS 0u
#define PRECAP_LVL_RELEASE 1u

/* sequence flag word */
#define PRECAP_SKIP 0x200u		/* skips the record handoff, nothing else in frame done */
#define PRECAP_SKIP_ANY 0x4200u		/* frame done's record test */
#define PRECAP_NO_PAINT 0x100u		/* endPaintBlack returns at once */

/* Base sequence object */
#define PRECAP_O_C8 0xC8u		/* != 0: the record handoff may stop the record */
#define PRECAP_O_D4 0xD4u		/* 0: discard and record decrement +0x190 */
#define PRECAP_O_PEND 0x190u		/* u16 buffers allocated, not recorded or discarded */
#define PRECAP_O_ST 0x194u		/* take state bits */
#define PRECAP_ST_STOP 0x8000u		/* continuous stop sent (TAKEPIC_CONTI_STOP 0x607EB066) */
#define PRECAP_ST_RV 0x10000u		/* recview started (startRecView 0x607ECAF6) */
#define PRECAP_O_MGR 0x208u		/* buffer manager */
#define PRECAP_O_BUF 0x210u		/* newest buffer, read by the end-of-burst recview */
#define PRECAP_O_RECCTL 0x260u		/* RecordController, 0: the record handoff returns at once */
#define PRECAP_O_SEND 0x284u		/* side list object */
#define PRECAP_S_N 0xB8u		/* its u16 count */
#define PRECAP_S_SLOTS 9u

/* buffer manager and its buffers */
#define PRECAP_M_LIST1 0x118u		/* frames done and not recorded, oldest first */
#define PRECAP_B_NEXT1 0x28u
#define PRECAP_B_STATE 0x38u		/* & 0xF */
#define PRECAP_B_TAKEN 3u		/* in list 1 */
#define PRECAP_B_HID 0xCu		/* take handle id */
#define PRECAP_B_KIND 0x10u
#define PRECAP_B_REL 0x8u		/* picture released */

/* take handles and pools */
#define PRECAP_HT_SIZE 0x14u
#define PRECAP_HT_N 0x1Du
#define PRECAP_HT_BUSY 0x12u		/* 1: TAKE owns the handle */
#define PRECAP_POOL_FREE 0x14u
#define PRECAP_POOL_TOTAL 0x18u

/* TAKE states */
#define PRECAP_ST_IDLE 0u
#define PRECAP_ST_TRIG2WAIT 5u
#define PRECAP_ST_FCSTAKE 7u
#define PRECAP_ST_STANDBY 11u
#define PRECAP_ST_STLRDY1 12u
#define PRECAP_ST_STLRDY2 13u
#define PRECAP_ST_STLTAKE 14u
#define PRECAP_ST_STLEND2 19u
#define PRECAP_ST_HSTAKE1 24u
#define PRECAP_ST_HSWAIT 28u
#define PRECAP_ST_HSRDY 29u
#define PRECAP_ST_HSEND 30u
#define PRECAP_HK_STATE 0x40u		/* take_hist kind: state entered */

/* take context */
#define PRECAP_DRIVE_H 3u
#define PRECAP_SHOCK_SILENT 2u

/* settings */
#define PRECAP_PROP_SEQ 0x8391u
#define PRECAP_PROP_H 0x85B1u
#define PRECAP_PROP_SILENT 0xF101u
#define PRECAP_PROP_NR 0xF121u
#define PRECAP_PROP_TV 0xA02Au		/* s16: -0x8000 live bulb/time, -0x7FFF live composite */
#define PRECAP_TV_BULB 0x8000u
#define PRECAP_TV_COMPOSITE 0x8001u
#define PRECAP_SEQ_NONE 0u		/* seq_kind until the first take (.data1 initial value) */
#define PRECAP_SEQ_BASE 1u		/* seq_kind of the Base sequence */
#define PRECAP_GATE_KIND 0x4520u	/* gate result for seq_kind */
#define PRECAP_GATE_TV PRECAP_PROP_TV

enum precap_mode { PRECAP_M_HOLD, PRECAP_M_COMMIT, PRECAP_M_CANCEL };

/* Wrapper call kinds: frame done held or dropped, frame done that commits, take fail. */
enum precap_kind { PRECAP_K_HOLD, PRECAP_K_COMMIT, PRECAP_K_FAIL };

/* Why a frame done passes to stock, PRECAP_GO if it does not. */
enum precap_why {
	PRECAP_GO, PRECAP_P_OFF, PRECAP_P_RING, PRECAP_P_AFTER, PRECAP_P_MODE, PRECAP_P_NR,
	PRECAP_P_SEQ, PRECAP_P_MGR, PRECAP_P_IDLE, PRECAP_P_FLAGS, PRECAP_P_REC, PRECAP_P_SIDE,
};

enum precap_fail {
	PRECAP_F_NONE, PRECAP_F_FLAGS, PRECAP_F_HEAD, PRECAP_F_FREE, PRECAP_F_POOLS,
	PRECAP_F_STALL, PRECAP_F_STATE, PRECAP_F_HANDLE, PRECAP_F_SLOT, PRECAP_F_SIDE,
	PRECAP_F_WALK, PRECAP_F_BAD, PRECAP_F_DEEP, PRECAP_F_HELD, PRECAP_F_USED, PRECAP_F_FLAG,
	PRECAP_F_REC, PRECAP_F_PEND, PRECAP_F_PASS, PRECAP_F_KEY, PRECAP_F_INSTALL, PRECAP_FAILS
};

static const char *const precap_fail_names[PRECAP_FAILS] = {
	"none", "flag word changed", "list head changed", "discard did not free",
	"pools not back", "stall", "take state", "handle not freed", "slot word changed",
	"side list grew", "list 1 malformed", "bad head", "ring too deep", "held frames left",
	"handles not free", "shooting flag left at 1", "record check", "+0x190 not back to 0",
	"pass with held frames", "key control", "slot install",
};

/* Outcome of the commit, or of one record in it. */
enum precap_cres {
	PRECAP_C_OK, PRECAP_C_REC, PRECAP_C_FLAGS, PRECAP_C_HEAD, PRECAP_C_BUSY, PRECAP_C_STAYED,
	PRECAP_C_STATE, PRECAP_C_PEND, PRECAP_C_ORDER
};

/* Drop outcome of one head. */
enum { PRECAP_D_OK, PRECAP_D_BUSY, PRECAP_D_BAD };

/* Key control: idle: waiting for a half press; wait: half down, waiting for AF ready; pass: the
   owner pressed the full press first, stock; run: our press held; stop: our release out or the
   burst stopped, waiting for the take to end; abort: release our press if it is still held. */
enum precap_cst { PRECAP_C_IDLE, PRECAP_C_WAIT, PRECAP_C_PASS, PRECAP_C_RUN, PRECAP_C_STOP,
		  PRECAP_C_ABORT };

static const char *const precap_cst_names[] = { "idle", "wait", "pass", "run", "stop", "abort" };

/* How a burst ended, for its summary line. */
enum precap_end { PRECAP_E_STOCK, PRECAP_E_COMMIT, PRECAP_E_CANCEL, PRECAP_E_OTHER };

static const char *const precap_sum_fmt[] = {
	"stock burst: %u frames, recorded %u, fps x100 %u",
	"commit: %u frames, saved %u, dropped %u, fps x100 %u",
	"cancel: %u frames, saved %u, dropped %u, fps x100 %u",
	"other end: %u frames, saved %u, dropped %u, fps x100 %u",
};

/* One wrapper call, for its own checks. */
struct precap_call {
	uint32_t head;		/* list-1 head at entry */
	uint32_t cur;		/* frame done: the frame just done */
	uint32_t send;		/* side list object */
	uint32_t st;		/* sequence +0x194 at entry */
	uint32_t f0, f1;	/* flag word before / after the stock call */
	uint16_t s0, s1;	/* side list count before / after */
	uint8_t kind, why, fail, mode;
};

struct precap_keys {
	uint32_t shadow, snap, k0, wr, rd;
};

/* The current burst as TAKE's history shows it. */
struct precap_burst {
	bool on, ring, in_wait, wait_flag, sum;
	uint32_t frames, t0, t1, end_t, wait_t, holds0, tends0;
};

struct precap_ctl {
	enum precap_cst st;
	bool need_up, rdy, take1, rel_ours, real_down, sync, refuse_logged;
	uint32_t st_t, half_t, rdy_t, inj_t, rel_t, kring_t, commits, tend0;
};

struct precap_state {
	/* task -> wrappers: slots armed, our burst, real full press seen; NR prop */
	volatile bool armed, ring, commit;
	volatile uint32_t nr;
	/* wrappers -> task: first fail, frames held, ring mode, take end calls, calls in progress */
	volatile uint32_t fail, h, mode, tends, depth;
	/* this burst: frames left to the stock record, dropped, held frames recorded, frames
	   recorded from the commit on */
	volatile uint32_t kept, gone, committed, after;
	/* wrapper counts */
	volatile uint32_t holds, drops, defers, paints, take_fails, passes;
	/* any task -> task */
	volatile bool want;
	volatile uint32_t offs;		/* fenykl_precap_enable(false) calls */
	volatile enum fenykl_precap_state shown;
	/* task */
	int task;
	bool installed, failed, gate_ok, install_logged;
	uint32_t offs_seen, gate, gate_logged;
	uint32_t ms, us, t_last;	/* own ms clock */
	uint32_t hrd;			/* take history read index */
	struct precap_keys k;
	struct precap_burst b;
	struct precap_ctl c;
	bool check;			/* the end check of the last pre-capture burst runs */
	uint32_t check_t;
	/* task counts */
	uint32_t bursts, commits, cancels, saved, dropped, cycles, injections, refused, races;
	uint32_t failsafes, installs, restores, torn, hist_over, last_fail;
};

static struct precap_state precap;

/* Calls on another core than CPU0: counted and passed through, nothing else. */
static FENYKL_SHARED uint32_t precap_other_core;

FENYKL_LOG_MODULE(fenykl_precap_log, "precap", FENYKL_LOG_INFO);

static struct fenykl_slot *const precap_slots[] = {
	&fenykl_slot_seq_frame_done, &fenykl_slot_seq_take_fail, &fenykl_slot_seq_take_end,
};

#define PRECAP_SLOTS (sizeof(precap_slots) / sizeof(precap_slots[0]))

static const uint32_t precap_pools[] = { FW_POOL_PRIM, FW_POOL_THUM, FW_POOL_EXIF };

#define PRECAP_POOLS (sizeof(precap_pools) / sizeof(precap_pools[0]))

/* Settings props of the gate: element 0 must equal (ne 0) or differ from (ne 1) v. */
static const struct {
	uint16_t prop;
	uint8_t ne, v;
} precap_gate_props[] = {
	{ PRECAP_PROP_SEQ, 0, 1 },	/* sequential */
	{ PRECAP_PROP_H, 0, 2 },	/* H */
	{ PRECAP_PROP_SILENT, 0, 1 },	/* silent */
	{ 0x83A1, 0, 0 },		/* flash off (else drive L) */
	{ PRECAP_PROP_NR, 0, 0 },	/* long-exposure NR off: on or Auto triple PRIM per handle */
	{ 0xC021, 1, 2 },		/* no C-AF or C-AF+TR */
	{ 0x85F1, 0, 0 },		/* no AE / HDR bracketing */
	{ 0x96C1, 0, 0 },		/* no flash bracketing */
	{ 0x96E2, 0, 0 },		/* no focus bracketing */
	{ 0xDE11, 0, 0 },		/* no interval */
	{ 0xE951, 0, 0 },		/* the 12-block PRIM pool */
	{ 0xC222, 1, 2 },		/* not handle alloc mode 1 (other pools) */
};

#define PRECAP_GATE_PROPS (sizeof(precap_gate_props) / sizeof(precap_gate_props[0]))
#define PRECAP_SELECTED 3u		/* the first three: silent sequential H selected */

/* --- reads --- */

/* Element 0 of a property, read as the firmware's getter does; ~0 when not registered. */
static uint32_t precap_prop(unsigned int id)
{
	uint32_t p = fbody_prop_obj(id);

	if ((id & 7u) == 4u)
		return fenykl_dram_ptr(p, 4) ? fenykl_rd32(p) : ~0u;
	if ((id & 7u) == 2u)
		return fenykl_dram_ptr(p, 2) ? fenykl_rd16(p) : ~0u;
	return fenykl_dram_ptr(p, 1) ? fenykl_rd8(p) : ~0u;
}

/* The first n gate props; 0 if they hold, else the prop that does not. */
static uint32_t precap_gate_n(unsigned int n)
{
	for (unsigned int i = 0; i < n; i++) {
		uint32_t v = precap_prop(precap_gate_props[i].prop);

		if (v == ~0u || (v == precap_gate_props[i].v) == precap_gate_props[i].ne)
			return precap_gate_props[i].prop;
	}
	return 0;
}

/* The settings gate: 0 if pre-capture may run, else the prop (or PRECAP_GATE_KIND) that fails. */
static uint32_t precap_gate(void)
{
	uint32_t g = precap_gate_n(PRECAP_GATE_PROPS), tv, kind;

	if (g)
		return g;
	tv = precap_prop(PRECAP_PROP_TV);
	if (tv == ~0u || tv == PRECAP_TV_BULB || tv == PRECAP_TV_COMPOSITE)
		return PRECAP_GATE_TV;
	kind = fenykl_rd16(FW_SEQ_KIND);
	if (kind != PRECAP_SEQ_NONE && kind != PRECAP_SEQ_BASE)
		return PRECAP_GATE_KIND;
	return 0;
}

static uint32_t precap_ht(uint32_t id)
{
	return FW_TAKE_HANDLES + PRECAP_HT_SIZE * id;
}

static bool precap_pools_full(void)
{
	for (unsigned int i = 0; i < PRECAP_POOLS; i++)
		if (fenykl_rd16(precap_pools[i] + PRECAP_POOL_FREE) !=
		    fenykl_rd16(precap_pools[i] + PRECAP_POOL_TOTAL))
			return false;
	return true;
}

static uint32_t precap_handles_used(void)
{
	uint32_t n = 0;

	for (uint32_t i = 0; i < PRECAP_HT_N; i++)
		n += fenykl_rd16(precap_ht(i)) != 0;
	return n;
}

static void precap_keys_read(struct precap_keys *k)
{
	*k = (struct precap_keys){
		.shadow = fenykl_rd16(FW_KEY_SHADOW1),
		.snap = fenykl_rd16(FW_KEY_STATE1),
		.k0 = fenykl_rd8(FW_KEY_STATE0),
		.wr = fenykl_rd32(FW_KEY_RING_WR),
		.rd = fenykl_rd32(FW_KEY_RING_RD),
	};
}

static uint32_t precap_kring_n(const struct precap_keys *k)
{
	uint32_t d = k->wr >= k->rd ? k->wr - k->rd : k->wr + PRECAP_KRING_BYTES - k->rd;

	return d / PRECAP_KRING_REC;
}

static bool precap_real_full(void)
{
	return !(precap.k.shadow & PRECAP_TRG2);
}

static bool precap_snap_full(void)
{
	return !(precap.k.snap & PRECAP_TRG2);
}

static bool precap_half_down(void)
{
	return !(precap.k.k0 & PRECAP_TRG1);
}

/* --- wrappers: HIGHLEVEL_MAIN, no log or blocking call --- */

static void precap_disarm(uint32_t fail)
{
	precap.armed = false;
	if (!precap.fail)
		precap.fail = fail;
}

/* Stores the flag word with our bit set or cleared; returns the word before. */
static uint32_t precap_flags_swap(bool set)
{
	volatile uint32_t *w = (volatile uint32_t *)FW_SEQ_FLAGS;
	uint32_t psw = fw_psw_get(), v;

	fw_irq_disable_all();
	v = *w;
	*w = set ? v | PRECAP_SKIP : v & ~PRECAP_SKIP;
	fw_irq_mask_restore(psw);
	return v;
}

static bool precap_seq_ok(uint32_t s)
{
	return fenykl_dram_ptr(s, 4) && s == fenykl_rd32(FW_SEQ_BASE) &&
	       fenykl_rd32(s) == FW_SEQ_BASE_VTABLE;
}

static bool precap_buf_ok(uint32_t b)
{
	return fenykl_dram_ptr(b, 4) &&
	       (fenykl_rd32(b + PRECAP_B_STATE) & 0xFu) == PRECAP_B_TAKEN;
}

/* Entry n - 1 of list 1 when the first n entries are buffers in state 3, else 0. */
static uint32_t precap_walk(uint32_t mgr, uint32_t n)
{
	uint32_t b = fenykl_rd32(mgr + PRECAP_M_LIST1), last = 0;

	for (uint32_t i = 0; i < n; i++) {
		if (!precap_buf_ok(b))
			return 0;
		last = b;
		b = fenykl_rd32(b + PRECAP_B_NEXT1);
	}
	return last;
}

/* Take handle id of a list-1 head in state 3 that still holds its picture and handle, else 0. */
static uint32_t precap_head_id(uint32_t buf)
{
	uint32_t id;

	if (!precap_buf_ok(buf))
		return 0;
	id = fenykl_rd32(buf + PRECAP_B_HID);
	if (id - 1u >= PRECAP_HT_N - 1u || fenykl_rd8(buf + PRECAP_B_KIND) ||
	    fenykl_rd32(buf + PRECAP_B_REL) || !fenykl_rd16(precap_ht(id)))
		return 0;
	return id;
}

/* Frees the list-1 head if TAKE has released its take handle. */
static uint32_t precap_drop(uint32_t s, uint32_t mgr, struct precap_call *c)
{
	uint32_t buf = fenykl_rd32(mgr + PRECAP_M_LIST1), id, x;

	id = precap_head_id(buf);
	if (!id)
		return PRECAP_D_BAD;
	x = fenykl_rd16(precap_ht(id) + PRECAP_HT_BUSY);
	if (x == 1u)
		return PRECAP_D_BUSY;
	if (x)
		return PRECAP_D_BAD;
	fw_seq_discard_frame((void *)s, fenykl_rd32(s + PRECAP_O_D4) == 0, 0);
	if (fenykl_rd32(mgr + PRECAP_M_LIST1) == buf) {
		c->fail = PRECAP_F_FREE;
		return PRECAP_D_BAD;
	}
	precap.h--;
	precap.gone++;
	precap.drops++;
	/* as the stock no-record branch without recview (0x607EAEDE) */
	*(volatile uint32_t *)(s + PRECAP_O_BUF) = 0;
	if (fenykl_rd16(precap_ht(id))) {
		c->fail = PRECAP_F_HANDLE;
		return PRECAP_D_BAD;
	}
	return PRECAP_D_OK;
}

/* Drops heads until at most target frames are held; a busy head waits for a later call. */
static void precap_trim(uint32_t s, uint32_t mgr, uint32_t target, struct precap_call *c)
{
	while (precap.h > target) {
		uint32_t k = precap_drop(s, mgr, c);

		if (k == PRECAP_D_OK)
			continue;
		if (k == PRECAP_D_BUSY) {
			precap.defers++;
			return;
		}
		if (!c->fail)
			c->fail = PRECAP_F_BAD;
		return;
	}
}

/* Drops every held frame before a stock call, so that the stock method sees the list as in a
   stock burst. */
static void precap_flush(uint32_t s, struct precap_call *c)
{
	uint32_t mgr;

	if (!precap.h || !precap_seq_ok(s))
		return;
	mgr = fenykl_rd32(s + PRECAP_O_MGR);
	if (!fenykl_dram_ptr(mgr, 4) || !precap_walk(mgr, precap.h)) {
		c->fail = PRECAP_F_WALK;
		return;
	}
	precap_trim(s, mgr, 0, c);
	if (precap.h && !c->fail)
		c->fail = PRECAP_F_HELD;
}

/* The ring mode for one frame-done call of our burst: the first frame done after the commit
   flag commits, one with the stop bit before it cancels; both are final. */
static uint32_t precap_mode_next(struct precap_call *c)
{
	if (precap.mode == PRECAP_M_COMMIT || precap.mode == PRECAP_M_CANCEL)
		return precap.mode;
	if (precap.commit) {
		c->kind = PRECAP_K_COMMIT;
		return PRECAP_M_COMMIT;
	}
	return c->st & PRECAP_ST_STOP ? PRECAP_M_CANCEL : PRECAP_M_HOLD;
}

/* The arm decision for one frame-done call. */
static uint32_t precap_check(uint32_t s, struct precap_call *c)
{
	uint32_t send;

	if (!precap.armed)
		return PRECAP_P_OFF;
	if (!precap.ring)
		return PRECAP_P_RING;
	if (c->mode == PRECAP_M_COMMIT && c->kind != PRECAP_K_COMMIT)
		return PRECAP_P_AFTER;
	if (fenykl_rd8(FW_TAKE_DRIVE) != PRECAP_DRIVE_H ||
	    fenykl_rd8(FW_TAKE_SHOCK) != PRECAP_SHOCK_SILENT)
		return PRECAP_P_MODE;
	if (precap.nr != 0)
		return PRECAP_P_NR;
	if (!precap_seq_ok(s))
		return PRECAP_P_SEQ;
	if (!fenykl_dram_ptr(fenykl_rd32(s + PRECAP_O_MGR), 4))
		return PRECAP_P_MGR;
	if (c->kind == PRECAP_K_COMMIT)
		return PRECAP_GO;
	if (!fenykl_rd16(FW_SEQ_INFLIGHT))
		return PRECAP_P_IDLE;
	if (fenykl_rd32(FW_SEQ_FLAGS) & PRECAP_SKIP_ANY)
		return PRECAP_P_FLAGS;
	if (!fenykl_rd32(s + PRECAP_O_RECCTL) || fenykl_rd32(s + PRECAP_O_C8))
		return PRECAP_P_REC;
	send = fenykl_rd32(s + PRECAP_O_SEND);
	c->send = send;
	if (!send)
		return PRECAP_GO;
	if (!fenykl_dram_ptr(send, 4))
		return PRECAP_P_SIDE;
	c->s0 = fenykl_rd16(send + PRECAP_S_N);
	if (c->s0 >= PRECAP_S_SLOTS)
		return PRECAP_P_SIDE;
	return PRECAP_GO;
}

static void precap_call_done(const struct precap_call *c)
{
	if (c->fail)
		precap_disarm(c->fail);
}

/* Records the list-1 head, a held frame, with the stock handoff. A head that left list 1 is no
   longer held, whatever the other checks say. */
static uint32_t precap_record(uint32_t s, uint32_t mgr)
{
	uint32_t buf = fenykl_rd32(mgr + PRECAP_M_LIST1), id = precap_head_id(buf);
	uint32_t p0, p1, state, next, res = PRECAP_C_OK;

	if (!id)
		return PRECAP_C_HEAD;
	if (fenykl_rd16(precap_ht(id) + PRECAP_HT_BUSY))
		return PRECAP_C_BUSY;
	p0 = fenykl_rd16(s + PRECAP_O_PEND);
	fw_seq_record_frame((void *)s);
	p1 = fenykl_rd16(s + PRECAP_O_PEND);
	state = fenykl_rd32(buf + PRECAP_B_STATE) & 0xFu;
	next = fenykl_rd32(mgr + PRECAP_M_LIST1);
	if (next == buf)
		res = PRECAP_C_STAYED;
	else if (state == PRECAP_B_TAKEN)
		res = PRECAP_C_STATE;
	else if (p1 + 1u != p0)
		res = PRECAP_C_PEND;
	if (next != buf)
		precap.h--;
	if (res == PRECAP_C_OK)
		precap.committed++;
	return res;
}

/* Records every held frame, oldest first; the frame just done must then be the head. */
static uint32_t precap_commit(uint32_t s, uint32_t mgr, const struct precap_call *c)
{
	if (!precap.h)
		return PRECAP_C_OK;
	if (!fenykl_rd32(s + PRECAP_O_RECCTL) || fenykl_rd32(s + PRECAP_O_C8))
		return PRECAP_C_REC;
	if (fenykl_rd32(FW_SEQ_FLAGS) & PRECAP_SKIP_ANY)
		return PRECAP_C_FLAGS;
	while (precap.h) {
		uint32_t k = precap_record(s, mgr);

		if (k != PRECAP_C_OK)
			return k;
	}
	if (fenykl_rd32(mgr + PRECAP_M_LIST1) != c->cur)
		return PRECAP_C_ORDER;
	return PRECAP_C_OK;
}

/* The first frame done after the commit flag records the held frames; the stock method then
   records the frame just done, now the list-1 head. A failed commit drops what is still held. */
static unsigned int precap_commit_call(void *seq, uint32_t mgr, struct precap_call *c)
{
	unsigned int r;

	precap.mode = PRECAP_M_COMMIT;
	if (precap_commit((uint32_t)seq, mgr, c) != PRECAP_C_OK) {
		c->fail = PRECAP_F_REC;
		precap_trim((uint32_t)seq, mgr, 0, c);
	}
	if (precap.h && !c->fail)
		c->fail = PRECAP_F_HELD;
	precap.kept++;
	precap.after++;
	r = fw_orig_seq_frame_done(seq);
	precap_call_done(c);
	return r;
}

/* A stock call. Frames still held (disarmed, or an unexpected pass) are dropped first. */
static unsigned int precap_pass(void *seq, struct precap_call *c)
{
	unsigned int r;

	if (precap.h) {
		precap_flush((uint32_t)seq, c);
		if (!c->fail && c->why != PRECAP_P_OFF)
			c->fail = PRECAP_F_PASS;
	}
	precap.kept++;
	if (c->why == PRECAP_P_AFTER)
		precap.after++;
	if (c->why != PRECAP_P_OFF && c->why != PRECAP_P_RING)
		precap.passes++;
	r = fw_orig_seq_frame_done(seq);
	precap_call_done(c);
	return r;
}

static unsigned int precap_frame_done_cpu0(void *seq)
{
	uint32_t s = (uint32_t)seq, mgr = 0, target;
	struct precap_call c = { .kind = PRECAP_K_HOLD };
	unsigned int r;

	if (fenykl_dram_ptr(s, 4))
		c.st = fenykl_rd32(s + PRECAP_O_ST);
	c.mode = (uint8_t)precap_mode_next(&c);
	c.why = (uint8_t)precap_check(s, &c);
	if (c.why == PRECAP_GO) {
		mgr = fenykl_rd32(s + PRECAP_O_MGR);
		c.head = fenykl_rd32(mgr + PRECAP_M_LIST1);
		c.cur = precap_walk(mgr, precap.h + 1u);
		if (!c.cur) {
			c.fail = PRECAP_F_WALK;
			c.why = PRECAP_P_MGR;
		}
	}
	if (c.why != PRECAP_GO)
		return precap_pass(seq, &c);
	if (c.kind == PRECAP_K_COMMIT)
		return precap_commit_call(seq, mgr, &c);
	precap.mode = c.mode;
	c.f0 = precap_flags_swap(true);
	r = fw_orig_seq_frame_done(seq);
	c.f1 = precap_flags_swap(false);
	if (c.send)
		c.s1 = fenykl_rd16(c.send + PRECAP_S_N);
	if (c.f1 != (c.f0 | PRECAP_SKIP))
		c.fail = PRECAP_F_FLAGS;
	else if (fenykl_rd32(s + PRECAP_O_SEND) != c.send || c.s1 != c.s0)
		c.fail = PRECAP_F_SIDE;
	if (fenykl_rd32(mgr + PRECAP_M_LIST1) != c.head) {
		c.fail = PRECAP_F_HEAD;
		precap_call_done(&c);
		return r;
	}
	precap.h++;
	precap.holds++;
	/* no request left: nothing will end this burst through the ring */
	target = !fenykl_rd16(FW_SEQ_INFLIGHT) || c.mode == PRECAP_M_CANCEL ? 0 : PRECAP_N;
	precap_trim(s, mgr, target, &c);
	if (!c.fail && precap.h > PRECAP_DEEP)
		c.fail = PRECAP_F_DEEP;
	if (!c.fail && !target && precap.h)
		c.fail = PRECAP_F_HELD;
	precap_call_done(&c);
	return r;
}

static unsigned int precap_frame_done(void *seq)
{
	unsigned int r;

	if (FENYKL_CORE_ID() != 0) {
		(*FENYKL_SHARED_PTR(precap_other_core))++;
		return fw_orig_seq_frame_done(seq);
	}
	precap.depth++;
	r = precap_frame_done_cpu0(seq);
	precap.depth--;
	return r;
}
FENYKL_CALLBACK(seq_frame_done_cb, precap_frame_done);

/* Take fail: drop every held frame first, so the stock body discards the failed request. */
static void precap_take_fail(void *seq)
{
	struct precap_call c = { .kind = PRECAP_K_FAIL };

	if (FENYKL_CORE_ID() != 0) {
		(*FENYKL_SHARED_PTR(precap_other_core))++;
		fw_orig_seq_take_fail(seq);
		return;
	}
	precap.depth++;
	precap.take_fails++;
	precap_flush((uint32_t)seq, &c);
	fw_orig_seq_take_fail(seq);
	precap_call_done(&c);
	precap.depth--;
}
FENYKL_CALLBACK(seq_take_fail_cb, precap_take_fail);

/* Whether a burst end without a file still needs endPaintBlack (read after the stock end proc). */
static bool precap_paint_needed(uint32_t s, uint32_t sh)
{
	return precap_seq_ok(s) && !precap.kept && !precap.committed && precap.gone && !precap.h &&
	       sh == 1u && !(fenykl_rd32(s + PRECAP_O_ST) & PRECAP_ST_RV) &&
	       !(fenykl_rd32(FW_SEQ_FLAGS) & PRECAP_NO_PAINT);
}

/* Take end proc: in a burst that recorded nothing no recview starts to clear the shooting flag,
   so endPaintBlack follows the stock body. The last wrapper call of a burst. */
static void precap_take_end(void *seq)
{
	if (FENYKL_CORE_ID() != 0) {
		(*FENYKL_SHARED_PTR(precap_other_core))++;
		fw_orig_seq_take_end(seq);
		return;
	}
	precap.depth++;
	fw_orig_seq_take_end(seq);
	if (precap_paint_needed((uint32_t)seq, fenykl_rd32(FW_SHOOTING))) {
		fw_seq_end_paint_black(seq);
		precap.paints++;
	}
	precap.tends++;
	precap.depth--;
}
FENYKL_CALLBACK(seq_take_end_cb, precap_take_end);

/* --- task: fail-safe --- */

static void precap_enter(enum precap_cst cst, uint32_t t);

/* Every fail-safe ends here: the wrappers disarm, the key control releases our press, the slots
   go back once nothing is in flight and nothing is injected until the option is cycled. */
static void precap_failsafe(uint32_t fail, const char *where, uint32_t a)
{
	struct precap_state *st = &precap;

	precap_disarm(fail);
	if (st->failed)
		return;
	st->failed = true;
	st->failsafes++;
	st->last_fail = fail;
	fenykl_log_err(&fenykl_precap_log, "fail-safe: %s (%s, %x), off until re-enabled",
		       precap_fail_names[fail], where, a);
	if (st->c.st == PRECAP_C_RUN || st->c.st == PRECAP_C_WAIT)
		precap_enter(PRECAP_C_ABORT, st->ms);
}

/* --- task: bursts --- */

static uint32_t precap_fps(const struct precap_burst *b)
{
	uint32_t dt = b->t1 - b->t0, f = b->frames < 40000u ? b->frames : 40000u;

	return f > 1 && dt ? (f - 1) * 100000u / dt : 0;
}

static enum precap_end precap_end_kind(const struct precap_burst *b)
{
	if (!b->ring)
		return PRECAP_E_STOCK;
	if (precap.mode == PRECAP_M_COMMIT)
		return PRECAP_E_COMMIT;
	if (precap.mode == PRECAP_M_CANCEL || (!precap.commit && !precap.kept && !precap.committed))
		return PRECAP_E_CANCEL;
	return PRECAP_E_OTHER;
}

/* Per burst, once its take end call is in (the wrappers' last call), or after PRECAP_SUM_MS. */
static void precap_summary(void)
{
	struct precap_state *st = &precap;
	struct precap_burst *b = &st->b;
	enum precap_end e = precap_end_kind(b);
	uint32_t saved = st->committed + st->after;

	b->sum = false;
	if (e == PRECAP_E_STOCK) {
		fenykl_log_dbg(&fenykl_precap_log, precap_sum_fmt[e], b->frames, st->kept,
			       precap_fps(b));
		return;
	}
	st->bursts++;
	st->commits += e == PRECAP_E_COMMIT;
	st->cancels += e == PRECAP_E_CANCEL;
	st->saved += saved;
	st->dropped += st->gone;
	fenykl_log_info(&fenykl_precap_log, precap_sum_fmt[e], b->frames, saved, st->gone,
			precap_fps(b));
}

static void precap_burst_start(uint32_t t)
{
	struct precap_state *st = &precap;
	struct precap_burst *b = &st->b;

	if (b->sum)
		precap_summary();
	*b = (struct precap_burst){
		.on = true, .t0 = t, .ring = st->ring, .holds0 = st->holds, .tends0 = st->tends,
	};
	if (!b->ring)
		st->kept = st->gone = st->committed = st->after = 0;
}

static void precap_burst_end(uint32_t t)
{
	struct precap_state *st = &precap;
	struct precap_burst *b = &st->b;

	b->on = false;
	b->sum = true;
	b->end_t = t;
	if (!b->ring)
		return;
	st->check = true;
	st->check_t = t;
}

/* A take state outside the silent H path while frames are held trips the fail-safe. */
static bool precap_state_ok(uint32_t s)
{
	switch (s) {
	case 12: case 13: case 14:		/* StlRdy1, StlRdy2, StlTake */
	case 18: case 19:			/* StlEnd1, StlEnd2 */
	case 24: case 26: case 27: case 28: case 29: case 30: /* HsContTake1/3/4, Wait, Rdy, End */
		return true;
	default:
		return s < PRECAP_ST_STLRDY1;
	}
}

static bool precap_holding(void)
{
	return precap.b.on && precap.holds != precap.b.holds0;
}

static void precap_stall_check(uint32_t t)
{
	struct precap_burst *b = &precap.b;

	if (!b->on || !b->in_wait || b->wait_flag || t - b->wait_t < PRECAP_STALL_MS)
		return;
	if (fenykl_rd16(FW_POOL_PRIM + PRECAP_POOL_FREE) || !precap_holding())
		return;
	b->wait_flag = true;
	precap_failsafe(PRECAP_F_STALL, "HsContWait", fenykl_rd16(FW_SEQ_INFLIGHT));
}

/* After a pre-capture burst nothing may be held and the pools, handles, +0x190 and the shooting
   flag must be back: PRECAP_BACK_COMMIT_MS after a commit (developments), PRECAP_BACK_CANCEL_MS
   after a cancel. A burst that starts before carries the check to its own end. */
static void precap_back_check(uint32_t t)
{
	struct precap_state *st = &precap;
	uint32_t s = fenykl_rd32(FW_SEQ_BASE), sh = fenykl_rd32(FW_SHOOTING), pend = ~0u, used;
	uint32_t wait = st->mode == PRECAP_M_CANCEL ? PRECAP_BACK_CANCEL_MS : PRECAP_BACK_COMMIT_MS;

	if (!st->check || st->b.on)
		return;
	if (precap_seq_ok(s))
		pend = fenykl_rd16(s + PRECAP_O_PEND);
	used = precap_handles_used();
	if (precap_pools_full() && !used && !st->h && !sh && !pend) {
		st->check = false;
		fenykl_log_dbg(&fenykl_precap_log, "pools back after %u ms", t - st->check_t);
		return;
	}
	if (t - st->check_t < wait)
		return;
	st->check = false;
	if (st->h)
		precap_failsafe(PRECAP_F_HELD, "after the burst", st->h);
	else if (used)
		precap_failsafe(PRECAP_F_USED, "after the burst", used);
	else if (!precap_pools_full())
		precap_failsafe(PRECAP_F_POOLS, "after the burst",
				fenykl_rd16(FW_POOL_PRIM + PRECAP_POOL_FREE));
	else if (sh)
		precap_failsafe(PRECAP_F_FLAG, "after the burst", sh);
	else
		precap_failsafe(PRECAP_F_PEND, "after the burst", pend);
}

/* --- task: key control --- */

static void precap_enter(enum precap_cst cst, uint32_t t)
{
	struct precap_state *st = &precap;

	fenykl_log_dbg(&fenykl_precap_log, "key %s -> %s", precap_cst_names[st->c.st],
		       precap_cst_names[cst]);
	st->c.st = cst;
	st->c.st_t = t;
	if (cst != PRECAP_C_IDLE)
		return;
	st->c.need_up = true;
	st->ring = false;
	st->commit = false;
}

/* Our burst: a fresh ring for the wrappers. Set before the injection, so that no frame done of
   the burst can miss it. The last burst's summary and end checks read the counters reset here. */
static void precap_ring_start(void)
{
	struct precap_state *st = &precap;

	if (st->b.sum)
		precap_summary();
	st->mode = PRECAP_M_HOLD;
	st->kept = st->gone = st->committed = st->after = 0;
	st->commit = false;
	st->c.tend0 = st->tends;
	st->ring = true;
}

/* The only key write: one shutter full press level into the UI_IN key ring. Refused unless
   UI_IN is idle (ring empty, so the snapshot it copies is current), the snapshot agrees with the
   producer shadow on every other kind-1 bit and the full press bits fit: press with real and
   snapshot up, release with real up and snapshot down (never while the real press is down).
   0 queued, < 0 refused, > 0 the firmware refused. */
static int precap_inject(uint32_t level)
{
	struct precap_state *st = &precap;
	struct precap_keys k;
	uint32_t fit;

	precap_keys_read(&k);
	fit = level == PRECAP_LVL_PRESS ? k.shadow & k.snap : k.shadow & ~k.snap;
	if (k.wr != k.rd || ((k.shadow ^ k.snap) & ~PRECAP_TRG2) || !(fit & PRECAP_TRG2)) {
		st->refused++;
		if (!st->c.refuse_logged)
			fenykl_log_warn(&fenykl_precap_log, "%s refused: shadow %04x snap %04x ring %u",
					level == PRECAP_LVL_PRESS ? "press" : "release", k.shadow,
					k.snap, precap_kring_n(&k));
		st->c.refuse_logged = true;
		return -1;
	}
	st->injections++;
	return fw_ui_key_inject_level(PRECAP_KEY_FULL, level) ? 1 : 0;
}

static void precap_key_fail(const char *why)
{
	precap_failsafe(PRECAP_F_KEY, why, precap.c.st);
}

/* Release our press; the snapshot must then follow the shadow within PRECAP_SYNC_MS. */
static int precap_release(uint32_t t)
{
	struct precap_state *st = &precap;
	int r = precap_inject(PRECAP_LVL_RELEASE);

	if (r)
		return r;
	st->c.rel_ours = true;
	st->c.rel_t = t;
	st->c.sync = true;
	return 0;
}

static void precap_sync_check(uint32_t t)
{
	struct precap_state *st = &precap;

	if (!st->c.sync)
		return;
	if (st->k.snap == st->k.shadow) {
		st->c.sync = false;
		return;
	}
	if (t - st->c.rel_t <= PRECAP_SYNC_MS)
		return;
	st->c.sync = false;
	precap_failsafe(PRECAP_F_KEY, "snapshot does not follow the shadow",
			st->k.shadow << 16 | st->k.snap);
}

/* Slots in, wrappers armed, no fail-safe and the option on. */
static bool precap_ready(void)
{
	return precap.installed && precap.armed && !precap.failed && precap.want;
}

static void precap_idle(uint32_t t)
{
	struct precap_state *st = &precap;
	uint32_t g;

	if (st->c.need_up) {
		if (!precap_half_down() && !precap_real_full())
			st->c.need_up = false;
		return;
	}
	if (!precap_half_down() || precap_real_full() || precap_snap_full())
		return;
	g = precap_gate();
	if (!precap_ready() || g) {
		fenykl_log_dbg(&fenykl_precap_log, "half press, stock: ready %u gate %x",
			       precap_ready(), g);
		st->c.need_up = true;
		return;
	}
	st->cycles++;
	st->c.half_t = t;
	st->c.take1 = st->c.rel_ours = st->c.refuse_logged = false;
	st->c.commits = 0;
	precap_enter(PRECAP_C_WAIT, t);
}

static void precap_wait(uint32_t t)
{
	struct precap_state *st = &precap;
	uint32_t g;
	int r;

	if (!precap_half_down()) {
		fenykl_log_dbg(&fenykl_precap_log, "half released after %u ms: no injection",
			       t - st->c.half_t);
		precap_enter(PRECAP_C_IDLE, t);
		return;
	}
	if (precap_real_full()) {
		fenykl_log_dbg(&fenykl_precap_log, "full press first: stock burst");
		precap_enter(PRECAP_C_PASS, t);
		return;
	}
	g = precap_gate();
	if (!precap_ready() || g) {
		fenykl_log_dbg(&fenykl_precap_log, "no injection: ready %u gate %x", precap_ready(),
			       g);
		precap_enter(PRECAP_C_IDLE, t);
		return;
	}
	if (!st->c.rdy || t - st->c.rdy_t < PRECAP_READY_MS ||
	    fenykl_rd16(FW_TAKE_STATE) != PRECAP_ST_TRIG2WAIT)
		return;
	if (st->h) {
		precap_key_fail("frames held before the injection");
		return;
	}
	precap_ring_start();
	r = precap_inject(PRECAP_LVL_PRESS);
	if (r) {
		st->ring = false;
		if (r > 0)
			precap_key_fail("press not queued");
		return;
	}
	st->c.inj_t = t;
	st->c.kring_t = t;
	st->c.real_down = false;
	fenykl_log_dbg(&fenykl_precap_log, "inject: half %u ms, ready %u ms", t - st->c.half_t,
		       t - st->c.rdy_t);
	precap_enter(PRECAP_C_RUN, t);
}

static void precap_pass_st(uint32_t t)
{
	if (!precap_half_down() && !precap_real_full())
		precap_enter(PRECAP_C_IDLE, t);
}

/* The commit flag for the wrapper, unless the burst was already stopped (OK, Fn1, a tilt):
   that burst is a cancel. */
static void precap_commit_set(uint32_t t)
{
	struct precap_state *st = &precap;
	uint32_t s = fenykl_rd32(FW_SEQ_BASE);
	uint32_t bits = precap_seq_ok(s) ? fenykl_rd32(s + PRECAP_O_ST) : 0;

	if (bits & PRECAP_ST_STOP) {
		st->races++;
		fenykl_log_dbg(&fenykl_precap_log, "full press after the stop: cancel");
		return;
	}
	st->commit = true;
	fenykl_log_dbg(&fenykl_precap_log, "commit %u ms after the injection", t - st->c.inj_t);
}

/* A real full press while ours is held is a commit; it posts no event. */
static void precap_commits(uint32_t t)
{
	struct precap_state *st = &precap;
	bool real = precap_real_full();

	if (st->c.st != PRECAP_C_RUN || real == st->c.real_down)
		return;
	st->c.real_down = real;
	if (!real)
		return;
	st->c.commits++;
	if (st->c.commits == 1u && st->ring)
		precap_commit_set(t);
}

static void precap_run(uint32_t t)
{
	struct precap_state *st = &precap;
	int r;

	if (!st->c.take1 && t - st->c.inj_t > PRECAP_TAKE1_MS) {
		precap_key_fail("no HsContTake1");
		return;
	}
	if (precap_half_down() || precap_real_full())
		return;
	if (!precap_snap_full()) {
		precap_enter(PRECAP_C_STOP, t);
		return;
	}
	r = precap_release(t);
	if (r > 0)
		precap_key_fail("release not queued");
	else if (!r)
		precap_enter(PRECAP_C_STOP, t);
}

/* Our press, if still held, goes once the real press is up and UI_IN is idle; while the real
   press is down its stock release ends the burst. */
static void precap_abort(uint32_t t)
{
	struct precap_state *st = &precap;

	if (!precap_snap_full() || st->c.rel_ours) {
		precap_enter(PRECAP_C_STOP, t);
		return;
	}
	if (precap_real_full()) {
		st->c.st_t = t;
		return;
	}
	if (!precap_release(t)) {
		precap_enter(PRECAP_C_STOP, t);
		return;
	}
	if (t - st->c.st_t <= PRECAP_END_MS)
		return;
	fenykl_log_err(&fenykl_precap_log, "release not possible: press the shutter fully once");
	precap_enter(PRECAP_C_IDLE, t);
}

/* The ring flags stay until the take end call of the burst (the wrappers' last call), so that a
   late frame done after the stop still sees them. */
static void precap_stop(uint32_t t)
{
	struct precap_state *st = &precap;
	bool over = !st->b.on && fenykl_rd16(FW_TAKE_STATE) < PRECAP_ST_STLRDY1;
	bool tend = !st->ring || !st->installed || st->tends != st->c.tend0;

	if (over && tend) {
		precap_enter(PRECAP_C_IDLE, t);
		return;
	}
	if (t - st->c.st_t <= PRECAP_END_MS)
		return;
	if (over) {
		fenykl_log_warn(&fenykl_precap_log, "no take end call after the burst");
		precap_enter(PRECAP_C_IDLE, t);
		return;
	}
	precap_failsafe(PRECAP_F_KEY, "take did not end", fenykl_rd16(FW_TAKE_STATE));
	precap_enter(PRECAP_C_IDLE, t);
}

/* Key ring full or a record pending for PRECAP_KRING_MS while our press is held. */
static void precap_kring_check(uint32_t t)
{
	struct precap_state *st = &precap;
	uint32_t n = precap_kring_n(&st->k);

	if (!n) {
		st->c.kring_t = t;
		return;
	}
	if (n >= PRECAP_KRING_FULL)
		precap_key_fail("key ring full");
	else if (t - st->c.kring_t > PRECAP_KRING_MS)
		precap_key_fail("key ring not draining");
}

static void precap_ctl(uint32_t t)
{
	precap_sync_check(t);
	if (precap.c.st == PRECAP_C_RUN)
		precap_kring_check(t);
	switch (precap.c.st) {
	case PRECAP_C_IDLE:
		precap_idle(t);
		break;
	case PRECAP_C_WAIT:
		precap_wait(t);
		break;
	case PRECAP_C_PASS:
		precap_pass_st(t);
		break;
	case PRECAP_C_RUN:
		precap_run(t);
		break;
	case PRECAP_C_ABORT:
		precap_abort(t);
		break;
	case PRECAP_C_STOP:
		precap_stop(t);
		break;
	}
}

/* Take states while our press is held, HsContEnd apart. */
static bool precap_run_state(uint32_t s)
{
	return s == PRECAP_ST_TRIG2WAIT || s == PRECAP_ST_FCSTAKE || s == PRECAP_ST_STANDBY ||
	       s == PRECAP_ST_STLRDY1 || s == PRECAP_ST_STLRDY2 ||
	       (s >= PRECAP_ST_HSTAKE1 && s <= PRECAP_ST_HSRDY);
}

static void precap_ctl_state(uint32_t s, uint32_t t)
{
	struct precap_state *st = &precap;

	st->c.rdy = s == PRECAP_ST_TRIG2WAIT;
	if (st->c.rdy)
		st->c.rdy_t = t;
	if (st->c.st != PRECAP_C_RUN)
		return;
	if (s == PRECAP_ST_HSTAKE1)
		st->c.take1 = true;
	if (s == PRECAP_ST_HSEND) {
		if (st->c.commits && !precap_real_full()) {
			precap_enter(PRECAP_C_STOP, t);
			return;
		}
		/* OK, Fn1 or a tilt: the producer resend posts the full release; a cancel */
		fenykl_log_dbg(&fenykl_precap_log, "burst stopped without our release");
		st->c.rel_t = t;
		st->c.sync = true;
		precap_enter(PRECAP_C_ABORT, t);
		return;
	}
	if (!precap_run_state(s))
		precap_failsafe(PRECAP_F_KEY, "unexpected take state", s);
}

static void precap_state_entered(uint32_t s, uint32_t t)
{
	struct precap_burst *b = &precap.b;

	b->in_wait = false;
	if (precap_holding() && !precap_state_ok(s))
		precap_failsafe(PRECAP_F_STATE, "take state", s);
	if (s == PRECAP_ST_HSWAIT && b->on) {
		b->in_wait = true;
		b->wait_flag = false;
		b->wait_t = t;
	}
	if (s == PRECAP_ST_STLTAKE || s == PRECAP_ST_HSTAKE1) {
		if (!b->on)
			precap_burst_start(t);
		b->frames++;
		b->t1 = t;
	}
	if ((s == PRECAP_ST_STLEND2 || s < PRECAP_ST_STLRDY1) && b->on)
		precap_burst_end(t);
	precap_ctl_state(s, t);
}

/* --- task: pollers --- */

/* Read twice: a set that changed between the reads is taken on a later poll. */
static void precap_keys_poll(void)
{
	struct precap_keys k, k2;

	precap_keys_read(&k);
	precap_keys_read(&k2);
	if (memcmp(&k, &k2, sizeof(k))) {
		precap.torn++;
		return;
	}
	precap.k = k;
}

/* The state entries of TAKE's history ring since the last poll. */
static void precap_hist(uint32_t t)
{
	struct precap_state *st = &precap;
	uint32_t w = fenykl_rd16(FW_TAKE_HIST_WR) & 0xFFu, o = fenykl_rd16(FW_TAKE_HIST_OLD) & 0xFFu;
	uint32_t n = (w - st->hrd) & 0xFFu, valid = (w - o) & 0xFFu;

	if (n > valid) {
		st->hist_over++;
		st->hrd = o;
		n = valid;
	}
	for (; n; n--, st->hrd = (st->hrd + 1u) & 0xFFu) {
		if (fenykl_rd8(FW_TAKE_HIST + 2u * st->hrd) == PRECAP_HK_STATE)
			precap_state_entered(fenykl_rd8(FW_TAKE_HIST + 2u * st->hrd + 1u), t);
	}
}

static void precap_summary_poll(uint32_t t)
{
	if (precap.b.sum && (precap.tends != precap.b.tends0 || t - precap.b.end_t >= PRECAP_SUM_MS))
		precap_summary();
}

/* A wrapper's fail reaches the task here. */
static void precap_fail_poll(void)
{
	if (precap.fail && !precap.failed)
		precap_failsafe(precap.fail, "wrapper", precap.h);
}

/* The slot words while installed: anything but our stub is a fail-safe. */
static void precap_slot_watch(void)
{
	if (!precap.installed)
		return;
	for (unsigned int i = 0; i < PRECAP_SLOTS; i++) {
		struct fenykl_slot *s = precap_slots[i];
		uint32_t w = fenykl_rd32(s->address);

		if (!s->current || w == s->current)
			continue;
		precap_failsafe(PRECAP_F_SLOT, "slot watch", s->address);
		if (w == s->expected)
			s->current = 0;
	}
}

/* --- task: install / restore --- */

/* Nothing of ours or the firmware's in flight: the slots may change. */
static bool precap_quiet(void)
{
	struct precap_state *st = &precap;

	return !st->h && !st->ring && !st->depth &&
	       (st->c.st == PRECAP_C_IDLE || st->c.st == PRECAP_C_PASS ||
		st->c.st == PRECAP_C_WAIT) &&
	       !fenykl_rd16(FW_TAKE_STATE) && !fenykl_rd16(FW_SEQ_INFLIGHT) &&
	       !fenykl_rd32(FW_SHOOTING);
}

static void precap_restore(void)
{
	struct precap_state *st = &precap;
	int r[PRECAP_SLOTS];

	st->armed = false;
	for (unsigned int i = 0; i < PRECAP_SLOTS; i++)
		r[i] = fenykl_slot_restore(precap_slots[i]);
	st->restores++;
	st->installed = false;
	if (r[0] || r[1] || r[2]) {
		fenykl_log_err(&fenykl_precap_log, "slot restore %d %d %d", r[0], r[1], r[2]);
		precap_failsafe(PRECAP_F_SLOT, "slot restore", 0);
		return;
	}
	fenykl_log_info(&fenykl_precap_log, "slots restored");
}

static void precap_install(void)
{
	struct precap_state *st = &precap;
	uint32_t obj = fenykl_rd32(FW_SEQ_BASE);
	uint32_t vt = fenykl_dram_ptr(obj, 4) ? fenykl_rd32(obj) : 0;
	bool stock = true;
	int r[PRECAP_SLOTS];

	for (unsigned int i = 0; i < PRECAP_SLOTS; i++)
		stock &= fenykl_rd32(precap_slots[i]->address) == precap_slots[i]->expected;
	/* section 4 holds the vtable and the wrapped code */
	if (!(fenykl_rd8(FW_SECTION_LOADED) & 8u) || vt != FW_SEQ_BASE_VTABLE) {
		if (!st->install_logged)
			fenykl_log_warn(&fenykl_precap_log, "install deferred: sections %02x vt %08x",
					fenykl_rd8(FW_SECTION_LOADED), vt);
		st->install_logged = true;
		return;
	}
	if (!stock) {
		precap_failsafe(PRECAP_F_INSTALL, "slot words not stock",
				fenykl_rd32(precap_slots[0]->address));
		return;
	}
	st->fail = PRECAP_F_NONE;
	st->armed = true;
	/* the fail and end wrappers first: with nothing held or dropped they only pass through */
	r[1] = FENYKL_SLOT_REPLACE(seq_take_fail, precap_take_fail);
	r[2] = r[1] ? 0 : FENYKL_SLOT_REPLACE(seq_take_end, precap_take_end);
	r[0] = r[1] || r[2] ? 0 : FENYKL_SLOT_REPLACE(seq_frame_done, precap_frame_done);
	st->installed = true;
	st->installs++;
	if (r[0] || r[1] || r[2]) {
		fenykl_log_err(&fenykl_precap_log, "slot install %d %d %d", r[0], r[1], r[2]);
		precap_failsafe(PRECAP_F_INSTALL, "slot install", 0);
		return;
	}
	fenykl_log_info(&fenykl_precap_log, "slots installed");
}

/* Slots in while the option is on and silent sequential H is selected, out otherwise or after a
   fail-safe, both only while nothing is in flight. Cycling the option clears a fail-safe once the
   slots are out. */
static void precap_slots_poll(void)
{
	struct precap_state *st = &precap;
	bool selected = !precap_gate_n(PRECAP_SELECTED);

	if (st->offs != st->offs_seen && !st->installed && st->c.st == PRECAP_C_IDLE) {
		st->offs_seen = st->offs;
		if (st->failed)
			fenykl_log_info(&fenykl_precap_log, "fail-safe cleared");
		st->failed = false;
		st->fail = PRECAP_F_NONE;
	}
	if (!precap_quiet())
		return;
	if (st->installed && (!st->want || !selected || st->failed)) {
		precap_restore();
		return;
	}
	if (!st->installed && st->want && selected && !st->failed)
		precap_install();
}

static void precap_shown_poll(void)
{
	struct precap_state *st = &precap;
	enum fenykl_precap_state s = FENYKL_PRECAP_OFF;

	if (st->failed)
		s = FENYKL_PRECAP_FAILED;
	else if (st->ring && st->b.on)
		s = st->mode == PRECAP_M_COMMIT || st->commit ? FENYKL_PRECAP_SAVE :
								FENYKL_PRECAP_RING;
	else if (precap_ready() && !st->gate)
		s = FENYKL_PRECAP_ARMED;
	st->shown = s;
}

/* --- task --- */

static uint32_t precap_clock(void)
{
	struct precap_state *st = &precap;
	uint32_t now = FENYKL_TIMER_NOW();

	st->us += FENYKL_TIMER_ELAPSED(st->t_last, now);
	st->t_last = now;
	st->ms += st->us / 1000u;
	st->us %= 1000u;
	return st->ms;
}

static void precap_poll(void)
{
	struct precap_state *st = &precap;
	uint32_t t = precap_clock();

	st->nr = precap_prop(PRECAP_PROP_NR);
	st->gate = precap_gate();
	if (st->gate != st->gate_logged && st->want) {
		fenykl_log_dbg(&fenykl_precap_log, "gate %x (was %x)", st->gate, st->gate_logged);
		st->gate_logged = st->gate;
	}
	precap_keys_poll();
	precap_commits(t);
	precap_hist(t);
	precap_stall_check(t);
	precap_back_check(t);
	precap_summary_poll(t);
	precap_slot_watch();
	precap_fail_poll();
	precap_ctl(t);
	precap_slots_poll();
	precap_shown_poll();
}

FENYKL_TASK(precap_main);
void precap_main(void)
{
	precap.t_last = FENYKL_TIMER_NOW();
	precap.hrd = fenykl_rd16(FW_TAKE_HIST_WR) & 0xFFu;
	precap.c.need_up = true;
	for (;;) {
		precap_poll();
		fenykl_task_sleep(PRECAP_POLL_MS);
	}
}

int fenykl_precap_start(void)
{
	struct precap_state *st = &precap;
	void *stack;

	if (st->task > 0)
		return 0;
	stack = fw_malloc(PRECAP_STACK);
	if (!stack) {
		fenykl_log_err(&fenykl_precap_log, "no task stack");
		return -1;
	}
	st->task = fw_OlyTaskCreate("PRECAP", precap_main_task, 2, 0, PRECAP_PRIO, PRECAP_STACK,
				    stack);
	if (st->task < 0) {
		fw_free(stack);
		fenykl_log_err(&fenykl_precap_log, "task: %d", st->task);
		return st->task;
	}
	fenykl_log_info(&fenykl_precap_log, "task %d pri %u, ring %u frames", st->task, PRECAP_PRIO,
			PRECAP_N);
	return 0;
}

void fenykl_precap_enable(bool on)
{
	if (!on && precap.want)
		precap.offs++;
	precap.want = on;
}

enum fenykl_precap_state fenykl_precap_state(void)
{
	return precap.shown;
}

void fenykl_precap_status(void)
{
	const struct precap_state *st = &precap;

	fenykl_log_info(&fenykl_precap_log, "on %u installed %u failed %u gate %x", st->want,
			st->installed, st->failed, st->gate);
	fenykl_log_info(&fenykl_precap_log, "  bursts %u commits %u cancels %u races %u",
			st->bursts, st->commits, st->cancels, st->races);
	fenykl_log_info(&fenykl_precap_log, "  saved %u dropped %u held %u paints %u", st->saved,
			st->dropped, st->h, st->paints);
	fenykl_log_info(&fenykl_precap_log, "  half presses %u injections %u refused %u torn %u",
			st->cycles, st->injections, st->refused, st->torn);
	fenykl_log_info(&fenykl_precap_log, "  holds %u drops %u defers %u passes %u", st->holds,
			st->drops, st->defers, st->passes);
	fenykl_log_info(&fenykl_precap_log, "  take fails %u other core %u installs %u restores %u",
			st->take_fails, *FENYKL_SHARED_PTR(precap_other_core), st->installs,
			st->restores);
	if (st->failsafes)
		fenykl_log_warn(&fenykl_precap_log, "  fail-safes %u, last: %s, history overflows %u",
				st->failsafes, precap_fail_names[st->last_fail], st->hist_over);
}
