/* E-M10 II boot probe: logs the firmware state the SDK relies on (cores, boot gate, loaded sections,
   card and USB state, tick and timer rate) into C:\FENYKL.LOG and, 30000 ticks later, C:\FENYKL.TCK.
   Reads firmware state only; deletes and writes its own two files while the card gate is open. */
#include <fenykl.h>

#define STACK_SIZE 0x4000u /* FENYKL task stack above FENYKL_REGION_END, 0xDEADBEEF-filled by the kernel */

static unsigned length(const char *s)
{
	unsigned n = 0;
	while (s[n])
		n++;
	return n;
}

static unsigned stack_untouched(void)
{
	const uint32_t *p = (const uint32_t *)FENYKL_REGION_END;
	while (p < (const uint32_t *)(FENYKL_REGION_END + STACK_SIZE) && *p == 0xDEADBEEFu)
		p++;
	return (uint32_t)p - FENYKL_REGION_END;
}

#define PROBE_TIMER_MASK 0x3FFFFFFFu /* 0x98900074 counts down from 0x3FFFFFFF */
#define PROBE_SHORT_TICKS 1000
#define PROBE_LONG_TICKS 30000
#define PROBE_READY_POLLS 300 /* x 100 ticks */
#define PROBE_KEY_USB 0x400u

static struct {
	char text[1024]; /* fixed formats with at most 3 words: < 64 bytes a line, 11 lines a file */
	unsigned int len;
} probe;

static void put(const char *fmt, uint32_t a, uint32_t b, uint32_t c)
{
	fw_sprintf(probe.text + probe.len, fmt, a, b, c);
	probe.len += length(probe.text + probe.len);
}

static uint32_t timer_now(void)
{
	return *(volatile uint32_t *)FW_TIMER_NOW;
}

static void put_card_state(const char *when)
{
	put("%s: fat_mounted %04x media status %08x", (uint32_t)when,
	    *(volatile uint16_t *)FW_FAT_MOUNTED, fw_fileif_get_media_status(0));
	put(" usb key %x\r\n", fw_ui_key_get_state(PROBE_KEY_USB), 0, 0);
}

/* Polls for the card gate; returns the polls needed or -1. */
static int wait_card(void)
{
	for (int i = 0; i < PROBE_READY_POLLS; i++) {
		if (fenykl_card_io_allowed())
			return i;
		fw_OlyTaskSleep(100);
	}
	return -1;
}

/* Replaces C:\NAME with probe.text; the rpc_fremove result goes into the text first. */
static void write_file(const char *path)
{
	void *fp;

	put("rpc_fremove %s -> %d\r\nend\r\n", (uint32_t)path, fw_rpc_fremove(path), 0);
	fp = fw_rpc_fopen(path, "w");
	if (!fp) {
		fw_dbg_puts("[bootprobe] rpc_fopen failed\n");
		return;
	}
	fw_rpc_fwrite(probe.text, 1, probe.len, fp);
	fw_rpc_fclose(fp);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	uint32_t t0, t1;
	int sleep_ret, polls;

	put("fenykl bootprobe E-M10II\r\nload base %08x image size %u\r\n", load_base, image_size, 0);
	put("core %u power-on factor %08x gate %04x\r\n", FENYKL_CORE_ID(),
	    *(volatile uint32_t *)FW_POWERON_FACTOR, *(volatile uint16_t *)FW_BOOT_CARD_MOUNT_REQUESTED);
	put("loaded mask %02x\r\n", *(volatile uint8_t *)FW_SECTION_LOADED, 0, 0);
	put_card_state("entry");
	t0 = timer_now();
	sleep_ret = fw_OlyTaskSleep(PROBE_SHORT_TICKS);
	t1 = timer_now();
	put("OlyTaskSleep(%u) -> %d: timer delta %u\r\n", PROBE_SHORT_TICKS, sleep_ret,
	    (t0 - t1) & PROBE_TIMER_MASK);
	polls = wait_card();
	if (polls < 0) {
		fw_dbg_puts("[bootprobe] card gate stayed closed\n");
		return;
	}
	put("card gate open after %d polls\r\n", polls, 0, 0);
	put_card_state("write");
	write_file("C:\\FENYKL.LOG");

	/* FENYKL.TCK, PROBE_LONG_TICKS later: the two FAT times differ by 30 s if a tick is 1 ms. */
	probe.len = 0;
	t0 = timer_now();
	sleep_ret = fw_OlyTaskSleep(PROBE_LONG_TICKS);
	t1 = timer_now();
	put("OlyTaskSleep(%u) -> %d: timer delta %u\r\n", PROBE_LONG_TICKS, sleep_ret,
	    (t0 - t1) & PROBE_TIMER_MASK);
	put("loaded mask %02x\r\n", *(volatile uint8_t *)FW_SECTION_LOADED, 0, 0);
	polls = wait_card();
	if (polls < 0) {
		fw_dbg_puts("[bootprobe] card gate stayed closed\n");
		return;
	}
	put_card_state("write");
	/* After the FENYKL.LOG write: covers the file RPC path. */
	put("stack untouched %u of %u\r\n", stack_untouched(), STACK_SIZE, 0);
	write_file("C:\\FENYKL.TCK");
}
