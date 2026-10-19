/*
 * The conv family: conv2d with zero padding (fenykl-nn docs/intspec.md), dense or depthwise, any
 * kh x kw, stride, dilation, int8 / ternary / int4 weights, requantized to u8 (bias holds the
 * rounding term, then >> shift and clamp to 0..255) or raw int32 (the head).
 *
 * Canonical output order: CHW. Layouts the variants use:
 *   chw   padded CHW u8 planes (as birddetect), output CHW
 *   hwc   padded HWC u8 (channels interleaved), weights [o][ky][kx][i], output HWC
 *   pair  pair words over the padded CHW planes (pixel p low 16 bits, p + 1 high 16 bits; see
 *         birddetect/net.h), output CHW
 */
#include "nnbench.h"

void nb_geo(const uint32_t *p, struct nb_geo *g)
{
	g->cin = p[NB_CIN];
	g->cout = p[NB_COUT];
	g->h = p[NB_H];
	g->w = p[NB_W];
	g->kh = p[NB_KH];
	g->kw = p[NB_KW];
	g->s = p[NB_S];
	g->d = p[NB_D];
	g->fl = p[NB_FL];
	g->ph = g->d * (g->kh / 2);
	g->pw = g->d * (g->kw / 2);
	g->hp = g->h + 2 * g->ph;
	g->wp = g->w + 2 * g->pw;
	g->ho = (g->hp - g->d * (g->kh - 1) - 1) / g->s + 1;
	g->wo = (g->wp - g->d * (g->kw - 1) - 1) / g->s + 1;
	g->taps = (g->fl & NB_FL_DW ? 1 : g->cin) * g->kh * g->kw;
}

/* Shift that spreads the outputs over 0..255: about log2(sqrt(n) * k). */
static unsigned int nb_shift(unsigned int n, unsigned int base)
{
	unsigned int s = base;

	for (; n >= 4; n >>= 2)
		s++;
	return s;
}

void nb_conv_weights(const uint32_t *p, int8_t *w, int32_t *b, uint8_t *s)
{
	struct nb_geo g;
	uint32_t x = nb_seed(p, NB_F_CONV);

	nb_geo(p, &g);
	for (unsigned int o = 0; o < g.cout; o++) {
		unsigned int left = p[NB_NZ], sh;
		int32_t bias;

		for (unsigned int t = 0; t < g.taps; t++, w++) {
			uint32_t v = nb_rand(&x);

			if (g.fl & NB_FL_TERN) {
				*w = 0;
				if (v % (g.taps - t) < left) {
					left--;
					*w = nb_rand(&x) & 1 ? 1 : -1;
				}
			} else if (g.fl & NB_FL_I4) {
				*w = (int8_t)(v % 15) - 7;
			} else {
				*w = (int8_t)(v % 255) - 127;
			}
		}
		if (g.fl & NB_FL_TERN) {
			sh = nb_shift(p[NB_NZ], 1);
			bias = (int32_t)(nb_rand(&x) % 201) - 100;
		} else {
			sh = nb_shift(g.taps, g.fl & NB_FL_I4 ? 3 : 7);
			bias = (int32_t)(nb_rand(&x) % 2001) - 1000;
		}
		if (g.fl & NB_FL_RAW)
			sh = 0;
		s[o] = (uint8_t)sh;
		b[o] = bias + (sh ? 1 << (sh - 1) : 0);
	}
}

static void nb_conv_setup(struct nb_run *r)
{
	struct nb_geo g;

	nb_geo(r->p, &g);
	r->src = nb_alloc(g.cin * g.h * g.w);
	r->w = nb_alloc(g.cout * g.taps);
	r->b = nb_alloc(4 * g.cout);
	r->s = nb_alloc(g.cout);
	if (!r->s)
		return;
	nb_fill_u8(r->src, g.cin * g.h * g.w, nb_seed(r->p, NB_F_CONV) ^ 0x5A5A);
	nb_conv_weights(r->p, r->w, r->b, r->s);
	r->n_out = g.cout * g.ho * g.wo;
	r->out_bytes = r->n_out * (g.fl & NB_FL_RAW ? 4 : 1);
	r->ops = r->n_out * g.taps;
	r->ops2 = g.fl & NB_FL_TERN ? r->n_out * r->p[NB_NZ] : r->n_out;
}

