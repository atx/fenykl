/*
 * Screenshot (sdk/docs/shot.md). fenykl_shot_start takes a snapshot of what the panel shows: the
 * live view buffer the display shows, the firmware's face/AF frames on img3 and the three GUI OSD
 * layers with their palettes, copied into firmware memory lent for the save (fenykl_shot_buf_alloc).
 * Each later fenykl_shot_step composes a strip of rows (shot_pix.c) and writes it to
 * C:\SHOTnnnn.PPM, then, with fenykl_shot_raw, the raw snapshot pieces, so the plugin task keeps
 * ticking during the write. The lent memory is given back first on every way out.
 */
#include <shot.h>
#include "shot_int.h"

#define SHOT_LV_TRIES 3u
#define SHOT_FLIP_WAIT_MS 200u
#define SHOT_OSD_TRIES 5u
#define SHOT_COMMIT_WAIT_MS 100u
#define SHOT_FRAMES_TRIES 3u
#define SHOT_FILES 10000u
#define SHOT_RAW_CHUNK 0x10000u
#define SHOT_PIECES 6u
#define SHOT_PATH_MAX 20u
#define SHOT_HOLD_MAX_MS 30000u

_Static_assert(FENYKL_SHOT_LAYERS == FENYKL_OSD_LAYERS, "layers");

/* A raw dump file: SHOTnnnn.<ext>. */
struct shot_piece {
	const char *ext;
	const uint8_t *p;
	uint32_t len;
	bool cached;		/* .bss: write the D-cache back before the file server reads it */
};

static const struct fenykl_shot_reg shot_regs[] = FENYKL_SHOT_REGS;

#define SHOT_REGS_N (sizeof(shot_regs) / sizeof(shot_regs[0]))

struct shot_state {
	bool scanned;			/* next_file found on the card */
	unsigned int raw_first;		/* the first shots of the boot also write the raw pieces */
	bool raw;			/* this one does */
	unsigned int next_file;
	unsigned int file;
	void *fp;
	bool ppm_partial;		/* SHOTnnnn.PPM opened, not yet closed after its last row */
	uint32_t buf, handle;		/* lent buffer (uncached), 0 = none */
	struct fenykl_disp_geom g;
	struct fenykl_shot_layout lay;
	struct fenykl_shot_cond start;	/* at the snapshot */
	uint8_t *out;			/* the strip */
	unsigned int strip, strips;	/* next strip to write, strips in the file */
	unsigned int piece;		/* next raw piece, SHOT_PIECES = none */
	uint32_t piece_off;
	struct shot_piece pieces[SHOT_PIECES];
	uint32_t t0, t_lent, capture_ms;
	uint32_t sum, written;
	int free_after;			/* lendable blocks left after the lend */
	uint32_t lv_src, osd_src[FENYKL_SHOT_LAYERS], hide, lv_flips;
	uint32_t regs[SHOT_REGS_N];	/* at the attempt, before the lend */
	struct fenykl_shot_frame frame;
	uint8_t pal_raw[FENYKL_OSD_PAL_TOTAL * 4];
	uint8_t pal[FENYKL_OSD_PAL_TOTAL * 4];	/* RGBA */
	uint8_t frames_pal[FENYKL_FRAMES_PAL * 4];
	uint8_t canvas[3 * FENYKL_DISP_W_MAX];
#if !FENYKL_SHOT_STRIP_LENT
	uint8_t strip_bss[3 * FENYKL_DISP_W_MAX * FENYKL_SHOT_STRIP_ROWS] __attribute__((aligned(64)));
#endif
	/* counters for the status */
	uint32_t snapshots, shots, failed, aborted, raw_failed, overwritten;
	uint32_t lv_torn, osd_torn, frames_torn, no_lv;
};

static struct shot_state shot_st;

FENYKL_LOG_MODULE(fenykl_shot_log, "shot", FENYKL_LOG_INFO);

static const uint8_t shot_stack[FENYKL_SHOT_LAYERS] = FENYKL_OSD_STACK;
static const uint8_t shot_bpp[FENYKL_SHOT_LAYERS] = FENYKL_OSD_BPP;
static const uint16_t shot_entries[FENYKL_SHOT_LAYERS] = FENYKL_OSD_PAL_ENTRIES;
static const uint16_t shot_pal_off[FENYKL_SHOT_LAYERS] = FENYKL_OSD_PAL_FIRST;

