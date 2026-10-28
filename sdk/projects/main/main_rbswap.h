/*
 * main: option "R/B swap". The img_exec hook (main_rbswap_hook.c) exchanges rows 0 and 2
 * of the colour matrix of every pass that applies the WB matrix, so red and blue swap in live view
 * and JPEG stills in every WB setting.
 */
#ifndef _MAIN_RBSWAP_H
#define _MAIN_RBSWAP_H

#include <stdbool.h>
#include <body.h>

/* Built for targets with img_exec only (plugin.toml); elsewhere the task side does nothing. */
#if FENYKL_HAS_IMG_EXEC
/* Written by the hook, read by the plugin task; FENYKL_SHARED_PTR(main_rbswap_hk) only. */
struct main_rbswap_hook_state {
	uint32_t enable;	/* written by the task (the option): swap while non-zero */
	uint32_t cls[4];	/* CPU0 passes by fenykl_img_mode_class 0..3 */
	uint32_t eng1;		/* CPU0 engine-1 passes (stills, review, playback) */
	uint32_t cpu1;		/* passes on CPU1, never swapped */
	uint32_t swaps[2];	/* swapped passes: [0] engine 1, [1] engine 2 */
	uint32_t identity;	/* class 0/1 passes skipped: ashMat has no off-diagonal */
	uint32_t no_param;	/* class 0/1 passes without IMG_PARAM */
	uint32_t kept;		/* ashMat changed during a swapped pass, so it was not restored */
	uint32_t m_seq;		/* +1 per swapped engine-1 pass; m_mode and m hold its input */
	uint32_t m_mode;
	int16_t m[FENYKL_IMG_MAT_N];
};

extern struct main_rbswap_hook_state main_rbswap_hk;

/* main_rbswap_hook.c: img_exec with the swap (main_img_hook.c); any task, any core */
unsigned int main_rbswap_exec(void *ctrl);

/* main_rbswap.c, plugin task */
void main_rbswap_start(void);
void main_rbswap_tick(void);
void main_rbswap_set_level(unsigned int level);
void main_rbswap_status(void);
#else
static inline void main_rbswap_start(void)
{
}

static inline void main_rbswap_tick(void)
{
}

static inline void main_rbswap_set_level(unsigned int level)
{
	(void)level;
}

static inline void main_rbswap_status(void)
{
}
#endif

#endif
