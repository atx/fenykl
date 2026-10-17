/* Mocks for the CPU1 runtime simulator test (test_cpu1.c), placed at the real firmware addresses with
   the jmp trick, and the fake TCB table word of task 0x1002. mock_args[] (test_cpu1.c):
     [0] OlyTaskStart calls  [1] its id   [2] OlyTaskStop calls  [3] its id
     [4] != 0: OlyTaskStop makes the fake TCB dormant
     [5] OlyTaskSleep calls  [6] != 0: sleeps left until the fake TCB becomes dormant
     [7] dcache_purge_all_irqsafe calls */
	.macro	MOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm
	.equ	ARGS, _mock_args
	.equ	TCB_STATE, 0xa07f0474 + 0xc

	.macro	DORMANT
	mov	0x10,d1
	mov	TCB_STATE,a0
	movhu	d1,(a0)
	.endm

/* k_tcb_table_cpu1: entry 1 is task 0x1002's TCB */
	.section .fw.tcbtab,"aw"
	.long	0, 0xa07f0474

/* int OlyTaskStart(id@D0); ret [d2,d3],12 */
	MOCK	OlyTaskStart
	mov	(ARGS),d1
	add	1,d1
	mov	d1,(ARGS)
	mov	d0,(ARGS+4)
	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0,d0
	ret	[d2,d3],12

/* int OlyTaskStop(id@D0); ret [d2,d3],12 */
	MOCK	OlyTaskStop
	mov	(ARGS+8),d1
	add	1,d1
	mov	d1,(ARGS+8)
	mov	d0,(ARGS+12)
	mov	(ARGS+16),d1
	cmp	0,d1
	beq	1f
	DORMANT
1:	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0,d0
	ret	[d2,d3],12

/* int OlyTaskSleep(ticks@D0); ret [d2],8 */
	MOCK	OlyTaskSleep
	mov	(ARGS+20),d1
	add	1,d1
	mov	d1,(ARGS+20)
	mov	(ARGS+24),d1
	cmp	0,d1
	beq	1f
	add	-1,d1
	mov	d1,(ARGS+24)
	cmp	0,d1
	bne	1f
	DORMANT
1:	mov	0xdead0d02,d2
	mov	0,d0
	ret	[d2],8

/* void dcache_purge_all_irqsafe(); ret [d2],8 */
	MOCK	dcache_purge_all_irqsafe
	mov	(ARGS+28),d1
	add	1,d1
	mov	d1,(ARGS+28)
	mov	0xdead0d02,d2
	ret	[d2],8

/* void sim_enter_task(entry@D0, arg@D1): enters `entry` the way the kernel's task start
   (k_task_exit_ret 0x6E6851F1) does, `calls` with D0 = exinf and the return address in the top
   stack word, and returns when it returns. */
	.section .mock,"ax"
	.global	_sim_enter_task
_sim_enter_task:
	add	-4,sp
	mov	d0,a0
	mov	d1,d0
	calls	(a0)
	ret	[],4
