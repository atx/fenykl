/*
 * main: the "screenshot" Fn action. The release of a short Fn in live view takes a
 * snapshot of what the panel shows: the live view buffer on img1 and the three GUI OSD layers with
 * their palettes, copied into a RECFILE block run lent for the save. Each later tick
 * composes 24 rows (main_shot.c) and writes them to C:\SHOTnnnn.PPM, so the other features keep
 * ticking during the ~3 s write.
 */
#include "main.h"
#include "main_shot.h"
#include <key.h>

/* live view display */
#define ACT_SHOT_FLIP_ENTRIES 3u
#define ACT_SHOT_LV_PITCH 1600u		/* 800x600 YUYV */
#define ACT_SHOT_LV_BYTES (ACT_SHOT_LV_PITCH * 600u)
#define ACT_SHOT_LV_TRIES 3u
#define ACT_SHOT_FLIP_WAIT_MS 200u
/* GUI OSD */
#define ACT_SHOT_PLANE_W 896u
#define ACT_SHOT_PLANE_H 600u
#define ACT_SHOT_OSD_TRIES 5u
/* RECFILE */
#define ACT_SHOT_RECFILE_BLOCKS 256u
#define ACT_SHOT_RECFILE_BLOCK 0x1E3C0u

#define ACT_SHOT_STRIP_ROWS 24u
#define ACT_SHOT_STRIPS (MAIN_SHOT_H / ACT_SHOT_STRIP_ROWS)
#define ACT_SHOT_OUT_ROW (3u * MAIN_SHOT_OUT_W)
#define ACT_SHOT_FILES 10000u

/* snapshot layout in the lent buffer */
#define ACT_SHOT_LV_ROW (2u * MAIN_SHOT_W)
#define ACT_SHOT_OFF_L0 (ACT_SHOT_LV_ROW * MAIN_SHOT_H)
#define ACT_SHOT_OFF_L1 (ACT_SHOT_OFF_L0 + MAIN_SHOT_W / 2 * MAIN_SHOT_H)
#define ACT_SHOT_OFF_L2 (ACT_SHOT_OFF_L1 + MAIN_SHOT_W * MAIN_SHOT_H)
#define ACT_SHOT_OFF_STRIP (ACT_SHOT_OFF_L2 + MAIN_SHOT_W / 2 * MAIN_SHOT_H)
#define ACT_SHOT_BYTES (ACT_SHOT_OFF_STRIP + ACT_SHOT_OUT_ROW * ACT_SHOT_STRIP_ROWS)
#define ACT_SHOT_NBLOCKS ((ACT_SHOT_BYTES + ACT_SHOT_RECFILE_BLOCK - 1) / ACT_SHOT_RECFILE_BLOCK)

struct act_shot_vram {
	uint32_t plane;
	uint16_t width, height;
	uint32_t pdata;
};

struct act_shot_state {
	bool scanned;			/* next_file found on the card */
	unsigned int next_file;
	void *fp;
	uint32_t buf;			/* lent RECFILE buffer (uncached), 0 = none */
	unsigned int strip;		/* next strip to write */
	uint32_t t0, capture_ms;
	unsigned int file;
	struct main_shot_frame frame;
	uint8_t pal[(256 + 16 + 8) * 4];	/* RGBA */
	uint8_t canvas[3 * MAIN_SHOT_W];
	/* counters for the status */
	uint32_t shots, failed, lv_torn, osd_torn, no_lv;
};

static struct act_shot_state act_shot_st;

static FENYKL_LOG_MODULE(act_shot_log, "shot", FENYKL_LOG_INFO);

/* bottom to top [inferred from camera OSD dumps; L1 on top (osdcard)]; 0x1A4 is logged at debug */
static const uint8_t act_shot_stack[MAIN_SHOT_LAYERS] = { 2, 0, 1 };
static const uint8_t act_shot_bpp[MAIN_SHOT_LAYERS] = { 4, 8, 4 };
static const uint16_t act_shot_entries[MAIN_SHOT_LAYERS] = { 16, 256, 8 };
static const uint16_t act_shot_pal_off[MAIN_SHOT_LAYERS] = { 256, 0, 272 };	/* entries */
static const uint32_t act_shot_off[MAIN_SHOT_LAYERS] = {
	ACT_SHOT_OFF_L0, ACT_SHOT_OFF_L1, ACT_SHOT_OFF_L2,
};
/* display shadow slot per layer: osd0 = L1, osd1 = L0, osd2 = L2 */
static const uint8_t act_shot_shadow_slot[MAIN_SHOT_LAYERS] = { 1, 0, 2 };

