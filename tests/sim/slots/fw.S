/* Mock firmware for the callback/slot/vtable simulator test (test_slots.c). The callers enter plugin
   code the way the firmware does (`calls (An)`, or a CALL with a register list and frame), keep
   sentinels in the callee-saved registers and canaries in their own frame, and return 0xbad if
   any of them, SP or their outgoing stack arguments changed. They are entered from C with
   `call x,[],0`, so they save D2/D3/A2/A3 themselves (movm), as firmware methods do. The mock
   targets sit at the real addresses of the stock values the slots and the vtable declare. */
	.equ	ARGS, _mock_args

	.macro	SENTINELS
	mov	0x5a5a0002,d2
	mov	0x5a5a0003,d3
	mov	0x5a5a000a,a2
	.endm

	/* Fails unless D2/D3/A2, SP (saved at (16,sp)) and the canary at (12,sp) are unchanged. */
	.macro	CHECK bad
	cmp	0x5a5a0002,d2
	bne	\bad
	cmp	0x5a5a0003,d3
	bne	\bad
	cmp	0x5a5a000a,a2
	bne	\bad
	mov	(16,sp),a1
	mov	sp,a0
	cmp	a1,a0
	bne	\bad
	mov	(12,sp),d1
	cmp	0xc0de000c,d1
	bne	\bad
	.endm

	/* Frame: (0) return slot, (4)/(8) outgoing stack arguments, (12) canary, (16) SP, (20) free.
	   Keeps D0/D1 (GCC arguments 1-2). */
	.macro	ENTER
	movm	[d2,d3,a2,a3],(sp)
	add	-24,sp
	mov	0xc0de000c,a0
	mov	a0,(12,sp)
	mov	sp,a1
	mov	a1,(16,sp)
	.endm

	.section .mock,"ax"

/* int sim_calls_cbmix(uint32_t fn): calls (a3) with a = 1@D0, p = &cells[0]@A0, b = 2@D1,
   q = &cells[1]@A1, c = 3@(4,SP), r = &cells[2]@(8,SP); returns the D0 result. */
	.global	_sim_calls_cbmix
_sim_calls_cbmix:
	ENTER
	mov	d0,a3
	SENTINELS
	mov	3,d0
	mov	d0,(4,sp)
	mov	_mock_cells+8,a0
	mov	a0,(8,sp)
	mov	1,d0
	mov	2,d1
	mov	_mock_cells,a0
	mov	_mock_cells+4,a1
	calls	(a3)
	CHECK	.Lbad_mix
	mov	(4,sp),d1
	cmp	3,d1
	bne	.Lbad_mix
	mov	(8,sp),a1
	cmp	_mock_cells+8,a1
	bne	.Lbad_mix
	ret	[d2,d3,a2,a3],40
.Lbad_mix:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40

/* uint32_t sim_calls_cbptr(uint32_t fn): calls (a3) with base = &cells[0]@A0, index = 2@D0 and
   returns the pointer result as the firmware reads it, from A0. */
	.global	_sim_calls_cbptr
_sim_calls_cbptr:
	ENTER
	mov	d0,a3
	SENTINELS
	mov	_mock_cells,a0
	mov	2,d0
	calls	(a3)
	mov	a0,(20,sp)			/* only A0 counts */
	CHECK	.Lbad_ptr
	mov	(20,sp),d0
	ret	[d2,d3,a2,a3],40
.Lbad_ptr:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40

/* int sim_call_cbcall(void): CALLs the callback stub bound in test_slots.c with [d2,a2],12, as a
   firmware caller of a CALL-entered function: a = 1@D0, b = 2@D1, c = 3@(4,SP). */
	.global	_sim_call_cbcall
_sim_call_cbcall:
	ENTER
	SENTINELS
	mov	0x5a5a000b,a3
	mov	3,d0
	mov	d0,(4,sp)
	mov	1,d0
	mov	2,d1
	call	_fenykl_cb_cbcall_cbcallfn,[d2,a2],12
	CHECK	.Lbad_call
	cmp	0x5a5a000b,a3
	bne	.Lbad_call
	mov	(4,sp),d1
	cmp	3,d1
	bne	.Lbad_call
	ret	[d2,d3,a2,a3],40
.Lbad_call:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40

/* unsigned sim_hook1(unsigned event): appevent_call_hook1 0x6E8606D6 (without the gate):
   calls (0x605FD840) if it is not 0, D0 = event in and out. */
	.global	_sim_hook1
_sim_hook1:
	ENTER
	SENTINELS
	mov	(0x605fd840),a0
	cmp	0,a0
	beq	1f
	calls	(a0)
1:	CHECK	.Lbad_hook1
	ret	[d2,d3,a2,a3],40
.Lbad_hook1:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40

/* int sim_change(unsigned id): usrset_call_change_cb 0x6E8D3411: calls (0x6034CCB4) with D0 = id;
   returns 0 or 0xbad. */
	.global	_sim_change
_sim_change:
	ENTER
	SENTINELS
	mov	(0x6034ccb4),a0
	exthu	d0
	calls	(a0)
	CHECK	.Lbad_change
	clr	d0
	ret	[d2,d3,a2,a3],40
.Lbad_change:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40

