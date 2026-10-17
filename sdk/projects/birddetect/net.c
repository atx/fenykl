/* The bird detector network in plain integer C (net.h; fenykl-nn docs/intspec.md). */
#include "net.h"

#define BD_HOT __attribute__((optimize("O2")))

/* Output rows per call between two pause callbacks. */
#define BD_STEM_BAND 4u		/* even: the pool takes row pairs */
#define BD_L2_BAND 2u
#define BD_TRUNK_BAND 1u

void bd_net_init(struct bd_net *n)
{
	uint8_t *p = (uint8_t *)n;

	for (unsigned int i = 0; i < sizeof(*n); i++)
		p[i] = 0;
}

static inline uint8_t bd_u8(int32_t v)
{
	if (v < 0)
		return 0;
	if (v > 255)
		return 255;
	return (uint8_t)v;
}

/* L0: int8 conv3x3 stride 2, 1 -> 12, output rows y0..y1-1. */
static void BD_HOT bd_stem(struct bd_net *n, unsigned int y0, unsigned int y1)
{
	for (unsigned int o = 0; o < BD_L0_C; o++) {
		const int8_t *w = bd_l0_w[o];
		int32_t b = bd_l0_b[o];
		unsigned int s = bd_l0_s[o];

		for (unsigned int y = y0; y < y1; y++) {
			const uint8_t *r = n->in + 2 * y * BD_PIN_W;
			uint8_t *out = n->l0 + (o * BD_L0_H + y) * BD_L0_W;

			for (unsigned int x = 0; x < BD_L0_W; x++, r += 2) {
				int32_t acc = b + w[0] * r[0] + w[1] * r[1] + w[2] * r[2] +
					      w[3] * r[BD_PIN_W] + w[4] * r[BD_PIN_W + 1] +
					      w[5] * r[BD_PIN_W + 2] + w[6] * r[2 * BD_PIN_W] +
					      w[7] * r[2 * BD_PIN_W + 1] + w[8] * r[2 * BD_PIN_W + 2];

				out[x] = bd_u8(acc >> s);
			}
		}
	}
}

/* 2x2 max pool of c planes w x h into the interior of padded planes, output rows y0..y1-1. */
static void BD_HOT bd_pool(const uint8_t *in, unsigned int c, unsigned int w, unsigned int h,
			   uint8_t *out, unsigned int y0, unsigned int y1)
{
	unsigned int ow = w / 2 + 2, oplane = ow * (h / 2 + 2);

	for (unsigned int i = 0; i < c; i++) {
		for (unsigned int y = y0; y < y1; y++) {
			const uint8_t *a = in + (i * h + 2 * y) * w, *b = a + w;
			uint8_t *d = out + i * oplane + (y + 1) * ow + 1;

			for (unsigned int x = 0; x < w / 2; x++, a += 2, b += 2) {
				uint8_t m = a[0] > a[1] ? a[0] : a[1];
				uint8_t m2 = b[0] > b[1] ? b[0] : b[1];

				d[x] = m > m2 ? m : m2;
			}
		}
	}
}

/* Pair words of n pixels (net.h); the pixel after the last is 0. */
static void BD_HOT bd_pairs(uint32_t *d, const uint8_t *p, unsigned int n)
{
	uint32_t lo = p[0];

	for (unsigned int i = 0; i < n; i++) {
		uint32_t hi = i + 1 < n ? p[i + 1] : 0;

		d[i] = lo | hi << 16;
		lo = hi;
	}
}

/* Output geometry of a ternary layer: row stride, plane stride, offset of pixel (0, 0). */
struct bd_dst {
	uint8_t *p;
	unsigned int w, plane, origin;
};

/* Lane value 0x8000 + acc back to the signed accumulator, rescaled. */
static inline uint8_t bd_lane(uint32_t lane, unsigned int s)
{
	return bd_u8(((int32_t)lane - 0x8000) >> s);
}

/*
 * Ternary conv3x3 stride 1, output rows y0..y1-1 of width w (a multiple of 4), from the pair words of padded
 * planes of row stride in_w. Each 16-bit lane holds 0x8000 + acc: gen_weights.py checks that the
 * accumulator bound stays below 2^15, so the lanes never carry into each other and the result is
 * exact.
 */
static void BD_HOT bd_tern(const struct bd_tern *t, const uint32_t *in, unsigned int in_w,
			   const struct bd_dst *dst, unsigned int w, unsigned int y0,
			   unsigned int y1)
{
	const uint16_t *off = t->off;

	for (unsigned int o = 0; o < t->cout; o++) {
		unsigned int np = t->npos[o], nn = t->nneg[o];
		const uint16_t *pos = off, *pend = off + np, *nend = pend + nn;
		uint32_t init = (uint32_t)(0x8000 + t->bias[o]) * 0x10001u;
		unsigned int s = t->shift[o];

		off = nend;
		for (unsigned int y = y0; y < y1; y++) {
			const uint8_t *r = (const uint8_t *)(in + y * in_w);
			uint8_t *out = dst->p + o * dst->plane + dst->origin + y * dst->w;

			for (unsigned int x = 0; x < w; x += 4, r += 16) {
				uint32_t a = init, b = init;
				const uint16_t *k;

				for (k = pos; k < pend; k++) {
					const uint32_t *q = (const uint32_t *)(r + *k);

					a += q[0];
					b += q[2];
				}
				for (; k < nend; k++) {
					const uint32_t *q = (const uint32_t *)(r + *k);

					a -= q[0];
					b -= q[2];
				}
				out[x] = bd_lane(a & 0xFFFFu, s);
				out[x + 1] = bd_lane(a >> 16, s);
				out[x + 2] = bd_lane(b & 0xFFFFu, s);
				out[x + 3] = bd_lane(b >> 16, s);
			}
		}
	}
}

