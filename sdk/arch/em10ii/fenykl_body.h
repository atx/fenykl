/* E-M10 II part of body.h (included from there): the GUI, lens, batt, afc and shot groups on the
   v1.4 firmware's data. The other groups are not implemented on this body (target.toml capabilities). */
#ifndef FENYKL_BODY_TARGET_H
#define FENYKL_BODY_TARGET_H

#define FENYKL_GUI_SS_LV 0x01u		/* super state of the live-view screens */
#define FENYKL_GUI_ST_LV 0x08u		/* its state while live view is shown */
#define FENYKL_GUI_SS_MENU 0x11u	/* main menu (SsMenu), the plugin settings tab included */
#define FENYKL_GUI_SS_PLAY 0x06u	/* playback: camera-seen as 0x06xx */

/* The registry word holding a property's object: the entry the lookup 0x60076078 reads, the
   override table's while it is in use and its object is non-zero. */
static inline uint32_t fbody_prop_slot(unsigned int prop)
{
	uint32_t entry = 8u * (prop >> 4) + 4u;

	if (fenykl_rd16(FW_PROP_OVERRIDE_VALID) && fenykl_rd32(FW_PROP_OVERRIDE + entry))
		return FW_PROP_OVERRIDE + entry;
	return FW_PROP_TABLE + entry;
}

/* A property's object, 0 if unregistered: the lookup without the getter 0x60076FD9's core-view
   mapping 0x600750FD, which is the identity on CPU0. */
static inline uint32_t fbody_prop_obj(unsigned int prop)
{
	return fenykl_rd32(fbody_prop_slot(prop));
}

static inline uint16_t fenykl_gui_state(void)
{
	return (uint16_t)(fenykl_rd8(FW_GUI_SUPER) << 8 | fenykl_rd8(FW_GUI_STATE));
}

static inline bool fenykl_gui_live_view(void)
{
	return fenykl_gui_state() == (FENYKL_GUI_SS_LV << 8 | FENYKL_GUI_ST_LV);
}

static inline bool fenykl_gui_playback(void)
{
	return FENYKL_GUI_SUPER(fenykl_gui_state()) == FENYKL_GUI_SS_PLAY;
}

static inline bool fenykl_gui_menu(void)
{
	return fenykl_rd8(FW_GUI_SUPER) == FENYKL_GUI_SS_MENU;
}

#if FENYKL_HAS_LENS
#define FBODY_PROP_AV 0xA03Au		/* setting 3001: shooting Av, u16, written on CPU0 */

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
	uint32_t p = fbody_prop_obj(FBODY_PROP_AV);

	return fenykl_dram_ptr(p, 2) ? fenykl_rd16(p) : 0;
}

/* A raw word of unknown scale on this body (firmware.toml av_effect). */
static inline uint32_t fenykl_lens_iris_av(void)
{
	return fenykl_rd16(FW_AV_EFFECT);
}
#endif

#if FENYKL_HAS_BATT
#define FBODY_SRC_BATT 0x200u

/* The value the 1 Hz source check stored: no A/D request, never busy, does not block. */
static inline int fenykl_batt_read(uint32_t *v)
{
	if (!fenykl_rd8(FW_POWER_SRC_CHECK))
		return FENYKL_BATT_ESTANDBY;
	*v = fenykl_rd16(FW_BATT_VBAT);
	return 0;
}

static inline uint32_t fenykl_power_src(void)
{
	return fenykl_rd16(FW_POWER_SRC);
}

static inline bool fenykl_power_src_batt(uint32_t src)
{
	return src == FBODY_SRC_BATT;
}
#endif

/* take_focus, for the afc and shot groups: u8 movie at +8 */
#define FBODY_TF_MOVIE 0x08u

static inline uint32_t fbody_tf(void)
{
	return fenykl_rd32(FW_TAKE_FOCUS_PTR);
}

