/* Recording appevent_post mock for the key simulator test: its real address and its real first six
   bytes (the relocate span), like ../hooks/appevent.S. Appends {event, param, flag} to
   sim_posts[sim_nposts++] (no bounds check: the test posts fewer than its array holds) and returns
   1. */
	.section .fw.appevent_post,"ax"
	.global	_fw_mock_appevent_post
_fw_mock_appevent_post:
	mov	d0,(8,sp)			/* 42 08  -- relocate span, 6 bytes */
	mov	d1,(12,sp)			/* 46 0c */
	mov	a0,d0				/* f1 d0 */
	mov	(_sim_nposts),d1
	mov	_sim_posts,a1
	asl2	d1
	add	d1,a1
	add	d1,a1
	add	d1,a1				/* a1 = &sim_posts[n] (12 bytes each) */
	mov	(8,sp),d1
	mov	d1,(a1)
	mov	(12,sp),d1
	mov	d1,(4,a1)
	mov	d0,(8,a1)
	mov	(_sim_nposts),d1
	inc	d1
	mov	d1,(_sim_nposts)
	mov	1,d0
	ret	[],32
