/* E-M10 II hook runtime (sdk/arch/em10ii/hooks.c) on the real entry spans of appevent_post and
   cw_updateDisplay, run in the GDB simulator with sim_start.S and fw.S. The SDK binds appevent_post
   itself (event layer), so the test hooks its span under the sim-only name simevent_post (same
   address and bytes, test_sim.py). In the simulator CHCTR
   (0x8070) is plain memory. plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
     1   originals before install: appevent_post(5, 7, 100) with 0x77 at 0xA1C184DA = 15+7+100+0x77,
         cw_updateDisplay(1) ran with A1 = 0x60BC53C4
     2   both installs succeed and are idempotent; each entry holds `jmp stub`
     4   hooked calls: the hooks ran, the relocated spans (absolute byte load, absolute A1) worked
         in the trampolines, the originals got the edited/unchanged arguments
     8   callee-saved registers and SP preserved through stub, wrapper and trampoline
     16  each install/remove: IRQ lock(0) and unlock paired, two D-cache flushes, one I-cache
         invalidate writing explicit CHCTR values: ICEN off (2), ICINV with ICEN off (0x12), entry
         value back (3); INV bits read as set at entry (0x33) are never written back
     32  remove: stock bytes back, calls reach the originals directly, remove idempotent
     64  an entry that is not the stock span is refused (EBYTES) and left untouched;
         fenykl_code_patch on one byte: a wrong `from` is refused (EBYTES, one lock and flush, no
         invalidate, byte untouched), 01 -> 03 and back as one install each, neighbours untouched
     128 ICBUSY stuck: install fails with EICACHE, the stock bytes are back, ICEN restored, one
         timeout counted after the poll bound; install works once ICBUSY clears */
#include <fenykl.h>

int call_ev_caller(int event, int param, int flag);
int call_cw_caller(int show_front, int unused1, int unused2);

#define CHCTR (*(volatile uint16_t *)0x8070u)
#define EVENT_INIT (*(volatile uint8_t *)0xA1C184DAu)
#define CW_A1 0x60BC53C4u

/* fw.S: [0] cw A1, [1] cw D0, [2] cw calls, [3] irq_lock calls, [4] its level, [5] irq_unlock calls,
   [6] its cookie, [7] dcache_flush_all calls */
volatile uint32_t mock[8];
uint32_t sim_exit_code = 99;

static struct {
	int ev_calls, cw_calls, cw_show;
} t;

static int on_event(unsigned event, unsigned param, unsigned flag)
{
	t.ev_calls++;
	return fw_orig_simevent_post(event + 1, param, flag) + 0x1000;
}
FENYKL_HOOK(simevent_post, on_event);

static void on_update(int show_front)
{
	t.cw_calls++;
	t.cw_show = show_front;
	fw_orig_cw_updateDisplay(show_front);
}
FENYKL_HOOK(cw_updateDisplay, on_update);

static int holds_jmp(const struct fenykl_hook *h)
{
	const volatile uint8_t *e = FENYKL_UNCACHED(h->entry);
	uint32_t rel = e[1] | e[2] << 8 | e[3] << 16 | (uint32_t)e[4] << 24;

	return e[0] == 0xdc && h->entry + rel == h->stub;
}

static int holds_stock(const struct fenykl_hook *h)
{
	const volatile uint8_t *e = FENYKL_UNCACHED(h->entry);

	for (int i = 0; i < h->length; i++)
		if (e[i] != h->original[i])
			return 0;
	return 1;
}

/* One install or remove: lock/unlock paired, two flushes, one invalidate that left ICEN on. */
static int one_patch(uint32_t locks, uint32_t flushes, uint32_t runs)
{
	struct fenykl_icache_stats st;

	fenykl_icache_get_stats(&st);
	return mock[3] == locks + 1 && mock[5] == locks + 1 && mock[4] == 0 &&
	       mock[6] == 0x80000700u && mock[7] == flushes + 2 && st.runs == runs + 1 &&
	       st.chctr[0] == 0x0003 && st.chctr[1] == 0x0002 && st.chctr[2] == 0x0012 &&
	       st.chctr[3] == 0x0003 && st.polls[0] == 0 && st.polls[1] == 0;
}

static int patch_counted(int (*op)(struct fenykl_hook *), struct fenykl_hook *h)
{
	struct fenykl_icache_stats st;
	uint32_t locks = mock[3], flushes = mock[7];

	fenykl_icache_get_stats(&st);
	CHCTR = 0x0003;
	return op(h) == 0 && one_patch(locks, flushes, st.runs);
}

/* INV bits that read back as 1 are cleared, not re-asserted. */
static int sim_sticky_inv(void)
{
	struct fenykl_icache_stats st;

	CHCTR = 0x0033;
	if (fenykl_icache_invalidate() != 0)
		return 1;
	fenykl_icache_get_stats(&st);
	return st.chctr[1] != 0x0002 || st.chctr[2] != 0x0012 || CHCTR != 0x0003;
}

