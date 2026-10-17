/* Links at the fenykl payload address. */
ENTRY(_start)
SECTIONS
{
	. = 0x6EA70000;
	.text : { *(.text.start) *(.text .text.*) }
	.rodata : { *(.rodata .rodata.*) }
	.data : { *(.data .data.*) }
	.bss : { *(.bss .bss.* COMMON) }
}
