/* Callbacks, slots and cloned vtables in the GDB simulator, with fw.S (firmware-style callers
   and targets) and the irq/cache mocks of tests/sim/hooks/support.S. plugin_main sets sim_exit_code
   to the failure bit mask (0 = pass):
     1   `calls` into a reverse stub: register args by class, two stack args, int result; the
         caller's D2/D3/A2, SP and outgoing stack args survive (sim_* return 0xbad otherwise)
     2   pointer result: the firmware reads it from A0
     4   CALL-entered stub ([d2,a2],12, one stack arg): the RET undoes the caller's CALL frame
     8   fw_call_<sig> forward thunks (calls, call, pointer result) with stack args; GCC's
         callee-saved registers survive although the thunk uses A2
     16  slot replace/restore: only from the expected value, EBUSY, EVALUE leaves the word alone
     32  replacement in a slot calls the original (fw_orig_<slot>)
     64  cloned vtable: one method replaced and calling the original, the others stock, another
         object and the stock table untouched, attach/detach errors and idempotence
   (the simulator's exit status keeps 8 bits) */
#include <fenykl.h>

#define HOOK1_SLOT (*(volatile uint32_t *)0x605FD840u)
#define CHANGE_SLOT (*(volatile uint32_t *)0x6034CCB4u)
#define STOCK_VTABLE 0x6EE3F58Cu

int sim_calls_cbmix(uint32_t fn);
uint32_t sim_calls_cbptr(uint32_t fn);
int sim_call_cbcall(void);
unsigned sim_hook1(unsigned event);
int sim_change(unsigned id);
int sim_vget(void *obj, unsigned *out, unsigned id);
int sim_vset(void *obj, unsigned id, unsigned type, unsigned value);
void mock_mix(void);
void mock_ptr(void);
void mock_callee(void);

volatile uint32_t mock_args[12]; /* fw.S and support.S record here */
unsigned mock_cells[4];
uint32_t sim_exit_code = 99;
static uint32_t fail;

static volatile uint32_t saw[6];
static int cbmix(unsigned a, unsigned *p, unsigned b, unsigned *q, unsigned c, unsigned *r)
{
	saw[0] = a, saw[1] = (uint32_t)p, saw[2] = b, saw[3] = (uint32_t)q, saw[4] = c;
	saw[5] = (uint32_t)r;
	return 0x1000 + a + 0x10 * b + 0x100 * c;
}
FENYKL_CALLBACK(cbmix, cbmix);

static unsigned *cbptr(unsigned *base, unsigned index)
{
	return base + index;
}
FENYKL_CALLBACK(cbptr, cbptr);

static int cbcallfn(unsigned a, unsigned b, unsigned c)
{
	return 100 * a + 10 * b + c;
}
FENYKL_CALLBACK(cbcall, cbcallfn);

static unsigned bump(unsigned event)
{
	return event + 1;
}
FENYKL_CALLBACK(appevent_hook1, bump);

static unsigned other(unsigned event)
{
	return event;
}
FENYKL_CALLBACK(appevent_hook1, other);

static volatile uint32_t changes, last_change;
static void on_change(unsigned id)
{
	changes++;
	last_change = id;
	fw_orig_usrset_change_cb(id);
}
FENYKL_CALLBACK(usrset_change_cb, on_change);

static volatile uint32_t gets;
static int counted_get(void *self, unsigned *out, unsigned id)
{
	gets++;
	int r = fw_orig_hfw_setting_proxy_get(self, out, id);
	*out += 0x100;
	return r;
}
FENYKL_CALLBACK(hfw_getSetting, counted_get);

static void check_callbacks(void)
{
	if (sim_calls_cbmix(FENYKL_CB(cbmix, cbmix)) != 0x1321 || saw[0] != 1 ||
	    saw[1] != (uint32_t)&mock_cells[0] || saw[2] != 2 || saw[3] != (uint32_t)&mock_cells[1] ||
	    saw[4] != 3 || saw[5] != (uint32_t)&mock_cells[2])
		fail |= 1;
	if (sim_calls_cbptr(FENYKL_CB(cbptr, cbptr)) != (uint32_t)&mock_cells[2])
		fail |= 2;
	if (sim_call_cbcall() != 123)
		fail |= 4;
}

/* Runs `call` with sentinels in D2/D3/A2/A3, as tests/sim/test_api.c does. */
#define PRESERVED(call)                                                                      \
	do {                                                                                 \
		register uint32_t d2 __asm__("d2") = 0x5a0000d2;                              \
		register uint32_t d3 __asm__("d3") = 0x5a0000d3;                              \
		register uint32_t a2 __asm__("a2") = 0x5a0000a2;                              \
		register uint32_t a3 __asm__("a3") = 0x5a0000a3;                              \
		__asm__ volatile("" : "+r"(d2), "+r"(d3), "+r"(a2), "+r"(a3));                \
		call;                                                                        \
		__asm__ volatile("" : "+r"(d2), "+r"(d3), "+r"(a2), "+r"(a3));                \
		if (d2 != 0x5a0000d2 || d3 != 0x5a0000d3 || a2 != 0x5a0000a2 ||             \
		    a3 != 0x5a0000a3)                                                        \
			fail |= 8;                                                           \
	} while (0)

