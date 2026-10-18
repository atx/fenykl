/* E-M10 II focus-peaking poller: every PK_POLL_TICKS reads the DSP task's current request, MVR's peaking
   gate, the current MVR frame record's peaking fields, MVR's stored peak request and the four peaking
   settings (through their properties, the way the firmware's own getter reads them). Logs changes,
   one dump of a peaking (cmd 0xE) DSP request per second and counts per second. Reads only: no
   firmware call but the log library's, no firmware RAM write, no DSP register access. Stops after
   PK_RUN_S. */
#include <fenykl.h>
#include <log.h>

#define PK_POLL_TICKS 10
#define PK_RUN_S 200
#define PK_LINES 3000
#define PK_LINES_PER_S 30

#define PK_DSP_REQ 0x8FF00194u		/* u32 -> 0x4C-byte DSP request record */
#define PK_DSP_CMD 0x44			/* record +0x44 u8 command, +0x45 caller core, +0x46 flag */
#define PK_DSP_PEAK 0x0E
#define PK_GATE 0xA1C2EEB0u		/* u8, MVR global peaking gate, 0 = never */
#define PK_REC_IDX 0xA1C2BE8Cu		/* u16, current MVR frame record */
#define PK_REC 0xA1C2BEE0u		/* 5 x 0x260 B */
#define PK_REC_N 5
#define PK_REC_SIZE 0x260u
#define PK_STORED 0xA1B8BD04u		/* uncached alias of MVR's stored peak request (CPU2 data) */
/* property tables read by the firmware's property getter: 8-byte entries, object pointer at +4 */
#define PK_PROP_OVR_VALID 0xA1BE81F0u	/* u16, override table valid */
#define PK_PROP_OVR 0xA1BF01F4u
#define PK_PROP 0xA1BE81F4u
#define PK_U8(a) (*(volatile uint8_t *)(a))
#define PK_U16(a) (*(volatile uint16_t *)(a))
#define PK_U32(a) (*(volatile uint32_t *)(a))

/* settings 0x237, 0x236, 0x2F8, 0x2F9 are properties 0xE351, 0xE341, 0xF0A1, 0xF0B1 (index 0) */
static const uint16_t pk_props[] = { 0xE351, 0xE341, 0xF0A1, 0xF0B1 };
#define PK_NPROPS (sizeof(pk_props) / sizeof(pk_props[0]))

struct pk_rec {
	uint32_t gate;
	uint32_t on;		/* record +0x130 u8 */
	uint32_t prop;		/* record +0x1BD..+0x1BF as bytes 2..0 */
	uint32_t qpkt;		/* record +0x21A..+0x21C as bytes 2..0 */
};

static struct {
	uint32_t req;
	struct pk_rec rec;
	uint32_t props[PK_NPROPS];
	uint32_t stored_addr;
	uint32_t last_dump;
	/* per second */
	uint32_t req_n, peak_n, stored_n, stored_chg;
	unsigned int lines, lines_sec, skipped;
} pk;

static FENYKL_LOG_MODULE(pk_mod, "peakpl", FENYKL_LOG_INFO);

static bool pk_budget(unsigned int n)
{
	if (pk.lines + n > PK_LINES || pk.lines_sec + n > PK_LINES_PER_S) {
		pk.skipped++;
		return false;
	}
	pk.lines += n;
	pk.lines_sec += n;
	return true;
}

/* n bytes at a lie in DRAM (either alias), naturally aligned (word alignment from 4 up). */
static bool pk_dram(uint32_t a, uint32_t n)
{
	uint32_t c = a & 0x3FFFFFFFu;

	if ((a >> 30) != 1 && (a >> 30) != 2)
		return false;
	if (a & (n >= 4 ? 3 : n - 1))
		return false;
	return c >= 0x20000000u && c + n <= 0x40000000u;
}

/* The firmware's property read: lookup 0x60076078, then a load of the width in the id's low bits. */
static uint32_t pk_prop(uint32_t id)
{
	uint32_t off = (id >> 4) * 8 + 4, obj = 0;

	if (PK_U16(PK_PROP_OVR_VALID))
		obj = PK_U32(PK_PROP_OVR + off);
	if (!obj)
		obj = PK_U32(PK_PROP + off);
	if ((id & 7) == 4)
		return pk_dram(obj, 4) ? PK_U32(obj) : ~0u;
	if ((id & 7) == 2)
		return pk_dram(obj, 2) ? PK_U16(obj) : ~0u;
	return pk_dram(obj, 1) ? PK_U8(obj) : ~0u;
}

static void pk_read_rec(struct pk_rec *r)
{
	uint32_t idx = PK_U16(PK_REC_IDX), b;

	memset(r, 0, sizeof(*r));
	r->gate = PK_U8(PK_GATE);
	if (idx >= PK_REC_N)
		return;
	b = PK_REC + idx * PK_REC_SIZE;
	r->on = PK_U8(b + 0x130);
	r->prop = PK_U8(b + 0x1BD) << 16 | PK_U8(b + 0x1BE) << 8 | PK_U8(b + 0x1BF);
	r->qpkt = PK_U8(b + 0x21A) << 16 | PK_U8(b + 0x21B) << 8 | PK_U8(b + 0x21C);
}

