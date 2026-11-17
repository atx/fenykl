/* Simulator harness for the pinned E-M10 Mark II loader bytes (tests/test_fenykl_loader.py).

   _sim_start plays Usr3: it CALLs the hooked tail at 0x6018D73A (hook + stock `ret [d2,a2,a3],16`)
   with canaries in D2/D3/A2/A3, checks that they and SP survive, then, if the stub created a task,
   enters it the way the kernel does (SP = stack base + size - 4, jump to the entry). Without a task
   it calls sim_finish (check.c) at once; otherwise the second task_self_end (end of the task) does.

   Each mock reads its arguments where the firmware function does (sdk/arch/em10ii/firmware.toml
   notes), records them in check.c's globals, clobbers D0/D1/A0/A1/MDR and every register its CALL
   saves, and returns with the firmware function's own RET. OlyTaskSleep advances the scenario: in the
   stub (no task created yet) the loaded-section mask becomes scn_loaded1 after scn_sec_after sleeps;
   in the task fileif_get_media_status returns scn_busy_status until scn_mount_after sleeps have
   passed, then 0x0700 (ready). The card holds FENYKL.TST (scn_tst, while scn_tst_present and not
   removed) and FENYKL.BIN (scn_bin, while scn_bin_present); rpc_fopen picks the file by the path's
   extension, rpc_fread copies it. CHCTR (abs16 0x8070) is plain memory here, initialised to 3. Exit
   status 1xx: see below. */

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

	.equ	USR3_SP, 0x618fe000
	.equ	FP_TST, 0xf11e0001	/* FILE*s returned by the rpc_fopen mock */
	.equ	FP_BIN, 0xf11e0002
	.equ	GATE, 0x61843f64
	.equ	FACTOR, 0xa1c17c14
	.equ	BATSRC, 0xa1bf8408
	.equ	BATLVL, 0xa1bf8406
	.equ	LOADED, 0xa1c18044
	.equ	READY_STATUS, 0x0700	/* fileif_get_media_status: FAT mounted, no DCF mount */
	.equ	CHCTR, 0x8070
	.equ	COOKIE, 0x0c00c1e5	/* irq_lock result */

	.text
	.global	_sim_start
_sim_start:
	mov	USR3_SP,a0
	mov	a0,sp
	mov	(_scn_gate),d0
	movhu	d0,(GATE)
	mov	(_scn_factor),d0
	mov	d0,(FACTOR)
	mov	(_scn_batsrc),d0
	movhu	d0,(BATSRC)
	mov	(_scn_batlvl),d0
	movhu	d0,(BATLVL)
	mov	(_scn_loaded0),d0
	movbu	d0,(LOADED)
	mov	3,d0
	movhu	d0,(CHCTR)
	mov	0x5eed0d02,d2
	mov	0x5eed0d03,d3
	mov	0x5eed0a02,a2
	mov	0x5eed0a03,a3
	call	_usr3_tail,[d2,a2,a3],16
	cmp	0x5eed0d02,d2
	bne	1f
	cmp	0x5eed0d03,d3
	bne	1f
	cmp	0x5eed0a02,a2
	bne	1f
	cmp	0x5eed0a03,a3
	bne	1f
	mov	sp,a0
	cmp	USR3_SP,a0
	bne	1f
	mov	(_create_count),d0
	cmp	0,d0
	beq	4f
	cmp	1,d0
	bne	2f
	mov	(_create_base),d0
	mov	(_create_size),d1
	add	d1,d0
	add	-4,d0
	mov	d0,a0
	mov	a0,sp
	mov	(_create_entry),a0
	jmp	(a0)
4:	call	_sim_finish,[],0	/* no task: the checks run now */
	mov	d0,d1
	mov	1,d0
	syscall
1:	EXIT	101			/* the hook clobbered a callee-saved register or SP */
2:	EXIT	102			/* OlyTaskCreate called more than once */

