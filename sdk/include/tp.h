/* Test programs for the dev monitor (docs/devmon.md, Test programs). A test program is linked at
   one slot of the exec region (`fenykl tp build`), uploaded and run by the monitor on CPU0 in its
   own task, and calls the monitor only through the function table it is passed. It includes this
   header alone, not fenykl.h: it has no firmware thunks. */
#ifndef FENYKL_TP_H
#define FENYKL_TP_H

#include <stdint.h>

#define FENYKL_TP_MAGIC 0x50544B46u	/* "FKTP" */
#define FENYKL_TP_API 1u

/* At the slot's first byte (sdk/tp/tp_crt.c). crc: CRC-32 (zlib) of the bytes [32, size), filled
   in by `fenykl tp build` after the link. */
struct fenykl_tp_hdr {
	uint32_t magic;
	uint32_t api;		/* FENYKL_TP_API the program was built against */
	uint32_t base;		/* link address: the slot */
	uint32_t size;		/* bytes in the file, header included */
	uint32_t mem;		/* size plus .bss, which the monitor clears */
	uint32_t entry;		/* tp_main */
	uint32_t crc;
	uint32_t reserved;
};

/* The monitor's functions. Version 1; later versions only append members (size grows). */
struct fenykl_tp_api {
	uint32_t version;	/* FENYKL_TP_API of the monitor */
	uint32_t size;		/* sizeof(struct fenykl_tp_api) of the monitor */
	/* A line in the monitor's log (card and LOG), formatted at once (%x %u %d %s). */
	void (*log)(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2);
	void (*sleep_ms)(uint32_t ms);
	/* 1 MHz counter, 30 bits, wraps. */
	uint32_t (*timer_us)(void);
	uint32_t (*rd32)(uint32_t addr);
	void (*wr32)(uint32_t addr, uint32_t v);
};

/* The program's entry; its result goes back to the PC. */
int tp_main(const struct fenykl_tp_api *api, uint32_t arg);

#endif
