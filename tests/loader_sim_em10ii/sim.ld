/* Simulator layout for the E-M10 Mark II loader stub test: the pinned hook and stub bytes at their run
   addresses, the harness in the gap after sec5 (0x60F8A800..0x61010000 on the camera). The
   .fw.<name> jumps to the mocks are placed by the test with --section-start at the addresses of
   loader.ld. */
ENTRY(_sim_start)

_usr3_tail = 0x6018d73a;

SECTIONS
{
	.hook 0x6018d73a : { KEEP(*(.hook)) }
	.stub 0x6005e000 : { KEEP(*(.stub)) }
	.text 0x60f00000 : {
		*(.text .text.*) *(.rodata .rodata.*) *(.data .data.*) *(.bss .bss.* COMMON)
	}
	/* written by the test payload at fixed addresses (payload.S) */
	.results 0x60ff0000 : { KEEP(*(.results)) }
}
