/*
 * caflog: per-frame trace of continuous AF to compare stock C-AF (FtAF monitor + re-scan) with the
 * scan-window controller fcs_caf_scan_run. No hooks, RAM reads only; with caflog.tracker=on also
 * bit 31 of fcs_func_flags (RAM, never the property). The firmware reloads that word at every FCS
 * packet post (0x6E7ED49A), just before 0x1202 tests it in the FCS_MOVIE task, so the poll can lose
 * the race: `ctl` lines show which controller ran.
 *
 * Every CAFLOG_POLL_TICKS the plugin task logs, while a C-AF packet is current, one line per AF
 * frame and every group of watched fields that changed.
 */
#include "cfg.h"
#include "log.h"

/* firmware data, v1.6 */
#define CAFLOG_FUNC_FLAGS 0x605AED40u	/* u32 fcs_func_flags, reloaded from prop 0x2B04 per packet */
#define CAFLOG_TRACKER_BIT 0x80000000u	/* 0x1202 runs fcs_caf_scan_run instead of FtAF */
#define CAFLOG_CTX_PTR 0x605AED1Cu	/* fcs_mvbuf_ptr: ctx, 0 before the first AF */
#define CAFLOG_W_PTR 0x605AED24u	/* fcs_iaf_work_ptr: W */
#define CAFLOG_PULSE 0x605522D2u	/* s16 lens pulse[0] */
#define CAFLOG_LAF 0x605522C0u		/* s16 laf_dis_m[8], pulses far -> near */
#define CAFLOG_INFO 0x605A9650u		/* fcs_caf_info, cleared at each 0x1200 */

/* ctx (fcs_mvbuf_ptr record) */
#define CAFLOG_CTX_PKT 0x04u		/* FCS_PACKET *: +0 u16 com, +0x10 u16 MagLV, +0x1C u16 */
#define CAFLOG_CTX_LENS 0x34u		/* lens_info *: +8 s8 zoom index, +0x274 + 0x10 * index u16 mm */
#define CAFLOG_CTX_HPF 0x60u		/* raw area values, u32 [channel][39], channel stride 0x9C */
#define CAFLOG_CTX_HPF_CH 0x9Cu
#define CAFLOG_CELLS 39u

/* W (IAF work), FtAF fields verified in fcs_ft_chg_detect 0x6E7DDEF5 and 0x6E7DF447 */
#define CAFLOG_W_PHASE 0x04u		/* u16 */
#define CAFLOG_W_SUB 0x06u		/* u16 */
#define CAFLOG_W_DIR 0x0Au		/* u16 search direction */
#define CAFLOG_W_SPEED 0x16u		/* u16 */
#define CAFLOG_W_TARGET 0x18u		/* s32 */
#define CAFLOG_W_CNT 0x28u		/* u16 frame index of the 8-slot rings, monitor/stabilise */
#define CAFLOG_W_PEAK 0x50u		/* s32 */
#define CAFLOG_W_FAR 0x5Cu		/* s32 search range */
#define CAFLOG_W_NEAR 0x60u		/* s32 */
#define CAFLOG_W_UNIT 0xBCu		/* u32 step unit */
#define CAFLOG_W_BASE 0x10Cu		/* u32 HPF1, HPF2, -, ch3, area value: in-focus reference */
#define CAFLOG_W_CHG_CNT 0x124u		/* u16 [ch * 10]: rise/fall TH1, TH2, TH3 */
#define CAFLOG_W_CHG_CH 0x14u		/* byte stride of a channel's counters */
#define CAFLOG_W_TH4_CNT 0x130u		/* u16 */
#define CAFLOG_W_CHG_TAB 0x174u		/* u16 CNT TH1..TH4 (ch0); TH % at +0x28 */
#define CAFLOG_W_CHG_RULE 0x1C4u	/* u16 [ch]: rule that fired, 0..3 = TH1..TH4 */
#define CAFLOG_W_CHG_FIRED 0x1CEu	/* u16 channel that fired */
#define CAFLOG_W_STORED 0x210u		/* s32 stored peak */
#define CAFLOG_W_CELL 0x430u		/* u16 ctx cell of AF area 0 */
#define CAFLOG_W_CHG_SUB 0x432u		/* u16 change-detect state (area 0 sub-state outside FtAF) */
#define CAFLOG_W_RING 0x454u		/* u32 [ch][8], ch stride 0x20, slot = W+0x28 % 8 */
#define CAFLOG_W_RING_CH 0x20u

