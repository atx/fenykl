/*
 * E-M10 II mocks for the HUD library simulator programs, next to fw.S: the D-cache flush, the GUI
 * widget calls and the GUI event post at their firmware addresses (jmp trick, RETs as the stock CALL
 * operands), and the stock functions behind the three GUI slots (sections placed at their addresses
 * by test_hud_sim.py) and a widget's attach method (_sim_attach), entered with calls. Each forwards
 * A0, A1, D0, D1 in mreg[] to c_<name> in sim_hud.h and returns its D0 in D0 and A0.
 */
	.macro	FORWARD name, frame, regs:vararg
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
	ret	\regs,\frame
	.endm

	.macro	CALLS name
	add	-4,sp
	mov	a0,(_mreg)
	mov	a1,(_mreg+4)
	mov	d0,(_mreg+8)
	mov	d1,(_mreg+12)
	add	-12,sp
	call	_c_\name,[],0
	add	12,sp
	mov	d0,a0
	ret	[],4
	.endm

	FORWARD	irq_lock, 8, [d2]
	FORWARD	irq_unlock, 8, [d2]
	FORWARD	dcache_flush_all, 8, [d2]
	FORWARD	gui_widget_create, 0x18, [d2,a2,a3]
	FORWARD	gui_container_append, 8, [a2]
	FORWARD	gui_image_set_handle, 0x18, [d2,d3,a2,a3]
	FORWARD	gui_post, 0x40, [a2]

	.section .fw.panel_create,"ax"
	CALLS	panel_create
	.section .fw.panel_destroy,"ax"
	CALLS	panel_destroy
	.section .fw.app_message,"ax"
	CALLS	app_message

	.section .mock,"ax"
	.global	_sim_attach
_sim_attach:
	CALLS	attach
