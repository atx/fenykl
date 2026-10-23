/* The birddetect6 fd_hw_shutdown hook (sdk/projects/birddetect6/hook.c) in the GDB simulator, with
   fw_hook.S (fd_hw_shutdown at its real address with its real entry bytes, so the trampoline
   relocates the real instructions), ../hooks/sim_start.S and ../hooks/support.S. The face RAM and
   the timer are plain simulator memory set by the test. plugin_main sets sim_exit_code to the
   failure bit mask (0 = pass):
     1  install or remove failed, a call changed a callee-saved register or SP, or the original
        did not run
     2  installed: a live cycle replaced the real candidates by the target's candidate, a call
        without a live cycle left the list alone, a moved target is taken over in one cycle
     4  removed: the list is left alone */
#include <stdbool.h>
#include <fenykl.h>

#include "../../../sdk/projects/birddetect6/birddetect6.h"
#include "../../../sdk/projects/birddetect6/inject.h"
#include "../../../sdk/projects/birddetect6/net.h"

unsigned call_fw_shutdown_caller(void);

volatile uint32_t mock_args[12]; /* the irq/cache mocks record here */
uint32_t sim_exit_code = 99;

volatile uint32_t shutdown_calls; /* the fd_hw_shutdown mock counts here */
/* inject.c's post-processing reads the model (bd6_model.c, not linked); the hook does not use it. */
const int32_t bd6_threshold = 0;
const uint8_t bd6_centroid_k = 0;
const uint8_t bd6_centroid_table[BD6_CENTROID_N] = { 0 };

#define TIMER_DOWN 0x98900074u
#define RAW_LIST 0x60608E34u
#define RAW_COUNT 70u
#define LIVE_CYCLES 0x6034CBFAu
#define CONF_MARK 0x77

struct cand {
	int16_t cx, cy, size, conf, angle, pose, rsv;
};

static volatile struct cand *const raw = (volatile struct cand *)RAW_LIST;
static volatile uint16_t *const live_cycles = (volatile uint16_t *)LIVE_CYCLES;

/* Real candidates at 3 and 20. */
static void fill_raw(void)
{
	for (unsigned int i = 0; i < RAW_COUNT; i++)
		raw[i] = (struct cand){ 0, 0, 0, CONF_MARK, 7, 1, 5 };
	raw[3].cx = 100, raw[3].cy = 200, raw[3].size = 60;
	raw[20].cx = 500, raw[20].cy = 300, raw[20].size = 50;
}

static bool raw_is_filled(void)
{
	for (unsigned int i = 0; i < RAW_COUNT; i++) {
		bool real = i == 3 || i == 20;

		if ((raw[i].size != 0) != real || raw[i].conf != CONF_MARK || raw[i].angle != 7)
			return false;
	}
	return raw[20].size == 50 && raw[20].cx == 500;
}

static bool raw_is_injected(int16_t cx, int16_t cy)
{
	for (unsigned int i = 1; i < RAW_COUNT; i++)
		if (raw[i].size != 0)
			return false;
	return raw[0].cx == cx && raw[0].cy == cy && raw[0].size == BD6_INJ_SIZE &&
	       raw[0].conf == BD6_INJ_CONF && raw[0].angle == 0;
}

/* One fd_hw_shutdown call through a firmware-style caller, after a live cycle if `live`. */
static void call(bool live, int *fail)
{
	uint32_t before = shutdown_calls;

	fill_raw();
	if (live)
		*live_cycles += 1;
	if (call_fw_shutdown_caller() != 0 || shutdown_calls != before + 1)
		*fail |= 1;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int fail = 0;

	(void)load_base;
	(void)image_size;
	memset(&bd6_hook, 0, sizeof(bd6_hook));
	*(volatile uint8_t *)0x60608E20u = 0; /* read by the relocated entry */
	*live_cycles = 0;
	*(volatile uint32_t *)TIMER_DOWN = 0x3FFFFFFFu - 1000000u;
	if (bd6_hook_install() != 0) {
		sim_exit_code = 1;
		return;
	}

	bd6_hook.target_us = BD6_NOW();
	bd6_hook.target = BD6_TGT(300, 200);
	call(true, &fail);
	if (!raw_is_injected(300, 200) || bd6_hook.injected != 1 || bd6_hook.cycles != 1)
		fail |= 2;
	call(false, &fail);
	if (!raw_is_filled() || bd6_hook.other != 1)
		fail |= 2;
	bd6_hook.target = BD6_TGT(500, 100);
	call(true, &fail);
	if (!raw_is_injected(500, 100) || bd6_hook.injected != 2)
		fail |= 2;

	if (fenykl_hook_remove(&fenykl_hook_fd_hw_shutdown) != 0)
		fail |= 1;
	call(true, &fail);
	if (!raw_is_filled() || bd6_hook.injected != 2)
		fail |= 4;
	sim_exit_code = fail;
}
