/*
 * Hand-written kernels (called from fam_conv.c and fam_dot.c). Each takes a pointer to a job of
 * 32-bit fields in d0 (layouts in the comments; the C structs match) and keeps d2, d3, a2, a3.
 */

/* One offset at byte k of the list a1: q = row + offset, d2 op= q[0], d3 op= q[2]. */
	.macro	TSTEP op, k
	movhu	(\k,a1),d0
	mov	(d0,a0),d1
	\op	d1,d2
	mov	(d0,a2),d1
	\op	d1,d3
	.endm

/* The list a1 .. a1 + d0 bytes (d0 = 2 * count): an odd one first, then two per pass. */
	.macro	TLIST op
	mov	a1,a3
	add	d0,a3
	btst	2,d0
	beq	1f
	TSTEP	\op,0
	add	2,a1
1:	cmp	a3,a1
	beq	3f
	setlb
	TSTEP	\op,0
	TSTEP	\op,2
	add	4,a1
	cmp	a3,a1
	lne
3:
	.endm

/*
 * Ternary conv rows on pair words (fam_conv.c pair_tab_asm), birddetect's bd_tern on run-time
 * offset lists: per group of 4 output pixels, a = init + sum q[0] - ..., b the same on q[2], q the
 * word at row + offset. Job: +0 row (advanced by 16 per group), +4 offsets (u16: npos + then nneg
 * -), +8 npos, +12 nneg, +16 init, +20 groups (>= 1, counted down), +24 out (2 words per group).
 */
	.section .text.nb_tern_rows,"ax"
	.global _nb_tern_rows
_nb_tern_rows:
	movm	[d2,d3,a2,a3],(sp)
	add	-4,sp
	mov	d0,(0,sp)
.Ltr_group:
	mov	(0,sp),a1
	mov	(0,a1),a0
	mov	(16,a1),d2
	mov	d2,d3
	mov	(8,a1),d0
	mov	(4,a1),a1
	mov	a0,a2
	add	8,a2
	add	d0,d0
	TLIST	add
	mov	(0,sp),a3
	mov	(12,a3),d0
	add	d0,d0
	TLIST	sub
	mov	(0,sp),a1
	mov	(24,a1),a0
	mov	d2,(0,a0)
	mov	d3,(4,a0)
	add	8,a0
	mov	a0,(24,a1)
	mov	(0,a1),a0
	add	16,a0
	mov	a0,(0,a1)
	mov	(20,a1),d0
	add	-1,d0
	mov	d0,(20,a1)
	bne	.Ltr_group
	add	4,sp
	ret	[d2,d3,a2,a3],16

/* Bit counts of one word in d0 into 4-bit fields (<= 4 each); d1 is clobbered. */
	.macro	NIB4
	mov	d0,d1
	lsr	1,d1
	and	0x55555555,d1
	sub	d1,d0
	mov	d0,d1
	lsr	2,d1
	and	0x33333333,d0
	and	0x33333333,d1
	add	d1,d0
	.endm

/*
 * Binary dot products (fam_dot.c bdot swar_asm): popcount(a ^ w) over nwords words, returned in
 * d0. Two words per step: their 4-bit counts summed (<= 8), then into byte lanes (<= 16 per step);
 * the byte lanes are summed every 15 steps. Job: +0 a, +4 w, +8 nwords (even, >= 2).
 */
	.section .text.nb_bdot_swar,"ax"
	.global _nb_bdot_swar
_nb_bdot_swar:
	movm	[d2,d3,a2,a3],(sp)
	add	-4,sp
	mov	d0,a2
	mov	(0,a2),a0
	mov	(4,a2),a1
	mov	(8,a2),d0
	asl2	d0
	mov	a0,a3
	add	d0,a3
	clr	d0
	mov	d0,(0,sp)
.Lbs_chunk:
	mov	a0,a2
	add	120,a2
	cmp	a3,a2
	bls	1f
	mov	a3,a2
1:	clr	d3
	setlb
	mov	(0,a0),d0
	mov	(0,a1),d1
	xor	d1,d0
	NIB4
	mov	d0,d2
	mov	(4,a0),d0
	mov	(4,a1),d1
	xor	d1,d0
	NIB4
	add	d0,d2
	mov	d2,d0
	lsr	4,d0
	and	0x0F0F0F0F,d2
	and	0x0F0F0F0F,d0
	add	d0,d3
	add	d2,d3
	add	8,a0
	add	8,a1
	cmp	a2,a0
	lne
	mov	d3,d0
	lsr	8,d0
	and	0x00FF00FF,d3
	and	0x00FF00FF,d0
	add	d0,d3
	mov	d3,d0
	lsr	16,d0
	add	d0,d3
	exthu	d3
	mov	(0,sp),d0
	add	d3,d0
	mov	d0,(0,sp)
	cmp	a3,a0
	bne	.Lbs_chunk
	mov	(0,sp),d0
	add	4,sp
	ret	[d2,d3,a2,a3],16

/* d3 += lut[byte of d1] for the byte in d1 (d1 is clobbered). */
	.macro	LUTB
	extbu	d1
	movbu	(d1,a2),d1
	add	d1,d3
	.endm

/*
 * Binary dot products with a 256-byte popcount table (fam_dot.c bdot lut8_asm): popcount(a ^ w),
 * returned in d0. Job: +0 a, +4 w, +8 nwords (>= 1), +12 lut.
 */
	.section .text.nb_bdot_lut8,"ax"
	.global _nb_bdot_lut8
