/* Payload entry. The loader's FENYKL task reserves 16 bytes (add -16,sp) and runs `calls (entry)` with
   D0 = load base, D1 = image size, so only the return address at (0,SP) is ours: (4,SP) and (8,SP)
   belong to the loader's frame. Provide the return slot and GCC's 8-byte home area for _fenykl_start,
   as a reverse thunk. On return the loader ends its task (task_self_end). */
	.section .text.start,"ax"
	.global _start
_start:
	add	-12,sp
	call	_fenykl_start,[],0
	ret	[],12
