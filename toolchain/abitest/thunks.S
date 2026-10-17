/* GCC-ABI -> Panasonic-ABI thunks (C calls firmware) and one reverse thunk (firmware calls C).
   GCC ABI at entry: (0,SP) = return address, args 1-2 in D0/D1 whatever their type, (4,SP) and
   (8,SP) = home slots for D0/D1 owned by the callee, arg n >= 3 at (4n,SP); D2/D3/A2/A3 preserved.
   Panasonic ABI: the CALL must carry the callee's own register list and frame size (copied from
   its RET/RETF), and the CALL stores its return address at (0,SP), so a thunk needs a frame of
   its own: 4 bytes + 4 per stack argument. */
	.text

/* int fw_mix(const int *p, int a, int b, const int *q): no stack arguments. */
	.global	_fw_mix
_fw_mix:
	add	-4,sp
	mov	d0,a0
	mov	d1,d0
	mov	(16,sp),d1		/* b: (12,SP) at entry */
	mov	(20,sp),a1		/* q: (16,SP) at entry */
	call	_pana_mix,[d2,d3,a2,a3],24
	ret	[],4

/* int fw_stack(const char *name, int mode, int exinf, void *entry, int prio, int size, void *base):
   three Panasonic stack arguments. */
	.global	_fw_stack
_fw_stack:
	add	-16,sp
	mov	(36,sp),a0		/* prio: (20,SP) at entry */
	mov	a0,(4,sp)
	mov	(40,sp),a0		/* size */
	mov	a0,(8,sp)
	mov	(44,sp),a0		/* base */
	mov	a0,(12,sp)
	mov	d0,a0			/* name */
	mov	d1,d0			/* mode */
	mov	(28,sp),d1		/* exinf: (12,SP) at entry */
	mov	(32,sp),a1		/* entry: (16,SP) at entry */
	call	_pana_stack,[d2,d3,a2,a3],0x30
	ret	[],16

/* int *fw_vsum(int *out, int n, ...): variadic, at most 8 variadic words are forwarded. */
	.global	_fw_vsum
_fw_vsum:
	add	-40,sp			/* return slot, n, 8 variadic words */
	mov	d1,(4,sp)		/* n: last named argument goes on the stack */
	mov	(52,sp),d1		/* variadic word k: (12+4k,SP) at entry -> (8+4k,SP) */
	mov	d1,(8,sp)
	mov	(56,sp),d1
	mov	d1,(12,sp)
	mov	(60,sp),d1
	mov	d1,(16,sp)
	mov	(64,sp),d1
	mov	d1,(20,sp)
	mov	(68,sp),d1
	mov	d1,(24,sp)
	mov	(72,sp),d1
	mov	d1,(28,sp)
	mov	(76,sp),d1
	mov	d1,(32,sp)
	mov	(80,sp),d1
	mov	d1,(36,sp)
	mov	d0,a0			/* out */
	call	_pana_vsum,[d2,a2],0x10
	ret	[],40			/* pointer result already in A0, where GCC expects it */

/* int fw_callcb(int (*fn)(void *, int), void *ctx, int v); fn must be a Panasonic-ABI function
   such as cb_thunk below. */
	.global	_fw_callcb
_fw_callcb:
	add	-4,sp
	mov	d0,a0			/* fn */
	mov	d1,a1			/* ctx */
	mov	(16,sp),d0		/* v: (12,SP) at entry */
	call	_pana_callcb,[d2,d3,a2,a3],0x14
	ret	[],4

/* Reverse thunk: the firmware calls it through CALLS with ctx@A0, v@D0 and it calls the GCC
   function int cb(void *ctx, int v). It must provide the return slot and GCC's home area, and it
   cannot be the target of a CALL with a non-empty register list or frame. */
	.global	_cb_thunk
_cb_thunk:
	add	-12,sp
	mov	d0,d1
	mov	a0,d0
	call	_cb,[],0
	ret	[],12
