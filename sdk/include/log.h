/*
 * Shared plugin logging (sdk/docs/logging.md). Producers store 32-byte binary records in a ring
 * per core; the flusher, a CPU0 task, formats them with fw_sprintf and appends them to
 * C:\FENYKL.LOG. Linked into every plugin.
 */
#ifndef _FENYKL_LOG_H
#define _FENYKL_LOG_H

#include <stdbool.h>
#include <fenykl.h>

/* Levels. OFF disables a module; TRACE is for per-frame chatter. */
enum fenykl_log_level {
	FENYKL_LOG_OFF,
	FENYKL_LOG_ERROR,
	FENYKL_LOG_WARN,
	FENYKL_LOG_INFO,
	FENYKL_LOG_DEBUG,
	FENYKL_LOG_TRACE,
};

/*
 * A module: a name of at most 7 characters and its runtime level. One per feature, file scope.
 * CPU1 reads the level through its own cache: a change from CPU0 may reach it late.
 */
struct fenykl_log_module {
	const char *name;
	volatile uint8_t level;
};

#define FENYKL_LOG_MODULE(sym, name, level) struct fenykl_log_module sym = { name, level }

/*
 * Compile-time ceiling, per translation unit: define before including log.h. Calls above it
 * compile to nothing (the arguments are not evaluated).
 */
#ifndef FENYKL_LOG_COMPILE_LEVEL
#define FENYKL_LOG_COMPILE_LEVEL FENYKL_LOG_DEBUG
#endif

#define __FENYKL_LOG_NARGS_PICK(a, b, c, d, e, f, n, ...) n
#define __FENYKL_LOG_NARGS(...) \
	__FENYKL_LOG_NARGS_PICK(__VA_ARGS__ __VA_OPT__(, ) 6, 5, 4, 3, 2, 1, 0)
#define __FENYKL_LOG_ARGS4(a, b, c, d, ...) \
	(uint32_t)(a), (uint32_t)(b), (uint32_t)(c), (uint32_t)(d)
/* The arguments padded with zeroes to four, as uint32_t. */
#define __FENYKL_LOG_ARGS(...) __FENYKL_LOG_ARGS4(__VA_ARGS__ __VA_OPT__(, ) 0, 0, 0, 0)

/*
 * The call sites. fmt is a string literal (a fw_sprintf format, at most 4 conversions out of
 * d i u x X c s p, `%s` only for strings that are still valid at flush time); up to 4 32-bit
 * arguments. Any context, either core, never blocks.
 */
#define fenykl_log(mod, lvl, fmt, ...)							\
	do {										\
		_Static_assert(__FENYKL_LOG_NARGS(__VA_ARGS__) <= 4,			\
			       "at most 4 log arguments");				\
		if ((lvl) <= FENYKL_LOG_COMPILE_LEVEL && (lvl) <= (mod)->level)		\
			fenykl_log_rec(mod, lvl, fmt, __FENYKL_LOG_ARGS(__VA_ARGS__));	\
	} while (0)

#define fenykl_log_err(mod, fmt, ...) \
	fenykl_log(mod, FENYKL_LOG_ERROR, fmt __VA_OPT__(, ) __VA_ARGS__)
#define fenykl_log_warn(mod, fmt, ...) \
	fenykl_log(mod, FENYKL_LOG_WARN, fmt __VA_OPT__(, ) __VA_ARGS__)
#define fenykl_log_info(mod, fmt, ...) \
	fenykl_log(mod, FENYKL_LOG_INFO, fmt __VA_OPT__(, ) __VA_ARGS__)
#define fenykl_log_dbg(mod, fmt, ...) \
	fenykl_log(mod, FENYKL_LOG_DEBUG, fmt __VA_OPT__(, ) __VA_ARGS__)
#define fenykl_log_trace(mod, fmt, ...) \
	fenykl_log(mod, FENYKL_LOG_TRACE, fmt __VA_OPT__(, ) __VA_ARGS__)

/* The longest fenykl_log_text message; the rest is cut and the line marked. */
#define FENYKL_LOG_TEXT_MAX 255u

/*
 * Underlying writers (any context, either core, never block). fenykl_log_rec() stores one
 * record; fenykl_log_text() copies `len` bytes of text, 20 per record.
 */
void fenykl_log_rec(const struct fenykl_log_module *mod, unsigned int level, const char *fmt,
		    uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3);
void fenykl_log_text(const struct fenykl_log_module *mod, unsigned int level, const char *text,
		     unsigned int len);

/* The file and its size cap: at init, a file larger than the cap is deleted and started fresh. */
#define FENYKL_LOG_FILE "C:\\FENYKL.LOG"
#define FENYKL_LOG_SIZE_CAP (4u << 20)
/* A flush (open .. close) longer than this is logged and counted. */
#define FENYKL_LOG_SLOW_US 500000u

