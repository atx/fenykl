/*
 * Camera body state for plugins that name no firmware symbol (projects/main): the GUI, the lens,
 * the battery, the firmware's AF statistics, the display and OSD buffers and the colour-matrix
 * bases. The API is the same on every
 * target; each target implements it in its own arch/<camera>/fenykl_body.h (static inline, so a
 * call costs what the firmware read costs).
 * Every group but the GUI is a capability (target.toml): a target without it has no declarations,
 * and plugin.toml [capabilities.<name>] builds the plugin files that use it only where it exists.
 */
#ifndef _FENYKL_BODY_H
#define _FENYKL_BODY_H

#include <stdbool.h>
#include <fenykl.h>

/* --- GUI, every target; CPU0 --- */

/* GUI super state << 8 | state, in the target's own encoding (for logs and target code). */
static inline uint16_t fenykl_gui_state(void);
#define FENYKL_GUI_SUPER(gui) ((gui) >> 8)
/* The GUI shows a live-view screen. */
static inline bool fenykl_gui_live_view(void);
/* The GUI shows single playback. */
static inline bool fenykl_gui_playback(void);
/* The GUI shows the main menu (the plugin settings tab included). */
static inline bool fenykl_gui_menu(void);

/* --- lens: lens and exposure readings; CPU0 --- */
#if FENYKL_HAS_LENS
/* An electronic lens is attached. */
static inline bool fenykl_lens_attached(void);
/* Object (focus) distance in mm, 0xFFFFFFFF = infinity; meaningless without a lens. */
static inline uint32_t fenykl_lens_focus_mm(void);
/* Focal length in integer mm of the live lens state; 0 without a lens. */
static inline uint32_t fenykl_lens_focal_mm(void);
/* Shooting Av, APEX x 256; 0 while unset (also f/1.0). */
static inline uint32_t fenykl_lens_av(void);
/* Live iris word, for logs only: of unknown scale. */
static inline uint32_t fenykl_lens_iris_av(void);
#endif

/* --- batt: battery voltage and power source; CPU0 task --- */
#if FENYKL_HAS_BATT
#define FENYKL_BATT_EBUSY (-1)		/* the battery check held the A/D too long: no read */
#define FENYKL_BATT_ESTANDBY (-2)	/* the stock check is off (standby): no read */

/* Battery voltage in the target's A/D counts into *v, read as the firmware's own checks do: 0 or
   FENYKL_BATT_E*. */
static inline int fenykl_batt_read(uint32_t *v);
/* Power source word, for logs: it changes when the source does. */
static inline uint32_t fenykl_power_src(void);
/* src (fenykl_power_src) is a battery, not an AC adapter or DC coupler. */
static inline bool fenykl_power_src_batt(uint32_t src);
#endif

/* --- afc: the firmware's own contrast client, AF settings and the contrast driver; CPU0 --- */
#if FENYKL_HAS_AFC
#define FENYKL_AF_AREA_NONE (-1)

/* The firmware client's statistics grid: channels HPF1, HPF2, HPF3, chan_words
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

/* --- shot: display, OSD layers and a buffer lent for the screenshot; CPU0 task --- */
#if FENYKL_HAS_SHOT
/*
 * Also from fenykl_body.h: FENYKL_DISP_W_MAX, FENYKL_DISP_H_MAX (the largest canvas),
 * FENYKL_OSD_LAYERS, FENYKL_OSD_PLANE_W, FENYKL_OSD_PLANE_H (an OSD buffer) and, per layer,
 * initializers FENYKL_OSD_BPP, FENYKL_OSD_PAL_ENTRIES and FENYKL_OSD_PAL_FIRST (its entries in
 * fenykl_osd_palettes(), FENYKL_OSD_PAL_TOTAL in all); FENYKL_OSD_PAL_YCC (1: palette entries are
 * {Y, Cb, Cr, alpha}, 0: {R, G, B, alpha}), FENYKL_OSD_ALPHA_MAX (the opaque palette alpha),
 * FENYKL_OSD_SHADOW_LAGS (1: after the GUI's flip the display keeps showing the old buffer until
 * its commit), FENYKL_OSD_STACK (layers bottom to top), FENYKL_FRAMES_PITCH, FENYKL_FRAMES_H and
 * FENYKL_FRAMES_PAL (the face/AF frame layer); for the screenshot FENYKL_SHOT_STRIP_ROWS and
 * FENYKL_SHOT_STRIP_LENT (rows per written strip; 1 = the strip is at the end of the lent buffer,
 * 0 = in .bss) and FENYKL_SHOT_REGS (struct fenykl_shot_reg initializers: the registers each
 * attempt logs at DEBUG and a raw dump records).
 */

/* What the panel shows. */
struct fenykl_disp_geom {
	uint16_t w, h;			/* the canvas: panel pixels */
	uint16_t square_w;		/* canvas width at square pixels: the file's */
	uint16_t osd_w, osd_h;		/* visible OSD region from the plane origin, scaled to w x h */
	uint16_t osd_alpha[3];		/* global alpha per OSD layer, 0x100 = opaque */
	uint32_t lv_pitch, lv_bytes;	/* the live-view buffers */
};

