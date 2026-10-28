/* E-PL3 part of body.h (included from there): the body API on the v1.6 firmware's data. */
#ifndef FENYKL_BODY_TARGET_H
#define FENYKL_BODY_TARGET_H

#define FENYKL_GUI_SS_REC 0x02u		/* every live-view screen */
#define FENYKL_GUI_SS_MENU 0x03u	/* main menu */
#define FENYKL_GUI_SS_PLAY 0x06u	/* single playback */

static inline uint16_t fenykl_gui_state(void)
{
	return (uint16_t)(fenykl_rd8(FW_GUI_SUPER) << 8 | fenykl_rd8(FW_GUI_STATE));
}

static inline bool fenykl_gui_live_view(void)
{
	return fenykl_rd8(FW_GUI_SUPER) == FENYKL_GUI_SS_REC;
}

static inline bool fenykl_gui_playback(void)
{
	return fenykl_rd8(FW_GUI_SUPER) == FENYKL_GUI_SS_PLAY;
}

#if FENYKL_HAS_LENS
#define FBODY_PROP_AV 0xA03Au		/* setting 3001: shooting Av, u16 */

static inline bool fenykl_lens_attached(void)
{
	return fenykl_rd16(FW_LENS_ATTACHED) != 0;
}

static inline uint32_t fenykl_lens_focus_mm(void)
{
	return fenykl_rd32(FW_LENS_OBJ_DST);
}

static inline uint32_t fenykl_lens_focal_mm(void)
{
	return fw_lens_focal_length_now() & 0xFFFFu;
}

static inline uint32_t fenykl_lens_av(void)
{
	const volatile uint16_t *p = fw_prop_get_obj(FBODY_PROP_AV);

	return p ? p[0] : 0;
}

static inline uint32_t fenykl_lens_iris_av(void)
{
	return fenykl_rd16(FW_AV_EFFECT);
}
#endif

#if FENYKL_HAS_BATT
#define FBODY_BATT_SEM 5		/* SBCHKCORE: buffer and event of bchk_ad_read are shared */
#define FBODY_BATT_SEM_MS 20		/* lock and read take 5-7 ms [camera] */
#define FBODY_BATT_CH 4u		/* A/D channel of the battery voltage */
#define FBODY_SRC_BATT 0x200u
#define FBODY_SRC_BATT2 0x400u		/* a battery too (hfw_get_battery_state); 0x100 AC or DC */

static inline int fenykl_batt_read(uint32_t *v)
{
	if (fw_OlySemaphoreWait(FBODY_BATT_SEM, FBODY_BATT_SEM_MS))
		return FENYKL_BATT_EBUSY;
	/* bchk_ad_read waits forever on its event, so read only while the stock check does */
	if (!fenykl_rd8(FW_POWER_SRC_CHECK)) {
		fw_OlySemaphoreSignal(FBODY_BATT_SEM);
		return FENYKL_BATT_ESTANDBY;
	}
	*v = fw_bchk_ad_read(FBODY_BATT_CH);
	fw_OlySemaphoreSignal(FBODY_BATT_SEM);
	return 0;
}

static inline uint32_t fenykl_power_src(void)
{
	return fenykl_rd16(FW_POWER_SRC);
}

static inline bool fenykl_power_src_batt(uint32_t src)
{
	return src == FBODY_SRC_BATT || src == FBODY_SRC_BATT2;
}
#endif

#if FENYKL_HAS_AFC
#define FBODY_TF_MODE 0x05u		/* take_focus u8 mode_focus: 1 MF [camera] */
#define FBODY_TF_MF 1u
#define FBODY_PROP_AREA 0x84D1u		/* AF area mode, 2 = all targets */
#define FBODY_PROP_TARGET 0x88B2u	/* AF target index */
#define FBODY_AREA_ALL 2u
#define FBODY_TARGETS 35u
#define FBODY_CTX_COLS 0x5Du
#define FBODY_CTX_ROWS 0x5Eu
#define FBODY_CTX_PATTERN 0x2D0u
#define FBODY_CTX_CUT3 0x2D6u		/* u16 */

static inline uint32_t fbody_prop_u8(unsigned int prop)
{
	const volatile uint8_t *p = fw_prop_get_obj(prop);

	return p ? p[0] : 0xFFu;
}

static inline bool fenykl_af_mf(void)
{
	uint32_t tf = fenykl_rd32(FW_TAKE_FOCUS_PTR);

	return tf && fenykl_rd8(tf + FBODY_TF_MODE) == FBODY_TF_MF;
}

static inline bool fenykl_af_half(void)
{
	return !(fenykl_rd8(FW_TRG_SNAPSHOT) & 1u);
}