/* --- copies --- */

static void shot_bytes(uint8_t *d, const uint8_t *s, unsigned int n)
{
	for (unsigned int i = 0; i < n; i++)
		d[i] = s[i];
}

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
static void shot_lv_place(struct shot_state *st, uint32_t lv)
{
	struct fenykl_shot_frame *f = &st->frame;
	uint32_t x = 0, y = 0;

	fenykl_disp_lv_pos(lv, &x, &y);
	f->lv_x = (uint16_t)((x < st->g.w ? x : st->g.w) & ~1u);
	f->lv_y = (uint16_t)(y < st->g.h ? y : st->g.h);
	f->lv_w = (uint16_t)(st->g.w - f->lv_x);
	f->lv_h = (uint16_t)(st->g.h - f->lv_y);
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
		if (!fenykl_dram(lv, st->g.lv_bytes)) {
			st->no_lv++;
			fenykl_log_warn(&fenykl_shot_log, "live view %08x not a DRAM buffer: OSD only",
					lv);
			return;
		}
		shot_lv_place(st, lv);
		t = FENYKL_TIMER_NOW();
		flips = shot_copy(st->buf + st->lay.lv, fenykl_cached(lv), st->g.lv_pitch,
				  st->lay.lv_row, f->lv_w / 2, f->lv_h, fenykl_disp_lv);
		t = FENYKL_TIMER_ELAPSED(t, FENYKL_TIMER_NOW());
		if (flips <= 1)
			break;
	}
	f->lv = FENYKL_UNCACHED(st->buf + st->lay.lv);
	f->lv_pitch = st->lay.lv_row;
	st->lv_src = lv;
	st->lv_flips = flips;
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

/* Every layer's display shadow holds b[draw ^ 1]: the DISPLAY task committed the GUI's flip. */
static bool shot_osd_committed(void)
{
	uint32_t b = (fenykl_osd_draw_buf() ^ 1u) & 1u;

	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++) {
		uint32_t p = fenykl_osd_buf(l, b);

		if (p && (p & 0x1FFFFFFFu) != (fenykl_osd_shown(l) & 0x1FFFFFFFu))
			return false;
	}
	return true;
}

/* After a GUI flip the panel shows the old buffer, which the GUI may draw into, until the commit. */
static void shot_osd_wait_commit(void)
{
	for (unsigned int i = 0; i < SHOT_COMMIT_WAIT_MS; i++) {
		if (shot_osd_committed())
			return;
		fenykl_task_sleep(1);
	}
	fenykl_log_warn(&fenykl_shot_log, "osd flip not committed in %u ms", SHOT_COMMIT_WAIT_MS);
}

static void shot_osd_layers(struct shot_state *st)
{
	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++) {
		unsigned int e = 4u * shot_pal_off[l];

		fenykl_shot_palette(fenykl_osd_palettes() + e, shot_entries[l], st->g.osd_alpha[l],
				    st->pal + e);
	}
	shot_bytes(st->pal_raw, fenykl_osd_palettes(), sizeof(st->pal_raw));
	for (unsigned int i = 0; i < FENYKL_SHOT_LAYERS; i++) {
		unsigned int l = shot_stack[i];
		struct fenykl_shot_layer *o = &st->frame.layer[i];

		o->pix = st->hide & (1u << (4 * l)) ? NULL : FENYKL_UNCACHED(st->buf + st->lay.osd[l]);
		o->pitch = st->lay.osd_row[l];
		o->bpp = shot_bpp[l];
		o->entries = shot_entries[l];
		o->pal = st->pal + 4 * shot_pal_off[l];
		o->w = st->g.osd_w;
		o->h = st->g.osd_h;
	}
}

