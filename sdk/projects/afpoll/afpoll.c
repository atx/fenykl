/* E-M10 II autofocus poller: every AF_POLL_TICKS reads TakeFcs's take_focus record, fcs's contrast
   evaluation state and grid, the FCS command log, the contrast driver's client list, the lens record,
   the half-press byte and the AF target properties; logs them on change, each new FCS command log
   entry, and once a second the frame counters and the centre area of both statistics buffers. Reads
   only: no firmware call but the log library's, no firmware RAM write. Stops after AF_RUN_S. */
#include <fenykl.h>
#include <log.h>

#define AF_POLL_TICKS 10
#define AF_RUN_S 200
#define AF_LINES 4000
#define AF_LINES_PER_S 40
#define AF_LENS_GAP_US 100000u		/* at most one lens line per 100 ms */

#define AF_TAKE_FOCUS_PTR 0xA1C1C3ECu	/* u32 -> take_focus (0xA1C1C3F0) */
#define AF_EVAL_STATE 0x61879A48u	/* u8: 0 idle, 2 running, 3 stop requested, 1 last frame, 4 update */
#define AF_CNT_START 0x61879928u	/* u32, +1 per frame-start event */
#define AF_CNT_DATA 0x6187992Cu		/* u32, +1 per data-complete event */
#define AF_PATTERN 0x61879925u		/* u8 area table index; cols, rows follow */
#define AF_COLS 0x61879926u
#define AF_ROWS 0x61879927u
#define AF_ROW_STRIDE 0x61879A4Cu	/* u32, words */
#define AF_AREA_SIZE 0x61879A50u	/* u32, words per channel */
#define AF_BUF_CNT 0x6187D7D8u		/* u32 -> cnt buffer, 4 channels */
#define AF_BUF_FC 0x6187D7DCu		/* u32 -> fc buffer, 3 channels */
#define AF_BUF_WORDS 0x1000u		/* each buffer is a 0x4000-byte pool slot */
#define AF_LOG_CODE 0x61879EA4u		/* u16 [32] */
#define AF_LOG_TIME 0x61879EE4u		/* u16 [32], ms */
#define AF_LOG_ARG 0x61879F24u		/* u16 [32] */
#define AF_LOG_IDX 0x61879F64u		/* u16, next slot */
#define AF_LOG_N 32u
#define AF_CNT_UNIT 0x618485BCu		/* u32 -> contrast unit: +4 head of the client list */
#define AF_LENS_PTR 0xA1C0004Cu		/* u32 -> lens record; state block at +4 */
#define AF_HALF 0xA1C1801Cu		/* u8, 1 while the shutter button is half pressed */
#define AF_PROP_TARGET 0x88B2u
#define AF_PROP_AREA 0x84D1u
#define AF_PROP_OVR_VALID 0xA1BE81F0u
#define AF_PROP_OVR 0xA1BF01F4u
#define AF_PROP 0xA1BE81F4u
#define AF_U8(a) (*(volatile uint8_t *)(a))
#define AF_U16(a) (*(volatile uint16_t *)(a))
#define AF_U32(a) (*(volatile uint32_t *)(a))

/* take_focus words logged on change: +0x00..+0x13, +0x18, +0x1C, +0x24, +0x34, +0x38, +0x40..+0x4F */
static const uint8_t af_tf_off[] = { 0x00, 0x04, 0x08, 0x0C, 0x10, 0x18, 0x1C, 0x24,
				     0x34, 0x38, 0x40, 0x44, 0x48, 0x4C };
#define AF_TF_N sizeof(af_tf_off)

struct af_misc {
	uint32_t state, pattern, cols, rows, stride, area;
	uint32_t half, target, area_mode;
};

struct af_list {
	uint32_t unit, head;
	uint32_t node[2][4];	/* setting, client, next, u16 flags; the setting's number is per frame */
};

struct af_lens {
	uint32_t state, obj_dst, av, pulse0;
};

static struct {
	uint32_t tf[AF_TF_N];
	struct af_misc misc;
	struct af_list list;
	struct af_lens lens;
	uint32_t lens_time;
	uint32_t log_idx;
	uint32_t cnt_start, cnt_data;
	unsigned int lines, lines_sec, skipped;
} af;

static FENYKL_LOG_MODULE(af_mod, "afpoll", FENYKL_LOG_INFO);

static bool af_budget(unsigned int n)
{
	if (af.lines + n > AF_LINES || af.lines_sec + n > AF_LINES_PER_S) {
		af.skipped++;
		return false;
	}
	af.lines += n;
	af.lines_sec += n;
	return true;
}

/* n bytes at a lie in DRAM (either alias), naturally aligned (word alignment from 4 up). */
static bool af_dram(uint32_t a, uint32_t n)
{
	uint32_t c = a & 0x3FFFFFFFu;

	if ((a >> 30) != 1 && (a >> 30) != 2)
		return false;
	if (a & (n >= 4 ? 3 : n - 1))
		return false;
	return c >= 0x20000000u && c + n <= 0x40000000u;
}

