/*
 * Card gate (sdk/README.md, Startup and USB boots): closed while the cable is in or the card is not
 * mounted, and for the rest of the boot once a cable was seen. CPU0 task context.
 */
#include <fenykl.h>

struct fcard_state {
	bool usb_seen;		/* cable seen this boot: the gate stays closed until power-off */
};

static struct fcard_state fcard;

/* After a USB session fat_mounted[0] reads 1 again, but the camera's FAT state is the one from
   before it: a write then lost the cluster chain of a file the PC had written. */
bool fenykl_card_io_allowed(void)
{
	if (fcard.usb_seen)
		return false;
	if (fenykl_usb_attached() || fenykl_usb_cable_present()) {
		fcard.usb_seen = true;
		return false;
	}
	return fenykl_card_mounted();
}

bool fenykl_card_usb_seen(void)
{
	return fcard.usb_seen;
}
