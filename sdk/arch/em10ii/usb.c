/* USB and card state for the E-M10 II (sdk/README.md, Startup and USB boots). */
#include <fenykl.h>

/* GIO key "UsbC": ui_key_get_state returns 0 for an active key (cable in) and 1 for an inactive one
   (0x60180E0A: input bit != active level). Camera: 1 with no cable; the cable-in value is not measured. */
#define FENYKL_EM10II_KEY_USB 0x400u
#define FENYKL_EM10II_MEDIA_MOUNTED 0x100u /* fileif_get_media_status: FAT mounted */
#define FENYKL_EM10II_MEDIA_DCF_MOUNT 0x4000u /* the DCF mount sequence runs */

/* The loader runs a payload only when Usr3 requested the card mount (u16 0x61843F64 == 1), which
   needs power-on factor 12, the power switch: a boot started by the cable never gets here. */
int fenykl_usb_attached(void)
{
	return 0;
}

/* Any value but 1 (0, or 0xFFFF for an unknown code) counts as a cable. */
bool fenykl_usb_cable_present(void)
{
	return fw_ui_key_get_state(FENYKL_EM10II_KEY_USB) != 1;
}

/* Ready rule: FAT mounted and no DCF mount running; card removal reads as status 1. */
bool fenykl_card_io_allowed(void)
{
	if (fenykl_usb_cable_present())
		return false;
	return (fw_fileif_get_media_status(0) &
		(FENYKL_EM10II_MEDIA_MOUNTED | FENYKL_EM10II_MEDIA_DCF_MOUNT)) ==
	       FENYKL_EM10II_MEDIA_MOUNTED;
}
