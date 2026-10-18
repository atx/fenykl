/* E-M10 II face-detection poller: every FP_POLL_TICKS reads the FD loop counters, the published FD
   frame descriptor, MVR's FD ring, the newest live results (FD-frame and display coordinates), the
   eye record, fcs's face selection, the AF packet and AF point, and the face properties; logs them on
   change (results, eyes and the AF point at most every FP_GAP_US) and every 5 s the FD cycle,
   pass and descriptor rates. No frame dump. Reads only: no firmware call but the log library's, no
   firmware RAM write. Stops after FP_RUN_S. */
#include <fenykl.h>
#include <log.h>

#define FP_POLL_TICKS 10
#define FP_RUN_S 200
#define FP_LINES 4000
#define FP_LINES_PER_S 40
#define FP_GAP_US 1000000u
#define FP_RATE_US 5000000u

#define FP_CYCLES 0x615E0156u		/* u16, +1 per completed live FD cycle */
#define FP_PASSES 0x615E0154u		/* u16, +1 per hardware pass */
#define FP_DESC 0xA1C2842Cu		/* 40 B: +0 Y addr, +0x1A u16 frame_count, +0x1C u16 buffer id */
#define FP_DESC_WORDS 10
#define FP_RING_IDX 0xA1C2BE48u		/* u16 */
#define FP_RING 0xA1C2BE4Cu		/* 5 x 12 B {u16 buffer id, .., u32 addr @+4, .., u16 scale @+0xA} */
#define FP_RING_N 5
#define FP_CURSOR 0x61893CC0u		/* u8, 0..2 */
#define FP_SLOT_OF 0x606161DFu		/* u8 [3]: newest completed slot per cursor */
#define FP_MAIN 0x61893CC2u		/* u8 [3]: main face per slot */
#define FP_RES0 0xA1C27E28u		/* live results, FD-frame coordinates, 3 x 0x8C */
#define FP_RES3 0xA1C27FCCu		/* live results, display coordinates, 3 x 0x8C */
#define FP_RES_SIZE 0x8Cu
#define FP_FACES 8			/* 14 B each: s16 cx, cy, size, conf, angle, pose; u8 flag */
#define FP_COUNT 0x70			/* u16; +0x74..+0x88 header */
#define FP_EYE_MODE 0xA1C2856Cu		/* u8 */
#define FP_EYE_ON 0xA1C2856Du		/* u8, 1 = parts detection runs */
#define FP_EYE 0xA1C2856Eu		/* eye record type 0, 0x26 B */
#define FP_EYE_HALVES 19
#define FP_SET_INFO 0x61893CB9u		/* u8 [7], 0x61893CB9..0x61893CBF */
#define FP_SEL_HDR 0x61879DD8u		/* u32 */
#define FP_SEL_FACE 0x61879DDCu		/* u32 */
#define FP_SEL_COUNT 0x61879E92u	/* u16 */
#define FP_SEL_KIND 0x61879E95u		/* u8, 1 face, 2/3 eye */
#define FP_SEL_IDX 0x61879E98u		/* u8, 0xFF none */
#define FP_AF_PKT 0x618797A4u		/* +0 u16 command, +6 face, +7 eye, +0x10 magnification */
#define FP_AF_POINT 0xA1C1C2E8u		/* 16 B */
#define FP_PROP_OVR_VALID 0xA1BE81F0u
#define FP_PROP_OVR 0xA1BF01F4u
#define FP_PROP 0xA1BE81F4u
#define FP_U8(a) (*(volatile uint8_t *)(a))
#define FP_U16(a) (*(volatile uint16_t *)(a))
#define FP_U32(a) (*(volatile uint32_t *)(a))

static const uint16_t fp_props[] = { 0x9121, 0x69F1, 0xC021, 0x6761, 0x7E91, 0x3081 };
#define FP_NPROPS (sizeof(fp_props) / sizeof(fp_props[0]))
/* AF packet words: +0x00..+0x13, +0x24, +0x28 */
static const uint8_t fp_pkt_off[] = { 0x00, 0x04, 0x08, 0x0C, 0x10, 0x24, 0x28 };
#define FP_PKT_N sizeof(fp_pkt_off)

struct fp_res {
	uint32_t main, count;
	uint32_t face[FP_FACES][3];	/* cx | cy << 16, size | conf << 16, angle | pose << 16 */
	uint32_t flags[2];		/* the eight u8 flags */
	uint32_t hdr[5];		/* +0x78..+0x8B; the image address +0x74 changes every cycle */
};

struct fp_eye {
	uint32_t on, mode;
	uint32_t w[(FP_EYE_HALVES + 1) / 2];
};

