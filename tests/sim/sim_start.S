/* Simulator harness for test_api.c: plays the loader (garbage in .bss, `calls` to the payload entry
   with D0 = load base, D1 = size, canaries in the caller's frame), then enters the FENYKL_TASK entry
   test_task_task the way a task is entered. Exit status: see test_api.c; 100 = the payload wrote
   into the loader's frame, 101 = the task entry returned. */
	.section .mock,"ax"
	.global _sim_start
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
2:	add	-16,sp
	mov	0xc0de0004,d0
	mov	d0,(4,sp)
	mov	0xc0de0008,d0
	mov	d0,(8,sp)
	mov	0xc0de000c,d0
	mov	d0,(12,sp)
	mov	0x6ea70000,d0
	mov	0x1234,d1
	mov	_start,a0
	calls	(a0)
	mov	100,d1
	mov	(4,sp),d0
	cmp	0xc0de0004,d0
	bne	.Lexit
	mov	(8,sp),d0
	cmp	0xc0de0008,d0
	bne	.Lexit
	mov	(12,sp),d0
	cmp	0xc0de000c,d0
	bne	.Lexit
	mov	_test_task_task,a0
	calls	(a0)
	mov	101,d1
.Lexit:
	mov	1,d0			/* SYS_exit(d1), GDB simulator only */
	syscall