static void nb_tick(struct nb_run *r)
{
	if (r->tick)
		r->tick(r);
}

static void nb_store(struct nb_run *r, uint32_t i, int32_t acc, unsigned int s)
{
	if (r->p[NB_FL] & NB_FL_RAW)
		((int32_t *)r->out)[i] = acc;
	else
		((uint8_t *)r->out)[i] = nb_u8(acc >> s);
}

/* --- chw: the reference (plain C) and the same loop with the udf00 multiply --- */

void nb_prep_chw(struct nb_run *r)
{
	struct nb_geo g;
	uint8_t *d;

	nb_geo(r->p, &g);
	d = r->in = nb_alloc(g.cin * g.hp * g.wp);
	if (!d)
		return;
	for (unsigned int i = 0; i < g.cin * g.hp * g.wp; i++)
		d[i] = 0;
	for (unsigned int c = 0; c < g.cin; c++)
		for (unsigned int y = 0; y < g.h; y++)
			for (unsigned int x = 0; x < g.w; x++)
				d[(c * g.hp + y + g.ph) * g.wp + x + g.pw] =
					r->src[(c * g.h + y) * g.w + x];
}

#define NB_CONV_CHW(fn, MUL)							\
	void NB_HOT fn(struct nb_run *r)					\
	{									\
		struct nb_geo g;						\
		const uint8_t *in = r->rin;					\
		const int8_t *w = r->w;						\
		uint32_t i = 0;							\
										\
		nb_geo(r->p, &g);						\
		for (unsigned int o = 0; o < g.cout; o++, w += g.taps) {	\
			unsigned int c0 = g.fl & NB_FL_DW ? o : 0;		\
			unsigned int nc = g.fl & NB_FL_DW ? 1 : g.cin;		\
										\
			for (unsigned int y = 0; y < g.ho; y++) {		\
				for (unsigned int x = 0; x < g.wo; x++, i++) {	\
					const int8_t *k = w;			\
					int32_t acc = r->b[o];			\
										\
					for (unsigned int c = c0; c < c0 + nc; c++) { \
						const uint8_t *pl = in + (c * g.hp + y * g.s) * g.wp + x * g.s; \
										\
						for (unsigned int ky = 0; ky < g.kh; ky++) \
							for (unsigned int kx = 0; kx < g.kw; kx++, k++) \
								acc += MUL(*k, pl[(ky * g.wp + kx) * g.d]); \
					}					\
					nb_store(r, i, acc, r->s[o]);		\
				}						\
				nb_tick(r);					\
			}							\
		}								\
	}

#define NB_MUL_C(a, b) ((a) * (b))
NB_CONV_CHW(nb_conv_c, NB_MUL_C)
static NB_CONV_CHW(nb_conv_cu, nb_mul)

uint32_t nb_get_chw(const struct nb_run *r, const void *buf, uint32_t i)
{
	if (r->p[NB_FL] & NB_FL_RAW)
		return ((const uint32_t *)buf)[i];
	return ((const uint8_t *)buf)[i];
}

/* --- hwc: asm dot products over contiguous kw x cin runs --- */

static int nb_ok_hwc(const struct nb_run *r)
{
	return !(r->p[NB_FL] & NB_FL_DW) && r->p[NB_D] == 1 && r->p[NB_KW] * r->p[NB_CIN] % 4 == 0;
}

static int nb_ok_hwc2(const struct nb_run *r)
{
	struct nb_geo g;

	nb_geo(r->p, &g);
	return nb_ok_hwc(r) && g.wo % 2 == 0;
}

