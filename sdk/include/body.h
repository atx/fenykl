/*
 * Camera body state for plugins that name no firmware symbol (projects/main): the GUI, the lens,
 * the battery, the firmware's AF statistics, the display and OSD buffers and the img_exec pass
 * layout. The API is the same on every target; each target implements it in its own
 * arch/<camera>/fenykl_body.h (static inline, so a call costs what the firmware read costs).
 * Every group but the GUI is a capability (target.toml): a target without it has no declarations,
 * and plugin.toml [capabilities.<name>] builds the plugin files that use it only where it exists.
 * The shot, img_exec and AF grid groups mirror the E-PL3's encodings (buffer index ^ 1, hide mask
 * bits, IMG_CTRL offsets, HPF channel layout): expect to reshape them with a second implementation.
 */
#ifndef _FENYKL_BODY_H
#define _FENYKL_BODY_H

#include <stdbool.h>
#include <fenykl.h>

/* --- GUI, every target; CPU0 --- */

/* The GUI shows a live-view screen (E-PL3: super state SS_REC). */
static inline bool fenykl_gui_live_view(void);
/* The GUI shows single playback. */
static inline bool fenykl_gui_playback(void);

/* --- lens: lens and exposure readings; CPU0 --- */
#if FENYKL_HAS_LENS
/* An electronic lens is attached. */
static inline bool fenykl_lens_attached(void);
/* Object (focus) distance in mm, 0xFFFFFFFF = infinity; meaningless without a lens. */
static inline uint32_t fenykl_lens_focus_mm(void);
/* Focal length in integer mm of the live lens state (the firmware's default without a lens). */
static inline uint32_t fenykl_lens_focal_mm(void);
/* Shooting Av, APEX x 256; 0 while unset (also f/1.0). */
static inline uint32_t fenykl_lens_av(void);
/* Live iris Av, APEX x 256 (for logs: not checked against the shooting Av). */
static inline uint32_t fenykl_lens_iris_av(void);
#endif

/* --- batt: battery voltage and power source; CPU0 task --- */
#if FENYKL_HAS_BATT
#define FENYKL_BATT_EBUSY (-1)		/* the battery check held the A/D too long: no read */
#define FENYKL_BATT_ESTANDBY (-2)	/* the stock check is off (standby): no read */

/* Battery voltage in the target's A/D counts into *v, read as the firmware's own checks do: 0 or
   FENYKL_BATT_E*. Blocks for a few ms. */
static inline int fenykl_batt_read(uint32_t *v);
/* Power source word, for logs: it changes when the source does. */
static inline uint32_t fenykl_power_src(void);
/* src (fenykl_power_src) is a battery, not an AC adapter or DC coupler. */
static inline bool fenykl_power_src_batt(uint32_t src);
#endif

/* --- afc: also the firmware's own contrast client and AF settings; CPU0 --- */
#if FENYKL_HAS_AFC
#define FENYKL_AF_AREA_NONE (-1)

/* The firmware client's statistics grid: channels HPF1, HPF2, HPF3, HPFV, chan_words u32 apart. */
struct fenykl_af_grid {
	const volatile uint32_t *buf;	/* DMA buffer, uncached */
	uint32_t chan_words;
	uint8_t cols, rows;
	uint8_t pattern;		/* grid pattern */
	uint16_t cut3;			/* HPF3 cut-off: differs between framing and scan */
};

/* Manual focus is set. */
static inline bool fenykl_af_mf(void);
/* The shutter button is half or fully pressed. */
static inline bool fenykl_af_half(void);
/* Grid area of the selected AF target, FENYKL_AF_AREA_NONE without one. */
static inline int fenykl_af_target_area(void);
/* The AF area mode is "all targets". */
static inline bool fenykl_af_all_targets(void);
/* The firmware client's grid into *g; false while it has none (MF, or freed). Values unchecked. */
static inline bool fenykl_af_grid(struct fenykl_af_grid *g);
#endif

