/* Shared between the CPU0 control task (cpu1frame.c) and the CPU1 worker (worker.c). */
#ifndef CPU1FRAME_SHARED_H
#define CPU1FRAME_SHARED_H

#include <fenykl.h>

/* The face-detection image: 640x480 Y8, 5 slots, published uncached. */
#define FD_WIDTH 640u
#define FD_HEIGHT 480u
#define FD_BYTES (FD_WIDTH * FD_HEIGHT)
#define ROW_WORDS (FD_WIDTH / 4)
#define CHUNKS 4 /* the worker sleeps 1 tick after each chunk of FD_HEIGHT / CHUNKS rows */

/* timer_now 0x6E606482: free-running 30-bit counter, 1 MHz. */
#define TIMER_NOW() (0x3FFFFFFFu - *(volatile uint32_t *)0x98900074u)
#define ELAPSED(from, to) (((to) - (from)) & 0x3FFFFFFFu)

/* Cached alias of a DRAM address, as the firmware's addr_to_cached (0x8/0xA -> 0x4/0x6). */
#define CACHED(p) ((const volatile uint32_t *)(((uint32_t)(p) & 0x3FFFFFFFu) | 0x40000000u))

/* FENYKL_SHARED, accessed only through FENYKL_SHARED_PTR. One request at a time: CPU0 writes req_addr,
   then req_seq; the worker writes the results, then res_seq = req_seq. */
struct shared {
	uint32_t req_seq, req_addr;   /* CPU0 -> CPU1: uncached address of a published FD slot */
	uint32_t phase;               /* 1 started, 2 hot loop timed, 3 returned */
	uint32_t core_id;             /* FENYKL_CORE_ID() on the worker: must be 1 */
	uint32_t beats;               /* +1 per fw_OlyTaskSleep(1) of the worker */
	uint32_t chunk_max;           /* longest timed chunk (us): time between two sleeps */
	uint32_t hot_us;              /* the sum loop over cache-resident data, FD_BYTES in all */
	uint32_t res_seq;             /* req_seq of the results below */
	uint32_t sum_u, us_u;         /* byte sum and time (us) through the uncached alias */
	uint32_t sum_c, us_c;         /* the same through the cached alias, D-cache purged per chunk */
};
extern struct shared shared;

#endif