static void nb_hwc_input(struct nb_run *r, const struct nb_geo *g)
{
	uint8_t *d = r->in = nb_alloc(g->hp * g->wp * g->cin);

	if (!d)
		return;
	for (unsigned int i = 0; i < g->hp * g->wp * g->cin; i++)
		d[i] = 0;
	for (unsigned int c = 0; c < g->cin; c++)
		for (unsigned int y = 0; y < g->h; y++)
			for (unsigned int x = 0; x < g->w; x++)
				d[((y + g->ph) * g->wp + x + g->pw) * g->cin + c] =
					r->src[(c * g->h + y) * g->w + x];
}

static void nb_prep_hwc(struct nb_run *r)
{
	struct nb_geo g;
	int8_t *w;

	nb_geo(r->p, &g);
	nb_hwc_input(r, &g);
	w = r->aux = nb_alloc(g.cout * g.taps);
	if (!w || !r->in)
		return;
	for (unsigned int o = 0; o < g.cout; o++)
		for (unsigned int c = 0; c < g.cin; c++)
			for (unsigned int ky = 0; ky < g.kh; ky++)
				for (unsigned int kx = 0; kx < g.kw; kx++)
					w[((o * g.kh + ky) * g.kw + kx) * g.cin + c] =
						r->w[((o * g.cin + c) * g.kh + ky) * g.kw + kx];
}

/* s + a[0..n) . w[0..n), n a multiple of 4: macbench's udf00 kernel. */
static inline int32_t nb_dot4(const uint8_t *a, const int8_t *w, uint32_t n, int32_t s)
{
	uint32_t x, y, k = n / 4;

#define NB_MAC1(off)							\
	"movbu (" #off ",%[w]),%[y]\n\t"				\
	"movbu (" #off ",%[a]),%[x]\n\t"				\
	"extb %[y]\n\t"							\
	"udf00 %[y],%[x]\n\t"						\
	"add %[x],%[s]\n\t"
	__asm__ volatile("setlb\n\t"
			 NB_MAC1(0) NB_MAC1(1) NB_MAC1(2) NB_MAC1(3)
			 "add 4,%[a]\n\t"
			 "add 4,%[w]\n\t"
			 "add -1,%[k]\n\t"
			 "lne"
			 : [s] "+d"(s), [x] "=&d"(x), [y] "=&d"(y), [k] "+d"(k), [a] "+a"(a),
			   [w] "+a"(w)
			 :
			 : "cc", "memory");
	return s;
}

/* Two dot products sharing the weights: s[0] += a . w, s[1] += b . w. */
static inline void nb_dot4x2(const uint8_t *a, const uint8_t *b, const int8_t *w, uint32_t n,
			     int32_t s[2])
{
	int32_t s0 = s[0], s1 = s[1];
	uint32_t x, y;
	uint32_t k = n / 4;
	void *kp = (void *)k;

#define NB_MAC2(off)							\
	"movbu (" #off ",%[w]),%[y]\n\t"				\
	"movbu (" #off ",%[a]),%[x]\n\t"				\
	"extb %[y]\n\t"							\
	"udf00 %[y],%[x]\n\t"						\
	"add %[x],%[s0]\n\t"						\
	"movbu (" #off ",%[b]),%[x]\n\t"				\
	"udf00 %[y],%[x]\n\t"						\
	"add %[x],%[s1]\n\t"
	__asm__ volatile("setlb\n\t"
			 NB_MAC2(0) NB_MAC2(1) NB_MAC2(2) NB_MAC2(3)
			 "add 4,%[a]\n\t"
			 "add 4,%[b]\n\t"
			 "add 4,%[w]\n\t"
			 "add -1,%[k]\n\t"
			 "cmp 0,%[k]\n\t"
			 "lne"
			 : [s0] "+d"(s0), [s1] "+d"(s1), [x] "=&d"(x), [y] "=&d"(y),
			   [k] "+a"(kp), [a] "+a"(a), [b] "+a"(b), [w] "+a"(w)
			 :
			 : "cc", "memory");
	s[0] = s0;
	s[1] = s1;
}