/* fcs_caf_info offsets */
#define CAFLOG_I_COM 0x04u		/* u16 */
#define CAFLOG_I_MODE 0x06u		/* u16 0x10 first, 0x20 tracking, 0x30/0x31 missed, 0x40 lost */
#define CAFLOG_I_DIR 0x0Cu		/* u16 */
#define CAFLOG_I_APPROACH 0x0Eu		/* u16 */
#define CAFLOG_I_RECEDE 0x10u		/* u16 */
#define CAFLOG_I_FLAGS 0x14u		/* u16 */
#define CAFLOG_I_RESULT 0x16u		/* u16 */
#define CAFLOG_I_SAF 0x18u		/* s32 */
#define CAFLOG_I_REF 0x1Cu		/* s32 window reference */
#define CAFLOG_I_LAST 0x24u		/* s32 last in-focus pulse */
#define CAFLOG_I_MISS 0x82u		/* u16 x2 miss counters */
#define CAFLOG_I_BASE 0x94u		/* u32 x5 base contrasts H1, H2, H3, Y, V */

#define CAFLOG_POLL_TICKS 4		/* ~4 ms with 1 ms ticks: >= 2 polls per 120 fps frame */
#define CAFLOG_RUN_US (10u * 60u * 1000000u)
#define CAFLOG_HEARTBEAT_US (10u * 1000000u)
#define CAFLOG_CAP_POLLS 50u		/* log size checked every ~200 ms */
#define CAFLOG_BOOT_CAP (1792u << 10)	/* bytes this boot */
#define CAFLOG_FILE_CAP (3584u << 10)	/* whole file, below the library's 4 MiB delete-at-init cap */
#define CAFLOG_FLUSH_MS 500u
#define CAFLOG_FLUSH_TRIES 50u
#define CAFLOG_ARGS 4
#define CAFLOG_GROUPS 11

enum caflog_ctl {
	CAFLOG_CTL_NONE,
	CAFLOG_CTL_IAF,
	CAFLOG_CTL_FTAF,
	CAFLOG_CTL_SCAN,
	CAFLOG_CTL_COUNT,
};

static const char *const caflog_ctl_names[] = { "none", "iaf", "ftaf", "scan" };

struct caflog_group {
	const char *fmt;
	void (*read)(uint32_t ctx, uint32_t w, uint32_t *v);
	bool caf_only;
};

struct caflog_state {
	uint32_t flags_orig;		/* fcs_func_flags at start */
	uint32_t flags_seen;		/* last value logged */
	bool tracker_set;		/* this plugin set the bit and has not put it back */
	uint32_t reapplies;
	uint32_t flag_changes;
	uint32_t polls;
	uint32_t caf_polls;		/* polls with a C-AF packet current */
	uint32_t frames;		/* frame lines */
	uint32_t missed;		/* monitor frames skipped between two polls (W+0x28 jumps) */
	uint32_t ctl_starts[CAFLOG_CTL_COUNT];
	uint32_t ctl;
	uint32_t frame_key[3];		/* phase << 16 | W+0x28, then the ring or raw values */
	bool frame_seen;
	uint32_t prev[CAFLOG_GROUPS][CAFLOG_ARGS];
	bool seen[CAFLOG_GROUPS];
};

static struct caflog_state caflog;

static FENYKL_LOG_MODULE(caflog_mod, "caflog", FENYKL_LOG_INFO);

