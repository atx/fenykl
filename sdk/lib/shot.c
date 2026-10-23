/*
 * Screenshot (sdk/docs/shot.md). fenykl_shot_start takes a snapshot of what the panel shows: the
 * live view buffer on img1, the firmware's face/AF frames on img3 and the three GUI OSD layers with
 * their palettes, copied into a RECFILE block run lent for the save. Each later fenykl_shot_step
 * composes 24 rows (shot_pix.c) and writes them to C:\SHOTnnnn.PPM, so the plugin task keeps
 * ticking during the ~9 s write.
 */
#include <shot.h>
#include "shot_int.h"

#define SHOT_LV_TRIES 3u
#define SHOT_FLIP_WAIT_MS 200u
#define SHOT_OSD_TRIES 5u
#define SHOT_FRAMES_TRIES 3u

#define SHOT_STRIP_ROWS 24u
#define SHOT_STRIPS (FENYKL_DISP_H / SHOT_STRIP_ROWS)
#define SHOT_OUT_ROW (3u * FENYKL_DISP_SQUARE_W)
#define SHOT_FILES 10000u

/* snapshot layout in the lent buffer */
#define SHOT_LV_ROW (2u * FENYKL_DISP_W)
#define SHOT_OFF_L0 (SHOT_LV_ROW * FENYKL_DISP_H)
#define SHOT_OFF_L1 (SHOT_OFF_L0 + FENYKL_DISP_W / 2 * FENYKL_DISP_H)
#define SHOT_OFF_L2 (SHOT_OFF_L1 + FENYKL_DISP_W * FENYKL_DISP_H)
#define SHOT_OFF_FRAMES (SHOT_OFF_L2 + FENYKL_DISP_W / 2 * FENYKL_DISP_H)
#define SHOT_OFF_STRIP (SHOT_OFF_FRAMES + FENYKL_FRAMES_PITCH * FENYKL_FRAMES_H)
#define SHOT_BYTES (SHOT_OFF_STRIP + SHOT_OUT_ROW * SHOT_STRIP_ROWS)

_Static_assert(FENYKL_SHOT_LAYERS == FENYKL_OSD_LAYERS, "layers");

struct shot_state {
	bool scanned;			/* next_file found on the card */
	unsigned int next_file;
	void *fp;
	uint32_t buf;			/* lent RECFILE buffer (uncached), 0 = none */
	unsigned int strip;		/* next strip to write */
	uint32_t t0, capture_ms;
	unsigned int file;
	struct fenykl_shot_frame frame;
	uint8_t pal[FENYKL_OSD_PAL_TOTAL * 4];	/* RGBA */
	uint8_t frames_pal[FENYKL_FRAMES_PAL * 4];
	uint8_t canvas[3 * FENYKL_DISP_W];
	/* counters for the status */
	uint32_t shots, failed, lv_torn, osd_torn, frames_torn, no_lv;
};

static struct shot_state shot_st;

FENYKL_LOG_MODULE(fenykl_shot_log, "shot", FENYKL_LOG_INFO);

static const uint8_t shot_stack[FENYKL_SHOT_LAYERS] = FENYKL_OSD_STACK;
static const uint8_t shot_bpp[FENYKL_SHOT_LAYERS] = FENYKL_OSD_BPP;
static const uint16_t shot_entries[FENYKL_SHOT_LAYERS] = FENYKL_OSD_PAL_ENTRIES;
static const uint16_t shot_pal_off[FENYKL_SHOT_LAYERS] = FENYKL_OSD_PAL_FIRST;
static const uint32_t shot_off[FENYKL_SHOT_LAYERS] = {
	SHOT_OFF_L0, SHOT_OFF_L1, SHOT_OFF_L2,
};

/* --- copies --- */

/*
 * Copies rows of words from src (cached alias, between two flushes) to dst (uncached) and returns
 * how often watch() changed, checked after each row.
 */
