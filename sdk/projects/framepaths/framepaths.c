/* framepaths: read-only probe of the live-view frame sources, normal and magnified.

   Hooks (CPU0, MOV task, record into RAM only, no firmware calls besides the original):
   - mov_lv_disp_flip 0x6E8AC521: once per LV frame, right before the frame's IPU pass. After the
     original it appends a record (time, LV frame counter, RAW descriptor, flipped display buffer, FD
     slot, sensor/zoom/GUI state) to a ring.
   - mov_yc_resize_pass 0x6E8A41B0: pass 2 of the magnified view on the rear LCD. When it returns, the
     intermediate YC buffer (0x6034CAC8) holds the frame and stays untouched until the next flip.

   The task (plugin_main, priority 11) drains the ring: transitions are logged in full, steady state
   as one summary line per second. When the state has been stable long enough it snapshots frames
   into a RECFILE block (recfile_alloc, released when done) with a cached word copy between two
   D-cache purges, checks the firmware's own ring counters before and after the copy (retrying a
   torn copy) and writes each snapshot to its own file with a 512-byte text header.

   C:\FENYKL.LOG is always LOG_SIZE bytes, padded ("w" does not truncate); the status block at its end
   is rewritten every LOG_PERIOD_MS. */
#include <fenykl.h>

/* --- firmware addresses (verified on the camera by this probe) --- */

#define CORE_ID 0x8050u /* 0 on CPU0 */
#define TIMER_DOWN 0x98900074u /* timer_now = 0x3FFFFFFF - this, 1 MHz */
#define LV_FRAME_COUNTER 0x60576464u /* u32, +1 per sensor frame handed out */
#define DESC_RING 0x606065F4u /* 3 descriptors of 0x70 B: +4 mode, +8 RAW, +0x16 rate, +0x18/+0x1A origin */
#define DESC_STRIDE 0x70u
#define DESC_SLOTS 3u
#define DESC_CUR 0x6060675Cu /* u16: descriptor of the frame MOV works on */
#define DESC_NEXT 0x60606748u /* u16[3]: ring order of the RAW hand-out */
#define DESC_HANDOUT 0x60606760u /* u16: slot handed out next */
#define DISP_IDX 0x60606744u /* u32: display ring index */
#define FLIP_SKIP 0x6060676Au /* u8: flip suppressed while non-zero */
#define IMG1_SHADOW 0x6034C880u /* u32: img1 address set by the flip */
#define FD_IDX 0x606067A4u /* u16 */
#define FD_PUB 0x606067B0u /* u32: FD slot published by mov_fd_publish */
#define CAMTOP 0x605AED51u /* u8: 8 = CLIP */
#define TAKE_EXPOSE 0x60575808u /* u8 mode, s8 clip_x, s8 clip_y */
#define EXP_CLIP 0x60603C3Cu /* s8 x %, s8 y %, (+2) u16 x, (+4) u16 y */
#define EXP_CLIP_YCHG 0x60603D40u /* u8 */
#define CLIP_EXT 0x6034CAC8u /* intermediate YC of CLIP pass 1 (LCD), 0 otherwise */
#define CLIP_CTRL 0x6034CACCu /* pass-2 IMG_CTRL, 0 outside CLIP */
#define OUT_TYPE 0x60606205u /* u8: 0 rear LCD */
#define IPU_LOCK 0x8FF00100u /* bits 2/4: engine 1/2 busy */
#define GUI_SUPER 0x60702278u
#define GUI_STATE 0x60702279u
#define RECFILE_FLAGS 0x605753F0u /* u8[256]: block in use */
#define RECFILE_BLOCKS 256u
#define RECFILE_BLOCK 0x1E3C0u
#define PROP_ZOOM 0x9812u /* u16 [0] base, [1] rate (x100) */

#define MODE_HS 0
#define MODE_CLIP 5

/* --- timing (ms unless noted) --- */

#define RUN_MS (15u * 60u * 1000u)
#define LOG_PERIOD_MS 10000u
#define NORMAL_STABLE_MS 5000u
#define ZOOM_STABLE_MS 3000u
#define IDLE_MS 500u
#define FULL_AFTER 2 /* records logged in full after each transition */
#define FULL_PER_SEC 30u /* flood guard for full lines */
#define ATTEMPTS 8
#define MAX_ABORTS 5 /* a dump whose trigger never comes (e.g. no pass 2 on the EVF) is given up */
#define EVENT_TICKS 1000 /* wait for a flip / resize pass */

#define RING 512u
#define LOG_SIZE 0x20000u
#define LOG_RESERVE 2048u
#define LOG_LINE 64u
#define HDR_SIZE 512u
#define SNAP_MAX 0xF0000u
#define CHUNK 0x10000u

