/*
 * Plugin configuration (sdk/docs/config.md). Modules declare options at file scope; the library
 * loads them from C:\FENYKL.CF0/CF1 at init and its saver task writes them back 2 s after the last
 * change. Linked into every plugin. CPU0 task context only, except
 * reading a value, which is a plain load from any context.
 */
#ifndef _FENYKL_CFG_H
#define _FENYKL_CFG_H

#include <stdbool.h>
#include <fenykl.h>

enum fenykl_cfg_type {
	FENYKL_CFG_BOOL,
	FENYKL_CFG_INT,
	FENYKL_CFG_ENUM,
};

/* A UI page. label: ASCII, at most 15 characters. */
struct fenykl_cfg_group {
	const char *label;
	uint8_t order;
};

struct fenykl_cfg_opt;
/* Called after the store, with the previous value. CPU0 task context (the caller of set, or the
   saver task when the file is loaded late). */
typedef void (*fenykl_cfg_change_fn)(const struct fenykl_cfg_opt *o, int32_t old);

struct fenykl_cfg_opt {
	const char *name;		/* "main.dof.show": the identity in the file, never renamed */
	const char *label;		/* ASCII, camera font, at most 15 characters */
	const struct fenykl_cfg_group *group;
	int32_t *value;			/* the live value, a plain global of the module */
	int32_t def, min, max, step;	/* BOOL: 0..1; ENUM: 0..count-1 */
	const char *const *names;	/* ENUM: one name per value, stored by name */
	fenykl_cfg_change_fn on_change;	/* optional */
	uint8_t type;			/* enum fenykl_cfg_type */
	uint8_t flags;			/* reserved (UI levels), 0 */
};

#define FENYKL_CFG_NAME_MAX 23u
#define FENYKL_CFG_LABEL_MAX 15u
#define FENYKL_CFG_OPTS_MAX 128u
#define FENYKL_CFG_BUF 4096u		/* file text, the largest file read or written */
#define FENYKL_CFG_RAW 1024u		/* lines carried verbatim (unknown names, invalid values) */
#define FENYKL_CFG_SAVE_MS 2000u	/* the saver writes this long after the last change */

#define FENYKL_CFG_SLOT0 "C:\\FENYKL.CF0"
#define FENYKL_CFG_SLOT1 "C:\\FENYKL.CF1"
#define FENYKL_CFG_IMPORT "C:\\FENYKL.CFG"

#define __FENYKL_CFG_SECTION __attribute__((section(".fenykl_cfg"), used, aligned(4), no_reorder))
#define __FENYKL_CFG_COUNT(a) (sizeof(a) / sizeof((a)[0]))

/*
 * Declarations, file scope. `sym` becomes `int32_t sym` (the value) and `fenykl_cfg_<sym>` (the
 * descriptor, in .fenykl_cfg). Trailing arguments are extra designated initializers, e.g.
 * `.on_change = fn`.
 */
#define FENYKL_CFG_GROUP(sym, lbl, ord) \
	const struct fenykl_cfg_group sym = { .label = (lbl), .order = (ord) }

#define __FENYKL_CFG_OPT(sym, nm, lbl, grp, typ, d, lo, hi, st, ...)			\
	int32_t sym = (d);								\
	const struct fenykl_cfg_opt fenykl_cfg_##sym __FENYKL_CFG_SECTION = {		\
		.name = (nm), .label = (lbl), .group = (grp), .value = &sym, .def = (d),	\
		.min = (lo), .max = (hi), .step = (st), .type = (typ) __VA_OPT__(, )	\
		__VA_ARGS__ }

#define FENYKL_CFG_BOOL(sym, name, label, group, def, ...) \
	__FENYKL_CFG_OPT(sym, name, label, group, FENYKL_CFG_BOOL, def, 0, 1, 1, __VA_ARGS__)
#define FENYKL_CFG_INT(sym, name, label, group, def, min, max, step, ...) \
	__FENYKL_CFG_OPT(sym, name, label, group, FENYKL_CFG_INT, def, min, max, step, __VA_ARGS__)
/* names: a `static const char *const x[]` array, one name per value (no spaces, no '='). */
#define FENYKL_CFG_ENUM(sym, name, label, group, def, nms, ...)				\
	__FENYKL_CFG_OPT(sym, name, label, group, FENYKL_CFG_ENUM, def, 0,		\
			 (int32_t)__FENYKL_CFG_COUNT(nms) - 1, 1, .names = (nms) __VA_OPT__(, ) \
			 __VA_ARGS__)

