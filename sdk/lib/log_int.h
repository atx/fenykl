/*
 * Internals of the logging library shared by log_rec.c (both cores) and log.c (CPU0 flusher).
 */
#ifndef _FENYKL_LOG_INT_H
#define _FENYKL_LOG_INT_H

#include "log.h"

/*
 * One record, 32 bytes (sdk/docs/logging.md section 3):
 *   w[0] timer_now at the write (30 bits)
 *   w[1] meta: seq 0-15 | len 16-20 | core 21 | kind 22-23 | level 24-26 | truncated 27
 *   w[2] module pointer
 *   w[3..7] kind FMT: format pointer + 4 arguments; kind TEXT/CONT: 20 bytes of text
 */
#define FLOG_SLOT_WORDS 8u

struct flog_slot {
	uint32_t w[FLOG_SLOT_WORDS];
};

enum flog_kind {
	FLOG_KIND_FMT,
	FLOG_KIND_TEXT,
	FLOG_KIND_CONT,
};

#define FLOG_TEXT_PER_SLOT 20u
#define FLOG_SEQ_MASK 0xFFFFu

#define FLOG_META(seq, len, core, kind, level, trunc)					\
	(((seq) & FLOG_SEQ_MASK) | (uint32_t)(len) << 16 | (uint32_t)(core) << 21 |	\
	 (uint32_t)(kind) << 22 | (uint32_t)(level) << 24 | (uint32_t)(trunc) << 27)
#define FLOG_META_SEQ(m) ((m) & FLOG_SEQ_MASK)
#define FLOG_META_LEN(m) (((m) >> 16) & 0x1Fu)
#define FLOG_META_CORE(m) (((m) >> 21) & 1u)
#define FLOG_META_KIND(m) (((m) >> 22) & 3u)
#define FLOG_META_LEVEL(m) (((m) >> 24) & 7u)
#define FLOG_META_TRUNC(m) (((m) >> 27) & 1u)

/*
 * Ring 0: CPU0 producers, many of them, serialised by the CPU0 interrupt mask; keeps the newest
 * records. Part of flog, used through the cached alias: its producers and its only consumer all
 * run on CPU0. head and tail are free-running counters: a record's slot is
 * counter % FLOG_RING0_RECORDS, its seq the counter's low 16 bits.
 */
#define FLOG_RING0_RECORDS 512u

struct flog_ring0 {
	uint32_t head;		/* records written */
	uint32_t tail;		/* records taken by the flusher or evicted */
	uint32_t overwritten;	/* records evicted while still queued */
	struct flog_slot slot[FLOG_RING0_RECORDS];
};

/*
 * Ring 1: CPU1 producers (serialised by the CPU1 interrupt mask), single consumer on CPU0. Only
 * CPU1 writes head, seq and dropped, only CPU0 writes tail; FENYKL_SHARED, used through the
 * uncached alias on both cores. Full: the new record is dropped (its seq is used up, so the
 * flusher sees the gap). Plugins without cpu1_sources get no slots: any CPU1 record is dropped
 * and counted.
 */
#ifndef FENYKL_PLUGIN_CPU1
#error "FENYKL_PLUGIN_CPU1 comes from the build (library sources only)"
#elif FENYKL_PLUGIN_CPU1
#define FLOG_RING1_RECORDS 128u
#else
#define FLOG_RING1_RECORDS 0u
#endif

struct flog_ring1 {
	uint32_t head, seq, dropped, cpu1_pad[5];
	uint32_t tail, cpu0_pad[7];
	struct flog_slot slot[FLOG_RING1_RECORDS];
};

#define FLOG_RING1_SLOT(i) ((i) % (FLOG_RING1_RECORDS ? FLOG_RING1_RECORDS : 1u))

extern struct flog_ring1 fenykl_log_ring1;

static inline volatile struct flog_ring1 *flog_ring1(void)
{
	return FENYKL_SHARED_PTR(fenykl_log_ring1);
}