static inline int fenykl_af_target_area(void)
{
	uint32_t t = fbody_prop_u8(FBODY_PROP_TARGET);

	if (t < FBODY_TARGETS && fenykl_rd8(FW_TAKE_FCS_AF_MODE2FCS + 2 * t + 1))
		return (int)fenykl_rd8(FW_TAKE_FCS_AF_MODE2FCS + 2 * t);
	return FENYKL_AF_AREA_NONE;
}

static inline bool fenykl_af_all_targets(void)
{
	return fbody_prop_u8(FBODY_PROP_AREA) == FBODY_AREA_ALL;
}

static inline bool fenykl_af_grid(struct fenykl_af_grid *g)
{
	uint32_t ctx = fenykl_rd32(FW_FCS_CONTRAST_CTX);

	if (!ctx)
		return false;
	g->buf = (const volatile uint32_t *)FW_FCS_DMA_BUF;
	g->chan_words = fenykl_rd32(FW_FCS_AREA_SIZE);
	g->cols = (uint8_t)fenykl_rd8(ctx + FBODY_CTX_COLS);
	g->rows = (uint8_t)fenykl_rd8(ctx + FBODY_CTX_ROWS);
	g->pattern = (uint8_t)fenykl_rd8(ctx + FBODY_CTX_PATTERN);
	g->cut3 = (uint16_t)fenykl_rd16(ctx + FBODY_CTX_CUT3);
	return true;
}

#define FENYKL_CNT_SET_SIZE 0x104u
#define FENYKL_CNT_SET_INFO 0x90u
#define FENYKL_CNT_SET_NUMBER 0x00u
#define FENYKL_CNT_REQ_SIZE 0x40u
#define FENYKL_CNT_EV_ST 0x400u		/* AF_ST */
#define FENYKL_CNT_EV_CP 0x800u		/* AF_CP */
#define FENYKL_CNT_EV_ERR 0u
#define FENYKL_CNT_DMA_WORDS 512u	/* 4 channels x 5 rows x 16 words, 768 bytes of slack */
#define FENYKL_CNT_DMA_ALIGN 64u
#define FENYKL_CNT_DMA_STATS 0u
#define FENYKL_CNT_STATS_CHANNELS 4u	/* HPF1, HPF2, HPF3, HPFV */

#define FBODY_TF_MOVIE 0x07u
#define FBODY_TF_LV_MODE 0xD4u
#define FBODY_CTX_REQ 0x2DCu		/* fcs's cam_request object */
#define FBODY_CTX_SETTING 0x378u
#define FBODY_SET_UPDATE 0x08u		/* u16 update bits, cleared by cam_cnt_apply */
#define FBODY_SET_DMA 0x88u
#define FBODY_SET_ENDLESS 0xFFFFu
#define FBODY_SET_ALL 0x1FFFu		/* every setting-driven update bit (0x1..0x1000) */
#define FBODY_CL_SETTING 0x08u		/* driver client +8: its contrast_setting */
#define FBODY_SNAP_COLS 7u		/* pattern 0 / 0xD: 7 x 5 */
#define FBODY_SNAP_ROWS 5u

/* The 0x40-byte request object. */
struct fbody_cnt_req {
	uint16_t type;		/* +0x00 */
	uint8_t flag;		/* +0x02 = 1 */
	uint8_t pad;
	uint32_t expo;		/* +0x04: 0, the driver keeps its sensor info */
	uint32_t smode;		/* +0x08 */
	uint32_t r0c, r10;
	uint32_t setting;	/* +0x14 contrast_setting */
	uint32_t r18, r1c;
	uint32_t step;		/* +0x20: 0 selects the async contrast path */
	uint32_t r24;
	uint32_t done_cb;	/* +0x28 */
	uint32_t frame_cb;	/* +0x2C */
	uint32_t events;	/* +0x30 */
	uint32_t arg;		/* +0x34 */
	uint32_t r38, r3c;
};

_Static_assert(sizeof(struct fbody_cnt_req) == FENYKL_CNT_REQ_SIZE, "request is 0x40 bytes");