static void caflog_tracker_changed(const struct fenykl_cfg_opt *o, int32_t old)
{
	fenykl_log_info(&caflog_mod, "cfg %s: %d -> %d", o->name, old, *o->value);
}

FENYKL_CFG_GROUP(caflog_grp, "C-AF log", 1);
FENYKL_CFG_BOOL(caflog_tracker, "caflog.tracker", "C-AF tracker", &caflog_grp, 0,
		.on_change = caflog_tracker_changed);

static uint32_t caflog_rd16(uint32_t a)
{
	return *(volatile uint16_t *)a;
}

static uint32_t caflog_rds16(uint32_t a)
{
	return (uint32_t)(int32_t)*(volatile int16_t *)a;
}

static uint32_t caflog_rd32(uint32_t a)
{
	return *(volatile uint32_t *)a;
}

/* A pointer read from firmware RAM that may be stale or unset: DRAM only. */
static bool caflog_ptr_ok(uint32_t p)
{
	return p >= 0x60000000u && p < 0x70000000u && !(p & 3);
}

static uint32_t caflog_packet(uint32_t ctx)
{
	uint32_t pkt;

	if (!ctx)
		return 0;
	pkt = caflog_rd32(ctx + CAFLOG_CTX_PKT);
	return caflog_ptr_ok(pkt) ? pkt : 0;
}

static uint32_t caflog_com(uint32_t ctx)
{
	uint32_t pkt = caflog_packet(ctx);

	return pkt ? caflog_rd16(pkt) : 0;
}

/* C-AF start, restart, burst frame, C-AF+TR. */
static bool caflog_is_caf(uint32_t com)
{
	return com == 0x1200 || com == 0x1202 || com == 0x1600 || com == 0x1800;
}

static uint32_t caflog_classify(uint32_t phase)
{
	if (!phase)
		return CAFLOG_CTL_NONE;
	if (phase < 0x1000)
		return CAFLOG_CTL_IAF;
	if (phase < 0x2000)
		return CAFLOG_CTL_FTAF;
	if ((phase & 0xF000) == 0x4000)
		return CAFLOG_CTL_SCAN;
	return CAFLOG_CTL_IAF;
}

/* FtAF monitor (0x1180, 0x1188, 0x1720) and stabilise (0x1190, 0x1198): W+0x28 counts frames. */
static bool caflog_is_monitor(uint32_t phase)
{
	return phase == 0x1180 || phase == 0x1188 || phase == 0x1720 || phase == 0x1190 ||
	       phase == 0x1198;
}

static void caflog_read_packet(uint32_t ctx, uint32_t w, uint32_t *v)
{
	uint32_t pkt = caflog_packet(ctx);

	(void)w;
	v[0] = pkt ? caflog_rd16(pkt) : 0;
	v[1] = pkt ? caflog_rd16(pkt + 0x1C) : 0;
	v[2] = pkt ? caflog_rd16(pkt + 0x10) : 0;
	v[3] = caflog_rd16(CAFLOG_INFO + CAFLOG_I_COM);
}

static void caflog_read_lens(uint32_t ctx, uint32_t w, uint32_t *v)
{
	uint32_t lens = ctx ? caflog_rd32(ctx + CAFLOG_CTX_LENS) : 0;
	int32_t zi;

	(void)w;
	v[0] = 0;
	if (caflog_ptr_ok(lens)) {
		zi = *(volatile int8_t *)(lens + 8);
		v[0] = caflog_rd16(lens + 0x274 + 0x10 * zi);
	}
	v[1] = caflog_rds16(CAFLOG_LAF);
	v[2] = caflog_rds16(CAFLOG_LAF + 2 * 7);
	v[3] = caflog_rds16(CAFLOG_LAF + 2 * 1) << 16 | (caflog_rds16(CAFLOG_LAF + 2 * 6) & 0xFFFF);
}

