/*
 * Internals of the HUD library shared by hud.c and the camera's part (hud_<camera>.c): what differs
 * per body in the layer-1 handles, the DRAM alias rules and the stock live-view image ids.
 */
#ifndef _FENYKL_HUD_INT_H
#define _FENYKL_HUD_INT_H

#include <hud.h>

#if FENYKL_TARGET_EPL3
#define HUD_SS_REC 0x02			/* GUI super state of every live-view screen */
#define HUD_DRAM_OFF(a) ((a) & 0x0FFFFFFFu)	/* the same for the cached and uncached alias */
/* Handles lie in the firmware's RAM below the code image. */
#define HUD_HANDLE_BAD(p) ((p) < FW_DRAM_START || (p) >= FW_IMAGE_START)
#define HUD_SIZE_BAD(w, h) ((w) != 896 || (h) != 600)

/* Image font 12 of the GUI (char -> image table 0x6F1B7908, the remaining-time digits): '-' at
   HUD_LV_DIGITS, '1'..'9' after it, then '0', ':', '+'; and '.' from image font 16 (0x6F1B8908),
   which is drawn in the same style. */
#define HUD_LV_DIGITS 1126u
#define HUD_LV_DOT 1933u

struct hud_osd_vram {
	uint32_t plane;
	uint16_t width, height;
	uint32_t pdata;
};
#elif FENYKL_TARGET_EM10II
/* DRAM is 512 MiB: cached 0x6/0x7, uncached 0xA/0xB (to_uncached 0x6007F686). */
#define HUD_DRAM_OFF(a) ((a) & 0x1FFFFFFFu)
/* Handles lie in the GUI's bss above the code image (0x619ADF88..). */
#define HUD_HANDLE_BAD(p) ((p) < FW_IMAGE_END || (p) >= FW_DRAM_END)
/* w and h are the layer window, at most the buffer (gpi_layer_vram). */
#define HUD_SIZE_BAD(w, h) ((w) > 896 || (h) > 600)

/* Image font 14 (data.gui_image_table), in the E-PL3's order and style; '.' from image font 25. */
#define HUD_LV_DIGITS 1120u
#define HUD_LV_DOT 1871u

/* gpi_layer_vram's descriptor (osd_vram_register 0x60E15829) */
struct hud_osd_vram {
	uint32_t pdata;
	uint32_t plane;
	uint16_t width, height;
	uint16_t x, y;
};

/* hud_em10ii.c: live view on the LCD or the EVF in one of `states` (FENYKL_HUD_INFO bits). */
bool hud_em10ii_visible(uint32_t states);
#endif

#endif