static void NB_HOT nb_conv_hwc_udf(struct nb_run *r)
{
	struct nb_geo g;
	const uint8_t *in = r->rin;
	unsigned int row = 0, n, cs, rs;

	nb_geo(r->p, &g);
	n = g.kw * g.cin;
	cs = g.s * g.cin;
	rs = g.wp * g.cin;
	for (unsigned int y = 0; y < g.ho; y++, row += g.s * rs) {
		const uint8_t *a = in + row;

		for (unsigned int x = 0; x < g.wo; x++, a += cs) {
			const int8_t *w = r->aux;
			uint32_t i = (y * g.wo + x) * g.cout;

			for (unsigned int o = 0; o < g.cout; o++, i++) {
				const uint8_t *ar = a;
				int32_t acc = r->b[o];

				for (unsigned int ky = 0; ky < g.kh; ky++, ar += rs, w += n)
					acc = nb_dot4(ar, w, n, acc);
				nb_store(r, i, acc, r->s[o]);
			}
		}
		nb_tick(r);
	}
}

static void NB_HOT nb_conv_hwc_udf2(struct nb_run *r)
{
	struct nb_geo g;
	const uint8_t *in = r->rin;
	unsigned int row = 0, n, cs, rs;

	nb_geo(r->p, &g);
	n = g.kw * g.cin;
	cs = g.s * g.cin;
	rs = g.wp * g.cin;
	for (unsigned int y = 0; y < g.ho; y++, row += g.s * rs) {
		const uint8_t *a = in + row;

		for (unsigned int x = 0; x < g.wo; x += 2, a += 2 * cs) {
			const int8_t *w = r->aux;
			uint32_t i = (y * g.wo + x) * g.cout;

			for (unsigned int o = 0; o < g.cout; o++, i++) {
				const uint8_t *ar = a;
				int32_t acc[2] = { r->b[o], r->b[o] };

				for (unsigned int ky = 0; ky < g.kh; ky++, ar += rs, w += n)
					nb_dot4x2(ar, ar + cs, w, n, acc);
				nb_store(r, i, acc[0], r->s[o]);
				nb_store(r, i + g.cout, acc[1], r->s[o]);
			}
		}
		nb_tick(r);
	}
}

static uint32_t nb_get_hwc(const struct nb_run *r, const void *buf, uint32_t i)
{
	struct nb_geo g;
	uint32_t plane, o, j;

	nb_geo(r->p, &g);
	plane = g.ho * g.wo;
	o = i / plane;
	j = (i % plane) * g.cout + o;
	if (g.fl & NB_FL_RAW)
		return ((const uint32_t *)buf)[j];
	return ((const uint8_t *)buf)[j];
}

/* --- pair: two neighbouring pixels per 32-bit word --- */

static int nb_ok_pair(const struct nb_run *r, unsigned int group)
{
	struct nb_geo g;

	nb_geo(r->p, &g);
	return g.s == 1 && g.wo % group == 0 && !(g.fl & NB_FL_RAW) &&
	       4 * g.cin * g.hp * g.wp < 65536;
}

/* Ternary lanes hold 0x8000 + acc: the bound must stay below 2^15 (as in birddetect). */
static int nb_ok_pair_tab(const struct nb_run *r)
{
	return (r->p[NB_FL] & NB_FL_TERN) && r->p[NB_NZ] * 255 + 600 < 32768 && nb_ok_pair(r, 4);
}

/* int4 lanes: one input channel's taps per flush, |sum| < 2^15. */
static int nb_ok_pair_i4(const struct nb_run *r)
{
	return (r->p[NB_FL] & NB_FL_I4) && r->p[NB_KH] * r->p[NB_KW] * 255 * 7 < 32768 &&
	       nb_ok_pair(r, 2);
}

void nb_pair_input(struct nb_run *r)
{
	struct nb_geo g;
	const uint8_t *p;
	uint32_t *d, n;

	nb_prep_chw(r);
	p = r->in;
	nb_geo(r->p, &g);
	n = g.cin * g.hp * g.wp;
	d = nb_alloc(4 * n);
	r->in = d;
	if (!d || !p)
		return;
	for (uint32_t i = 0; i < n; i++)
		d[i] = p[i] | (uint32_t)(i + 1 < n ? p[i + 1] : 0) << 16;
}