struct fp_sel {
	uint32_t hdr, face, count, kind, idx;
	uint32_t pkt[FP_PKT_N];
};

struct fp_misc {
	uint32_t desc[FP_DESC_WORDS];	/* frame_count, buffer id and address masked out */
	uint32_t set_info[2];
	uint32_t ring_id[FP_RING_N], ring_addr[FP_RING_N];
	uint32_t props[FP_NPROPS];
};

static struct {
	struct fp_res res[2];
	uint32_t res_time[2];
	struct fp_eye eye;
	uint32_t eye_time;
	struct fp_sel sel;
	uint32_t point[4], point_time;
	struct fp_misc misc;
	/* per FP_RATE_US */
	uint32_t cycles, passes, frame, desc_chg, max_faces;
	unsigned int lines, lines_sec, skipped;
} fp;

static FENYKL_LOG_MODULE(fp_mod, "facepl", FENYKL_LOG_INFO);

static bool fp_budget(unsigned int n)
{
	if (fp.lines + n > FP_LINES || fp.lines_sec + n > FP_LINES_PER_S) {
		fp.skipped++;
		return false;
	}
	fp.lines += n;
	fp.lines_sec += n;
	return true;
}

/* n bytes at a lie in DRAM (either alias), naturally aligned (word alignment from 4 up). */
static bool fp_dram(uint32_t a, uint32_t n)
{
	uint32_t c = a & 0x3FFFFFFFu;

	if ((a >> 30) != 1 && (a >> 30) != 2)
		return false;
	if (a & (n >= 4 ? 3 : n - 1))
		return false;
	return c >= 0x20000000u && c + n <= 0x40000000u;
}

/* The firmware's property read: lookup 0x60076078, then a load of the width in the id's low bits. */
static uint32_t fp_prop(uint32_t id)
{
	uint32_t off = (id >> 4) * 8 + 4, obj = 0;

	if (FP_U16(FP_PROP_OVR_VALID))
		obj = FP_U32(FP_PROP_OVR + off);
	if (!obj)
		obj = FP_U32(FP_PROP + off);
	if ((id & 7) == 4)
		return fp_dram(obj, 4) ? FP_U32(obj) : ~0u;
	if ((id & 7) == 2)
		return fp_dram(obj, 2) ? FP_U16(obj) : ~0u;
	return fp_dram(obj, 1) ? FP_U8(obj) : ~0u;
}

static uint32_t fp_pair(uint32_t a)
{
	return FP_U16(a) | (uint32_t)FP_U16(a + 2) << 16;
}

/* Reads the newest completed slot; returns its index, or 3 if the cursor is out of range. The slot
   index rotates every cycle, so it is not part of the compared record. */
static uint32_t fp_read_res(uint32_t base, struct fp_res *r)
{
	uint32_t cursor = FP_U8(FP_CURSOR), slot, b;

	memset(r, 0, sizeof(*r));
	if (cursor >= 3)
		return 3;
	slot = FP_U8(FP_SLOT_OF + cursor);
	if (slot >= 3)
		return 3;
	r->main = FP_U8(FP_MAIN + slot);
	b = base + slot * FP_RES_SIZE;
	r->count = FP_U16(b + FP_COUNT);
	for (unsigned int i = 0; i < FP_FACES && i < r->count; i++) {
		uint32_t f = b + 14 * i;

		r->face[i][0] = fp_pair(f);
		r->face[i][1] = fp_pair(f + 4);
		r->face[i][2] = fp_pair(f + 8);
		r->flags[i / 4] |= (uint32_t)FP_U8(f + 12) << (8 * (i % 4));
	}
	for (unsigned int i = 0; i < 5; i++)
		r->hdr[i] = FP_U32(b + 0x78 + 4 * i);
	return slot;
}

/* The newest slot stays as it is until the cursor moves; FACE_FD can move it between the two reads,
   so they must agree. */
