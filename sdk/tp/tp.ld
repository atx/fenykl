/* A test program at its slot (`fenykl tp build`): TP_BASE and TP_SLOT_SIZE come from --defsym.
   The header first, then code and data as one image; .bss follows, cleared by the monitor. */
ENTRY(_tp_main)

SECTIONS
{
	. = TP_BASE;
	/* every file section ends aligned: an empty .data would leave the file short of _tp_size */
	.text : { KEEP(*(.tp_hdr)) *(.text .text.*) . = ALIGN(4); }
	.rodata : { *(.rodata .rodata.*) . = ALIGN(4); }
	.data : { *(.data .data.*) . = ALIGN(4); }
	_tp_file_end = .;
	.bss (NOLOAD) : { *(.bss .bss.* COMMON) . = ALIGN(4); }
	_tp_mem_end = .;
	_tp_size = ABSOLUTE(_tp_file_end - TP_BASE);
	_tp_mem = ABSOLUTE(_tp_mem_end - TP_BASE);
	ASSERT(_tp_mem_end - TP_BASE <= TP_SLOT_SIZE, "test program larger than its slot")
	/DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) }
}
