/* The small families: pooling, element-wise ops, head post-processing, input preparation and
   memory. */
#include "nnbench.h"

static uint32_t nb_get_u8(const struct nb_run *r, const void *buf, uint32_t i)
{
	(void)r;
	return ((const uint8_t *)buf)[i];
}

static uint32_t nb_get_u32(const struct nb_run *r, const void *buf, uint32_t i)
{
	(void)r;
	return ((const uint32_t *)buf)[i];
}

static void nb_prep_src(struct nb_run *r)
{
	r->in = r->src;
}

static void nb_tick(struct nb_run *r)
{
	if (r->tick)
		r->tick(r);
}

/* --- pool: CHW u8 [c][h][w] --- */

enum { NB_POOL_OP, NB_POOL_C, NB_POOL_H, NB_POOL_W };
enum { NB_POOL_MAX2, NB_POOL_AVG2, NB_POOL_GAP };

static void nb_pool_setup(struct nb_run *r)
{
	uint32_t c = r->p[NB_POOL_C], n = c * r->p[NB_POOL_H] * r->p[NB_POOL_W];

	r->src = nb_alloc(n);
	if (!r->src)
		return;
	nb_fill_u8(r->src, n, nb_seed(r->p, NB_F_POOL));
	r->n_out = r->p[NB_POOL_OP] == NB_POOL_GAP ? c : n / 4;
	r->out_bytes = r->n_out;
	r->ops = r->n_out;
	r->ops2 = n;
}

static void NB_HOT nb_run_pool(struct nb_run *r)
{
	unsigned int c = r->p[NB_POOL_C], h = r->p[NB_POOL_H], w = r->p[NB_POOL_W], n = h * w;
	const uint8_t *in = r->rin;
	uint8_t *d = r->out;

	if (!n)
		return;
	if (r->p[NB_POOL_OP] == NB_POOL_GAP) {
		for (unsigned int i = 0; i < c; i++, in += n) {
			uint32_t s = 0;

			for (unsigned int j = 0; j < n; j++)
				s += in[j];
			d[i] = (uint8_t)((s + n / 2) / n);
		}
		return;
	}
	for (unsigned int i = 0; i < c * h / 2; i++, in += 2 * w) {
		const uint8_t *a = in, *b = in + w;

		for (unsigned int x = 0; x < w / 2; x++, a += 2, b += 2) {
			if (r->p[NB_POOL_OP] == NB_POOL_MAX2) {
				uint8_t m = a[0] > a[1] ? a[0] : a[1];
				uint8_t m2 = b[0] > b[1] ? b[0] : b[1];

				*d++ = m > m2 ? m : m2;
			} else {
				*d++ = (uint8_t)((a[0] + a[1] + b[0] + b[1] + 2) >> 2);
			}
		}
	}
}

static int nb_ok_pool_w(const struct nb_run *r)
{
	return r->p[NB_POOL_OP] != NB_POOL_GAP && r->p[NB_POOL_W] % 8 == 0;
}

/* Word loads, 8 input columns per step: max2 by nb_maxpool2_row (nb_kern.h), avg2 as 16-bit lane
   sums (as prep box2 cw). */
static void NB_HOT nb_run_pool_w(struct nb_run *r)
{
	unsigned int c = r->p[NB_POOL_C], h = r->p[NB_POOL_H], w = r->p[NB_POOL_W];
	const uint32_t *in = r->rin;
	uint8_t *d = r->out;

	for (unsigned int i = 0; i < c * h / 2; i++, in += w / 2) {
		const uint32_t *a = in, *b = in + w / 4;

		if (r->p[NB_POOL_OP] == NB_POOL_MAX2) {
			nb_maxpool2_row(a, b, (uint32_t *)d, w);
			d += w / 2;
			continue;
		}
		for (unsigned int x = 0; x < w / 8; x++, a += 2, b += 2, d += 4) {
			for (unsigned int k = 0; k < 2; k++) {
				uint32_t l = (a[k] & 0x00FF00FFu) + (a[k] >> 8 & 0x00FF00FFu) +
					     (b[k] & 0x00FF00FFu) + (b[k] >> 8 & 0x00FF00FFu) + 0x00020002u;

				d[2 * k] = (uint8_t)((l & 0xFFFF) >> 2);
				d[2 * k + 1] = (uint8_t)(l >> 18);
			}
		}
	}
}

