/* Hook install/remove for the E-PL3. Publishes on this
   core only: CPU1 is neither stopped nor has its I-cache invalidated (README, Hooks). */
#include <fenykl.h>

#define JMP_SIZE 5

static void jmp_to_stub(const struct fenykl_hook *hook, uint8_t *out)
{
	uint32_t rel = hook->stub - hook->entry;

	out[0] = 0xdc; /* jmp d32, relative to the jmp */
	for (int i = 1; i < JMP_SIZE; i++)
		out[i] = rel >> (8 * (i - 1));
}

/* The entry holds `head`, followed by the rest of the stock span. */
static int holds(const volatile uint8_t *entry, const struct fenykl_hook *hook, const uint8_t *head)
{
	for (int i = 0; i < hook->length; i++)
		if (entry[i] != (i < JMP_SIZE ? head[i] : hook->original[i]))
			return 0;
	return 1;
}

static int replace_locked(const struct fenykl_hook *hook, const uint8_t *from, const uint8_t *to)
{
	/* Code is written and checked through the uncached alias; the CPU never wrote its cached alias. */
	volatile uint8_t *entry = FENYKL_UNCACHED(hook->entry);

	if (!holds(entry, hook, from))
		return FENYKL_HOOK_EBYTES;
	for (int i = 0; i < JMP_SIZE; i++)
		entry[i] = to[i];
	int result = holds(entry, hook, to) ? 0 : FENYKL_HOOK_EVERIFY;
	/* Drop stale lines of the entry from this core's I-cache: invalidate(0) leaves it disabled. */
	fw_cache_invalidate(0);
	while (fw_cache_busy(0))
		;
	fw_cache_enable(0);
	return result;
}

static int replace(const struct fenykl_hook *hook, const uint8_t *from, const uint8_t *to)
{
	/* Write back dirty D-cache lines first, as the loader does, so none can land on the patch later. */
	fw_dcache_purge_all_irqsafe();
	uint32_t psw = fw_psw_get();
	fw_irq_disable_all();
	int result = replace_locked(hook, from, to);
	fw_irq_mask_restore(psw);
	return result;
}

int fenykl_hook_install(struct fenykl_hook *hook)
{
	uint8_t jmp[JMP_SIZE];

	if (hook->installed)
		return 0;
	jmp_to_stub(hook, jmp);
	int result = replace(hook, hook->original, jmp);
	if (result == 0)
		hook->installed = 1;
	return result;
}

int fenykl_hook_remove(struct fenykl_hook *hook)
{
	uint8_t jmp[JMP_SIZE];

	if (!hook->installed)
		return 0;
	jmp_to_stub(hook, jmp);
	int result = replace(hook, jmp, hook->original);
	if (result == 0)
		hook->installed = 0;
	return result;
}