static uint8_t act_shot_rd8(uint32_t a)
{
	return *(volatile uint8_t *)(uintptr_t)a;
}

static uint16_t act_shot_rd16(uint32_t a)
{
	return *(volatile uint16_t *)(uintptr_t)a;
}

static uint32_t act_shot_rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

static uint32_t act_shot_cached(uint32_t a)
{
	return (a & 0x3FFFFFFFu) | 0x40000000u;
}

static bool act_shot_dram(uint32_t a, uint32_t bytes)
{
	uint32_t c = act_shot_cached(a);

	return (a & 3) == 0 && ((a >> 28) == 0x6 || (a >> 28) == 0xA) && c + bytes <= FW_IMAGE_START;
}

static bool act_shot_claim(uint16_t gui)
{
	return FENYKL_GUI_SUPER(gui) == FENYKL_GUI_SS_REC;
}

/* --- copies --- */

/*
 * Copies rows of words from src (cached alias, between two purges) to dst (uncached) and returns
 * how often the masked word at watch changed, checked after each row.
 */
static unsigned int act_shot_copy(uint32_t dst, uint32_t src, uint32_t pitch, uint32_t dst_pitch,
				  unsigned int words, unsigned int rows, uint32_t watch,
				  uint32_t mask)
{
	uint32_t last = act_shot_rd32(watch) & mask;
	unsigned int changes = 0;

	fw_dcache_purge_all_irqsafe();
	for (unsigned int y = 0; y < rows; y++) {
		const volatile uint32_t *s = (const volatile uint32_t *)(uintptr_t)(src + y * pitch);
		volatile uint32_t *d = (volatile uint32_t *)(uintptr_t)(dst + y * dst_pitch);
		uint32_t now;

		for (unsigned int x = 0; x < words; x++)
			d[x] = s[x];
		now = act_shot_rd32(watch) & mask;
		if (now != last)
			changes++;
		last = now;
	}
	fw_dcache_purge_all_irqsafe();
	return changes;
}

/* LV placement of the shown buffer: its flip table entry's x, y. */
static void act_shot_lv_place(struct main_shot_frame *f, uint32_t lv)
{
	uint32_t x = 0, y = 0;

	for (unsigned int i = 0; i < ACT_SHOT_FLIP_ENTRIES; i++) {
		uint32_t e = FW_MOV_DISP_FLIP_TBL + 16 * i;

		if ((act_shot_rd32(e) & 0x1FFFFFFFu) != (lv & 0x1FFFFFFFu))
			continue;
		x = act_shot_rd32(e + 4);
		y = act_shot_rd32(e + 8);
		break;
	}
	f->lv_x = (uint16_t)((x < MAIN_SHOT_W ? x : MAIN_SHOT_W) & ~1u);
	f->lv_y = (uint16_t)(y < MAIN_SHOT_H ? y : MAIN_SHOT_H);
	f->lv_w = (uint16_t)(MAIN_SHOT_W - f->lv_x);
	f->lv_h = (uint16_t)(MAIN_SHOT_H - f->lv_y);
	fenykl_log_dbg(&act_shot_log, "lv %08x at %u,%u width %u", lv, f->lv_x, f->lv_y,
		       f->lv_w);
	fenykl_log_dbg(&act_shot_log, "img1 regs x %08x y %08x mix %08x",
		       act_shot_rd32(FW_DISP_IMG1_X), act_shot_rd32(FW_DISP_IMG1_Y),
		       act_shot_rd32(FW_DISP_MIX));
}

static uint32_t act_shot_wait_flip(void)
{
	uint32_t lv = act_shot_rd32(FW_MOV_IMG1_SHADOW);

	for (unsigned int i = 0; i < ACT_SHOT_FLIP_WAIT_MS; i++) {
		uint32_t now;

		fw_OlyTaskSleep(1);
		now = act_shot_rd32(FW_MOV_IMG1_SHADOW);
		if (now != lv)
			return now;
	}
	return lv;
}