_nb_bdot_lut8:
	movm	[d2,d3,a2,a3],(sp)
	mov	d0,a2
	mov	(0,a2),a0
	mov	(4,a2),a1
	mov	(8,a2),d0
	mov	(12,a2),a2
	asl2	d0
	mov	a0,a3
	add	d0,a3
	clr	d3
	setlb
	mov	(0,a0),d0
	mov	(0,a1),d1
	xor	d1,d0
	mov	d0,d1
	LUTB
	mov	d0,d1
	lsr	8,d1
	LUTB
	mov	d0,d1
	lsr	16,d1
	LUTB
	lsr	24,d0
	movbu	(d0,a2),d0
	add	d0,d3
	add	4,a0
	add	4,a1
	cmp	a3,a0
	lne
	mov	d3,d0
	ret	[d2,d3,a2,a3],16

/* Two int4 MACs of q byte k (low nibble first) with activations 2k, 2k + 1, sign by shifts. */
	.macro	I4S k
	movbu	(\k,a0),d0
	mov	d0,d1
	asl	28,d1
	asr	28,d1
	movbu	(2*\k,a1),d2
	udf00	d1,d2
	add	d2,d3
	asl	24,d0
	asr	28,d0
	movbu	(2*\k+1,a1),d2
	udf00	d0,d2
	add	d2,d3
	.endm

/*
 * Packed int4 dot product (fam_dot.c dot i4_asm, the layout of i4_cu): sum of a[i] * w[i], the
 * weights two's complement nibbles, low first; returned in d0. Job: +0 a, +4 q, +8 q bytes (even,
 * >= 2).
 */
	.section .text.nb_dot_i4s,"ax"
	.global _nb_dot_i4s
_nb_dot_i4s:
	movm	[d2,d3,a2,a3],(sp)
	mov	d0,a2
	mov	(0,a2),a1
	mov	(4,a2),a0
	mov	(8,a2),d0
	mov	a0,a3
	add	d0,a3
	clr	d3
	setlb
	I4S	0
	I4S	1
	add	2,a0
	add	4,a1
	cmp	a3,a0
	lne
	mov	d3,d0
	ret	[d2,d3,a2,a3],16

/* One tap of the spread tables: d2 += a[ai] * lut[index in d0] (16-bit lanes for 2 outputs). */
	.macro	SPREAD ai
	mov	(d0,a2),d0
	movbu	(\ai,a1),d1
	udf00	d0,d1
	add	d1,d2
	.endm

/* The lanes of d2 into the totals d3 (output 0) and (4,sp) (output 1); d2 = 0. */
	.macro	FLUSH2
	mov	d2,d0
	exth	d0
	add	d0,d3
	sub	d0,d2
	asr	16,d2
	mov	(4,sp),d0
	add	d2,d0
	mov	d0,(4,sp)
	clr	d2
	.endm

/* The totals into the job (+16 output 0, +20 output 1) and return. */
	.macro	DONE2
	mov	(0,sp),a0
	mov	d3,(16,a0)
	mov	(4,sp),d0
	mov	d0,(20,a0)
	add	8,sp
	ret	[d2,d3,a2,a3],16
	.endm

/* Common entry of the spread kernels: a1 = a, a0 = q, a3 = q end, a2 = lut, totals 0. */
	.macro	SPREAD_ENTRY
	movm	[d2,d3,a2,a3],(sp)
	add	-8,sp
	mov	d0,(0,sp)
	mov	d0,a2
	mov	(0,a2),a1
	mov	(4,a2),a0
	mov	(8,a2),d0
	mov	a0,a3
	add	d0,a3
	mov	(12,a2),a2
	clr	d2
	clr	d3
	mov	d3,(4,sp)
	.endm

/*
 * int4 weights of two outputs interleaved (fam_dot.c dot i4_lut): q byte i = w0[i] (low nibble) |
 * w1[i] << 4; lut[q] = w0 + w1 * 2^16, so one udf00 gives both products. 16 taps per flush keep
 * each lane below 2^15 (255 * 8 * 16). Job: +0 a, +4 q, +8 taps (multiple of 16), +12 lut (256
 * words), +16 / +20 the two sums (out).
 */
	.section .text.nb_dot_i4_lut,"ax"
	.global _nb_dot_i4_lut
_nb_dot_i4_lut:
	SPREAD_ENTRY
	setlb
	.irp	k,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
	movbu	(\k,a0),d0
	asl2	d0
	SPREAD	\k
	.endr
	FLUSH2
	add	16,a0
	add	16,a1
	cmp	a3,a0
	lne
	DONE2

/*
 * int2 weights {-2..1} of two outputs interleaved (fam_dot.c dot i2_lut): nibble j of q byte i is
 * tap 2i + j, w0 (bits 0-1) | w1 << 2; lut[nibble] = w0 + w1 * 2^16. 32 taps per flush (255 * 2 *
 * 32 < 2^15). Job: +0 a, +4 q, +8 q bytes (multiple of 16), +12 lut (16 words), +16 / +20 sums.
 */
	.section .text.nb_dot_i2_lut,"ax"
	.global _nb_dot_i2_lut
_nb_dot_i2_lut:
	SPREAD_ENTRY
	setlb
	.irp	k,0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
	movbu	(\k,a0),d0
	and	15,d0
	asl2	d0
	SPREAD	2*\k
	movbu	(\k,a0),d0
	lsr	2,d0
	and	0x3C,d0
	SPREAD	2*\k+1
	.endr
	FLUSH2
	add	16,a0
	add	32,a1
	cmp	a3,a0
	lne
	DONE2