static const struct nb_var nb_pool_vars[] = {
	{ "c", NULL, nb_prep_src, nb_run_pool, nb_get_u8 },
	{ "cw", nb_ok_pool_w, nb_prep_src, nb_run_pool_w, nb_get_u8 },
};

const struct nb_fam nb_fam_pool = {
	"pool", { "op", "c", "h", "w" }, "out", "in", nb_pool_setup,
	nb_pool_vars, 2, NULL,
};

/* --- elem: n = c x h x w elements --- */

enum { NB_EL_OP, NB_EL_C, NB_EL_H, NB_EL_W };
enum {
	NB_EL_RQ_SHIFT,	/* int32 -> u8: (v + 128) >> 8, clamp */
	NB_EL_RQ_MUL,	/* int32 -> u8: (v * 23170 + 2^15) >> 16, clamp */
	NB_EL_CLAMP6,	/* u8 -> min(v, 170): ReLU6 at a 6/9 scale */
	NB_EL_ADD,	/* residual: (a * 181 + b * 90 + 128) >> 8, clamp */
	NB_EL_UP2,	/* nearest 2x upsample of [c][h][w] */
	NB_EL_CAT_CHW,	/* two [c][h][w] -> [2c][h][w] */
	NB_EL_CAT_HWC,	/* two [h][w][c] -> [h][w][2c] */
	NB_EL_PAIRS,	/* u8 -> pair words */
};

static void nb_elem_setup(struct nb_run *r)
{
	uint32_t n = r->p[NB_EL_C] * r->p[NB_EL_H] * r->p[NB_EL_W], op = r->p[NB_EL_OP];
	uint32_t x = nb_seed(r->p, NB_F_ELEM);
	uint32_t in = op <= NB_EL_RQ_MUL ? 4 * n : n;

	r->src = nb_alloc(in);
	r->src2 = nb_alloc(n);
	if (!r->src2)
		return;
	nb_fill_u8(r->src2, n, x ^ 0x77);
	if (op <= NB_EL_RQ_MUL)
		for (uint32_t i = 0; i < n; i++)
			((int32_t *)r->src)[i] = (int32_t)(nb_rand(&x) % 80001) - 40000;
	else
		nb_fill_u8(r->src, n, x);
	r->ops = n;
	r->ops2 = n;
	r->n_out = n;
	if (op == NB_EL_UP2)
		r->n_out = 4 * n;
	if (op == NB_EL_CAT_CHW || op == NB_EL_CAT_HWC)
		r->n_out = 2 * n;
	r->out_bytes = op == NB_EL_PAIRS ? 4 * n : r->n_out;
}