static uint8_t rd8(uint32_t a)
{
	return *(volatile uint8_t *)(uintptr_t)a;
}

static uint16_t rd16(uint32_t a)
{
	return *(volatile uint16_t *)(uintptr_t)a;
}

static uint32_t rd32(uint32_t a)
{
	return *(volatile uint32_t *)(uintptr_t)a;
}

static uint32_t now_us(void)
{
	return (0x3FFFFFFFu - rd32(TIMER_DOWN)) & 0x3FFFFFFFu;
}

static uint32_t since_us(uint32_t t)
{
	return (now_us() - t) & 0x3FFFFFFFu;
}

static int dram_pointer(uint32_t p)
{
	return (p & 3) == 0 && ((p >> 28) == 0x6 || (p >> 28) == 0xA);
}

static uint32_t cached_alias(uint32_t p)
{
	return (p & 0x3FFFFFFFu) | 0x40000000u;
}

/* --- hooks --- */

struct rec {
	uint32_t t, flip, fc, raw, dsp, fd, ext, ctrl, ipu, rz, p2, span;
	int16_t mode;
	uint16_t cur, rate, ox, oy, didx, fdidx, zbase, zrate, cpx, cpy;
	uint8_t cam, te_mode, out, gsup, gst, skip, ychg;
	int8_t te_x, te_y, clx, cly;
};

static struct rec ring[RING];
static volatile uint32_t head, tail;
static volatile uint32_t flips, overruns, flip_cpu1, resize_cpu1, last_flip_t;
static volatile uint32_t rz_seq, rz_res, rz_flip, rz_dur, rz_span;
static volatile uint32_t zoom_prop; /* prop_get_obj(0x9812), set before the hooks are installed */

static void on_flip(void)
{
	if (rd32(CORE_ID) != 0) {
		flip_cpu1++;
		fw_orig_mov_lv_disp_flip();
		return;
	}
	fw_orig_mov_lv_disp_flip();
	uint32_t t = now_us(), f = flips + 1;
	last_flip_t = t;
	if (head - tail >= RING) {
		overruns++;
		flips = f;
		return;
	}
	struct rec *r = &ring[head % RING];
	uint32_t cur = rd16(DESC_CUR);
	uint32_t desc = DESC_RING + DESC_STRIDE * (cur < DESC_SLOTS ? cur : 0);
	r->t = t;
	r->flip = f;
	r->fc = rd32(LV_FRAME_COUNTER);
	r->cur = (uint16_t)cur;
	r->mode = (int16_t)rd32(desc + 4);
	r->raw = rd32(desc + 8);
	r->rate = rd16(desc + 0x16);
	r->ox = rd16(desc + 0x18);
	r->oy = rd16(desc + 0x1A);
	r->didx = (uint16_t)rd32(DISP_IDX);
	r->dsp = rd32(IMG1_SHADOW);
	r->skip = rd8(FLIP_SKIP);
	r->fdidx = rd16(FD_IDX);
	r->fd = rd32(FD_PUB);
	r->cam = rd8(CAMTOP);
	r->te_mode = rd8(TAKE_EXPOSE);
	r->te_x = (int8_t)rd8(TAKE_EXPOSE + 1);
	r->te_y = (int8_t)rd8(TAKE_EXPOSE + 2);
	r->clx = (int8_t)rd8(EXP_CLIP);
	r->cly = (int8_t)rd8(EXP_CLIP + 1);
	r->cpx = rd16(EXP_CLIP + 2);
	r->cpy = rd16(EXP_CLIP + 4);
	r->ychg = rd8(EXP_CLIP_YCHG);
	r->ext = rd32(CLIP_EXT);
	r->ctrl = rd32(CLIP_CTRL);
	r->out = rd8(OUT_TYPE);
	r->ipu = rd32(IPU_LOCK);
	r->gsup = rd8(GUI_SUPER);
	r->gst = rd8(GUI_STATE);
	uint32_t zp = zoom_prop;
	r->zbase = zp ? rd16(zp) : 0;
	r->zrate = zp ? rd16(zp + 2) : 0;
	r->rz = rz_seq;
	r->p2 = rz_dur;
	r->span = rz_span;
	head++;
	flips = f;
}
FENYKL_HOOK(mov_lv_disp_flip, on_flip);

static unsigned on_resize(void *in, void *out)
{
	if (rd32(CORE_ID) != 0) {
		resize_cpu1++;
		return fw_orig_mov_yc_resize_pass(in, out);
	}
	uint32_t t0 = now_us();
	unsigned r = fw_orig_mov_yc_resize_pass(in, out);
	uint32_t t1 = now_us();
	rz_res = r;
	rz_flip = flips;
	rz_dur = (t1 - t0) & 0x3FFFFFFFu;
	rz_span = (t1 - last_flip_t) & 0x3FFFFFFFu;
	rz_seq++;
	return r;
}
FENYKL_HOOK(mov_yc_resize_pass, on_resize);

