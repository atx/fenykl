/* Mock firmware functions for the thunk test (test_api.c) and the USB boot test, one per ABI shape.
   A shape is a firmware.toml argument pattern: the classes of `args` in order, `_va` for a trailing
   "...", `none` for no arguments. ARGS_<pattern> reads each argument where the firmware function
   does (D0, D1 and A0, A1 by class, the rest on the caller's stack, which
   is (frame + 4, SP) here) and stores it in C parameter order into mock_args[]. The macros are
   written from the firmware ABI, not from the generator.

   test_sim.py instantiates MOCK for every firmware.toml function without a hand mock (FW) below,
   with its id, its return kind and its CALL operands. Each mock stores its id in mock_args[11],
   clobbers D0/D1/A0/A1/MDR and every
   register its CALL saves, returns 0x7e000000 + id in the register of its return kind (`d`: D0,
   `a`: A0, `v`: none) and garbage in the other, and returns with the function's own RET. A jmp at
   the firmware address (section .fw.<name>, placed with --section-start) leads to it. A function
   with a new argument pattern does not assemble until its ARGS_ macro is added here. */

	.equ	ARGS, _mock_args

	.macro	FW name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.section .mock,"ax"
_mock_\name:
	.endm

	.macro	SCRATCH			/* clobber D1, A1, MDR */
	mov	0xdead0001,d1
	mov	d1,mdr
	mov	0xdead0002,a1
	.endm

	.macro	RESULT_v id
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	.endm

	.macro	RESULT_d id
	mov	0x7e000000 + \id,d0
	mov	0xdead0004,a0
	.endm

	.macro	RESULT_a id
	mov	0xdead0003,d0
	mov	0x7e000000 + \id,a0
	.endm

	.macro	MOCK name, args, ret, id, frame, saves:vararg
	FW	\name
	ARGS_\args \frame
	mov	\id,d0
	mov	d0,(ARGS+44)
	.ifnb	\saves
	.irp	reg, \saves
	mov	0xdead0000,\reg
	.endr
	.endif
	SCRATCH
	RESULT_\ret \id
	ret	[\saves],\frame
	.endm

	.macro	ARGS_none frame
	.endm

	.macro	ARGS_a frame
	mov	a0,(ARGS)
	.endm

	.macro	ARGS_d frame
	mov	d0,(ARGS)
	.endm

	.macro	ARGS_aa frame
	mov	a0,(ARGS)
	mov	a1,(ARGS+4)
	.endm

	.macro	ARGS_ad frame
	mov	a0,(ARGS)
	mov	d0,(ARGS+4)
	.endm

	.macro	ARGS_dd frame
	mov	d0,(ARGS)
	mov	d1,(ARGS+4)
	.endm

	.macro	ARGS_add frame
	mov	a0,(ARGS)
	mov	d0,(ARGS+4)
	mov	d1,(ARGS+8)
	.endm

	.macro	ARGS_adda frame
	mov	a0,(ARGS)
	mov	d0,(ARGS+4)
	mov	d1,(ARGS+8)
	mov	a1,(ARGS+12)
	.endm

	.macro	ARGS_dda frame
	mov	d0,(ARGS)
	mov	d1,(ARGS+4)
	mov	a0,(ARGS+8)
	.endm

/* (A0, A1, D0, D1, stack 0..2): OlyTaskCreate */
	.macro	ARGS_aadddda frame
	mov	a0,(ARGS)
	mov	a1,(ARGS+4)
	mov	d0,(ARGS+8)
	mov	d1,(ARGS+12)
	mov	(\frame+4,sp),d0
	mov	d0,(ARGS+16)
	mov	(\frame+8,sp),d0
	mov	d0,(ARGS+20)
	mov	(\frame+12,sp),d0
	mov	d0,(ARGS+24)
	.endm

/* (A0, stack 0 = the last named argument, stack 1..8 = 8 variadic words): sprintf */
	.macro	ARGS_aa_va frame
	mov	a0,(ARGS)
	mov	sp,a0
	add	\frame+4,a0
	mov	ARGS+4,a1
	mov	9,d1
1:	mov	(a0),d0
	mov	d0,(a1)
	inc4	a0
	inc4	a1
	add	-1,d1
	bne	1b
	.endm

/* int OlyTaskSleep(ticks@D0) = ticks + 1; ret [d2],8. ticks 1000 comes from a FENYKL_TASK entry
   whose function returned: end the simulation with sim_exit_code. */
	FW	OlyTaskSleep
	cmp	1000,d0
	bne	1f
	mov	(_sim_exit_code),d1
	cmp	255,d1		/* the exit status is 8 bits: 256 and up would wrap */
	bls	3f
	mov	255,d1
3:	mov	1,d0
	syscall
1:	mov	d0,(ARGS)
	add	1,d0
	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0004,a0
	ret	[d2],8
