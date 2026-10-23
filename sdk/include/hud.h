/*
 * Live-view HUD elements (sdk/docs/hud.md): text in the camera's 32 px UI font or in the style of the
 * stock live-view info items, copied into OSD layer 1 from the plugin's cw_updateDisplay hook, so
 * every shown frame contains it. Linked into every plugin. CPU0 only: the task calls init, add,
 * text, hide and tick; the hook calls fenykl_hud_frame and then fw_orig_cw_updateDisplay.
 */
#ifndef _FENYKL_HUD_H
#define _FENYKL_HUD_H

#include <stdbool.h>
#include <fenykl.h>

#define FENYKL_HUD_ELEMS_MAX 8u
#define FENYKL_HUD_TEXT_MAX 12		/* characters per element, '\n' included */
#define FENYKL_HUD_MARGIN 2		/* text inset from the top, and from the side it is aligned to */
#define FENYKL_HUD_LINE_H 36		/* 32 px glyph cell + 2 * margin */
#define FENYKL_HUD_LV_MARGIN 1		/* FENYKL_HUD_LV: text inset from the side it is aligned to */
#define FENYKL_HUD_LV_LINE_H 23		/* FENYKL_HUD_LV: one line, outline included, no margin */
/* '\n' starts a line this much lower; n lines need h = (n - 1) * pitch + LINE_H */
#define FENYKL_HUD_PITCH 32		/* the glyph cell */
#define FENYKL_HUD_LV_PITCH 21		/* outlines of neighbouring lines abut, as in the stock ISO item */
#define FENYKL_HUD_BITMAP_MAX 0x3FFC	/* w * h: fw_malloc refuses more */

#if FENYKL_TARGET_EPL3
#define FENYKL_HUD_CANVAS_W 720
#define FENYKL_HUD_CANVAS_H 480

/* Layer-1 palette indices in live view [camera]. */
#define FENYKL_HUD_CLEAR 0xFF		/* transparent */
#define FENYKL_HUD_BLACK 0x10
#define FENYKL_HUD_TRANSLUCENT 0x11	/* black, alpha 8/15 */
#define FENYKL_HUD_FAINT 0x12		/* black, alpha 4/15 */
/* ink[] for white text: anti-alias greys 0x06, 0x05 and body 0x04 [camera] */
#define FENYKL_HUD_WHITE { 0x06, 0x05, 0x04 }
/* Coloured ink: ramps of body, 2/3 and 1/3 brightness in the live-view L1 palette (stock palette 4,
   matching the camera's L1 copy), like the white one. Yellow is the stock exposure-compensation
   text [camera]. Red 0x39 has no ramp; its darkest shade is the black outline colour. */
#define FENYKL_HUD_GREEN { 0x09, 0x08, 0x07 }
#define FENYKL_HUD_YELLOW { 0x03, 0x02, 0x01 }
#define FENYKL_HUD_RED { 0x10, 0x39, 0x39 }

/* Visible states: bit n = GUI state n of super state 0x02 (SS_REC). */
#define FENYKL_HUD_STATE(n) (1u << (n))
/* Basic, Histogram, Highlight&Shadow */
#define FENYKL_HUD_INFO_SCREENS \
	(FENYKL_HUD_STATE(0x03) | FENYKL_HUD_STATE(0x04) | FENYKL_HUD_STATE(0x05))
#elif FENYKL_TARGET_EM10II
/* The LCD shows the canvas 1:1; the EVF shows its left FENYKL_HUD_EVF_W columns, scaled
   (fenykl_hud_output). */
#define FENYKL_HUD_CANVAS_W 720
#define FENYKL_HUD_CANVAS_H 480
#define FENYKL_HUD_EVF_W 640

/* Layer-1 palette indices: the E-PL3's colours at the same indices in the live-view layer-1
   palette [camera dump] and in 19 of the 59 stock GUI palettes (about every third). Only indices
   < FENYKL_HUD_PAL_ENTRIES reach the display (128-entry hardware palette). */
#define FENYKL_HUD_PAL_ENTRIES 0x80
#define FENYKL_HUD_CLEAR 0x00		/* transparent (alpha 0) */
#define FENYKL_HUD_BLACK 0x10
#define FENYKL_HUD_TRANSLUCENT 0x11	/* black, alpha 0x11/0x1F */
#define FENYKL_HUD_FAINT 0x12		/* black, alpha 8/0x1F */
#define FENYKL_HUD_WHITE { 0x06, 0x05, 0x04 }
#define FENYKL_HUD_GREEN { 0x09, 0x08, 0x07 }
#define FENYKL_HUD_YELLOW { 0x03, 0x02, 0x01 }
/* No fixed red: index 0x39 differs between the GUI palettes. Use fenykl_hud_color. */