#if FENYKL_HAS_AFC
#define FBODY_TF_MODE 0x06u		/* take_focus u8 mode_focus: 0 S-AF and S-AF+MF, 1 MF, 2 C-AF */
#define FBODY_TF_MF 1u
#define FBODY_PROP_AREA 0x84D1u		/* u8 AF area mode */
#define FBODY_PROP_TARGET 0x88B2u	/* u16 AF target index, 9 per row */
#define FBODY_AREA_ALL 2u		/* the E-PL3's value, not seen on this body */
#define FBODY_TARGETS 81u
#define FBODY_FCS_RUNNING 2u
#define FBODY_CTX_REQ 0xC2Cu		/* fcs's cam_request object */
#define FBODY_CTX_SETTING 0xD24u
#define FBODY_GRID 9u			/* pattern 0: 9 x 9 areas */

#define FENYKL_CNT_SET_SIZE 0x228u
#define FENYKL_CNT_SET_INFO 0x18Cu	/* the driver clears 0x9C result bytes from here at attach */
#define FENYKL_CNT_SET_NUMBER 0x00u
#define FENYKL_CNT_REQ_SIZE 0x88u
#define FENYKL_CNT_EV_ST 0x001u		/* frame start */
#define FENYKL_CNT_EV_CP 0x002u		/* data complete */
#define FENYKL_CNT_EV_ERR 0x200u
/* cnt (4 channels) at word 0, fc (3 channels) at FBODY_DMA_FC, 9 rows of 16 words per channel; a
   10th row after either output's last channel still fits, with 256 bytes to spare. */
#define FENYKL_CNT_DMA_WORDS 1152u
#define FENYKL_CNT_DMA_ALIGN 128u	/* as fcs's buffers 0xBB544E80 / 0xBB548E80 */
#define FENYKL_CNT_DMA_STATS FBODY_DMA_FC
#define FENYKL_CNT_STATS_CHANNELS 3u	/* fc: HPF1..3, the values fcs evaluates */
#define FBODY_DMA_FC 640u

/* contrast_setting */
#define FBODY_SET_UPDATE 0x08u		/* u32 update bits, cleared by cam_cnt_apply 0x60243E6F */
/* The bits fcs sets (every `or` into the word in 0x603F0000..0x60460000); cam_cnt_apply also knows
   0x1000 and 0x2000..0x10000, whose registers fcs never programs. */
#define FBODY_SET_ALL 0x60FFFu
#define FBODY_SET_VDIV 0x7Cu		/* u16 */
#define FBODY_SET_AREA_ADD 0x8Au	/* u8: DMA rows = vdiv - area_add (0x60241980) */
#define FBODY_SET_CNT_DMA 0x8Cu
#define FBODY_SET_FC_DMA 0x90u
#define FBODY_SET_ENDLESS 0xFFFFu

/* request */
#define FBODY_REQ_TYPE 0x00u		/* u16 7 */
#define FBODY_REQ_FLAG 0x02u		/* u8 1 */
#define FBODY_REQ_SETTING 0x1Cu
#define FBODY_REQ_DONE 0x38u
#define FBODY_REQ_FRAME 0x4Cu
#define FBODY_REQ_EVENTS 0x6Cu

/* client list node */
#define FBODY_NODE_SETTING 0x00u
#define FBODY_NODE_NEXT 0x0Cu
#define FBODY_NODES 2u			/* the driver's node pool */
#define FBODY_DRAM(a) ((a) & 0x1FFFFFFFu)	/* cached 0x6/0x7, uncached 0xA/0xB */

static inline bool fenykl_af_mf(void)
{
	uint32_t tf = fbody_tf();

	return tf && fenykl_rd8(tf + FBODY_TF_MODE) == FBODY_TF_MF;
}

static inline bool fenykl_af_half(void)
{
	return fenykl_rd8(FW_TRG_HALF) == 1;
}

