/*
 * main: the E-M10 II R/B swap, plugin task side (body.h wb_base). No hook: while the option is on,
 * each colour-matrix base's registry word points at a copy with rows 0 and 2 exchanged, so every
 * live-view and still matrix the firmware computes from it is swapped; off puts the stock words
 * back. All registry work is done here in the tick (main_rbswap_base.c), which re-asserts the
 * copies against a rebuilt or moved registry. Logs the WB setting, each base's registry word and
 * stock coefficients (on the card before its first store), and the live-view matrix after each
 * toggle and every 10 s while on.
 */
#include "main.h"
#include "main_rbswap.h"

#define MAIN_RBSWAP_WB_CMX_FIRST (1000u / MAIN_TICK_MS)
#define MAIN_RBSWAP_WB_CMX_EVERY (10000u / MAIN_TICK_MS)
#define MAIN_RBSWAP_WB_FLUSH_TRIES 20u	/* ticks a busy flusher may delay a first store */
#define MAIN_RBSWAP_WB_MODE_NONE (-2)	/* below fenykl_wb_mode's range: log the first read */

struct main_rbswap_wb_state {
	bool started;
	bool on;		/* the option as last applied */
	bool flush_due;		/* a stock was logged and is not on the card yet */
	bool refused;		/* a refused or waiting base was logged since the enable */
	bool logged[FENYKL_WB_BASE_N];	/* the base's stock was logged since the enable */
	unsigned int flush_tries;
	int mode;		/* WB setting last logged */
	uint32_t ticks;
	uint32_t cmx_at;	/* tick of the next live-view matrix log, 0 = none */
	uint32_t stores, foreign, moves, lost;
};

static struct main_rbswap_wb_state main_rbswap_wb_st = { .mode = MAIN_RBSWAP_WB_MODE_NONE };

static void main_rbswap_wb_log_mat(const volatile int16_t *m)
{
	for (unsigned int r = 0; r < 3; r++)
		fenykl_log_info(&main_rbswap_log, "  r%u %d %d %d", r, m[3 * r], m[3 * r + 1],
				m[3 * r + 2]);
}

static void main_rbswap_wb_log_mode(void)
{
	int mode = fenykl_wb_mode();

	if (mode == main_rbswap_wb_st.mode)
		return;
	main_rbswap_wb_st.mode = mode;
	fenykl_log_info(&main_rbswap_log, "WB mode %d", mode);
}

static void main_rbswap_wb_log_cmx(void)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;
	int16_t m[FENYKL_WB_MAT_N];

	fenykl_wb_lv_matrix(m);
	fenykl_log_info(&main_rbswap_log, "live-view matrix (R/B swap %s)", st->on ? "on" : "off");
	main_rbswap_wb_log_mat(m);
	st->cmx_at = st->on ? st->ticks + MAIN_RBSWAP_WB_CMX_EVERY : 0;
}

/* Base i's registry word and stock coefficients, once per enable; false while it has none. */
static bool main_rbswap_wb_log_stock(unsigned int i)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;
	uint32_t slot = fenykl_wb_base_slot(i), obj = fenykl_rd32(slot);

	if (st->logged[i])
		return true;
	if (!fenykl_dram_ptr(obj, 2)) {
		if (!st->refused)
			fenykl_log_err(&main_rbswap_log, "base %u slot %08x obj %08x: waiting", i,
				       slot, obj);
		st->refused = true;
		return false;
	}
	fenykl_log_info(&main_rbswap_log, "base %u slot %08x head %08x stock %08x", i, slot,
			fenykl_wb_base_head(slot), obj);
	main_rbswap_wb_log_mat(FENYKL_UNCACHED(obj));
	st->logged[i] = true;
	st->flush_due = true;
	return true;
}

static void main_rbswap_wb_log_refused(unsigned int i, int r, uint32_t slot, uint32_t v)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;

	if (r == MAIN_RBSWAP_CHANGED) {
		fenykl_log_warn(&main_rbswap_log, "base %u slot %08x changed during the store", i, slot);
		return;
	}
	if (st->refused)
		return;
	st->refused = true;
	if (r == MAIN_RBSWAP_ENTRY)
		fenykl_log_err(&main_rbswap_log, "base %u slot %08x entry %08x not the base's", i, slot,
			       fenykl_wb_base_head(slot));
	else
		fenykl_log_err(&main_rbswap_log, "base %u slot %08x obj %08x invalid", i, slot, v);
}