/* Visible INFO screens: bit n = value n of the live-view INFO screen (fenykl_hud_info_screen). */
#define FENYKL_HUD_INFO(n) (1u << (n))
#define FENYKL_HUD_INFO_IMAGE 0		/* image only */
#define FENYKL_HUD_INFO_BASIC 1
#define FENYKL_HUD_INFO_CUSTOM1 0x11	/* histogram by default */
#define FENYKL_HUD_INFO_CUSTOM2 0x12	/* level gauge by default */
#define FENYKL_HUD_INFO_SCREENS \
	(FENYKL_HUD_INFO(FENYKL_HUD_INFO_BASIC) | FENYKL_HUD_INFO(FENYKL_HUD_INFO_CUSTOM1))

enum fenykl_hud_output {
	FENYKL_HUD_OUT_NONE,		/* display 0 closed or on another device */
	FENYKL_HUD_OUT_LCD,		/* canvas 720 x 480 visible */
	FENYKL_HUD_OUT_EVF,		/* canvas 640 x 480 visible */
};

/* Output device of display 0 (enum fenykl_hud_output). */
int fenykl_hud_output(void);
/* The live-view INFO screen (FENYKL_HUD_INFO_*), or -1 if its property is not registered. */
int fenykl_hud_info_screen(void);
/* Index < FENYKL_HUD_PAL_ENTRIES of the opaque live layer-1 palette entry nearest to rgb (0xRRGGBB,
   sum of squared differences), or -1 if none is opaque. */
int fenykl_hud_color(uint32_t rgb);
#else
#error "hud: no canvas for this target"
#endif

enum fenykl_hud_align {
	FENYKL_HUD_LEFT,
	FENYKL_HUD_CENTER,
	FENYKL_HUD_RIGHT,
};

/*
 * FENYKL_HUD_LV looks like the stock live-view info items (sdk/docs/hud.md 3): "0"-"9", "+", "-",
 * ":" and "." are the GUI's own outlined images (their white in ink[]), a space is 11 px, any other
 * character is the UI font glyph scaled to 2/3, made bold and given the stock outline (0x10) and
 * shadow (0x11, 0x12), on the images' baseline.
 */
enum fenykl_hud_style {
	FENYKL_HUD_PLAIN,
	FENYKL_HUD_LV,
};

#define FENYKL_HUD_EHANDLE (-1)		/* layer-1 buffer handle is not a DRAM pointer */
#define FENYKL_HUD_EPLANE (-2)		/* unexpected plane or size */
#define FENYKL_HUD_EPDATA (-3)		/* implausible buffer address */
#define FENYKL_HUD_EOVERLAP (-4)	/* the two buffers overlap */
#define FENYKL_HUD_EBPP (-5)		/* layer 1 is not 8 bpp */
#define FENYKL_HUD_ERECT (-6)		/* element rectangle outside the canvas or not word aligned */
#define FENYKL_HUD_EFULL (-7)		/* FENYKL_HUD_ELEMS_MAX elements already added */
#define FENYKL_HUD_ENOMEM (-8)		/* fw_malloc failed */
#define FENYKL_HUD_ENOFONT (-9)		/* no language font loaded yet */
#define FENYKL_HUD_EGLYPH (-10)		/* a glyph or stock image is missing, or the text is too long */
#define FENYKL_HUD_EINIT (-11)		/* fenykl_hud_init has not succeeded */

/* An element: a fixed rectangle the text is rendered into. The caller sets the public fields and
   keeps the struct alive; fenykl_hud_add fills the rest. */
struct fenykl_hud_elem {
	int16_t x, y;		/* canvas, x % 4 == 0 */
	int16_t w, h;		/* w % 4 == 0; h = FENYKL_HUD_LINE_H (FENYKL_HUD_LV_LINE_H) for one line,
				   + FENYKL_HUD_PITCH (FENYKL_HUD_LV_PITCH) per further line */
	uint8_t bg;		/* FENYKL_HUD_CLEAR (only glyph pixels show) or a box colour */
	uint8_t align;		/* enum fenykl_hud_align: where the text sits in the box */
	uint8_t style;		/* enum fenykl_hud_style */
	uint8_t ink[3];		/* glyph codes 1..3 (two anti-alias shades, body), FENYKL_HUD_WHITE */
	/* private */
	uint32_t *bmp[2];	/* w * h bytes each, rendered text */
	volatile uint32_t pub;	/* bits 2.. sequence, bit 1 shown, bit 0 bmp index */
	volatile uint32_t written[2];	/* pub last copied into each layer-1 buffer */
	volatile uint8_t stale;	/* the hook copied while pub changed */
	uint8_t clears;		/* hide: clears left for fenykl_hud_tick */
};

