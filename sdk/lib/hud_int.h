/*
 * Internals of the HUD library shared by hud.c (elements, rendering) and the camera's part
 * (hud_<camera>.c: visibility, init, tick and how the elements reach the screen): the shared state,
 * the camera part's entry points, the fmt 0x03 encoder (hud_rle.c) and what differs per body.
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

#endif

#define HUD_PUB_SHOWN 2u
#define HUD_PUB_INDEX 1u

/* hud.c's state; the camera part reads the elements and sets states and ready. */
struct hud_state {
	uint32_t states;		/* fenykl_hud_init's visible states */
	bool ready;			/* fenykl_hud_init succeeded */
	struct fenykl_hud_elem *elem[FENYKL_HUD_ELEMS_MAX];
	volatile unsigned int count;
	uint32_t nofont, glyph_miss;	/* refused renders */
};

extern struct hud_state hud;

/* hud.c: a new pub for e, the sequence advanced. */
void hud_publish(struct fenykl_hud_elem *e, uint32_t flags);

/* The camera part: hud_add allocates e's bitmaps and prepares element `index` (0 or
   FENYKL_HUD_E*, nothing kept); hud_show takes e's spare bitmap idx, just rendered. */
int hud_add(struct fenykl_hud_elem *e, unsigned int index);
void hud_show(struct fenykl_hud_elem *e, uint32_t idx);

/* hud_rle.c: words of the largest fmt 0x03 bitmap of w x h pixels (the header and one code per
   pixel, six per u64), and of one all of a single colour (runs of 4 within each row). */
#define HUD_RLE_WORDS(w, h) (1 + 2 * (((w) * (h) + 5) / 6))
#define HUD_RLE_FLAT_WORDS(w, h) (1 + 2 * (((h) * (((w) + 3) / 4) + 5) / 6))

/*
 * Encodes px (w x h, row-major palette indices, 0 = transparent) into out as the stock bitmap:
 * u32 header {h-1:12, w-1:12, fmt 0x03:8}, then six 10-bit codes (colour << 2 | run-1, run 1..4,
 * runs end at the row end) per little-endian u64, the unused codes 0. Returns the stream size in
 * bytes as the stock image records give it (header + the bytes up to the last code), or -1 if
 * max_words is too small.
 */
int hud_rle_encode(const uint8_t *px, int w, int h, uint32_t *out, int max_words);

#endif