/* Six u16 counters as one hex digit each, saturated at 15. */
static uint32_t caflog_nibbles(uint32_t a)
{
	uint32_t v = 0;

	for (int i = 0; i < 6; i++) {
		uint32_t n = caflog_rd16(a + 2 * i);

		v = v << 4 | (n > 15 ? 15 : n);
	}
	return v;
}

static void caflog_read_chg(uint32_t ctx, uint32_t w, uint32_t *v)
{
	uint32_t ch = caflog_rd16(w + CAFLOG_W_CHG_FIRED) & 1;
	uint32_t th4 = caflog_rd16(w + CAFLOG_W_TH4_CNT);

	(void)ctx;
	v[0] = caflog_rd16(w + CAFLOG_W_CHG_SUB);
	v[1] = caflog_nibbles(w + CAFLOG_W_CHG_CNT);
	v[2] = caflog_nibbles(w + CAFLOG_W_CHG_CNT + CAFLOG_W_CHG_CH);
	v[3] = (th4 > 15 ? 15 : th4) << 8 | ch << 4 | (caflog_rd16(w + CAFLOG_W_CHG_RULE + 2 * ch) & 15);
}

static void caflog_read_tab(uint32_t ctx, uint32_t w, uint32_t *v)
{
	(void)ctx;
	v[0] = caflog_rd32(w + CAFLOG_W_CHG_TAB);
	v[1] = caflog_rd32(w + CAFLOG_W_CHG_TAB + 4);
	v[2] = caflog_rd32(w + CAFLOG_W_CHG_TAB + 0x28);
	v[3] = caflog_rd32(w + CAFLOG_W_CHG_TAB + 0x2C);
}

static void caflog_read_base(uint32_t ctx, uint32_t w, uint32_t *v)
{
	(void)ctx;
	v[0] = caflog_rd32(w + CAFLOG_W_BASE);
	v[1] = caflog_rd32(w + CAFLOG_W_BASE + 4);
	v[2] = caflog_rd32(w + CAFLOG_W_BASE + 0xC);
	v[3] = caflog_rd32(w + CAFLOG_W_BASE + 0x10);
}

static void caflog_read_search(uint32_t ctx, uint32_t w, uint32_t *v)
{
	(void)ctx;
	v[0] = caflog_rd16(w + CAFLOG_W_PHASE) << 16 | caflog_rd16(w + CAFLOG_W_SUB);
	v[1] = caflog_rd16(w + CAFLOG_W_DIR);
	v[2] = caflog_rd32(w + CAFLOG_W_TARGET);
	v[3] = caflog_rd16(w + CAFLOG_W_SPEED);
}

static void caflog_read_peak(uint32_t ctx, uint32_t w, uint32_t *v)
{
	(void)ctx;
	v[0] = caflog_rd32(w + CAFLOG_W_PEAK);
	v[1] = caflog_rd32(w + CAFLOG_W_STORED);
	v[2] = caflog_rd32(w + CAFLOG_W_UNIT);
	v[3] = 0;
}

static void caflog_read_range(uint32_t ctx, uint32_t w, uint32_t *v)
{
	(void)ctx;
	v[0] = caflog_rd32(w + CAFLOG_W_FAR);
	v[1] = caflog_rd32(w + CAFLOG_W_NEAR);
	v[2] = 0;
	v[3] = 0;
}

static void caflog_read_window(uint32_t ctx, uint32_t w, uint32_t *v)
{
	uint32_t i = CAFLOG_INFO;

	(void)ctx;
	(void)w;
	v[0] = caflog_rd16(i + CAFLOG_I_MODE);
	v[1] = caflog_rd16(i + CAFLOG_I_DIR);
	v[2] = caflog_rd16(i + CAFLOG_I_APPROACH) << 16 | caflog_rd16(i + CAFLOG_I_RECEDE);
	v[3] = caflog_rd16(i + CAFLOG_I_FLAGS) << 16 | caflog_rd16(i + CAFLOG_I_RESULT);
}