/* The firmware's property read: lookup 0x60076078, then a load of the width in the id's low bits. */
static uint32_t af_prop(uint32_t id)
{
	uint32_t off = (id >> 4) * 8 + 4, obj = 0;

	if (AF_U16(AF_PROP_OVR_VALID))
		obj = AF_U32(AF_PROP_OVR + off);
	if (!obj)
		obj = AF_U32(AF_PROP + off);
	if ((id & 7) == 4)
		return af_dram(obj, 4) ? AF_U32(obj) : ~0u;
	if ((id & 7) == 2)
		return af_dram(obj, 2) ? AF_U16(obj) : ~0u;
	return af_dram(obj, 1) ? AF_U8(obj) : ~0u;
}

static void af_read_tf(uint32_t tf, uint32_t *w)
{
	for (unsigned int i = 0; i < AF_TF_N; i++)
		w[i] = tf ? AF_U32(tf + af_tf_off[i]) : 0;
}

/* TAKE_FOCUS (priority 25) preempts the probe: two equal reads rule out a half-written record. */
static void af_poll_tf(void)
{
	uint32_t tf = AF_U32(AF_TAKE_FOCUS_PTR);
	uint32_t a[AF_TF_N], b[AF_TF_N];

	if (!af_dram(tf, 0x50))
		tf = 0;
	af_read_tf(tf, a);
	af_read_tf(tf, b);
	if (memcmp(a, b, sizeof(a)) || !memcmp(a, af.tf, sizeof(a)) || !af_budget(4))
		return;
	memcpy(af.tf, a, sizeof(a));
	fenykl_log_info(&af_mod, "tf +00 %08x %08x %08x %08x", a[0], a[1], a[2], a[3]);
	fenykl_log_info(&af_mod, "tf +10 %08x +18 %08x %08x +24 %08x", a[4], a[5], a[6], a[7]);
	fenykl_log_info(&af_mod, "tf +34 %08x %08x, +3c %08x", a[8], a[9], tf ? AF_U32(tf + 0x3C) : 0);
	fenykl_log_info(&af_mod, "tf +40 %08x %08x %08x %08x", a[10], a[11], a[12], a[13]);
}

static void af_poll_misc(void)
{
	struct af_misc m;

	m.state = AF_U8(AF_EVAL_STATE);
	m.pattern = AF_U8(AF_PATTERN);
	m.cols = AF_U8(AF_COLS);
	m.rows = AF_U8(AF_ROWS);
	m.stride = AF_U32(AF_ROW_STRIDE);
	m.area = AF_U32(AF_AREA_SIZE);
	m.half = AF_U8(AF_HALF);
	m.target = af_prop(AF_PROP_TARGET);
	m.area_mode = af_prop(AF_PROP_AREA);
	if (!memcmp(&m, &af.misc, sizeof(m)) || !af_budget(3))
		return;
	af.misc = m;
	fenykl_log_info(&af_mod, "eval state %u half %u target %x area mode %x", m.state, m.half,
			m.target, m.area_mode);
	fenykl_log_info(&af_mod, "grid pattern %u %ux%u stride %u", m.pattern, m.cols, m.rows,
			m.stride);
	fenykl_log_info(&af_mod, "grid area %u words", m.area);
}

/* The log writes code, time, arg, then advances the index: entries before the index are complete. */
static void af_poll_log(void)
{
	uint32_t idx = AF_U16(AF_LOG_IDX) % AF_LOG_N;

	while (af.log_idx != idx) {
		uint32_t i = af.log_idx;

		if (!af_budget(1))
			return;
		fenykl_log_info(&af_mod, "fcs %04x arg %04x t %u", AF_U16(AF_LOG_CODE + 2 * i),
				AF_U16(AF_LOG_ARG + 2 * i), AF_U16(AF_LOG_TIME + 2 * i));
		af.log_idx = (i + 1) % AF_LOG_N;
	}
}

static void af_read_node(uint32_t n, uint32_t *w)
{
	if (!af_dram(n, 0x14))
		return;
	w[0] = AF_U32(n);
	w[1] = AF_U32(n + 4);
	w[2] = AF_U32(n + 0xC);
	w[3] = AF_U16(n + 0x10);
}