/* Byte offset of tap t of output channel o in the pair words. */
static uint16_t nb_pair_off(const struct nb_geo *g, unsigned int o, unsigned int t)
{
	unsigned int kk = g->kh * g->kw, c = g->fl & NB_FL_DW ? o : t / kk;
	unsigned int ky = t % kk / g->kw, kx = t % g->kw;

	return (uint16_t)(4 * ((c * g->hp + ky * g->d) * g->wp + kx * g->d));
}

/* aux: pair_tab_asm's row of group sums (2 words per group of 4 pixels), then npos[cout],
   nneg[cout] and per channel the + offsets and the - offsets (birddetect's struct bd_tern built at
   run time). */
static uint16_t *nb_pair_tab(const struct nb_run *r, const struct nb_geo *g)
{
	return (uint16_t *)((uint8_t *)r->aux + 2 * g->wo);
}

void nb_prep_pair_tab(struct nb_run *r)
{
	struct nb_geo g;
	uint32_t nz = 0;
	uint16_t *t;

	nb_geo(r->p, &g);
	nb_pair_input(r);
	for (uint32_t i = 0; i < g.cout * g.taps; i++)
		nz += r->w[i] != 0;
	r->aux = nb_alloc(2 * g.wo + 2 * (2 * g.cout + nz));
	if (!r->aux || !r->in)
		return;
	t = nb_pair_tab(r, &g);
	uint16_t *off = t + 2 * g.cout;
	for (unsigned int o = 0; o < g.cout; o++) {
		const int8_t *w = r->w + o * g.taps;

		t[o] = t[g.cout + o] = 0;
		for (int sign = 1; sign >= -1; sign -= 2)
			for (unsigned int k = 0; k < g.taps; k++)
				if (w[k] == sign) {
					*off++ = nb_pair_off(&g, o, k);
					t[(sign < 0) * g.cout + o]++;
				}
	}
}

/* birddetect's bd_tern (net.c) on run-time offset lists, 4 outputs per pass. */
static void NB_HOT nb_conv_pair_tab(struct nb_run *r)
{
	struct nb_geo g;
	const uint16_t *t, *off;
	const uint32_t *in = r->rin;
	uint8_t *out = r->out;

	nb_geo(r->p, &g);
	t = nb_pair_tab(r, &g);
	off = t + 2 * g.cout;
	for (unsigned int o = 0; o < g.cout; o++) {
		const uint16_t *pos = off, *pend = off + t[o], *nend = pend + t[g.cout + o];
		uint32_t init = (uint32_t)(0x8000 + r->b[o]) * 0x10001u;
		unsigned int s = r->s[o];

		off = nend;
		for (unsigned int y = 0; y < g.ho; y++) {
			const uint8_t *row = (const uint8_t *)(in + y * g.wp);
			uint8_t *d = out + (o * g.ho + y) * g.wo;

			for (unsigned int x = 0; x < g.wo; x += 4, row += 16) {
				uint32_t a = init, b = init;
				const uint16_t *k;

				for (k = pos; k < pend; k++) {
					const uint32_t *q = (const uint32_t *)(row + *k);

					a += q[0];
					b += q[2];
				}
				for (; k < nend; k++) {
					const uint32_t *q = (const uint32_t *)(row + *k);

					a -= q[0];
					b -= q[2];
				}
				d[x] = nb_lane(a & 0xFFFFu, s);
				d[x + 1] = nb_lane(a >> 16, s);
				d[x + 2] = nb_lane(b & 0xFFFFu, s);
				d[x + 3] = nb_lane(b >> 16, s);
			}
			nb_tick(r);
		}
	}
}