static unsigned int shot_copy(uint32_t dst, uint32_t src, uint32_t pitch, uint32_t dst_pitch,
			      unsigned int words, unsigned int rows, uint32_t (*watch)(void))
{
	uint32_t last = watch();
	unsigned int changes = 0;

	fenykl_dcache_flush();
	for (unsigned int y = 0; y < rows; y++) {
		const volatile uint32_t *s = (const volatile uint32_t *)(uintptr_t)(src + y * pitch);
		volatile uint32_t *d = (volatile uint32_t *)(uintptr_t)(dst + y * dst_pitch);
		uint32_t now;

		for (unsigned int x = 0; x < words; x++)
			d[x] = s[x];
		now = watch();
		if (now != last)
			changes++;
		last = now;
	}
	fenykl_dcache_flush();
	return changes;
}

/* LV placement of the shown buffer: its flip table entry's x, y. */
static void shot_lv_place(struct fenykl_shot_frame *f, uint32_t lv)
{
	uint32_t x = 0, y = 0;

	fenykl_disp_lv_pos(lv, &x, &y);
	f->lv_x = (uint16_t)((x < FENYKL_DISP_W ? x : FENYKL_DISP_W) & ~1u);
	f->lv_y = (uint16_t)(y < FENYKL_DISP_H ? y : FENYKL_DISP_H);
	f->lv_w = (uint16_t)(FENYKL_DISP_W - f->lv_x);
	f->lv_h = (uint16_t)(FENYKL_DISP_H - f->lv_y);
	fenykl_log_dbg(&fenykl_shot_log, "lv %08x at %u,%u width %u", lv, f->lv_x, f->lv_y,
		       f->lv_w);
	fenykl_log_dbg(&fenykl_shot_log, "img1 regs x %08x y %08x mix %08x",
		       fenykl_disp_img1_x(), fenykl_disp_img1_y(), fenykl_disp_mix());
}

static uint32_t shot_wait_flip(void)
{
	uint32_t lv = fenykl_disp_lv();

	for (unsigned int i = 0; i < SHOT_FLIP_WAIT_MS; i++) {
		uint32_t now;

		fenykl_task_sleep(1);
		now = fenykl_disp_lv();
		if (now != lv)
			return now;
	}
	return lv;
}

/*
 * The shown LV buffer is next written by the pass after the second flip from now: a copy
 * started at a flip is whole if it saw at most one more.
 */
static void shot_copy_lv(struct shot_state *st)
{
	struct fenykl_shot_frame *f = &st->frame;
	unsigned int flips = 0, tries;
	uint32_t lv = 0, t = 0;

	f->lv = NULL;
	for (tries = 1; tries <= SHOT_LV_TRIES; tries++) {
		lv = shot_wait_flip();
		if (!fenykl_dram(lv, FENYKL_DISP_LV_BYTES)) {
			st->no_lv++;
			fenykl_log_warn(&fenykl_shot_log, "img1 %08x not a DRAM buffer: OSD only", lv);
			return;
		}
		shot_lv_place(f, lv);
		t = FENYKL_TIMER_NOW();
		flips = shot_copy(st->buf, fenykl_cached(lv), FENYKL_DISP_LV_PITCH,
				  SHOT_LV_ROW, f->lv_w / 2, f->lv_h, fenykl_disp_lv);
		t = FENYKL_TIMER_ELAPSED(t, FENYKL_TIMER_NOW());
		if (flips <= 1)
			break;
	}
	f->lv = FENYKL_UNCACHED(st->buf);
	f->lv_pitch = SHOT_LV_ROW;
	fenykl_log_dbg(&fenykl_shot_log, "lv copy %u us, flips %u, tries %u", t, flips,
		       tries > SHOT_LV_TRIES ? SHOT_LV_TRIES : tries);
	if (flips > 1) {
		st->lv_torn++;
		fenykl_log_warn(&fenykl_shot_log, "lv torn: %u flips in a %u us copy", flips, t);
	}
}

/* Shown buffer of layer l: the one the display shows, else draw ^ 1. The GUI's plane record
   sometimes fails the check in live view [camera]; the display's shown address is used then. */
