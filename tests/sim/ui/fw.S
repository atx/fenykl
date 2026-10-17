/*
 * The GUI data store mock the settings page test (test_ui.c) needs on top of tests/sim/log/fw.S
 * and cfg/fw.S (linked with them), placed at the real firmware address with the jmp trick. It
 * forwards to c_gui_data_set: A0, A1, D0, D1 go to mreg[].
 */
	.section .fw.gui_data_set,"ax"
	jmp	_mock_gui_data_set
	.section .mock,"ax"
_mock_gui_data_set:
	mov	a0,(_mreg)
	mov	a1,(_mreg+4)
	mov	d0,(_mreg+8)
	mov	d1,(_mreg+12)
	add	-12,sp
	call	_c_gui_data_set,[],0
	add	12,sp
	mov	d0,a0
	ret	[d2,a2],0x10
