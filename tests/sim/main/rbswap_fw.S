/* The E-M10 II interrupt masking main_rbswap_base.c uses (fenykl_irq_save/restore), as mocks at the
   real addresses with the jmp trick: sim_masked counts the masks, sim_psw records the restore. */
	.macro	MOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm

/* unsigned psw_get(); retf [],0 */
	MOCK	psw_get
	mov	0x00000700,d0
	ret	[],0

/* void irq_disable_all(); retf [],0 */
	MOCK	irq_disable_all
	mov	(_sim_masked),d0
	add	1,d0
	mov	d0,(_sim_masked)
	ret	[],0

/* void irq_mask_restore(psw@D0); retf [],0 */
	MOCK	irq_mask_restore
	mov	d0,(_sim_psw)
	ret	[],0
