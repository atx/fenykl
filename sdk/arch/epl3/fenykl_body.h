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

#endif