/* --- text --- */

struct text {
	char *buf;
	unsigned len, limit;
	int full;
};

static uint32_t dropped_lines;

static char logbuf[LOG_SIZE];
static struct text lg = {logbuf, 0, LOG_SIZE - LOG_RESERVE, 0};
static char hdrbuf[HDR_SIZE];
static struct text hd = {hdrbuf, 0, HDR_SIZE, 0};
static struct text *out = &lg;

static void put(const char *s)
{
	for (; *s; s++) {
		if (out->len >= out->limit) {
			out->full = 1;
			return;
		}
		out->buf[out->len++] = *s;
	}
}

static void nl(void)
{
	put("\r\n");
}

static void hex(uint32_t v, int digits)
{
	char s[9];
	for (int i = digits - 1; i >= 0; i--, v >>= 4)
		s[i] = "0123456789ABCDEF"[v & 15];
	s[digits] = 0;
	put(s);
}

static void dec(uint32_t v)
{
	char s[11];
	int n = 0;
	do
		s[n++] = (char)('0' + v % 10);
	while (v /= 10);
	char r[11];
	for (int i = 0; i < n; i++)
		r[i] = s[n - 1 - i];
	r[n] = 0;
	put(r);
}

static void sdec(int32_t v)
{
	if (v < 0) {
		put("-");
		v = -v;
	}
	dec((uint32_t)v);
}

static void kv(const char *k, uint32_t v)
{
	put(k);
	dec(v);
}

static void kx(const char *k, uint32_t v)
{
	put(k);
	hex(v, 8);
}

/* Ends a log line begun at `start`, or drops it if it did not fit (no partial lines). */
static void end_line(unsigned start)
{
	nl();
	if (!lg.full)
		return;
	lg.len = start;
	lg.full = 0;
	dropped_lines++;
}

/* --- log file --- */

static uint32_t t_start, log_attempts, log_ok, log_open_fail, log_short;

static uint32_t ms(void)
{
	return since_us(t_start) / 1000u;
}

static void write_log(void)
{
	for (unsigned i = lg.len; i < LOG_SIZE; i++) {
		unsigned col = i % LOG_LINE;
		logbuf[i] = col == LOG_LINE - 2 ? '\r' : col == LOG_LINE - 1 ? '\n' : ' ';
	}
	log_attempts++;
	void *fp = fw_rpc_fopen("C:\\FENYKL.LOG", "w");
	if (!fp) {
		log_open_fail++;
		return;
	}
	int n = fw_rpc_fwrite(logbuf, 1, LOG_SIZE, fp);
	int c = fw_rpc_fclose(fp);
	if (n != (int)LOG_SIZE || c == -1) {
		log_short++;
		return;
	}
	log_ok++;
}

/* --- dumps --- */

enum kind { K_RAW, K_DSP, K_FD, K_YC };

struct dump {
	const char *name, *file, *format;
	enum kind kind;
	int16_t mode; /* descriptor mode required: 0 normal, 5 CLIP */
	uint16_t mag; /* 0 normal, 5 or 7 */
	uint32_t bytes;
	uint16_t width, height, stride;
	uint8_t done, ok, aborts;
};

static struct dump dumps[] = {
	{"LVRAW", "C:\\FP_LVRAW.BIN", "raw", K_RAW, MODE_HS, 0, 0xEFD80, 1400, 614, 1600, 0, 0, 0},
	{"LVDSP", "C:\\FP_LVDSP.BIN", "yc422", K_DSP, MODE_HS, 0, 0xF0000, 800, 600, 1600, 0, 0, 0},
	{"FD", "C:\\FP_FD.BIN", "y8", K_FD, MODE_HS, 0, 0x4B000, 640, 480, 640, 0, 0, 0},
	{"ZRAW5", "C:\\FP_ZRAW5.BIN", "raw", K_RAW, MODE_CLIP, 5, 0xEA600, 800, 600, 1600, 0, 0, 0},
	{"ZYC5", "C:\\FP_ZYC5.BIN", "yc422", K_YC, MODE_CLIP, 5, 0xA8C00, 640, 480, 1280, 0, 0, 0},
	{"ZDSP5", "C:\\FP_ZDSP5.BIN", "yc422", K_DSP, MODE_CLIP, 5, 0xF0000, 800, 600, 1600, 0, 0, 0},
	{"ZRAW7", "C:\\FP_ZRAW7.BIN", "raw", K_RAW, MODE_CLIP, 7, 0xEA600, 800, 600, 1600, 0, 0, 0},
	{"ZYC7", "C:\\FP_ZYC7.BIN", "yc422", K_YC, MODE_CLIP, 7, 0xA8C00, 640, 480, 1280, 0, 0, 0},
	{"ZDSP7", "C:\\FP_ZDSP7.BIN", "yc422", K_DSP, MODE_CLIP, 7, 0xF0000, 800, 600, 1600, 0, 0, 0},
};
#define NDUMPS (sizeof(dumps) / sizeof(dumps[0]))

