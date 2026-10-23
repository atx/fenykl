/* Slot replacement: a slot is a data word the firmware calls through. It is swapped with a plain store
   under masked interrupts, with the D-cache written back before and after (fenykl_dcache_flush), so
   the word in DRAM changes too. Data only: no code is patched, no I-cache work. Publishes on this core
   only (README, Callbacks and slots). */
#include <fenykl.h>

static int slot_swap(volatile uint32_t *word, uint32_t from, uint32_t to)
{
	int result = FENYKL_SLOT_EVALUE;
	uint32_t psw;

	fenykl_dcache_flush();
	psw = fw_psw_get();
	fw_irq_disable_all();
	if (*word == from) {
		*word = to;
		result = *word == to ? 0 : FENYKL_SLOT_EVERIFY;
	}
	fw_irq_mask_restore(psw);
	fenykl_dcache_flush();
	return result;
}

int fenykl_slot_replace(struct fenykl_slot *slot, uint32_t fn)
{
	int result;

	if (slot->current)
		return slot->current == fn ? 0 : FENYKL_SLOT_EBUSY;
	result = slot_swap((volatile uint32_t *)slot->address, slot->expected, fn);
	if (result == 0)
		slot->current = fn;
	return result;
}

int fenykl_slot_restore(struct fenykl_slot *slot)
{
	int result;

	if (!slot->current)
		return 0;
	result = slot_swap((volatile uint32_t *)slot->address, slot->current, slot->expected);
	if (result == 0)
		slot->current = 0;
	return result;
}
