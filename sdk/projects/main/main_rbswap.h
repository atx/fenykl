/*
 * main: option "R/B swap". The img_exec hook (main_rbswap_hook.c) exchanges rows 0 and 2
 * of the colour matrix of every pass that applies the WB matrix, so red and blue swap in live view
 * and JPEG stills in every WB setting.
 */
#ifndef _MAIN_RBSWAP_H
#define _MAIN_RBSWAP_H

#include <stdbool.h>
#include <fenykl.h>

/* IMG_CTRL (A0 of img_exec) and IMG_PARAM fields */
#define MAIN_RBSWAP_CTRL_FLAG 0x54u	/* u32 ulFlag */
#define MAIN_RBSWAP_CTRL_PARAM 0x60u	/* IMG_PARAM *pImgParam */
#define MAIN_RBSWAP_CTRL_MODE 0xC4u	/* u8 ucMode */
#define MAIN_RBSWAP_FLAG_ENGINE2 0x10000000u
#define MAIN_RBSWAP_PARAM_MAT 0x18u	/* s16 ashMat[9], row major, 0x80 = 1.0 */
#define MAIN_RBSWAP_MAT_N 9

/* Written by the hook, read by the plugin task; FENYKL_SHARED_PTR(main_rbswap_hk) only. */
struct main_rbswap_hook_state {
	uint32_t enable;	/* written by the task (the option): swap while non-zero */
	uint32_t cls[4];	/* CPU0 passes by img_mode_class 0..3 */
	uint32_t eng1;		/* CPU0 engine-1 passes (stills, review, playback) */
	uint32_t cpu1;		/* passes on CPU1, never swapped */
	uint32_t swaps[2];	/* swapped passes: [0] engine 1, [1] engine 2 */
	uint32_t identity;	/* class 0/1 passes skipped: ashMat has no off-diagonal */
	uint32_t no_param;	/* class 0/1 passes without IMG_PARAM */
	uint32_t kept;		/* ashMat changed during a swapped pass, so it was not restored */
	uint32_t m_seq;		/* +1 per swapped engine-1 pass; m_mode and m hold its input */
	uint32_t m_mode;
	int16_t m[MAIN_RBSWAP_MAT_N];
};

extern struct main_rbswap_hook_state main_rbswap_hk;

/* main_rbswap_hook.c, plugin task: 0 = patched (fenykl_hook_install) */
int main_rbswap_hook_install(void);

/* main_rbswap.c, plugin task */
int main_rbswap_start(void);
void main_rbswap_tick(void);
void main_rbswap_set_level(unsigned int level);
void main_rbswap_status(void);

#endif