/* CPU0 task context only (file RPC). Zero fields take the defaults. */
struct fenykl_log_config {
	unsigned int flush_ms;	/* flusher period, default 2000 */
	bool tee;		/* the flusher task copies the console capture into the log */
};

#define FENYKL_LOG_EOPEN	(-1)	/* the file could not be opened: records stay queued */
#define FENYKL_LOG_EAPPEND	(-2)	/* "a" fails on an existing file: C:\FENYKL.ERR says so */
#define FENYKL_LOG_EBUSY	(-3)	/* another flush is running, or init was already done */
#define FENYKL_LOG_ETASK	(-4)	/* the flusher task could not be created */
#define FENYKL_LOG_ENOCARD	(-5)	/* fenykl_card_io_allowed() is false: records stay queued */

/*
 * Measures the file, applies the size cap and appends the boot header. Call once, before any
 * record is wanted: records before it are only counted. 0 or <0. FENYKL_LOG_ENOCARD: the card
 * gate is closed; records are taken and the card work is done by the first flush after it opens.
 */
int fenykl_log_init(const struct fenykl_log_config *cfg);
/*
 * Drains both rings into the file and closes it before it returns: 0, or <0 (records stay
 * queued). The flusher task calls it; a CPU0 task may call it on an error path to put what is
 * queued on the card at once. FENYKL_LOG_EBUSY: the flusher task is in a flush; sleep, call again.
 * FENYKL_LOG_ENOCARD: the card gate is closed (USB cable in or card not mounted); nothing touched.
 */
int fenykl_log_flush(void);
/*
 * Creates the flusher task "FYKLOG" (priority FENYKL_LIB_TASK_PRIO: E-PL3 12, E-M10 II 55; 4 KiB
 * stack): flushes every flush_ms, or early when a ring is half full, and polls the console tee every
 * 20 ms.
 */
int fenykl_log_task_start(void);
void fenykl_log_set_level(struct fenykl_log_module *mod, unsigned int level);

struct fenykl_log_stats {
	uint32_t produced[2];	/* records attempted per ring (ring 1 counts drops too) */
	uint32_t written[2];	/* records formatted into the file per ring */
	uint32_t lost[2];	/* sequence gaps the flusher saw per ring ([lost N] lines); a ring 1
				   gap shows only with the next ring 1 record */
	uint32_t queued[2];	/* records waiting in each ring */
	uint32_t overwritten0;	/* ring 0 records evicted by newer ones */
	uint32_t dropped1;	/* ring 1 records refused because it was full */
	uint32_t early;		/* CPU0 records before fenykl_log_init (not stored) */
	uint32_t bad;		/* records printed as <bad record>: bad pointer, format or length */
	uint32_t truncated;	/* fenykl_log_text calls cut at FENYKL_LOG_TEXT_MAX */
	uint32_t stamp_jumps;	/* consecutive records of a ring more than 2^29 us apart */
	uint32_t flushes;	/* flushes that opened the file */
	uint32_t open_fail;
	uint32_t short_write;
	uint32_t flush_max_us;	/* slowest flush (open .. close) */
	uint32_t flush_slow;	/* flushes > FENYKL_LOG_SLOW_US */
	uint32_t flush_busy;	/* 1 while a flush is between open and close */
	uint32_t flush_since;	/* timer_now when that flush opened the file */
	uint32_t gate_skipped;	/* flushes skipped while fenykl_card_io_allowed() was false */
	uint32_t gate_max_ms;	/* longest period the gate was closed, ms (flush period resolution) */
	uint32_t bytes;		/* bytes appended in this boot, the boot header included */
	/* bytes in the file before this boot's header (0 after a cap delete) */
	uint32_t file_size;
	uint32_t removed_size;	/* size of the file the cap deleted at init, else 0 */
	uint32_t boot_cause;	/* at init: E-PL3 boot cause byte 0x6044151A, E-M10 II power-on factor */
	uint32_t tee_bytes;
	uint32_t tee_records;	/* console lines logged */
	uint32_t tee_slots;	/* ring 0 slots they took (20 text bytes each) */
	uint32_t tee_overflow;
};

void fenykl_log_get_stats(struct fenykl_log_stats *out);

/*
 * FW_TIMER_NOW: free-running 30-bit counter at 1 MHz (counts down; this counts up), both
 * cores. FENYKL_LOG_ELAPSED is the wrap-safe difference.
 */
#define FENYKL_LOG_TIMER_MASK 0x3FFFFFFFu
#define FENYKL_LOG_TIMER_NOW() (FENYKL_LOG_TIMER_MASK - *(volatile uint32_t *)FW_TIMER_NOW)
#define FENYKL_LOG_ELAPSED(from, to) (((to) - (from)) & FENYKL_LOG_TIMER_MASK)

#endif /* _FENYKL_LOG_H */
