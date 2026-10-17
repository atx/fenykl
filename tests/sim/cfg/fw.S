/*
 * The cache mock the configuration library needs on top of tests/sim/log/fw.S (linked with it):
 * the simulator has no cache, so the purge only counts.
 */
	.section .fw.dcache_purge_all_irqsafe,"ax"
	jmp	_mock_dcache_purge_all_irqsafe
	.section .mock,"ax"
_mock_dcache_purge_all_irqsafe:
	mov	(_mock_purges),d0
	add	1,d0
	mov	d0,(_mock_purges)
	ret	[d2],8
