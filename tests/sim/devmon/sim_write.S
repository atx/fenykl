/* void sim_write(buf@D0, len@D1): SYS_write to stdout (GDB simulator). */
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
