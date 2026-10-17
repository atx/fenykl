/* img_exec at its real address with its real first six bytes, for the main R/B swap simulator
   test (test_main_rbswap.c). It records the colour matrix the pass sees (IMG_CTRL +0x60 ->
   IMG_PARAM +0x18, 9 x s16) in seen_mat. If store_new is set it then stores 0x40 into ashMat[1],
   as a firmware task updating the matrix during the pass would. Without IMG_PARAM it only
   returns. Returns 0x2d00; CALL [d2,d3,a2,a3],0x14. */
	.section .fw.img_exec,"ax"
	.global	_fw_mock_img_exec
_fw_mock_img_exec:
	.byte	0x93,0x9a,0x00,0x31,0x50,0x80	/* mov a0,a3; mov 0,a2; mov (0x8050),d1 -- relocate span */
	mov	(0x60,a3),a0
	cmp	0,a0
	beq	2f
	add	0x18,a0
	mov	_seen_mat,a1
	mov	9,d0
1:	movhu	(a0),d1
	movhu	d1,(a1)
	add	2,a0
	add	2,a1
	add	-1,d0
	bne	1b
	mov	(_store_new),d0
	cmp	0,d0
	beq	2f
	mov	(0x60,a3),a0
	mov	0x40,d1
	movhu	d1,(0x1a,a0)
2:	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	mov	0x2d00,d0
	ret	[d2,d3,a2,a3],0x14

	.section .mock,"ax"
/* unsigned fw_exec_caller(ctrl@D0): enters img_exec as its firmware callers do (A0, CALL
   [d2,d3,a2,a3],0x14) with sentinels in D2/D3/A2/A3; returns 0xbad if any of them or SP changed;
   caller: [d2,d3,a2,a3],0x18. */
	.global	_fw_exec_caller
_fw_exec_caller:
	mov	sp,a2
	mov	a2,(4,sp)
	mov	d0,a0
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	call	fw_addr_img_exec,[d2,d3,a2,a3],0x14
	cmp	0x5a5a0002,d2
	bne	.Lbad
	cmp	0x5a5a0003,d3
	bne	.Lbad
	cmp	0x5a5a000a,a2
	bne	.Lbad
	cmp	0x5a5a000b,a3
	bne	.Lbad
	mov	(4,sp),a1
	mov	sp,a0
	cmp	a1,a0
	bne	.Lbad
	ret	[d2,d3,a2,a3],0x18
.Lbad:	mov	0xbad,d0
	ret	[d2,d3,a2,a3],0x18

/* GCC -> Panasonic thunk: unsigned call_fw_exec_caller(void *ctrl); GCC passes it in D0. */
	.global	_call_fw_exec_caller
_call_fw_exec_caller:
	add	-4,sp
	call	_fw_exec_caller,[d2,d3,a2,a3],0x18
	ret	[],4