static inline uint32_t fenykl_cnt_fcs_setting(uint8_t *pat, uint8_t *cols, uint8_t *rows)
{
	uint32_t tf = fenykl_rd32(FW_TAKE_FOCUS_PTR), ctx = fenykl_rd32(FW_FCS_CONTRAST_CTX);
	uint32_t set = ctx + FBODY_CTX_SETTING, p;

	if (!tf || !ctx || fenykl_rd8(FW_FCS_AF_EVAL_STATE) != 2 || fenykl_af_half())
		return 0;
	if (fenykl_rd8(tf + FBODY_TF_MODE) == FBODY_TF_MF || fenykl_rd8(tf + FBODY_TF_MOVIE))
		return 0;
	p = fenykl_rd8(ctx + FBODY_CTX_PATTERN);
	if ((p != 0 && p != 0xD) || fenykl_rd8(ctx + FBODY_CTX_COLS) != FBODY_SNAP_COLS ||
	    fenykl_rd8(ctx + FBODY_CTX_ROWS) != FBODY_SNAP_ROWS)
		return 0;
	if (fenykl_rd16(set + FENYKL_CNT_SET_NUMBER) != FBODY_SET_ENDLESS ||
	    fenykl_rd32(set + FBODY_SET_DMA) != FW_FCS_DMA_BUF ||
	    fenykl_rd32(ctx + FBODY_CTX_REQ + 0x14u) != set)
		return 0;
	*pat = (uint8_t)p;
	*cols = FBODY_SNAP_COLS;
	*rows = FBODY_SNAP_ROWS;
	return set;
}

static inline void fenykl_cnt_setting_prep(uint8_t *set, uint32_t dma)
{
	*(volatile uint16_t *)(set + FENYKL_CNT_SET_NUMBER) = FBODY_SET_ENDLESS;
	*(volatile uint16_t *)(set + FBODY_SET_UPDATE) = FBODY_SET_ALL;
	*(uint32_t *)(set + FBODY_SET_DMA) = dma;
}

static inline void fenykl_cnt_req_prep(void *req, uint32_t set, uint32_t frame_cb, uint32_t done_cb,
				       uint32_t arg)
{
	struct fbody_cnt_req *rq = req;

	rq->type = 7;
	rq->flag = 1;
	rq->setting = set;
	rq->done_cb = done_cb;
	rq->frame_cb = frame_cb;
	rq->events = FENYKL_CNT_EV_ST | FENYKL_CNT_EV_CP;
	rq->arg = arg;
}

/* Owner of a driver slot word: 0 empty, 1 set's, 2 foreign. */
static inline uint32_t fbody_cnt_slot_owner(uint32_t slot, uint32_t set)
{
	uint32_t cl = fenykl_rd32(slot);

	if (!cl)
		return 0;
	return fenykl_rd32(cl + FBODY_CL_SETTING) == set ? 1 : 2;
}

static inline bool fenykl_cnt_foreign(uint32_t set)
{
	return fbody_cnt_slot_owner(FW_CNT_CLIENT, set) == 2 ||
	       fbody_cnt_slot_owner(FW_CNT_QUEUED, set) == 2;
}

/* Stills screens of SS_REC: Basic, Histogram, H&S, STILL_OFF, STILL_OVER, level gauge. */
static inline bool fenykl_cnt_lv(void)
{
	uint32_t st = fenykl_rd8(FW_GUI_STATE);

	if (fenykl_rd8(FW_GUI_SUPER) != FENYKL_GUI_SS_REC)
		return false;
	return (st >= 0x03 && st <= 0x05) || (st >= 0x09 && st <= 0x0B);
}

static inline bool fenykl_cnt_movie(void)
{
	uint32_t tf = fenykl_rd32(FW_TAKE_FOCUS_PTR);

	return !tf || fenykl_rd8(tf + FBODY_TF_MOVIE) || fenykl_rd8(tf + FBODY_TF_LV_MODE);
}

static inline bool fenykl_cnt_drv_dbg(void)
{
	return fenykl_rd16(FW_CNT_DRV_DBG) != 0;
}
#endif

#if FENYKL_HAS_SHOT
#define FENYKL_DISP_LV_PITCH 1600u	/* 800x600 YUYV */
#define FENYKL_DISP_LV_BYTES (FENYKL_DISP_LV_PITCH * 600u)
/* The panel shows the top-left 720 x 480 of the OSD planes, 16:9: 853 x 480 at square pixels. */
#define FENYKL_DISP_W 720u
#define FENYKL_DISP_H 480u
#define FENYKL_DISP_SQUARE_W 853u	/* 720 * 32 / 27 */
#define FENYKL_OSD_LAYERS 3u
#define FENYKL_OSD_PLANE_W 896u
#define FENYKL_OSD_PLANE_H 600u
#define FENYKL_OSD_BPP { 4, 8, 4 }
/* osd_palettes: layer 1 256 entries, layer 0 16 (+0x400), layer 2 8 (+0x440) */
#define FENYKL_OSD_PAL_ENTRIES { 16, 256, 8 }
#define FENYKL_OSD_PAL_FIRST { 256, 0, 272 }
#define FENYKL_OSD_PAL_TOTAL (256u + 16u + 8u)
/* bottom to top [inferred from camera OSD dumps; L1 on top (osdcard)]; 0x1A4 is logged at debug */
#define FENYKL_OSD_STACK { 2, 0, 1 }
/* img3: the face/AF frames, 8 bpp palette indices, index 0 transparent; above img1, below the OSD */
#define FENYKL_FRAMES_PITCH 448u
#define FENYKL_FRAMES_H 240u
#define FENYKL_FRAMES_PAL 8u

