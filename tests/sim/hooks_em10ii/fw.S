/* Mock firmware for the E-M10 II hook test. The two hook targets sit at their real addresses with
   their real relocate spans; the primitives hooks.c calls are jmp mocks that count into mock[]
   (test_hooks.c). The callers enter the targets with their stock CALL operands and check D2, D3,
   A2, A3 and SP. */
	.equ	MOCK, _mock

/* appevent_post: (event@D0, param@D1, flag@A0) -> 3*event + param + flag + (0xA1C184DA). */
	.section .fw.appevent_post,"ax"
	mov	d0,d3				/* 83 -- relocate span, 9 bytes */
	mov	d1,a2				/* f1 e6 */
	movbu	(0xa1c184da),d1			/* fc a9 da 84 c1 a1 */
	mov	d3,d0
	add	d3,d0
	add	d3,d0
	mov	a2,d2
	add	d2,d0
	mov	a0,d2
	add	d2,d0
	add	d1,d0
	mov	0x77777777,d1
	mov	d1,mdr				/* a nested CALL would clobber MDR: forces ret, not retf */
	ret	[d2,d3,a2],16

/* cw_updateDisplay(show_front@D0): records D0 and the A1 the relocated span loaded, counts calls. */
	.section .fw.cw_updateDisplay,"ax"
	mov	0x60bc53c4,a1			/* fc dd c4 53 bc 60 -- relocate span */
	mov	a1,(MOCK+0)
	mov	d0,(MOCK+4)
	mov	(MOCK+8),d0
	inc	d0
	mov	d0,(MOCK+8)
	clr	d0
	clr	d2
	mov	d2,a2
	ret	[d2,a2],0x54

	.macro	JMOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm

/* unsigned irq_lock(level@D0), ret [d2],8: counts in mock[3], records the level in mock[4]. */
	JMOCK	irq_lock
	mov	d0,(MOCK+16)
	mov	(MOCK+12),d0
	inc	d0
	mov	d0,(MOCK+12)
	mov	0x80000700,d0
	clr	d2
	ret	[d2],8

/* void irq_unlock(cookie@D0), ret [d2],8: counts in mock[5], records the cookie in mock[6]. */
	JMOCK	irq_unlock
	mov	d0,(MOCK+24)
	mov	(MOCK+20),d0
	inc	d0
	mov	d0,(MOCK+20)
	clr	d2
	ret	[d2],8

/* void dcache_flush_all(), ret [d2],8: counts in mock[7]. */
	JMOCK	dcache_flush_all
	mov	(MOCK+28),d0
	inc	d0
	mov	d0,(MOCK+28)
	clr	d2
	ret	[d2],8

	.macro	CALLER name, target, saves, frame
	.section .mock,"ax"
/* int name(D0, D1, A0): calls target with sentinels in D2/D3/A2/A3; 0xbad if any of them or SP
   changed. Entered with [d2,d3,a2,a3],0x18. */
_\name:
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	mov	0x5a5a000b,a3
	mov	sp,a1
	mov	a1,(4,sp)
	call	\target,\saves,\frame
	cmp	0x5a5a0002,d2
	bne	1f
	cmp	0x5a5a0003,d3
	bne	1f
	cmp	0x5a5a000a,a2
	bne	1f
	cmp	0x5a5a000b,a3
	bne	1f
	mov	(4,sp),a1
	mov	sp,a0
	cmp	a1,a0
	bne	1f
	ret	[d2,d3,a2,a3],0x18
1:	mov	0xbad,d0
	ret	[d2,d3,a2,a3],0x18

/* GCC -> Panasonic: int call_name(int d0, int d1, int a0); GCC arg 3 at (12,sp) goes to A0. */
	.global	_call_\name
_call_\name:
	add	-4,sp
	mov	(16,sp),a0
	call	_\name,[d2,d3,a2,a3],0x18
	ret	[],4
	.endm

	CALLER	ev_caller, fw_addr_appevent_post, "[d2,d3,a2]", 16
	CALLER	cw_caller, fw_addr_cw_updateDisplay, "[d2,a2]", 0x54
