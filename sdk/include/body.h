/*
 * Camera body state for plugins that name no firmware symbol (projects/main): the GUI, the lens,
 * the battery, the firmware's AF statistics, the display and OSD buffers, the img_exec pass
 * layout and the peaking passes and overlay. The API is the same on every target; each target
 * implements it in its own arch/<camera>/fenykl_body.h (static inline, so a call costs what the
 * firmware read costs).
 * Every group but the GUI is a capability (target.toml): a target without it has no declarations,
 * and plugin.toml [capabilities.<name>] builds the plugin files that use it only where it exists.
 * The shot, img_exec and peak groups mirror the E-PL3's encodings (buffer index ^ 1, hide mask
 * bits, IMG_CTRL offsets, display layers): expect to reshape them with a second implementation.
 */
#ifndef _FENYKL_BODY_H
#define _FENYKL_BODY_H

#include <stdbool.h>
#include <fenykl.h>

/* --- GUI, every target; CPU0 --- */

/* GUI super state << 8 | state, in the target's own encoding (for logs and target code). */
static inline uint16_t fenykl_gui_state(void);
#define FENYKL_GUI_SUPER(gui) ((gui) >> 8)
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
   FENYKL_BATT_E*. May block for a few ms (E-PL3: waits for an A/D request). */
static inline int fenykl_batt_read(uint32_t *v);
/* Power source word, for logs: it changes when the source does. */
static inline uint32_t fenykl_power_src(void);
/* src (fenykl_power_src) is a battery, not an AC adapter or DC coupler. */
static inline bool fenykl_power_src_batt(uint32_t src);
#endif

/* --- afc: the firmware's own contrast client, AF settings and the contrast driver; CPU0 --- */
#if FENYKL_HAS_AFC
#define FENYKL_AF_AREA_NONE (-1)

/* The firmware client's statistics grid: channels HPF1, HPF2, HPF3 (E-PL3: and HPFV), chan_words
   u32 apart. */
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

/*
 * The contrast driver's client protocol, for the own client (lib/afc.c). Also from fenykl_body.h:
 * FENYKL_CNT_SET_SIZE (contrast_setting bytes), FENYKL_CNT_SET_INFO (its leading parameter part;
 * the driver writes the rest), FENYKL_CNT_SET_NUMBER (offset of its u16 frame count: 0xFFFF
 * endless, 0 = detach at the next frame), FENYKL_CNT_REQ_SIZE (cam_request object bytes),
 * FENYKL_CNT_EV_ST, FENYKL_CNT_EV_CP, FENYKL_CNT_EV_ERR (frame callback events: frame start, data
 * complete, error; 0 if the body has none), FENYKL_CNT_DMA_WORDS and FENYKL_CNT_DMA_ALIGN (the
 * client's DMA buffer), FENYKL_CNT_DMA_STATS (word offset in it of the statistics a plugin reads)
 * and FENYKL_CNT_STATS_CHANNELS (their HPF channels, from HPF1).
 */
/* fcs's contrast_setting while fcs runs a grid the own client may copy (stills framing through AF,
   no half-press); its grid pattern, cols and rows into *pat, *cols, *rows. 0 otherwise. */
static inline uint32_t fenykl_cnt_fcs_setting(uint8_t *pat, uint8_t *cols, uint8_t *rows);
/* Readies set, a copy of fcs's, for an attach writing into dma (uncached): endless, every update
   bit fcs uses. */
static inline void fenykl_cnt_setting_prep(uint8_t *set, uint32_t dma);
/* Fills the zeroed request req for an attach of set with the callbacks; arg where the body has
   a callback argument. */
static inline void fenykl_cnt_req_prep(void *req, uint32_t set, uint32_t frame_cb, uint32_t done_cb,
				       uint32_t arg);
/* The driver has a client linked other than the one of set. */
static inline bool fenykl_cnt_foreign(uint32_t set);
/* The GUI shows a stills live-view screen the own client may run on. */
static inline bool fenykl_cnt_lv(void);
/* Movie mode, or another live-view mode the own client must avoid (true without take_focus). */
static inline bool fenykl_cnt_movie(void);
/* The driver runs a debug mode (false on a body without one). */
static inline bool fenykl_cnt_drv_dbg(void);
#endif

/* --- shot: display, OSD layers and the lent RECFILE buffer; CPU0 task --- */
#if FENYKL_HAS_SHOT
/*
 * Also from fenykl_body.h: FENYKL_DISP_W, FENYKL_DISP_H (the panel: the OSD canvas it shows),
 * FENYKL_DISP_SQUARE_W (its width at square pixels), FENYKL_DISP_LV_PITCH, FENYKL_DISP_LV_BYTES
 * (the live-view buffers), FENYKL_OSD_LAYERS, FENYKL_OSD_PLANE_W, FENYKL_OSD_PLANE_H (an OSD
 * buffer) and, per layer,
 * initializers FENYKL_OSD_BPP, FENYKL_OSD_PAL_ENTRIES and FENYKL_OSD_PAL_FIRST (its entries in
 * fenykl_osd_palettes(), FENYKL_OSD_PAL_TOTAL in all), FENYKL_OSD_STACK (layers bottom to top),
 * FENYKL_FRAMES_PITCH, FENYKL_FRAMES_H and FENYKL_FRAMES_PAL (the face/AF frame layer).
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
/* The firmware's face/AF frame layer (FENYKL_FRAMES_PITCH x FENYKL_FRAMES_H palette indices): the
   buffer the display shows (any alias, 0 = none), its source size w x h scaled to to_w x to_h from
   the canvas origin, and its FENYKL_FRAMES_PAL colours {R, G, B, pad}. */
