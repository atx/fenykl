/* appevent_post mock for the hook and event simulator tests. It sits at its real address with its
   real first six bytes (the relocate span), so the generated stub/trampoline operate on real
   instructions. fw_caller enters it with the firmware CALL and checks the callee-saved registers
   and SP. */
	.section .fw.appevent_post,"ax"
	.global	_fw_mock_appevent_post
_fw_mock_appevent_post:				/* int (event@D0, param@D1, flag@A0) = 3*event + param + flag */
	mov	d0,(8,sp)			/* 42 08  -- relocate span, 6 bytes */
	mov	d1,(12,sp)			/* 46 0c */
	mov	a0,d0				/* f1 d0 */
	mov	(8,sp),d1
	add	d1,d0
	add	d1,d0
	add	d1,d0
	mov	(12,sp),d1
	add	d1,d0
	mov	0x77777777,d1
	mov	d1,mdr				/* a nested CALL would clobber MDR: forces ret, not retf */
	ret	[],32

	.section .mock,"ax"
/* int fw_caller(event@D0, param@D1, flag@A0): calls appevent_post with sentinels in D2/D3/A2/A3
   and a saved SP, returns 0xbad if any of them or SP changed; caller uses [d2,d3,a2,a3],0x18. */
	.global	_fw_caller
_fw_caller:
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	mov	sp,a1
	mov	a1,(4,sp)
	call	fw_addr_appevent_post,[],32
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

/* GCC -> Panasonic thunk: int call_fw_caller(int event, int param, int flag); flag (GCC arg 3 at
   (12,sp)) goes to A0, event/param stay in D0/D1. */
	.global	_call_fw_caller
_call_fw_caller:
	add	-4,sp
	mov	(16,sp),a0
	call	_fw_caller,[d2,d3,a2,a3],0x18
	ret	[],4
