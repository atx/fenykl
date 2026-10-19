/* The bird detector network on generated code and nnbench's max pool (net.h). */
#include "net.h"
#include "../nnbench/nb_kern.h"

void bd3_net_init(struct bd3_net *n)
{
	uint32_t *p = (uint32_t *)n;

	for (unsigned int i = 0; i < sizeof(*n) / 4; i++)
		p[i] = 0;
}

void NB_HOT bd3_net_input(struct bd3_net *n, const uint8_t *frame)
{
	for (unsigned int y = 0; y < BD3_IN_H; y++) {
		const uint32_t *s = (const uint32_t *)(frame + y * BD3_FRAME_PITCH);
		uint32_t *d = (uint32_t *)(n->in + (y + 1) * BD3_PIN_PITCH + 4);

		for (unsigned int i = 0; i < BD3_FRAME_PITCH / 4; i++)
			d[i] = s[i];
	}
}

/* fenykl-nn make_testvectors: per plane of width w, height h. */
static uint8_t bd3_pat_px(enum bd3_pattern p, unsigned int plane, unsigned int x, unsigned int y,
			  unsigned int w, unsigned int h)
{
	if (p == BD3_PAT_U0_V255)
		return plane == 0 ? 128 : plane == 1 ? 0 : 255;
	if (p == BD3_PAT_HGRAD_NEUTRAL && plane)
		return 128;
	if (p == BD3_PAT_HGRAD || p == BD3_PAT_HGRAD_NEUTRAL)
		return (uint8_t)(x * 255 / (w - 1));
	if (p == BD3_PAT_VGRAD)
		return (uint8_t)(y * 255 / (h - 1));
	return (uint8_t)((x + y) % 2 * 255);
}

void bd3_net_pattern(struct bd3_net *n, enum bd3_pattern p)
{
	for (unsigned int y = 0; y < BD3_IN_H; y++) {
		uint8_t *d = n->in + (y + 1) * BD3_PIN_PITCH + 4;

		for (unsigned int k = 0; k < BD3_IN_W / 2; k++, d += 4) {
			d[0] = bd3_pat_px(p, 0, 2 * k, y, BD3_IN_W, BD3_IN_H);
			d[1] = bd3_pat_px(p, 1, k, y, BD3_IN_W / 2, BD3_IN_H);
			d[2] = bd3_pat_px(p, 0, 2 * k + 1, y, BD3_IN_W, BD3_IN_H);
			d[3] = bd3_pat_px(p, 2, k, y, BD3_IN_W / 2, BD3_IN_H);
		}
	}
}

/* Pair words of one interior row of w pixels p, at padded column 0 of the row d. */
static void NB_HOT bd3_pairs_row(uint32_t *d, const uint8_t *p, unsigned int w)
{
	uint32_t lo = 0;

	for (unsigned int j = 0; j < w; j++) {
		d[j] = lo | (uint32_t)p[j] << 16;
		lo = p[j];
	}
	d[w] = lo;
}

/* 2x2 max pool of c planes w x h (u8) into the pair words of padded planes. */
static void NB_HOT bd3_pool(struct bd3_net *n, const uint8_t *in, unsigned int c, unsigned int w,
			    unsigned int h, uint32_t *out)
{
	unsigned int ow = w / 2 + 2, oplane = ow * (h / 2 + 2);

	for (unsigned int i = 0; i < c; i++) {
		for (unsigned int y = 0; y < h / 2; y++) {
			const uint32_t *a = (const uint32_t *)(in + (i * h + 2 * y) * w);

			nb_maxpool2_row(a, a + w / 4, n->row, w);
			bd3_pairs_row(out + i * oplane + (y + 1) * ow, (const uint8_t *)n->row,
				      w / 2);
		}
	}
}

static void bd3_pause(bd3_pause_fn *pause, void *ctx, enum bd3_stage st, bool end)
{
	if (pause)
		pause(ctx, st, end);
}

/* Ternary conv3x3, output h x w, from pair words of padded planes (row stride w + 2), block after
   block; a call's first output pixel (x, y) at out + y * pitch + x * step. Pause after each output
   row of a block. */
static void NB_HOT bd3_tern(const struct bd3_tern *t, const uint32_t *in, unsigned int w,
			    unsigned int h, uint8_t *out, unsigned int pitch, unsigned int step,
			    bd3_pause_fn *pause, void *ctx, enum bd3_stage st)
{
	for (unsigned int b = 0; b < t->nblk; b++) {
		bd3_gen_fn *fn = t->blk[b];

		for (unsigned int y = 0; y < h; y++) {
			for (unsigned int x = 0; x < w; x += 4)
				fn(in + y * (w + 2) + x, out + y * pitch + x * step);
			bd3_pause(pause, ctx, st, b + 1 == t->nblk && y + 1 == h);
		}
	}
}

void bd3_net_run(struct bd3_net *n, bd3_pause_fn *pause, void *ctx)
{
	uint32_t *src = n->pa;

	for (unsigned int y = 0; y < BD3_L0_H; y++) {
		const uint8_t *in = n->in + 2 * y * BD3_PIN_PITCH;
		uint8_t *out = n->l0 + y * BD3_L0_W;

		for (unsigned int x = 0; x < BD3_L0_W; x += BD3_GEN_PX)
			bd3_stem(in + 4 * x, out + x);
		bd3_pause(pause, ctx, BD3_ST_L0, y + 1 == BD3_L0_H);
	}
	bd3_pool(n, n->l0, BD3_L0_C, BD3_L0_W, BD3_L0_H, n->p1);
	bd3_pause(pause, ctx, BD3_ST_L1, true);
	bd3_tern(&bd3_l2, n->p1, BD3_L2_W, BD3_L2_H, n->l2, BD3_L2_W, 1, pause, ctx, BD3_ST_L2);
	bd3_pool(n, n->l2, BD3_L2_C, BD3_L2_W, BD3_L2_H, n->pa);
	bd3_pause(pause, ctx, BD3_ST_L3, true);
	for (unsigned int l = 0; l + 1 < BD3_TRUNK; l++) {
		uint32_t *next = src == n->pa ? n->pb : n->pa;

		bd3_tern(&bd3_trunk[l], src, BD3_GRID_W, BD3_GRID_H, (uint8_t *)(next + BD3_P3_W),
			 4 * BD3_P3_W, 4, pause, ctx, BD3_ST_L4 + l);
		src = next;
	}
	bd3_tern(&bd3_trunk[BD3_TRUNK - 1], src, BD3_GRID_W, BD3_GRID_H, n->l8, BD3_GRID_W, 1, pause,
		 ctx, BD3_ST_L4 + BD3_TRUNK - 1);
	for (unsigned int y = 0; y < BD3_GRID_H; y++)
		for (unsigned int x = 0; x < BD3_GRID_W; x += BD3_GEN_PX)
			bd3_head(n->l8 + y * BD3_GRID_W + x, n->logit + y * BD3_GRID_W + x);
	bd3_pause(pause, ctx, BD3_ST_L9, true);
}

uint32_t bd3_logit_sum(const int32_t *logit)
{
	uint32_t sum = 0;

	for (unsigned int i = 0; i < BD3_CELLS; i++)
		sum = ((sum << 1) | (sum >> 31)) + (uint32_t)logit[i];
	return sum;
}