static inline uint32_t flog_irq_save(void)
{
	uint32_t psw = fw_psw_get();

	fw_irq_disable_all();
	return psw;
}

/* Records copied out of a ring per round: FLOG_BATCH + a text message's tail. */
#define FLOG_STAGE_RECORDS 48u
#define FLOG_BATCH 32u
#define FLOG_LINE_MAX 320u /* prefix + FENYKL_LOG_TEXT_MAX + CR LF */
#define FLOG_OUT_SIZE 0x1000u
#define FLOG_FMT_ARGS 4u
#define FLOG_SUBST_SIZE 12u /* "<%08x>" and its NUL */
#define FLOG_ERR_SIZE 128u
/* Firmware console capture: uart_putchar's boot capture buffer. */
#define FLOG_TEE_SIZE 767u

/*
 * Seconds and microseconds since a stream's init (no 64-bit division in the payload: libgcc's
 * brings debug sections the link refuses).
 */
struct flog_stamp {
	uint32_t s, us;
};

/* A raw timer_now and its extended stamp. */
struct flog_time {
	uint32_t raw;
	struct flog_stamp ext;
};

/*
 * Formatting state of one record stream. A record's stamp is the first one at or after `at` with
 * its raw stamp (sdk/docs/logging.md section 6.1).
 */
struct flog_stream {
	struct flog_time at;	/* the last record, or a later lower bound for the next one */
	uint32_t pos;		/* ring counter of the next record */
	uint32_t fresh;		/* ring head at the end of the last flush */
	uint32_t expect;	/* next seq, 16 bits */
	uint32_t ring;		/* 0 or 1 */
};

/* Ring slots a text message of len bytes takes (at least one). */
static inline unsigned int flog_text_slots(unsigned int len)
{
	return len ? (len + FLOG_TEXT_PER_SLOT - 1) / FLOG_TEXT_PER_SLOT : 1;
}

/* All mutable state of the library but ring 1; the flusher's part is CPU0 task context only. */
struct flog_state {
	/* Set by fenykl_log_init once the streams start; CPU0 records before only count early. */
	volatile bool ready;
	volatile uint32_t early;
	volatile struct flog_ring0 ring0;

	struct fenykl_log_config cfg;
	struct fenykl_log_stats st;
	bool busy;			/* a flush holds the file and the stage buffers */
	uint32_t last_flush;		/* timer_now at the start of the last flush */
	uint32_t pending_slow;		/* duration of a slow flush, logged by the next one */
	uint32_t t_init;		/* timer_now at fenykl_log_init */
	bool init_pending;		/* the card part of init waits for the first open gate */

	/* Card gate (fenykl_card_io_allowed), checked by every flush. */
	bool gate_closed;
	uint32_t gate_last;		/* timer_now at the last check while closed */
	uint32_t gate_us;		/* closed time not yet in gate_ms */
	uint32_t gate_ms;		/* closed time of this period */
	uint32_t gate_skips;		/* flushes skipped in this period */

	struct flog_time clock;		/* timer_now, extended by every flush */
	struct flog_time fresh_at;	/* records from stream[].fresh on are later (a second early) */
	struct flog_stream stream[2];	/* per ring */
	struct flog_slot stage[2][FLOG_STAGE_RECORDS];

	void *out_fp;
	unsigned int out_len;
	char out[FLOG_OUT_SIZE];
	char line[FLOG_LINE_MAX];
	char subst[FLOG_FMT_ARGS][FLOG_SUBST_SIZE];
	char err[FLOG_ERR_SIZE];

	bool tee_off;			/* the "capture is off" line was logged */
	unsigned int tee_len;
	uint8_t tee_copy[FLOG_TEE_SIZE];
	char tee_line[FENYKL_LOG_TEXT_MAX];
};

extern struct flog_state flog;

/*
 * Copies what the firmware console wrote into its capture buffer since the last poll and
 * restarts the capture; complete lines become text records (log.c; the flusher task calls it
 * every 20 ms).
 */
void flog_tee_poll(void);

#endif /* _FENYKL_LOG_INT_H */
