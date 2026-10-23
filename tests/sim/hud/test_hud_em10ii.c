/*
 * E-M10 II part of the HUD library (sdk/lib/hud_em10ii.c, the E-M10 II checks of hud.c) in the GDB
 * simulator: the visibility predicate over GUI state, display request, output device, layer mask and
 * the INFO screen property (registry and override table), the output device, the colour pick from
 * the live layer-1 palette, and fenykl_hud_init's checks of the E-M10 II handles. plugin_main sets
 * sim_exit_code to the mask of failed checks (0 = pass).
 */
#include "sim_hud.h"

enum {
	SIM_FAIL_VISIBLE = 1,	/* the predicate over GUI state, request, device and mask */
	SIM_FAIL_OUTPUT = 2,	/* device 1 LCD, 6 EVF, anything else none */
	SIM_FAIL_INFO = 4,	/* INFO screen from the registry or the override table */
	SIM_FAIL_COLOR = 8,	/* nearest opaque entry below 0x80 */
	SIM_FAIL_INIT = 16,	/* window sizes up to the buffer, handles above the code image */
};

#define SIM_PROP_OVR (FW_PROP_OVERRIDE + 8u * (0x8321u >> 4) + 4u)

const void *const sim_images[1];
static volatile int8_t sim_info_ovr;

/* fw.S's glyph mock forwards here; no text is rendered in this program. */
uint32_t c_gpi_get_glyph(void)
{
	return 0;
}

static bool sim_visible_in(uint32_t gui, uint32_t req, uint32_t dev, uint16_t hide)
{
	SIM_U8(SIM_SUPER) = (uint8_t)(gui >> 8);
	SIM_U8(SIM_STATE) = (uint8_t)gui;
	SIM_U32(FW_GUI_DISP_REQ) = req;
	SIM_U32(FW_DISP_DEVICE) = dev;
	SIM_U16(SIM_HIDE_MASK) = hide;
	bool v = fenykl_hud_visible();

	SIM_U8(SIM_SUPER) = 0x01;
	SIM_U8(SIM_STATE) = 0x08;
	SIM_U32(FW_GUI_DISP_REQ) = 0xF001;
	SIM_U32(FW_DISP_DEVICE) = 1;
	SIM_U16(SIM_HIDE_MASK) = 0;
	return v;
}

static int sim_check_visible(void)
{
	static const int8_t shown[] = { 1, 0x11 }, hidden[] = { 0, 0x12, 2, -1 };

	if (!sim_visible_in(0x0108, 0xF001, 1, 0) || !sim_visible_in(0x0108, 0xF001, 6, 0))
		return 0;
	if (sim_visible_in(0x1101, 0xF001, 1, 0) || sim_visible_in(0x0403, 0xF001, 1, 0) ||
	    sim_visible_in(0x0108, 0xF003, 1, 0) || sim_visible_in(0x0108, 0xF001, 0, 0) ||
	    sim_visible_in(0x0108, 0xF001, 5, 0) || sim_visible_in(0x0108, 0xF001, 1, 0x10))
		return 0;
	if (!sim_visible_in(0x0108, 0xF001, 1, 0x101))
		return 0;
	for (unsigned int i = 0; i < sizeof(shown); i++) {
		sim_info = shown[i];
		if (!fenykl_hud_visible())
			return 0;
	}
	for (unsigned int i = 0; i < sizeof(hidden); i++) {
		sim_info = hidden[i];
		if (fenykl_hud_visible())
			return 0;
	}
	sim_info = SIM_INFO_BASIC;
	return 1;
}

static int sim_check_output(void)
{
	static const uint32_t dev[] = { 0, 1, 2, 3, 4, 5, 6, 7, 0x12 };
	static const int8_t out[] = { 0, 1, 0, 0, 0, 0, 2, 0, 0 };
	int ok = 1;

	for (unsigned int i = 0; i < sizeof(dev) / sizeof(dev[0]); i++) {
		SIM_U32(FW_DISP_DEVICE) = dev[i];
		ok &= fenykl_hud_output() == out[i];
	}
	SIM_U32(FW_DISP_DEVICE) = 1;
	return ok && FENYKL_HUD_OUT_LCD == 1 && FENYKL_HUD_OUT_EVF == 2;
}