static inline int fenykl_af_target_area(void)
{
	uint32_t p = fbody_prop_obj(FBODY_PROP_TARGET), t;

	if (!fenykl_dram_ptr(p, 2))
		return FENYKL_AF_AREA_NONE;
	t = fenykl_rd16(p);
	if (t < FBODY_TARGETS && fenykl_rd8(FW_TAKE_FCS_AF_MODE2FCS + 2 * t + 1))
		return (int)fenykl_rd8(FW_TAKE_FCS_AF_MODE2FCS + 2 * t);
	return FENYKL_AF_AREA_NONE;
}

static inline bool fenykl_af_all_targets(void)
{
	uint32_t p = fbody_prop_obj(FBODY_PROP_AREA);

	return fenykl_dram_ptr(p, 1) && fenykl_rd8(p) == FBODY_AREA_ALL;
}

/* fcs's fc buffer: the values fcs evaluates. fcs allocates it once and never frees it. */
static inline bool fenykl_af_grid(struct fenykl_af_grid *g)
{
	uint32_t buf = fenykl_rd32(FW_FCS_FC_BUF);

	if (!buf)
		return false;
	g->buf = (const volatile uint32_t *)buf;
	g->chan_words = fenykl_rd32(FW_FCS_AREA_SIZE);
	g->cols = (uint8_t)fenykl_rd8(FW_FCS_COLS);
	g->rows = (uint8_t)fenykl_rd8(FW_FCS_ROWS);
	g->pattern = (uint8_t)fenykl_rd8(FW_FCS_PATTERN);
	g->cut3 = (uint16_t)fenykl_rd16(FW_FCS_CUT3);
	return true;
}

static inline uint32_t fenykl_cnt_fcs_setting(uint8_t *pat, uint8_t *cols, uint8_t *rows)
{
	uint32_t tf = fbody_tf(), ctx = fenykl_rd32(FW_FCS_CONTRAST_CTX);
	uint32_t set = ctx + FBODY_CTX_SETTING;

	if (!tf || !ctx || fenykl_rd8(FW_FCS_AF_EVAL_STATE) != FBODY_FCS_RUNNING || fenykl_af_half())
		return 0;
	if (fenykl_rd8(tf + FBODY_TF_MODE) == FBODY_TF_MF || fenykl_rd8(tf + FBODY_TF_MOVIE))
		return 0;
	if (fenykl_rd8(FW_FCS_PATTERN) != 0 || fenykl_rd8(FW_FCS_COLS) != FBODY_GRID ||
	    fenykl_rd8(FW_FCS_ROWS) != FBODY_GRID)
		return 0;
	if (fenykl_rd16(set + FBODY_SET_VDIV) - fenykl_rd8(set + FBODY_SET_AREA_ADD) != FBODY_GRID)
		return 0;
	if (fenykl_rd16(set + FENYKL_CNT_SET_NUMBER) != FBODY_SET_ENDLESS ||
	    fenykl_rd32(set + FBODY_SET_CNT_DMA) != fenykl_rd32(FW_FCS_CNT_BUF) ||
	    fenykl_rd32(set + FBODY_SET_FC_DMA) != fenykl_rd32(FW_FCS_FC_BUF) ||
	    fenykl_rd32(ctx + FBODY_CTX_REQ + FBODY_REQ_SETTING) != set)
		return 0;
	*pat = 0;
	*cols = FBODY_GRID;
	*rows = FBODY_GRID;
	return set;
}

/* Both DMA pointers to the client's buffer: a copy left pointing at fcs's would overwrite them. */
static inline void fenykl_cnt_setting_prep(uint8_t *set, uint32_t dma)
{
	*(volatile uint16_t *)(set + FENYKL_CNT_SET_NUMBER) = FBODY_SET_ENDLESS;
	*(uint32_t *)(set + FBODY_SET_UPDATE) = FBODY_SET_ALL;
	*(uint32_t *)(set + FBODY_SET_CNT_DMA) = dma;
	*(uint32_t *)(set + FBODY_SET_FC_DMA) = dma + 4u * FBODY_DMA_FC;
}