#define NB_ELEM_RUN(fn, MUL)							\
	static void NB_HOT fn(struct nb_run *r)					\
	{									\
		uint32_t c = r->p[NB_EL_C], h = r->p[NB_EL_H], w = r->p[NB_EL_W]; \
		uint32_t n = c * h * w;						\
		const uint8_t *a = r->rin, *b = r->src2;			\
		const int32_t *v = r->rin;					\
		uint8_t *d = r->out;						\
										\
		switch (r->p[NB_EL_OP]) {					\
		case NB_EL_RQ_SHIFT:						\
			for (uint32_t i = 0; i < n; i++)			\
				d[i] = nb_u8((v[i] + 128) >> 8);		\
			break;							\
		case NB_EL_RQ_MUL:						\
			for (uint32_t i = 0; i < n; i++)			\
				d[i] = nb_u8((MUL(v[i], 23170) + 32768) >> 16);	\
			break;							\
		case NB_EL_CLAMP6:						\
			for (uint32_t i = 0; i < n; i++)			\
				d[i] = a[i] < 170 ? a[i] : 170;			\
			break;							\
		case NB_EL_ADD:							\
			for (uint32_t i = 0; i < n; i++)			\
				d[i] = nb_u8((MUL(a[i], 181) + MUL(b[i], 90) + 128) >> 8); \
			break;							\
		case NB_EL_UP2:							\
			for (uint32_t y = 0; y < c * h; y++, a += w, d += 4 * w) { \
				for (uint32_t x = 0; x < w; x++)		\
					d[2 * x] = d[2 * x + 1] = a[x];		\
				for (uint32_t x = 0; x < 2 * w; x++)		\
					d[2 * w + x] = d[x];			\
			}							\
			break;							\
		case NB_EL_CAT_CHW:						\
			for (uint32_t i = 0; i < n; i++)			\
				d[i] = a[i];					\
			for (uint32_t i = 0; i < n; i++)			\
				d[n + i] = b[i];				\
			break;							\
		case NB_EL_CAT_HWC:						\
			for (uint32_t i = 0; i < h * w; i++, a += c, b += c, d += 2 * c) { \
				for (uint32_t j = 0; j < c; j++)		\
					d[j] = a[j];				\
				for (uint32_t j = 0; j < c; j++)		\
					d[c + j] = b[j];			\
			}							\
			break;							\
		case NB_EL_PAIRS:						\
			for (uint32_t i = 0; i < n; i++)			\
				((uint32_t *)r->out)[i] = a[i] | (uint32_t)(i + 1 < n ? a[i + 1] : 0) << 16; \
			break;							\
		}								\
	}

#define NB_MUL_C(a, b) ((a) * (b))
NB_ELEM_RUN(nb_run_elem_c, NB_MUL_C)
NB_ELEM_RUN(nb_run_elem_cu, nb_mul)

static int nb_ok_elem_mul(const struct nb_run *r)
{
	return r->p[NB_EL_OP] == NB_EL_RQ_MUL || r->p[NB_EL_OP] == NB_EL_ADD;
}

static int nb_ok_elem_word(const struct nb_run *r)
{
	return (r->p[NB_EL_OP] == NB_EL_UP2 || r->p[NB_EL_OP] == NB_EL_CAT_CHW) &&
	       r->p[NB_EL_W] % 4 == 0;
}

/* Word loads and stores: upsample writes each doubled pixel as one halfword, concat copies words. */
static void NB_HOT nb_run_elem_w(struct nb_run *r)
{
	uint32_t c = r->p[NB_EL_C], h = r->p[NB_EL_H], w = r->p[NB_EL_W], n = c * h * w;

	if (r->p[NB_EL_OP] == NB_EL_CAT_CHW) {
		const uint32_t *a = r->rin, *b = (const uint32_t *)r->src2;
		uint32_t *d = r->out;

		for (uint32_t i = 0; i < n / 4; i++)
			d[i] = a[i];
		for (uint32_t i = 0; i < n / 4; i++)
			d[n / 4 + i] = b[i];
		return;
	}
	const uint8_t *a = r->rin;
	uint16_t *d = r->out;

	for (uint32_t y = 0; y < c * h; y++, a += w, d += 2 * w) {
		for (uint32_t x = 0; x < w; x++)
			d[x] = (uint16_t)(a[x] | a[x] << 8);
		for (uint32_t x = 0; x < w; x++)
			d[w + x] = d[x];
	}
}

static uint32_t nb_get_elem(const struct nb_run *r, const void *buf, uint32_t i)
{
	return r->p[NB_EL_OP] == NB_EL_PAIRS ? nb_get_u32(r, buf, i) : nb_get_u8(r, buf, i);
}

static const struct nb_var nb_elem_vars[] = {
	{ "c", NULL, nb_prep_src, nb_run_elem_c, nb_get_elem },
	{ "cu", nb_ok_elem_mul, nb_prep_src, nb_run_elem_cu, nb_get_elem },
	{ "cw", nb_ok_elem_word, nb_prep_src, nb_run_elem_w, nb_get_elem },
};

const struct nb_fam nb_fam_elem = {
	"elem", { "op", "c", "h", "w" }, "elem", "elem", nb_elem_setup,
	nb_elem_vars, 3, NULL,
};