static void caflog_read_wpos(uint32_t ctx, uint32_t w, uint32_t *v)
{
	uint32_t i = CAFLOG_INFO;

	(void)ctx;
	(void)w;
	v[0] = caflog_rd32(i + CAFLOG_I_SAF);
	v[1] = caflog_rd32(i + CAFLOG_I_REF);
	v[2] = caflog_rd32(i + CAFLOG_I_LAST);
	v[3] = caflog_rd16(i + CAFLOG_I_MISS) << 16 | caflog_rd16(i + CAFLOG_I_MISS + 2);
}

static void caflog_read_wbase(uint32_t ctx, uint32_t w, uint32_t *v)
{
	uint32_t i = CAFLOG_INFO + CAFLOG_I_BASE;

	(void)ctx;
	(void)w;
	v[0] = caflog_rd32(i);
	v[1] = caflog_rd32(i + 4);
	v[2] = caflog_rd32(i + 8);
	v[3] = 0;
}

static const struct caflog_group caflog_groups[] = {
	{ "k com %04x seq %u mag %u caf %04x", caflog_read_packet, false },
	{ "z %u mm laf %d %d %08x", caflog_read_lens, false },
	{ "c %04x %06x %06x %03x", caflog_read_chg, true },
	{ "T %08x %08x %08x %08x", caflog_read_tab, true },
	{ "b %u %u %u %u", caflog_read_base, true },
	{ "s %08x dir %u tgt %d spd %u", caflog_read_search, true },
	{ "p peak %d st %d unit %u", caflog_read_peak, true },
	{ "R far %d near %d", caflog_read_range, true },
	{ "w %04x dir %u ar %08x fr %08x", caflog_read_window, true },
	{ "v saf %d ref %d last %d miss %08x", caflog_read_wpos, true },
	{ "V %u %u %u", caflog_read_wbase, true },
};

_Static_assert(sizeof(caflog_groups) / sizeof(caflog_groups[0]) == CAFLOG_GROUPS,
	       "one prev[] row per group");

static void caflog_poll_group(unsigned int i, uint32_t ctx, uint32_t w, bool caf)
{
	const struct caflog_group *g = &caflog_groups[i];
	uint32_t v[CAFLOG_ARGS];

	if (g->caf_only && !caf)
		return;
	g->read(ctx, w, v);
	if (caflog.seen[i] && !memcmp(v, caflog.prev[i], sizeof(v)))
		return;
	fenykl_log_info(&caflog_mod, g->fmt, v[0], v[1], v[2], v[3]);
	memcpy(caflog.prev[i], v, sizeof(v));
	caflog.seen[i] = true;
}

static uint32_t caflog_ring(uint32_t w, uint32_t ch, uint32_t cnt)
{
	return caflog_rd32(w + CAFLOG_W_RING + ch * CAFLOG_W_RING_CH + ((cnt - 1) & 7) * 4);
}

static void caflog_ctl_change(uint32_t ctl)
{
	if (ctl == caflog.ctl)
		return;
	caflog.ctl = ctl;
	caflog.ctl_starts[ctl]++;
	fenykl_log_info(&caflog_mod, "ctl %s, flags %08x", caflog_ctl_names[ctl],
			caflog_rd32(CAFLOG_FUNC_FLAGS));
}

/*
 * One line per AF frame. Monitor and stabilise: a new W+0x28 gives `m` with the last complete
 * slot of the controller's rings. Elsewhere W+0x28 may stand still: a new raw area value gives `f`.
 */