static void pk_poll_req(uint32_t now)
{
	/* CPU2 or CPU1 wrote the record through their own caches: read it uncached */
	uint32_t req = PK_U32(PK_DSP_REQ), r = (uint32_t)FENYKL_UNCACHED(req);

	if (req == pk.req)
		return;
	pk.req = req;
	pk.req_n++;
	if (!pk_dram(req, 0x4C) || PK_U8(r + PK_DSP_CMD) != PK_DSP_PEAK)
		return;
	pk.peak_n++;
	if (FENYKL_LOG_ELAPSED(pk.last_dump, now) < 1000000u || !pk_budget(3))
		return;
	pk.last_dump = now;
	fenykl_log_info(&pk_mod, "dsp req %08x: +44 %08x, +00 %08x %08x", req, PK_U32(r + 0x44),
			PK_U32(r), PK_U32(r + 4));
	fenykl_log_info(&pk_mod, "dsp +08 %08x %08x %08x %08x", PK_U32(r + 8), PK_U32(r + 0xC),
			PK_U32(r + 0x10), PK_U32(r + 0x14));
	fenykl_log_info(&pk_mod, "dsp +18 %08x %08x %08x %08x", PK_U32(r + 0x18), PK_U32(r + 0x1C),
			PK_U32(r + 0x20), PK_U32(r + 0x24));
}

static void pk_poll(uint32_t now)
{
	struct pk_rec a, b;
	uint32_t addr;

	pk_poll_req(now);
	/* CPU2 rewrites the record per frame: two equal reads rule out a half-done copy */
	pk_read_rec(&a);
	pk_read_rec(&b);
	if (!memcmp(&a, &b, sizeof(a)) && memcmp(&a, &pk.rec, sizeof(a)) && pk_budget(2)) {
		pk.rec = a;
		fenykl_log_info(&pk_mod, "mvr gate %u rec %u on %u", a.gate, PK_U16(PK_REC_IDX),
				a.on);
		fenykl_log_info(&pk_mod, "mvr colour/level/bright prop %06x qpkt %06x", a.prop, a.qpkt);
	}
	addr = PK_U32(PK_STORED + 4);
	if (addr)
		pk.stored_n++;
	if (addr != pk.stored_addr) {
		pk.stored_addr = addr;
		pk.stored_chg++;
	}
	for (unsigned int i = 0; i < PK_NPROPS; i++) {
		uint32_t v = pk_prop(pk_props[i]);

		if (v == pk.props[i] || !pk_budget(1))
			continue;
		pk.props[i] = v;
		fenykl_log_info(&pk_mod, "prop %04x = %x", pk_props[i], v);
	}
}

/* Sv (record +0x30) follows the exposure, so it is sampled here, not logged on change. */
static void pk_second(void)
{
	uint32_t idx = PK_U16(PK_REC_IDX);

	pk.lines_sec = 0;
	if ((pk.req_n || pk.stored_n || pk.stored_chg) && pk_budget(2)) {
		fenykl_log_info(&pk_mod, "sec dsp %u peak %u, stored %u polls %u changes", pk.req_n,
				pk.peak_n, pk.stored_n, pk.stored_chg);
		fenykl_log_info(&pk_mod, "sec stored +0 %08x +4 %08x +8 %08x, sv %u", PK_U32(PK_STORED),
				PK_U32(PK_STORED + 4), PK_U32(PK_STORED + 8),
				idx < PK_REC_N ? PK_U16(PK_REC + idx * PK_REC_SIZE + 0x30) : ~0u);
	}
	pk.req_n = pk.peak_n = pk.stored_n = pk.stored_chg = 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	struct fenykl_log_stats st;
	uint32_t start = FENYKL_LOG_TIMER_NOW(), sec = start, now = start;

	fenykl_log_info(&pk_mod, "peakpoll: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	pk.rec.gate = ~0u; /* the first poll logs everything */
	for (unsigned int i = 0; i < PK_NPROPS; i++)
		pk.props[i] = ~1u;
	pk.last_dump = start - 1000000u;
	while (FENYKL_LOG_ELAPSED(start, now) < PK_RUN_S * 1000000u) {
		pk_poll(now);
		if (FENYKL_LOG_ELAPSED(sec, now) >= 1000000u) {
			sec = now;
			pk_second();
		}
		fw_OlyTaskSleep(PK_POLL_TICKS);
		now = FENYKL_LOG_TIMER_NOW();
	}
	fenykl_log_get_stats(&st);
	fenykl_log_info(&pk_mod, "peakpoll end: %u s, %u lines, %u skipped, %u overwritten",
			FENYKL_LOG_ELAPSED(start, now) / 1000000u, pk.lines, pk.skipped,
			st.overwritten0);
}
