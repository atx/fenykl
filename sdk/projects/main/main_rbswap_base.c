/*
 * main: the E-M10 II R/B swap's registry work on the colour-matrix bases (body.h wb_base), free of
 * logging and options so that the simulator test (tests/sim/main/) runs it alone. A registry word
 * is changed only by a single aligned 32-bit store, and only while it still holds the value checked
 * (interrupts masked); the stock object is only read; a copy is never written while a registry word
 * points at it.
 */
#include "main_rbswap.h"

struct main_rbswap_base_mat {
	int16_t m[FENYKL_WB_MAT_N];
} __attribute__((aligned(4)));

struct main_rbswap_base_state {
	struct main_rbswap_base base[FENYKL_WB_BASE_N];
};

static struct main_rbswap_base_state main_rbswap_base_st;

/* Read by the firmware on every core through the registry: a .shared line of its own, so no cached
   data shares it, used through the uncached alias only. */
static FENYKL_SHARED struct main_rbswap_base_mat main_rbswap_base_mats[FENYKL_WB_BASE_N];

static volatile int16_t *main_rbswap_base_mat(unsigned int i)
{
	return FENYKL_UNCACHED(main_rbswap_base_mats[i].m);
}

const volatile int16_t *main_rbswap_base_copy(unsigned int i)
{
	return main_rbswap_base_mat(i);
}

const struct main_rbswap_base *main_rbswap_base_get(unsigned int i)
{
	return &main_rbswap_base_st.base[i];
}

/* Stores new in slot if it still holds old. */
static bool main_rbswap_base_cas(uint32_t slot, uint32_t old, uint32_t new)
{
	uint32_t psw = fenykl_irq_save();
	bool same = fenykl_rd32(slot) == old;

	if (same)
		fenykl_wr32(slot, new);
	fenykl_irq_restore(psw);
	return same;
}

/* A stock base: a DRAM object of s16 outside our copies (either alias). */
static bool main_rbswap_base_valid(uint32_t stock)
{
	uint32_t a = (uint32_t)FENYKL_UNCACHED(stock);
	uint32_t mats = (uint32_t)FENYKL_UNCACHED(main_rbswap_base_mats);

	return fenykl_dram_ptr(stock, 2) && a - mats >= sizeof(main_rbswap_base_mats);
}

/* The stock base, read now through the uncached alias, with rows 0 and 2 exchanged. */
static void main_rbswap_base_swapped(int16_t *s, uint32_t stock)
{
	const volatile int16_t *b = FENYKL_UNCACHED(stock);
	int16_t m[FENYKL_WB_MAT_N];

	for (unsigned int i = 0; i < FENYKL_WB_MAT_N; i++)
		m[i] = b[i];
	main_rbswap_pxm(s, m);
}

static bool main_rbswap_base_same(const volatile int16_t *copy, uint32_t stock)
{
	int16_t s[FENYKL_WB_MAT_N];

	main_rbswap_base_swapped(s, stock);
	for (unsigned int i = 0; i < FENYKL_WB_MAT_N; i++)
		if (copy[i] != s[i])
			return false;
	return true;
}

/* Writes the swapped stock base into copy; false if it does not read back (stock changed). */
static bool main_rbswap_base_fill(volatile int16_t *copy, uint32_t stock)
{
	int16_t s[FENYKL_WB_MAT_N];

	main_rbswap_base_swapped(s, stock);
	for (unsigned int i = 0; i < FENYKL_WB_MAT_N; i++)
		copy[i] = s[i];
	return main_rbswap_base_same(copy, stock);
}

/* Puts the stock word back into slot if it holds the copy; true if slot holds the stock after. */
static bool main_rbswap_base_put_back(uint32_t slot, uint32_t c, uint32_t stock)
{
	return main_rbswap_base_cas(slot, c, stock) || fenykl_rd32(slot) == stock;
}

/*
 * Both the recorded slot and the one the lookup gives now: an override entry cloned from the
 * table word while it held the copy points at the copy without being recorded.
 */
/* The recorded slot only. */
static bool main_rbswap_base_restore_slot(unsigned int i)
{
	struct main_rbswap_base *b = &main_rbswap_base_st.base[i];
	uint32_t slot = b->slot;

	b->slot = 0;
	if (!slot)
		return true;
	return main_rbswap_base_put_back(slot, (uint32_t)main_rbswap_base_mat(i), b->stock);
}

bool main_rbswap_base_restore(unsigned int i)
{
	struct main_rbswap_base *b = &main_rbswap_base_st.base[i];
	uint32_t c = (uint32_t)main_rbswap_base_mat(i);
	uint32_t slot = b->slot, now = fenykl_wb_base_slot(i);
	bool ok = main_rbswap_base_restore_slot(i);

	if (b->stock && now != slot && fenykl_rd32(now) == c)
		ok = main_rbswap_base_put_back(now, c, b->stock) && ok;
	return ok;
}

/*
 * slot holds the copy c already: the same slot with a changed stock gets its stock word back first
 * (KEPT: go on with v, now the stock); a moved slot (an override entry cloned from the table word)
 * is adopted.
 */
static int main_rbswap_base_held(unsigned int i, uint32_t slot, uint32_t stock, bool moved,
				 uint32_t *v)
{
	struct main_rbswap_base *b = &main_rbswap_base_st.base[i];
	volatile int16_t *copy = main_rbswap_base_mat(i);

	if (!stock)
		return MAIN_RBSWAP_INVALID;
	if (moved && main_rbswap_base_same(copy, stock)) {
		b->slot = slot;
		b->stock = stock;
		return MAIN_RBSWAP_MOVED;
	}
	if (!main_rbswap_base_cas(slot, (uint32_t)copy, stock))
		return MAIN_RBSWAP_CHANGED;
	*v = stock;
	return MAIN_RBSWAP_KEPT;
}

int main_rbswap_base_assert(unsigned int i)
{
	struct main_rbswap_base *b = &main_rbswap_base_st.base[i];
	volatile int16_t *copy = main_rbswap_base_mat(i);
	uint32_t slot = fenykl_wb_base_slot(i), c = (uint32_t)copy, v = fenykl_rd32(slot);
	uint32_t had = b->slot, stock = b->stock;	/* last known: recovers an unrecorded copy */
	bool moved = had && had != slot;
	bool foreign = had && !moved && v != c && v != stock;
	int r;

	if (had == slot && v == c && main_rbswap_base_same(copy, stock))
		return MAIN_RBSWAP_KEPT;
	if (!fenykl_wb_base_entry_ok(i, slot))
		return MAIN_RBSWAP_ENTRY;
	if (moved)
		main_rbswap_base_restore_slot(i);
	b->slot = 0;
	if (v == c) {
		r = main_rbswap_base_held(i, slot, stock, moved, &v);
		if (r != MAIN_RBSWAP_KEPT)
			return r;
	}
	if (!main_rbswap_base_valid(v) || !main_rbswap_base_fill(copy, v))
		return MAIN_RBSWAP_INVALID;
	if (!main_rbswap_base_cas(slot, v, c))
		return MAIN_RBSWAP_CHANGED;
	b->slot = slot;
	b->stock = v;
	return moved ? MAIN_RBSWAP_MOVED : foreign ? MAIN_RBSWAP_FOREIGN : MAIN_RBSWAP_STORED;
}