/*
 * The shown LV buffer is next written by the pass after the second flip from now: a copy
 * started at a flip is whole if it saw at most one more.
 */
static void act_shot_copy_lv(struct act_shot_state *st)
{
	struct main_shot_frame *f = &st->frame;
	unsigned int flips = 0, tries;
	uint32_t lv = 0, t = 0;

	f->lv = NULL;
	for (tries = 1; tries <= ACT_SHOT_LV_TRIES; tries++) {
		lv = act_shot_wait_flip();
		if (!act_shot_dram(lv, ACT_SHOT_LV_BYTES)) {
			st->no_lv++;
			fenykl_log_warn(&act_shot_log, "img1 %08x not a DRAM buffer: OSD only", lv);
			return;
		}
		act_shot_lv_place(f, lv);
		t = FENYKL_LOG_TIMER_NOW();
		flips = act_shot_copy(st->buf, act_shot_cached(lv), ACT_SHOT_LV_PITCH,
				      ACT_SHOT_LV_ROW, f->lv_w / 2, f->lv_h, FW_MOV_IMG1_SHADOW,
				      0xFFFFFFFFu);
		t = FENYKL_LOG_ELAPSED(t, FENYKL_LOG_TIMER_NOW());
		if (flips <= 1)
			break;
	}
	f->lv = FENYKL_UNCACHED(st->buf);
	f->lv_pitch = ACT_SHOT_LV_ROW;
	fenykl_log_dbg(&act_shot_log, "lv copy %u us, flips %u, tries %u", t, flips,
		       tries > ACT_SHOT_LV_TRIES ? ACT_SHOT_LV_TRIES : tries);
	if (flips > 1) {
		st->lv_torn++;
		fenykl_log_warn(&act_shot_log, "lv torn: %u flips in a %u us copy", flips, t);
	}
}

static uint32_t act_shot_osd_pdata(unsigned int l, unsigned int b)
{
	uint32_t h = act_shot_rd32(FW_GPI_LAYER_VRAM + 4 * (l * 2 + b));
	const volatile struct act_shot_vram *v = (const volatile struct act_shot_vram *)(uintptr_t)h;

	if (!act_shot_dram(h, sizeof(*v)) || v->width != ACT_SHOT_PLANE_W ||
	    v->height != ACT_SHOT_PLANE_H || !act_shot_dram(v->pdata, 0)) {
		fenykl_log_err(&act_shot_log, "L%u buffer %u: handle %08x not an OSD plane", l, b, h);
		return 0;
	}
	return v->pdata;
}

/* Shown buffer of layer l: the one its display shadow points at, else draw ^ 1. */
static uint32_t act_shot_osd_shown(unsigned int l, uint8_t draw)
{
	uint32_t shadow = act_shot_rd32(FW_OSD_SHADOW + 4 * act_shot_shadow_slot[l]);
	uint32_t p[2];

	for (unsigned int b = 0; b < 2; b++) {
		p[b] = act_shot_osd_pdata(l, b);
		if (!p[b])
			return 0;
	}
	for (unsigned int b = 0; b < 2; b++)
		if ((shadow & 0x1FFFFFFFu) == (p[b] & 0x1FFFFFFFu))
			return p[b];
	return p[(draw ^ 1u) & 1u];
}

