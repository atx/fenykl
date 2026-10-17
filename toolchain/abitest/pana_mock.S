/* Mock "firmware" functions written the way the E-PL3 firmware's Panasonic compiler emits them:
   no prologue (the caller's CALL saves the registers and allocates the frame), integer args in
   D0/D1, pointer args in A0/A1, further args on the stack from (frame + 4, SP), the last named
   argument of a variadic function on the stack, ints returned in D0, pointers in A0. Every mock
   clobbers the registers its CALL saved, plus MDR. */
	.text

/* int pana_mix(const int *p@A0, int a@D0, int b@D1, const int *q@A1) = *p + 2a + 3b + 5*q
   (shape of rpc_fread); callers: call pana_mix,[d2,d3,a2,a3],24 */
	.global	_pana_mix
_pana_mix:
	mov	(a0),d2
	add	d0,d2
	add	d0,d2
	mov	3,d3
	mul	d3,d1
	add	d1,d2
	mov	(a1),d3
	mov	5,d1
	mul	d1,d3
	add	d3,d2
	mov	d2,d0
	mov	0x11111111,a2
	mov	0x22222222,a3
	ret	[d2,d3,a2,a3],24

/* int pana_stack(const char *name@A0, int mode@D0, int exinf@D1, void *entry@A1,
                  int prio@(4), int size@(8), void *base@(12))
   = *name ^ mode<<1 ^ exinf<<2 ^ entry<<3 ^ prio<<4 ^ size<<5 ^ base<<6
   (shape of OlyTaskCreate); callers: call pana_stack,[d2,d3,a2,a3],0x30 */
	.global	_pana_stack
_pana_stack:
	movbu	(a0),d2
	mov	d0,d3
	asl	1,d3
	xor	d3,d2
	mov	d1,d3
	asl	2,d3
	xor	d3,d2
	mov	a1,d3
	asl	3,d3
	xor	d3,d2
	mov	(0x34,sp),d3
	asl	4,d3
	xor	d3,d2
	mov	(0x38,sp),d3
	asl	5,d3
	xor	d3,d2
	mov	(0x3c,sp),d3
	asl	6,d3
	xor	d3,d2
	mov	d2,d0
	mov	d0,mdr
	mov	0x33333333,a2
	mov	0x44444444,a3
	ret	[d2,d3,a2,a3],0x30

/* int *pana_vsum(int *out@A0, int n@(4), ...@(8)): *out = sum of the n varargs, returns out + 1
   (shape of sprintf: the last named argument is on the stack); callers: call pana_vsum,[d2,a2],0x10 */
	.global	_pana_vsum
_pana_vsum:
	mov	(0x14,sp),d2
	mov	sp,a2
	add	0x18,a2
	clr	d0
.Lvsum_loop:
	cmp	0,d2
	beq	.Lvsum_done
	mov	(a2),d1
	add	d1,d0
	inc4	a2
	add	-1,d2
	bra	.Lvsum_loop
.Lvsum_done:
	mov	d0,(a0)
	add	4,a0
	ret	[d2,a2],0x10

/* int pana_callcb(int (*fn)(void *ctx@A0, int v@D0)@A0, void *ctx@A1, int v@D0)
   Calls fn through CALLS like the firmware calls callbacks and returns its result, or 0xbad if
   D2/D3/A2/A3 were not preserved across the call; callers: call pana_callcb,[d2,d3,a2,a3],0x14 */
	.global	_pana_callcb
_pana_callcb:
	mov	a0,d1
	mov	a1,a0
	mov	d1,a1
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	calls	(a1)
	cmp	0x5a5a0002,d2
	bne	.Lcallcb_bad
	cmp	0x5a5a0003,d3
	bne	.Lcallcb_bad
	cmp	0x5a5a000a,a2
	bne	.Lcallcb_bad
	cmp	0x5a5a000b,a3
	bne	.Lcallcb_bad
	ret	[d2,d3,a2,a3],0x14
.Lcallcb_bad:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],0x14