/* nb_conv_pair_tab with the offset loops in asm, one call per output row. */
void NB_HOT nb_conv_pair_tab_asm(struct nb_run *r)
{
	struct nb_geo g;
	const uint16_t *t, *off;
	const uint32_t *in = r->rin;
	uint8_t *out = r->out;
	uint32_t *acc = r->aux;

	nb_geo(r->p, &g);
	t = nb_pair_tab(r, &g);
	off = t + 2 * g.cout;
	for (unsigned int o = 0; o < g.cout; o++) {
		struct nb_tern_job j = { .off = off, .npos = t[o], .nneg = t[g.cout + o],
					 .init = (uint32_t)(0x8000 + r->b[o]) * 0x10001u };
		unsigned int s = r->s[o];

		off += j.npos + j.nneg;
		for (unsigned int y = 0; y < g.ho; y++) {
			uint8_t *d = out + (o * g.ho + y) * g.wo;

			j.row = in + y * g.wp;
			j.groups = g.wo / 4;
			j.out = acc;
			nb_tern_rows(&j);
			for (unsigned int x = 0; x < g.wo; x += 4) {
				uint32_t a = acc[x / 2], b = acc[x / 2 + 1];

				d[x] = nb_lane(a & 0xFFFFu, s);
				d[x + 1] = nb_lane(a >> 16, s);
				d[x + 2] = nb_lane(b & 0xFFFFu, s);
				d[x + 3] = nb_lane(b >> 16, s);
			}
			nb_tick(r);
		}
	}
}

/* aux: the byte offsets of one channel's kh x kw taps. */
static void nb_prep_pair_i4(struct nb_run *r)
{
	struct nb_geo g;
	uint16_t *t;

	nb_geo(r->p, &g);
	nb_pair_input(r);
	t = r->aux = nb_alloc(2 * g.kh * g.kw);
	if (!t)
		return;
	for (unsigned int k = 0; k < g.kh * g.kw; k++)
		t[k] = nb_pair_off(&g, 0, k);
}

/*
 * int4 weights times pair words: one udf00 multiply gives both pixels' products (w * (p0 + p1 *
 * 2^16) = w p0 + w p1 2^16). One input channel's sums stay below 2^15, so they are split after
 * each channel: s0 = the sign-extended low half, s1 = (sum - s0) >> 16.
 */
static void NB_HOT nb_conv_pair_i4(struct nb_run *r)
{
	struct nb_geo g;
	const uint16_t *t = r->aux;
	const uint8_t *in = r->rin;
	uint8_t *out = r->out;
	unsigned int kk;

	nb_geo(r->p, &g);
	kk = g.kh * g.kw;
	for (unsigned int o = 0; o < g.cout; o++) {
		unsigned int c0 = g.fl & NB_FL_DW ? o : 0, nc = g.fl & NB_FL_DW ? 1 : g.cin;
		unsigned int s = r->s[o];

		for (unsigned int y = 0; y < g.ho; y++) {
			uint8_t *d = out + (o * g.ho + y) * g.wo;

			for (unsigned int x = 0; x < g.wo; x += 2) {
				const int8_t *w = r->w + o * g.taps;
				const uint8_t *pl = in + 4 * ((c0 * g.hp + y) * g.wp + x);
				int32_t a0 = r->b[o], a1 = a0;

				for (unsigned int c = 0; c < nc; c++, pl += 4 * g.hp * g.wp) {
					uint32_t sum = 0;
					int32_t s0;

					for (unsigned int k = 0; k < kk; k++, w++)
						sum += (uint32_t)nb_mul(*(const int32_t *)(pl + t[k]), *w);
					s0 = (int16_t)sum;
					a0 += s0;
					a1 += (int32_t)(sum - (uint32_t)s0) >> 16;
				}
				d[x] = nb_u8(a0 >> s);
				d[x + 1] = nb_u8(a1 >> s);
			}
			nb_tick(r);
		}
	}
}

/* --- gen: code generated per layer with the weights baked in (gen_kernels.py) --- */

/* gen: 2 pixels per call (int8) or ternary pair words; gen1 / gen4: int8, 1 / 4 pixels. */
static unsigned int nb_gen_px(const struct nb_run *r)
{
	return r->v->name[3] == '1' ? 1 : r->v->name[3] == '4' ? 4 : r->p[NB_FL] & NB_FL_TERN ? 4 : 2;
}