/* r: main_rbswap_base_assert's result for base i, whose slot held v before. */
static void main_rbswap_wb_log_assert(unsigned int i, int r, uint32_t slot, uint32_t v)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;
	const struct main_rbswap_base *b = main_rbswap_base_get(i);

	if (r < 0) {
		main_rbswap_wb_log_refused(i, r, slot, v);
		return;
	}
	st->stores++;
	if (r == MAIN_RBSWAP_FOREIGN) {
		st->foreign++;
		fenykl_log_warn(&main_rbswap_log, "base %u slot %08x held %08x: copy rebuilt", i, slot,
				v);
	} else if (r == MAIN_RBSWAP_MOVED) {
		st->moves++;
		fenykl_log_warn(&main_rbswap_log, "base %u moved to slot %08x (held %08x)", i, slot, v);
	}
	fenykl_log_info(&main_rbswap_log, "base %u stored: slot %08x stock %08x copy %08x", i,
			b->slot, b->stock, (uint32_t)main_rbswap_base_copy(i));
	main_rbswap_wb_log_mat(main_rbswap_base_copy(i));
}

/* Each base whose stock is logged and on the card; the others wait for a later tick. */
static void main_rbswap_wb_assert(void)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;
	bool ready[FENYKL_WB_BASE_N];
	unsigned int i;
	int r;

	for (i = 0; i < FENYKL_WB_BASE_N; i++)
		ready[i] = main_rbswap_wb_log_stock(i);
	if (st->flush_due) {
		r = fenykl_log_flush();
		if (r == FENYKL_LOG_EBUSY && ++st->flush_tries < MAIN_RBSWAP_WB_FLUSH_TRIES)
			return;
		st->flush_due = false;
		st->flush_tries = 0;
		if (r)
			fenykl_log_warn(&main_rbswap_log, "log flush %d before the store", r);
	}
	for (i = 0; i < FENYKL_WB_BASE_N; i++) {
		uint32_t slot = fenykl_wb_base_slot(i), v = fenykl_rd32(slot);

		if (!ready[i])
			continue;
		r = main_rbswap_base_assert(i);
		if (r != MAIN_RBSWAP_KEPT)
			main_rbswap_wb_log_assert(i, r, slot, v);
	}
}

static void main_rbswap_wb_enable(void)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;

	for (unsigned int i = 0; i < FENYKL_WB_BASE_N; i++)
		st->logged[i] = false;
	st->on = true;
	st->refused = false;
	st->flush_due = false;
	st->flush_tries = 0;
	st->cmx_at = st->ticks + MAIN_RBSWAP_WB_CMX_FIRST;
}

static void main_rbswap_wb_disable(void)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;
	unsigned int restored = 0, left = 0;

	for (unsigned int i = 0; i < FENYKL_WB_BASE_N; i++) {
		uint32_t slot = main_rbswap_base_get(i)->slot;
		uint32_t now = fenykl_wb_base_slot(i);

		if (main_rbswap_base_restore(i)) {
			restored += slot != 0;
			continue;
		}
		left++;
		fenykl_log_warn(&main_rbswap_log, "base %u slot %08x, lookup %08x holds %08x: left", i,
				slot, now, fenykl_rd32(now));
	}
	st->lost += left;
	st->on = false;
	st->cmx_at = st->ticks + MAIN_RBSWAP_WB_CMX_FIRST;
	fenykl_log_info(&main_rbswap_log, "R/B swap off: %u bases restored, %u left", restored,
			left);
}

void main_rbswap_start(void)
{
	main_rbswap_wb_st.started = true;
	fenykl_log_info(&main_rbswap_log, "R/B swap %s", main_rbswap_on ? "on" : "off");
	main_rbswap_wb_log_mode();
}

/* Option off and never on: no registry write. */
void main_rbswap_tick(void)
{
	struct main_rbswap_wb_state *st = &main_rbswap_wb_st;
	bool want = *(volatile int32_t *)&main_rbswap_on != 0;	/* set by other tasks */

	if (!st->started)
		return;
	st->ticks++;
	main_rbswap_wb_log_mode();
	if (want && !st->on)
		main_rbswap_wb_enable();
	else if (!want && st->on)
		main_rbswap_wb_disable();
	if (st->on)
		main_rbswap_wb_assert();
	if (st->cmx_at && st->ticks >= st->cmx_at)
		main_rbswap_wb_log_cmx();
}

void main_rbswap_status(void)
{
	const struct main_rbswap_wb_state *st = &main_rbswap_wb_st;

	fenykl_log_info(&main_rbswap_log, "rbswap %s: stores %u moves %u", st->on ? "on" : "off",
			st->stores, st->moves);
	fenykl_log_info(&main_rbswap_log, "  foreign %u lost %u", st->foreign, st->lost);
}
