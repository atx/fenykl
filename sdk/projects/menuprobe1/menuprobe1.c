/* E-M10 II menu probe 1: why the Eye-Fi row of Custom menu tab K is grayed. Condition 113 of the
   mode-check rule of setting 0x193 (slot mode_cond_eyefi) becomes a pass-through: it calls the stock
   condition, logs its result, the Eye-Fi status, the other seven conditions of the rule and the
   mode-check arguments, and returns the stock result unchanged. The slot is restored after
   MP1_RUN_TICKS. Data write only; card writes only through the log library. */
#include <fenykl.h>
#include <log.h>

#define MP1_RUN_TICKS 300000	/* ~5 min with the slot replaced */
#define MP1_POLL_TICKS 1000
#define MP1_LOG_CALLS 32	/* calls logged in full; the rest are counted */
#define MP1_ARG(off) (*(volatile uint8_t *)(FW_MODE_CHECK_ARGS + (off)))
#define MP1_ARG16(off) (*(volatile uint16_t *)(FW_MODE_CHECK_ARGS + (off)))

/* The other conditions of the 0x193 rule (list 0x60B000EB: 47 48 52 56 96 98 113 123). */
static const uint8_t mp1_conds[7] = { 47, 48, 52, 56, 96, 98, 123 };

static struct {
	volatile uint32_t calls;	/* condition 113 calls, all cores */
	volatile uint32_t calls_other;	/* of them not on CPU0 (only counted) */
	volatile uint32_t blocked;	/* stock result non-zero */
} mp1;

static FENYKL_LOG_MODULE(mp1_mod, "mprobe1", FENYKL_LOG_INFO);

static unsigned int mp1_cond(unsigned int n)
{
	return fw_call_mode_cond(((const uint32_t *)FW_MODE_COND_TABLE)[n]);
}

/* eyefi_status of the existing device, -1 while there is none (the getter would create it). */
static int mp1_eyefi(void)
{
	void *dev = *(void *volatile *)FW_EYEFI_DEV;

	return dev ? (int)fw_eyefi_status(dev) : -1;
}

/* Runs inside mode_check in the caller's task: records only, no file I/O. */
static unsigned int mp1_cond113(void)
{
	unsigned int r = fw_orig_mode_cond_eyefi();
	unsigned int c[7];
	uint32_t n = ++mp1.calls;

	if (r)
		mp1.blocked++;
	if (FENYKL_CORE_ID()) {
		mp1.calls_other++;
		return r;
	}
	if (n > MP1_LOG_CALLS)
		return r;
	for (unsigned int i = 0; i < 7; i++)
		c[i] = mp1_cond(mp1_conds[i]);
	fenykl_log_info(&mp1_mod, "c113 #%u -> %x, eyefi_status %d, id %x", n, r, mp1_eyefi(),
			MP1_ARG16(0));
	fenykl_log_info(&mp1_mod, "c47 %x c48 %x c52 %x c56 %x", c[0], c[1], c[2], c[3]);
	fenykl_log_info(&mp1_mod, "c96 %x c98 %x c123 %x, core %u", c[4], c[5], c[6],
			FENYKL_CORE_ID());
	fenykl_log_info(&mp1_mod, "args %02x %02x %02x, a6 %x", MP1_ARG(2), MP1_ARG(3), MP1_ARG(4),
			MP1_ARG16(6));
	return r;
}
FENYKL_CALLBACK(mode_cond, mp1_cond113);

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	uint32_t seen = 0;
	int ret;

	fenykl_log_info(&mp1_mod, "menuprobe1: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	/* sec4 (condition table, conditions) is loaded: the loader starts the payload only after sec4 and sec5. */
	ret = FENYKL_SLOT_REPLACE(mode_cond_eyefi, mp1_cond113);
	fenykl_log_info(&mp1_mod, "slot %08x replaced -> %d (word %08x)", fenykl_slot_mode_cond_eyefi.address,
			ret, *(volatile uint32_t *)fenykl_slot_mode_cond_eyefi.address);
	if (ret)
		return;
	for (uint32_t t = 0; t < MP1_RUN_TICKS; t += MP1_POLL_TICKS) {
		fw_OlyTaskSleep(MP1_POLL_TICKS);
		if (mp1.calls == seen)
			continue;
		seen = mp1.calls;
		fenykl_log_info(&mp1_mod, "t %u s: calls %u, blocked %u, not CPU0 %u", t / 1000u, seen,
				mp1.blocked, mp1.calls_other);
	}
	ret = fenykl_slot_restore(&fenykl_slot_mode_cond_eyefi);
	fenykl_log_info(&mp1_mod, "slot restored -> %d (word %08x), calls %u, blocked %u", ret,
			*(volatile uint32_t *)fenykl_slot_mode_cond_eyefi.address, mp1.calls, mp1.blocked);
}