static inline uint32_t fenykl_frames_shown(void);
static inline void fenykl_frames_geom(uint16_t *w, uint16_t *h, uint16_t *to_w, uint16_t *to_h);
static inline const uint8_t *fenykl_frames_palette(void);
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

/* --- peak: a line image pass after the live-view pass and an alpha overlay layer; CPU0 --- */
#if FENYKL_HAS_PEAK
/*
 * Needs the shot and img_exec groups. Also from fenykl_body.h: FENYKL_PEAK_CTRL_SIZE (bytes of
 * an img_exec argument copy), FENYKL_PEAK_LN_W, FENYKL_PEAK_LN_H (line image: 1 B/px, row pitch,
 * rows), FENYKL_PEAK_LUM_BYTES (the pass's statistics scratch), FENYKL_PEAK_LUT_N (u16 entries of
 * a Y LUT), FENYKL_PEAK_SHADOW_N (words a shadow patch saves), FENYKL_OVL_RED (an opaque red
 * overlay pixel, 4 B/px) and struct fenykl_ovl (the overlay's firmware request and saved
 * registers, kept by the caller).
 */
struct fenykl_ovl;

struct fenykl_peak_layout {
	uint32_t out1_bytes;		/* the pass's display output buffer */
	uint16_t ln_w, ln_h;		/* line image area from 0,0 */
	uint16_t x, y, w, h;		/* live-view area in the display output buffer */
};

/* ctrl (img_exec's argument) is a still live-view pass a line pass may follow. */
static inline bool fenykl_peak_lv_pass(const void *ctrl);
/* Live-view passes a and b have the same output layout. */
static inline bool fenykl_peak_same_layout(const void *a, const void *b);
/* The layout of live-view pass ctrl into *l; NULL, or why a line pass cannot follow it. */
static inline const char *fenykl_peak_layout(const void *ctrl, struct fenykl_peak_layout *l);
/* Turns copy (FENYKL_PEAK_CTRL_SIZE bytes of a live-view pass) into its line pass: display output
   to out1 (l->out1_bytes), line image to line, statistics to lum (uncached), other outputs off. */
static inline void fenykl_peak_line_ctrl(void *copy, const struct fenykl_peak_layout *l,
					 uint32_t out1, uint32_t line, uint32_t lum);
/* Display output buffer of pass ctrl, physical. */
static inline uint32_t fenykl_peak_out1(const void *ctrl);
/* In img_hw_start of a line pass, around the original: the Y LUTs to lut_zero (all 0), the edge
   coring to coring, edge gain and limits to their maximum; the stock words into saved. */
static inline void fenykl_peak_shadow_patch(uint32_t *saved, uint32_t coring,
					    const uint16_t *lut_zero);
static inline void fenykl_peak_shadow_restore(const uint32_t *saved);

/* The display is the rear LCD. */
static inline bool fenykl_disp_rear(void);
/* The live view is magnified (magnify, MF assist). */
static inline bool fenykl_lv_magnified(void);
/* The GUI shows a still live-view screen (no menu, panel or movie screen). */
static inline bool fenykl_gui_still_lv(void);
/* Physical address of the live-view buffer the display latched for the frame shown. */
static inline uint32_t fenykl_disp_lv_latched(void);

/* The firmware shows nothing on the overlay layer. */
static inline bool fenykl_ovl_free(void);
/* Shows buf (w x h of a FENYKL_PEAK_LN_W pitch, 4 B/px) scaled to to_w x to_h at to_x, to_y of the
   display, opaque; the stock layer setup into o. The firmware's status, 0 = shown; on any other
   the caller calls fenykl_ovl_hide. */
static inline unsigned int fenykl_ovl_show(struct fenykl_ovl *o, uint32_t buf, uint16_t w,
					   uint16_t h, uint16_t to_x, uint16_t to_y, uint16_t to_w,
					   uint16_t to_h);
/* Points the shown overlay at buf; interrupts masked (fenykl_irq_save, fenykl.h). */
static inline void fenykl_ovl_flip(uint32_t buf);
/* Physical address of the overlay buffer the display latched. */
static inline uint32_t fenykl_ovl_latched(void);
/* Takes the overlay down and puts the stock layer setup back: the firmware's status, 0 = ok. */
static inline unsigned int fenykl_ovl_hide(struct fenykl_ovl *o);

#endif

#include "fenykl_body.h"

#endif
