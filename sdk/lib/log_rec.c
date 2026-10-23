/*
 * Record writers (sdk/docs/logging.md section 4.2). Built as CPU1 code: they run on either core,
 * from any context, and call nothing but the per-core PSW primitives.
 */
#define FENYKL_CPU1 1
#include "log_int.h"

FENYKL_SHARED struct flog_ring1 fenykl_log_ring1;

static void flog_fill_fmt(volatile struct flog_slot *s, uint32_t meta,
			  const struct fenykl_log_module *mod, const char *fmt,
			  uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3)
{
	s->w[0] = FENYKL_TIMER_NOW();
	s->w[1] = meta;
	s->w[2] = (uint32_t)mod;
	s->w[3] = (uint32_t)fmt;
	s->w[4] = a0;
	s->w[5] = a1;
	s->w[6] = a2;
	s->w[7] = a3;
}

/* Text record i of a message: 20 bytes from text + 20 * i, zero padded. */
static void flog_fill_text(volatile struct flog_slot *s, uint32_t t, uint32_t meta,
			   const struct fenykl_log_module *mod, const uint8_t *text, unsigned int n)
{
	s->w[0] = t;
	s->w[1] = meta;
	s->w[2] = (uint32_t)mod;
	for (unsigned int w = 0; w < FLOG_TEXT_PER_SLOT / 4; w++) {
		uint32_t v = 0;

		for (unsigned int b = 0; b < 4; b++) {
			unsigned int i = 4 * w + b;

			if (i < n)
				v |= (uint32_t)text[i] << (8 * b);
		}
		s->w[3 + w] = v;
	}
}


/* Bytes of a len-byte message that go into its record i. */
static unsigned int flog_text_chunk(unsigned int len, unsigned int i)
{
	unsigned int n = len - FLOG_TEXT_PER_SLOT * i;

	return n > FLOG_TEXT_PER_SLOT ? FLOG_TEXT_PER_SLOT : n;
}

/* Ring 0's next slot, evicting the oldest record when the ring is full. CPU0 IRQs masked. */
static uint32_t flog_ring0_claim(volatile struct flog_ring0 *r)
{
	uint32_t h = r->head;

	if (h - r->tail >= FLOG_RING0_RECORDS) {
		r->tail = h - FLOG_RING0_RECORDS + 1;
		r->overwritten++;
	}
	return h;
}

/* Ring 0 under the CPU0 interrupt mask: evicts the oldest records when full. */
static void flog_ring0_text(const struct fenykl_log_module *mod, unsigned int level,
			    const uint8_t *text, unsigned int len, bool trunc)
{
	volatile struct flog_ring0 *r = &flog.ring0;
	unsigned int k = flog_text_slots(len);
	uint32_t psw = flog_irq_save();
	uint32_t t = FENYKL_TIMER_NOW();

	for (unsigned int i = 0; i < k; i++) {
		uint32_t h = flog_ring0_claim(r);
		unsigned int n = flog_text_chunk(len, i);

		flog_fill_text(&r->slot[h % FLOG_RING0_RECORDS], t,
			       FLOG_META(h, n, 0, i ? FLOG_KIND_CONT : FLOG_KIND_TEXT, level,
					 trunc && !i),
			       mod, text + FLOG_TEXT_PER_SLOT * i, n);
		r->head = h + 1;
	}
	fw_irq_mask_restore(psw);
}

/* Ring 1 under the CPU1 interrupt mask: drops the whole message when it does not fit. */
static void flog_ring1_text(const struct fenykl_log_module *mod, unsigned int level,
			    const uint8_t *text, unsigned int len, bool trunc)
{
	volatile struct flog_ring1 *r = flog_ring1();
	unsigned int k = flog_text_slots(len);
	uint32_t psw = flog_irq_save();
	uint32_t seq = r->seq;
	uint32_t h, t;

	r->seq = seq + k;
	h = r->head;
	if (FLOG_RING1_RECORDS - (h - r->tail) < k) {
		r->dropped += k;
		goto out;
	}
	t = FENYKL_TIMER_NOW();
	for (unsigned int i = 0; i < k; i++) {
		unsigned int n = flog_text_chunk(len, i);

		flog_fill_text(&r->slot[FLOG_RING1_SLOT(h + i)], t,
			       FLOG_META(seq + i, n, 1, i ? FLOG_KIND_CONT : FLOG_KIND_TEXT, level,
					 trunc && !i),
			       mod, text + FLOG_TEXT_PER_SLOT * i, n);
	}
	r->head = h + k; /* publishes the records: written after them */
out:
	fw_irq_mask_restore(psw);
}

static void flog_ring0_rec(const struct fenykl_log_module *mod, unsigned int level,
			   const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3)
{
	volatile struct flog_ring0 *r = &flog.ring0;
	uint32_t psw = flog_irq_save();
	uint32_t h = flog_ring0_claim(r);

	flog_fill_fmt(&r->slot[h % FLOG_RING0_RECORDS], FLOG_META(h, 0, 0, FLOG_KIND_FMT, level, 0),
		      mod, fmt, a0, a1, a2, a3);
	r->head = h + 1;
	fw_irq_mask_restore(psw);
}

static void flog_ring1_rec(const struct fenykl_log_module *mod, unsigned int level,
			   const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3)
{
	volatile struct flog_ring1 *r = flog_ring1();
	uint32_t psw = flog_irq_save();
	uint32_t seq = r->seq++;
	uint32_t h = r->head;

	if (h - r->tail >= FLOG_RING1_RECORDS) {
		r->dropped++;
		goto out;
	}
	flog_fill_fmt(&r->slot[FLOG_RING1_SLOT(h)], FLOG_META(seq, 0, 1, FLOG_KIND_FMT, level, 0),
		      mod, fmt, a0, a1, a2, a3);
	r->head = h + 1;
out:
	fw_irq_mask_restore(psw);
}

void fenykl_log_rec(const struct fenykl_log_module *mod, unsigned int level, const char *fmt,
		    uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3)
{
	if (FENYKL_CORE_ID()) {
		flog_ring1_rec(mod, level, fmt, a0, a1, a2, a3);
		return;
	}
	if (!flog.ready) {
		flog.early++;
		return;
	}
	flog_ring0_rec(mod, level, fmt, a0, a1, a2, a3);
}

void fenykl_log_text(const struct fenykl_log_module *mod, unsigned int level, const char *text,
		     unsigned int len)
{
	bool trunc = len > FENYKL_LOG_TEXT_MAX;

	if (trunc)
		len = FENYKL_LOG_TEXT_MAX;
	if (FENYKL_CORE_ID()) {
		flog_ring1_text(mod, level, (const uint8_t *)text, len, trunc);
		return;
	}
	if (!flog.ready) {
		flog.early++;
		return;
	}
	flog_ring0_text(mod, level, (const uint8_t *)text, len, trunc);
}