static uint32_t shot_osd_shown(unsigned int l, uint32_t draw)
{
	uint32_t shadow = fenykl_osd_shown(l);
	uint32_t bytes = FENYKL_OSD_PLANE_W * FENYKL_OSD_PLANE_H * shot_bpp[l] / 8;
	uint32_t p[2];

	for (unsigned int b = 0; b < 2; b++) {
		p[b] = fenykl_osd_buf(l, b);
		if (p[b])
			continue;
		if (!fenykl_dram(shadow, bytes)) {
			fenykl_log_err(&fenykl_shot_log, "L%u buffer %u: not an OSD plane, shown %08x",
				       l, b, shadow);
			return 0;
		}
		fenykl_log_warn(&fenykl_shot_log, "L%u buffer %u: not an OSD plane, shown %08x used",
				l, b, shadow);
		return shadow;
	}
	for (unsigned int b = 0; b < 2; b++)
		if ((shadow & 0x1FFFFFFFu) == (p[b] & 0x1FFFFFFFu))
			return p[b];
	return p[(draw ^ 1u) & 1u];
}

/* The three shown OSD layers, copied until no GUI flip happened during the copy. */
static bool shot_copy_osd(struct shot_state *st)
{
	uint32_t src[FENYKL_SHOT_LAYERS];
	unsigned int flips = 0, tries;
	uint32_t hide = 0;

	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++)
		if (fenykl_osd_bpp(l) != shot_bpp[l]) {
			fenykl_log_err(&fenykl_shot_log, "layer %u bpp %u, expected %u", l,
				       fenykl_osd_bpp(l), shot_bpp[l]);
			return false;
		}
	for (tries = 1; tries <= SHOT_OSD_TRIES; tries++) {
		uint32_t draw = fenykl_osd_draw_buf();

		flips = 0;
		hide = fenykl_osd_hide_mask();
		for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++) {
			unsigned int row = FENYKL_DISP_W * shot_bpp[l] / 8;

			src[l] = shot_osd_shown(l, draw);
			if (!src[l])
				return false;
			flips += shot_copy(st->buf + shot_off[l], fenykl_cached(src[l]),
					   FENYKL_OSD_PLANE_W * shot_bpp[l] / 8, row, row / 4,
					   FENYKL_DISP_H, fenykl_osd_draw_buf);
		}
		if (fenykl_osd_draw_buf() != draw)
			flips++;
		if (!flips)
			break;
	}
	fenykl_shot_palette(fenykl_osd_palettes(), sizeof(st->pal) / 4, st->pal);
	for (unsigned int i = 0; i < FENYKL_SHOT_LAYERS; i++) {
		unsigned int l = shot_stack[i];
		struct fenykl_shot_layer *o = &st->frame.layer[i];

		o->pix = hide & (1u << (4 * l)) ? NULL : FENYKL_UNCACHED(st->buf + shot_off[l]);
		o->pitch = FENYKL_DISP_W * shot_bpp[l] / 8;
		o->bpp = shot_bpp[l];
		o->entries = shot_entries[l];
		o->pal = st->pal + 4 * shot_pal_off[l];
	}
	fenykl_log_dbg(&fenykl_shot_log, "osd L0 %08x L1 %08x L2 %08x", src[0], src[1], src[2]);
	fenykl_log_dbg(&fenykl_shot_log, "osd hide %03x, tries %u", hide,
		       tries > SHOT_OSD_TRIES ? SHOT_OSD_TRIES : tries);
	if (flips) {
		st->osd_torn++;
		fenykl_log_warn(&fenykl_shot_log, "osd torn: the GUI flipped during every copy");
	}
	return true;
}