#define FBODY_FLIP_ENTRIES 3u
#define FBODY_RECFILE_BLOCKS 256u
#define FBODY_RECFILE_BLOCK 0x1E3C0u

/* gpi_layer_vram's descriptor */
struct fbody_osd_vram {
	uint32_t plane;
	uint16_t width, height;
	uint32_t pdata;
};

/* display shadow slot per layer: osd0 = L1, osd1 = L0, osd2 = L2 */
static const uint8_t fbody_osd_shadow_slot[FENYKL_OSD_LAYERS] = { 1, 0, 2 };

static inline uint32_t fenykl_cached(uint32_t a)
{
	return (a & 0x3FFFFFFFu) | 0x40000000u;
}

static inline bool fenykl_dram(uint32_t a, uint32_t bytes)
{
	return fenykl_dram_ptr(a, 4) && fenykl_cached(a) + bytes <= FW_IMAGE_START;
}

static inline uint32_t fenykl_disp_lv(void)
{
	return fenykl_rd32(FW_MOV_IMG1_SHADOW);
}

static inline bool fenykl_disp_lv_pos(uint32_t lv, uint32_t *x, uint32_t *y)
{
	for (unsigned int i = 0; i < FBODY_FLIP_ENTRIES; i++) {
		uint32_t e = FW_MOV_DISP_FLIP_TBL + 16 * i;

		if ((fenykl_rd32(e) & 0x1FFFFFFFu) != (lv & 0x1FFFFFFFu))
			continue;
		*x = fenykl_rd32(e + 4);
		*y = fenykl_rd32(e + 8);
		return true;
	}
	return false;
}

static inline uint32_t fenykl_disp_img1_x(void)
{
	return fenykl_rd32(FW_DISP_IMG1_X);
}

static inline uint32_t fenykl_disp_img1_y(void)
{
	return fenykl_rd32(FW_DISP_IMG1_Y);
}

static inline uint32_t fenykl_disp_mix(void)
{
	return fenykl_rd32(FW_DISP_MIX);
}

static inline unsigned int fenykl_osd_bpp(unsigned int l)
{
	return fenykl_rd8(FW_GPI_LAYER_BPP + l);
}

static inline uint32_t fenykl_osd_draw_buf(void)
{
	return fenykl_rd8(FW_GPI_DRAW_BUF);
}

static inline uint32_t fenykl_osd_hide_mask(void)
{
	return fenykl_rd16(FW_GPI_HIDE_MASK);
}

static inline uint32_t fenykl_osd_buf(unsigned int l, unsigned int b)
{
	uint32_t h = fenykl_rd32(FW_GPI_LAYER_VRAM + 4 * (l * 2 + b));
	const volatile struct fbody_osd_vram *v =
		(const volatile struct fbody_osd_vram *)(uintptr_t)h;

	if (!fenykl_dram(h, sizeof(*v)) || v->width != FENYKL_OSD_PLANE_W ||
	    v->height != FENYKL_OSD_PLANE_H || !fenykl_dram(v->pdata, 0))
		return 0;
	return v->pdata;
}

static inline uint32_t fenykl_osd_shown(unsigned int l)
{
	return fenykl_rd32(FW_OSD_SHADOW + 4 * fbody_osd_shadow_slot[l]);
}

static inline const uint8_t *fenykl_osd_palettes(void)
{
	return (const uint8_t *)FW_OSD_PALETTES;
}

static inline uint32_t fenykl_frames_shown(void)
{
	return fenykl_rd32(FW_DISP_IMG3_SHADOW);
}

static inline void fenykl_frames_geom(uint16_t *w, uint16_t *h, uint16_t *to_w, uint16_t *to_h)
{
	*w = (uint16_t)fenykl_rd16(FW_DISP_IMG3_GEOM + 4);
	*h = (uint16_t)fenykl_rd16(FW_DISP_IMG3_GEOM + 6);
	*to_w = (uint16_t)fenykl_rd16(FW_DISP_IMG3_GEOM + 12);
	*to_h = (uint16_t)fenykl_rd16(FW_DISP_IMG3_GEOM + 14);
}

static inline const uint8_t *fenykl_frames_palette(void)
{
	return (const uint8_t *)FW_FRAME_PALETTE;
}

static inline bool fenykl_recfile_fits(uint32_t bytes)
{
	uint32_t blocks = (bytes + FBODY_RECFILE_BLOCK - 1) / FBODY_RECFILE_BLOCK;
	unsigned int run = 0;

	for (unsigned int i = 0; i < FBODY_RECFILE_BLOCKS; i++) {
		run = fenykl_rd8(FW_RECFILE_USED + i) ? 0 : run + 1;
		if (run >= blocks)
			return true;
	}
	return false;
}

