/* Test payload for the loader simulation, linked at LOAD + 0x1C (after the header): records that it
   ran, D0, D1 and SP in check.c's `payload` (section .results at 0x6EBF0000) and returns. */
	.equ	REC, 0x6ebf0000

	.text
	.global	_start
_start:
	mov	d0,(REC+4)
	mov	d1,(REC+8)
	mov	sp,a0
	mov	a0,(REC+12)
	mov	1,d0
	mov	d0,(REC)
	rets
