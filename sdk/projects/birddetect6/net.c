/* The bird detector network on generated code and a SWAR max pool (net.h). */
#include "net.h"

#define BD6_HOT __attribute__((optimize("O2", "no-tree-loop-distribute-patterns")))

/* Per byte lane max(a, b): the borrow of a - b per lane (Hacker's Delight 2-18) as a mask. */
static inline uint32_t bd6_max_u8x4(uint32_t a, uint32_t b)
{
	const uint32_t h = 0x80808080u;
	uint32_t d = ((a | h) - (b & ~h)) ^ ((a ^ ~b) & h);
	uint32_t lt = ((~a & b) | (~(a ^ b) & d)) & h;

	return a ^ ((a ^ b) & ((lt << 1) - (lt >> 7)));
}

/* 2x2 max pool of the rows a, b (w bytes, w a multiple of 8) into w / 2 bytes at d: SWAR byte
   maxes of the two rows, then of the even and odd columns. All word aligned. */
static inline void bd6_maxpool2_row(const uint32_t *a, const uint32_t *b, uint32_t *d,
				    unsigned int w)
{
	for (unsigned int x = 0; x < w / 8; x++, a += 2, b += 2) {
		uint32_t v1 = bd6_max_u8x4(a[0], b[0]), v2 = bd6_max_u8x4(a[1], b[1]);
		uint32_t e = (v1 & 0x00FF00FFu) | (v2 & 0x00FF00FFu) << 8;
		uint32_t o = (v1 >> 8 & 0x00FF00FFu) | (v2 & 0xFF00FF00u);
		uint32_t m = bd6_max_u8x4(e, o);	/* bytes: out 0, 2, 1, 3 */

		d[x] = (m & 0xFF0000FFu) | (m >> 8 & 0xFF00u) | (m << 8 & 0xFF0000u);
	}
}

void bd6_net_init(struct bd6_net *n)
{
	uint32_t *p = (uint32_t *)n;

	for (unsigned int i = 0; i < sizeof(*n) / 4; i++)
		p[i] = 0;
}

void BD6_HOT bd6_net_input(struct bd6_net *n, const uint8_t *frame)
{
	for (unsigned int y = 0; y < BD6_IN_H; y++) {
		const uint32_t *s = (const uint32_t *)(frame + y * BD6_FRAME_PITCH);
		uint32_t *d = (uint32_t *)(n->in + (y + 1) * BD6_PIN_PITCH + 4);

		for (unsigned int i = 0; i < BD6_FRAME_PITCH / 4; i++)
			d[i] = s[i];
	}
}

/* fenykl-nn make_testvectors: per plane of width w, height h. */
static uint8_t bd6_pat_px(enum bd6_pattern p, unsigned int plane, unsigned int x, unsigned int y,
			  unsigned int w, unsigned int h)
{
	if (p == BD6_PAT_U0_V255)
		return plane == 0 ? 128 : plane == 1 ? 0 : 255;
	if (p == BD6_PAT_HGRAD_NEUTRAL && plane)
		return 128;
	if (p == BD6_PAT_HGRAD || p == BD6_PAT_HGRAD_NEUTRAL)
		return (uint8_t)(x * 255 / (w - 1));
	if (p == BD6_PAT_VGRAD)
		return (uint8_t)(y * 255 / (h - 1));
	return (uint8_t)((x + y) % 2 * 255);
}

void bd6_net_pattern(struct bd6_net *n, enum bd6_pattern p)
{
	for (unsigned int y = 0; y < BD6_IN_H; y++) {
		uint8_t *d = n->in + (y + 1) * BD6_PIN_PITCH + 4;

		for (unsigned int k = 0; k < BD6_IN_W / 2; k++, d += 4) {
			d[0] = bd6_pat_px(p, 0, 2 * k, y, BD6_IN_W, BD6_IN_H);
			d[1] = bd6_pat_px(p, 1, k, y, BD6_IN_W / 2, BD6_IN_H);
			d[2] = bd6_pat_px(p, 0, 2 * k + 1, y, BD6_IN_W, BD6_IN_H);
			d[3] = bd6_pat_px(p, 2, k, y, BD6_IN_W / 2, BD6_IN_H);
		}
	}
}

/* Pair words of one interior row of w pixels p, at padded column 0 of the row d. */
static void BD6_HOT bd6_pairs_row(uint32_t *d, const uint8_t *p, unsigned int w)
{
	uint32_t lo = 0;

	for (unsigned int j = 0; j < w; j++) {
		d[j] = lo | (uint32_t)p[j] << 16;
		lo = p[j];
	}
	d[w] = lo;
}

