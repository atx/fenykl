/* Simulator harness for the pinned loader bytes (tests/test_fenykl_loader.py).

   _sim_start plays usr1_task: it CALLs the hooked tail at 0x6E70A0AB (hook + stock `ret [d2],8`)
   with canaries in D2/D3/A2/A3, checks that they and SP survive, then enters the task recorded by the
   OlyTaskCreate mock the way the kernel would (SP = stack base + size, jump to the entry). The second
   OlyTaskSelfEnd (end of the task) calls sim_finish (check.c) and exits with its result.

   Each mock reads its arguments where the firmware function does (sdk/firmware.toml notes), records
   them in check.c's globals, clobbers D0/D1/A0/A1/MDR and every register its CALL saves, and returns
   with the firmware function's own RET. rpc_fopen fails scn_fail_opens times as on an unmounted card,
   then the mount completes (fat_mounted[0] = 1) and it finds the file if scn_file_present. rpc_fread
   copies scn_file to the cached alias of its buffer. Exit status 1xx: see the comments below. */

	.macro	MOCK name
	.section .fw.\name,"ax"
	jmp	_mock_\name
	.text
_mock_\name:
	.endm

	.macro	SCRATCH			/* clobber D1, A1, MDR */
	mov	0xdead0001,d1
	mov	d1,mdr
	mov	0xdead0002,a1
	.endm

	.macro	COUNT var
	mov	(\var),d0
	inc	d0
	mov	d0,(\var)
	.endm

	.macro	EXIT code
	mov	\code,d1
	mov	1,d0			/* SYS_exit(d1), GDB simulator only */
	syscall
	.endm

	.equ	USTART1_SP, 0x6ebfe000
	.equ	FP, 0xf11e0000		/* FILE* returned by the rpc_fopen mock */
	.equ	MOUNTED, 0x6059c978	/* fat_mounted[0] */

	.text
	.global	_sim_start
_sim_start:
	mov	USTART1_SP,a0
	mov	a0,sp
	mov	0x5eed0d02,d2
	mov	0x5eed0d03,d3
	mov	0x5eed0a02,a2
	mov	0x5eed0a03,a3
	mov	(_scn_fail_opens),d0
	cmp	0,d0
	bne	3f
	mov	1,d0			/* card mounted from the start */
	mov	MOUNTED,a0
	movhu	d0,(a0)
3:	call	_usr1_tail,[d2],8
	cmp	0x5eed0d02,d2
	bne	1f
	cmp	0x5eed0d03,d3
	bne	1f
	cmp	0x5eed0a02,a2
	bne	1f
	cmp	0x5eed0a03,a3
	bne	1f
	mov	sp,a0
	cmp	USTART1_SP,a0
	bne	1f
	mov	(_create_count),d0
	cmp	1,d0
	bne	2f
	mov	(_create_base),d0
	mov	(_create_size),d1
	add	d1,d0
	mov	d0,a0
	mov	a0,sp
	mov	(_create_entry),a0
	jmp	(a0)
1:	EXIT	101			/* the hook clobbered a callee-saved register or SP */
2:	EXIT	102			/* OlyTaskCreate not called exactly once */

/* OlyTaskCreate(name@A0, entry@A1, mode@D0, exinf@D1, prio u16@(4,SP), size@(8,SP), base@(12,SP))
   = 0x7c; caller's (4,SP) is (0x34,SP) here; ret [d2,d3,a2,a3],0x30 */
	MOCK	OlyTaskCreate
	mov	a0,(_create_name)
	mov	a1,(_create_entry)
	mov	d0,(_create_mode)
	mov	d1,(_create_exinf)
	movhu	(0x34,sp),d0
	mov	d0,(_create_prio)
	mov	(0x38,sp),d0
	mov	d0,(_create_size)
	mov	(0x3c,sp),d0
	mov	d0,(_create_base)
	COUNT	_create_count
	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	SCRATCH
	mov	0xdead0004,a0
	mov	0x7c,d0
	ret	[d2,d3,a2,a3],0x30

/* OlyTaskSelfEnd(); ret [],8. The first call ends USTART1 (returns here, the real one sleeps
   forever), the second one ends the FENYKL task: check the records and exit. */
	MOCK	OlyTaskSelfEnd
	COUNT	_selfend_count
	cmp	1,d0
	bne	1f
	SCRATCH
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	ret	[],8
1:	call	_sim_finish,[],0	/* GCC code: empty register list and frame */
	mov	d0,d1
	mov	1,d0
	syscall

/* int OlyTaskSleep(ticks@D0); ret [d2],8 */
	MOCK	OlyTaskSleep
	mov	d0,(_sleep_ticks)
	COUNT	_sleep_count
	cmp	1000,d0
	bls	1f
	EXIT	103			/* runaway sleep loop */
1:	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0004,a0
	clr	d0
	ret	[d2],8

/* FILE *rpc_fopen(path@A0, mode@A1) -> A0; ret [],16 */
	MOCK	rpc_fopen
	mov	a0,(_fopen_path)
	mov	a1,(_fopen_mode)
	COUNT	_fopen_count
	mov	(_scn_fail_opens),d1
	cmp	d1,d0
	bhi	2f			/* mounted */
	bne	1f
	mov	1,d1			/* the last failure before the mount completes */
	mov	MOUNTED,a0
	movhu	d1,(a0)
1:	mov	0,a0
	bra	3f
2:	mov	(_scn_file_present),d1
	cmp	0,d1
	beq	1b
	mov	FP,a0
3:	SCRATCH
	mov	0xdead0003,d0
	ret	[],16

/* int rpc_fread(buf@A0, size@D0, n@D1, fp@A1) -> D0 = items; ret [],24. Copies min(n, file length)
   bytes to the cached alias of buf, (buf & 0x3fffffff) | 0x40000000: the simulator has one alias. */
	MOCK	rpc_fread
	mov	a0,(_fread_buf)
	mov	d0,(_fread_size)
	mov	d1,(_fread_n)
	mov	a1,(_fread_fp)
	COUNT	_fread_count
	mov	(_fread_buf),d0
	and	0x3fffffff,d0
	or	0x40000000,d0
	mov	d0,a0
	mov	(_scn_file_len),d0
	mov	(_fread_n),d1
	cmp	d1,d0
	bls	1f
	mov	d1,d0			/* D0 = bytes to copy */
1:	mov	d0,(_fread_result)
	mov	_scn_file,a1
	cmp	0,d0
	beq	3f
2:	movbu	(a1),d1
	movbu	d1,(a0)
	inc	a0
	inc	a1
	add	-1,d0
	bne	2b
3:	SCRATCH
	mov	0xdead0004,a0
	mov	(_fread_result),d0
	ret	[],24

/* int rpc_fclose(fp@A0); ret [],12 */
	MOCK	rpc_fclose
	mov	a0,(_fclose_fp)
	COUNT	_fclose_count
	SCRATCH
	mov	0xdead0004,a0
	clr	d0
	ret	[],12

/* dcache_purge_all_irqsafe(); ret [d2],8 */
	MOCK	dcache_purge_all_irqsafe
	COUNT	_purge_count
	mov	(_fread_count),d0
	mov	d0,(_purge_after_reads)
	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	ret	[d2],8

/* dbg_puts_hex(msg@A0, value@D0); ret [d2,a2],24 */
	MOCK	dbg_puts_hex
	mov	a0,(_status_msg)
	mov	d0,(_status)
	COUNT	_status_count
	mov	0xdead0d02,d2
	mov	0xdead0a02,a2
	SCRATCH
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	ret	[d2,a2],24
