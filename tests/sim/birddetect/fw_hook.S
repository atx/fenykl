/* fd_hw_shutdown at its real address with its real first six bytes, for the birddetect hook
   simulator test (test_hook.c). It counts calls in shutdown_calls, clobbers the scratch registers
   and returns with ret [],8. */
	.section .fw.fd_hw_shutdown,"ax"
	.global	_fw_mock_fd_hw_shutdown
_fw_mock_fd_hw_shutdown:
	.byte	0xfc,0xa8,0x20,0x8e,0x60,0x60	/* movbu (0x60608E20),d0 -- relocate span */
	mov	(_shutdown_calls),d1
	inc	d1
	mov	d1,(_shutdown_calls)
	mov	0xdead0001,d1
	mov	0xdead0002,a1
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	ret	[],8

	.section .mock,"ax"
/* unsigned fw_shutdown_caller(): calls fd_hw_shutdown as 0x6E8CDF3B does (CALL [],8) with
   sentinels in D2/D3/A2/A3; returns 0xbad if any of them or SP changed, else 0;
   caller: [d2,d3,a2,a3],0x18. */
	.global	_fw_shutdown_caller
_fw_shutdown_caller:
	mov	sp,a2
	mov	a2,(4,sp)
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	call	fw_addr_fd_hw_shutdown,[],8
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
	clr	d0
	ret	[d2,d3,a2,a3],0x18
.Lbad:	mov	0xbad,d0
	ret	[d2,d3,a2,a3],0x18

/* GCC -> Panasonic thunk: unsigned call_fw_shutdown_caller(void). */
	.global	_call_fw_shutdown_caller
_call_fw_shutdown_caller:
	add	-4,sp
	call	_fw_shutdown_caller,[d2,d3,a2,a3],0x18
	ret	[],4
