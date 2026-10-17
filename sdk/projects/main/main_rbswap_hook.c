/*
 * main: the img_exec hook of the R/B swap, free of other firmware calls so that the
 * simulator test (tests/sim/main/) runs it on img_exec's real entry bytes. It writes P x M:
 * rows 0 and 2 of the camera's matrix exchanged.
 *
 * img_exec is synchronous: the pass packs ashMat into the IPU shadow and has finished before it
 * returns [verified], so the restore cannot undo the swap. Only ucMode classes 0/1
 * read ashMat; the others use a ROM matrix. Class 0/1 passes with an identity ashMat are skipped:
 * stills follow the WB pass with identity passes, and swapping those would cancel it (P x P = I).
 * Runs on the caller's task and stack (MOV, REC_IMAGE, ...).
 */
#include "main_rbswap.h"

FENYKL_SHARED struct main_rbswap_hook_state main_rbswap_hk;

/* img_mode_class 0x6E762F48 for ucMode 0..0x1D; larger values are class 3 */
static const uint8_t main_rbswap_class[0x1E] = {
	0, 0, 0, 0, 0, 0, 3, 0, 0, 1, 1, 1, 3, 3, 3, 3,
	3, 3, 2, 2, 2, 2, 2, 3, 0, 0, 0, 0, 2, 2,
};

static bool main_rbswap_identity(const volatile int16_t *m)
{
	return !(m[1] || m[2] || m[3] || m[5] || m[6] || m[7]);
}

/*
 * P x M: rows 0 and 2 exchanged, R' = M row 2, B' = M row 0. Row sums stay 0x80, so the row-sum
 * packer reproduces it exactly.
 */
static void main_rbswap_pxm(int16_t *out, const int16_t *m)
{
	for (unsigned int i = 0; i < 3; i++) {
		out[i] = m[6 + i];
		out[3 + i] = m[3 + i];
		out[6 + i] = m[i];
	}
}

static unsigned int main_rbswap_exec(void *ctrl)
{
	volatile struct main_rbswap_hook_state *s = FENYKL_SHARED_PTR(main_rbswap_hk);
	const volatile uint8_t *c = ctrl;
	volatile int16_t *m;
	int16_t saved[MAIN_RBSWAP_MAT_N], swapped[MAIN_RBSWAP_MAT_N];
	unsigned int mode, cls, eng2, r, i;
	uint8_t *param;

	if (FENYKL_CORE_ID() != 0) {
		s->cpu1++;
		return fw_orig_img_exec(ctrl);
	}
	if (!ctrl)
		return fw_orig_img_exec(ctrl);
	mode = c[MAIN_RBSWAP_CTRL_MODE];
	cls = mode < sizeof(main_rbswap_class) ? main_rbswap_class[mode] : 3;
	eng2 = (*(const volatile uint32_t *)(c + MAIN_RBSWAP_CTRL_FLAG) &
		MAIN_RBSWAP_FLAG_ENGINE2) != 0;
	s->cls[cls]++;
	if (!eng2)
		s->eng1++;
	if (!s->enable || cls > 1)
		return fw_orig_img_exec(ctrl);

	param = *(uint8_t *const volatile *)(c + MAIN_RBSWAP_CTRL_PARAM);
	if (!param) {
		s->no_param++;
		return fw_orig_img_exec(ctrl);
	}
	m = (volatile int16_t *)(param + MAIN_RBSWAP_PARAM_MAT);
	if (main_rbswap_identity(m)) {
		s->identity++;
		return fw_orig_img_exec(ctrl);
	}

	for (i = 0; i < MAIN_RBSWAP_MAT_N; i++)
		saved[i] = m[i];
	main_rbswap_pxm(swapped, saved);
	for (i = 0; i < MAIN_RBSWAP_MAT_N; i++)
		m[i] = swapped[i];
	s->swaps[eng2]++;
	if (!eng2) {
		s->m_mode = mode;
		for (i = 0; i < MAIN_RBSWAP_MAT_N; i++)
			s->m[i] = saved[i];
		s->m_seq++;
	}

	r = fw_orig_img_exec(ctrl);

	/* A firmware task may have stored a new matrix meanwhile (LV WB update): keep that one. */
	for (i = 0; i < MAIN_RBSWAP_MAT_N; i++)
		if (m[i] != swapped[i]) {
			s->kept++;
			return r;
		}
	for (i = 0; i < MAIN_RBSWAP_MAT_N; i++)
		m[i] = saved[i];
	return r;
}
FENYKL_HOOK(img_exec, main_rbswap_exec);

int main_rbswap_hook_install(void)
{
	return fenykl_hook_install(&fenykl_hook_img_exec);
}