/* 2x2 max pool of c planes w x h (u8) into the pair words of padded planes. */
static void BD6_HOT bd6_pool(struct bd6_net *n, const uint8_t *in, unsigned int c, unsigned int w,
			     unsigned int h, uint32_t *out)
{
	unsigned int ow = w / 2 + 2, oplane = ow * (h / 2 + 2);

	for (unsigned int i = 0; i < c; i++) {
		for (unsigned int y = 0; y < h / 2; y++) {
			const uint32_t *a = (const uint32_t *)(in + (i * h + 2 * y) * w);

			bd6_maxpool2_row(a, a + w / 4, n->row, w);
			bd6_pairs_row(out + i * oplane + (y + 1) * ow, (const uint8_t *)n->row,
				      w / 2);
		}
	}
}

static void bd6_pause(bd6_pause_fn *pause, void *ctx, enum bd6_stage st, bool end)
{
	if (pause)
		pause(ctx, st, end);
}

/* Power-of-two conv3x3, output h x w, from pair words of padded planes (row stride w + 2), block
   after block; a call's first output pixel (x, y) at out + y * pitch + x * step. Pause after each
   output row of a block. */
static void BD6_HOT bd6_pot(const struct bd6_pot *t, const uint32_t *in, unsigned int w,
			    unsigned int h, uint8_t *out, unsigned int pitch, unsigned int step,
			    bd6_pause_fn *pause, void *ctx, enum bd6_stage st)
{
	for (unsigned int b = 0; b < t->nblk; b++) {
		bd6_gen_fn *fn = t->blk[b];

		for (unsigned int y = 0; y < h; y++) {
			for (unsigned int x = 0; x < w; x += 4)
				fn(in + y * (w + 2) + x, out + y * pitch + x * step);
			bd6_pause(pause, ctx, st, b + 1 == t->nblk && y + 1 == h);
		}
	}
}

void bd6_net_run(struct bd6_net *n, bd6_pause_fn *pause, void *ctx)
{
	uint32_t *src = n->pa;

	for (unsigned int y = 0; y < BD6_L0_H; y++) {
		const uint8_t *in = n->in + 2 * y * BD6_PIN_PITCH;
		uint8_t *out = n->l0 + y * BD6_L0_W;

		for (unsigned int x = 0; x < BD6_L0_W; x += BD6_GEN_PX)
			bd6_stem(in + 4 * x, out + x);
		bd6_pause(pause, ctx, BD6_ST_L0, y + 1 == BD6_L0_H);
	}
	bd6_pool(n, n->l0, BD6_L0_C, BD6_L0_W, BD6_L0_H, n->p1);
	bd6_pause(pause, ctx, BD6_ST_L1, true);
	bd6_pot(&bd6_l2, n->p1, BD6_L2_W, BD6_L2_H, n->l2, BD6_L2_W, 1, pause, ctx, BD6_ST_L2);
	bd6_pool(n, n->l2, BD6_L2_C, BD6_L2_W, BD6_L2_H, n->pa);
	bd6_pause(pause, ctx, BD6_ST_L3, true);
	for (unsigned int l = 0; l + 1 < BD6_TRUNK; l++) {
		uint32_t *next = src == n->pa ? n->pb : n->pa;

		bd6_pot(&bd6_trunk[l], src, BD6_GRID_W, BD6_GRID_H, (uint8_t *)(next + BD6_P3_W),
			4 * BD6_P3_W, 4, pause, ctx, BD6_ST_L4 + l);
		src = next;
	}
	bd6_pot(&bd6_trunk[BD6_TRUNK - 1], src, BD6_GRID_W, BD6_GRID_H, n->l9, BD6_GRID_W, 1, pause,
		ctx, BD6_ST_L4 + BD6_TRUNK - 1);
	for (unsigned int y = 0; y < BD6_GRID_H; y++)
		for (unsigned int x = 0; x < BD6_GRID_W; x += BD6_GEN_PX)
			bd6_head(n->l9 + y * BD6_GRID_W + x, n->logit + y * BD6_GRID_W + x);
	bd6_pause(pause, ctx, BD6_ST_L10, true);
}

uint32_t bd6_logit_sum(const int32_t *logit)
{
	uint32_t sum = 0;

	for (unsigned int i = 0; i < BD6_LOGITS; i++)
		sum = ((sum << 1) | (sum >> 31)) + (uint32_t)logit[i];
	return sum;
}