/* No callback argument word on this body: the callbacks get the request in A0. */
static inline void fenykl_cnt_req_prep(void *req, uint32_t set, uint32_t frame_cb, uint32_t done_cb,
				       uint32_t arg)
{
	uint8_t *r = req;

	(void)arg;
	*(uint16_t *)(r + FBODY_REQ_TYPE) = 7;
	r[FBODY_REQ_FLAG] = 1;
	*(uint32_t *)(r + FBODY_REQ_SETTING) = set;
	*(uint32_t *)(r + FBODY_REQ_DONE) = done_cb;
	*(uint32_t *)(r + FBODY_REQ_FRAME) = frame_cb;
	*(uint32_t *)(r + FBODY_REQ_EVENTS) = FENYKL_CNT_EV_ST | FENYKL_CNT_EV_CP | FENYKL_CNT_EV_ERR;
}

/* Walks the driver's client list; more nodes than its pool holds counts as foreign. */
static inline bool fenykl_cnt_foreign(uint32_t set)
{
	uint32_t unit = fenykl_rd32(FW_CNT_UNIT), n;

	if (!unit)
		return false;
	n = fenykl_rd32(unit + 4u);
	for (unsigned int i = 0; n; i++) {
		if (i == FBODY_NODES ||
		    FBODY_DRAM(fenykl_rd32(n + FBODY_NODE_SETTING)) != FBODY_DRAM(set))
			return true;
		n = fenykl_rd32(n + FBODY_NODE_NEXT);
	}
	return false;
}

static inline bool fenykl_cnt_lv(void)
{
	return fenykl_gui_live_view();
}

/* No live-view mode flag like the E-PL3's take_focus +0xD4 was found. */
static inline bool fenykl_cnt_movie(void)
{
	uint32_t tf = fbody_tf();

	return !tf || fenykl_rd8(tf + FBODY_TF_MOVIE);
}

/* No driver debug mode word like the E-PL3's 0x605AEFD4 was found. */
static inline bool fenykl_cnt_drv_dbg(void)
{
	return false;
}
#endif

#if FENYKL_HAS_WB_BASE
#define FENYKL_WB_BASE_N 3u
#define FENYKL_WB_MAT_N 9u
/* u8 WB kind: 0 auto, 1 preset, 2 one-touch, 3 custom Kelvin, 4 Underwater (inferred from the
   kind map of 0x604F0F5A and the paired 0/4 branches at 0x60217F0F, 0x6051CC3B) */
#define FBODY_PROP_WB 0x8121u

/*
 * 0xAA6A: base of the preset and one-touch light matrices (wb_get_matrix 0x602117D7 at 0x60211815);
 * 0xAA4A: of the other family (0x6021184F) and the sRGB take matrix table (0x60510C29..); 0xAA5A:
 * of the AdobeRGB take table (0x60510D6D..). No other code names them. The firmware reads them
 * through the registry at every use, CPU1/CPU2 through to_uncached, which keeps 0xA... addresses.
 * Their stock objects are constants in the firmware image (descriptors 0x600A64DC, 0x600A64F4,
 * 0x600A650C of the table 0x6009ED3C, registered with no data block), in no saved block.
 */
static inline unsigned int fbody_wb_base_prop(unsigned int i)
{
	static const uint16_t prop[FENYKL_WB_BASE_N] = { 0xAA6Au, 0xAA4Au, 0xAA5Au };

	return prop[i];
}

static inline uint32_t fenykl_wb_base_slot(unsigned int i)
{
	return fbody_prop_slot(fbody_wb_base_prop(i));
}

/* u16 id, u16 element count: the registry fill 0x600760D6 copies both from the descriptor. */
static inline uint32_t fenykl_wb_base_head(uint32_t slot)
{
	return fenykl_rd32(slot - 4u);
}

static inline bool fenykl_wb_base_entry_ok(unsigned int i, uint32_t slot)
{
	return fenykl_wb_base_head(slot) == (FENYKL_WB_MAT_N << 16 | fbody_wb_base_prop(i));
}

