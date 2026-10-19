/*
 * birddetect3: the fd_hw_shutdown hook (birddetect's override, camera-verified, without its slew
 * limit). TSK_FACE_FD (CPU0) calls fd_hw_shutdown once per live FD cycle after the last hardware
 * pass, right before face_fd_postprocess merges the raw candidates, and once per still FD. A call
 * is a live cycle when the live-only counter 0x6034CBFA moved since the last one. Then the hook
 * zeroes cx/cy/size of all 70 raw candidates (real faces are not wanted) and, while a fresh target
 * is published, writes one candidate at index 0 on the detected point (inject.c).
 *
 * No firmware calls besides the original: only RAM the face task itself uses, from inside it.
 */
#include "birddetect3.h"

#define BD3_RAW_LIST 0x60608E34u	/* raw candidates, 70 x struct bd3_cand */
#define BD3_RAW_COUNT 70u
#define BD3_LIVE_CYCLES 0x6034CBFAu	/* u16, +1 per completed live pass set */

/* One raw candidate as face_fd_collect leaves it. */
struct bd3_cand {
	int16_t cx, cy, size, conf, angle, pose, rsv;
};

struct bd3_hook bd3_hook;

static void bd3_on_hw_shutdown(void)
{
	struct bd3_hook *h = &bd3_hook;
	volatile struct bd3_cand *raw = (volatile struct bd3_cand *)BD3_RAW_LIST;
	uint16_t cycles = *(const volatile uint16_t *)BD3_LIVE_CYCLES;
	uint32_t target, age;
	int16_t x, y;

	fw_orig_fd_hw_shutdown();
	if (cycles == (uint16_t)h->live_cycles) {
		h->other++;
		return;
	}
	h->live_cycles = cycles;
	h->cycles++;
	target = h->target;
	age = BD3_ELAPSED(h->target_us, BD3_NOW());
	for (unsigned int i = 0; i < BD3_RAW_COUNT; i++) {
		raw[i].cx = 0;
		raw[i].cy = 0;
		raw[i].size = 0;
	}
	if (!bd3_inj_pos(target, age, &x, &y)) {
		h->cleared++;
		return;
	}
	raw[0].cx = x;
	raw[0].cy = y;
	raw[0].size = BD3_INJ_SIZE;
	raw[0].conf = BD3_INJ_CONF;
	raw[0].angle = 0;
	raw[0].pose = 0;
	raw[0].rsv = 0;
	h->injected++;
}
FENYKL_HOOK(fd_hw_shutdown, bd3_on_hw_shutdown);

int bd3_hook_install(void)
{
	return fenykl_hook_install(&fenykl_hook_fd_hw_shutdown);
}
