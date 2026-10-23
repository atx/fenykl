/* The live-view face-detection (FD) frame: 640 x 480 Y8, written by the IPU into a ring of 5 slots
   (ROM table 0x6E9EA9EC = 0xADC56300 + 0x4B000 i). Once per FD frame mov_fd_publish 0x6E8AC61D
   copies the ring index 0x606067A4 to 0x606067AF and stores the slot address (uncached alias) at
   0x606067B0. CPU0 reads it there (a cached firmware variable: not from CPU1). */
#ifndef BD_FD_FRAME_H
#define BD_FD_FRAME_H

#include <fenykl.h>

#define BD_FD_PUB 0x606067B0u		/* u32: FD slot published by mov_fd_publish */
#define BD_FD_SLOT0 0xADC56300u
#define BD_FD_SLOT_SIZE 0x4B000u
#define BD_FD_SLOTS 5u

static inline bool bd_fd_is_slot(uint32_t addr)
{
	uint32_t off = (uint32_t)FENYKL_UNCACHED(addr) - BD_FD_SLOT0;

	return off % BD_FD_SLOT_SIZE == 0 && off / BD_FD_SLOT_SIZE < BD_FD_SLOTS;
}

#endif