static inline int fenykl_wb_mode(void)
{
	uint32_t p = fbody_prop_obj(FBODY_PROP_WB);

	return fenykl_dram_ptr(p, 1) ? (int)fenykl_rd8(p) : -1;
}

static inline void fenykl_wb_lv_matrix(int16_t *m)
{
	for (unsigned int i = 0; i < FENYKL_WB_MAT_N; i++)
		m[i] = (int16_t)fenykl_rd16(FW_WB_LV_CMX + 2u * i);
}
#endif

#if FENYKL_HAS_SHOT
/* LCD 720 x 480 at 1:1; EVF 1024 x 768 with the OSD's top-left 640 x 480 scaled up 1.6x. Both
   panels have square pixels. */
#define FENYKL_DISP_W_MAX 1024u
#define FENYKL_DISP_H_MAX 768u
#define FENYKL_OSD_LAYERS 3u
#define FENYKL_OSD_PLANE_W 896u
#define FENYKL_OSD_PLANE_H 600u
/* Storage: every OSD buffer is 896 x 600 bytes and layers 0/2 have 32-entry palettes, although
   gpi_layer_bpp reads 4/8/4 (the GUI's colour class). */
#define FENYKL_OSD_BPP { 8, 8, 8 }
/* osd_palette: layer 1 256 entries, layer 0 32 (+0x400), layer 2 32 (+0x480); {R, G, B, A 0..32} */
#define FENYKL_OSD_PAL_ENTRIES { 32, 256, 32 }
#define FENYKL_OSD_PAL_FIRST { 256, 0, 288 }
#define FENYKL_OSD_PAL_TOTAL 320u
#define FENYKL_OSD_PAL_YCC 0
#define FENYKL_OSD_ALPHA_MAX 32u
/* The display shadow keeps the old buffer from gpi_flush's toggle until the DISPLAY task commits. */
#define FENYKL_OSD_SHADOW_LAGS 1
#define FENYKL_OSD_STACK { 2, 0, 1 }	/* mix code 3: L1 over L0 over L2 */
/* img3 (face/AF frames) is not read: its source geometry and palette are not known */
#define FENYKL_FRAMES_PITCH 0u
#define FENYKL_FRAMES_H 0u
#define FENYKL_FRAMES_PAL 0u
/* 16-row strips in .bss; the snapshot in PRIM block 0 */
#define FENYKL_SHOT_STRIP_ROWS 16u
#define FENYKL_SHOT_STRIP_LENT 0
#define FENYKL_SHOT_REGS {						\
	{ "disp_device", FW_DISP_DEVICE, 4 },				\
	{ "disp_state", FW_DISP_STATE, 1 },				\
	{ "layer_en", FW_DISP_LAYER_EN, 4 },				\
	{ "img1_latched", FW_DISP_IMG_LATCHED, 4 },			\
	{ "img2_latched", FW_DISP_IMG_LATCHED + 4, 4 },			\
	{ "img3_latched", FW_DISP_IMG_LATCHED + 8, 4 },			\
	{ "img1_jump", FW_DISP_IMG_JUMP, 4 },				\
	{ "img2_jump", FW_DISP_IMG_JUMP + 4, 4 },			\
	{ "img1_x", FW_DISP_IMG1_X, 4 },				\
	{ "img1_y", FW_DISP_IMG1_Y, 4 },				\
	{ "img1_h", FW_DISP_IMG1_H, 4 },				\
	{ "mix", FW_DISP_MIX, 4 },					\
	{ "osd2_alpha", FW_DISP_OSD_ALPHA, 4 },				\
	{ "osd1_alpha", FW_DISP_OSD_ALPHA + 4, 4 },			\
	{ "osd0_alpha", FW_DISP_OSD_ALPHA + 8, 4 },			\
	{ "osd0_shadow", FW_OSD_SHADOW, 4 },				\
	{ "osd1_shadow", FW_OSD_SHADOW + 4, 4 },			\
	{ "osd2_shadow", FW_OSD_SHADOW + 8, 4 },			\
	{ "gpi_layer_bpp", FW_GPI_LAYER_BPP, 4 },			\
	{ "gpi_draw_buf", FW_GPI_DRAW_BUF, 1 },				\
	{ "gpi_hide_mask", FW_GPI_HIDE_MASK, 2 },			\
	{ "gpi_one_mode", FW_GPI_ONE_MODE, 1 },				\
	{ "prim_used", FW_POOL_PRIM + FBODY_POOL_USED, 4 },		\
	{ "prim_free", FW_POOL_PRIM + FBODY_POOL_FREE, 2 },		\
	{ "prim_total", FW_POOL_PRIM + FBODY_POOL_TOTAL, 2 },		\
	{ "prim_reserve", FW_POOL_PRIM + FBODY_PRIM_RESERVE, 4 },	\
	{ "imgwork_mode", FW_IMGWORK_MODE, 2 },				\
	{ "pool0e00_used", FW_POOL_0E00 + FBODY_POOL_USED, 4 },		\
	{ "pool0e00_free", FW_POOL_0E00 + FBODY_POOL_FREE, 2 },		\
	{ "gui_super", FW_GUI_SUPER, 1 },				\
	{ "gui_state", FW_GUI_STATE, 1 },				\
	{ "trg_half", FW_TRG_HALF, 1 },					\
	{ "take_state", FW_TAKE_STATE, 2 },				\
	{ "seq_inflight", FW_SEQ_INFLIGHT, 2 },				\
	{ "shooting", FW_SHOOTING, 4 },					\
}