/* What fenykl_hud_init read for one layer-1 buffer (for the plugin's log). */
struct fenykl_hud_buf {
	uint32_t handle;	/* struct osd_vram * */
	uint32_t plane;
	uint16_t width, height;
	uint32_t pdata;
};

struct fenykl_hud_stats {
	uint32_t updates;	/* hook calls */
	uint32_t shows;		/* of those, show_front == 0 */
	uint32_t drawn;		/* element copies into the shown buffer */
	uint32_t hidden;	/* element copies skipped: state not visible or layer 1 masked */
	uint32_t erased;	/* the GUI had painted over an element copied earlier into that buffer */
	uint32_t overrides;	/* the one-layer override chose the shown buffer */
	uint32_t stale;		/* repaints by fenykl_hud_tick after a race with the hook */
	uint32_t cpu1;		/* hook calls on CPU1 (skipped) */
	uint32_t nofont;	/* renders refused: no font */
	uint32_t glyph_miss;	/* renders refused: missing glyph or text too long */
};

/* Checks the layer-1 buffers (buf[2] receives what was read) and sets the states in which elements
   are shown (FENYKL_HUD_STATE bits; E-M10 II: FENYKL_HUD_INFO bits). 0 or FENYKL_HUD_E*. */
int fenykl_hud_init(uint32_t states, struct fenykl_hud_buf buf[2]) FENYKL_CPU0_ONLY("fenykl_hud_init");
/* After fenykl_hud_init: checks e's rectangle and allocates its bitmaps; e is not shown until
   fenykl_hud_text. 0 or FENYKL_HUD_E*. */
int fenykl_hud_add(struct fenykl_hud_elem *e) FENYKL_CPU0_ONLY("fenykl_hud_add");
/* Renders s, publishes it to the hook and paints it into both buffers if visible. 0, or
   FENYKL_HUD_ENOFONT / EGLYPH (the element is unchanged). */
int fenykl_hud_text(struct fenykl_hud_elem *e, const char *s) FENYKL_CPU0_ONLY("fenykl_hud_text");
/* Clears e's spare bitmap to e->bg and calls fn(box, e->w, e->h, arg) to draw into it (row-major
   layer-1 indices, FENYKL_HUD_CLEAR transparent); then publishes and paints it as fenykl_hud_text.
   fn's nonzero return is returned and leaves e unchanged. */
int fenykl_hud_draw(struct fenykl_hud_elem *e,
		    int (*fn)(uint8_t *box, int w, int h, void *arg), void *arg)
	FENYKL_CPU0_ONLY("fenykl_hud_draw");
/* Decodes stock image id (fmt 0x03, 8 bpp layer-1 indices, as images 1095-1098) from the firmware's
   common resource into px, row-major, 0 = transparent; sets *w, *h. 0, or FENYKL_HUD_EGLYPH if the
   image is missing, not fmt 0x03 or larger than max pixels. */
int fenykl_hud_image(uint16_t id, uint8_t *px, int max, int *w, int *h);
/* Unpublishes e and, if visible, clears its rectangle to FENYKL_HUD_CLEAR in both buffers, now
   and at the next two fenykl_hud_tick calls. */
void fenykl_hud_hide(struct fenykl_hud_elem *e) FENYKL_CPU0_ONLY("fenykl_hud_hide");
/* Call every ~100 ms from the task: repaints after a race with the hook, repeats hide clears. */
void fenykl_hud_tick(void) FENYKL_CPU0_ONLY("fenykl_hud_tick");
/* Width of s (its widest line) in pixels in style (enum fenykl_hud_style) with the current font,
   or FENYKL_HUD_ENOFONT / EGLYPH. */
int fenykl_hud_text_width(uint8_t style, const char *s) FENYKL_CPU0_ONLY("fenykl_hud_text_width");
/* Font id of the loaded language (15 for English), 0 while the GUI has not loaded one. */
uint32_t fenykl_hud_font(void);
/* Layer 1 is not masked and the GUI is in one of the states given to fenykl_hud_init. E-M10 II:
   live view with its info display on the LCD or the EVF, on one of the INFO screens given. */
bool fenykl_hud_visible(void);
/* From the plugin's cw_updateDisplay hook, before the original: copies the published elements
   into the buffer about to be shown (show_front == 0 only). Counts and skips CPU1 calls. */
void fenykl_hud_frame(int show_front);
void fenykl_hud_get_stats(struct fenykl_hud_stats *st);

#endif