static inline uint32_t fenykl_recfile_alloc(uint32_t bytes)
{
	return fw_recfile_alloc(bytes);
}

static inline void fenykl_recfile_free(uint32_t buf)
{
	fw_recfile_free(buf);
}
#endif

#if FENYKL_HAS_IMG_EXEC
#define FENYKL_IMG_CTRL_FLAG 0x54u	/* u32 ulFlag */
#define FENYKL_IMG_CTRL_PARAM 0x60u	/* IMG_PARAM *pImgParam */
#define FENYKL_IMG_CTRL_MODE 0xC4u	/* u8 ucMode */
#define FENYKL_IMG_FLAG_ENGINE2 0x10000000u
#define FENYKL_IMG_PARAM_MAT 0x18u	/* s16 ashMat[9] */
#define FENYKL_IMG_MAT_N 9

/* img_mode_class 0x6E762F48 for ucMode 0..0x1D; larger values are class 3 */
static const uint8_t fbody_img_class[0x1E] = {
	0, 0, 0, 0, 0, 0, 3, 0, 0, 1, 1, 1, 3, 3, 3, 3,
	3, 3, 2, 2, 2, 2, 2, 3, 0, 0, 0, 0, 2, 2,
};

static inline unsigned int fenykl_img_mode_class(unsigned int mode)
{
	return mode < sizeof(fbody_img_class) ? fbody_img_class[mode] : 3;
}
#endif

#if FENYKL_HAS_PEAK
/*
 * Line pass: a second engine-2 pass on a copy of the live-view pass's IMG_CTRL. Output 2 (the
 * face-detection output, Y8, 640x480 stock) whole at 320x240 and its area halved: the resizer
 * low-passes the edge image. Overlay: img2 in format 3 placed by api_disp_Disp_LayerCustom
 * (DISPLAY command 6); a flip writes the img2 shadow, address and enable bit under the update hold,
 * as disp_img1_set_addr_locked does for img1.
 */
#define FENYKL_PEAK_CTRL_SIZE 0xC8u
#define FENYKL_PEAK_LN_W 320u
#define FENYKL_PEAK_LN_H 240u
#define FENYKL_PEAK_LUM_BYTES (2u * 0x1E3C0u)	/* two RECFILE blocks */
#define FENYKL_PEAK_LUT_N 256u
#define FENYKL_PEAK_SHADOW_N 6u
#define FENYKL_OVL_RED 0xFF0000FFu		/* R << 24 | G << 16 | B << 8 | alpha [camera] */

#define FBODY_C_W1 0x10u		/* output 1: display, YUYV */
#define FBODY_C_W1B 0x14u		/* output 2: face detection, Y8 */
#define FBODY_C_W1C 0x18u
#define FBODY_C_W1D 0x1Cu
#define FBODY_C_W2 0x20u
#define FBODY_C_FNRY 0x24u
#define FBODY_C_FNRC 0x28u
#define FBODY_C_LUM 0x40u
#define FBODY_C_OUT_HW 0x8Eu		/* u16 output 1 whole, Vw at +2 */
#define FBODY_C_OUT2_HW 0x92u		/* u16 output 2 whole, Vw at +2 */
#define FBODY_C_AREA_HS 0x98u		/* u16 output 1 area start, Vs at +2 */
#define FBODY_C_AREA_HW 0x9Cu		/* u16 output 1 area size, Vw at +2 */
#define FBODY_C_AREA2_HS 0xA0u		/* u16 output 2 area start, Vs at +2 */
#define FBODY_C_AREA2_HW 0xA4u		/* u16 output 2 area size, Vw at +2 */
#define FBODY_C_LUM_HW 0xBAu		/* u8, Vw at +1 */
#define FBODY_MODE_LV 7u
#define FBODY_F_ALT_CTX 0x2u
#define FBODY_F_2000 0x2000u
#define FBODY_F_2PLANE 0x10000u
#define FBODY_F_OUT2 0x4000000u
#define FBODY_F_ART 0x20000000u
#define FBODY_FD_W 640u
#define FBODY_FD_H 480u
#define FBODY_LUM_CELL 16u		/* assumed upper bound of bytes per LUM cell */
#define FBODY_PHYS 0x3FFFFFFFu		/* the display reads back addresses without bits 30-31 */

#define FBODY_X_LUT_A 0x5ACu
#define FBODY_X_LUT_B 0x11C4u
#define FBODY_X_EDGE_A 0x280u
#define FBODY_X_LIM_A 0x284u
#define FBODY_X_EDGE_B 0xF6Cu
#define FBODY_X_LIM_B 0xF70u
#define FBODY_EDGE_GAIN 255u
#define FBODY_EDGE_LIM 0x03FF03FFu

