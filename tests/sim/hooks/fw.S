/* Mock firmware for the hook simulator test (appevent_post: appevent.S). */

/* Synthetic target with stack arguments (the PoC had none). stacksum(a@D0, b@D1, c@(4,SP), d@(8,SP))
   = a + b + c + d; CALL [],0x10. Inside the callee the caller's (4,SP)/(8,SP) are (20,SP)/(24,SP).
   Placed at a free address; its first six bytes are the relocate span. */
	.section .fw.stacksum,"ax"
	.global	_fw_mock_stacksum
_fw_mock_stacksum:
	mov	d0,(8,sp)			/* 42 08  -- relocate span */
	mov	d1,(12,sp)			/* 46 0c */
	mov	a0,d0				/* f1 d0  (a0 unused; keeps the span 6 relocatable bytes) */
	mov	(8,sp),d0
	mov	(12,sp),d1
	add	d1,d0
	mov	(20,sp),d1
	add	d1,d0
	mov	(24,sp),d1
	add	d1,d0
	ret	[],0x10

	.section .mock,"ax"
/* int fw_stackcaller(a@D0, b@D1, c@(4,SP), d@(8,SP)): enters stacksum with sentinels in D2/D3/A2/A3
   and checks them and SP; caller: [d2,d3,a2,a3],0x18 (SP -= 24 on entry). After its own `add -16,sp`
   the incoming stack args c/d sit at (44,SP)/(48,SP). Returns 0xbad if a register or SP changed. */
	.global	_fw_stackcaller
_fw_stackcaller:
	add	-16,sp
	mov	sp,a0
	add	16,a0
	mov	a0,(12,sp)			/* entry SP, for the check */
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	mov	(44,sp),a0			/* c */
	mov	a0,(4,sp)
	mov	(48,sp),a0			/* d */
	mov	a0,(8,sp)
	call	fw_addr_stacksum,[],0x10	/* a@D0, b@D1 still live */
	mov	(12,sp),a1
	mov	sp,a0
	add	16,a0
	cmp	a1,a0
	bne	.Lbads
	cmp	0x5a5a0002,d2
	bne	.Lbads
	cmp	0x5a5a0003,d3
	bne	.Lbads
	cmp	0x5a5a000a,a2
	bne	.Lbads
	cmp	0x5a5a000b,a3
	bne	.Lbads
	add	16,sp
	ret	[d2,d3,a2,a3],0x18
.Lbads:	mov	0xbad,d0
	add	16,sp
	ret	[d2,d3,a2,a3],0x18

/* int call_fw_stackcaller(int a, int b, int c, int d): a@D0, b@D1 pass through; c (GCC (12,SP)) and
   d (GCC (16,SP)) go to fw_stackcaller's (4,SP)/(8,SP). Thunk frame 12, so GCC c/d are at (24,SP)/(28,SP). */
	.global	_call_fw_stackcaller
_call_fw_stackcaller:
	add	-12,sp
	mov	(24,sp),a0			/* c */
	mov	a0,(4,sp)
	mov	(28,sp),a0			/* d */
	mov	a0,(8,sp)
	call	_fw_stackcaller,[d2,d3,a2,a3],0x18
	ret	[],12


/* gpi_flush at its real address with its real first instruction (the relocate span reads the
   absolute flag 0x6034D5B5, mapped by the test): if the flag is 1, toggle the draw index 0x6034D5B4
   and clear the flag. Void, no arguments, CALL [],4. */
	.section .fw.gpi_flush,"ax"
	.global	_fw_mock_gpi_flush
_fw_mock_gpi_flush:
	movbu	(0x6034d5b5),d0			/* fc a8 b5 d5 34 60 -- relocate span */
	cmp	1,d0
	bne	1f
	movbu	(0x6034d5b4),d0
	xor	1,d0
	movbu	d0,(0x6034d5b4)
	clr	d0
	movbu	d0,(0x6034d5b5)
1:	ret	[],4

	.section .mock,"ax"
/* int fw_flush_caller(void): calls gpi_flush as gui_gpi_flush does, with sentinels in D2/D3/A2/A3;
   returns 0, or 0xbad if any of them or SP changed. Caller uses [d2,d3,a2,a3],0x18. */
	.global	_fw_flush_caller
_fw_flush_caller:
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	mov	sp,a1
	mov	a1,(4,sp)
	call	fw_addr_gpi_flush,[],4
	cmp	0x5a5a0002,d2
	bne	.Lbadf
	cmp	0x5a5a0003,d3
	bne	.Lbadf
	cmp	0x5a5a000a,a2
	bne	.Lbadf
	cmp	0x5a5a000b,a3
	bne	.Lbadf
	mov	(4,sp),a1
	mov	sp,a0
	cmp	a1,a0
	bne	.Lbadf
	clr	d0
	ret	[d2,d3,a2,a3],0x18
.Lbadf:	mov	0xbad,d0
	ret	[d2,d3,a2,a3],0x18

	.global	_call_fw_flush_caller
_call_fw_flush_caller:
	add	-4,sp
	call	_fw_flush_caller,[d2,d3,a2,a3],0x18
	ret	[],4


/* mov_yc_resize_pass at its real address with its real first six bytes (an absolute load into A2,
   one of the registers its CALL saves): unsigned (in@A0, out@A1) = *in + 2 * *out + (0x6034CACC);
   CALL [d2,d3,a2,a3],0x14, the first hook target with a non-empty register list. */
	.section .fw.mov_yc_resize_pass,"ax"
	.global	_fw_mock_mov_yc_resize_pass
_fw_mock_mov_yc_resize_pass:
	mov	(0x6034cacc),a2			/* fc a2 cc ca 34 60 -- relocate span */
	mov	(a0),d0
	mov	(a1),d1
	add	d1,d0
	add	d1,d0
	mov	a2,d1
	add	d1,d0
	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a03,a3
	ret	[d2,d3,a2,a3],0x14

	.section .mock,"ax"
/* unsigned fw_resize_caller(in@D0, out@D1): enters mov_yc_resize_pass as mov_clip_disp_pass does
   (A0/A1, CALL [d2,d3,a2,a3],0x14) with sentinels in D2/D3/A2/A3; returns 0xbad if any of them or
   SP changed; caller: [d2,d3,a2,a3],0x18. */
	.global	_fw_resize_caller
_fw_resize_caller:
	mov	sp,a2
	mov	a2,(4,sp)
	mov	d0,a0
	mov	d1,a1
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	call	fw_addr_mov_yc_resize_pass,[d2,d3,a2,a3],0x14
	cmp	0x5a5a0002,d2
	bne	.Lbad_resize
	cmp	0x5a5a0003,d3
	bne	.Lbad_resize
	cmp	0x5a5a000a,a2
	bne	.Lbad_resize
	cmp	0x5a5a000b,a3
	bne	.Lbad_resize
	mov	(4,sp),a1
	mov	sp,a0
	cmp	a1,a0
	bne	.Lbad_resize
	ret	[d2,d3,a2,a3],0x18
.Lbad_resize:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],0x18

/* GCC -> Panasonic thunk: unsigned call_fw_resize_caller(const unsigned *in, const unsigned *out);
   GCC passes both in D0/D1, which fw_resize_caller moves to A0/A1. */
	.global	_call_fw_resize_caller
_call_fw_resize_caller:
	add	-4,sp
	call	_fw_resize_caller,[d2,d3,a2,a3],0x18
	ret	[],4