/* The three shown OSD layers, copied until no GUI flip happened during the copy. */
static bool shot_copy_osd(struct shot_state *st)
{
	unsigned int flips = 0, tries;

	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++)
		if (fenykl_osd_bpp(l) != shot_bpp[l]) {
			fenykl_log_err(&fenykl_shot_log, "layer %u bpp %u, expected %u", l,
				       fenykl_osd_bpp(l), shot_bpp[l]);
			return false;
		}
	for (tries = 1; tries <= SHOT_OSD_TRIES; tries++) {
		uint32_t draw;

		if (FENYKL_OSD_SHADOW_LAGS)
			shot_osd_wait_commit();
		draw = fenykl_osd_draw_buf();
		flips = 0;
		st->hide = fenykl_osd_hide_mask();
		for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++) {
			unsigned int row = st->lay.osd_row[l];

			st->osd_src[l] = shot_osd_shown(l, draw);
			if (!st->osd_src[l])
				return false;
			flips += shot_copy(st->buf + st->lay.osd[l], fenykl_cached(st->osd_src[l]),
					   FENYKL_OSD_PLANE_W * shot_bpp[l] / 8, row, row / 4,
					   st->g.osd_h, fenykl_osd_draw_buf);
		}
		if (fenykl_osd_draw_buf() != draw)
			flips++;
		if (!flips)
			break;
	}
	shot_osd_layers(st);
	fenykl_log_dbg(&fenykl_shot_log, "osd L0 %08x L1 %08x L2 %08x", st->osd_src[0],
		       st->osd_src[1], st->osd_src[2]);
	fenykl_log_dbg(&fenykl_shot_log, "osd hide %03x, tries %u", st->hide,
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
	if (!fenykl_frames_shown()) {
		fenykl_log_dbg(&fenykl_shot_log, "no frames layer");
		return;
	}
	fenykl_frames_geom(&fr->w, &fr->h, &fr->to_w, &fr->to_h);
	if (!fr->w || fr->w > FENYKL_FRAMES_PITCH || !fr->h || fr->h > FENYKL_FRAMES_H ||
	    !fr->to_w || fr->to_w > st->g.w || !fr->to_h || fr->to_h > st->g.h) {
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
		flips = shot_copy(st->buf + st->lay.frames, fenykl_cached(src), FENYKL_FRAMES_PITCH,
				  FENYKL_FRAMES_PITCH, (fr->w + 3) / 4, fr->h, fenykl_frames_shown);
		if (!flips)
			break;
	}
	shot_bytes(st->frames_pal, fenykl_frames_palette(), sizeof(st->frames_pal));
	fr->pix = FENYKL_UNCACHED(st->buf + st->lay.frames);
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

/* 32-bit sum of the snapshot in the lent buffer. */
static uint32_t shot_sum(const struct shot_state *st)
{
	const volatile uint32_t *p = (const volatile uint32_t *)FENYKL_UNCACHED(st->buf);
	uint32_t sum = 0;

	for (uint32_t i = 0; i < st->lay.snap / 4; i++)
		sum += p[i];
	return sum;
}

/* Largest index in the copy of each layer with fewer than 256 entries (8 bpp only). */
static void shot_log_max(const struct shot_state *st)
{
	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++) {
		const volatile uint8_t *p = FENYKL_UNCACHED(st->buf + st->lay.osd[l]);
		uint32_t n = st->lay.osd_row[l] * st->g.osd_h;
		unsigned int max = 0;

		if (shot_bpp[l] != 8 || shot_entries[l] >= 256)
			continue;
		for (uint32_t i = 0; i < n; i++)
			if (p[i] > max)
				max = p[i];
		fenykl_log_info(&fenykl_shot_log, "L%u largest index %02x of %u entries", l, max,
				shot_entries[l]);
	}
}

/* Registers for the raw dump, and at DEBUG in the log: the state the gate and the lend saw. */
static void shot_read_regs(struct shot_state *st)
{
	for (unsigned int i = 0; i < SHOT_REGS_N; i++) {
		const struct fenykl_shot_reg *r = &shot_regs[i];

		st->regs[i] = r->bytes == 1 ? fenykl_rd8(r->addr) :
			      r->bytes == 2 ? fenykl_rd16(r->addr) : fenykl_rd32(r->addr);
		fenykl_log_dbg(&fenykl_shot_log, "reg %s %08x = %08x", r->name, r->addr,
			       st->regs[i]);
	}
}

/* --- card file and buffer --- */

static void shot_path(char *out, unsigned int n, const char *ext)
{
	fenykl_sprintf(out, "C:\\SHOT%04u.%s", n, ext);
}