/* The three shown OSD layers, copied until no GUI flip happened during the copy. */
static bool act_shot_copy_osd(struct act_shot_state *st)
{
	uint32_t src[MAIN_SHOT_LAYERS];
	unsigned int flips = 0, tries;
	uint16_t hide = 0;

	for (unsigned int l = 0; l < MAIN_SHOT_LAYERS; l++)
		if (act_shot_rd8(FW_GPI_LAYER_BPP + l) != act_shot_bpp[l]) {
			fenykl_log_err(&act_shot_log, "layer %u bpp %u, expected %u", l,
				       act_shot_rd8(FW_GPI_LAYER_BPP + l), act_shot_bpp[l]);
			return false;
		}
	for (tries = 1; tries <= ACT_SHOT_OSD_TRIES; tries++) {
		uint8_t draw = act_shot_rd8(FW_GPI_DRAW_BUF);

		flips = 0;
		hide = act_shot_rd16(FW_GPI_HIDE_MASK);
		for (unsigned int l = 0; l < MAIN_SHOT_LAYERS; l++) {
			unsigned int row = MAIN_SHOT_W * act_shot_bpp[l] / 8;

			src[l] = act_shot_osd_shown(l, draw);
			if (!src[l])
				return false;
			flips += act_shot_copy(st->buf + act_shot_off[l], act_shot_cached(src[l]),
					       ACT_SHOT_PLANE_W * act_shot_bpp[l] / 8, row, row / 4,
					       MAIN_SHOT_H, FW_GPI_DRAW_BUF, 0xFFu);
		}
		if (act_shot_rd8(FW_GPI_DRAW_BUF) != draw)
			flips++;
		if (!flips)
			break;
	}
	main_shot_palette((const uint8_t *)FW_OSD_PALETTES, sizeof(st->pal) / 4, st->pal);
	for (unsigned int i = 0; i < MAIN_SHOT_LAYERS; i++) {
		unsigned int l = act_shot_stack[i];
		struct main_shot_layer *o = &st->frame.layer[i];

		o->pix = hide & (1u << (4 * l)) ? NULL : FENYKL_UNCACHED(st->buf + act_shot_off[l]);
		o->pitch = MAIN_SHOT_W * act_shot_bpp[l] / 8;
		o->bpp = act_shot_bpp[l];
		o->entries = act_shot_entries[l];
		o->pal = st->pal + 4 * act_shot_pal_off[l];
	}
	fenykl_log_dbg(&act_shot_log, "osd L0 %08x L1 %08x L2 %08x", src[0], src[1], src[2]);
	fenykl_log_dbg(&act_shot_log, "osd hide %03x, tries %u", hide,
		       tries > ACT_SHOT_OSD_TRIES ? ACT_SHOT_OSD_TRIES : tries);
	if (flips) {
		st->osd_torn++;
		fenykl_log_warn(&act_shot_log, "osd torn: the GUI flipped during every copy");
	}
	return true;
}

/* --- card file and buffer --- */

static void act_shot_path(char *out, unsigned int n)
{
	fw_sprintf(out, "C:\\SHOT%04u.PPM", n);
}

static bool act_shot_exists(unsigned int n)
{
	char path[20];
	void *fp;

	act_shot_path(path, n);
	fp = fw_rpc_fopen(path, "r");
	if (!fp)
		return false;
	fw_rpc_fclose(fp);
	return true;
}

/* End of the run of files from SHOT0000: doubling, then bisection; gaps are found per shot. */
static unsigned int act_shot_scan(void)
{
	unsigned int lo, hi = 1;

	if (!act_shot_exists(0))
		return 0;
	while (hi < ACT_SHOT_FILES && act_shot_exists(hi))
		hi *= 2;
	lo = hi / 2;
	if (hi > ACT_SHOT_FILES)
		hi = ACT_SHOT_FILES;
	while (hi - lo > 1) {
		unsigned int mid = lo + (hi - lo) / 2;

		if (act_shot_exists(mid))
			lo = mid;
		else
			hi = mid;
	}
	return hi;
}

static bool act_shot_free_run(void)
{
	unsigned int run = 0;

	for (unsigned int i = 0; i < ACT_SHOT_RECFILE_BLOCKS; i++) {
		run = act_shot_rd8(FW_RECFILE_USED + i) ? 0 : run + 1;
		if (run >= ACT_SHOT_NBLOCKS)
			return true;
	}
	return false;
}

static void act_shot_end(struct act_shot_state *st)
{
	if (st->fp)
		fw_rpc_fclose(st->fp);
	st->fp = NULL;
	if (st->buf)
		fw_recfile_free(st->buf);
	st->buf = 0;
}

static int act_shot_fail(struct act_shot_state *st, const char *what)
{
	st->failed++;
	fenykl_log_err(&act_shot_log, "SHOT%04u.PPM: %s", st->file, what);
	act_shot_end(st);
	return -1;
}

static bool act_shot_write(struct act_shot_state *st, uint32_t data, unsigned int len)
{
	return fw_rpc_fwrite(FENYKL_UNCACHED(data), 1, len, st->fp) == (int)len;
}

/* --- action --- */