static uint32_t snapbuf; /* recfile_alloc result (uncached alias), 0 = none */
static int nofree_logged;

struct snap {
	uint32_t src, f0, f1, fc0, fc1, rz0, rz1, t0, t1, copy_us;
	int attempts, valid;
	const char *why; /* last reason for a retry or abort */
};

static int free_run_ok(uint32_t blocks)
{
	uint32_t run = 0;
	for (uint32_t i = 0; i < RECFILE_BLOCKS; i++) {
		run = rd8(RECFILE_FLAGS + i) ? 0 : run + 1;
		if (run >= blocks)
			return 1;
	}
	return 0;
}

static int claim_buffer(void)
{
	if (snapbuf)
		return 1;
	unsigned start = lg.len;
	put("B t");
	dec(ms());
	put(" recfile ");
	if (!free_run_ok((SNAP_MAX + RECFILE_BLOCK - 1) / RECFILE_BLOCK)) {
		if (nofree_logged) {
			lg.len = start;
			return 0;
		}
		nofree_logged = 1;
		put("no free run of blocks, dumps postponed");
		end_line(start);
		return 0;
	}
	nofree_logged = 0;
	snapbuf = fw_recfile_alloc(SNAP_MAX);
	kx("alloc ", snapbuf);
	end_line(start);
	return snapbuf != 0;
}

static void release_buffer(void)
{
	if (!snapbuf)
		return;
	fw_recfile_free(snapbuf);
	unsigned start = lg.len;
	put("B t");
	dec(ms());
	kx(" recfile free ", snapbuf);
	end_line(start);
	snapbuf = 0;
}

static void copy_words(uint32_t dst, uint32_t src, uint32_t bytes)
{
	volatile uint32_t *d = (volatile uint32_t *)(uintptr_t)dst;
	const volatile uint32_t *s = (const volatile uint32_t *)(uintptr_t)src;
	for (uint32_t n = bytes / 16; n; n--, s += 4, d += 4) {
		uint32_t a = s[0], b = s[1], c = s[2], e = s[3];
		d[0] = a;
		d[1] = b;
		d[2] = c;
		d[3] = e;
	}
}

/* Slot whose next[] entry is n, i.e. the RAW slot handed out last when n is the next one. */
static uint32_t handed_out_before(uint32_t n)
{
	for (uint32_t i = 0; i < DESC_SLOTS; i++)
		if (rd16(DESC_NEXT + 2 * i) == n)
			return i;
	return 0xFFFF;
}

static int16_t cur_mode(void)
{
	uint32_t cur = rd16(DESC_CUR);
	return cur < DESC_SLOTS ? (int16_t)rd32(DESC_RING + DESC_STRIDE * cur + 4) : -1;
}

static int wait_flip(void)
{
	uint32_t f = flips;
	for (int i = 0; i < EVENT_TICKS; i++) {
		fw_OlyTaskSleep(1);
		if (flips != f)
			return 1;
	}
	return 0;
}

static int wait_resize(void)
{
	uint32_t s = rz_seq;
	for (int i = 0; i < EVENT_TICKS; i++) {
		fw_OlyTaskSleep(1);
		if (rz_seq != s)
			return 1;
	}
	return 0;
}