static bool shot_exists(unsigned int n)
{
	char path[SHOT_PATH_MAX];
	void *fp;

	shot_path(path, n, "PPM");
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

static uint32_t shot_held_ms(const struct shot_state *st)
{
	return FENYKL_TIMER_ELAPSED(st->t_lent, FENYKL_TIMER_NOW()) / 1000u;
}

/* Gives the lent buffer back, then closes an open file; a PPM not written to its end is removed
   while the card gate is open (raw pieces already written stay). */
static void shot_end(struct shot_state *st)
{
	char path[SHOT_PATH_MAX];
	int r;

	if (st->buf) {
		uint32_t fr = fenykl_shot_buf_free(st->handle);
		struct fenykl_shot_cond c;

		st->buf = 0;
		if (fr)
			fenykl_log_err(&fenykl_shot_log, "buffer %x not given back: %x", st->handle, fr);
		fenykl_shot_cond(&c);
		fenykl_log_info(&fenykl_shot_log, "lent %u bytes for %u ms", st->lay.bytes,
				shot_held_ms(st));
		fenykl_log_info(&fenykl_shot_log, "  now %d free, used %08x, other pools %08x",
				c.buf_free, c.pool_used, c.other_used);
	}
	if (st->fp) {
		fenykl_fclose(st->fp);
		st->fp = NULL;
	}
	if (!st->ppm_partial)
		return;
	st->ppm_partial = false;
	if (!fenykl_card_io_allowed()) {
		fenykl_log_warn(&fenykl_shot_log, "SHOT%04u.PPM partial, card not available: kept",
				st->file);
		return;
	}
	shot_path(path, st->file, "PPM");
	r = fw_rpc_fremove(path);
	if (r)
		fenykl_log_warn(&fenykl_shot_log, "SHOT%04u.PPM partial, remove: %d", st->file, r);
	else
		fenykl_log_info(&fenykl_shot_log, "SHOT%04u.PPM partial: removed", st->file);
}

static bool shot_fail(struct shot_state *st, const char *what)
{
	shot_end(st);
	st->failed++;
	fenykl_log_err(&fenykl_shot_log, "SHOT%04u: %s", st->file, what);
	return false;
}

static bool shot_abort(struct shot_state *st, const char *why)
{
	shot_end(st);
	st->aborted++;
	fenykl_log_warn(&fenykl_shot_log, "SHOT%04u aborted: %s", st->file, why);
	return false;
}

/* The rules again while the buffer is lent; false = aborted (logged). */
static bool shot_still_ok(struct shot_state *st)
{
	struct fenykl_shot_cond c;
	const char *why;

	fenykl_shot_cond(&c);
	why = fenykl_shot_gate(&c, &st->start);
	return why ? shot_abort(st, why) : true;
}

/* p: cached (.bss, written back first) or uncached. */
static bool shot_write(struct shot_state *st, const void *p, unsigned int len, bool cached)
{
	if (cached)
		fenykl_dcache_flush();
	if (fenykl_fwrite(FENYKL_UNCACHED(p), 1, len, st->fp) != (int)len)
		return false;
	st->written += len;
	return true;
}

static bool shot_open(struct shot_state *st, const char *ext)
{
	char path[SHOT_PATH_MAX];

	shot_path(path, st->file, ext);
	st->fp = fenykl_fopen(path, "w");
	return st->fp != NULL;
}

static bool shot_close(struct shot_state *st)
{
	int r = fenykl_fclose(st->fp);

	st->fp = NULL;
	return !r;
}

/* The sum check before the lent buffer goes back; the PPM is written. */
static bool shot_done(struct shot_state *st)
{
	uint32_t sum = shot_sum(st);

	if (sum != st->sum) {
		st->overwritten++;
		fenykl_log_warn(&fenykl_shot_log, "snapshot overwritten during the write: %08x, was %08x",
				sum, st->sum);
	} else {
		fenykl_log_info(&fenykl_shot_log, "snapshot sum %08x unchanged", sum);
	}
	shot_end(st);
	st->shots++;
	fenykl_log_info(&fenykl_shot_log, "SHOT%04u.PPM written in %u ms (capture %u ms), %u bytes",
			st->file, FENYKL_TIMER_ELAPSED(st->t0, FENYKL_TIMER_NOW()) / 1000u,
			st->capture_ms, st->written);
	return false;
}

/* --- raw dump --- */

struct shot_text {
	char *p;
	uint32_t len;
};

static void shot_text_end(struct shot_text *t)
{
	while (t->p[t->len])
		t->len++;
}

#define SHOT_PUT(t, ...)						\
	do {								\
		fenykl_sprintf((t)->p + (t)->len, __VA_ARGS__);		\
		shot_text_end(t);					\
	} while (0)

/* The display state of the snapshot as text into the strip; returns its length. */
static uint32_t shot_reg_text(struct shot_state *st)
{
	const struct fenykl_shot_frame *f = &st->frame;
	struct shot_text t = { (char *)st->out, 0 };

	t.p[0] = 0;
	SHOT_PUT(&t, "geom %u x %u square_w %u osd %u x %u\n", st->g.w, st->g.h, st->g.square_w,
		 st->g.osd_w, st->g.osd_h);
	SHOT_PUT(&t, "lv_pitch %u lv_bytes %u osd_alpha %03x %03x %03x\n", st->g.lv_pitch,
		 st->g.lv_bytes, st->g.osd_alpha[0], st->g.osd_alpha[1], st->g.osd_alpha[2]);
	SHOT_PUT(&t, "lv %08x at %u,%u %u x %u flips %u\n", st->lv_src, f->lv_x, f->lv_y, f->lv_w,
		 f->lv_h, st->lv_flips);
	SHOT_PUT(&t, "osd L0 %08x L1 %08x L2 %08x hide %03x\n", st->osd_src[0], st->osd_src[1],
		 st->osd_src[2], st->hide);
	SHOT_PUT(&t, "lent %08x handle %x mode %08x\n", st->buf, st->handle, st->start.mode);
	SHOT_PUT(&t, "free %d before the lend, %d after; used %08x, other pools %08x\n",
		 st->start.buf_free, st->free_after, st->start.pool_used, st->start.other_used);
	for (unsigned int i = 0; i < SHOT_REGS_N; i++)
		SHOT_PUT(&t, "%s %08x %08x\n", shot_regs[i].name, shot_regs[i].addr, st->regs[i]);
	return t.len;
}

/* LV as YUYV rows of lv_row bytes, each OSD layer as its copied rows, the palettes, the text. */
static void shot_raw_begin(struct shot_state *st)
{
	static const char *const ext[FENYKL_SHOT_LAYERS] = { "L0", "L1", "L2" };
	const uint8_t *buf = FENYKL_UNCACHED(st->buf);

	st->pieces[0] = (struct shot_piece){
		"LV", buf + st->lay.lv, st->frame.lv ? st->lay.lv_row * st->frame.lv_h : 0, false
	};
	for (unsigned int l = 0; l < FENYKL_SHOT_LAYERS; l++)
		st->pieces[1 + l] = (struct shot_piece){
			ext[l], buf + st->lay.osd[l], st->lay.osd_row[l] * st->g.osd_h, false
		};
	st->pieces[4] = (struct shot_piece){ "PAL", st->pal_raw, sizeof(st->pal_raw), true };
	st->pieces[5] = (struct shot_piece){
		"REG", st->out, shot_reg_text(st), !FENYKL_SHOT_STRIP_LENT
	};
	st->piece = 0;
	st->piece_off = 0;
}

/* A raw file failed: the PPM is written all the same. */
static bool shot_raw_fail(struct shot_state *st, const char *what)
{
	st->raw_failed++;
	fenykl_log_err(&fenykl_shot_log, "SHOT%04u.%s: %s", st->file, st->pieces[st->piece].ext,
		       what);
	return shot_done(st);
}

/* One chunk of the current piece; false once the last is written (done) or on an error. */
static bool shot_raw_step(struct shot_state *st)
{
	struct shot_piece *pc;
	uint32_t n;

	while (st->piece < SHOT_PIECES && !st->pieces[st->piece].len)
		st->piece++;
	if (st->piece == SHOT_PIECES)
		return shot_done(st);
	pc = &st->pieces[st->piece];
	if (!st->fp && !shot_open(st, pc->ext))
		return shot_raw_fail(st, "cannot open");
	n = pc->len - st->piece_off;
	if (n > SHOT_RAW_CHUNK)
		n = SHOT_RAW_CHUNK;
	if (!shot_write(st, pc->p + st->piece_off, n, pc->cached))
		return shot_raw_fail(st, "write failed");
	st->piece_off += n;
	if (st->piece_off < pc->len)
		return true;
	if (!shot_close(st))
		return shot_raw_fail(st, "close failed");
	st->piece_off = 0;
	for (st->piece++; st->piece < SHOT_PIECES && !st->pieces[st->piece].len; st->piece++)
		;
	return st->piece < SHOT_PIECES ? true : shot_done(st);
}

/* --- snapshot --- */

/* Reads the output and lends the buffer after the log flush (card I/O): the registers, the rules,
   the geometry and the free count at the lend. */
static bool shot_lend(struct shot_state *st)
{
	struct fenykl_shot_cond after;
	const char *why;
	uint32_t buf;
	int r = fenykl_log_flush();

	if (r < 0)
		fenykl_log_dbg(&fenykl_shot_log, "log flush before the lend: %d", r);
	shot_read_regs(st);
	fenykl_shot_cond(&st->start);
	fenykl_log_info(&fenykl_shot_log, "shot: %d free, used %08x, other pools %08x",
			st->start.buf_free, st->start.pool_used, st->start.other_used);
	fenykl_log_info(&fenykl_shot_log, "  stills shared %d, mode %x", st->start.still_shared,
			st->start.mode);
	why = fenykl_shot_gate(&st->start, NULL);
	if (why) {
		fenykl_log_warn(&fenykl_shot_log, "%s: no screenshot", why);
		return false;
	}
	if (!fenykl_disp_geom(&st->g)) {
		fenykl_log_warn(&fenykl_shot_log, "output not supported: no screenshot");
		return false;
	}
	fenykl_shot_layout(&st->g, &st->lay);
	buf = fenykl_shot_buf_alloc(st->lay.bytes, &st->handle);
	if (!buf) {
		fenykl_log_warn(&fenykl_shot_log, "no buffer of %u bytes (%d free): no screenshot",
				st->lay.bytes, st->start.buf_free);
		return false;
	}
	st->buf = buf;
	st->t_lent = FENYKL_TIMER_NOW();
	fenykl_shot_cond(&after);
	st->free_after = after.buf_free;
	if (!fenykl_dram(buf, st->lay.bytes)) {
		fenykl_log_err(&fenykl_shot_log, "lent buffer %08x", buf);
		return shot_fail(st, "no buffer");
	}
	/* the alloc can sleep on the pool lock: the rules again before anything is copied */
	why = fenykl_shot_gate(&after, &st->start);
	return why ? shot_abort(st, why) : true;
}

static bool shot_snapshot(struct shot_state *st)
{
	/* the copies sleep on flips: the rules between them */
	shot_copy_lv(st);
	if (!shot_still_ok(st))
		return false;
	shot_copy_frames(st);
	if (!shot_copy_osd(st))
		return shot_fail(st, "no OSD planes");
	st->frame.w = st->g.w;
	st->frame.h = st->g.h;
	st->sum = shot_sum(st);
	st->capture_ms = FENYKL_TIMER_ELAPSED(st->t0, FENYKL_TIMER_NOW()) / 1000u;
	st->snapshots++;
	fenykl_log_info(&fenykl_shot_log, "snapshot %u x %u in %08x, %d free after the lend",
			st->g.w, st->g.h, st->buf, st->free_after);
	fenykl_log_info(&fenykl_shot_log, "  lv flips %u, capture %u ms", st->lv_flips,
			st->capture_ms);
	shot_log_max(st);
	return true;
}

/* The file number; false = no screenshot (logged). */
static bool shot_number(struct shot_state *st)
{
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
	return true;
}

/* The strip: at the end of the lent buffer, or in .bss. */
static uint8_t *shot_out(struct shot_state *st)
{
#if FENYKL_SHOT_STRIP_LENT
	return FENYKL_UNCACHED(st->buf + st->lay.snap);
#else
	return st->strip_bss;
#endif
}

/* --- API --- */

/* The PPM is opened before the lend, so that the file open is not part of the hold. */
bool fenykl_shot_start(void)
{
	struct shot_state *st = &shot_st;
	unsigned int n;

	if (!fenykl_card_io_allowed()) {
		fenykl_log_warn(&fenykl_shot_log, "card not available: no screenshot");
		return false;
	}
	st->t0 = FENYKL_TIMER_NOW();
	st->written = 0;
	if (!shot_number(st))
		return false;
	if (!shot_open(st, "PPM"))
		return shot_fail(st, "cannot open");
	st->ppm_partial = true;
	if (!shot_lend(st)) {
		shot_end(st);
		return false;
	}
	if (!shot_snapshot(st) || !shot_still_ok(st))
		return false;
	st->out = shot_out(st);
	n = fenykl_shot_ppm_header((char *)st->out, st->g.square_w, st->g.h);
	if (!shot_write(st, st->out, n, !FENYKL_SHOT_STRIP_LENT))
		return shot_fail(st, "write failed");
	st->strip = 0;
	st->strips = (st->g.h + FENYKL_SHOT_STRIP_ROWS - 1) / FENYKL_SHOT_STRIP_ROWS;
	st->piece = SHOT_PIECES;
	st->raw = st->snapshots <= st->raw_first;
	if (st->raw)
		fenykl_log_info(&fenykl_shot_log, "raw pieces too: shot %u of the first %u",
				st->snapshots, st->raw_first);
	return shot_still_ok(st);
}

/* One strip of file rows: composed on the canvas, stretched to square pixels where they differ. */
static bool shot_strip(struct shot_state *st)
{
	unsigned int y0 = st->strip * FENYKL_SHOT_STRIP_ROWS, rows = st->g.h - y0;
	unsigned int out_row = 3u * st->g.square_w;

	if (rows > FENYKL_SHOT_STRIP_ROWS)
		rows = FENYKL_SHOT_STRIP_ROWS;
	for (unsigned int r = 0; r < rows; r++) {
		uint8_t *out = st->out + r * out_row;

		if (st->g.square_w == st->g.w) {
			fenykl_shot_compose(&st->frame, y0 + r, out);
			continue;
		}
		fenykl_shot_compose(&st->frame, y0 + r, st->canvas);
		fenykl_shot_stretch(st->canvas, st->g.w, out, st->g.square_w);
	}
	if (!shot_write(st, st->out, out_row * rows, !FENYKL_SHOT_STRIP_LENT))
		return shot_fail(st, "write failed");
	if (++st->strip < st->strips)
		return true;
	if (!shot_close(st))
		return shot_fail(st, "close failed");
	st->ppm_partial = false;
	if (!st->raw)
		return shot_done(st);
	shot_raw_begin(st);
	return true;
}

bool fenykl_shot_step(void)
{
	struct shot_state *st = &shot_st;
	uint32_t held;

	if (!st->buf)
		return false;
	if (!fenykl_card_io_allowed())
		return shot_fail(st, "card gone while writing");
	if (!shot_still_ok(st))
		return false;
	held = shot_held_ms(st);
	if (held > SHOT_HOLD_MAX_MS) {
		fenykl_log_warn(&fenykl_shot_log, "lent for %u ms: strip %u of %u, raw piece %u",
				held, st->strip, st->strips, st->piece);
		return shot_abort(st, "lent too long");
	}
	if (st->strip < st->strips)
		return shot_strip(st);
	return shot_raw_step(st);
}

void fenykl_shot_raw(unsigned int first)
{
	shot_st.raw_first = first;
}

void fenykl_shot_status(void)
{
	struct shot_state *st = &shot_st;

	fenykl_log_info(&fenykl_shot_log, "shots %u failed %u, torn lv %u osd %u", st->shots,
			st->failed, st->lv_torn, st->osd_torn);
	if (st->aborted || st->overwritten || st->raw_failed)
		fenykl_log_info(&fenykl_shot_log, "  aborted %u, overwritten %u, raw failed %u",
				st->aborted, st->overwritten, st->raw_failed);
	if (st->frames_torn)
		fenykl_log_info(&fenykl_shot_log, "  torn frames %u", st->frames_torn);
	if (st->no_lv)
		fenykl_log_info(&fenykl_shot_log, "  without live view %u", st->no_lv);
}
