/* Firmware helpers the hook runtime (sdk/arch/epl3/hooks.c) calls, as mocks placed at their real
   addresses with the jmp trick. Minimal: they only have to return and let the runtime proceed.
   mock_args[] (from test_hooks.c) records what the runtime passed, so a check could inspect it. */
	.macro	MOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm
	.equ	ARGS, _mock_args

/* void dcache_purge_all_irqsafe(); ret [d2],8 */
	MOCK	dcache_purge_all_irqsafe
	mov	0xdead0d02,d2
	ret	[d2],8

/* unsigned psw_get(); retf [],0 */
	MOCK	psw_get
	mov	0x00000700,d0
	ret	[],0

/* void irq_disable_all(); retf [],0: counts calls in mock_args[0] */
	MOCK	irq_disable_all
	mov	(ARGS),d0
	add	1,d0
	mov	d0,(ARGS)
	ret	[],0

/* void irq_mask_restore(psw@D0); retf [],0: records it in mock_args[1] */
	MOCK	irq_mask_restore
	mov	d0,(ARGS+4)
	ret	[],0

/* void cache_invalidate(d@D0); ret [d2],8: records d in mock_args[2] */
	MOCK	cache_invalidate
	mov	d0,(ARGS+8)
	mov	0xdead0d02,d2
	ret	[d2],8

/* unsigned cache_enable(d@D0); retf [d2],4: records d in mock_args[3] */
	MOCK	cache_enable
	mov	d0,(ARGS+12)
	mov	0xdead0d02,d2
	ret	[d2],4

/* unsigned cache_busy(d@D0); retf [],0: always returns 0 (never busy) */
	MOCK	cache_busy
	mov	0,d0
	ret	[],0
