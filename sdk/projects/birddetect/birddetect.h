/* birddetect: shared by the control task (birddetect.c), the FD hook (hook.c) and the CPU1
   worker (worker.c). */
#ifndef BIRDDETECT_H
#define BIRDDETECT_H

#include <fenykl.h>
#include "inject.h"
#include "log.h"
#include "net.h"

/* timer_now: 1 MHz, both cores. */
#define BD_NOW() FENYKL_LOG_TIMER_NOW()
#define BD_ELAPSED(from, to) FENYKL_LOG_ELAPSED(from, to)

/* Cached alias of a DRAM address (the firmware's addr_to_cached: 0x8/0xA -> 0x4/0x6). */
#define BD_CACHED(p) ((const uint8_t *)(((uint32_t)(p) & 0x3FFFFFFFu) | 0x40000000u))

#define BD_SLICE_US 6000u	/* the worker sleeps a tick after at most this much work (+ a band) */

/* Timed stages of one inference: sampling, then the groups of enum bd_group. */
enum bd_stage {
	BD_ST_SAMPLE,
	BD_ST_STEM,
	BD_ST_L2,
	BD_ST_TRUNK,
	BD_STAGES,
};

/*
 * FENYKL_SHARED, used only through FENYKL_SHARED_PTR. One request at a time: CPU0 writes req_addr
 * and req_t, then req_seq; the worker writes the results, then res_seq = req_seq.
 */
struct bd_shared {
	uint32_t req_seq;
	uint32_t req_addr;		/* uncached FD slot address */
	uint32_t req_t;			/* timer_now when CPU0 saw the publish */
	uint32_t core_id, phase;	/* phase: 1 self-test done, 2 serving, 3 returned */
	uint32_t beats;			/* +1 per fw_OlyTaskSleep(1) */
	uint32_t selftest[BD_PATTERNS];	/* bd_logit_sum of each pattern */
	uint32_t res_seq;
	uint32_t lat_us;		/* publish seen by CPU0 .. CPU1 started */
	uint32_t wall_us;		/* CPU1 start .. results, sleeps included */
	uint32_t slice_max_us;		/* longest work between two sleeps */
	uint32_t us[BD_STAGES];		/* work per stage, sleeps excluded */
	int32_t logit[BD_CELLS];
};
extern struct bd_shared bd_shared;

/* The FD hook (hook.c): the control task writes target_us, then target (inject.h). */
struct bd_hook {
	volatile uint32_t target, target_us;
	volatile uint32_t live_cycles;	/* 0x6034CBFA at the last live cycle */
	struct bd_inj inj;
	volatile uint32_t cycles;	/* live cycles */
	volatile uint32_t other;	/* calls that were no live cycle */
	volatile uint32_t injected;	/* live cycles with our candidate */
	volatile uint32_t cleared;	/* live cycles with an empty list */
};
extern struct bd_hook bd_hook;

int bd_hook_install(void);

#endif