/* One copy attempt right after the triggering event; 1 = consistent, 0 = retry, -1 = abort. */
static int attempt(const struct dump *d, struct snap *s)
{
	uint32_t raw_cur = 0, n0 = 0, n1, last_out;
	if (d->kind == K_YC) {
		if (!wait_resize()) {
			s->why = "no resize pass";
			return -1;
		}
		s->rz0 = rz_seq;
		uint32_t res = rz_res, rf = rz_flip;
		s->f0 = flips;
		if (rz_seq != s->rz0 || res != 0 || s->f0 != rf) {
			s->why = "missed the window";
			return 0;
		}
		s->src = rd32(CLIP_EXT);
	} else {
		if (!wait_flip()) {
			s->why = "no flip";
			return -1;
		}
		s->f0 = flips;
		if (cur_mode() != d->mode) {
			s->why = "mode changed";
			return -1;
		}
		if (d->kind == K_RAW) {
			/* MOV got this frame after its frame-done callback, and the hand-out skips the
			   current slot: the copy is torn only if the slot is handed out again meanwhile. */
			raw_cur = rd16(DESC_CUR);
			if (raw_cur >= DESC_SLOTS) {
				s->why = "no current descriptor";
				return 0;
			}
			s->src = rd32(DESC_RING + DESC_STRIDE * raw_cur + 8);
			n0 = rd16(DESC_HANDOUT);
		} else {
			s->src = rd32(d->kind == K_DSP ? IMG1_SHADOW : FD_PUB);
		}
		s->rz0 = rz_seq;
	}
	s->fc0 = rd32(LV_FRAME_COUNTER);
	if (flips != s->f0) {
		s->why = "flip during setup";
		return 0;
	}
	if (!dram_pointer(s->src) || cached_alias(s->src) + d->bytes > 0x70000000u) {
		s->why = "source not in DRAM";
		return -1;
	}
	fw_dcache_purge_all_irqsafe();
	s->t0 = now_us();
	copy_words((uint32_t)FENYKL_UNCACHED(snapbuf), cached_alias(s->src), d->bytes);
	s->t1 = now_us();
	fw_dcache_purge_all_irqsafe();
	s->copy_us = (s->t1 - s->t0) & 0x3FFFFFFFu;
	s->f1 = flips;
	s->fc1 = rd32(LV_FRAME_COUNTER);
	s->rz1 = rz_seq;
	switch (d->kind) {
	case K_RAW:
		n1 = rd16(DESC_HANDOUT);
		last_out = handed_out_before(n1);
		if (s->fc1 - s->fc0 <= 1 && ((n1 == n0 && s->fc1 == s->fc0) ||
					     (last_out < DESC_SLOTS && last_out != raw_cur)))
			return 1;
		s->why = "RAW slot handed out during the copy";
		return 0;
	case K_DSP:
	case K_FD:
		if (s->f1 - s->f0 <= 1)
			return 1;
		s->why = "two flips during the copy";
		return 0;
	case K_YC:
		if (s->f1 == s->f0 && s->rz1 == s->rz0)
			return 1;
		s->why = "flip during the copy";
		return 0;
	}
	return -1;
}

static void header_line(const char *k, uint32_t v)
{
	put(k);
	put("=");
	dec(v);
	put("\n");
}

static void header_pair(const char *k, uint32_t a, uint32_t b)
{
	put(k);
	put("=");
	dec(a);
	put(",");
	dec(b);
	put("\n");
}

static void build_header(const struct dump *d, const struct snap *s, const struct rec *r)
{
	out = &hd;
	hd.len = 0;
	hd.full = 0;
	put("FPDUMP1\nname=");
	put(d->name);
	put("\nformat=");
	put(d->format);
	put("\naddr=0x");
	hex(s->src, 8);
	put("\n");
	header_line("offset", HDR_SIZE);
	header_line("bytes", d->bytes);
	header_line("width", d->width);
	header_line("height", d->height);
	header_line("stride", d->stride);
	header_line("mode", (uint32_t)(int32_t)r->mode);
	header_line("mag", d->mag);
	header_pair("zoom", r->zbase, r->zrate);
	header_pair("origin", r->ox, r->oy);
	put("clip=");
	sdec(r->te_x);
	put(",");
	sdec(r->te_y);
	put("\n");
	header_line("out", r->out);
	header_pair("flips", s->f0, s->f1);
	header_pair("frames", s->fc0, s->fc1);
	header_pair("resize", s->rz0, s->rz1);
	header_pair("t_ms", ((s->t0 - t_start) & 0x3FFFFFFFu) / 1000u,
		    ((s->t1 - t_start) & 0x3FFFFFFFu) / 1000u);
	header_line("copy_us", s->copy_us);
	header_line("attempts", (uint32_t)s->attempts);
	header_line("valid", (uint32_t)s->valid);
	for (unsigned i = hd.len; i < HDR_SIZE; i++)
		hdrbuf[i] = i == HDR_SIZE - 1 ? '\n' : ' ';
	out = &lg;
}

/* Writes the 512-byte header and the snapshot; returns 0 or a negative step number. */
static int write_dump(const struct dump *d)
{
	void *fp = fw_rpc_fopen(d->file, "w");
	if (!fp)
		return -1;
	int r = 0;
	if (fw_rpc_fwrite(hdrbuf, 1, HDR_SIZE, fp) != (int)HDR_SIZE)
		r = -2;
	uint8_t *src = FENYKL_UNCACHED(snapbuf);
	for (uint32_t off = 0; r == 0 && off < d->bytes; off += CHUNK) {
		uint32_t n = d->bytes - off < CHUNK ? d->bytes - off : CHUNK;
		if (fw_rpc_fwrite(src + off, 1, n, fp) != (int)n)
			r = -3;
	}
	if (fw_rpc_fclose(fp) == -1 && r == 0)
		r = -4;
	return r;
}

