/*
 * Mocks for the logging library simulator test (test_log.c), placed at the real firmware
 * addresses with the jmp trick. The PSW primitives are plain assembly; the others forward to C
 * functions c_<name> (test_log.c): they store A0, A1, D0, D1 and the entry SP in mreg[] and call
 * the C function with the GCC convention; its D0 is returned in D0 and A0.
 */
	.macro	MOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm

	.macro	FORWARD name, regs, frame
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

	.equ	IE, 0x800

/* unsigned psw_get(); retf [],0 */
	MOCK	psw_get
	mov	(_mock_psw),d0
	ret	[],0

/* void irq_disable_all(); retf [],0: clears IE, counts */
	MOCK	irq_disable_all
	mov	(_mock_psw),d1
	and	~IE,d1
	mov	d1,(_mock_psw)
	mov	(_mock_masks),d1
	add	1,d1
	mov	d1,(_mock_masks)
	ret	[],0

/* void irq_mask_restore(psw@D0); retf [],0 */
	MOCK	irq_mask_restore
	mov	d0,(_mock_psw)
	ret	[],0

/* unsigned ui_key_get_state(code@D0); ret [d2,d3,a2],24: the UsbC key state, 0 = cable in */
	MOCK	ui_key_get_state
	mov	(_mock_usb_key),d0
	ret	[d2,d3,a2],24

	FORWARD	sprintf, [], 12
	FORWARD	rpc_fopen, [], 16
	FORWARD	rpc_fclose, [], 12
	FORWARD	rpc_fread, [], 24
	FORWARD	rpc_fwrite, [], 24
	FORWARD	rpc_fremove, [], 12
	FORWARD	malloc, [], 4
	FORWARD	free, [], 4
	FORWARD	OlyTaskSleep, [d2], 8

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