static uint8_t code[4] = { 0x80, 0x01, 0xdf, 0x00 };

static int code_is(uint8_t imm)
{
	const volatile uint8_t *c = FENYKL_UNCACHED(code);

	return c[0] == 0x80 && c[1] == imm && c[2] == 0xdf && c[3] == 0x00;
}

static int sim_code_patch(void)
{
	static const uint8_t one = 1, three = 3;
	uint32_t at = (uint32_t)&code[1], locks = mock[3], flushes = mock[7];
	struct fenykl_icache_stats st;

	fenykl_icache_get_stats(&st);
	CHCTR = 0x0003;
	if (fenykl_code_patch(at, &three, &one, 1) != FENYKL_HOOK_EBYTES || !code_is(1) ||
	    mock[3] != locks + 1 || mock[5] != locks + 1 || mock[7] != flushes + 1)
		return 1;
	locks = mock[3];
	flushes = mock[7];
	if (fenykl_code_patch(at, &one, &three, 1) != 0 || !code_is(3) ||
	    !one_patch(locks, flushes, st.runs))
		return 1;
	locks = mock[3];
	flushes = mock[7];
	return fenykl_code_patch(at, &three, &one, 1) != 0 || !code_is(1) ||
	       !one_patch(locks, flushes, st.runs + 1);
}

static int sim_icache(void)
{
	struct fenykl_hook *h = &fenykl_hook_cw_updateDisplay;
	struct fenykl_icache_stats st;

	CHCTR = 0x0007; /* ICEN, DCEN and a stuck ICBUSY */
	if (fenykl_hook_install(h) != FENYKL_HOOK_EICACHE || h->installed || !holds_stock(h))
		return 1;
	fenykl_icache_get_stats(&st);
	if (st.timeouts != 1 || st.polls[0] != 20000 || st.polls[1] != 0 || CHCTR != 0x0007)
		return 1;
	CHCTR = 0x0003;
	return fenykl_hook_install(h) != 0 || !holds_jmp(h) || fenykl_hook_remove(h) != 0 ||
	       !holds_stock(h);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct fenykl_hook *ev = &fenykl_hook_simevent_post, *cw = &fenykl_hook_cw_updateDisplay;
	volatile uint8_t *ev_entry = FENYKL_UNCACHED(ev->entry);
	int fail = 0, r;

	(void)load_base;
	(void)image_size;
	EVENT_INIT = 0x77;
	if (call_ev_caller(5, 7, 100) != 15 + 7 + 100 + 0x77 || call_cw_caller(1, 0, 0) != 0 ||
	    mock[0] != CW_A1 || mock[1] != 1 || mock[2] != 1)
		fail |= 1;

	if (!patch_counted(fenykl_hook_install, ev))
		fail |= 16;
	if (!patch_counted(fenykl_hook_install, cw))
		fail |= 16;
	if (fenykl_hook_install(ev) != 0 || fenykl_hook_install(cw) != 0 || !holds_jmp(ev) ||
	    !holds_jmp(cw))
		fail |= 2;

	r = call_ev_caller(5, 7, 100);
	if (r == 0xbad)
		fail |= 8;
	else if (r != 18 + 7 + 100 + 0x77 + 0x1000 || t.ev_calls != 1)
		fail |= 4;
	mock[0] = 0;
	r = call_cw_caller(0x10000, 0, 0);
	if (r == 0xbad)
		fail |= 8;
	else if (t.cw_calls != 1 || t.cw_show != 0x10000 || mock[0] != CW_A1 ||
		 mock[1] != 0x10000 || mock[2] != 2)
		fail |= 4;

	if (!patch_counted(fenykl_hook_remove, ev) || !patch_counted(fenykl_hook_remove, cw))
		fail |= 16;
	if (fenykl_hook_remove(ev) != 0 || !holds_stock(ev) || !holds_stock(cw) ||
	    call_ev_caller(5, 7, 100) != 15 + 7 + 100 + 0x77 || call_cw_caller(1, 0, 0) != 0 ||
	    t.ev_calls != 1 || t.cw_calls != 1 || mock[2] != 3)
		fail |= 32;

	ev_entry[5] ^= 0xff;
	if (fenykl_hook_install(ev) != FENYKL_HOOK_EBYTES || ev->installed || ev_entry[0] != 0x83)
		fail |= 64;
	ev_entry[5] ^= 0xff;

	if (sim_sticky_inv())
		fail |= 16;
	if (sim_icache())
		fail |= 128;
	if (sim_code_patch())
		fail |= 64;

	sim_exit_code = fail;
}
