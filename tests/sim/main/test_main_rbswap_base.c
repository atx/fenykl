/*
 * The E-M10 II R/B swap's registry work (sdk/projects/main/main_rbswap_base.c) on a fake property
 * registry at the real data.prop_table / prop_override addresses, with the slots and entry checks
 * of the body (fenykl_wb_base_slot, fenykl_wb_base_entry_ok) and rbswap_fw.S for the interrupt
 * mask. Checks the first store (copy = rows 0 and 2 of the stock base exchanged, stock untouched,
 * stored under the mask), no store while the copy holds, re-assert after a registry rebuilt to the
 * same or to a new object, a stock changed in place, a slot moved to the override table (the old
 * one restored) also when the override entry holds the copy already, the refusals (null, odd,
 * outside DRAM, another alias or base's copy, a wrong entry head), the restore, also over a slot that
 * no longer holds the copy and over an unrecorded override entry holding it, and the recovery of a
 * copy left in a slot with nothing recorded. plugin_main sets sim_exit_code to 0 or the number of the first failed
 * check.
 */
#include <fenykl.h>

#include "../../../sdk/projects/main/main_rbswap.h"

#define CHECK(n, cond)					\
	do {						\
		if (!(cond) && !sim_fail)		\
			sim_fail = (n);			\
	} while (0)

#define SIM_HEAD (FENYKL_WB_MAT_N << 16 | 0xAA6Au)	/* base 0: prop 0xAA6A, 9 elements */
#define SIM_CACHED(a) ((a) - 0x40000000u)		/* cached alias of an 0xA... address */

struct sim_mat {
	int16_t m[FENYKL_WB_MAT_N];
} __attribute__((aligned(4)));

uint32_t sim_exit_code = 99;
uint32_t sim_masked, sim_psw;	/* rbswap_fw.S */
static uint32_t sim_fail;

static struct sim_mat sim_stock = {
	{ 0x14A, -0x2E, -0x1C, -0x3A, 0x160, -0x26, 0x05, -0x50, 0x14B }
};
static const int16_t sim_swapped[FENYKL_WB_MAT_N] = { 0x05, -0x50, 0x14B, -0x3A, 0x160, -0x26,
						       0x14A, -0x2E, -0x1C };
static struct sim_mat sim_other = { { 0x100, 1, 2, 3, 0x100, 4, 5, 6, 0x100 } };
static const int16_t sim_other_swapped[FENYKL_WB_MAT_N] = { 5, 6, 0x100, 3, 0x100, 4, 0x100, 1, 2 };
static struct sim_mat sim_override = { { 0x80, 0, 7, 0, 0x80, 0, 9, 0, 0x80 } };

static uint32_t sim_copy(void)
{
	return (uint32_t)main_rbswap_base_copy(0);
}

static bool sim_same(const volatile int16_t *a, const int16_t *b)
{
	for (unsigned int i = 0; i < FENYKL_WB_MAT_N; i++)
		if (a[i] != b[i])
			return false;
	return true;
}

static uint32_t sim_other_u(void)
{
	return (uint32_t)FENYKL_UNCACHED(sim_other.m);
}