#define FBODY_DEV_LCD 1u		/* disp_device: tft */
#define FBODY_DEV_EVF 6u		/* bievf */
#define FBODY_DISP_LIVE 2u		/* disp_state */
#define FBODY_EN_IMG1 0x20u		/* disp_layer_en */
#define FBODY_EN_IMG2 0x10u
#define FBODY_PRIM_ID 0x0000u		/* imgbuff pool id of pool_prim */
#define FBODY_PRIM_BASE 0xA33E8C00u	/* block 0, uncached */
#define FBODY_PRIM_BLOCK 0x1895000u
#define FBODY_PRIM_RESERVE 0x40u	/* u32 imgwork reserve mask, blocks 4..11 or 9..11 */
#define FBODY_POOL_USED 0x04u		/* u32 in-use mask in a basic descriptor, bit n = block n */
#define FBODY_POOL_FREE 0x14u		/* u16 free blocks, in every descriptor */
#define FBODY_POOL_TOTAL 0x18u		/* u16 blocks */
#define FBODY_PROP_BEAUTY 0xE951u	/* != 0: takes go to pool 0x2700, over block 0 */

/* Pools registered over PRIM block 0's bytes with their own accounting (0x6050A0B9..0x6050B5B2):
   take alternatives 0x0600, 0x0800, 0x2700, 0x3E00, movie playback 0x4200, the rest without an
   allocation site of a fixed id. Bit i of fenykl_shot_cond's other_used = entry i. */
static const uint16_t fbody_block0_pools[] = {
	0x0200, 0x0300, 0x0400, 0x0600, 0x0800, 0x1F00, 0x2000,
	0x2700, 0x2A00, 0x3A00, 0x3E00, 0x4200, 0x4F00,
};

#define FBODY_BLOCK0_POOLS (sizeof(fbody_block0_pools) / sizeof(fbody_block0_pools[0]))

/* gpi_layer_vram's descriptor */
struct fbody_osd_vram {
	uint32_t pdata, plane;
	uint16_t w, h, x, y;
};

/* display shadow slot per layer: osd0 = L1, osd1 = L0, osd2 = L2 */
static const uint8_t fbody_osd_shadow_slot[FENYKL_OSD_LAYERS] = { 1, 0, 2 };

static inline uint32_t fenykl_cached(uint32_t a)
{
	return (a & 0x1FFFFFFFu) | 0x60000000u;
}

