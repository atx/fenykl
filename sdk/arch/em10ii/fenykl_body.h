/* E-M10 II part of body.h (included from there): the GUI, batt and afc groups on the v1.4
   firmware's data. The other groups are not implemented on this body (target.toml capabilities). */
#ifndef FENYKL_BODY_TARGET_H
#define FENYKL_BODY_TARGET_H

#define FENYKL_GUI_SS_LV 0x01u		/* super state of the live-view screens */
#define FENYKL_GUI_ST_LV 0x08u		/* its state while live view is shown */
#define FENYKL_GUI_SS_PLAY 0x06u	/* single playback, from static analysis only */

/* A property's object, 0 if unregistered: the lookup 0x60076078 (the override table first) without
   the getter 0x60076FD9's core-view mapping 0x600750FD, which is the identity on CPU0. */
static inline uint32_t fbody_prop_obj(unsigned int prop)
{
	uint32_t entry = 8u * (prop >> 4) + 4u, obj = 0;

	if (fenykl_rd16(FW_PROP_OVERRIDE_VALID))
		obj = fenykl_rd32(FW_PROP_OVERRIDE + entry);
	if (!obj)
		obj = fenykl_rd32(FW_PROP_TABLE + entry);
	return obj;
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

#if FENYKL_HAS_AFC
#define FBODY_TF_MODE 0x06u		/* take_focus u8 mode_focus: 0 S-AF and S-AF+MF, 1 MF, 2 C-AF */
#define FBODY_TF_MF 1u
#define FBODY_TF_MOVIE 0x08u
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

static inline uint32_t fbody_tf(void)
{
	return fenykl_rd32(FW_TAKE_FOCUS_PTR);
}

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

#endif