#define FBODY_EN_IMG2 0x10u
#define FBODY_FMT_KEEP 0xFF0FCE00u	/* all but format, bits 12-13 and alpha */
#define FBODY_FMT3 0x00301000u		/* format 3, bit 12 as disp_fmt_set sets it */
#define FBODY_ALPHA_FULL 0x80u
#define FBODY_DEV_TFT 1u
#define FBODY_CAMTOP_CLIP 8u

/* api_disp_Disp_LayerCustom's request: a layer and the parameter block */
struct fbody_wh {
	uint16_t w, h;
};

struct fbody_layer {
	struct fbody_wh *from_area, *from_target, *from_coord, *to_target, *to_coord;
	uint32_t addr[2];
	uint8_t type;
};

struct fbody_custom {
	void *bg;
	struct fbody_layer *img[3], *osd[3];
	uint32_t blink;
	int8_t mixorder;
	uint8_t color_range;
};

struct fenykl_ovl {
	struct fbody_wh from_area, from_target, from_coord, to_target, to_coord;
	struct fbody_layer layer;
	struct fbody_custom custom;
	uint32_t fmt, conv, alpha;	/* stock img2 setup */
};

static const uint16_t fbody_shadow_off[FENYKL_PEAK_SHADOW_N] = {
	FBODY_X_LUT_A, FBODY_X_LUT_B, FBODY_X_EDGE_A, FBODY_X_LIM_A, FBODY_X_EDGE_B, FBODY_X_LIM_B,
};

static inline uint32_t fbody_c16(const void *c, uint32_t off)
{
	return *(const uint16_t *)((const uint8_t *)c + off);
}

static inline void fbody_set16(void *c, uint32_t off, uint32_t v)
{
	*(uint16_t *)((uint8_t *)c + off) = (uint16_t)v;
}

static inline uint32_t fbody_c8(const void *c, uint32_t off)
{
	return ((const uint8_t *)c)[off];
}

static inline bool fenykl_peak_lv_pass(const void *ctrl)
{
	uint32_t flag = fenykl_rd32((uint32_t)ctrl + FENYKL_IMG_CTRL_FLAG);
	uint32_t need = FENYKL_IMG_FLAG_ENGINE2 | FBODY_F_OUT2;

	if (fenykl_rd8((uint32_t)ctrl + FENYKL_IMG_CTRL_MODE) != FBODY_MODE_LV)
		return false;
	if ((flag & need) != need)
		return false;
	return !(flag & (FBODY_F_ALT_CTX | FBODY_F_2000 | FBODY_F_2PLANE | FBODY_F_ART));
}

static inline bool fenykl_peak_same_layout(const void *a, const void *b)
{
	static const uint8_t off16[] = {
		FBODY_C_OUT_HW, FBODY_C_OUT_HW + 2, FBODY_C_OUT2_HW, FBODY_C_OUT2_HW + 2,
		FBODY_C_AREA_HS, FBODY_C_AREA_HS + 2, FBODY_C_AREA_HW, FBODY_C_AREA_HW + 2,
		FBODY_C_AREA2_HS, FBODY_C_AREA2_HS + 2, FBODY_C_AREA2_HW, FBODY_C_AREA2_HW + 2,
	};

	for (unsigned int i = 0; i < sizeof(off16); i++)
		if (fbody_c16(a, off16[i]) != fbody_c16(b, off16[i]))
			return false;
	return fbody_c16(a, FBODY_C_LUM_HW) == fbody_c16(b, FBODY_C_LUM_HW);
}

static inline const char *fenykl_peak_layout(const void *ctrl, struct fenykl_peak_layout *l)
{
	uint32_t hw = fbody_c16(ctrl, FBODY_C_OUT_HW), vw = fbody_c16(ctrl, FBODY_C_OUT_HW + 2);
	uint32_t a2w = fbody_c16(ctrl, FBODY_C_AREA2_HW), a2v = fbody_c16(ctrl, FBODY_C_AREA2_HW + 2);
	uint32_t lum = fbody_c8(ctrl, FBODY_C_LUM_HW) * fbody_c8(ctrl, FBODY_C_LUM_HW + 1) *
		       FBODY_LUM_CELL;

	if (fbody_c16(ctrl, FBODY_C_OUT2_HW) != FBODY_FD_W ||
	    fbody_c16(ctrl, FBODY_C_OUT2_HW + 2) != FBODY_FD_H)
		return "output 2 not 640x480";
	if (fbody_c16(ctrl, FBODY_C_AREA2_HS) || fbody_c16(ctrl, FBODY_C_AREA2_HS + 2) || !a2w ||
	    !a2v || a2w > FBODY_FD_W || a2v > FBODY_FD_H || a2w % 8)
		return "output 2 area not handled";
	if (!hw || !vw || hw > 2048 || vw > 2048)
		return "output 1 not handled";
	if (lum > FENYKL_PEAK_LUM_BYTES)
		return "LUM output too large";
	*l = (struct fenykl_peak_layout){
		.out1_bytes = hw * vw * 2,
		.ln_w = (uint16_t)(a2w / 2), .ln_h = (uint16_t)(a2v / 2),
		.x = (uint16_t)fbody_c16(ctrl, FBODY_C_AREA_HS),
		.y = (uint16_t)fbody_c16(ctrl, FBODY_C_AREA_HS + 2),
		.w = (uint16_t)fbody_c16(ctrl, FBODY_C_AREA_HW),
		.h = (uint16_t)fbody_c16(ctrl, FBODY_C_AREA_HW + 2),
	};
	if (!l->w || !l->h)
		return "output 1 area empty";
	return NULL;
}

