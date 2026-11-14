/*
 * main: option "R/B swap": red and blue exchanged in live view and JPEG stills in every WB setting
 * while it is on. Rows 0 and 2 of a colour matrix are exchanged: on the E-PL3 those of every
 * img_exec pass that applies the WB matrix (main_rbswap_hook.c), on the E-M10 II those of the
 * colour-matrix bases every matrix is computed from (main_rbswap_wb.c, main_rbswap_base.c).
 */
#ifndef _MAIN_RBSWAP_H
#define _MAIN_RBSWAP_H

#include <stdbool.h>
#include <body.h>

/* Built for targets with one of the two (plugin.toml); elsewhere the task side does nothing. */
#define MAIN_RBSWAP (FENYKL_HAS_IMG_EXEC || FENYKL_HAS_WB_BASE)
#if FENYKL_HAS_IMG_EXEC && FENYKL_HAS_WB_BASE
#error "main_rbswap: img_exec and wb_base each implement the R/B swap"
#endif

/* P x M: rows 0 and 2 of m exchanged, R' = M row 2, B' = M row 0; row sums are kept. */
static inline void main_rbswap_pxm(int16_t *out, const int16_t *m)
{
	for (unsigned int i = 0; i < 3; i++) {
		out[i] = m[6 + i];
		out[3 + i] = m[3 + i];
		out[6 + i] = m[i];
	}
}

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
#endif

#if FENYKL_HAS_WB_BASE
/* One base's registry state: the word our copy was stored in and the object it held before. */
struct main_rbswap_base {
	uint32_t slot;		/* 0 = no copy stored */
	uint32_t stock;
};

/* main_rbswap_base.c results of main_rbswap_base_assert */
#define MAIN_RBSWAP_KEPT	0	/* the slot holds the copy of an unchanged stock */
#define MAIN_RBSWAP_STORED	1	/* copy built and stored over the stock object */
#define MAIN_RBSWAP_FOREIGN	2	/* as STORED, but the slot held neither stock nor copy */
#define MAIN_RBSWAP_MOVED	3	/* the lookup moved to another slot: the old one restored, the
					   new one stored, or adopted if it already held the copy */
#define MAIN_RBSWAP_INVALID	(-1)	/* the slot's object is no valid base: nothing stored */
#define MAIN_RBSWAP_ENTRY	(-2)	/* the slot's entry is not the base's: nothing stored */
#define MAIN_RBSWAP_CHANGED	(-3)	/* the slot changed before the store: nothing stored */

/*
 * main_rbswap_base.c, plugin task. Makes the registry word the firmware reads base i from now point
 * at the base's uncached copy, rows 0 and 2 of the stock base exchanged, after putting back the
 * previous stock object (a moved slot, a changed stock).
 */
int main_rbswap_base_assert(unsigned int i);
/* Puts the stock object back into the recorded slot and into the slot the lookup gives now, where
   either holds the copy; false if one holds neither the copy nor the stock. */
bool main_rbswap_base_restore(unsigned int i);
const struct main_rbswap_base *main_rbswap_base_get(unsigned int i);
/* The copy of base i, uncached. */
const volatile int16_t *main_rbswap_base_copy(unsigned int i);
#endif

#if MAIN_RBSWAP
extern int32_t main_rbswap_on;
extern struct fenykl_log_module main_rbswap_log;

/* main_rbswap.c (E-PL3) or main_rbswap_wb.c (E-M10 II), plugin task */
void main_rbswap_start(void);
void main_rbswap_tick(void);
void main_rbswap_status(void);
/* main_rbswap.c */
void main_rbswap_set_level(unsigned int level);
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