/* Cached 0x6/0x7, uncached 0xA/0xB, or as the display registers hold it, 0x2/0x3; above the code
   image, where the firmware's RAM and its LV and OSD buffers lie. */
static inline bool fenykl_dram(uint32_t a, uint32_t bytes)
{
	uint32_t p = a & 0x1FFFFFFFu;

	return !(a & 3u) && ((1u << (a >> 29)) & 0x2Au) && p >= (FW_IMAGE_END & 0x1FFFFFFFu) &&
	       bytes <= 0x20000000u - p;
}

static inline bool fenykl_disp_ready(void)
{
	return fenykl_rd8(FW_DISP_STATE) == FBODY_DISP_LIVE;
}

/* The OSD global alpha of layer l: 0x9C0002E0/E4/E8 belong to osd idx 2/1/0. */
static inline uint16_t fbody_osd_alpha(unsigned int l)
{
	return (uint16_t)(fenykl_rd32(FW_DISP_OSD_ALPHA + 4 * (2u - fbody_osd_shadow_slot[l])) &
			  0x1FFu);
}

static inline bool fenykl_disp_geom(struct fenykl_disp_geom *g)
{
	uint32_t dev = fenykl_rd32(FW_DISP_DEVICE), w = fenykl_rd32(FW_DISP_IMG1_X) & 0xFFFFu;
	uint32_t h = fenykl_rd32(FW_DISP_IMG1_H) & 0xFFFFu;
	uint32_t img2 = fenykl_rd32(FW_DISP_LAYER_EN) & FBODY_EN_IMG2 ? 1u : 0u;
	uint32_t pitch = 64u * fenykl_rd32(FW_DISP_IMG_JUMP + 4 * img2);

	if (dev == FBODY_DEV_LCD && w == 720 && h == 480) {
		g->osd_w = 720;
		g->osd_h = 480;
	} else if (dev == FBODY_DEV_EVF && w == 1024 && h == 768) {
		g->osd_w = 640;
		g->osd_h = 480;
	} else {
		return false;
	}
	if (pitch < 2 * w || pitch > 2 * FENYKL_DISP_W_MAX * 4)
		return false;
	g->w = g->square_w = (uint16_t)w;
	g->h = (uint16_t)h;
	g->lv_pitch = pitch;
	g->lv_bytes = pitch * h;
	for (unsigned int l = 0; l < FENYKL_OSD_LAYERS; l++)
		g->osd_alpha[l] = fbody_osd_alpha(l);
	return true;
}

/* MENU points img2 at the live-view block shown when it opens and may turn img1 off. */
static inline uint32_t fenykl_disp_lv(void)
{
	uint32_t en = fenykl_rd32(FW_DISP_LAYER_EN);

	if (en & FBODY_EN_IMG2)
		return fenykl_rd32(FW_DISP_IMG_LATCHED + 4);
	if (en & FBODY_EN_IMG1)
		return fenykl_rd32(FW_DISP_IMG_LATCHED);
	return 0;
}

