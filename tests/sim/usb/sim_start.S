/* Simulator harness for test_usb.c: stores the power-on factor _sim_poweron_factor (--defsym) where
   the firmware keeps it, runs the payload entry _start (crt0 -> fenykl_start), then exits with
   sim_exit_code: 0 = plugin_main ran, 7 = it was skipped with the breadcrumb, 9 = skipped silently. */
	.section .mock,"ax"
	.global	_sim_start
_sim_start:
	mov	0x6eaee000,a0
	mov	a0,sp
	mov	_sim_poweron_factor,d0
	mov	d0,(0x60551630)
	mov	0x6ea70000,d0
	mov	0x1234,d1
	mov	_start,a0
	calls	(a0)
	mov	(_sim_exit_code),d1
	cmp	7,d1
	bne	1f
	mov	(_mock_args),d0		/* fw_dbg_puts' string, 0 if never called */
	cmp	0,d0
	bne	1f
	mov	9,d1
1:	mov	1,d0
	syscall
