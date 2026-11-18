/* The memory sampler's control block (docs/devmon.md, Memory sampler). tp_main's arg is its
   uncached address; the ring of records follows it at SMP_CTL_SIZE. py/fenykl/sampler.py mirrors
   this layout. All fields are little-endian. */
#ifndef FENYKL_SAMPLER_H
#define FENYKL_SAMPLER_H

#include <stddef.h>
#include <stdint.h>

#define SMP_MAGIC 0x4D534B46u		/* "FKSM" */
#define SMP_WORDS_MAX 32u
#define SMP_CTL_SIZE 0x280u
#define SMP_DURATION_MAX_MS 3600000u
/* The job buffer after the exec region's 16 slots (uncached): the ring ends before it. */
#define SMP_RING_END 0xA100A800u

#define SMP_PERIODIC 0u
#define SMP_CHANGE 1u

/* state: 0 before the run (the host), then RUNNING, then DONE or a negative error. */
#define SMP_RUNNING 1
#define SMP_DONE 2
#define SMP_EMAGIC (-1)		/* no control block at arg */
#define SMP_EARGS (-2)		/* mode, word count, width, flags, ring or duration */
#define SMP_EALIGN (-3)		/* an address not aligned to its width */
#define SMP_EDSP (-4)		/* an address in DSP local memory */
#define SMP_EPERIOD (-5)	/* period_us below loop_us, the measured cost of one pass */
#define SMP_EPLACE (-6)		/* the block or the ring outside the slots after the program */

/* word flags */
#define SMP_DEREF 1u		/* addr holds a pointer: read the u32 there, then pointer + offset */
#define SMP_TRIGGER 2u		/* SMP_CHANGE: a change of (value & mask) makes a record */

struct smp_word {
	uint32_t addr;
	uint32_t offset;	/* SMP_DEREF */
	uint32_t mask;		/* SMP_TRIGGER */
	uint16_t width;		/* 8, 16 or 32: of the value, the pointer is always 32 */
	uint16_t flags;
};

struct smp_ctl {
	/* the host, before the run */
	uint32_t magic;
	uint32_t mode;
	uint32_t period_us;
	uint32_t duration_ms;	/* 1..SMP_DURATION_MAX_MS: the program always ends */
	uint32_t nwords;
	uint32_t cap;		/* records in the ring */
	/* the host, during the run (uncached WRITE) */
	uint32_t stop;		/* nonzero: end the run */
	uint32_t tail;		/* records the host has taken */
	/* the program */
	int32_t state;
	uint32_t head;		/* records written */
	uint32_t drops;		/* records lost to a full ring */
	uint32_t overruns;	/* periods missed: the pass started a period or more late */
	uint32_t loop_us;	/* cost of one pass, measured before the run */
	uint32_t loop_max_us;	/* longest pass in the run */
	uint32_t t_start, t_end;
	uint32_t invalid;	/* SMP_DEREF reads not made: the pointer led outside RAM */
	uint32_t reserved[3];
	struct smp_word word[SMP_WORDS_MAX];
	uint32_t pad[(SMP_CTL_SIZE - 0x50 - SMP_WORDS_MAX * 16) / 4];
};

_Static_assert(sizeof(struct smp_ctl) == SMP_CTL_SIZE, "control block size");
/* The offsets py/fenykl/sampler.py uses. */
_Static_assert(offsetof(struct smp_ctl, cap) == 0x14, "cap");
_Static_assert(offsetof(struct smp_ctl, stop) == 0x18, "stop");
_Static_assert(offsetof(struct smp_ctl, tail) == 0x1C, "tail");
_Static_assert(offsetof(struct smp_ctl, state) == 0x20, "state");
_Static_assert(offsetof(struct smp_ctl, invalid) == 0x40, "invalid");
_Static_assert(offsetof(struct smp_ctl, word) == 0x50, "words");
_Static_assert(sizeof(struct smp_word) == 16, "word size");

/* A record: its index since the start (written last, so a record whose seq matches is whole),
   the microseconds since t_start, the passes lost just before it (overruns and drops), the
   words not read (bit i: word i's pointer led outside RAM, its slot holds the pointer), then one
   u32 per word. */
#define SMP_REC_SEQ 0u
#define SMP_REC_T 1u
#define SMP_REC_LOST 2u
#define SMP_REC_BAD 3u
#define SMP_REC_HEAD 4u

#endif