static inline void fenykl_peak_line_ctrl(void *copy, const struct fenykl_peak_layout *l,
					 uint32_t out1, uint32_t line, uint32_t lum)
{
	uint32_t c = (uint32_t)copy;

	fenykl_wr32(c + FBODY_C_W1, out1);
	fenykl_wr32(c + FBODY_C_W1B, line);
	fenykl_wr32(c + FBODY_C_W1C, 0);
	fenykl_wr32(c + FBODY_C_W1D, 0);
	fenykl_wr32(c + FBODY_C_W2, 0);
	fenykl_wr32(c + FBODY_C_FNRY, 0);
	fenykl_wr32(c + FBODY_C_FNRC, 0);
	fenykl_wr32(c + FBODY_C_LUM, lum);
	fbody_set16(copy, FBODY_C_OUT2_HW, FENYKL_PEAK_LN_W);
	fbody_set16(copy, FBODY_C_OUT2_HW + 2, FENYKL_PEAK_LN_H);
	fbody_set16(copy, FBODY_C_AREA2_HW, l->ln_w);
	fbody_set16(copy, FBODY_C_AREA2_HW + 2, l->ln_h);
}

static inline uint32_t fenykl_peak_out1(const void *ctrl)
{
	return fenykl_rd32((uint32_t)ctrl + FBODY_C_W1) & FBODY_PHYS;
}

static inline void fenykl_peak_shadow_patch(uint32_t *saved, uint32_t coring,
					    const uint16_t *lut_zero)
{
	uint32_t patch[FENYKL_PEAK_SHADOW_N];

	for (unsigned int i = 0; i < FENYKL_PEAK_SHADOW_N; i++)
		saved[i] = fenykl_rd32(FW_IMG_CTX_E2 + fbody_shadow_off[i]);
	patch[0] = (uint32_t)lut_zero;
	patch[1] = (uint32_t)lut_zero;
	patch[2] = (saved[2] & 0xFFu) | coring << 8 | FBODY_EDGE_GAIN << 16;
	patch[3] = FBODY_EDGE_LIM;
	patch[4] = (saved[4] & 0xFFu) | coring << 8 | FBODY_EDGE_GAIN << 16;
	patch[5] = FBODY_EDGE_LIM;
	for (unsigned int i = 0; i < FENYKL_PEAK_SHADOW_N; i++)
		fenykl_wr32(FW_IMG_CTX_E2 + fbody_shadow_off[i], patch[i]);
	fenykl_wr8(FW_IMG_LUT_DIRTY, 1);
	fenykl_wr8(FW_IMG_LUT_DIRTY + 2, 1);
}

/* The registers and LUT RAM are written: the next pass must load the stock LUTs again. */
static inline void fenykl_peak_shadow_restore(const uint32_t *saved)
{
	for (unsigned int i = 0; i < FENYKL_PEAK_SHADOW_N; i++)
		fenykl_wr32(FW_IMG_CTX_E2 + fbody_shadow_off[i], saved[i]);
	fenykl_wr8(FW_IMG_LUT_DIRTY, 1);
	fenykl_wr8(FW_IMG_LUT_DIRTY + 2, 1);
}

static inline bool fenykl_disp_rear(void)
{
	return fenykl_rd8(FW_DISP_DEV) == FBODY_DEV_TFT;
}

static inline bool fenykl_lv_magnified(void)
{
	return fenykl_rd8(FW_CAMTOP_STATE) == FBODY_CAMTOP_CLIP;
}

static inline bool fenykl_gui_still_lv(void)
{
	uint32_t s = fenykl_rd8(FW_GUI_STATE);

	if (!fenykl_gui_live_view())
		return false;
	/* STILL_BASIC, STILL_HISTO, STILL_HS, STILL_OFF, STILL_SLOPE */
	return s == 0x03u || s == 0x04u || s == 0x05u || s == 0x09u || s == 0x0Bu;
}