static int sim_check_info(void)
{
	int ok = fenykl_hud_info_screen() == SIM_INFO_BASIC;

	/* the override table wins while valid and its entry is set */
	sim_info_ovr = SIM_INFO_IMAGE;
	SIM_U32(SIM_PROP_OVR) = (uint32_t)&sim_info_ovr;
	ok &= fenykl_hud_info_screen() == SIM_INFO_BASIC;
	SIM_U16(FW_PROP_OVERRIDE_VALID) = 1;
	ok &= fenykl_hud_info_screen() == SIM_INFO_IMAGE && !fenykl_hud_visible();
	SIM_U32(SIM_PROP_OVR) = 0;
	ok &= fenykl_hud_info_screen() == SIM_INFO_BASIC;
	SIM_U16(FW_PROP_OVERRIDE_VALID) = 0;
	/* an uncached object, an unregistered property */
	SIM_U32(SIM_PROP_INFO) = (uint32_t)FENYKL_UNCACHED(&sim_info);
	ok &= fenykl_hud_info_screen() == SIM_INFO_BASIC;
	SIM_U32(SIM_PROP_INFO) = 0;
	ok &= fenykl_hud_info_screen() == -1 && !fenykl_hud_visible();
	SIM_U32(SIM_PROP_INFO) = (uint32_t)&sim_info;
	return ok;
}

static void sim_pal(unsigned int i, uint8_t r, uint8_t g, uint8_t b, uint8_t a)
{
	uint32_t e = FW_OSD_PALETTE + 4 * i;

	SIM_U8(e) = r;
	SIM_U8(e + 1) = g;
	SIM_U8(e + 2) = b;
	SIM_U8(e + 3) = a;
}

static int sim_check_color(void)
{
	for (unsigned int i = 0; i < 256; i++)
		sim_pal(i, 0, 0, 0, 0);
	if (fenykl_hud_color(0xFF0000) != -1)
		return 0;
	sim_pal(0x20, 250, 10, 10, 0x1F);
	sim_pal(0x21, 255, 0, 0, 0x11);		/* not opaque */
	sim_pal(0x7F, 200, 200, 200, 0x1F);
	sim_pal(0x90, 255, 0, 0, 0x1F);		/* beyond the hardware palette */
	return fenykl_hud_color(0xFF0000) == 0x20 && fenykl_hud_color(0xFFFFFF) == 0x7F &&
	       fenykl_hud_color(0x000000) == 0x20;
}

/* E-M10 II handles: w/h are the layer window, at most 896 x 600; the handle lies above the image. */
static int sim_check_init(void)
{
	struct fenykl_hud_buf buf[2];
	uint32_t v0 = SIM_U32(SIM_VRAM_TABLE + 8);
	int ok;

	SIM_U16(v0 + 8) = 720;
	SIM_U16(v0 + 10) = 480;
	ok = fenykl_hud_init(FENYKL_HUD_INFO_SCREENS, buf) == 0 && buf[0].pdata == SIM_BUF0 &&
	     buf[1].pdata == SIM_BUF1 && buf[0].width == 720;
	SIM_U16(v0 + 8) = 900;
	ok &= fenykl_hud_init(FENYKL_HUD_INFO_SCREENS, buf) == FENYKL_HUD_EPLANE;
	SIM_U16(v0 + 8) = SIM_PITCH;
	SIM_U16(v0 + 10) = 600;
	SIM_U32(SIM_VRAM_TABLE + 8) = FW_IMAGE_END - 0x100;
	ok &= fenykl_hud_init(FENYKL_HUD_INFO_SCREENS, buf) == FENYKL_HUD_EHANDLE;
	SIM_U32(SIM_VRAM_TABLE + 8) = v0;
	SIM_U32(v0) = 0x40000000u;		/* not DRAM */
	ok &= fenykl_hud_init(FENYKL_HUD_INFO_SCREENS, buf) == FENYKL_HUD_EPDATA;
	SIM_U32(v0) = SIM_BUF0;
	return ok && fenykl_hud_init(FENYKL_HUD_INFO_SCREENS, buf) == 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	int fail = 0;

	sim_setup_memory();
	if (!sim_check_init())
		fail |= SIM_FAIL_INIT;
	if (!sim_check_visible())
		fail |= SIM_FAIL_VISIBLE;
	if (!sim_check_output())
		fail |= SIM_FAIL_OUTPUT;
	if (!sim_check_info())
		fail |= SIM_FAIL_INFO;
	if (!sim_check_color())
		fail |= SIM_FAIL_COLOR;
	sim_exit_code = (uint32_t)fail;
}
