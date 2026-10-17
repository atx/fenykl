/*
 * birddetect: the fd_hw_shutdown hook (the same override with a synthetic box is camera-verified).
 * TSK_FACE_FD (CPU0) calls fd_hw_shutdown once per live FD cycle after the last hardware pass,
 * right before face_fd_postprocess merges the raw candidates, and once per still FD. A call is a
 * live cycle when the live-only counter 0x6034CBFA moved since the last one. Then the hook zeroes
 * cx/cy/size of all 70 raw candidates (real faces are not wanted) and, while a fresh target is
 * published, writes one candidate at index 0 that glides towards it (inject.c).
 *
 * No firmware calls besides the original: only RAM the face task itself uses, from inside it.
 */
#include "birddetect.h"

#define BD_RAW_LIST 0x60608E34u		/* raw candidates, 70 x struct bd_cand */
#define BD_RAW_COUNT 70u
#define BD_LIVE_CYCLES 0x6034CBFAu	/* u16, +1 per completed live pass set */

/* One raw candidate as face_fd_collect leaves it. */
struct bd_cand {
	int16_t cx, cy, size, conf, angle, pose, rsv;
};

struct bd_hook bd_hook;

static void bd_on_hw_shutdown(void)
{
	struct bd_hook *h = &bd_hook;
	volatile struct bd_cand *raw = (volatile struct bd_cand *)BD_RAW_LIST;
	uint16_t cycles = *(const volatile uint16_t *)BD_LIVE_CYCLES;
	uint32_t target, age;

	fw_orig_fd_hw_shutdown();
	if (cycles == (uint16_t)h->live_cycles) {
		h->other++;
		return;
	}
	h->live_cycles = cycles;
	h->cycles++;
	target = h->target;
	age = BD_ELAPSED(h->target_us, BD_NOW());
	for (unsigned int i = 0; i < BD_RAW_COUNT; i++) {
		raw[i].cx = 0;
		raw[i].cy = 0;
		raw[i].size = 0;
	}
	if (!bd_inj_step(&h->inj, target, age)) {
		h->cleared++;
		return;
	}
	raw[0].cx = h->inj.cx;
	raw[0].cy = h->inj.cy;
	raw[0].size = BD_INJ_SIZE;
	raw[0].conf = BD_INJ_CONF;
	raw[0].angle = 0;
	raw[0].pose = 0;
	raw[0].rsv = 0;
	h->injected++;
}
FENYKL_HOOK(fd_hw_shutdown, bd_on_hw_shutdown);

int bd_hook_install(void)
{
	return fenykl_hook_install(&fenykl_hook_fd_hw_shutdown);
}
