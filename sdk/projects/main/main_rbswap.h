/*
 * main: option "R/B swap": red and blue exchanged in live view and JPEG stills in every WB setting
 * while it is on. Rows 0 and 2 of the colour-matrix bases every matrix is computed from are
 * exchanged (main_rbswap_wb.c, main_rbswap_base.c).
 */
#ifndef _MAIN_RBSWAP_H
#define _MAIN_RBSWAP_H

#include <stdbool.h>
#include <body.h>

/* Built for targets with wb_base (plugin.toml); elsewhere the task side does nothing. */
#define MAIN_RBSWAP FENYKL_HAS_WB_BASE

/* P x M: rows 0 and 2 of m exchanged, R' = M row 2, B' = M row 0; row sums are kept. */
static inline void main_rbswap_pxm(int16_t *out, const int16_t *m)
{
	for (unsigned int i = 0; i < 3; i++) {
		out[i] = m[6 + i];
		out[3 + i] = m[3 + i];
		out[6 + i] = m[i];
	}
}

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

/* main_rbswap_wb.c, plugin task */
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