/* OlyTaskCreate(name@A0, entry@A1, mode@D0, exinf@D1, prio u16@(4,SP), size@(8,SP), base@(12,SP))
   = 0x7c; caller's (4,SP) is (0x38,SP) here; ret [d2,d3,a2,a3],0x34 */
	MOCK	OlyTaskCreate
	mov	a0,(_create_name)
	mov	a1,(_create_entry)
	mov	d0,(_create_mode)
	mov	d1,(_create_exinf)
	movhu	(0x38,sp),d0
	mov	d0,(_create_prio)
	mov	(0x3c,sp),d0
	mov	d0,(_create_size)
	mov	(0x40,sp),d0
	mov	d0,(_create_base)
	COUNT	_create_count
	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	SCRATCH
	mov	0xdead0004,a0
	mov	0x7c,d0
	ret	[d2,d3,a2,a3],0x34

/* task_self_end(); ret [],8. The first call ends Usr3 (returns here, the real one does not
   return), the second one ends the FENYKL task: check the records and exit. */
	MOCK	task_self_end
	COUNT	_selfend_count
	cmp	2,d0
	beq	1f
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
	mov	(_create_count),d1
	cmp	0,d1
	bne	2f
	mov	d0,(_stub_sleep_ticks)	/* stub: sections load */
	COUNT	_stub_sleeps
	mov	(_scn_sec_after),d1
	cmp	d1,d0
	bcs	3f
	mov	(_scn_loaded1),d0
	movbu	d0,(LOADED)
	bra	3f
2:	mov	d0,(_task_sleep_ticks)	/* task: the card mounts */
	COUNT	_task_sleeps
3:	mov	(_stub_sleeps),d0
	mov	(_task_sleeps),d1
	add	d1,d0
	cmp	1000,d0
	bls	4f
	EXIT	103			/* runaway sleep loop */
4:	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0004,a0
	clr	d0
	ret	[d2],8

/* FILE *rpc_fopen(path@A0, mode@A1) -> A0; ret [],4. Records the first 4 paths; character 10
   of "C:\FENYKL.TST" / "C:\FENYKL.BIN" selects the file. */
	MOCK	rpc_fopen
	mov	a1,(_fopen_mode)
	mov	(_fopen_count),d0
	cmp	4,d0
	bcc	1f
	asl2	d0
	mov	_fopen_paths,a1
	add	d0,a1
	mov	a0,(a1)
1:	COUNT	_fopen_count
	mov	(_status_calls_ready),d0
	cmp	0,d0
	bne	1f
	EXIT	104			/* opened before the card was ready */
1:	movbu	(10,a0),d0
	mov	0,a0
	cmp	0x54,d0			/* 'T' */
	bne	2f
	mov	(_scn_tst_present),d1
	mov	(_fremove_done),d0
	cmp	0,d0
	bne	3f
	cmp	0,d1
	beq	3f
	mov	FP_TST,a0
	bra	3f
2:	mov	(_scn_bin_present),d1
	cmp	0,d1
	beq	3f
	mov	FP_BIN,a0
3:	SCRATCH
	mov	0xdead0003,d0
	ret	[],4

/* int rpc_fread(buf@A0, size@D0, n@D1, fp@A1) -> D0 = items; ret [d2,d3,a2,a3],0x18.
   Copies min(n, file length) bytes of the file behind fp to buf. */
	MOCK	rpc_fread
	mov	a0,(_fread_buf)
	mov	d0,(_fread_size)
	mov	d1,(_fread_n)
	mov	a1,(_fread_fp)
	COUNT	_fread_count
	mov	(_fread_buf),a0
	mov	(_scn_bin_len),d0
	mov	_scn_bin,a1
	mov	(_fread_fp),d1
	cmp	FP_BIN,d1
	beq	1f
	mov	(_scn_tst_len),d0
	mov	_scn_tst,a1
	cmp	FP_TST,d1
	beq	1f
	EXIT	105			/* read from a FILE* rpc_fopen did not return */
1:	mov	(_fread_n),d1
	cmp	d1,d0
	bls	1f
	mov	d1,d0			/* D0 = bytes to copy */
1:	mov	d0,(_fread_result)
	cmp	0,d0
	beq	3f
2:	movbu	(a1),d1
	movbu	d1,(a0)
	inc	a0
	inc	a1
	add	-1,d0
	bne	2b