/* --- shot: display, OSD layers and the lent RECFILE buffer; CPU0 task --- */
#if FENYKL_HAS_SHOT
/*
 * Also from fenykl_body.h: FENYKL_DISP_LV_PITCH, FENYKL_DISP_LV_BYTES (the live-view buffers),
 * FENYKL_OSD_LAYERS, FENYKL_OSD_PLANE_W, FENYKL_OSD_PLANE_H (an OSD buffer) and, per layer,
 * initializers FENYKL_OSD_BPP, FENYKL_OSD_PAL_ENTRIES and FENYKL_OSD_PAL_FIRST (its entries in
 * fenykl_osd_palettes(), FENYKL_OSD_PAL_TOTAL in all), FENYKL_OSD_STACK (layers bottom to top).
 */

/* a is a word-aligned DRAM address whose next bytes lie below the firmware image. */
static inline bool fenykl_dram(uint32_t a, uint32_t bytes);
/* Cached alias of a DRAM address. */
static inline uint32_t fenykl_cached(uint32_t a);
/* The live-view buffer the display shows (any alias). */
static inline uint32_t fenykl_disp_lv(void);
/* Where buffer lv is placed on the display; false (*x, *y untouched) if it is not a live-view
   display buffer. */
static inline bool fenykl_disp_lv_pos(uint32_t lv, uint32_t *x, uint32_t *y);
/* Display controller registers, for logs: img1 x and y (start << 16 | size), OSD mix order. */
static inline uint32_t fenykl_disp_img1_x(void);
static inline uint32_t fenykl_disp_img1_y(void);
static inline uint32_t fenykl_disp_mix(void);
/* Bits per pixel the GUI set for layer l. */
static inline unsigned int fenykl_osd_bpp(unsigned int l);
/* Buffer index the GUI draws into; between frames the shown one is this ^ 1. */
static inline uint32_t fenykl_osd_draw_buf(void);
/* Hidden layers: bit 4 * l for layer l. */
static inline uint32_t fenykl_osd_hide_mask(void);
/* Pixels of buffer b of layer l, 0 if its descriptor is not a FENYKL_OSD_PLANE_W x _H plane. */
static inline uint32_t fenykl_osd_buf(unsigned int l, unsigned int b);
/* The buffer of layer l the display shows (any alias). */
static inline uint32_t fenykl_osd_shown(unsigned int l);
/* All layer palettes: {Y, Cb, Cr, alpha} per entry. */
static inline const uint8_t *fenykl_osd_palettes(void);
/* RECFILE has a free run for bytes: fenykl_recfile_alloc would not block. */
static inline bool fenykl_recfile_fits(uint32_t bytes);
/* Lends bytes of RECFILE, uncached; blocks until free. fenykl_recfile_free gives it back. */
static inline uint32_t fenykl_recfile_alloc(uint32_t bytes);
static inline void fenykl_recfile_free(uint32_t buf);
#endif

/* --- img_exec: the layout an img_exec hook reads; any core --- */
#if FENYKL_HAS_IMG_EXEC
/*
 * Also from fenykl_body.h, offsets into IMG_CTRL (img_exec's argument) and IMG_PARAM:
 * FENYKL_IMG_CTRL_FLAG (u32), FENYKL_IMG_CTRL_PARAM (IMG_PARAM *), FENYKL_IMG_CTRL_MODE (u8 mode),
 * FENYKL_IMG_FLAG_ENGINE2 (flag bit: engine 2), FENYKL_IMG_PARAM_MAT (s16 colour matrix, row major,
 * 0x80 = 1.0) and FENYKL_IMG_MAT_N (its entries).
 */

/* The pass class of mode: 0 and 1 apply the colour matrix, 2 and 3 do not. */
static inline unsigned int fenykl_img_mode_class(unsigned int mode);
#endif

#include "fenykl_body.h"

#endif