static struct rec last; /* newest drained record */

/* Snapshots dump d and writes it. Marks it done unless the state changed before a copy. */
static void do_dump(struct dump *d)
{
	struct snap s = {0};
	int r = 0;
	while (s.attempts < ATTEMPTS) {
		s.attempts++;
		r = attempt(d, &s);
		if (r != 0)
			break;
	}
	s.valid = r == 1;
	unsigned start = lg.len;
	put("D t");
	dec(ms());
	put(" ");
	put(d->name);
	if (r < 0) {
		put(" aborted: ");
		put(s.why);
		kv(" attempts ", (uint32_t)s.attempts);
		if (++d->aborts >= MAX_ABORTS) {
			d->done = 1;
			put(", given up");
		}
		end_line(start);
		return;
	}
	build_header(d, &s, &last);
	uint32_t tw = now_us();
	int w = write_dump(d);
	tw = since_us(tw) / 1000u;
	d->done = 1;
	d->ok = s.valid && w == 0;
	put(s.valid ? " ok" : " TORN");
	kv(" attempts ", (uint32_t)s.attempts);
	if (!s.valid) {
		put(" (");
		put(s.why);
		put(")");
	}
	kx(" src ", s.src);
	kv(" bytes ", d->bytes);
	kv(" flips ", s.f0);
	kv("-", s.f1);
	kv(" fc ", s.fc0);
	kv("-", s.fc1);
	kv(" rz ", s.rz0);
	kv("-", s.rz1);
	kv(" copy_us ", s.copy_us);
	kv(" write_ms ", tw);
	if (w != 0) {
		put(" WRITE-FAIL ");
		sdec(w);
	}
	end_line(start);
}

/* --- per-frame log --- */

static int have_last, full_left, idle_logged;
static uint32_t records, transitions, suppressed, full_sec, full_in_sec;
static uint32_t stable_since_ms;
static struct rec stable_ref;

/* Fields whose change is a transition (logged in full). Addresses that cycle are not part of it. */
static int same_state(const struct rec *a, const struct rec *b)
{
	return a->mode == b->mode && a->cam == b->cam && a->te_mode == b->te_mode && a->te_x == b->te_x &&
	       a->te_y == b->te_y && a->clx == b->clx && a->cly == b->cly && a->cpx == b->cpx &&
	       a->cpy == b->cpy && a->ychg == b->ychg && a->zbase == b->zbase && a->zrate == b->zrate &&
	       a->ox == b->ox && a->oy == b->oy && a->rate == b->rate && a->out == b->out &&
	       a->gsup == b->gsup && a->gst == b->gst && a->skip == b->skip && a->ext == b->ext &&
	       a->ctrl == b->ctrl;
}

/* State that must hold still before a dump (GUI and debug fields excluded; the window origin
   only counts in CLIP, its meaning in other modes is unknown). */
static int same_stable(const struct rec *a, const struct rec *b)
{
	return a->mode == b->mode && a->cam == b->cam && a->te_mode == b->te_mode &&
	       a->te_x == b->te_x && a->te_y == b->te_y && a->zrate == b->zrate && a->out == b->out &&
	       (a->mode != MODE_CLIP || (a->ox == b->ox && a->oy == b->oy));
}

static void log_full(const struct rec *r, uint32_t dt)
{
	unsigned start = lg.len;
	kv("F", r->flip);
	kv(" t", ((r->t - t_start) & 0x3FFFFFFFu) / 1000u);
	kv(" dt", dt);
	kv(" fc", r->fc);
	put(" m");
	sdec(r->mode);
	kv(" c", r->cur);
	kx(" raw", r->raw);
	kv(" o", r->ox);
	kv(",", r->oy);
	kv(" r", r->rate);
	kv(" d", r->didx);
	kx(":", r->dsp);
	kv(" f", r->fdidx);
	kx(":", r->fd);
	kv(" cam", r->cam);
	kv(" te", r->te_mode);
	put(",");
	sdec(r->te_x);
	put(",");
	sdec(r->te_y);
	put(" cl");
	sdec(r->clx);
	put(",");
	sdec(r->cly);
	kv(",", r->cpx);
	kv(",", r->cpy);
	kv(",", r->ychg);
	kv(" z", r->zbase);
	kv("/", r->zrate);
	kx(" yc", r->ext);
	kx(" ic", r->ctrl);
	put(" g");
	hex(r->gsup, 2);
	put(":");
	hex(r->gst, 2);
	kv(" out", r->out);
	put(" ipu");
	hex(r->ipu, 2);
	kv(" k", r->skip);
	kv(" rz", r->rz);
	end_line(start);
}