3:	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	SCRATCH
	mov	0xdead0004,a0
	mov	(_fread_result),d0
	ret	[d2,d3,a2,a3],0x18

/* int rpc_fremove(path@A0) -> D0 (0 = removed); ret [d2,a2,a3],0x18 */
	MOCK	rpc_fremove
	mov	a0,(_fremove_path)
	COUNT	_fremove_count
	mov	(_fclose_count),d0
	mov	d0,(_fremove_after_closes)
	mov	(_scn_remove_result),d0
	cmp	0,d0
	bne	1f
	mov	1,d1
	mov	d1,(_fremove_done)
1:	mov	0xdead0d02,d2
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	SCRATCH
	mov	0xdead0004,a0
	mov	(_scn_remove_result),d0
	ret	[d2,a2,a3],0x18

/* int fileif_exists(path@A0) -> D0 1 = exists; ret [],8 */
	MOCK	fileif_exists
	mov	a0,(_exists_path)
	COUNT	_exists_count
	SCRATCH
	mov	0xdead0004,a0
	mov	(_scn_exists_after),d0
	ret	[],8

/* unsigned fileif_get_media_status(drive@D0); ret [d2,a2],0xc */
	MOCK	fileif_get_media_status
	mov	d0,(_status_drive)
	COUNT	_status_calls
	mov	(_scn_busy_status),d0
	mov	(_task_sleeps),d1
	mov	(_scn_mount_after),a0
	cmp	a0,d1
	bcs	1f
	mov	1,d0
	mov	d0,(_status_calls_ready)
	mov	READY_STATUS,d0
1:	mov	0xdead0d02,d2
	mov	0xdead0a02,a2
	SCRATCH
	mov	0xdead0004,a0
	ret	[d2,a2],0xc

/* unsigned ui_key_get_state(code@D0); ret [d2,d3,a2],0x18 */
	MOCK	ui_key_get_state
	mov	d0,(_key_code)
	COUNT	_key_count
	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a02,a2
	SCRATCH
	mov	0xdead0004,a0
	mov	(_scn_usb_key),d0
	ret	[d2,d3,a2],0x18

/* u8 pwr_wake_input_levels(); retf [d2,d3],8 (a leaf: return through MDR) */
	MOCK	pwr_wake_input_levels
	COUNT	_levels_count
	mov	(_create_count),d0
	cmp	0,d0
	beq	1f
	EXIT	106			/* called from the task */
1:	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0001,d1
	mov	0xdead0002,a1
	mov	0xdead0004,a0
	mov	(_scn_levels),d0
	retf	[d2,d3],8

/* int rpc_fclose(fp@A0); ret [d2,a2,a3],0x10 */
	MOCK	rpc_fclose
	mov	a0,(_fclose_fp)
	COUNT	_fclose_count
	mov	0xdead0d02,d2
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	SCRATCH
	mov	0xdead0004,a0
	clr	d0
	ret	[d2,a2,a3],0x10

/* unsigned irq_lock(level@D0) -> cookie; ret [d2],8 */
	MOCK	irq_lock
	mov	d0,(_lock_level)
	COUNT	_lock_count
	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0004,a0
	mov	COOKIE,d0
	ret	[d2],8

/* irq_unlock(cookie@D0); ret [d2],8 */
	MOCK	irq_unlock
	mov	d0,(_unlock_cookie)
	COUNT	_unlock_count
	movhu	(CHCTR),d0
	mov	d0,(_unlock_chctr)
	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	ret	[d2],8

/* dcache_flush_all(); ret [d2],8 */
	MOCK	dcache_flush_all
	COUNT	_flush_count
	mov	(_lock_count),d0
	mov	(_unlock_count),d1
	sub	d1,d0
	mov	d0,(_flush_locks)
	mov	(_fread_count),d0
	mov	d0,(_flush_after_reads)
	mov	(_fremove_count),d0
	mov	d0,(_flush_after_removes)
	mov	(_exists_count),d0
	mov	d0,(_flush_after_exists)
	movhu	(CHCTR),d0
	mov	d0,(_flush_chctr)
	mov	0xdead0d02,d2
	SCRATCH
	mov	0xdead0003,d0
	mov	0xdead0004,a0
	ret	[d2],8
