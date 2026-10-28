/*
 * main: option "Peaking": focus peaking over the still live view (main_peak.c).
 */
#ifndef _MAIN_PEAK_H
#define _MAIN_PEAK_H

#include <stdint.h>
#include <body.h>

/* Built for targets with the peak capability only (plugin.toml); elsewhere it does nothing. */
#if FENYKL_HAS_PEAK
/* img_exec hook (main_img_hook.c), after the R/B swap: r is the pass's result, t_start the timer
   at the hook's entry; returns r. Any task, any core. */
unsigned int main_peak_after(void *ctrl, unsigned int r, uint32_t t_start);

/* plugin task: after the img_exec hook is installed */
int main_peak_start(void);
void main_peak_set_level(unsigned int level);
void main_peak_status(void);
#else
static inline unsigned int main_peak_after(void *ctrl, unsigned int r, uint32_t t_start)
{
	(void)ctrl;
	(void)t_start;
	return r;
}

static inline int main_peak_start(void)
{
	return 0;
}

static inline void main_peak_set_level(unsigned int level)
{
	(void)level;
}

static inline void main_peak_status(void)
{
}
#endif

#endif
