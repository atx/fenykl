/* Hook install/remove for the E-M10 II. The patch is written from CPU0 through the uncached alias
   with CPU0's interrupts and dispatch masked, the D-cache is written back and CPU0's I-cache is
   invalidated. CPU1/CPU2 are neither stopped nor invalidated (sdk/README.md, Hooks). */
#include <fenykl.h>

#define JMP_SIZE 5

/* CHCTR, the executing core's cache control: ICEN and ICINV as written by the CPU1/CPU2 entries
   (0x30 with the enables clear, then 3). ICBUSY is the I-side twin of DCBUSY 8 (dcache_flush_all),
   as in the E-PL3 cache driver; no stock code of this body reads it, so every wait is bounded. Like
   the stock entries, every write is an explicit value: the INV bits are never written back as read
   (a DCINV reading 1 would otherwise be re-asserted with dirty lines in the D-cache). */
#define CHCTR ((volatile uint16_t *)FW_CHCTR)
#define CHCTR_ICEN 0x01u
#define CHCTR_ICBUSY 0x04u
#define CHCTR_ICINV 0x10u
#define CHCTR_INV 0x30u /* ICINV | DCINV */
#define FENYKL_EM10II_ICBUSY_POLLS 20000u

static struct fenykl_icache_stats icache;

static int icache_wait(uint32_t *polls)
{
	for (uint32_t i = 0; i < FENYKL_EM10II_ICBUSY_POLLS; i++) {
		if (!(*CHCTR & CHCTR_ICBUSY)) {
			*polls = i;
			return 0;
		}
	}
	*polls = FENYKL_EM10II_ICBUSY_POLLS;
	return -1;
}

/* `off`: CHCTR with ICEN and the INV bits clear, already written. */
static int icache_drop(uint16_t off)
{
	if (icache_wait(&icache.polls[0]))
		return FENYKL_ICACHE_EBUSY;
	*CHCTR = off | CHCTR_ICINV;
	icache.chctr[2] = *CHCTR;
	if (icache_wait(&icache.polls[1]))
		return FENYKL_ICACHE_ETIMEOUT;
	return 0;
}

/* Interrupts masked by the caller. */
static int icache_invalidate_locked(void)
{
	uint16_t entry = *CHCTR;
	uint16_t base = entry & ~CHCTR_INV; /* restored at exit: ICEN as at entry */
	uint16_t off = base & ~CHCTR_ICEN;

	icache.chctr[0] = entry;
	icache.chctr[2] = 0;
	icache.polls[1] = 0;
	*CHCTR = off;
	icache.chctr[1] = *CHCTR;
	int result = icache_drop(off);
	*CHCTR = base;
	icache.chctr[3] = *CHCTR;
	icache.runs++;
	if (result)
		icache.timeouts++;
	for (int i = 0; i < 2; i++)
		if (icache.polls[i] > icache.polls_max)
			icache.polls_max = icache.polls[i];
	return result;
}

int fenykl_icache_invalidate(void)
{
	unsigned int cookie = fw_irq_lock(0);
	int result = icache_invalidate_locked();

	fw_irq_unlock(cookie);
	return result;
}

void fenykl_icache_get_stats(struct fenykl_icache_stats *out)
{
	*out = icache;
}

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

static void put(volatile uint8_t *entry, const uint8_t *bytes)
{
	for (int i = 0; i < JMP_SIZE; i++)
		entry[i] = bytes[i];
}

static int replace_locked(const struct fenykl_hook *hook, const uint8_t *from, const uint8_t *to)
{
	volatile uint8_t *entry = FENYKL_UNCACHED(hook->entry);

	/* No dirty line may be written back over the entry later. */
	fw_dcache_flush_all();
	if (!holds(entry, hook, from))
		return FENYKL_HOOK_EBYTES;
	put(entry, to);
	int result = holds(entry, hook, to) ? 0 : FENYKL_HOOK_EVERIFY;
	fw_dcache_flush_all();
	if (result == 0 && icache_invalidate_locked() != 0)
		result = FENYKL_HOOK_EICACHE;
	if (result == 0)
		return 0;
	/* CPU0 fetched nothing from the entry since the write: its I-cache holds old lines or none. */
	put(entry, from);
	fw_dcache_flush_all();
	return result;
}

static int replace(const struct fenykl_hook *hook, const uint8_t *from, const uint8_t *to)
{
	unsigned int cookie = fw_irq_lock(0);
	int result = replace_locked(hook, from, to);

	fw_irq_unlock(cookie);
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