static void sim_first_store(uint32_t slot)
{
	const struct main_rbswap_base *b = main_rbswap_base_get(0);

	fenykl_wr32(slot, (uint32_t)sim_stock.m);
	CHECK(1, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	CHECK(2, fenykl_rd32(slot) == sim_copy() && sim_copy() >= 0xA0000000u);
	CHECK(3, sim_same(main_rbswap_base_copy(0), sim_swapped));
	CHECK(4, b->slot == slot && b->stock == (uint32_t)sim_stock.m);
	CHECK(5, sim_stock.m[0] == 0x14A && sim_stock.m[8] == 0x14B);
	CHECK(6, sim_masked && sim_psw == 0x700);
	CHECK(7, main_rbswap_base_assert(0) == MAIN_RBSWAP_KEPT);
	CHECK(8, fenykl_rd32(slot) == sim_copy());
}

static void sim_rebuilt(uint32_t slot)
{
	fenykl_wr32(slot, (uint32_t)sim_stock.m);
	CHECK(10, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	CHECK(11, fenykl_rd32(slot) == sim_copy());

	fenykl_wr32(slot, sim_other_u());
	CHECK(12, main_rbswap_base_assert(0) == MAIN_RBSWAP_FOREIGN);
	CHECK(13, fenykl_rd32(slot) == sim_copy());
	CHECK(14, sim_same(main_rbswap_base_copy(0), sim_other_swapped));
	CHECK(15, main_rbswap_base_get(0)->stock == sim_other_u());

	/* The stock changed in place: the stock word goes back before the copy is rewritten. */
	sim_other.m[1] = 0x11;
	CHECK(16, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	CHECK(17, main_rbswap_base_copy(0)[7] == 0x11 && fenykl_rd32(slot) == sim_copy());
	sim_other.m[1] = 1;
	CHECK(18, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
}

static void sim_moved(uint32_t main, uint32_t ovr)
{
	fenykl_wr16(FW_PROP_OVERRIDE_VALID, 1);
	CHECK(20, fenykl_wb_base_slot(0) == main);	/* empty override entry: the lookup keeps main */
	fenykl_wr32(ovr, (uint32_t)sim_override.m);
	CHECK(21, fenykl_wb_base_slot(0) == ovr);
	CHECK(22, main_rbswap_base_assert(0) == MAIN_RBSWAP_MOVED);
	CHECK(23, fenykl_rd32(main) == sim_other_u());
	CHECK(24, fenykl_rd32(ovr) == sim_copy() && main_rbswap_base_get(0)->slot == ovr);
	CHECK(25, main_rbswap_base_restore(0));
	CHECK(26, fenykl_rd32(ovr) == (uint32_t)sim_override.m);
	CHECK(27, main_rbswap_base_get(0)->slot == 0 && main_rbswap_base_restore(0));

	/* The override entry cloned from the table word while the copy is stored: adopted. */
	fenykl_wr32(ovr, 0);
	CHECK(28, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	fenykl_wr32(ovr, sim_copy());
	CHECK(29, main_rbswap_base_assert(0) == MAIN_RBSWAP_MOVED);
	CHECK(30, fenykl_rd32(main) == sim_other_u() && fenykl_rd32(ovr) == sim_copy());
	CHECK(31, main_rbswap_base_get(0)->slot == ovr &&
		  main_rbswap_base_get(0)->stock == sim_other_u());
	CHECK(32, main_rbswap_base_assert(0) == MAIN_RBSWAP_KEPT);
	CHECK(33, main_rbswap_base_restore(0) && fenykl_rd32(ovr) == sim_other_u());
	fenykl_wr32(ovr, 0);
	fenykl_wr16(FW_PROP_OVERRIDE_VALID, 0);
}

static void sim_refused(uint32_t slot)
{
	const uint32_t bad[] = {
		0, (uint32_t)sim_stock.m + 1, 0x10000000u, SIM_CACHED(sim_copy()),
		(uint32_t)main_rbswap_base_copy(2), SIM_CACHED((uint32_t)main_rbswap_base_copy(1)),
	};
	const uint32_t heads[] = { SIM_HEAD + 1, SIM_HEAD + (1u << 16) };
	unsigned int i;

	for (i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
		fenykl_wr32(slot, bad[i]);
		CHECK(40 + i, main_rbswap_base_assert(0) == MAIN_RBSWAP_INVALID);
		CHECK(50 + i, fenykl_rd32(slot) == bad[i] && main_rbswap_base_get(0)->slot == 0);
	}
	fenykl_wr32(slot, (uint32_t)sim_stock.m);
	for (i = 0; i < sizeof(heads) / sizeof(heads[0]); i++) {
		fenykl_wr32(slot - 4u, heads[i]);
		CHECK(60 + i, main_rbswap_base_assert(0) == MAIN_RBSWAP_ENTRY);
		CHECK(62 + i, fenykl_rd32(slot) == (uint32_t)sim_stock.m);
	}
	fenykl_wr32(slot - 4u, SIM_HEAD);
}

/* Disable with an override entry cloned from the table word but not yet adopted: both put back. */
static void sim_cloned_off(uint32_t main, uint32_t ovr)
{
	fenykl_wr32(main, (uint32_t)sim_stock.m);
	CHECK(90, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	fenykl_wr32(ovr, sim_copy());
	fenykl_wr16(FW_PROP_OVERRIDE_VALID, 1);
	CHECK(91, main_rbswap_base_restore(0));
	CHECK(92, fenykl_rd32(main) == (uint32_t)sim_stock.m &&
		  fenykl_rd32(ovr) == (uint32_t)sim_stock.m);
	fenykl_wr32(ovr, 0);
	fenykl_wr16(FW_PROP_OVERRIDE_VALID, 0);
}

/* The copy in the slot with nothing recorded: the last known stock goes back, a fresh copy is stored. */
static void sim_unrecorded(uint32_t slot)
{
	fenykl_wr32(slot, sim_copy());
	CHECK(93, main_rbswap_base_get(0)->slot == 0);
	CHECK(94, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	CHECK(95, fenykl_rd32(slot) == sim_copy() && main_rbswap_base_get(0)->slot == slot &&
		  main_rbswap_base_get(0)->stock == (uint32_t)sim_stock.m);
	/* A rebuild already put the stock back: the restore counts as done. */
	fenykl_wr32(slot, (uint32_t)sim_stock.m);
	CHECK(96, main_rbswap_base_restore(0));
}

static void sim_lost(uint32_t slot)
{
	fenykl_wr32(slot, (uint32_t)sim_stock.m);
	CHECK(70, main_rbswap_base_assert(0) == MAIN_RBSWAP_STORED);
	fenykl_wr32(slot, (uint32_t)sim_other.m);
	CHECK(71, !main_rbswap_base_restore(0));
	CHECK(72, fenykl_rd32(slot) == (uint32_t)sim_other.m && main_rbswap_base_get(0)->slot == 0);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	uint32_t main = fenykl_wb_base_slot(0), ovr = main - FW_PROP_TABLE + FW_PROP_OVERRIDE;

	(void)load_base;
	(void)image_size;
	CHECK(80, main == FW_PROP_TABLE + 8u * 0xAA6u + 4u);
	CHECK(81, fenykl_wb_base_slot(2) == FW_PROP_TABLE + 8u * 0xAA5u + 4u);
	fenykl_wr32(main - 4u, SIM_HEAD);
	fenykl_wr32(ovr - 4u, SIM_HEAD);
	sim_first_store(main);
	sim_rebuilt(main);
	sim_moved(main, ovr);
	sim_refused(main);
	sim_cloned_off(main, ovr);
	sim_unrecorded(main);
	sim_lost(main);
	sim_exit_code = sim_fail;
}
