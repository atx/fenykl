/* Slot and cloned-vtable replacement for the E-PL3.
   A slot is a data word, read through the D-cache: it is swapped with a plain store under masked
   interrupts, and the D-cache is written back before (what the new value points to, e.g. a clone) and
   after (the word). Publishes on this core only, like hooks.c (README, Callbacks and slots). */
#include <fenykl.h>

static int swap(volatile uint32_t *word, uint32_t from, uint32_t to)
{
	fw_dcache_purge_all_irqsafe();
	uint32_t psw = fw_psw_get();
	fw_irq_disable_all();
	int result = FENYKL_SLOT_EVALUE;
	if (*word == from) {
		*word = to;
		result = *word == to ? 0 : FENYKL_SLOT_EVERIFY;
	}
	fw_irq_mask_restore(psw);
	fw_dcache_purge_all_irqsafe();
	return result;
}

int fenykl_slot_replace(struct fenykl_slot *slot, uint32_t fn)
{
	if (slot->current)
		return slot->current == fn ? 0 : FENYKL_SLOT_EBUSY;
	int result = swap((volatile uint32_t *)slot->address, slot->expected, fn);
	if (result == 0)
		slot->current = fn;
	return result;
}

int fenykl_slot_restore(struct fenykl_slot *slot)
{
	if (!slot->current)
		return 0;
	int result = swap((volatile uint32_t *)slot->address, slot->current, slot->expected);
	if (result == 0)
		slot->current = 0;
	return result;
}

int fenykl_vtable_attach(struct fenykl_vtable *vt, void *object)
{
	if (vt->object)
		return vt->object == (uint32_t)object ? 0 : FENYKL_SLOT_EBUSY;
	const uint32_t *stock = (const uint32_t *)vt->address;
	for (uint32_t i = 0; i < vt->count; i++)
		if (!vt->clone[i])
			vt->clone[i] = stock[i];
	/* The object's first word is its vtable pointer; it must still be the stock table. */
	int result = swap(object, vt->address, (uint32_t)vt->clone);
	if (result == 0)
		vt->object = (uint32_t)object;
	return result;
}

int fenykl_vtable_detach(struct fenykl_vtable *vt)
{
	if (!vt->object)
		return 0;
	int result = swap((volatile uint32_t *)vt->object, (uint32_t)vt->clone, vt->address);
	if (result == 0)
		vt->object = 0;
	return result;
}