/* Per-second summary of the frames in the current window. */
static struct {
	uint32_t sec, n, fc, rz, dt_min, dt_max, p2_min, p2_max, sp_min, sp_max;
	int16_t mode;
	int open;
} win;

static void flush_window(void)
{
	if (!win.open)
		return;
	unsigned start = lg.len;
	kv("S ", win.sec);
	kv("s n", win.n);
	kv(" fc+", win.fc);
	kv(" dt", win.dt_min);
	kv("-", win.dt_max);
	put(" m");
	sdec(win.mode);
	kv(" rz+", win.rz);
	if (win.rz) {
		kv(" p2 ", win.p2_min);
		kv("-", win.p2_max);
		kv(" span ", win.sp_min);
		kv("-", win.sp_max);
	}
	end_line(start);
	win.open = 0;
}

/* dt: flip interval (0 = no consecutive predecessor); fc, rz: increments since the predecessor. */
static void account(const struct rec *r, uint32_t dt, uint32_t fc, uint32_t rz)
{
	uint32_t sec = ((r->t - t_start) & 0x3FFFFFFFu) / 1000000u;
	if (win.open && sec != win.sec)
		flush_window();
	if (!win.open) {
		win.open = 1;
		win.sec = sec;
		win.n = win.fc = win.rz = 0;
		win.dt_min = win.p2_min = win.sp_min = 0xFFFFFFFFu;
		win.dt_max = win.p2_max = win.sp_max = 0;
	}
	win.n++;
	win.fc += fc;
	win.mode = r->mode;
	if (dt) {
		win.dt_min = dt < win.dt_min ? dt : win.dt_min;
		win.dt_max = dt > win.dt_max ? dt : win.dt_max;
	}
	if (rz) {
		win.rz += rz;
		win.p2_min = r->p2 < win.p2_min ? r->p2 : win.p2_min;
		win.p2_max = r->p2 > win.p2_max ? r->p2 : win.p2_max;
		win.sp_min = r->span < win.sp_min ? r->span : win.sp_min;
		win.sp_max = r->span > win.sp_max ? r->span : win.sp_max;
	}
}

static void drain(void)
{
	while (tail != head) {
		struct rec r = ring[tail % RING];
		tail++;
		records++;
		uint32_t dt = have_last && r.flip == last.flip + 1 ? (r.t - last.t) & 0x3FFFFFFFu : 0;
		if (!have_last || !same_state(&r, &last)) {
			transitions++;
			full_left = FULL_AFTER + 1;
		}
		if (have_last && r.flip != last.flip + 1) {
			unsigned start = lg.len;
			kv("G lost records before F", r.flip);
			end_line(start);
		}
		uint32_t sec = ((r.t - t_start) & 0x3FFFFFFFu) / 1000000u;
		if (sec != full_sec) {
			full_sec = sec;
			full_in_sec = 0;
		}
		if (full_left) {
			full_left--;
			if (full_in_sec < FULL_PER_SEC) {
				full_in_sec++;
				log_full(&r, dt);
			} else {
				suppressed++;
			}
		}
		/* the LV frame counter restarts with the live view */
		uint32_t fcd = have_last && r.fc >= last.fc ? r.fc - last.fc : 0;
		account(&r, dt, fcd, have_last ? r.rz - last.rz : 0);
		if (!have_last || !same_stable(&r, &stable_ref)) {
			stable_ref = r;
			stable_since_ms = ((r.t - t_start) & 0x3FFFFFFFu) / 1000u;
		}
		last = r;
		have_last = 1;
		idle_logged = 0;
	}
	if (have_last && !idle_logged && since_us(last_flip_t) > IDLE_MS * 1000u) {
		flush_window();
		unsigned start = lg.len;
		kv("I t", ms());
		kv(" no frames since F", last.flip);
		end_line(start);
		idle_logged = 1;
	}
}

/* --- status and main loop --- */

static uint32_t flip_install = 0xFFFFFFFFu, resize_install = 0xFFFFFFFFu;

static void log_status(const char *what)
{
	unsigned keep = lg.len;
	lg.limit = LOG_SIZE;
	kv("status ", log_attempts + 1);
	put(" ");
	put(what);
	kv(" (ok ", log_ok);
	kv(" fopen-fail ", log_open_fail);
	kv(" short ", log_short);
	kv(") t ", ms());
	kv(" flips ", flips);
	kv(" records ", records);
	kv(" transitions ", transitions);
	kv(" suppressed ", suppressed);
	kv(" overruns ", overruns);
	kv(" rz ", rz_seq);
	nl();
	put("  cpu1 flip ");
	dec(flip_cpu1);
	kv(" resize ", resize_cpu1);
	put(flip_cpu1 || resize_cpu1 ? " FAIL (hook ran on CPU1)" : " (ok)");
	put(" install flip ");
	sdec((int32_t)flip_install);
	put(" resize ");
	sdec((int32_t)resize_install);
	kx(" zoomprop ", zoom_prop);
	kx(" buf ", snapbuf);
	nl();
	put("  dumps");
	for (unsigned i = 0; i < NDUMPS; i++) {
		put(" ");
		put(dumps[i].name);
		put(dumps[i].ok ? "=ok" : dumps[i].done ? "=torn/fail" : "=-");
	}
	kv(" dropped-lines ", dropped_lines);
	nl();
	put("end of log");
	nl();
	write_log();
	lg.len = keep;
	lg.limit = LOG_SIZE - LOG_RESERVE;
}

