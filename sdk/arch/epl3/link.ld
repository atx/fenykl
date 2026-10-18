/* E-PL3 FENYKL.BIN payload. The loader reads
   the file to 0x6EA70000; the first 0x1C bytes are the FNL2 header added by `fenykl payload pack`,
   so the code starts at 0x6EA7001C. Everything including .bss must stay below the loader stack at
   0x6EAEA000. ORIGIN and LENGTH follow load and max_size in target.toml (`fenykl payload pack`
   checks the result against them). */
ENTRY(_start)

MEMORY
{
	payload (rwx) : ORIGIN = 0x6EA7001C, LENGTH = 0x7A000 - 0x1C
}

SECTIONS
{
	.text : { KEEP(*(.text.start)) *(.text .text.*) } >payload
	.rodata : { *(.rodata .rodata.*) } >payload
	/* Option descriptors of sdk/lib/cfg.c (sdk/docs/config.md), iterated as one array. */
	.fenykl_cfg : ALIGN(4) {
		_fenykl_cfg_start = .;
		KEEP(*(.fenykl_cfg))
		_fenykl_cfg_end = .;
	} >payload
	.data : { *(.data .data.*) . = ALIGN(4); } >payload
	/* FENYKL_SHARED data of both cores: whole 64-byte
	   lines, used only through the uncached alias. In the file, so the loader zeroes it through the
	   uncached alias; .bss is cleared through the cached one. */
	.shared : ALIGN(64) { *(.shared) . = ALIGN(64); } >payload
	.bss (NOLOAD) : ALIGN(4) {
		_fenykl_bss_start = .;	/* C: fenykl_bss_start (C symbols get a leading underscore) */
		*(.bss .bss.* COMMON)
		. = ALIGN(4);
		_fenykl_bss_end = .;
	} >payload
	/DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) }
}
