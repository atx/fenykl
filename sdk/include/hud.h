/*
 * Live-view HUD elements (sdk/docs/hud.md): text in the camera's 32 px UI font or in the style of the
 * stock live-view info items. E-PL3: copied into OSD layer 1 from the plugin's cw_updateDisplay
 * hook, so every shown frame contains it. E-M10 II: each element is a stock image widget on the
 * live-view info panel, drawn by the GUI. Linked into every plugin. CPU0 only: the task calls init,
 * add, text, hide and tick; on the E-PL3 the hook calls fenykl_hud_frame and then
 * fw_orig_cw_updateDisplay.
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
#define FENYKL_HUD_BITMAP_MAX 0x3FFC	/* w * h: fw_malloc refuses more. E-M10 II: the elements
					   share a fixed pool (sdk/lib/hud_em10ii.c) */

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
/* The live-view info panel's 640 x 480 layout, where the stock items' rects lie: the EVF's screen
   positions. The LCD (720 wide) shows x < 165 at the same place and moves the stock items further
   right (right column +80); where it puts an element there is not verified. */
#define FENYKL_HUD_CANVAS_W 640
#define FENYKL_HUD_CANVAS_H 480

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
	FENYKL_HUD_OUT_LCD,		/* rear LCD */
	FENYKL_HUD_OUT_EVF,		/* built-in EVF */
};

/* Output device of display 0 (enum fenykl_hud_output). */
int fenykl_hud_output(void);
/* The live-view INFO screen (FENYKL_HUD_INFO_*), or -1 if its property is not registered. */
int fenykl_hud_info_screen(void);
/* An s8 property's value (the INFO screen is 0x8321), or -1 if it is not registered. */
int fenykl_hud_prop(unsigned int prop);

/* The GUI's panel, for the plugin's log. */
struct fenykl_hud_panel {
	int32_t id;		/* panel id on screen */
	int32_t lv_id;		/* panel id the live-view info viewer shows */
	uint32_t obj;		/* panel object, 0 if unreadable */
	uint32_t vtable;	/* its first word, 0 if obj is no DRAM pointer */
};

/* Reads the GUI's panel ids and object (reads only). */
void fenykl_hud_panel(struct fenykl_hud_panel *p);
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
#define FENYKL_HUD_ENOMEM (-8)		/* fw_malloc failed; E-M10 II: the pool is used up */
#define FENYKL_HUD_ENOFONT (-9)		/* no language font loaded yet */
#define FENYKL_HUD_EGLYPH (-10)		/* a glyph or stock image is missing, or the text is too long */
#define FENYKL_HUD_EINIT (-11)		/* fenykl_hud_init has not succeeded */
#define FENYKL_HUD_ESTOCK (-12)		/* E-M10 II: the ISO item's widget record is not the stock one */
#define FENYKL_HUD_ESLOT (-13)		/* E-M10 II: a GUI slot was not replaced (all restored) */

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
#if FENYKL_TARGET_EPL3
	volatile uint32_t written[2];	/* pub last copied into each layer-1 buffer while visible */
	volatile uint32_t sum[2];	/* checksum of the box last copied into each buffer */
	volatile uint8_t ours[2];	/* the buffer holds that box until wiped, cleared or repainted */
	volatile uint8_t stale;	/* the hook copied while pub changed */
	uint8_t clears;		/* hide: clears left for fenykl_hud_tick */
#endif
};

#if FENYKL_TARGET_EPL3
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
	uint32_t wiped;		/* boxes cleared by fenykl_hud_tick after a hidden stretch */
	uint32_t covered;	/* boxes it left alone: the GUI had painted over them */
	uint32_t cpu1;		/* hook calls on CPU1 (skipped) */
	uint32_t nofont;	/* renders refused: no font */
	uint32_t glyph_miss;	/* renders refused: missing glyph or text too long */
};

/* Checks the layer-1 buffers (buf[2] receives what was read) and sets the states in which elements
   are shown (FENYKL_HUD_STATE bits). 0 or FENYKL_HUD_E*. */