/* --- post: int32 logits of a cell grid --- */

enum { NB_PO_OP, NB_PO_CELLS, NB_PO_GW };
enum { NB_PO_SIGMOID, NB_PO_ARGMAX, NB_PO_THRESH, NB_PO_CENTROID, NB_PO_SOFTMAX };
#define NB_PO_THR 20000

/* sigmoid and softmax: one byte per cell */
static int nb_post_bytes(const struct nb_run *r)
{
	return r->p[NB_PO_OP] == NB_PO_SIGMOID || r->p[NB_PO_OP] == NB_PO_SOFTMAX;
}

static void nb_post_setup(struct nb_run *r)
{
	uint32_t n = r->p[NB_PO_CELLS], x = nb_seed(r->p, NB_F_POST);
	int32_t *l = nb_alloc(4 * n);

	r->src = (uint8_t *)l;
	uint16_t *e;

	r->w = nb_alloc(256 + 512);	/* the sigmoid table, then the exp table */
	if (!r->w)
		return;
	for (uint32_t i = 0; i < n; i++)
		l[i] = (int32_t)(nb_rand(&x) % 80001) - 40000;
	for (unsigned int i = 0; i < 256; i++)
		((uint8_t *)r->w)[i] = (uint8_t)(i * i >> 8);
	/* exp(-k / 32) in 12 bits: 63519 / 65536 = exp(-1 / 32) */
	e = (uint16_t *)((uint8_t *)r->w + 256);
	for (uint32_t k = 0, v = 4095u << 16; k < 256; k++, v = (uint32_t)nb_mul((int32_t)(v >> 16), 63519))
		e[k] = (uint16_t)((v + 32768) >> 16);
	r->ops = n;
	r->ops2 = n;
	r->n_out = nb_post_bytes(r) ? n : r->p[NB_PO_OP] == NB_PO_CENTROID ? 5 : 1;
	r->out_bytes = nb_post_bytes(r) ? n : 4 * (n + 1);
}

#define NB_POST_RUN(fn, MUL)							\
	static void NB_HOT fn(struct nb_run *r)					\
	{									\
		uint32_t n = r->p[NB_PO_CELLS], gw = r->p[NB_PO_GW];		\
		const int32_t *l = r->rin;					\
		uint32_t *o = r->out;						\
										\
		switch (r->p[NB_PO_OP]) {					\
		case NB_PO_SIGMOID: {						\
			const uint8_t *t = (const uint8_t *)r->w;		\
										\
			for (uint32_t i = 0; i < n; i++)			\
				((uint8_t *)r->out)[i] = t[nb_u8((l[i] >> 8) + 128)]; \
			break;							\
		}								\
		/* exp table e[min((max - l) >> 6, 255)], then e * 256 / sum */ \
		case NB_PO_SOFTMAX: {						\
			const uint16_t *e = (const uint16_t *)((const uint8_t *)r->w + 256); \
			uint8_t *p = r->out;					\
			int32_t m = l[0];					\
			uint32_t sum = 0, rc;					\
										\
			for (uint32_t i = 1; i < n; i++)			\
				m = l[i] > m ? l[i] : m;			\
			for (uint32_t i = 0; i < n; i++) {			\
				uint32_t k = (uint32_t)(m - l[i]) >> 6;		\
										\
				p[i] = (uint8_t)(k > 255 ? 255 : k);		\
				sum += e[p[i]];					\
			}							\
			if (!sum)						\
				break;						\
			rc = (1u << 31) / sum;					\
			for (uint32_t i = 0; i < n; i++) {			\
				uint32_t q = (uint32_t)MUL((int32_t)e[p[i]], (int32_t)rc) >> 23; \
										\
				p[i] = (uint8_t)(q > 255 ? 255 : q);		\
			}							\
			break;							\
		}								\
		case NB_PO_ARGMAX: {						\
			uint32_t best = 0;					\
										\
			for (uint32_t i = 1; i < n; i++)			\
				if (l[i] > l[best])				\
					best = i;				\
			o[0] = best;						\
			break;							\
		}								\
		case NB_PO_THRESH: {						\
			uint32_t k = 0;						\
										\
			for (uint32_t i = 0; i < n; i++)			\
				if (l[i] >= NB_PO_THR)				\
					o[1 + k++] = i;				\
			o[0] = k;						\
			break;							\
		}								\
		case NB_PO_CENTROID: {						\
			int32_t sw = 0, sx = 0, sy = 0;				\
			uint32_t x = 0, y = 0;					\
										\
			for (uint32_t i = 0; i < n; i++) {			\
				int32_t v = l[i] - NB_PO_THR;			\
										\
				if (v >= 0) {					\
					v >>= 6;				\
					sw += v;				\
					sx += MUL(v, (int32_t)x);		\
					sy += MUL(v, (int32_t)y);		\
				}						\
				if (++x == gw) {				\
					x = 0;					\
					y++;					\
				}						\
			}							\
			o[0] = sw;						\
			o[1] = sx;						\
			o[2] = sy;						\
			o[3] = sw ? (uint32_t)(sx * 256 / sw) : 0;		\
			o[4] = sw ? (uint32_t)(sy * 256 / sw) : 0;		\
			break;							\
		}								\
		}								\
	}

