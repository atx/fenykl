/* The event layer (sdk/lib/event.c) in the GDB simulator with ../hooks/fw.S (appevent_post mock at
   its real address with its real entry bytes: 3*event + param + flag; fw_caller checks the
   callee-saved registers), ../hooks/sim_start.S and ../hooks/support.S. plugin_main sets
   sim_exit_code to the failure bit mask (0 = pass):
     1   stock result before install; install patches the entry (jmp) and is idempotent
     2   prio order (equal prio in registration order), a disabled handler skipped, the edits of
         every handler reach the firmware, registers and SP preserved
     4   CONSUME: hook returns 0, later handlers skipped, counters
     8   inject from a handler reaches the firmware with its own arguments, no handler sees it
     16  a handler posting through fw_appevent_post: nested entries counted, the third skips the
         chain (bypassed), recursion ends
     32  add: no fn (EARG), twice (EBUSY), after install by prio; enabled = false pauses
     64  remove restores the stock entry (idempotent); dispatch without the patch runs the chain;
         event count
     128 CPU1 entry: counted, chain skipped, stock result
   (the simulator's exit status keeps 8 bits) */
#include <event.h>

#define SIM_CORE (*(volatile uint32_t *)0x8050u)
#define EV_CONSUME 0x77u
#define EV_INJECT 0x55u
#define EV_NEST 0x66u

int call_fw_caller(int event, int param, int flag);

volatile uint32_t mock_args[12]; /* support.S records here */
uint32_t sim_exit_code = 99;

static char trace[16];
static unsigned int ntrace;
static int inj_ret, nest_ret;

static void mark(char id)
{
	if (ntrace < sizeof(trace) - 1)
		trace[ntrace++] = id;
}

static int traced(const char *want)
{
	unsigned int i;

	for (i = 0; want[i]; i++)
		if (i >= ntrace || trace[i] != want[i])
			return 0;
	return i == ntrace;
}

static enum fenykl_event_action on_b(struct fenykl_event *ev, void *arg)
{
	(void)arg;
	mark('b');
	if (ev->event == 5)
		ev->event = 6;
	if (ev->event == EV_INJECT)
		inj_ret = fenykl_event_inject(0x40, 1, 2);
	if (ev->event == EV_NEST)
		nest_ret = fw_appevent_post(EV_NEST, 0, 0);
	return FENYKL_EVENT_PASS;
}

static enum fenykl_event_action on_a(struct fenykl_event *ev, void *arg)
{
	mark(*(const char *)arg);
	if (ev->event == 6)
		ev->param += 2;
	return ev->event == EV_CONSUME ? FENYKL_EVENT_CONSUME : FENYKL_EVENT_PASS;
}

static enum fenykl_event_action on_c(struct fenykl_event *ev, void *arg)
{
	(void)arg;
	mark('c');
	if (ev->event == 6)
		ev->flag += 3;
	return FENYKL_EVENT_PASS;
}

static enum fenykl_event_action on_other(struct fenykl_event *ev, void *arg)
{
	(void)ev;
	mark(*(const char *)arg);
	return FENYKL_EVENT_PASS;
}

static struct fenykl_event_handler ha = {
	.name = "a", .fn = on_a, .arg = "a", .prio = 10, .enabled = true,
};
static struct fenykl_event_handler hc = { .name = "c", .fn = on_c, .prio = 10, .enabled = true };
static struct fenykl_event_handler hb = { .name = "b", .fn = on_b, .prio = 5, .enabled = true };
static struct fenykl_event_handler hd = {
	.name = "d", .fn = on_other, .arg = "d", .prio = 0, .enabled = false,
};
static struct fenykl_event_handler hf = {
	.name = "f", .fn = on_other, .arg = "f", .prio = 255, .enabled = true,
};
static struct fenykl_event_handler hnull = { .name = "null", .enabled = true };

static int post(int event, int param, int flag)
{
	ntrace = 0;
	return call_fw_caller(event, param, flag);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	volatile uint8_t *entry = FENYKL_UNCACHED(fenykl_hook_appevent_post.entry);
	struct fenykl_event_stats st;
	uint8_t stock[6];
	uint32_t seen_c;
	int fail = 0;
	int r;

	for (int i = 0; i < 6; i++)
		stock[i] = entry[i];
	if (post(5, 7, 100) != 122 || fenykl_event_add(&ha) || fenykl_event_add(&hc) ||
	    fenykl_event_add(&hb) || fenykl_event_add(&hd) || fenykl_event_install() ||
	    fenykl_event_install() || entry[0] != 0xdc)
		fail |= 1;

	r = post(5, 7, 100);
	if (r != 3 * 6 + 9 + 103 || !traced("bac"))
		fail |= 2;

	seen_c = hc.private.seen;
	fenykl_event_get_stats(&st);
	if (post(EV_CONSUME, 0, 0) != 0 || !traced("ba") || hc.private.seen != seen_c ||
	    ha.private.consumed != 1 || hb.private.consumed != 0 || st.consumed != 0)
		fail |= 4;
	fenykl_event_get_stats(&st);
	if (st.consumed != 1)
		fail |= 4;

	r = post(EV_INJECT, 1, 1);
	fenykl_event_get_stats(&st);
	if (r != 3 * EV_INJECT + 2 || inj_ret != 3 * 0x40 + 3 || !traced("bac") || st.injected != 1 ||
	    st.nested != 0 || hb.private.seen != 3)
		fail |= 8;

	r = post(EV_NEST, 0, 0);
	fenykl_event_get_stats(&st);
	if (r != 3 * EV_NEST || nest_ret != 3 * EV_NEST || !traced("bbacac") || st.nested != 2 ||
	    st.bypassed != 1)
		fail |= 16;

	if (fenykl_event_add(&hnull) != FENYKL_EVENT_EARG ||
	    fenykl_event_add(&ha) != FENYKL_EVENT_EBUSY || fenykl_event_add(&hf) ||
	    post(5, 7, 100) != 3 * 6 + 9 + 103 || !traced("bacf"))
		fail |= 32;
	hc.enabled = false;
	if (post(5, 7, 100) != 3 * 6 + 9 + 100 || !traced("baf"))
		fail |= 32;

	if (fenykl_event_remove() || fenykl_event_remove() || post(5, 7, 100) != 122 ||
	    !traced(""))
		fail |= 64;
	for (int i = 0; i < 6; i++)
		if (entry[i] != stock[i])
			fail |= 64;
	ntrace = 0;
	if (fenykl_event_dispatch(5, 7, 100) != 3 * 6 + 9 + 100 || !traced("baf"))
		fail |= 64;
	fenykl_event_get_stats(&st);
	if (st.events != 9 || st.cpu1 != 0)
		fail |= 64;

	SIM_CORE = 1;
	ntrace = 0;
	r = fenykl_event_dispatch(5, 7, 100);
	SIM_CORE = 0;
	fenykl_event_get_stats(&st);
	if (r != 122 || !traced("") || st.cpu1 != 1 || st.events != 9)
		fail |= 128;

	sim_exit_code = fail;
}