static void check_forward_thunks(void)
{
	int r = 0;
	unsigned *p = 0;

	PRESERVED(r = fw_call_cbmix((uint32_t)mock_mix, 11, &mock_cells[0], 22, &mock_cells[1], 33,
				    &mock_cells[2]));
	if (r != 0x600d || mock_args[0] != 11 || mock_args[1] != (uint32_t)&mock_cells[0] ||
	    mock_args[2] != 22 || mock_args[3] != (uint32_t)&mock_cells[1] || mock_args[4] != 33 ||
	    mock_args[5] != (uint32_t)&mock_cells[2])
		fail |= 8;
	PRESERVED(p = fw_call_cbptr((uint32_t)mock_ptr, &mock_cells[0], 3));
	if (p != &mock_cells[3])
		fail |= 8;
	PRESERVED(r = fw_call_cbcall((uint32_t)mock_callee, 1, 20, 300));
	if (r != 321)
		fail |= 8;
}

static void check_slots(void)
{
	struct fenykl_slot *s = &fenykl_slot_appevent_hook1;

	HOOK1_SLOT = 0;
	if (sim_hook1(7) != 7 || FENYKL_SLOT_REPLACE(appevent_hook1, bump) != 0 ||
	    HOOK1_SLOT != FENYKL_CB(appevent_hook1, bump) || sim_hook1(7) != 8 ||
	    FENYKL_SLOT_REPLACE(appevent_hook1, bump) != 0 ||
	    FENYKL_SLOT_REPLACE(appevent_hook1, other) != FENYKL_SLOT_EBUSY)
		fail |= 16;
	/* Someone else took the slot: restore must not overwrite it. */
	HOOK1_SLOT = 0x1234;
	if (fenykl_slot_restore(s) != FENYKL_SLOT_EVALUE || HOOK1_SLOT != 0x1234)
		fail |= 16;
	HOOK1_SLOT = FENYKL_CB(appevent_hook1, bump);
	if (fenykl_slot_restore(s) != 0 || HOOK1_SLOT != 0 || sim_hook1(7) != 7 ||
	    fenykl_slot_restore(s) != 0)
		fail |= 16;
	/* A slot that does not hold the stock value is never replaced. */
	HOOK1_SLOT = 0x1234;
	if (FENYKL_SLOT_REPLACE(appevent_hook1, bump) != FENYKL_SLOT_EVALUE || HOOK1_SLOT != 0x1234 ||
	    s->current != 0)
		fail |= 16;
	HOOK1_SLOT = 0;
	/* irq_disable_all was called and the saved PSW restored (support.S). */
	if (mock_args[0] == 0 || mock_args[1] != 0x700)
		fail |= 16;

	CHANGE_SLOT = fenykl_slot_usrset_change_cb.expected;
	if (FENYKL_SLOT_REPLACE(usrset_change_cb, on_change) != 0 || sim_change(0x10042) != 0 ||
	    changes != 1 || last_change != 0x42 || mock_args[6] != 0x42 || mock_args[7] != 1 ||
	    fenykl_slot_restore(&fenykl_slot_usrset_change_cb) != 0 || sim_change(5) != 0 ||
	    changes != 1 || mock_args[6] != 5 || mock_args[7] != 2)
		fail |= 32;
}

static void check_vtable(void)
{
	struct fenykl_vtable *vt = &fenykl_vtable_hfw_setting_proxy;
	const uint32_t *stock = (const uint32_t *)STOCK_VTABLE;
	static uint32_t object[2] = {STOCK_VTABLE, 0x1000};
	static uint32_t other_object[2] = {STOCK_VTABLE, 0x2000};
	static uint32_t wrong_class[2] = {0x6EE3F000, 0};
	unsigned out = 0;

	if (sim_vget(object, &out, 5) != 0 || out != 0x1005)
		fail |= 64;
	FENYKL_VTABLE_SET(hfw_setting_proxy_get, counted_get);
	if (fenykl_vtable_attach(vt, object) != 0 || object[0] != (uint32_t)vt->clone ||
	    fenykl_vtable_attach(vt, object) != 0 ||
	    fenykl_vtable_attach(vt, other_object) != FENYKL_SLOT_EBUSY)
		fail |= 64;
	for (int i = 0; i < 28; i++)
		if (vt->clone[i] != (i == 2 ? FENYKL_CB(hfw_getSetting, counted_get) : stock[i]))
			fail |= 64;
	if (stock[2] != fenykl_slot_hfw_setting_proxy_get.expected)
		fail |= 64;
	/* id is u16: the original masks it (exthu), the replacement passes it through. */
	if (sim_vget(object, &out, 0x30005) != 0 || out != 0x1105 || gets != 1)
		fail |= 64;
	if (sim_vset(object, 7, 0, 99) != 1 || mock_args[8] != (uint32_t)object || mock_args[9] != 7 ||
	    mock_args[10] != 0 || mock_args[11] != 99)
		fail |= 64;
	if (sim_vget(other_object, &out, 5) != 0 || out != 0x2005 || gets != 1)
		fail |= 64;
	if (fenykl_vtable_detach(vt) != 0 || object[0] != STOCK_VTABLE ||
	    fenykl_vtable_detach(vt) != 0 || sim_vget(object, &out, 5) != 0 || out != 0x1005 ||
	    gets != 1)
		fail |= 64;
	if (fenykl_vtable_attach(vt, wrong_class) != FENYKL_SLOT_EVALUE ||
	    wrong_class[0] != 0x6EE3F000 || vt->object != 0)
		fail |= 64;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	check_callbacks();
	check_forward_thunks();
	check_slots();
	check_vtable();
	sim_exit_code = fail;
}
