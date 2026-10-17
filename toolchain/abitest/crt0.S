/* Simulator start-up: stack at the top of the fenykl loader stack, run main, exit(main()). */
	.section .text.start,"ax"
	.global _start
_start:
	mov	0x6eaee000,a0
	mov	a0,sp
	add	-12,sp			/* return-address slot + GCC's 8-byte register home area */
	call	_main,[],0
	mov	d0,d1
	mov	1,d0			/* SYS_exit, handled by the GDB simulator only */
	syscall
