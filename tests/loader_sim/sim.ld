/* Simulator layout for the loader stub test: the pinned hook and stub bytes at their run addresses,
   the harness above the loader stack. The .fw.<name> jumps to the mocks are placed by the test with
   --section-start at the addresses of loader.ld. */
ENTRY(_sim_start)

_usr1_tail = 0x6e70a0ab;

SECTIONS
{
	.hook 0x6e70a0ab : { KEEP(*(.hook)) }
	.stub 0x6e7ba37b : { KEEP(*(.stub)) }
	.text 0x6eb00000 : {
		*(.text .text.*) *(.rodata .rodata.*) *(.data .data.*) *(.bss .bss.* COMMON)
	}
	/* written by the test payload at fixed addresses (payload.S) */
	.results 0x6ebf0000 : { KEEP(*(.results)) }
}