/* The camera state the screenshot's start and abort rules read (fenykl_shot_gate). */
struct fenykl_shot_cond {
	bool ready;			/* fenykl_disp_ready(): needed to start */
	bool movie;			/* movie mode, or no shooting state */
	bool playback;			/* the GUI in playback */
	bool still_shared;		/* stills go to a pool over the lent memory */
	uint32_t mode;			/* changes with the display output */
	int buf_free;			/* free blocks of the lent pool, for the log */
	uint32_t pool_used;		/* blocks of the lent pool in use, a mask: a start needs none */
	uint32_t other_used;		/* pools over the lent memory in use, a mask of the body's list */
};

/* A register for the raw dump's sidecar. */
struct fenykl_shot_reg {
	const char *name;
	uint32_t addr;
	uint8_t bytes;			/* 1, 2 or 4 */
};

/* a is a word-aligned DRAM address whose next bytes lie in the firmware's RAM (any alias). */
static inline bool fenykl_dram(uint32_t a, uint32_t bytes);
/* Cached alias of a DRAM address. */
static inline uint32_t fenykl_cached(uint32_t a);
/* The display's registers are live (not switching between output devices). */
static inline bool fenykl_disp_ready(void);
/* What the panel shows into *g; false on an output the screenshot does not support. */
static inline bool fenykl_disp_geom(struct fenykl_disp_geom *g);
/* The live-view buffer the display shows (any alias), 0 = none. */
static inline uint32_t fenykl_disp_lv(void);
/* Where buffer lv is placed on the display; false (*x, *y untouched) if it is not a live-view
   display buffer. */
static inline bool fenykl_disp_lv_pos(uint32_t lv, uint32_t *x, uint32_t *y);
/* Display controller registers, for logs: img1 x and y (start << 16 | size), OSD mix order. */
static inline uint32_t fenykl_disp_img1_x(void);
static inline uint32_t fenykl_disp_img1_y(void);
static inline uint32_t fenykl_disp_mix(void);
/* Bits per pixel of layer l's buffers. */
static inline unsigned int fenykl_osd_bpp(unsigned int l);
/* Buffer index the GUI draws into; between frames the shown one is this ^ 1. */
static inline uint32_t fenykl_osd_draw_buf(void);
/* Hidden layers: bit 4 * l for layer l. */
static inline uint32_t fenykl_osd_hide_mask(void);
/* Pixels of buffer b of layer l, 0 if its descriptor is not a FENYKL_OSD_PLANE_W x _H plane. */
static inline uint32_t fenykl_osd_buf(unsigned int l, unsigned int b);
/* The buffer of layer l the display shows (any alias). */
static inline uint32_t fenykl_osd_shown(unsigned int l);
/* All layer palettes, 4 bytes per entry (FENYKL_OSD_PAL_YCC). */
static inline const uint8_t *fenykl_osd_palettes(void);
/* The firmware's face/AF frame layer (FENYKL_FRAMES_PITCH x FENYKL_FRAMES_H palette indices): the
   buffer the display shows (any alias, 0 = none), its source size w x h scaled to to_w x to_h from
   the canvas origin, and its FENYKL_FRAMES_PAL colours {R, G, B, pad}. */
static inline uint32_t fenykl_frames_shown(void);
static inline void fenykl_frames_geom(uint16_t *w, uint16_t *h, uint16_t *to_w, uint16_t *to_h);
static inline const uint8_t *fenykl_frames_palette(void);
/* The state the screenshot's gate reads, into *c. */
static inline void fenykl_shot_cond(struct fenykl_shot_cond *c);
/* Lends bytes of firmware memory, uncached, without blocking (PRIM block 0); 0 = none.
   fenykl_shot_buf_free(*handle) gives it back: the firmware's status, 0 = freed. */
static inline uint32_t fenykl_shot_buf_alloc(uint32_t bytes, uint32_t *handle);
static inline uint32_t fenykl_shot_buf_free(uint32_t handle);
#endif

/* --- wb_base: the colour-matrix bases in the property registry; CPU0 task --- */
#if FENYKL_HAS_WB_BASE
/*
 * Every live-view and still colour matrix is B x M (3 x 3 s16, row major) with M per light source
 * and FENYKL_WB_BASE_N bases B; a base with rows 0 and 2 exchanged swaps R and B in every pass. A
 * base is changed by storing the uncached address of a copy (FENYKL_SHARED) in its registry word,
 * never by writing the stock object. Also from fenykl_body.h: FENYKL_WB_BASE_N and FENYKL_WB_MAT_N
 * (entries of a matrix).
 */

/* Uncached address of the registry word that holds base i's object address (i < FENYKL_WB_BASE_N),
   in the firmware's lookup order. */
static inline uint32_t fenykl_wb_base_slot(unsigned int i);
/* The other word of slot's registry entry, for logs. */
static inline uint32_t fenykl_wb_base_head(uint32_t slot);
/* slot's registry entry is base i's: its other word names the base's property and size. */
static inline bool fenykl_wb_base_entry_ok(unsigned int i, uint32_t slot);
/* The WB setting in the target's encoding, for logs; -1 if unreadable. */
static inline int fenykl_wb_mode(void);
/* The live-view colour matrix the firmware last computed into m (FENYKL_WB_MAT_N), for logs. */
static inline void fenykl_wb_lv_matrix(int16_t *m);
#endif

#include "fenykl_body.h"

#endif