static void caflog_poll_frame(uint32_t ctx, uint32_t w)
{
	uint32_t phase = caflog_rd16(w + CAFLOG_W_PHASE);
	uint32_t cnt = caflog_rd16(w + CAFLOG_W_CNT);
	uint32_t cell = caflog_rd16(w + CAFLOG_W_CELL);
	uint32_t key[3] = { phase << 16 | cnt, 0, 0 };
	bool monitor = caflog_is_monitor(phase);
	uint32_t prev_cnt = caflog.frame_key[0] & 0xFFFF;
	bool same_phase;

	caflog_ctl_change(caflog_classify(phase));
	if (!monitor && cell < CAFLOG_CELLS) {
		key[1] = caflog_rd32(ctx + CAFLOG_CTX_HPF + cell * 4);
		key[2] = caflog_rd32(ctx + CAFLOG_CTX_HPF + CAFLOG_CTX_HPF_CH + cell * 4);
	}
	if (caflog.frame_seen && !memcmp(key, caflog.frame_key, sizeof(key)))
		return;
	same_phase = caflog.frame_seen && caflog.frame_key[0] >> 16 == phase;
	if (monitor && same_phase && cnt > prev_cnt + 1)
		caflog.missed += cnt - prev_cnt - 1;
	memcpy(caflog.frame_key, key, sizeof(key));
	caflog.frame_seen = true;
	caflog.frames++;
	if (monitor) {
		fenykl_log_info(&caflog_mod, "m %08x %u %u %u",
				caflog_rd16(w + CAFLOG_W_CHG_SUB) << 16 | cnt, caflog_ring(w, 0, cnt),
				caflog_ring(w, 1, cnt), caflog_ring(w, 3, cnt));
		return;
	}
	fenykl_log_info(&caflog_mod, "f %08x %d %u %u", key[0], caflog_rds16(CAFLOG_PULSE), key[1],
			key[2]);
}

static void caflog_put_flags(uint32_t flags)
{
	*(volatile uint32_t *)CAFLOG_FUNC_FLAGS = flags;
}

/* Puts back the original bit 31; the firmware reloads the word at its next packet anyway. */
static void caflog_restore_flags(void)
{
	uint32_t flags = caflog_rd32(CAFLOG_FUNC_FLAGS) & ~CAFLOG_TRACKER_BIT;

	flags |= caflog.flags_orig & CAFLOG_TRACKER_BIT;
	caflog_put_flags(flags);
	caflog.flags_seen = flags;
	caflog.tracker_set = false;
	fenykl_log_info(&caflog_mod, "x restored %08x", flags);
}

/* Logs every change of fcs_func_flags; with the tracker on, sets bit 31 again after a reload. */
static void caflog_poll_flags(uint32_t com)
{
	uint32_t flags = caflog_rd32(CAFLOG_FUNC_FLAGS);

	if (flags != caflog.flags_seen) {
		caflog.flag_changes++;
		fenykl_log_info(&caflog_mod, "x %08x -> %08x com %04x", caflog.flags_seen, flags, com);
		caflog.flags_seen = flags;
	}
	if (caflog_tracker && !(flags & CAFLOG_TRACKER_BIT)) {
		caflog_put_flags(flags | CAFLOG_TRACKER_BIT);
		caflog.flags_seen = flags | CAFLOG_TRACKER_BIT;
		caflog.tracker_set = true;
		caflog.reapplies++;
		fenykl_log_info(&caflog_mod, "x set %08x, #%u com %04x", caflog.flags_seen,
				caflog.reapplies, com);
		return;
	}
	if (!caflog_tracker && caflog.tracker_set)
		caflog_restore_flags();
}

static void caflog_heartbeat(const char *when)
{
	struct fenykl_log_stats st;

	fenykl_log_get_stats(&st);
	fenykl_log_info(&caflog_mod, "%s: polls %u caf %u frames %u", when, caflog.polls,
			caflog.caf_polls, caflog.frames);
	fenykl_log_info(&caflog_mod, "%s: missed %u, flags %08x", when, caflog.missed,
			caflog_rd32(CAFLOG_FUNC_FLAGS));
	fenykl_log_info(&caflog_mod, "%s: flag sets %u changes %u", when, caflog.reapplies,
			caflog.flag_changes);
	fenykl_log_info(&caflog_mod, "%s: ctl iaf %u ftaf %u scan %u", when,
			caflog.ctl_starts[CAFLOG_CTL_IAF], caflog.ctl_starts[CAFLOG_CTL_FTAF],
			caflog.ctl_starts[CAFLOG_CTL_SCAN]);
	fenykl_log_info(&caflog_mod, "%s: log bytes %u file %u lost %u", when, st.bytes,
			st.file_size, st.overwritten0);
}

