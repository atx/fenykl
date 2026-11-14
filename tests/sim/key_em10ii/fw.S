/* Mock firmware for the E-M10 II key layer test (test_key.c). appevent_post sits at its real
   address with its real relocate span and appends {event, param, flag} to sim_posts[sim_nposts++]
   (no bounds check: the test posts fewer than its array holds). The primitives the event layer,
   the key layer and hooks.c call are jmp mocks with their stock RET operands. */

/* appevent_post(event@D0, param@D1, flag@A0), entered with [d2,d3,a2],16. */
	.section .fw.appevent_post,"ax"
	mov	d0,d3				/* 83 -- relocate span, 9 bytes */
	mov	d1,a2				/* f1 e6 */
	movbu	(0xa1c184da),d1			/* fc a9 da 84 c1 a1 */
	mov	(_sim_nposts),d0
	mov	_sim_posts,a1
	asl2	d0
	add	d0,a1
	add	d0,a1
	add	d0,a1				/* a1 = &sim_posts[n] (12 bytes each) */
	mov	d3,(a1)
	mov	a2,(4,a1)
	mov	a0,(8,a1)
	mov	(_sim_nposts),d0
	inc	d0
	mov	d0,(_sim_nposts)
	mov	1,d0
	ret	[d2,d3,a2],16

	.macro	JMOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm

	JMOCK	psw_get
	mov	0x0f00,d0
	ret	[],0

	JMOCK	irq_disable_all
	ret	[],0

	JMOCK	irq_mask_restore
	ret	[],0

	JMOCK	irq_lock
	mov	0x80000700,d0
	ret	[d2],8

	JMOCK	irq_unlock
	ret	[d2],8

	JMOCK	dcache_flush_all
	ret	[d2],8