static inline uint32_t fenykl_disp_lv_latched(void)
{
	return fenykl_rd32(FW_DISP_IMG1_LATCHED) & FBODY_PHYS;
}

static inline bool fenykl_ovl_free(void)
{
	return fenykl_rd32(FW_DISP_IMG2_SHADOW) == 0;
}

/* disp_hw_hold: false holds the register update, true releases it. */
static inline void fbody_disp_hold(bool release)
{
	uint32_t bits = fenykl_rd32(FW_DISP_HOLD_STATE) == 0x10u ? 0x20u : 0x60u;
	uint32_t reg = release ? FW_DISP_UPD_RELEASE : FW_DISP_UPD_HOLD;

	fenykl_wr32(reg, fenykl_rd32(reg) | bits);
}

/* DISPLAY command 6 for img2 alone; addr 0 takes it down. */
static inline unsigned int fbody_ovl_custom(struct fenykl_ovl *o, uint32_t addr)
{
	o->layer = (struct fbody_layer){
		.from_area = &o->from_area, .from_target = &o->from_target,
		.from_coord = &o->from_coord, .to_target = &o->to_target, .to_coord = &o->to_coord,
		.addr = { addr, 0 },
	};
	o->custom = (struct fbody_custom){
		.img = { 0, &o->layer, 0 }, .blink = 0xFFFFFFFFu, .mixorder = -1,
		.color_range = 0xFFu,
	};
	return fw_api_disp_Disp_LayerCustom(&o->custom);
}

static inline void fenykl_ovl_flip(uint32_t buf)
{
	fenykl_wr32(FW_DISP_IMG2_SHADOW, buf);
	fbody_disp_hold(false);
	fenykl_wr32(FW_DISP_IMG2_ADDR, buf);
	fenykl_wr32(FW_DISP_LAYER_EN, fenykl_rd32(FW_DISP_LAYER_EN) | FBODY_EN_IMG2);
	fbody_disp_hold(true);
}

static inline unsigned int fenykl_ovl_show(struct fenykl_ovl *o, uint32_t buf, uint16_t w,
					   uint16_t h, uint16_t to_x, uint16_t to_y, uint16_t to_w,
					   uint16_t to_h)
{
	uint32_t psw;
	unsigned int r;

	o->fmt = fenykl_rd32(FW_DISP_IMG2_FMT);
	o->conv = fenykl_rd32(FW_DISP_IMG2_CONV);
	o->alpha = fenykl_rd32(FW_DISP_IMG2_ALPHA);
	fenykl_wr32(FW_DISP_IMG2_ALPHA, FBODY_ALPHA_FULL);
	fenykl_wr32(FW_DISP_IMG2_FMT, (o->fmt & FBODY_FMT_KEEP) | FBODY_FMT3 | 2 * FBODY_ALPHA_FULL);
	fenykl_wr32(FW_DISP_IMG2_CONV, 0);
	o->from_area = (struct fbody_wh){ FENYKL_PEAK_LN_W, FENYKL_PEAK_LN_H };
	o->from_target = (struct fbody_wh){ w, h };
	o->from_coord = (struct fbody_wh){ 0, 0 };
	o->to_target = (struct fbody_wh){ to_w, to_h };
	o->to_coord = (struct fbody_wh){ to_x, to_y };
	r = fbody_ovl_custom(o, buf);
	if (r)
		return r;
	/* Enabled now: a DISPLAY commit may be far off while only the live view flips. */
	psw = fenykl_irq_save();
	fenykl_ovl_flip(buf);
	fenykl_irq_restore(psw);
	return 0;
}

static inline uint32_t fenykl_ovl_latched(void)
{
	return fenykl_rd32(FW_DISP_IMG2_LATCHED) & FBODY_PHYS;
}

static inline unsigned int fenykl_ovl_hide(struct fenykl_ovl *o)
{
	unsigned int r = fbody_ovl_custom(o, 0);
	uint32_t psw = fenykl_irq_save();

	fenykl_wr32(FW_DISP_IMG2_SHADOW, 0);
	fbody_disp_hold(false);
	fenykl_wr32(FW_DISP_LAYER_EN, fenykl_rd32(FW_DISP_LAYER_EN) & ~FBODY_EN_IMG2);
	fenykl_wr32(FW_DISP_IMG2_ALPHA, o->alpha);
	fenykl_wr32(FW_DISP_IMG2_FMT, o->fmt);
	fenykl_wr32(FW_DISP_IMG2_CONV, o->conv);
	fbody_disp_hold(true);
	fenykl_irq_restore(psw);
	return r;
}
#endif

#endif