static int act_shot_start(uint32_t press_us)
{
	struct act_shot_state *st = &act_shot_st;
	char path[20];
	unsigned int n;

	(void)press_us;
	if (!fenykl_card_io_allowed()) {
		fenykl_log_warn(&act_shot_log, "card not available: no screenshot");
		return -1;
	}
	st->t0 = FENYKL_LOG_TIMER_NOW();
	if (!st->scanned) {
		st->next_file = act_shot_scan();
		st->scanned = true;
	}
	while (st->next_file < ACT_SHOT_FILES && act_shot_exists(st->next_file))
		st->next_file++;
	if (st->next_file >= ACT_SHOT_FILES) {
		fenykl_log_warn(&act_shot_log, "SHOT9999.PPM reached: no screenshot");
		return -1;
	}
	st->file = st->next_file;
	if (!act_shot_free_run()) {
		fenykl_log_warn(&act_shot_log, "RECFILE has no %u free blocks: no screenshot",
				ACT_SHOT_NBLOCKS);
		return -1;
	}
	n = fw_recfile_alloc(ACT_SHOT_BYTES);
	if (!act_shot_dram(n, ACT_SHOT_BYTES)) {
		fenykl_log_err(&act_shot_log, "recfile_alloc returned %08x", n);
		return act_shot_fail(st, "no buffer");
	}
	st->buf = n;
	act_shot_copy_lv(st);
	if (!act_shot_copy_osd(st))
		return act_shot_fail(st, "no OSD planes");
	st->capture_ms = FENYKL_LOG_ELAPSED(st->t0, FENYKL_LOG_TIMER_NOW()) / 1000u;
	act_shot_path(path, st->file);
	st->fp = fw_rpc_fopen(path, "w");
	if (!st->fp)
		return act_shot_fail(st, "cannot open");
	st->next_file++;
	n = main_shot_ppm_header(FENYKL_UNCACHED(st->buf + ACT_SHOT_OFF_STRIP), MAIN_SHOT_OUT_W,
				 MAIN_SHOT_H);
	if (!act_shot_write(st, st->buf + ACT_SHOT_OFF_STRIP, n))
		return act_shot_fail(st, "write failed");
	st->strip = 0;
	return 0;
}

/* One strip per tick; false when the file is done or failed. */
static bool act_shot_step(void)
{
	struct act_shot_state *st = &act_shot_st;
	uint8_t *out = FENYKL_UNCACHED(st->buf + ACT_SHOT_OFF_STRIP);

	if (!fenykl_card_io_allowed()) {
		act_shot_fail(st, "card gone while writing");
		return false;
	}
	for (unsigned int r = 0; r < ACT_SHOT_STRIP_ROWS; r++) {
		main_shot_compose(&st->frame, st->strip * ACT_SHOT_STRIP_ROWS + r, st->canvas);
		main_shot_stretch(st->canvas, MAIN_SHOT_W, out + r * ACT_SHOT_OUT_ROW,
				  MAIN_SHOT_OUT_W);
	}
	if (!act_shot_write(st, st->buf + ACT_SHOT_OFF_STRIP,
			    ACT_SHOT_OUT_ROW * ACT_SHOT_STRIP_ROWS)) {
		act_shot_fail(st, "write failed");
		return false;
	}
	if (++st->strip < ACT_SHOT_STRIPS)
		return true;
	act_shot_end(st);
	st->shots++;
	fenykl_log_info(&act_shot_log, "SHOT%04u.PPM written in %u ms (capture %u ms)", st->file,
			FENYKL_LOG_ELAPSED(st->t0, FENYKL_LOG_TIMER_NOW()) / 1000u, st->capture_ms);
	return false;
}

static void act_shot_status(void)
{
	struct act_shot_state *st = &act_shot_st;

	fenykl_log_info(&act_shot_log, "shots %u failed %u, torn lv %u osd %u", st->shots,
			st->failed, st->lv_torn, st->osd_torn);
	if (st->no_lv)
		fenykl_log_info(&act_shot_log, "  without live view %u", st->no_lv);
}

const struct main_fn_act act_shot = {
	.name = "screenshot",
	.log = &act_shot_log,
	.claim = act_shot_claim,
	.start = act_shot_start,
	.step = act_shot_step,
	.status = act_shot_status,
};
