/* USB and card state for the E-PL3 (README, Startup and USB boots). */
#include <fenykl.h>

/* Key code "UsbC" (GIO key kind 1): plugging the cable in switched the camera on. */
#define FENYKL_EPL3_POWERON_USB 0x400u
/* GIO key "UsbC": ui_key_get_state returns 0 while the cable is in. */
#define FENYKL_EPL3_KEY_USB 0x400u

int fenykl_usb_attached(void)
{
	return *(volatile uint32_t *)FW_POWERON_FACTOR == FENYKL_EPL3_POWERON_USB;
}

/* The firmware's own test, pcsub_is_usb_connected 0x6EE194E8. */
bool fenykl_usb_cable_present(void)
{
	return fw_ui_key_get_state(FENYKL_EPL3_KEY_USB) == 0;
}

bool fenykl_card_mounted(void)
{
	return *(volatile uint16_t *)FW_FAT_MOUNTED == 1;
}
