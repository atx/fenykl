/* E-M10 II FENYKL.BIN payload. The loader reads the file to 0x60DA5000, the never-loaded gap between
   sec4 and sec5; the first 0x1C bytes are the FNL2 header added by `fenykl payload pack`, so the code
   starts at 0x60DA501C. Everything including .bss must stay below the FENYKL task stack at
   0x60E0C000..0x60E10000. ORIGIN and LENGTH follow load and max_size in target.toml (`fenykl payload
   pack` checks the result against them). */
ENTRY(_start)

MEMORY
{
	payload (rwx) : ORIGIN = 0x60DA501C, LENGTH = 0x67000 - 0x1C
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
	/* FENYKL_SHARED data (lib/log_rec.c's CPU1 ring): whole 64-byte lines, used only through the
	   uncached alias. In the file as zeros: the loader copies them through the cached alias and
	   writes the D-cache back before the entry. */
	.shared : ALIGN(64) { *(.shared) . = ALIGN(64); } >payload
	.bss (NOLOAD) : ALIGN(4) {
		_fenykl_bss_start = .;	/* C: fenykl_bss_start (C symbols get a leading underscore) */
		*(.bss .bss.* COMMON)
		. = ALIGN(4);
		_fenykl_bss_end = .;
	} >payload
	/DISCARD/ : { *(.comment) *(.note .note.*) *(.eh_frame .eh_frame_hdr) }
}
