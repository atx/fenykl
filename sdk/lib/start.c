#include <fenykl.h>

extern char fenykl_bss_start[], fenykl_bss_end[];

void fenykl_start(uint32_t load_base, uint32_t image_size);

void fenykl_start(uint32_t load_base, uint32_t image_size)
{
	for (char *p = fenykl_bss_start; p < fenykl_bss_end; p++)
		*p = 0;
	/* The PC may mount the card: no file I/O, hooks or tasks on a USB boot. */
	if (!FENYKL_RUN_ON_USB && fenykl_usb_attached()) {
		fw_dbg_puts("[fenykl] USB boot, plugin skipped\n");
		return;
	}
	plugin_main(load_base, image_size);
}
