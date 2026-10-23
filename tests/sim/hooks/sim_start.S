/* Simulator harness for test_hooks.c: sets the stack, clears .bss (the loader does), runs the
   payload entry _start (crt0 -> fenykl_start -> plugin_main), then exits with sim_exit_code. */
	.section .mock,"ax"
	.global	_sim_start
_sim_start:
	mov	0x6eaee000,a0
	mov	a0,sp
	mov	_fenykl_bss_start,a0
	mov	_fenykl_bss_end,a1
	mov	0xa5a5a5a5,d0
1:	cmp	a1,a0
	bcc	2f
	mov	d0,(a0)
	add	4,a0
	bra	1b
2:	mov	0x6ea70000,d0
	mov	0x1234,d1
	mov	_start,a0
	calls	(a0)
	mov	(_sim_exit_code),d1
	cmp	255,d1		/* the exit status is 8 bits: 256 and up would wrap */
	bls	3f
	mov	255,d1
3:	mov	1,d0
	syscall