static void fp_poll_res(unsigned int t, uint32_t now)
{
	uint32_t base = t ? FP_RES3 : FP_RES0, slot;
	struct fp_res a, b;

	if (FENYKL_LOG_ELAPSED(fp.res_time[t], now) < FP_GAP_US)
		return;
	slot = fp_read_res(base, &a);
	if (slot != fp_read_res(base, &b) || slot >= 3 || memcmp(&a, &b, sizeof(a)) ||
	    !memcmp(&a, &fp.res[t], sizeof(a)))
		return;
	if (a.count > fp.max_faces)
		fp.max_faces = a.count;
	if (!fp_budget(4 + (a.count < FP_FACES ? a.count : FP_FACES)))
		return;
	fp.res[t] = a;
	fp.res_time[t] = now;
	fenykl_log_info(&fp_mod, "res%u slot %u count %u main %u", t ? 3 : 0, slot, a.count, a.main);
	fenykl_log_info(&fp_mod, "res%u flags %08x %08x addr %08x", t ? 3 : 0, a.flags[0], a.flags[1],
			FP_U32(base + slot * FP_RES_SIZE + 0x74));
	fenykl_log_info(&fp_mod, "res%u +78 %08x %08x %08x", t ? 3 : 0, a.hdr[0], a.hdr[1], a.hdr[2]);
	fenykl_log_info(&fp_mod, "res%u +84 %08x %08x", t ? 3 : 0, a.hdr[3], a.hdr[4]);
	for (unsigned int i = 0; i < FP_FACES && i < a.count; i++)
		fenykl_log_info(&fp_mod, "f%u xy %08x size/conf %08x angle/pose %08x", i, a.face[i][0],
				a.face[i][1], a.face[i][2]);
}

static void fp_read_eye(struct fp_eye *e)
{
	memset(e, 0, sizeof(*e));
	e->on = FP_U8(FP_EYE_ON);
	e->mode = FP_U8(FP_EYE_MODE);
	for (unsigned int i = 0; i < FP_EYE_HALVES; i++)
		e->w[i / 2] |= (uint32_t)FP_U16(FP_EYE + 2 * i) << (16 * (i % 2));
}

static void fp_poll_eye(uint32_t now)
{
	struct fp_eye a, b;

	if (FENYKL_LOG_ELAPSED(fp.eye_time, now) < FP_GAP_US)
		return;
	fp_read_eye(&a);
	fp_read_eye(&b);
	if (memcmp(&a, &b, sizeof(a)) || !memcmp(&a, &fp.eye, sizeof(a)) || !fp_budget(4))
		return;
	fp.eye = a;
	fp.eye_time = now;
	fenykl_log_info(&fp_mod, "eye on %u mode %u, +00 %08x %08x", a.on, a.mode, a.w[0], a.w[1]);
	fenykl_log_info(&fp_mod, "eye +08 %08x %08x %08x %08x", a.w[2], a.w[3], a.w[4], a.w[5]);
	fenykl_log_info(&fp_mod, "eye +18 %08x %08x %08x %08x", a.w[6], a.w[7], a.w[8], a.w[9]);
}

static void fp_poll_sel(void)
{
	struct fp_sel s;

	s.hdr = FP_U32(FP_SEL_HDR);
	s.face = FP_U32(FP_SEL_FACE);
	s.count = FP_U16(FP_SEL_COUNT);
	s.kind = FP_U8(FP_SEL_KIND);
	s.idx = FP_U8(FP_SEL_IDX);
	for (unsigned int i = 0; i < FP_PKT_N; i++)
		s.pkt[i] = FP_U32(FP_AF_PKT + fp_pkt_off[i]);
	if (!memcmp(&s, &fp.sel, sizeof(s)) || !fp_budget(4))
		return;
	fp.sel = s;
	fenykl_log_info(&fp_mod, "sel kind %u idx %u count %u face %08x", s.kind, s.idx, s.count,
			s.face);
	fenykl_log_info(&fp_mod, "sel hdr %08x", s.hdr);
	fenykl_log_info(&fp_mod, "afpkt +00 %08x %08x %08x %08x", s.pkt[0], s.pkt[1], s.pkt[2],
			s.pkt[3]);
	fenykl_log_info(&fp_mod, "afpkt +10 %08x +24 %08x %08x", s.pkt[4], s.pkt[5], s.pkt[6]);
}

static void fp_poll_point(uint32_t now)
{
	uint32_t p[4];

	if (FENYKL_LOG_ELAPSED(fp.point_time, now) < FP_GAP_US)
		return;
	for (unsigned int i = 0; i < 4; i++)
		p[i] = FP_U32(FP_AF_POINT + 4 * i);
	if (!memcmp(p, fp.point, sizeof(p)) || !fp_budget(1))
		return;
	memcpy(fp.point, p, sizeof(p));
	fp.point_time = now;
	fenykl_log_info(&fp_mod, "afpoint %08x %08x %08x %08x", p[0], p[1], p[2], p[3]);
}