static bool caflog_log_full(void)
{
	struct fenykl_log_stats st;

	fenykl_log_get_stats(&st);
	return st.bytes >= CAFLOG_BOOT_CAP || st.file_size + st.bytes >= CAFLOG_FILE_CAP;
}

static void caflog_poll(void)
{
	uint32_t ctx = caflog_rd32(CAFLOG_CTX_PTR);
	uint32_t w = caflog_rd32(CAFLOG_W_PTR);
	uint32_t com = caflog_com(ctx);
	bool caf = caflog_ptr_ok(ctx) && caflog_ptr_ok(w) && caflog_is_caf(com);

	caflog.polls++;
	caflog_poll_flags(com);
	for (unsigned int i = 0; i < CAFLOG_GROUPS; i++)
		caflog_poll_group(i, ctx, w, caf);
	if (!caf)
		return;
	caflog.caf_polls++;
	caflog_poll_frame(ctx, w);
}

static void caflog_final_flush(void)
{
	for (unsigned int i = 0; i < CAFLOG_FLUSH_TRIES; i++) {
		if (fenykl_log_flush() != FENYKL_LOG_EBUSY)
			return;
		fw_OlyTaskSleep(CAFLOG_POLL_TICKS);
	}
}

static void caflog_start_cfg(void)
{
	struct fenykl_cfg_stats st;
	int r = fenykl_cfg_init();

	fenykl_cfg_get_stats(&st);
	fenykl_log_info(&caflog_mod, "cfg init %d: slot %d seq %u, import %u", r, st.slot, st.seq,
			st.imported);
	fenykl_log_info(&caflog_mod, "cfg saver task %d, tracker %s", fenykl_cfg_task_start(),
			caflog_tracker ? "on" : "off");
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_log_config cfg = { .flush_ms = CAFLOG_FLUSH_MS };
	int init = fenykl_log_init(&cfg);
	int task = fenykl_log_task_start();
	uint32_t elapsed = 0, beat = 0, last;
	const char *reason = "time";

	(void)load_base;
	(void)image_size;
	caflog.flags_orig = caflog_rd32(CAFLOG_FUNC_FLAGS);
	caflog.flags_seen = caflog.flags_orig;
	fenykl_log_info(&caflog_mod, "caflog: log init %d task %d, flags %08x", init, task,
			caflog.flags_orig);
	caflog_start_cfg();

	last = FENYKL_LOG_TIMER_NOW();
	while (elapsed < CAFLOG_RUN_US) {
		uint32_t now;

		caflog_poll();
		if (caflog.polls % CAFLOG_CAP_POLLS == 0 && caflog_log_full()) {
			reason = "log cap";
			break;
		}
		fw_OlyTaskSleep(CAFLOG_POLL_TICKS);
		now = FENYKL_LOG_TIMER_NOW();
		elapsed += FENYKL_LOG_ELAPSED(last, now);
		beat += FENYKL_LOG_ELAPSED(last, now);
		last = now;
		if (beat >= CAFLOG_HEARTBEAT_US) {
			beat = 0;
			caflog_heartbeat("hb");
		}
	}

	if (caflog.tracker_set)
		caflog_restore_flags();
	caflog_heartbeat("end");
	fenykl_log_info(&caflog_mod, "end: %s after %u s, flags %08x", reason, elapsed / 1000000u,
			caflog_rd32(CAFLOG_FUNC_FLAGS));
	caflog_final_flush();
}
