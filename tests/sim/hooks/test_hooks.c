/* Hook mechanism in the fenykl SDK, run in the GDB
   simulator with sim_start.S and fw.S plus the irq/cache mocks of support.S. Ports the PoC
   scenarios and adds the stack-argument case the PoC lacked (stacksum). plugin_main sets
   sim_exit_code to the failure bit mask (0 = pass):
   The appevent_post scenario hooks simevent_post, a sim-only alias of the mock at appevent_post's
   address (the SDK event layer binds appevent_post itself, tests/sim/event/):
     1   original appevent_post (3*5 + 7 + 100 = 122) before install
     2   install reported success (and is idempotent)
     4   after install: hook adds 1 to event (+3) and 0x1000, calls the original, count == 1
     8   callee-saved registers / SP preserved across the bridge (fw_caller returns 0xbad otherwise)
     16  the entry holds the jmp (0xdc) after install
     32  after remove: original result again, count unchanged, remove idempotent, bytes restored
     64  stacksum original (1+2+3+4 = 10) before install, or the stack hook: c,d arrived through the
         stack, hook doubled the result (2*(1+2+3+4) = 20)
     128 gpi_flush (void, no args, relocated absolute load): the hook ran before the original saw the
         flag, the original then flipped the draw index; registers and SP preserved. Also
         mov_yc_resize_pass (CALL [d2,d3,a2,a3],0x14, relocated absolute load into A2): original
         result before install, hook sees both pointers and adds 0x100, registers and SP preserved
   (the simulator's exit status keeps 8 bits) */
#include <fenykl.h>

int call_fw_caller(int event, int param, int flag);
int call_fw_stackcaller(int a, int b, int c, int d);
int call_fw_flush_caller(void);
unsigned call_fw_resize_caller(const unsigned *in, const unsigned *out);

#define GPI_DRAW_BUF (*(volatile uint8_t *)0x6034D5B4u)
#define GPI_DIRTY (*(volatile uint8_t *)0x6034D5B5u)
#define CLIP_IMG_CTRL (*(volatile uint32_t *)0x6034CACCu)

volatile uint32_t mock_args[12]; /* the irq/cache mocks record here */
uint32_t sim_exit_code = 99;

static volatile int count;
static int observe(unsigned event, unsigned param, unsigned flag)
{
	count++;
	return fw_orig_simevent_post(event + 1, param, flag) + 0x1000;
}
FENYKL_HOOK(simevent_post, observe);

static volatile int saw_c, saw_d;
static int stackhook(unsigned a, unsigned b, unsigned c, unsigned d)
{
	saw_c = c;
	saw_d = d;
	return 2 * fw_orig_stacksum(a, b, c, d);
}
FENYKL_HOOK(stacksum, stackhook);

static volatile int flushes, saw_dirty, saw_draw;
static void flushhook(void)
{
	flushes++;
	saw_dirty = GPI_DIRTY;
	saw_draw = GPI_DRAW_BUF;
	fw_orig_gpi_flush();
}
FENYKL_HOOK(gpi_flush, flushhook);

static volatile int resizes;
static void *volatile rs_in, *volatile rs_out;
static unsigned resizehook(void *in, void *out)
{
	resizes++;
	rs_in = in;
	rs_out = out;
	return fw_orig_mov_yc_resize_pass(in, out) + 0x100;
}
FENYKL_HOOK(mov_yc_resize_pass, resizehook);

static uint8_t stock[6];
static int entry_differs(struct fenykl_hook *h)
{
	volatile uint8_t *entry = FENYKL_UNCACHED(h->entry);
	for (int i = 0; i < 6; i++)
		if (entry[i] != stock[i])
			return 1;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	int fail = 0;
	volatile uint8_t *entry = FENYKL_UNCACHED(fenykl_hook_simevent_post.entry);

	if (call_fw_caller(5, 7, 100) != 122)
		fail |= 1;

	for (int i = 0; i < 6; i++)
		stock[i] = entry[i];
	if (fenykl_hook_install(&fenykl_hook_simevent_post) != 0 ||
	    fenykl_hook_install(&fenykl_hook_simevent_post) != 0)
		fail |= 2;
	if (entry[0] != 0xdc)
		fail |= 16;

	int r = call_fw_caller(5, 7, 100);
	if (r == 0xbad)
		fail |= 8;
	else if (r != 125 + 0x1000 || count != 1)
		fail |= 4;

	if (fenykl_hook_remove(&fenykl_hook_simevent_post) != 0 ||
	    fenykl_hook_remove(&fenykl_hook_simevent_post) != 0 ||
	    call_fw_caller(5, 7, 100) != 122 || count != 1 ||
	    entry_differs(&fenykl_hook_simevent_post))
		fail |= 32;

	if (call_fw_stackcaller(1, 2, 3, 4) != 10)
		fail |= 64;
	if (fenykl_hook_install(&fenykl_hook_stacksum) != 0 ||
	    call_fw_stackcaller(1, 2, 3, 4) != 20 || saw_c != 3 || saw_d != 4)
		fail |= 64;
	fenykl_hook_remove(&fenykl_hook_stacksum);

	GPI_DRAW_BUF = 0;
	GPI_DIRTY = 1;
	if (fenykl_hook_install(&fenykl_hook_gpi_flush) != 0 || call_fw_flush_caller() != 0 ||
	    flushes != 1 || saw_dirty != 1 || saw_draw != 0 || GPI_DRAW_BUF != 1 || GPI_DIRTY != 0 ||
	    call_fw_flush_caller() != 0 || flushes != 2 || GPI_DRAW_BUF != 1)
		fail |= 128;
	fenykl_hook_remove(&fenykl_hook_gpi_flush);

	static const unsigned rin = 5, rout = 7; /* 5 + 2 * 7 + 0x300 */
	CLIP_IMG_CTRL = 0x300;
	if (call_fw_resize_caller(&rin, &rout) != 0x313 ||
	    fenykl_hook_install(&fenykl_hook_mov_yc_resize_pass) != 0 ||
	    call_fw_resize_caller(&rin, &rout) != 0x413 || resizes != 1 || rs_in != &rin ||
	    rs_out != &rout)
		fail |= 128;
	fenykl_hook_remove(&fenykl_hook_mov_yc_resize_pass);

	sim_exit_code = fail;
}
