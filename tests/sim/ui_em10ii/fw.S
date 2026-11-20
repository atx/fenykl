/*
 * Mock firmware for the E-M10 II settings tab test (test_ui.c), placed at the real firmware
 * addresses with the jmp trick. Each mock stores A0, A1, D0, D1 in mreg[] and forwards to
 * c_<name>; its RET matches the stock CALL operands (firmware.toml). The option picker's stock
 * handler sits at its real address (menu_picker_stock_fn), entered with calls like the stock one.
 */
	.macro	CMOCK name, ret
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	mov	a0,(_mreg)
	mov	a1,(_mreg+4)
	mov	d0,(_mreg+8)
	mov	d1,(_mreg+12)
	add	-12,sp
	call	_c_\name,[],0
	add	12,sp
	mov	d0,a0
	\ret
	.endm

	CMOCK	psw_get, "ret [],0"
	CMOCK	irq_disable_all, "ret [],0"
	CMOCK	irq_mask_restore, "ret [],0"
	CMOCK	irq_lock, "ret [d2],8"
	CMOCK	irq_unlock, "ret [d2],8"
	CMOCK	dcache_flush_all, "ret [d2],8"
	CMOCK	OlyTaskSleep, "ret [d2],8"
	CMOCK	menu_picker_create, "ret [d2,d3,a2],0x10"
	CMOCK	ui_key_get_state, "ret [d2,d3,a2],0x18"
	CMOCK	fileif_get_media_status, "ret [d2,a2],0xc"

	.section .fw.picker_stock,"ax"
	add	-4,sp
	mov	a0,(_mreg)
	mov	d0,(_mreg+8)
	mov	d1,(_mreg+12)
	add	-12,sp
	call	_c_picker_stock,[],0
	add	12,sp
	ret	[],4