NB_POST_RUN(nb_run_post_c, NB_MUL_C)
NB_POST_RUN(nb_run_post_cu, nb_mul)

static int nb_ok_post_mul(const struct nb_run *r)
{
	return r->p[NB_PO_OP] == NB_PO_CENTROID || r->p[NB_PO_OP] == NB_PO_SOFTMAX;
}

/* Threshold lists end at their count: element 0 is a checksum of the count and the list. */
static uint32_t nb_get_post(const struct nb_run *r, const void *buf, uint32_t i)
{
	const uint32_t *o = buf;
	uint32_t s = 0;

	if (nb_post_bytes(r))
		return nb_get_u8(r, buf, i);
	if (r->p[NB_PO_OP] != NB_PO_THRESH)
		return o[i];
	for (uint32_t k = 0; k <= o[0] && k <= r->p[NB_PO_CELLS]; k++)
		s = ((s << 1) | (s >> 31)) + o[k];
	return s;
}

static const struct nb_var nb_post_vars[] = {
	{ "c", NULL, nb_prep_src, nb_run_post_c, nb_get_post },
	{ "cu", nb_ok_post_mul, nb_prep_src, nb_run_post_cu, nb_get_post },
};

const struct nb_fam nb_fam_post = {
	"post", { "op", "cells", "gw" }, "cell", "cell", nb_post_setup,
	nb_post_vars, 2, NULL,
};

/* --- prep: a 640-wide luma source of sh rows to oh x ow: a synthetic strip, or in the live modes
   the live FD frame (sh = 480) --- */

#define NB_SRC_W NB_FRAME_W

enum { NB_PR_OP, NB_PR_OW, NB_PR_OH, NB_PR_SH };
enum { NB_PR_POINT, NB_PR_BOX2, NB_PR_BOX4, NB_PR_BOXN, NB_PR_BILIN };

static void nb_prep_setup(struct nb_run *r)
{
	uint32_t n = NB_SRC_W * r->p[NB_PR_SH];

	r->n_out = r->p[NB_PR_OW] * r->p[NB_PR_OH];
	r->out_bytes = r->n_out;
	r->ops = r->n_out;
	r->ops2 = n;
	if (r->k->mode >= NB_LIVE) {
		r->src = (uint8_t *)r->live_addr;
		return;
	}
	r->src = nb_alloc(n);
	if (r->src)
		nb_fill_u8(r->src, n, nb_seed(r->p, NB_F_PREP));
}

/*
 * aux tables, per column then per row: point: source x, y (birddetect's (2i + 1) * S / (2 n));
 * boxN: start of the cell (end = next start), then rcp[area] = round(2^16 / area); bilinear:
 * x0 << 8 | fx and y0 << 8 | fy of the centre (2i + 1) * S / (2 n) - 1/2 in 1/256 pixel.
 */