static void af_poll_list(void)
{
	struct af_list l;
	uint32_t u;

	memset(&l, 0, sizeof(l));
	l.unit = AF_U32(AF_CNT_UNIT);
	u = l.unit;
	if (af_dram(u, 8)) {
		l.head = AF_U32(u + 4);
		af_read_node(l.head, l.node[0]);
		af_read_node(l.node[0][2], l.node[1]);
	}
	if (!memcmp(&l, &af.list, sizeof(l)) || !af_budget(2))
		return;
	af.list = l;
	fenykl_log_info(&af_mod, "clients unit %08x head %08x: set %08x cl %08x", l.unit, l.head,
			l.node[0][0], l.node[0][1]);
	fenykl_log_info(&af_mod, "clients flags %x next %08x: set %08x cl %08x", l.node[0][3],
			l.node[0][2], l.node[1][0], l.node[1][1]);
}

static void af_poll_lens(uint32_t now)
{
	uint32_t r = AF_U32(AF_LENS_PTR);
	struct af_lens l;

	if (!af_dram(r, 0x28) || FENYKL_LOG_ELAPSED(af.lens_time, now) < AF_LENS_GAP_US)
		return;
	l.state = AF_U32(r + 4);
	l.obj_dst = AF_U32(r + 0xC);
	l.av = AF_U16(r + 0x10);
	l.pulse0 = AF_U16(r + 0x26);
	if (!memcmp(&l, &af.lens, sizeof(l)) || !af_budget(1))
		return;
	af.lens = l;
	af.lens_time = now;
	fenykl_log_info(&af_mod, "lens state %x obj_dst %u av %x pulse %u", l.state, l.obj_dst, l.av,
			l.pulse0);
}

/* Word w of the centre area of channel ch, or ~0 if the buffer or geometry does not fit. */
static uint32_t af_centre(uint32_t ptr, uint32_t ch, uint32_t nch)
{
	uint32_t b = AF_U32(ptr), area = AF_U32(AF_AREA_SIZE);
	uint32_t w = AF_U8(AF_ROWS) / 2 * AF_U32(AF_ROW_STRIDE) + AF_U8(AF_COLS) / 2;

	/* the hardware writes the buffers by DMA: read them uncached */
	b = (uint32_t)FENYKL_UNCACHED(b);
	if (!af_dram(b, AF_BUF_WORDS * 4) || !area || area > AF_BUF_WORDS / nch || w >= area)
		return ~0u;
	return AF_U32(b + 4 * (ch * area + w));
}

static void af_second(void)
{
	uint32_t s = AF_U32(AF_CNT_START), d = AF_U32(AF_CNT_DATA);

	af.lines_sec = 0;
	if ((s != af.cnt_start || d != af.cnt_data) && af_budget(3)) {
		fenykl_log_info(&af_mod, "sec frames start %u data %u, state %u", s - af.cnt_start,
				d - af.cnt_data, AF_U8(AF_EVAL_STATE));
		fenykl_log_info(&af_mod, "sec fc centre %u %u %u", af_centre(AF_BUF_FC, 0, 3),
				af_centre(AF_BUF_FC, 1, 3), af_centre(AF_BUF_FC, 2, 3));
		fenykl_log_info(&af_mod, "sec cnt centre %u %u %u %u", af_centre(AF_BUF_CNT, 0, 4),
				af_centre(AF_BUF_CNT, 1, 4), af_centre(AF_BUF_CNT, 2, 4),
				af_centre(AF_BUF_CNT, 3, 4));
	}
	af.cnt_start = s;
	af.cnt_data = d;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;
	uint32_t start = FENYKL_LOG_TIMER_NOW(), sec = start, now = start;

	fenykl_log_info(&af_mod, "afpoll: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	fenykl_log_info(&af_mod, "buffers cnt %08x fc %08x, fcs log at %u", AF_U32(AF_BUF_CNT),
			AF_U32(AF_BUF_FC), AF_U16(AF_LOG_IDX));
	/* the first poll logs everything; the command log from the entry the firmware writes next */
	af.tf[0] = ~0u;
	af.misc.state = ~0u;
	af.list.unit = ~0u;
	af.lens.state = ~0u;
	af.lens_time = start - AF_LENS_GAP_US;
	af.log_idx = AF_U16(AF_LOG_IDX) % AF_LOG_N;
	af.cnt_start = AF_U32(AF_CNT_START);
	af.cnt_data = AF_U32(AF_CNT_DATA);
	while (FENYKL_LOG_ELAPSED(start, now) < AF_RUN_S * 1000000u) {
		af_poll_tf();
		af_poll_misc();
		af_poll_log();
		af_poll_list();
		af_poll_lens(now);
		if (FENYKL_LOG_ELAPSED(sec, now) >= 1000000u) {
			sec = now;
			af_second();
		}
		fw_OlyTaskSleep(AF_POLL_TICKS);
		now = FENYKL_LOG_TIMER_NOW();
	}
	fenykl_log_get_stats(&st);
	fenykl_log_info(&af_mod, "afpoll end: %u s, %u lines, %u skipped, %u overwritten",
			FENYKL_LOG_ELAPSED(start, now) / 1000000u, af.lines, af.skipped,
			st.overwritten0);
}