/* The face/AF frame layer the display shows, copied until it did not flip during the copy. */
static void shot_copy_frames(struct shot_state *st)
{
	struct fenykl_shot_frames *fr = &st->frame.frames;
	unsigned int flips = 0, tries;
	uint32_t src = 0;

	fr->pix = NULL;
	fenykl_frames_geom(&fr->w, &fr->h, &fr->to_w, &fr->to_h);
	if (!fr->w || fr->w > FENYKL_FRAMES_PITCH || !fr->h || fr->h > FENYKL_FRAMES_H ||
	    !fr->to_w || fr->to_w > FENYKL_DISP_W || !fr->to_h || fr->to_h > FENYKL_DISP_H) {
		fenykl_log_warn(&fenykl_shot_log, "frames %ux%u -> %ux%u: not drawn", fr->w, fr->h,
				fr->to_w, fr->to_h);
		return;
	}
	for (tries = 1; tries <= SHOT_FRAMES_TRIES; tries++) {
		src = fenykl_frames_shown();
		if (!src) {
			fenykl_log_dbg(&fenykl_shot_log, "no frames layer");
			return;
		}
		if (!fenykl_dram(src, FENYKL_FRAMES_PITCH * fr->h)) {
			fenykl_log_warn(&fenykl_shot_log, "frames %08x not a DRAM buffer: not drawn", src);
			return;
		}
		flips = shot_copy(st->buf + SHOT_OFF_FRAMES, fenykl_cached(src), FENYKL_FRAMES_PITCH,
				  FENYKL_FRAMES_PITCH, (fr->w + 3) / 4, fr->h, fenykl_frames_shown);
		if (!flips)
			break;
	}
	for (unsigned int i = 0; i < sizeof(st->frames_pal); i++)
		st->frames_pal[i] = fenykl_frames_palette()[i];
	fr->pix = FENYKL_UNCACHED(st->buf + SHOT_OFF_FRAMES);
	fr->pitch = FENYKL_FRAMES_PITCH;
	fr->entries = FENYKL_FRAMES_PAL;
	fr->pal = st->frames_pal;
	fenykl_log_dbg(&fenykl_shot_log, "frames %08x, tries %u", src,
		       tries > SHOT_FRAMES_TRIES ? SHOT_FRAMES_TRIES : tries);
	fenykl_log_dbg(&fenykl_shot_log, "frames %ux%u -> %ux%u", fr->w, fr->h, fr->to_w, fr->to_h);
	if (flips) {
		st->frames_torn++;
		fenykl_log_warn(&fenykl_shot_log, "frames torn: flipped during every copy");
	}
}

/* --- card file and buffer --- */

static void shot_path(char *out, unsigned int n)
{
	fenykl_sprintf(out, "C:\\SHOT%04u.PPM", n);
}

static bool shot_exists(unsigned int n)
{
	char path[20];
	void *fp;

	shot_path(path, n);
	fp = fenykl_fopen(path, "r");
	if (!fp)
		return false;
	fenykl_fclose(fp);
	return true;
}

/* End of the run of files from SHOT0000: doubling, then bisection; gaps are found per shot. */
static unsigned int shot_scan(void)
{
	unsigned int lo, hi = 1;

	if (!shot_exists(0))
		return 0;
	while (hi < SHOT_FILES && shot_exists(hi))
		hi *= 2;
	lo = hi / 2;
	if (hi > SHOT_FILES)
		hi = SHOT_FILES;
	while (hi - lo > 1) {
		unsigned int mid = lo + (hi - lo) / 2;

		if (shot_exists(mid))
			lo = mid;
		else
			hi = mid;
	}
	return hi;
}

static void shot_end(struct shot_state *st)
{
	if (st->fp)
		fenykl_fclose(st->fp);
	st->fp = NULL;
	if (st->buf)
		fenykl_recfile_free(st->buf);
	st->buf = 0;
}

static bool shot_fail(struct shot_state *st, const char *what)
{
	st->failed++;
	fenykl_log_err(&fenykl_shot_log, "SHOT%04u.PPM: %s", st->file, what);
	shot_end(st);
	return false;
}

static bool shot_write(struct shot_state *st, uint32_t data, unsigned int len)
{
	return fenykl_fwrite(FENYKL_UNCACHED(data), 1, len, st->fp) == (int)len;
}

