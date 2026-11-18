/*
 * The example test program sdk/tp/examples/peek.c, built by `fenykl tp build` for slot 2 and placed
 * there by the test (.tpimg), in the GDB simulator: the monitor's header check (devmon_proto.c),
 * .bss cleared as the monitor does, then tp_main called through the entry with a function table
 * whose log records its call. plugin_main sets sim_exit_code to 0 or the first failed check.
 */
#include <fenykl.h>
#include "../../../sdk/lib/devmon_int.h"

uint32_t sim_exit_code = 99;

static const uint32_t word = 0x9234ABCDu;
static const char *log_fmt;
static uint32_t log_calls, log_a[3];

static void t_log(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2)
{
	log_fmt = fmt;
	log_a[0] = a0;
	log_a[1] = a1;
	log_a[2] = a2;
	log_calls++;
}

static void t_sleep(uint32_t ms)
{
	(void)ms;
}

static uint32_t t_timer(void)
{
	return 0;
}

static uint32_t t_rd32(uint32_t addr)
{
	return *(volatile uint32_t *)addr;
}

static void t_wr32(uint32_t addr, uint32_t v)
{
	*(volatile uint32_t *)addr = v;
}

static const struct fenykl_tp_api api = {
	.version = FENYKL_TP_API,
	.size = sizeof(struct fenykl_tp_api),
	.log = t_log,
	.sleep_ms = t_sleep,
	.timer_us = t_timer,
	.rd32 = t_rd32,
	.wr32 = t_wr32,
};

static uint32_t t_run(const struct fenykl_tp_hdr *h)
{
	int (*entry)(const struct fenykl_tp_api *, uint32_t);
	int r;

	for (uint32_t a = h->base + h->size; a < h->base + h->mem; a++)
		*(volatile uint8_t *)a = 0;
	entry = (int (*)(const struct fenykl_tp_api *, uint32_t))h->entry;
	r = entry(&api, (uint32_t)&word);
	if ((uint32_t)r != word)	/* bit 31 set: the whole word comes back */
		return 3;
	if (log_calls != 1 || log_a[0] != (uint32_t)&word || log_a[1] != word || log_a[2] != 1)
		return 4;
	if (log_fmt[0] != 'p' || log_fmt < (const char *)h->base ||
	    log_fmt >= (const char *)h->base + h->size)
		return 5;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	const struct fenykl_tp_hdr *h = (const struct fenykl_tp_hdr *)FDM_SLOT_ADDR(2);
	uint32_t r;

	(void)load_base;
	(void)image_size;
	if (fdm_tp_check(h, 2) || !fdm_tp_check(h, 1)) {
		sim_exit_code = 1;
		return;
	}
	if (h->mem <= h->size) {
		sim_exit_code = 2;
		return;
	}
	/* .bss dirty as after an earlier program, cleared before each run */
	*(volatile uint32_t *)(h->base + h->size) = 0x55;
	r = t_run(h);
	if (!r) {
		log_calls = 0;
		*(volatile uint32_t *)(h->base + h->size) = 0x55;
		r = t_run(h);
		r = r ? 10 + r : 0;
	}
	sim_exit_code = r;
}
