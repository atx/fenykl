/*
 * HUD library, E-M10 II part (sdk/docs/hud.md): when live view shows the elements, the output device,
 * the INFO screen and colours from the live layer-1 palette. Reads only; GUI task or plugin task.
 */
#include <body.h>
#include "hud_int.h"

#define HUD_LAYER1_HIDDEN 0x10u		/* gpi_hide_mask bit of layer 1 */
#define HUD_DISP_REQ_INFO 0xF001u	/* EVT_REQ_REC_DISP_INFO: the live-view info display */
#define HUD_DEV_LCD 1u			/* disp_device: rear LCD */
#define HUD_DEV_EVF 6u			/* built-in EVF */
#define HUD_PROP_INFO 0x8321u		/* s8 property: the live-view INFO screen */
#define HUD_PAL_ALPHA 3			/* osd_palette entry {R, G, B, alpha >> 3} */
#define HUD_PAL_OPAQUE 0x1Fu

int fenykl_hud_output(void)
{
	uint32_t dev = fenykl_rd32(FW_DISP_DEVICE);

	if (dev == HUD_DEV_LCD)
		return FENYKL_HUD_OUT_LCD;
	if (dev == HUD_DEV_EVF)
		return FENYKL_HUD_OUT_EVF;
	return FENYKL_HUD_OUT_NONE;
}

int fenykl_hud_info_screen(void)
{
	uint32_t obj = fbody_prop_obj(HUD_PROP_INFO);

	if (!fenykl_dram_ptr(obj, 1))
		return -1;
	return (int8_t)fenykl_rd8(obj);
}

bool hud_em10ii_visible(uint32_t states)
{
	if (fenykl_rd16(FW_GPI_HIDE_MASK) & HUD_LAYER1_HIDDEN)
		return false;
	if (!fenykl_gui_live_view() || fenykl_rd32(FW_GUI_DISP_REQ) != HUD_DISP_REQ_INFO)
		return false;
	if (fenykl_hud_output() == FENYKL_HUD_OUT_NONE)
		return false;
	int info = fenykl_hud_info_screen();

	return info >= 0 && info < 32 && (states & FENYKL_HUD_INFO(info));
}

int fenykl_hud_color(uint32_t rgb)
{
	const volatile uint8_t *pal = (const volatile uint8_t *)FW_OSD_PALETTE;
	uint32_t best = 0xFFFFFFFFu;
	int pick = -1;

	for (int i = 0; i < FENYKL_HUD_PAL_ENTRIES; i++) {
		const volatile uint8_t *e = pal + 4 * i;

		if (e[HUD_PAL_ALPHA] < HUD_PAL_OPAQUE)
			continue;
		int dr = e[0] - (int)(rgb >> 16 & 0xFF);
		int dg = e[1] - (int)(rgb >> 8 & 0xFF);
		int db = e[2] - (int)(rgb & 0xFF);
		uint32_t d = (uint32_t)(dr * dr + dg * dg + db * db);

		if (d < best) {
			best = d;
			pick = i;
		}
	}
	return pick;
}