int fenykl_hud_init(uint32_t states, struct fenykl_hud_buf buf[2]) FENYKL_CPU0_ONLY("fenykl_hud_init");
/* From the plugin's cw_updateDisplay hook, before the original: copies the published elements
   into the buffer about to be shown (show_front == 0 only). Counts and skips CPU1 calls. */
void fenykl_hud_frame(int show_front);
#else
struct fenykl_hud_stats {
	uint32_t creates;	/* info panel creates (GUI task) */
	uint32_t made;		/* widgets made in them */
	uint32_t failed;	/* widgets the factory or the panel's child list refused */
	uint32_t destroys;	/* destroys of the panel holding the widgets */
	uint32_t heap_free;	/* GUI heap free bytes of that panel's pool after the last create */
	uint32_t posts;		/* update events posted by fenykl_hud_tick */
	uint32_t post_fail;	/* of those, refused by the GUI queue */
	uint32_t unstuck;	/* of those, repeated after 5 ticks without the event arriving */
	uint32_t messages;	/* update events the GUI task received */
	uint32_t swaps;		/* image handles set on a widget */
	uint32_t cpu1;		/* slot calls on CPU1 (passed to the stock function) */
	uint32_t nofont;	/* renders refused: no font */
	uint32_t glyph_miss;	/* renders refused: missing glyph or text too long */
};

/* Checks the stock record the widgets are made from, sets the INFO screens in which elements are
   shown (FENYKL_HUD_INFO bits) and replaces the info panel's create and destroy and the GUI app's
   message slots, all or nothing. Elements get their widgets when the GUI next builds the panel.
   0 or FENYKL_HUD_E*. */
int fenykl_hud_init(uint32_t states) FENYKL_CPU0_ONLY("fenykl_hud_init");
#endif
/* After fenykl_hud_init: checks e's rectangle and allocates its bitmaps; e is not shown until
   fenykl_hud_text. 0 or FENYKL_HUD_E*. */
int fenykl_hud_add(struct fenykl_hud_elem *e) FENYKL_CPU0_ONLY("fenykl_hud_add");
/* Renders s and publishes it: E-PL3 to the hook, painted into both buffers if visible; E-M10 II
   to the next fenykl_hud_tick. 0, or FENYKL_HUD_ENOFONT / EGLYPH (the element is unchanged). */
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
/* Unpublishes e. E-PL3: if visible, clears its rectangle to FENYKL_HUD_CLEAR in both buffers, now
   and at the next two fenykl_hud_tick calls. E-M10 II: the next tick shows the empty image. */
void fenykl_hud_hide(struct fenykl_hud_elem *e) FENYKL_CPU0_ONLY("fenykl_hud_hide");
/* Call every ~100 ms from the task. E-PL3: repaints after a race with the hook, repeats hide
   clears, clears the boxes left on the canvas from ~0.5 s (5 calls) hidden on, and while visible
   repaints an element whose buffers do not hold what it publishes. E-M10 II: encodes the changed
   elements and posts one GUI event for all that show other than they should (content while
   visible, else the empty image). */
void fenykl_hud_tick(void) FENYKL_CPU0_ONLY("fenykl_hud_tick");
/* Width of s (its widest line) in pixels in style (enum fenykl_hud_style) with the current font,
   or FENYKL_HUD_ENOFONT / EGLYPH. */
int fenykl_hud_text_width(uint8_t style, const char *s) FENYKL_CPU0_ONLY("fenykl_hud_text_width");
/* Font id of the loaded language (15 for English), 0 while the GUI has not loaded one. */
uint32_t fenykl_hud_font(void);
/* Layer 1 is not masked and the GUI is in one of the states given to fenykl_hud_init. E-M10 II:
   live view with its info display on the LCD or the EVF, on one of the INFO screens given. */
bool fenykl_hud_visible(void);
void fenykl_hud_get_stats(struct fenykl_hud_stats *st);

#endif
