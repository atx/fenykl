/* birddetect6: shared by the control task (birddetect6.c), the FD hook (hook.c) and the CPU1
   worker (worker.c). */
#ifndef BIRDDETECT6_H
#define BIRDDETECT6_H

#include <fenykl.h>
#include "inject.h"
#include "log.h"
#include "net.h"

/* timer_now: 1 MHz, both cores. */
#define BD6_NOW() FENYKL_TIMER_NOW()
#define BD6_ELAPSED(from, to) FENYKL_TIMER_ELAPSED(from, to)

/* Cached alias of a DRAM address (the firmware's addr_to_cached: 0x8/0xA -> 0x4/0x6). */
#define BD6_CACHED(p) ((const uint8_t *)(((uint32_t)(p) & 0x3FFFFFFFu) | 0x40000000u))

#define BD6_SLICE_US 6000u	/* the worker sleeps a tick after at most this much work (+ a band) */

/*
 * FENYKL_SHARED, used only through FENYKL_SHARED_PTR. One request at a time: CPU0 writes req_addr
 * and req_t, then req_seq; the worker writes the results, then res_seq = req_seq.
 */
struct bd6_shared {
	uint32_t req_seq;
	uint32_t req_addr;		/* the 96x72 YUYV resize output */
	uint32_t req_t;			/* timer_now when the resize finished */
	uint32_t core_id, phase;	/* phase: 1 self-test done, 2 serving, 3 returned */
	uint32_t beats;			/* +1 per fw_OlyTaskSleep(1) */
	uint32_t selftest[BD6_PATTERNS];	/* bd6_logit_sum of each pattern */
	struct bd6_det selftest_det[BD6_PATTERNS];	/* bd6_post of each pattern on CPU1 */
	uint32_t selftest_us;		/* work per self-test inference, sleeps excluded */
	uint32_t res_seq;
	uint32_t lat_us;		/* resize done .. CPU1 started */
	uint32_t wall_us;		/* CPU1 start .. results, sleeps included */
	uint32_t slice_max_us;		/* longest work between two sleeps */
	uint32_t us[BD6_STAGES];	/* work per stage, sleeps excluded */
	int32_t logit[BD6_LOGITS];
};
extern struct bd6_shared bd6_shared;

/* The FD hook (hook.c): the control task writes target_us, then target (inject.h). */
struct bd6_hook {
	volatile uint32_t target, target_us;
	volatile uint32_t live_cycles;	/* 0x6034CBFA at the last live cycle */
	volatile uint32_t cycles;	/* live cycles */
	volatile uint32_t other;	/* calls that were no live cycle */
	volatile uint32_t injected;	/* live cycles with our candidate */
	volatile uint32_t cleared;	/* live cycles with an empty list */
};
extern struct bd6_hook bd6_hook;

int bd6_hook_install(void);

/* bd6_shot.c: the Fn screenshot task; fw_OlyTaskCreate's result, -1 without a stack. */
int bd6_shot_start(void);

#endif