#define FENYKL_CFG_ENOFILE	(-1)	/* no valid file: defaults (not an error) */
#define FENYKL_CFG_ENOCARD	(-2)	/* card gate closed: nothing touched, retried later */
#define FENYKL_CFG_EINVAL	(-3)	/* value out of range, or a bad option */
#define FENYKL_CFG_EIO		(-4)	/* remove, open, write or close failed (logged) */
#define FENYKL_CFG_ENOSPC	(-5)	/* the text or the carried lines do not fit: nothing written */
#define FENYKL_CFG_EDECL	(-6)	/* bad declarations (logged): nothing is loaded or saved */
#define FENYKL_CFG_ECONTEXT	(-7)	/* called on CPU1 */
#define FENYKL_CFG_EBUSY	(-8)	/* another save runs, or init was already done */
#define FENYKL_CFG_ETASK	(-9)	/* the saver task could not be created */

/*
 * Checks the declarations, loads the newer valid slot and applies it, then applies and schedules
 * the deletion of C:\FENYKL.CFG. Values that change call on_change. CPU0 task, once, before hooks
 * that read options are installed. 0, FENYKL_CFG_ENOFILE, FENYKL_CFG_ENOCARD (defaults now, the
 * saver task loads at the first open gate; options set before that keep their value), or <0.
 */
int fenykl_cfg_init(void);
/* Creates the saver task "FYKCFG" (priority FENYKL_LIB_TASK_PRIO: E-PL3 12, E-M10 II 55; 4 KiB stack). */
int fenykl_cfg_task_start(void);
/*
 * Validates (range, enum index), stores, marks dirty and calls on_change if the value changed.
 * 0, FENYKL_CFG_EINVAL or FENYKL_CFG_ECONTEXT. CPU0 task context only: never from CPU1 (refused
 * and logged) nor from a hook (not detectable: hooks run in other tasks or interrupts).
 */
int fenykl_cfg_set(const struct fenykl_cfg_opt *o, int32_t v);
int fenykl_cfg_reset(const struct fenykl_cfg_opt *o);
/* The value as the file shows it: "on"/"off", the enum name, decimal. n >= 12. */
const char *fenykl_cfg_str(const struct fenykl_cfg_opt *o, char *buf, unsigned int n);
/* Writes now if dirty: 0 or <0 (FENYKL_CFG_ENOCARD, _EIO, _ENOSPC, _EBUSY, _EDECL). */
int fenykl_cfg_save(void);
bool fenykl_cfg_dirty(void);

/* Iteration in link order. */
extern const struct fenykl_cfg_opt fenykl_cfg_start[], fenykl_cfg_end[];
#define fenykl_cfg_for_each(o) for ((o) = fenykl_cfg_start; (o) < fenykl_cfg_end; (o)++)

enum fenykl_cfg_slot_state {
	FENYKL_CFG_SLOT_MISSING,	/* no file */
	FENYKL_CFG_SLOT_VALID,
	FENYKL_CFG_SLOT_TOO_LONG,	/* FENYKL_CFG_BUF bytes or more */
	FENYKL_CFG_SLOT_BAD_HEADER,
	FENYKL_CFG_SLOT_BAD_CRC,	/* no CRC line at the end, or a wrong CRC: torn or edited */
};

struct fenykl_cfg_stats {
	int32_t slot;			/* slot of the current file, -1 if none */
	uint32_t seq;			/* its sequence number */
	uint8_t slot_state[2];		/* enum fenykl_cfg_slot_state, at the last load */
	uint32_t slot_seq[2];
	uint32_t applied;		/* lines applied at the last load (slot and import) */
	uint32_t invalid;		/* known names with a bad value: default used, line carried */
	uint32_t unknown;		/* unknown names: carried */
	uint32_t duplicates;		/* names seen again in the same file: the last wins */
	uint32_t malformed;		/* lines without '=' or with an empty name: dropped */
	uint32_t raw_bytes;		/* carried text */
	bool raw_overflow;		/* carried lines did not fit FENYKL_CFG_RAW: saving refused */
	bool imported;			/* C:\FENYKL.CFG was applied */
	bool import_pending;		/* ... and is not deleted yet */
	uint32_t saves;			/* completed saves */
	uint32_t save_fail;
	int32_t last_remove;		/* rpc_fremove result of the last save */
	uint32_t last_bytes;		/* bytes of the last completed save */
	uint32_t gate_holds;		/* saves or loads held by a closed card gate */
};

void fenykl_cfg_get_stats(struct fenykl_cfg_stats *out);

#endif /* _FENYKL_CFG_H */
