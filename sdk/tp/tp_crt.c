/* The header of every test program (sdk/include/tp.h), first in its slot; `fenykl tp build`
   fills in crc. */
#include <tp.h>

/* Absolute symbols of tp.ld: their addresses are the sizes. */
extern char tp_size[], tp_mem[];

__attribute__((section(".tp_hdr"), used)) const struct fenykl_tp_hdr tp_hdr = {
	.magic = FENYKL_TP_MAGIC,
	.api = FENYKL_TP_API,
	.base = (uint32_t)&tp_hdr,
	.size = (uint32_t)tp_size,
	.mem = (uint32_t)tp_mem,
	.entry = (uint32_t)tp_main,
};