static void nb_prep_prep(struct nb_run *r)
{
	uint32_t ow = r->p[NB_PR_OW], oh = r->p[NB_PR_OH], op = r->p[NB_PR_OP];
	uint32_t *t = r->aux = nb_alloc(4 * (ow + oh + 2 + 64));

	r->in = r->src;
	if (!t)
		return;
	for (uint32_t i = 0; i <= ow + oh + 1; i++) {
		uint32_t col = i <= ow, j = col ? i : i - ow - 1, n = col ? ow : oh;
		uint32_t S = col ? NB_SRC_W : r->p[NB_PR_SH];

		if (op == NB_PR_POINT || op == NB_PR_BOXN) {
			t[i] = op == NB_PR_POINT ? (2 * j + 1) * S / (2 * n) : j * S / n;
		} else {
			int32_t c = (int32_t)((2 * j + 1) * S * 256 / (2 * n)) - 128;

			if (c < 0)
				c = 0;
			if (c > (int32_t)(S - 2) * 256 + 255)
				c = (int32_t)(S - 2) * 256 + 255;
			t[i] = (uint32_t)c;
		}
	}
	for (uint32_t a = 1; a < 64; a++)
		t[ow + oh + 2 + a] = (65536 + a / 2) / a;
}

#define NB_PREP_RUN(fn, MUL)							\
	static void NB_HOT fn(struct nb_run *r)					\
	{									\
		uint32_t ow = r->p[NB_PR_OW], oh = r->p[NB_PR_OH];		\
		const uint32_t *tx = r->aux, *ty = tx + ow + 1, *rcp = ty + oh + 1; \
		const uint8_t *s = r->rin;					\
		uint8_t *d = r->out;						\
										\
		switch (r->p[NB_PR_OP]) {					\
		case NB_PR_POINT:						\
			for (uint32_t y = 0; y < oh; y++, d += ow) {		\
				const uint8_t *row = s + ty[y] * NB_SRC_W;	\
										\
				for (uint32_t x = 0; x < ow; x++)		\
					d[x] = row[tx[x]];			\
			}							\
			break;							\
		case NB_PR_BOX2:						\
		case NB_PR_BOX4: {						\
			uint32_t f = r->p[NB_PR_OP] == NB_PR_BOX2 ? 2 : 4;	\
										\
			for (uint32_t y = 0; y < oh; y++, d += ow) {		\
				const uint8_t *row = s + y * f * NB_SRC_W;	\
										\
				for (uint32_t x = 0; x < ow; x++) {		\
					uint32_t sum = 0;			\
										\
					for (uint32_t j = 0; j < f; j++)	\
						for (uint32_t i = 0; i < f; i++) \
							sum += row[j * NB_SRC_W + x * f + i]; \
					d[x] = (uint8_t)((sum + f * f / 2) / (f * f)); \
				}						\
			}							\
			break;							\
		}								\
		case NB_PR_BOXN:						\
			for (uint32_t y = 0; y < oh; y++, d += ow) {		\
				for (uint32_t x = 0; x < ow; x++) {		\
					uint32_t sum = 0;			\
										\
					for (uint32_t j = ty[y]; j < ty[y + 1]; j++) \
						for (uint32_t i = tx[x]; i < tx[x + 1]; i++) \
							sum += s[j * NB_SRC_W + i]; \
					uint32_t a = (tx[x + 1] - tx[x]) * (ty[y + 1] - ty[y]); \
					d[x] = nb_u8((MUL((int32_t)sum, (int32_t)rcp[a]) + 32768) >> 16); \
				}						\
				nb_tick(r);					\
			}							\
			break;							\
		case NB_PR_BILIN:						\
			for (uint32_t y = 0; y < oh; y++, d += ow) {		\
				const uint8_t *r0 = s + (ty[y] >> 8) * NB_SRC_W, *r1 = r0 + NB_SRC_W; \
				int32_t fy = ty[y] & 255;			\
										\
				for (uint32_t x = 0; x < ow; x++) {		\
					uint32_t x0 = tx[x] >> 8;		\
					int32_t fx = tx[x] & 255;		\
					int32_t t0 = MUL(r0[x0], 256 - fx) + MUL(r0[x0 + 1], fx); \
					int32_t t1 = MUL(r1[x0], 256 - fx) + MUL(r1[x0 + 1], fx); \
										\
					d[x] = (uint8_t)((MUL(t0, 256 - fy) + MUL(t1, fy) + 32768) >> 16); \
				}						\
			}							\
			break;							\
		}								\
	}

