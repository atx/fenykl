/* USB detection for the E-PL3 (README, Startup and USB boots). */
#include <fenykl.h>

/* Power-on factor of this boot (a key code): ui_key_gio_init 0x6E7121D5 maps the PMU wake register
   0x98E00048 through the table 0x6E914248 into it, in usr_start_tasks before USTART1 starts. */
#define FENYKL_EPL3_POWERON_FACTOR (*(volatile uint32_t *)0x60551630u)
/* Key code "UsbC" (GIO key kind 1): plugging the cable in switched the camera on. */
#define FENYKL_EPL3_POWERON_USB 0x400u
/* GIO key "UsbC": ui_key_get_state returns 0 while the cable is in. */
#define FENYKL_EPL3_KEY_USB 0x400u
/* fat_mounted[0], drive C: 1 once the camera has (re)mounted the card. */
#define FENYKL_EPL3_FAT_MOUNTED (*(volatile uint16_t *)0x6059C978u)

int fenykl_usb_attached(void)
{
	return FENYKL_EPL3_POWERON_FACTOR == FENYKL_EPL3_POWERON_USB;
}

/* The firmware's own test, pcsub_is_usb_connected 0x6EE194E8. */
bool fenykl_usb_cable_present(void)
{
	return fw_ui_key_get_state(FENYKL_EPL3_KEY_USB) == 0;
}

bool fenykl_card_io_allowed(void)
{
	return !fenykl_usb_cable_present() && FENYKL_EPL3_FAT_MOUNTED == 1;
}