static int nb_ok_gen(const struct nb_run *r)
{
	if (r->v->name[3] == '4' && (r->p[NB_FL] & NB_FL_TERN))
		return 0;
	return nb_find_gen(r, nb_gen_px(r)) != NULL;
}

static int nb_gen_pair(const struct nb_run *r)
{
	return (r->p[NB_FL] & NB_FL_TERN) != 0;
}

static void nb_prep_gen_any(struct nb_run *r)
{
	struct nb_geo g;

	r->code = (uint32_t)(r->gen->end - r->gen->beg);
	if (nb_gen_pair(r)) {
		nb_pair_input(r);
		return;
	}
	nb_geo(r->p, &g);
	nb_hwc_input(r, &g);
}

static void nb_prep_gen(struct nb_run *r)
{
	r->gen = nb_find_gen(r, nb_gen_px(r));
	nb_prep_gen_any(r);
}

static void NB_HOT nb_conv_gen(struct nb_run *r)
{
	struct nb_geo g;
	void (*fn)(const void *, void *) = r->gen->fn;
	unsigned int px = r->gen->px;

	nb_geo(r->p, &g);
	if (nb_gen_pair(r)) {
		const uint32_t *in = r->rin;
		uint8_t *out = r->out;

		for (unsigned int y = 0; y < g.ho; y++) {
			for (unsigned int x = 0; x < g.wo; x += 4)
				fn(in + y * g.wp + x, out + y * g.wo + x);
			nb_tick(r);
		}
		return;
	}
	const uint8_t *in = r->rin;
	unsigned int ob = g.fl & NB_FL_RAW ? 4 : 1, cs = g.s * g.cin;

	for (unsigned int y = 0; y < g.ho; y++) {
		const uint8_t *a = in + y * g.s * g.wp * g.cin;
		uint8_t *d = (uint8_t *)r->out + y * g.wo * g.cout * ob;

		for (unsigned int x = 0; x < g.wo; x += px, a += px * cs, d += px * g.cout * ob)
			fn(a, d);
		nb_tick(r);
	}
}

static uint32_t nb_get_gen(const struct nb_run *r, const void *buf, uint32_t i)
{
	return nb_gen_pair(r) ? nb_get_chw(r, buf, i) : nb_get_hwc(r, buf, i);
}

static const struct nb_var nb_conv_vars[] = {
	{ "c", NULL, nb_prep_chw, nb_conv_c, nb_get_chw },
	{ "cu", NULL, nb_prep_chw, nb_conv_cu, nb_get_chw },
	{ "hwc_udf", nb_ok_hwc, nb_prep_hwc, nb_conv_hwc_udf, nb_get_hwc },
	{ "hwc_udf2", nb_ok_hwc2, nb_prep_hwc, nb_conv_hwc_udf2, nb_get_hwc },
	{ "pair_tab", nb_ok_pair_tab, nb_prep_pair_tab, nb_conv_pair_tab, nb_get_chw },
	{ "pair_tab_asm", nb_ok_pair_tab, nb_prep_pair_tab, nb_conv_pair_tab_asm, nb_get_chw },
	{ "pair_i4", nb_ok_pair_i4, nb_prep_pair_i4, nb_conv_pair_i4, nb_get_chw },
	{ "gen", nb_ok_gen, nb_prep_gen, nb_conv_gen, nb_get_gen },
	{ "gen1", nb_ok_gen, nb_prep_gen, nb_conv_gen, nb_get_gen },
	{ "gen4", nb_ok_gen, nb_prep_gen, nb_conv_gen, nb_get_gen },
};

const struct nb_fam nb_fam_conv = {
	"conv",
	{ "cin", "cout", "h", "w", "kh", "kw", "s", "d", "fl", "nz" },
	"mac",
	"out",	/* ternary: non-zero weight applications */
	nb_conv_setup,
	nb_conv_vars,
	sizeof(nb_conv_vars) / sizeof(nb_conv_vars[0]),
	NULL,
};