static void fp_read_misc(struct fp_misc *m)
{
	for (unsigned int i = 0; i < FP_DESC_WORDS; i++)
		m->desc[i] = FP_U32(FP_DESC + 4 * i);
	m->desc[0] = 0;			/* Y address */
	m->desc[4] = 0;			/* IS shift, per frame */
	m->desc[6] &= 0xFFFFu;		/* scale; frame_count is the high half */
	m->desc[7] &= 0xFFFF0000u;	/* buffer id is the low half */
	m->set_info[0] = FP_U32(FP_SET_INFO - 1) >> 8;	/* bytes +0..+2 */
	m->set_info[1] = FP_U16(FP_SET_INFO + 3) | (uint32_t)FP_U16(FP_SET_INFO + 5) << 16;
	for (unsigned int i = 0; i < FP_RING_N; i++) {
		m->ring_id[i] = FP_U16(FP_RING + 12 * i);
		m->ring_addr[i] = FP_U32(FP_RING + 12 * i + 4);
	}
	for (unsigned int i = 0; i < FP_NPROPS; i++)
		m->props[i] = fp_prop(fp_props[i]);
}

/* Slow state: the descriptor's geometry (CPU2 writes it word by word: two equal reads), set_info,
   the MVR ring and the properties. */
static void fp_poll_misc(void)
{
	struct fp_misc a, b;

	fp_read_misc(&a);
	fp_read_misc(&b);
	if (memcmp(&a, &b, sizeof(a)) || !memcmp(&a, &fp.misc, sizeof(a)) || !fp_budget(10))
		return;
	fp.misc = a;
	fenykl_log_info(&fp_mod, "desc +04 %08x %08x %08x", a.desc[1], a.desc[2], a.desc[3]);
	fenykl_log_info(&fp_mod, "desc +14 %08x %08x %08x (+10 masked)", a.desc[5], a.desc[6],
			a.desc[7]);
	fenykl_log_info(&fp_mod, "desc +20 %08x %08x, set_info %08x %08x", a.desc[8], a.desc[9],
			a.set_info[0], a.set_info[1]);
	for (unsigned int i = 0; i < FP_RING_N; i++)
		fenykl_log_info(&fp_mod, "ring %u id %x addr %08x, idx %u", i, a.ring_id[i],
				a.ring_addr[i], FP_U16(FP_RING_IDX));
	fenykl_log_info(&fp_mod, "props 9121 %x 69f1 %x c021 %x", a.props[0], a.props[1],
			a.props[2]);
	fenykl_log_info(&fp_mod, "props 6761 %x 7e91 %x 3081 %x", a.props[3], a.props[4],
			a.props[5]);
}

static void fp_rates(void)
{
	uint32_t c = FP_U16(FP_CYCLES), p = FP_U16(FP_PASSES), f = FP_U16(FP_DESC + 0x1A);

	if (fp_budget(1))
		fenykl_log_info(&fp_mod, "5s cycles %u passes %u desc %u frames, %u max faces",
				(c - fp.cycles) & 0xFFFFu, (p - fp.passes) & 0xFFFFu,
				(f - fp.frame) & 0xFFFFu, fp.max_faces);
	fp.cycles = c;
	fp.passes = p;
	fp.frame = f;
	fp.max_faces = 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;
	uint32_t start = FENYKL_LOG_TIMER_NOW(), sec = start, rate = start, now = start;

	fenykl_log_info(&fp_mod, "facepoll: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	/* the first poll logs everything */
	fp.res[0].count = fp.res[1].count = ~0u;
	fp.res_time[0] = fp.res_time[1] = fp.eye_time = fp.point_time = start - FP_GAP_US;
	fp.eye.on = ~0u;
	fp.sel.kind = ~0u;
	fp.point[0] = ~0u;
	fp.misc.desc[0] = ~0u;
	fp.cycles = FP_U16(FP_CYCLES);
	fp.passes = FP_U16(FP_PASSES);
	fp.frame = FP_U16(FP_DESC + 0x1A);
	while (FENYKL_LOG_ELAPSED(start, now) < FP_RUN_S * 1000000u) {
		fp_poll_res(0, now);
		fp_poll_res(1, now);
		fp_poll_eye(now);
		fp_poll_sel();
		fp_poll_point(now);
		fp_poll_misc();
		if (FENYKL_LOG_ELAPSED(sec, now) >= 1000000u) {
			sec = now;
			fp.lines_sec = 0;
		}
		if (FENYKL_LOG_ELAPSED(rate, now) >= FP_RATE_US) {
			rate = now;
			fp_rates();
		}
		fw_OlyTaskSleep(FP_POLL_TICKS);
		now = FENYKL_LOG_TIMER_NOW();
	}
	fenykl_log_get_stats(&st);
	fenykl_log_info(&fp_mod, "facepoll end: %u s, %u lines, %u skipped, %u overwritten",
			FENYKL_LOG_ELAPSED(start, now) / 1000000u, fp.lines, fp.skipped,
			st.overwritten0);
}