static unsigned magnification(uint32_t zrate)
{
	if (!zoom_prop)
		return 5;
	if (zrate >= 450 && zrate < 600)
		return 5;
	if (zrate >= 650 && zrate < 750)
		return 7;
	return 0;
}

/* The dump group the current state qualifies for: magnification 0 (normal), 5 or 7; -1 none. */
static int stable_group(void)
{
	if (!have_last || since_us(last_flip_t) > IDLE_MS * 1000u)
		return -1;
	uint32_t stable = ms() - stable_since_ms;
	if (last.mode == MODE_HS && last.te_mode == 0 && stable >= NORMAL_STABLE_MS)
		return 0;
	if (last.mode == MODE_CLIP && stable >= ZOOM_STABLE_MS) {
		unsigned mag = magnification(last.zrate);
		return mag ? (int)mag : -1;
	}
	return -1;
}

/* Runs the pending dumps while their state holds; returns 1 if it tried any. */
static int schedule_dumps(void)
{
	int wrote = 0;
	for (unsigned i = 0; i < NDUMPS; i++) {
		struct dump *d = &dumps[i];
		if (d->done || (d->kind == K_YC && resize_install != 0))
			continue;
		int group = stable_group();
		if (group < 0 || d->mode != last.mode || d->mag != (unsigned)group)
			continue;
		if (!claim_buffer())
			break;
		do_dump(d);
		wrote = 1;
		drain();
	}
	release_buffer();
	return wrote;
}

static void legend(void)
{
	put("F flip t ms dt us fc LV-frame-counter m desc-mode c desc-idx raw RAW o origin r desc+16 ");
	put("d disp-idx:img1 f fd-idx:fd-addr cam camtop te take-expose(mode,x,y) cl exp-clip(x%,y%,x,y,ychg) ");
	put("z zoom-prop(base/rate) yc ext-YC ic pass2-ctrl g GUI out output ipu lock k flip-skip rz resize-seq");
	nl();
	put("S per second: n flips, fc+ sensor frames, dt flip interval us, rz+ resize passes, p2 pass-2 us, ");
	put("span flip->pass-2-end us; I idle; D dump; B buffer; G gap in the record ring");
	nl();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	t_start = now_us();
	put("fenykl framepaths, load ");
	hex(load_base, 8);
	put(" size ");
	hex(image_size, 8);
	nl();
	legend();
	zoom_prop = (uint32_t)(uintptr_t)fw_prop_get_obj(PROP_ZOOM);
	if (!dram_pointer(zoom_prop))
		zoom_prop = 0;
	kx("prop 0x9812 data ", zoom_prop);
	if (zoom_prop) {
		kv(" base ", rd16(zoom_prop));
		kv(" rate ", rd16(zoom_prop + 2));
	}
	nl();
	flip_install = (uint32_t)fenykl_hook_install(&fenykl_hook_mov_lv_disp_flip);
	resize_install = (uint32_t)fenykl_hook_install(&fenykl_hook_mov_yc_resize_pass);
	put("hook mov_lv_disp_flip ");
	sdec((int32_t)flip_install);
	put(" mov_yc_resize_pass ");
	sdec((int32_t)resize_install);
	nl();
	if (flip_install != 0) {
		fenykl_hook_remove(&fenykl_hook_mov_yc_resize_pass);
		put("no per-frame hook, done (nothing patched)");
		nl();
		log_status("final");
		return;
	}
	log_status("start");

	uint32_t last_write = 0;
	while (ms() < RUN_MS) {
		fw_OlyTaskSleep(5);
		drain();
		int wrote = schedule_dumps();
		if (wrote || ms() - last_write >= LOG_PERIOD_MS) {
			log_status("periodic");
			last_write = ms();
		}
	}
	fenykl_hook_remove(&fenykl_hook_mov_lv_disp_flip);
	fenykl_hook_remove(&fenykl_hook_mov_yc_resize_pass);
	drain();
	flush_window();
	release_buffer();
	put("run over: hooks removed");
	nl();
	log_status("final");
}
