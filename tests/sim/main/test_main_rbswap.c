/* The main plugin's R/B swap hook (sdk/projects/main/main_rbswap_hook.c) in the GDB simulator, with
   img_exec.S (img_exec at its real address with its real entry bytes), ../hooks/sim_start.S and
   ../hooks/support.S. plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
     1   install failed, or a pass changed the result or a callee-saved register / SP
     2   option off: the pass sees the camera matrix M, nothing counted as swapped
     4   on, live view (ucMode 7, engine 2): the pass sees P x M (rows 0 and 2 of M exchanged), M is
         restored afterwards
     8   on, classes 2 (ucMode 0x15) and 3 (ucMode 6, 0x40): not swapped
     16  on, an identity class 0 pass: not swapped, identity counted
     32  on, still (ucMode 9, engine 1): P x M, its ucMode and input matrix recorded
     64  on, the matrix changes during the pass: the hook keeps the new matrix (kept counted)
     128 on, no IMG_PARAM: the original runs, no_param counted
     256 off again: passes through
     512 the per-class and engine-1 counters */
#include <fenykl.h>

#include "../../../sdk/projects/main/main_rbswap.h"

unsigned call_fw_exec_caller(void *ctrl);

volatile uint32_t mock_args[12]; /* the irq/cache mocks record here */
uint32_t sim_exit_code = 99;
volatile int16_t seen_mat[9]; /* written by the img_exec mock */
volatile uint32_t store_new;

static const int16_t camera[9] = { 0xB5, -0x1C, -0x19, -0x1C, 0xC0, -0x24, 0x01, -0x44, 0xC3 };
static const int16_t pxm[9] = { 0x01, -0x44, 0xC3, -0x1C, 0xC0, -0x24, 0xB5, -0x1C, -0x19 };
static const int16_t identity[9] = { 0x80, 0, 0, 0, 0x80, 0, 0, 0, 0x80 };

static uint32_t ctrl_mem[0xC8 / 4], param_mem[0x60 / 4];
static uint8_t *const ctrl = (uint8_t *)ctrl_mem;
static uint8_t *const param = (uint8_t *)param_mem;

static int16_t *mat(void)
{
	return (int16_t *)(param + FENYKL_IMG_PARAM_MAT);
}

static int same(const volatile int16_t *a, const int16_t *b)
{
	for (int i = 0; i < 9; i++)
		if (a[i] != b[i])
			return 0;
	return 1;
}

/* One pass with ucMode `mode`, engine 2 if `eng2`, ashMat = `m`; returns the img_exec result. */
static unsigned pass(unsigned mode, int eng2, const int16_t *m)
{
	ctrl[FENYKL_IMG_CTRL_MODE] = (uint8_t)mode;
	*(uint32_t *)(ctrl + FENYKL_IMG_CTRL_FLAG) = eng2 ? FENYKL_IMG_FLAG_ENGINE2 : 0;
	*(uint8_t **)(ctrl + FENYKL_IMG_CTRL_PARAM) = param;
	memcpy(mat(), m, sizeof(camera));
	for (int i = 0; i < 9; i++)
		seen_mat[i] = 0x7777;
	return call_fw_exec_caller(ctrl);
}

/* The pass saw `in` and left `out` in ashMat. */
static int saw(const int16_t *in, const int16_t *out)
{
	return same(seen_mat, in) && same(mat(), out);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	volatile struct main_rbswap_hook_state *s = FENYKL_SHARED_PTR(main_rbswap_hk);
	volatile uint32_t *words = (volatile uint32_t *)s;
	int16_t updated[9];
	int fail = 0;

	(void)load_base;
	(void)image_size;
	for (unsigned i = 0; i < sizeof(struct main_rbswap_hook_state) / 4; i++)
		words[i] = 0;

	if (main_rbswap_hook_install() != 0)
		fail |= 1;

	if (pass(7, 1, camera) != 0x2d00)
		fail |= 1;
	if (!saw(camera, camera) || s->swaps[1] != 0)
		fail |= 2;

	s->enable = 1;
	if (pass(7, 1, camera) != 0x2d00)
		fail |= 1;
	if (!saw(pxm, camera) || s->swaps[1] != 1 || s->swaps[0] != 0 || s->m_seq != 0)
		fail |= 4;

	pass(0x15, 1, camera);
	if (!saw(camera, camera))
		fail |= 8;
	pass(6, 0, camera);
	if (!saw(camera, camera))
		fail |= 8;
	pass(0x40, 0, camera);
	if (!saw(camera, camera) || s->swaps[0] != 0)
		fail |= 8;

	pass(0, 0, identity);
	if (!saw(identity, identity) || s->identity != 1 || s->swaps[0] != 0)
		fail |= 16;

	pass(9, 0, camera);
	if (!saw(pxm, camera) || s->swaps[0] != 1 || s->m_seq != 1 || s->m_mode != 9 ||
	    !same(s->m, camera))
		fail |= 32;

	store_new = 1;
	pass(7, 1, camera);
	store_new = 0;
	memcpy(updated, pxm, sizeof(updated));
	updated[1] = 0x40;
	if (!saw(pxm, updated) || s->kept != 1)
		fail |= 64;

	*(uint8_t **)(ctrl + FENYKL_IMG_CTRL_PARAM) = 0;
	if (call_fw_exec_caller(ctrl) != 0x2d00 || s->no_param != 1)
		fail |= 128;

	s->enable = 0;
	pass(9, 0, camera);
	if (!saw(camera, camera) || s->swaps[0] != 1 || s->m_seq != 1)
		fail |= 256;

	/* passes: 7, 7, 0x15, 6, 0x40, 0, 9, 7, 7 (no IMG_PARAM), 9; engine 1: 6, 0x40, 0, 9, 9 */
	if (s->cls[0] != 5 || s->cls[1] != 2 || s->cls[2] != 1 || s->cls[3] != 2 || s->eng1 != 5 ||
	    s->cpu1 != 0 || s->swaps[1] != 2)
		fail |= 512;

	fenykl_hook_remove(&fenykl_hook_img_exec);
	sim_exit_code = fail;
}
