/*
 * Mocks for the HUD library simulator test (test_hud.c), placed at the real firmware addresses
 * with the jmp trick. They forward to C functions c_<name>: A0, A1, D0, D1 and the entry SP go to
 * mreg[], the C function's D0 is returned in D0 and A0.
 */
	.macro	MOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm

	.macro	FORWARD name, frame, regs:vararg
	MOCK	\name
	mov	a0,(_mreg)
	mov	a1,(_mreg+4)
	mov	d0,(_mreg+8)
	mov	d1,(_mreg+12)
	mov	sp,a0
	mov	a0,(_mreg+16)
	add	-12,sp
	call	_c_\name,[],0
	add	12,sp
	mov	d0,a0
	ret	\regs,\frame
	.endm

	FORWARD	gpi_get_glyph, 0x14, [d2,d3,a2,a3]
	FORWARD	malloc, 4, []
	FORWARD	free, 4, []

/* void sim_write(buf@D0, len@D1): SYS_write to stdout (GDB simulator) */
	.section .mock,"ax"
	.global	_sim_write
_sim_write:
	add	-20,sp
	mov	d0,(12,sp)
	mov	d1,(16,sp)
	mov	1,d1
	mov	5,d0
	syscall
	add	20,sp
	ret	[],0