/* All layers share one window: no flip table. */
static inline bool fenykl_disp_lv_pos(uint32_t lv, uint32_t *x, uint32_t *y)
{
	if (!lv || lv != fenykl_disp_lv())
		return false;
	*x = 0;
	*y = 0;
	return true;
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

/* 8, or 0 when gpi_layer_bpp is not the 4/8/4 this reading rests on. */
static inline unsigned int fenykl_osd_bpp(unsigned int l)
{
	static const uint8_t cls[FENYKL_OSD_LAYERS] = { 4, 8, 4 };

	return fenykl_rd8(FW_GPI_LAYER_BPP + l) == cls[l] ? 8u : 0u;
}

static inline uint32_t fenykl_osd_draw_buf(void)
{
	return fenykl_rd8(FW_GPI_DRAW_BUF);
}

static inline uint32_t fenykl_osd_hide_mask(void)
{
	return fenykl_rd16(FW_GPI_HIDE_MASK);
}

/* w/h change on a one-layer flip and pdata reads 0 while the OSD is off: only pdata is checked. */
static inline uint32_t fenykl_osd_buf(unsigned int l, unsigned int b)
{
	uint32_t h = fenykl_rd32(FW_GPI_LAYER_VRAM + 4 * (l * 2 + b));
	const volatile struct fbody_osd_vram *v =
		(const volatile struct fbody_osd_vram *)(uintptr_t)h;

	if (!fenykl_dram(h, sizeof(*v)) ||
	    !fenykl_dram(v->pdata, FENYKL_OSD_PLANE_W * FENYKL_OSD_PLANE_H))
		return 0;
	return v->pdata;
}

static inline uint32_t fenykl_osd_shown(unsigned int l)
{
	return fenykl_rd32(FW_OSD_SHADOW + 4 * fbody_osd_shadow_slot[l]);
}

static inline const uint8_t *fenykl_osd_palettes(void)
{
	return (const uint8_t *)FW_OSD_PALETTE;
}

static inline uint32_t fenykl_frames_shown(void)
{
	return 0;
}

static inline void fenykl_frames_geom(uint16_t *w, uint16_t *h, uint16_t *to_w, uint16_t *to_h)
{
	*w = *h = *to_w = *to_h = 0;
}

static inline const uint8_t *fenykl_frames_palette(void)
{
	return NULL;
}

/* The pools over block 0 not idle (free != total, or no descriptor), a mask of fbody_block0_pools. */
static inline uint32_t fbody_block0_busy(void)
{
	uint32_t busy = 0;

	for (unsigned int i = 0; i < FBODY_BLOCK0_POOLS; i++) {
		uint32_t d = fenykl_rd32(FW_POOL_TABLE + 8u * (fbody_block0_pools[i] >> 8));

		if (!d || fenykl_rd16(d + FBODY_POOL_FREE) != fenykl_rd16(d + FBODY_POOL_TOTAL))
			busy |= 1u << i;
	}
	return busy;
}

/* PRIM is the still path's own accounted pool, so a still taken while block 0 is held uses the
   next block: no half-press or take rule. pool_used: PRIM frames in flight (the imgwork reserve
   excluded); an unregistered prop 0xE951 counts as set. */
static inline void fenykl_shot_cond(struct fenykl_shot_cond *c)
{
	uint32_t tf = fbody_tf(), beauty = fbody_prop_obj(FBODY_PROP_BEAUTY);

	c->ready = fenykl_disp_ready();
	c->movie = !tf || fenykl_rd8(tf + FBODY_TF_MOVIE);
	c->playback = fenykl_gui_playback();
	c->still_shared = !fenykl_dram_ptr(beauty, 1) || fenykl_rd8(beauty);
	c->mode = fenykl_rd32(FW_DISP_DEVICE);
	c->buf_free = fenykl_rd16(FW_POOL_PRIM + FBODY_POOL_FREE);
	c->pool_used = fenykl_rd32(FW_POOL_PRIM + FBODY_POOL_USED) &
		       ~fenykl_rd32(FW_POOL_PRIM + FBODY_PRIM_RESERVE);
	c->other_used = fbody_block0_busy();
}

/* PRIM block 0: imgbuff_alloc takes the lowest free block (id 1 + index); any other block is given
   back at once. The handle is the block id. Task context: imgbuff_alloc may sleep on its lock. */
static inline uint32_t fenykl_shot_buf_alloc(uint32_t bytes, uint32_t *handle)
{
	uint16_t id = 0;

	if (bytes > FBODY_PRIM_BLOCK || fw_imgbuff_alloc(&id, FBODY_PRIM_ID))
		return 0;
	if ((id & 0xFFu) != 1u || fw_imgbuff_addr(id) != FBODY_PRIM_BASE) {
		fw_imgbuff_free(id);
		return 0;
	}
	*handle = id;
	return FBODY_PRIM_BASE;
}

static inline uint32_t fenykl_shot_buf_free(uint32_t handle)
{
	return fw_imgbuff_free(handle);
}
#endif

#endif