/* --- API --- */

bool fenykl_shot_start(void)
{
	struct shot_state *st = &shot_st;
	char path[20];
	unsigned int n;

	if (!fenykl_card_io_allowed()) {
		fenykl_log_warn(&fenykl_shot_log, "card not available: no screenshot");
		return false;
	}
	st->t0 = FENYKL_TIMER_NOW();
	if (!st->scanned) {
		st->next_file = shot_scan();
		st->scanned = true;
	}
	while (st->next_file < SHOT_FILES && shot_exists(st->next_file))
		st->next_file++;
	if (st->next_file >= SHOT_FILES) {
		fenykl_log_warn(&fenykl_shot_log, "SHOT9999.PPM reached: no screenshot");
		return false;
	}
	st->file = st->next_file;
	if (!fenykl_recfile_fits(SHOT_BYTES)) {
		fenykl_log_warn(&fenykl_shot_log, "RECFILE has no free run of %u bytes: no screenshot",
				SHOT_BYTES);
		return false;
	}
	n = fenykl_recfile_alloc(SHOT_BYTES);
	if (!fenykl_dram(n, SHOT_BYTES)) {
		fenykl_log_err(&fenykl_shot_log, "recfile_alloc returned %08x", n);
		return shot_fail(st, "no buffer");
	}
	st->buf = n;
	shot_copy_lv(st);
	shot_copy_frames(st);
	if (!shot_copy_osd(st))
		return shot_fail(st, "no OSD planes");
	st->capture_ms = FENYKL_TIMER_ELAPSED(st->t0, FENYKL_TIMER_NOW()) / 1000u;
	shot_path(path, st->file);
	st->fp = fenykl_fopen(path, "w");
	if (!st->fp)
		return shot_fail(st, "cannot open");
	st->next_file++;
	n = fenykl_shot_ppm_header(FENYKL_UNCACHED(st->buf + SHOT_OFF_STRIP),
				   FENYKL_DISP_SQUARE_W, FENYKL_DISP_H);
	if (!shot_write(st, st->buf + SHOT_OFF_STRIP, n))
		return shot_fail(st, "write failed");
	st->strip = 0;
	return true;
}

bool fenykl_shot_step(void)
{
	struct shot_state *st = &shot_st;
	uint8_t *out = FENYKL_UNCACHED(st->buf + SHOT_OFF_STRIP);

	if (!fenykl_card_io_allowed()) {
		shot_fail(st, "card gone while writing");
		return false;
	}
	for (unsigned int r = 0; r < SHOT_STRIP_ROWS; r++) {
		fenykl_shot_compose(&st->frame, st->strip * SHOT_STRIP_ROWS + r, st->canvas);
		fenykl_shot_stretch(st->canvas, FENYKL_DISP_W, out + r * SHOT_OUT_ROW,
				    FENYKL_DISP_SQUARE_W);
	}
	if (!shot_write(st, st->buf + SHOT_OFF_STRIP,
			    SHOT_OUT_ROW * SHOT_STRIP_ROWS)) {
		shot_fail(st, "write failed");
		return false;
	}
	if (++st->strip < SHOT_STRIPS)
		return true;
	shot_end(st);
	st->shots++;
	fenykl_log_info(&fenykl_shot_log, "SHOT%04u.PPM written in %u ms (capture %u ms)", st->file,
			FENYKL_TIMER_ELAPSED(st->t0, FENYKL_TIMER_NOW()) / 1000u, st->capture_ms);
	return false;
}

void fenykl_shot_status(void)
{
	struct shot_state *st = &shot_st;

	fenykl_log_info(&fenykl_shot_log, "shots %u failed %u, torn lv %u osd %u", st->shots,
			st->failed, st->lv_torn, st->osd_torn);
	if (st->frames_torn)
		fenykl_log_info(&fenykl_shot_log, "  torn frames %u", st->frames_torn);
	if (st->no_lv)
		fenykl_log_info(&fenykl_shot_log, "  without live view %u", st->no_lv);
}