NB_PREP_RUN(nb_run_prep_c, NB_MUL_C)
NB_PREP_RUN(nb_run_prep_cu, nb_mul)

static int nb_ok_prep_mul(const struct nb_run *r)
{
	return r->p[NB_PR_OP] == NB_PR_BOXN || r->p[NB_PR_OP] == NB_PR_BILIN;
}

static int nb_ok_prep_word(const struct nb_run *r)
{
	return r->p[NB_PR_OP] == NB_PR_BOX2 || r->p[NB_PR_OP] == NB_PR_BOX4;
}

/* Box filters on word loads: 16-bit lanes of even and odd pixels summed (SWAR). */
static void NB_HOT nb_run_prep_w(struct nb_run *r)
{
	uint32_t ow = r->p[NB_PR_OW], oh = r->p[NB_PR_OH];
	const uint32_t *s = r->rin;
	uint8_t *d = r->out;

	if (r->p[NB_PR_OP] == NB_PR_BOX2) {
		for (uint32_t y = 0; y < oh; y++, s += NB_SRC_W / 2, d += ow) {
			for (uint32_t x = 0; x < ow / 2; x++) {
				uint32_t a = s[x], b = s[NB_SRC_W / 4 + x];
				uint32_t l = (a & 0x00FF00FFu) + (a >> 8 & 0x00FF00FFu) +
					     (b & 0x00FF00FFu) + (b >> 8 & 0x00FF00FFu) + 0x00020002u;

				d[2 * x] = (uint8_t)((l & 0xFFFF) >> 2);
				d[2 * x + 1] = (uint8_t)(l >> 18);
			}
		}
		return;
	}
	for (uint32_t y = 0; y < oh; y++, s += NB_SRC_W, d += ow) {
		for (uint32_t x = 0; x < ow; x++) {
			uint32_t l = 0;

			for (uint32_t j = 0; j < 4; j++) {
				uint32_t a = s[j * NB_SRC_W / 4 + x];

				l += (a & 0x00FF00FFu) + (a >> 8 & 0x00FF00FFu);
			}
			d[x] = (uint8_t)(((l & 0xFFFF) + (l >> 16) + 8) >> 4);
		}
	}
}

static const struct nb_var nb_prep_vars[] = {
	{ "c", NULL, nb_prep_prep, nb_run_prep_c, nb_get_u8 },
	{ "cu", nb_ok_prep_mul, nb_prep_prep, nb_run_prep_cu, nb_get_u8 },
	{ "cw", nb_ok_prep_word, nb_prep_prep, nb_run_prep_w, nb_get_u8 },
};

const struct nb_fam nb_fam_prep = {
	"prep", { "op", "ow", "oh", "sh" }, "out", "srcbyte", nb_prep_setup,
	nb_prep_vars, 3, NULL,
};

/* --- mem: copy, set, sequential and strided reads over size bytes --- */

enum { NB_ME_OP, NB_ME_SIZE, NB_ME_STRIDE };
enum { NB_ME_COPY, NB_ME_SET, NB_ME_SUM, NB_ME_STRIDED };

static void nb_mem_setup(struct nb_run *r)
{
	uint32_t n = r->p[NB_ME_SIZE], op = r->p[NB_ME_OP];

	r->src = nb_alloc(n);
	if (!r->src)
		return;
	nb_fill_u8(r->src, n, nb_seed(r->p, NB_F_MEM));
	r->n_out = op == NB_ME_COPY || op == NB_ME_SET ? n : 1;
	r->out_bytes = r->n_out < 4 ? 4 : r->n_out;
	r->ops = op == NB_ME_STRIDED ? n / r->p[NB_ME_STRIDE] : n;
	r->ops2 = n;
}