/* int sim_vget(void *obj, unsigned *out, unsigned id): as gui_menu_get_setting 0x6F10994C: A0 = obj,
   A2 = (8,vptr), A1 = out, D0 = id, calls (a2). D3 is the sentinel the callee must keep. */
	.global	_sim_vget
_sim_vget:
	ENTER
	mov	d0,a0
	mov	d1,a1
	mov	(52,sp),d0			/* id: GCC (12,SP) at entry, + 16 (movm) + 24 */
	mov	0x5a5a0003,d3
	mov	0x5a5a0002,d2
	mov	(a0),a2
	mov	(8,a2),a2
	calls	(a2)
	cmp	0x5a5a0003,d3
	bne	.Lbad_vget
	cmp	0x5a5a0002,d2
	bne	.Lbad_vget
	mov	(16,sp),a1
	mov	sp,a0
	cmp	a1,a0
	bne	.Lbad_vget
	ret	[d2,d3,a2,a3],40
.Lbad_vget:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40

/* int sim_vset(void *obj, unsigned id, unsigned type, unsigned value): as gui_menu_set_setting
   0x6F109962: A0 = obj, D0 = id, D1 = type, A1 = value, calls (a2) with A2 = (4,vptr). */
	.global	_sim_vset
_sim_vset:
	ENTER
	mov	d0,a0
	mov	d1,d0
	mov	(52,sp),d1
	mov	(56,sp),a1
	mov	0x5a5a0003,d3
	mov	(a0),a2
	mov	(4,a2),a2
	calls	(a2)
	cmp	0x5a5a0003,d3
	bne	.Lbad_vset
	ret	[d2,d3,a2,a3],40
.Lbad_vset:
	mov	0xbad,d0
	ret	[d2,d3,a2,a3],40


/* Targets for the fw_call_* forward thunks. */

/* int mock_mix(a@D0, p@A0, b@D1, q@A1, c@(4,SP), r@(8,SP)): self-saving (`calls`); records the
   arguments in mock_args[0..5], clobbers D2/D3/A2/A3 (restored by its RET) and MDR. */
	.global	_mock_mix
_mock_mix:
	movm	[d2,d3,a2,a3],(sp)
	add	-8,sp
	mov	d0,(ARGS)
	mov	a0,(ARGS+4)
	mov	d1,(ARGS+8)
	mov	a1,(ARGS+12)
	mov	(28,sp),d2			/* c: (4,SP) at entry + 24 */
	mov	d2,(ARGS+16)
	mov	(32,sp),d2			/* r */
	mov	d2,(ARGS+20)
	mov	0xdead0d02,d2
	mov	0xdead0d03,d3
	mov	0xdead0a02,a2
	mov	0xdead0a03,a3
	mov	d2,mdr
	mov	0x600d,d0
	mov	0xdead0004,a0
	ret	[d2,d3,a2,a3],24

/* unsigned *mock_ptr(base@A0, index@D0): rets A0 = base + 4 * index, garbage in D0. */
	.global	_mock_ptr
_mock_ptr:
	asl2	d0
	add	d0,a0
	mov	0xdead0003,d0
	rets

/* int mock_callee(a@D0, b@D1, c@(4,SP)): CALL-entered with [d2,a2],12 (c at (16,SP));
   returns a + b + c, clobbers D2/A2 (the RET restores them). */
	.global	_mock_callee
_mock_callee:
	add	d1,d0
	mov	(16,sp),d1
	add	d1,d0
	mov	0xdead0d02,d2
	mov	0xdead0a02,a2
	ret	[d2,a2],12


/* Stock targets at their real addresses (firmware.toml slots/vtables). */

/* usrsetmgr_change_cb 0x6EC0A5DB: void (id@D0); mock_args[6] = id, mock_args[7]++. */
	.section .fw.change_cb,"ax"
_mock_change_cb:
	add	-4,sp
	mov	d0,(ARGS+24)
	mov	(ARGS+28),d0
	add	1,d0
	mov	d0,(ARGS+28)
	mov	0xdead0003,d0
	ret	[],4

/* hfw_getSetting 0x6EB8D163, shaped like the real one: *out = (u16)id + object word 1, returns 0. */
	.section .fw.hfw_get,"ax"
_mock_hfw_get:
	movm	[a2],(sp)
	add	-8,sp
	mov	a1,a2
	mov	(4,a0),d1
	exthu	d0
	add	d1,d0
	mov	d0,(a2)
	clr	d0
	mov	0xdead0a01,a1
	ret	[a2],12

/* hfw_setSetting 0x6EB8D898: records this/id/type/value in mock_args[8..11], returns 1. */
	.section .fw.hfw_set,"ax"
_mock_hfw_set:
	movm	[d2,d3,a2,a3],(sp)
	add	-40,sp
	mov	a0,(ARGS+32)
	mov	d0,(ARGS+36)
	mov	d1,(ARGS+40)
	mov	a1,(ARGS+44)
	mov	1,d0
	ret	[d2,d3,a2,a3],56

/* The stock vtable 0x6EE3F58C: 28 slots, methods +4 and +8 mocked, the rest fail loudly. */
	.section .fw.vtable,"aw"
	.long	_mock_vbad, _mock_hfw_set, _mock_hfw_get
	.rept	25
	.long	_mock_vbad
	.endr

	.section .mock,"ax"
_mock_vbad:
	mov	0xbad,d0
	rets
