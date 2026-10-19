/*
 * nnbench's kernels: the asm ternary row kernel of nb_kern.S, the SWAR max pool (birddetect3 links
 * it directly) and the lane helpers. Pure code, no firmware calls.
 */
#ifndef NB_KERN_H
#define NB_KERN_H

#include <stdint.h>

#define NB_HOT __attribute__((optimize("O2", "no-tree-loop-distribute-patterns")))

static inline uint8_t nb_u8(int32_t v)
{
	if (v < 0)
		return 0;
	if (v > 255)
		return 255;
	return (uint8_t)v;
}

/* A 16-bit pair-word lane holding 0x8000 + acc, rescaled to u8. */
static inline uint8_t nb_lane(uint32_t lane, unsigned int s)
{
	return nb_u8(((int32_t)lane - 0x8000) >> s);
}

/*
 * nb_kern.S: ternary conv, one output row of one channel on pair words (pair_tab_asm). Per group of
 * 4 output pixels: out[0] = init + sum q[0] (npos offsets) - sum q[0] (nneg offsets), out[1] the
 * same on q[2], q the word at row + offset; row advances 16 bytes per group.
 */
struct nb_tern_job {
	const void *row;
	const uint16_t *off;	/* npos + offsets, then nneg - offsets (bytes) */
	uint32_t npos, nneg, init, groups;
	uint32_t *out;		/* 2 words per group */
};
void nb_tern_rows(struct nb_tern_job *j);

/* Per byte lane max(a, b): the borrow of a - b per lane (Hacker's Delight 2-18) as a mask. */
static inline uint32_t nb_max_u8x4(uint32_t a, uint32_t b)
{
	const uint32_t h = 0x80808080u;
	uint32_t d = ((a | h) - (b & ~h)) ^ ((a ^ ~b) & h);
	uint32_t lt = ((~a & b) | (~(a ^ b) & d)) & h;

	return a ^ ((a ^ b) & ((lt << 1) - (lt >> 7)));
}

/* 2x2 max pool of the rows a, b (w bytes, w a multiple of 8) into w / 2 bytes at d (cw): SWAR
   byte maxes of the two rows, then of the even and odd columns. All word aligned. */
static inline void nb_maxpool2_row(const uint32_t *a, const uint32_t *b, uint32_t *d,
				   unsigned int w)
{
	for (unsigned int x = 0; x < w / 8; x++, a += 2, b += 2) {
		uint32_t v1 = nb_max_u8x4(a[0], b[0]), v2 = nb_max_u8x4(a[1], b[1]);
		uint32_t e = (v1 & 0x00FF00FFu) | (v2 & 0x00FF00FFu) << 8;
		uint32_t o = (v1 >> 8 & 0x00FF00FFu) | (v2 & 0xFF00FF00u);
		uint32_t m = nb_max_u8x4(e, o);	/* bytes: out 0, 2, 1, 3 */

		d[x] = (m & 0xFF0000FFu) | (m >> 8 & 0xFF00u) | (m << 8 & 0xFF0000u);
	}
}

#endif