static void NB_HOT nb_run_mem_c(struct nb_run *r)
{
	uint32_t n = r->p[NB_ME_SIZE], st = r->p[NB_ME_STRIDE];
	const uint8_t *s = r->rin;
	uint8_t *d = r->out;
	uint32_t sum = 0;

	switch (r->p[NB_ME_OP]) {
	case NB_ME_COPY:
		for (uint32_t i = 0; i < n; i++)
			d[i] = s[i];
		return;
	case NB_ME_SET:
		for (uint32_t i = 0; i < n; i++)
			d[i] = 0x5A;
		return;
	case NB_ME_SUM:
		for (uint32_t i = 0; i < n; i++)
			sum += s[i];
		break;
	case NB_ME_STRIDED:
		for (uint32_t i = 0; i < n; i += st)
			sum += s[i];
		break;
	}
	*(uint32_t *)d = sum;
}

static int nb_ok_mem_word(const struct nb_run *r)
{
	return r->p[NB_ME_OP] != NB_ME_STRIDED && r->p[NB_ME_SIZE] % 512 == 0;
}

static void NB_HOT nb_run_mem_w(struct nb_run *r)
{
	uint32_t n = r->p[NB_ME_SIZE] / 4;
	const uint32_t *s = r->rin;
	uint32_t *d = r->out, sum = 0;

	switch (r->p[NB_ME_OP]) {
	case NB_ME_COPY:
		for (uint32_t i = 0; i < n; i++)
			d[i] = s[i];
		return;
	case NB_ME_SET:
		for (uint32_t i = 0; i < n; i++)
			d[i] = 0x5A5A5A5Au;
		return;
	}
	/* 128 words of two 16-bit lanes stay below 2^16 */
	for (uint32_t i = 0; i < n; i += 128) {
		uint32_t l = 0;

		for (uint32_t j = i; j < i + 128; j++)
			l += (s[j] & 0x00FF00FFu) + (s[j] >> 8 & 0x00FF00FFu);
		sum += (l & 0xFFFF) + (l >> 16);
	}
	*d = sum;
}

static int nb_ok_mem_asm(const struct nb_run *r)
{
	return r->p[NB_ME_OP] == NB_ME_COPY && r->p[NB_ME_SIZE] % 32 == 0;
}

/* 8 words per pass, loads two ahead of their stores. */
static void nb_run_mem_asm(struct nb_run *r)
{
	const uint32_t *s = r->rin;
	uint32_t *d = r->out, k = r->p[NB_ME_SIZE] / 32, x, y;

#define NB_CP2(o)							\
	"mov (" #o ",%[s]),%[x]\n\t"					\
	"mov (" #o "+4,%[s]),%[y]\n\t"					\
	"mov %[x],(" #o ",%[d])\n\t"					\
	"mov %[y],(" #o "+4,%[d])\n\t"
	__asm__ volatile("setlb\n\t"
			 NB_CP2(0) NB_CP2(8) NB_CP2(16) NB_CP2(24)
			 "add 32,%[s]\n\t"
			 "add 32,%[d]\n\t"
			 "add -1,%[k]\n\t"
			 "lne"
			 : [x] "=&d"(x), [y] "=&d"(y), [k] "+d"(k), [s] "+a"(s), [d] "+a"(d)
			 :
			 : "cc", "memory");
}

static uint32_t nb_get_mem(const struct nb_run *r, const void *buf, uint32_t i)
{
	return r->n_out == 1 ? nb_get_u32(r, buf, i) : nb_get_u8(r, buf, i);
}

static const struct nb_var nb_mem_vars[] = {
	{ "c", NULL, nb_prep_src, nb_run_mem_c, nb_get_mem },
	{ "cw", nb_ok_mem_word, nb_prep_src, nb_run_mem_w, nb_get_mem },
	{ "asm", nb_ok_mem_asm, nb_prep_src, nb_run_mem_asm, nb_get_mem },
};

const struct nb_fam nb_fam_mem = {
	"mem", { "op", "size", "stride" }, "byte", "byte", nb_mem_setup,
	nb_mem_vars, 3, NULL,
};