/* L9: int8 conv1x1 32 -> 1 on the last trunk output, int32 logits. */
static void BD_HOT bd_head(struct bd_net *n)
{
	const uint8_t *in = n->trunk[BD_TRUNK_LAYERS - 1] + BD_P3_W + 1;

	for (unsigned int y = 0; y < BD_GRID_H; y++) {
		for (unsigned int x = 0; x < BD_GRID_W; x++) {
			const uint8_t *p = in + y * BD_P3_W + x;
			int32_t acc = bd_l9_b;

			for (unsigned int i = 0; i < BD_TRUNK_C; i++, p += BD_P3_PLANE)
				acc += bd_l9_w[i] * *p;
			n->logit[y * BD_GRID_W + x] = acc;
		}
	}
}

static void bd_pause(bd_pause_fn *pause, void *ctx, enum bd_group g)
{
	if (pause)
		pause(ctx, g);
}

void bd_net_run(struct bd_net *n, bd_pause_fn *pause, void *ctx)
{
	static const struct bd_dst l2 = { 0, BD_L2_W, BD_L2_W * BD_L2_H, 0 };
	struct bd_dst d = l2;
	const uint8_t *in;

	for (unsigned int y = 0; y < BD_L0_H; y += BD_STEM_BAND) {
		bd_stem(n, y, y + BD_STEM_BAND);
		bd_pool(n->l0, BD_L0_C, BD_L0_W, BD_L0_H, n->l1, y / 2, (y + BD_STEM_BAND) / 2);
		bd_pause(pause, ctx, BD_G_STEM);
	}
	d.p = n->l2;
	bd_pairs(n->pairs, n->l1, BD_L0_C * BD_P1_H * BD_P1_W);
	for (unsigned int y = 0; y < BD_L2_H; y += BD_L2_BAND) {
		bd_tern(&bd_l2, n->pairs, BD_P1_W, &d, BD_L2_W, y, y + BD_L2_BAND);
		bd_pool(n->l2, BD_L2_C, BD_L2_W, BD_L2_H, n->l3, y / 2, (y + BD_L2_BAND) / 2);
		bd_pause(pause, ctx, BD_G_L2);
	}
	d.w = BD_P3_W;
	d.plane = BD_P3_PLANE;
	d.origin = BD_P3_W + 1;
	in = n->l3;
	for (unsigned int l = 0; l < BD_TRUNK_LAYERS; l++) {
		d.p = n->trunk[l];
		bd_pairs(n->pairs, in, (l ? BD_TRUNK_C : BD_L2_C) * BD_P3_PLANE);
		for (unsigned int y = 0; y < BD_GRID_H; y += BD_TRUNK_BAND) {
			bd_tern(&bd_trunk[l], n->pairs, BD_P3_W, &d, BD_GRID_W, y, y + BD_TRUNK_BAND);
			bd_pause(pause, ctx, BD_G_TRUNK);
		}
		in = d.p;
	}
	bd_head(n);
	bd_pause(pause, ctx, BD_G_TRUNK);
}

/* handoff section 1: xs = (2x + 1) * 640 // 192, ys = min((2y + 1) * 480 // 144, 478) */
void BD_HOT bd_net_sample(struct bd_net *n, const uint8_t *frame)
{
	for (unsigned int y = 0; y < BD_IN_H; y++) {
		unsigned int ys = (2 * y + 1) * BD_SRC_H / (2 * BD_IN_H);
		const uint8_t *row = frame + (ys < BD_SRC_H - 2 ? ys : BD_SRC_H - 2) * BD_SRC_W;
		uint8_t *d = bd_net_px(n, 0, y);

		for (unsigned int x = 0; x < BD_IN_W; x++)
			d[x] = row[(2 * x + 1) * BD_SRC_W / (2 * BD_IN_W)];
	}
}

void bd_net_pattern(struct bd_net *n, enum bd_pattern p)
{
	for (unsigned int y = 0; y < BD_IN_H; y++) {
		for (unsigned int x = 0; x < BD_IN_W; x++) {
			unsigned int v;

			if (p == BD_PAT_HGRAD)
				v = x * 255 / (BD_IN_W - 1);
			else if (p == BD_PAT_VGRAD)
				v = y * 255 / (BD_IN_H - 1);
			else
				v = (x + y) % 2 * 255;
			*bd_net_px(n, x, y) = (uint8_t)v;
		}
	}
}

uint32_t bd_logit_sum(const int32_t *logit)
{
	uint32_t sum = 0;

	for (unsigned int i = 0; i < BD_CELLS; i++)
		sum = ((sum << 1) | (sum >> 31)) + (uint32_t)logit[i];
	return sum;
}
