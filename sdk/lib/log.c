/*
 * The CPU0 side of the logging library (sdk/docs/logging.md): init (size cap, boot header), the
 * flusher (drain both rings, format with fw_sprintf, append to C:\FENYKL.LOG), its task and the
 * firmware console tee. Task context only: everything here may block in the file RPC.
 */
#include "log_int.h"

#define FLOG_FMT_MAX 160u	/* a longer format (or one without NUL) is a bad record */
#define FLOG_STR_MAX 128u	/* the same for a %s argument */
#define FLOG_NAME_MAX 64u	/* the same for a module name */
#define FLOG_NAME_WIDTH 7u	/* module name column of a line */
#define FLOG_SPEC_LIMIT 100u	/* a width or precision of a format must be below this */
#define FLOG_NO_PREC (~0u)
#define FLOG_INT_LEN 11u	/* longest formatted 32-bit integer: "-2147483648" */
#define FLOG_SUBST_LEN 10u	/* "<%08x>" */
#define FLOG_TEXT_MARGIN 16u	/* line room a text record keeps free for " [truncated]" CR LF */
#define FLOG_USEC_PER_SEC 1000000u
#define FLOG_JUMP_US (1u << 29)
#define FLOG_PROBE_SIZE 0x2000u	/* fw_malloc returns NULL above 0x3FFC [camera] */
#define FLOG_STATUS_EVERY 20u	/* flushes between two status blocks */
#define FLOG_FLUSH_MS 2000u	/* default flusher period */
#define FLOG_APPEND_TRIES 3
#define FLOG_APPEND_RETRY_MS 100u
#define FLOG_TASK_PRIO 12
#define FLOG_TASK_STACK 0x1000u
#define FLOG_TEE_POLL_MS 20u
#define FLOG_IDLE_POLL_MS 100u
#define FLOG_ERR_FILE "C:\\FENYKL.ERR"
#define FLOG_BOOT_CAUSE (*(volatile uint8_t *)FW_BOOT_CAUSE)
#define FLOG_BUILD_ID (*(const uint32_t *)(FENYKL_LOAD_BASE + 0x10u)) /* FENYKL.BIN header sum */
/* Firmware console capture: uart_putchar's boot capture buffer. */
#define FLOG_TEE_BUF ((const volatile uint8_t *)FW_UART_TEE_BUF)
#define FLOG_TEE_COUNT (*(volatile uint16_t *)FW_UART_TEE_COUNT)
#define FLOG_TEE_STATE1 (*(volatile uint8_t *)FW_UART_TEE_STATE1)

extern char fenykl_bss_end[];

struct flog_state flog;

static FENYKL_LOG_MODULE(flog_mod, "log", FENYKL_LOG_INFO);
static FENYKL_LOG_MODULE(flog_tee_mod, "fwcon", FENYKL_LOG_INFO);

/* --- small helpers --- */

static unsigned int flog_put_dec(char *p, uint32_t v, unsigned int width, char pad)
{
	char tmp[10];
	unsigned int n = 0, i = 0;

	do {
		tmp[n++] = '0' + v % 10;
		v /= 10;
	} while (v);
	for (; width > n; width--)
		p[i++] = pad;
	while (n)
		p[i++] = tmp[--n];
	return i;
}

static bool flog_in_image(uint32_t p)
{
	return p >= FENYKL_LOAD_BASE && p < (uint32_t)fenykl_bss_end;
}

/*
 * Length of a NUL-terminated string at p, or -1 if p is outside the payload and firmware images
 * or has no NUL within max bytes.
 */
static int flog_str_len(uint32_t p, unsigned int max)
{
	const char *s = (const char *)p;

	if (!flog_in_image(p) && !(p >= FW_MAIN_CODE_START && p < FW_MAIN_CODE_END))
		return -1;
	for (unsigned int i = 0; i < max; i++)
		if (!s[i])
			return i;
	return -1;
}

/* --- output buffer, written to flog.out_fp --- */

static void flog_out_write(void)
{
	int n;

	if (!flog.out_len)
		return;
	n = fw_rpc_fwrite(flog.out, 1, flog.out_len, flog.out_fp);
	if (n > 0)
		flog.st.bytes += n;
	if (n != (int)flog.out_len)
		flog.st.short_write++;
	flog.out_len = 0;
}

static void flog_out_put(const char *s, unsigned int n)
{
	if (flog.out_len + n > FLOG_OUT_SIZE)
		flog_out_write();
	for (unsigned int i = 0; i < n; i++)
		flog.out[flog.out_len++] = s[i];
}

/* --- formatting --- */

static struct flog_stamp flog_stamp_add(struct flog_stamp a, uint32_t us)
{
	a.s += us / FLOG_USEC_PER_SEC;
	a.us += us % FLOG_USEC_PER_SEC;
	if (a.us >= FLOG_USEC_PER_SEC) {
		a.us -= FLOG_USEC_PER_SEC;
		a.s++;
	}
	return a;
}

static struct flog_stamp flog_stamp_of(const struct flog_stream *s, uint32_t t)
{
	return flog_stamp_add(s->ext, FENYKL_LOG_ELAPSED(s->last, t));
}

static bool flog_stamp_not_after(struct flog_stamp a, struct flog_stamp b)
{
	return a.s < b.s || (a.s == b.s && a.us <= b.us);
}

/* "[ssssss.uuuuuu] L module : " into flog.line; returns its length. */
static unsigned int flog_line_prefix(struct flog_stamp t, unsigned int level, unsigned int core,
				     const char *name)
{
	static const char levels[8] = { '?', 'E', 'W', 'I', 'D', 'T', '?', '?' };
	char *line = flog.line;
	unsigned int n = 0, k = 0;

	line[n++] = '[';
	n += flog_put_dec(line + n, t.s, 6, ' ');
	line[n++] = '.';
	n += flog_put_dec(line + n, t.us, 6, '0');
	line[n++] = ']';
	line[n++] = ' ';
	line[n++] = levels[level & 7] + (core ? 'a' - 'A' : 0);
	line[n++] = ' ';
	if (name) {
		for (; k < FLOG_NAME_WIDTH && name[k]; k++)
			line[n++] = name[k];
	}
	for (; k < FLOG_NAME_WIDTH; k++)
		line[n++] = ' ';
	line[n++] = ':';
	line[n++] = ' ';
	return n;
}

/* Ends flog.line at n with CR LF and queues it for the file. */
static void flog_line_end(unsigned int n)
{
	flog.line[n++] = '\r';
	flog.line[n++] = '\n';
	flog_out_put(flog.line, n);
}

/* The same for a line whose text from n on is NUL-terminated (fw_sprintf output). */
static void flog_line_end_str(unsigned int n)
{
	while (flog.line[n])
		n++;
	flog_line_end(n);
}

/* A line of the library itself, stamped now on ring 0's clock: fw_sprintf, at most 6 words. */
static void flog_note(const char *fmt, uint32_t a, uint32_t b, uint32_t c, uint32_t d,
		      uint32_t e, uint32_t f)
{
	struct flog_stamp now = flog_stamp_of(&flog.stream[0], FENYKL_LOG_TIMER_NOW());
	unsigned int n = flog_line_prefix(now, FENYKL_LOG_INFO, 0, flog_mod.name);

	fw_sprintf(flog.line + n, fmt, a, b, c, d, e, f);
	flog_line_end_str(n);
}

/*
 * Parses the flags, width, precision and length modifier of a conversion that starts after its
 * '%' at p. Returns a pointer to the conversion character, or NULL for a width or precision of
 * FLOG_SPEC_LIMIT or more.
 */
static const char *flog_fmt_spec(const char *p, unsigned int *width, unsigned int *prec)
{
	unsigned int w = 0, pr = FLOG_NO_PREC;

	while (*p == '-' || *p == '0' || *p == ' ' || *p == '+' || *p == '#')
		p++;
	while (*p >= '0' && *p <= '9' && w < FLOG_SPEC_LIMIT)
		w = w * 10 + *p++ - '0';
	if (*p == '.') {
		pr = 0;
		for (p++; *p >= '0' && *p <= '9' && pr < FLOG_SPEC_LIMIT; p++)
			pr = pr * 10 + *p - '0';
	}
	if (w >= FLOG_SPEC_LIMIT || (pr != FLOG_NO_PREC && pr >= FLOG_SPEC_LIMIT))
		return NULL;
	while (*p == 'l' || *p == 'h')
		p++;
	*width = w;
	*prec = pr;
	return p;
}

/*
 * Upper bound of the formatted length of conversion c of *arg, or -1 if it is not one of
 * d i u x X c s p. A %s argument that does not point to a string in an image is replaced by
 * "<address>", formatted into subst.
 */
static int flog_fmt_conv_len(char c, unsigned int prec, uint32_t *arg, char *subst)
{
	int len;

	switch (c) {
	case 's':
		len = flog_str_len(*arg, FLOG_STR_MAX);
		if (len < 0) {
			fw_sprintf(subst, "<%08x>", *arg);
			*arg = (uint32_t)subst;
			len = FLOG_SUBST_LEN;
		}
		return (unsigned int)len < prec ? (unsigned int)len : prec;
	case 'd':
	case 'i':
	case 'u':
	case 'x':
	case 'X':
	case 'c':
	case 'p':
		return prec != FLOG_NO_PREC && prec > FLOG_INT_LEN ? prec : FLOG_INT_LEN;
	default:
		return -1;
	}
}

/*
 * Checks fmt against the record's arguments: at most 4 conversions of d i u x X c s p (with
 * flags, width, precision, h/l), %s arguments that point into an image and end within
 * FLOG_STR_MAX (others are replaced by "<address>"). Returns an upper bound of the formatted
 * length, or -1.
 */
static int flog_fmt_check(const char *fmt, uint32_t *arg)
{
	unsigned int bound = 0, conv = 0;

	for (unsigned int i = 0; i < FLOG_FMT_MAX; i++) {
		const char *p;
		unsigned int width, prec;
		int len;

		if (!fmt[i])
			return bound + i;
		if (fmt[i] != '%')
			continue;
		p = flog_fmt_spec(fmt + i + 1, &width, &prec);
		if (!p)
			return -1;
		i = p - fmt;
		if (*p == '%')
			continue;
		if (conv == FLOG_FMT_ARGS)
			return -1;
		len = flog_fmt_conv_len(*p, prec, &arg[conv], flog.subst[conv]);
		if (len < 0)
			return -1;
		bound += width > (unsigned int)len ? width : (unsigned int)len;
		conv++;
	}
	return -1;
}

static bool flog_slot_sane(const struct flog_slot *r)
{
	uint32_t m = r->w[1];

	return FLOG_META_KIND(m) <= FLOG_KIND_CONT && FLOG_META_LEVEL(m) <= FENYKL_LOG_TRACE &&
	       FLOG_META_LEN(m) <= FLOG_TEXT_PER_SLOT && flog_in_image(r->w[2]);
}

/* A [lost N] line for a sequence gap before record r. */
static void flog_emit_gap(struct flog_stream *s, const struct flog_slot *r, uint32_t gap)
{
	unsigned int k = flog_line_prefix(flog_stamp_of(s, r->w[0]), FENYKL_LOG_WARN, s->ring,
					  flog_mod.name);

	fw_sprintf(flog.line + k, "[lost %u records, ring %u]", gap, s->ring);
	flog_line_end_str(k);
	flog.st.lost[s->ring] += gap;
}

/* Advances the stream's extended stamp to raw stamp t. */
static void flog_stream_advance(struct flog_stream *s, uint32_t t)
{
	uint32_t delta = FENYKL_LOG_ELAPSED(s->last, t);

	if (delta >= FLOG_JUMP_US)
		flog.st.stamp_jumps++;
	s->ext = flog_stamp_add(s->ext, delta);
	s->last = t;
}

/* Records of the message at b[0]: a text record and its continuation records among the n. */
static unsigned int flog_msg_records(const struct flog_slot *b, unsigned int n)
{
	unsigned int count = 1;

	if (FLOG_META_KIND(b[0].w[1]) == FLOG_KIND_FMT)
		return count;
	while (count < n && FLOG_META_KIND(b[count].w[1]) == FLOG_KIND_CONT)
		count++;
	return count;
}

static void flog_emit_bad_record(const struct flog_slot *r, unsigned int k)
{
	flog.st.bad++;
	fw_sprintf(flog.line + k, "<bad record %08x %08x %08x>", r->w[1], r->w[2], r->w[3]);
	flog_line_end_str(k);
}

static void flog_emit_fmt(const struct flog_slot *r, unsigned int k)
{
	uint32_t arg[FLOG_FMT_ARGS] = { r->w[4], r->w[5], r->w[6], r->w[7] };
	int bound = flog_in_image(r->w[3]) ? flog_fmt_check((const char *)r->w[3], arg) : -1;

	if (bound < 0 || k + bound + 2 > FLOG_LINE_MAX) {
		flog.st.bad++;
		fw_sprintf(flog.line + k, "<bad format %08x: %08x %08x %08x %08x>", r->w[3],
			   r->w[4], r->w[5], r->w[6], r->w[7]);
	} else {
		fw_sprintf(flog.line + k, (const char *)r->w[3], arg[0], arg[1], arg[2], arg[3]);
	}
	flog_line_end_str(k);
}

/* The text of the count records at r; control characters become '.'. */
static void flog_emit_text(const struct flog_slot *r, unsigned int count, unsigned int k)
{
	char *line = flog.line;
	uint32_t m = r->w[1];

	if (FLOG_META_KIND(m) == FLOG_KIND_CONT) {
		line[k++] = '.';
		line[k++] = '.';
		line[k++] = '.';
	}
	for (unsigned int j = 0; j < count; j++) {
		const uint8_t *t = (const uint8_t *)&r[j].w[3];
		unsigned int len = FLOG_META_LEN(r[j].w[1]);

		for (unsigned int c = 0; c < len && k < FLOG_LINE_MAX - FLOG_TEXT_MARGIN; c++)
			line[k++] = t[c] < 0x20 || t[c] == 0x7F ? '.' : t[c];
	}
	if (FLOG_META_TRUNC(m)) {
		flog.st.truncated++;
		for (const char *t = " [truncated]"; *t; t++)
			line[k++] = *t;
	}
	flog_line_end(k);
}

/*
 * Formats the message starting at b[*i] (a record and, for text, its continuation records in b)
 * and advances *i past it. Sequence gaps become [lost N] lines.
 */
static void flog_emit(struct flog_stream *s, const struct flog_slot *b, unsigned int n,
		      unsigned int *i)
{
	const struct flog_slot *r = &b[*i];
	const struct fenykl_log_module *mod = (const struct fenykl_log_module *)r->w[2];
	uint32_t m = r->w[1];
	uint32_t gap = (FLOG_META_SEQ(m) - s->expect) & FLOG_SEQ_MASK;
	unsigned int count, k;
	bool name_ok;

	if (gap)
		flog_emit_gap(s, r, gap);
	flog_stream_advance(s, r->w[0]);

	count = flog_msg_records(r, n - *i);
	s->expect = (FLOG_META_SEQ(r[count - 1].w[1]) + 1) & FLOG_SEQ_MASK;
	flog.st.written[s->ring] += count;
	*i += count;

	name_ok = flog_slot_sane(r) && flog_str_len((uint32_t)mod->name, FLOG_NAME_MAX) >= 0;
	k = flog_line_prefix(s->ext, FLOG_META_LEVEL(m), FLOG_META_CORE(m),
			     name_ok ? mod->name : "?");
	if (!name_ok)
		flog_emit_bad_record(r, k);
	else if (FLOG_META_KIND(m) == FLOG_KIND_FMT)
		flog_emit_fmt(r, k);
	else
		flog_emit_text(r, count, k);
}

/* --- draining the rings --- */

/*
 * Copies up to FLOG_BATCH records [from, to) of a ring with `size` slots, plus the continuation
 * records of a text message cut by the batch end. Returns the number copied.
 */
static unsigned int flog_copy_range(const volatile struct flog_slot *slot, unsigned int size,
				    uint32_t from, uint32_t to, struct flog_slot *b)
{
	uint32_t n = to - from;

	if (n > FLOG_BATCH) {
		n = FLOG_BATCH;
		while (n < FLOG_STAGE_RECORDS && n < to - from &&
		       FLOG_META_KIND(slot[(from + n) % size].w[1]) == FLOG_KIND_CONT)
			n++;
	}
	for (unsigned int i = 0; i < n; i++)
		for (unsigned int w = 0; w < FLOG_SLOT_WORDS; w++)
			b[i].w[w] = slot[(from + i) % size].w[w];
	return n;
}

static unsigned int flog_ring0_take(void)
{
	volatile struct flog_ring0 *r = &flog.ring0;
	uint32_t psw = flog_irq_save();
	uint32_t t = r->tail;
	unsigned int n = flog_copy_range(r->slot, FLOG_RING0_RECORDS, t, r->head, flog.stage[0]);

	r->tail = t + n;
	fw_irq_mask_restore(psw);
	return n;
}

static unsigned int flog_ring1_take(void)
{
#if FLOG_RING1_RECORDS
	volatile struct flog_ring1 *r = flog_ring1();
	uint32_t t = r->tail, h = r->head;
	unsigned int n;

	if (h - t > FLOG_RING1_RECORDS)
		h = t + FLOG_RING1_RECORDS;
	n = flog_copy_range(r->slot, FLOG_RING1_RECORDS, t, h, flog.stage[1]);
	r->tail = t + n; /* frees the slots for CPU1: after the copy */
	return n;
#else
	return 0;
#endif
}

static unsigned int flog_queued(void)
{
	return flog.ring0.head - flog.ring0.tail + flog_ring1()->head - flog_ring1()->tail;
}

/* Formats n0 staged ring 0 and n1 staged ring 1 records, merged by stamp. */
static void flog_merge(unsigned int n0, unsigned int n1)
{
	struct flog_stream *s0 = &flog.stream[0], *s1 = &flog.stream[1];
	const struct flog_slot *b0 = flog.stage[0], *b1 = flog.stage[1];
	unsigned int i0 = 0, i1 = 0;

	while (i0 < n0 || i1 < n1) {
		bool first0 = i1 >= n1 ||
			      (i0 < n0 && flog_stamp_not_after(flog_stamp_of(s0, b0[i0].w[0]),
							       flog_stamp_of(s1, b1[i1].w[0])));

		if (first0)
			flog_emit(s0, b0, n0, &i0);
		else
			flog_emit(s1, b1, n1, &i1);
	}
}

/*
 * Drains both rings into flog.out_fp, merging the two rounds' batches by stamp. Bounded: records
 * that arrive faster than it writes are left for the next flush.
 */
static void flog_drain(void)
{
	const unsigned int rounds = (FLOG_RING0_RECORDS + FLOG_RING1_RECORDS) / FLOG_BATCH + 2;

	for (unsigned int round = 0; round < rounds; round++) {
		unsigned int n0 = flog_ring0_take(), n1 = flog_ring1_take();

		if (!n0 && !n1)
			break;
		flog_merge(n0, n1);
	}
}

/* --- flushing --- */

static void flog_status(void)
{
	const struct fenykl_log_stats *st = &flog.st;

	flog_note("status: flushes %u, written %u+%u, lost %u+%u, overwritten %u", st->flushes,
		  st->written[0], st->written[1], st->lost[0], st->lost[1],
		  flog.ring0.overwritten);
	flog_note("status: dropped %u, open fails %u, short writes %u, bad %u, max flush %u us, slow %u",
		  flog_ring1()->dropped, st->open_fail, st->short_write, st->bad, st->flush_max_us,
		  st->flush_slow);
	if (flog.cfg.tee)
		flog_note("status: tee %u bytes, %u records, %u overflows", st->tee_bytes,
			  st->tee_records, st->tee_overflow, 0, 0, 0);
	if (st->gate_skipped)
		flog_note("status: card gate skipped %u flushes, longest closed %u ms",
			  st->gate_skipped, st->gate_max_ms, 0, 0, 0, 0);
}

/* --- card gate (fenykl_card_io_allowed) --- */

/* Adds the time since the last check to the closed period. */
static void flog_gate_time(uint32_t now)
{
	flog.gate_us += FENYKL_LOG_ELAPSED(flog.gate_last, now);
	flog.gate_last = now;
	flog.gate_ms += flog.gate_us / 1000u;
	flog.gate_us %= 1000u;
	if (flog.gate_ms > flog.st.gate_max_ms)
		flog.st.gate_max_ms = flog.gate_ms;
}

static void flog_gate_close(uint32_t now)
{
	flog.gate_closed = true;
	flog.gate_last = now;
	flog.gate_us = 0;
	flog.gate_ms = 0;
	flog.gate_skips = 0;
	fenykl_log_warn(&flog_mod, "card gate closed (USB cable in or card not mounted): log on hold");
}

static void flog_gate_reopen(uint32_t now)
{
	flog_gate_time(now);
	flog.gate_closed = false;
	fenykl_log_info(&flog_mod, "card gate open again after %u ms, %u flushes skipped",
			flog.gate_ms, flog.gate_skips);
}

/* Checks the gate; a closed gate counts the flush as skipped. Logs both transitions. */
static bool flog_gate_open(void)
{
	uint32_t now = FENYKL_LOG_TIMER_NOW();

	if (fenykl_card_io_allowed()) {
		if (flog.gate_closed)
			flog_gate_reopen(now);
		return true;
	}
	if (flog.gate_closed)
		flog_gate_time(now);
	else
		flog_gate_close(now);
	flog.gate_skips++;
	flog.st.gate_skipped++;
	return false;
}

/* Claims the flusher for this call: false while init is not done or another flush runs. */
static bool flog_flush_claim(void)
{
	uint32_t psw = flog_irq_save();
	bool taken = flog.ready && !flog.busy;

	if (taken)
		flog.busy = true;
	fw_irq_mask_restore(psw);
	return taken;
}

/* Appends the queued records to the open file and closes it. */
static void flog_flush_write(void)
{
	if (flog.pending_slow)
		flog_note("previous flush took %u us (> %u)", flog.pending_slow, FENYKL_LOG_SLOW_US,
			  0, 0, 0, 0);
	flog.pending_slow = 0;
	if (++flog.st.flushes % FLOG_STATUS_EVERY == 0)
		flog_status();
	flog_drain();
	flog_out_write();
	fw_rpc_fclose(flog.out_fp);
}

/* Counts the flush that opened the file at t0; a slow one is logged by the next flush. */
static void flog_flush_time(uint32_t t0)
{
	uint32_t us = FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());

	if (us > flog.st.flush_max_us)
		flog.st.flush_max_us = us;
	if (us > FENYKL_LOG_SLOW_US) {
		flog.st.flush_slow++;
		flog.pending_slow = us;
	}
}

static int flog_flush_file(void)
{
	uint32_t t0 = FENYKL_LOG_TIMER_NOW();
	int ret = 0;

	flog.st.flush_since = t0;
	flog.st.flush_busy = 1;
	flog.out_fp = fw_rpc_fopen(FENYKL_LOG_FILE, "a");
	if (!flog.out_fp) {
		flog.st.open_fail++;
		ret = FENYKL_LOG_EOPEN;
		goto out;
	}
	flog_flush_write();
	flog_flush_time(t0);
out:
	flog.st.flush_busy = 0;
	return ret;
}

static int flog_init_card(void);

static int flog_flush_gated(void)
{
	int ret;

	if (!flog_gate_open())
		return FENYKL_LOG_ENOCARD;
	if (flog.init_pending) {
		ret = flog_init_card();
		if (ret)
			return ret;
	}
	if (flog_queued() || flog.pending_slow)
		return flog_flush_file();
	return 0;
}

int fenykl_log_flush(void)
{
	int ret;

	if (!flog_flush_claim())
		return FENYKL_LOG_EBUSY;
	flog.last_flush = FENYKL_LOG_TIMER_NOW();
	ret = flog_flush_gated();
	flog.busy = false;
	return ret;
}

/* --- init --- */

/* Bytes in the file, read through in chunks (the RPC has no stat); -1 if it cannot be opened. */
static int flog_file_size(void)
{
	void *fp = fw_rpc_fopen(FENYKL_LOG_FILE, "r");
	char *buf;
	unsigned int chunk;
	int size = 0, n;

	if (!fp)
		return -1;
	buf = fw_malloc(FLOG_PROBE_SIZE);
	chunk = buf ? FLOG_PROBE_SIZE : FLOG_OUT_SIZE;
	if (!buf)
		buf = flog.out;
	do {
		n = fw_rpc_fread(buf, 1, chunk, fp);
		if (n > 0)
			size += n;
	} while (n == (int)chunk);
	fw_rpc_fclose(fp);
	if (buf != flog.out)
		fw_free(buf);
	return size;
}

/* Error file for an append that does not work: padded so that "w" leaves no older tail. */
static void flog_append_error(void)
{
	char *err = flog.err;
	unsigned int n = 0;
	void *fp;

	fw_sprintf(err, "fenykl log: rpc_fopen(\"%s\", \"a\") failed 3 times on an existing file; "
			"no log is written in this boot (build %08x)",
		   FENYKL_LOG_FILE, FLOG_BUILD_ID);
	while (err[n])
		n++;
	while (n < FLOG_ERR_SIZE - 2)
		err[n++] = ' ';
	err[n++] = '\r';
	err[n++] = '\n';
	fp = fw_rpc_fopen(FLOG_ERR_FILE, "w");
	if (!fp)
		return;
	fw_rpc_fwrite(err, 1, FLOG_ERR_SIZE, fp);
	fw_rpc_fclose(fp);
}

/* Takes the configuration and starts both streams; returns timer_now of the start. */
static uint32_t flog_init_state(const struct fenykl_log_config *cfg)
{
	uint32_t t_init;

	if (cfg)
		flog.cfg = *cfg;
	if (!flog.cfg.flush_ms)
		flog.cfg.flush_ms = FLOG_FLUSH_MS;
	t_init = FENYKL_LOG_TIMER_NOW();
	flog.stream[0] = (struct flog_stream){ .last = t_init, .ring = 0 };
	flog.stream[1] = (struct flog_stream){ .last = t_init, .ring = 1 };
	flog.st.boot_cause = FLOG_BOOT_CAUSE;
	return t_init;
}

/*
 * Measures the file and deletes it if it is over the cap. Returns its size, or -1 if there is
 * no file (any more); *removed is the rpc_fremove result.
 */
static int flog_init_cap(int *removed)
{
	int size = flog_file_size();

	*removed = 0;
	if (size > (int)FENYKL_LOG_SIZE_CAP) {
		*removed = fw_rpc_fremove(FENYKL_LOG_FILE);
		if (*removed == 0) {
			flog.st.removed_size = size;
			size = -1;
		}
	}
	flog.st.file_size = size < 0 ? 0 : size;
	return size;
}

/* Creates the file, or appends to the existing one with a few tries. */
static void *flog_init_open(int size)
{
	void *fp;

	if (size < 0)
		return fw_rpc_fopen(FENYKL_LOG_FILE, "w");
	for (int i = 0; i < FLOG_APPEND_TRIES; i++) {
		if (i)
			fw_OlyTaskSleep(FLOG_APPEND_RETRY_MS);
		fp = fw_rpc_fopen(FENYKL_LOG_FILE, "a");
		if (fp)
			return fp;
	}
	return NULL;
}

/* Writes the boot header into the open file and closes it. */
static void flog_init_header(uint32_t t_init, int size, int removed)
{
	static const char rule[] = "\r\n==================================================\r\n";

	flog_out_put(rule, sizeof(rule) - 1);
	flog_note("fenykl log boot: build %08x, boot cause %02x, timer %08x, file %u bytes before this boot",
		  FLOG_BUILD_ID, flog.st.boot_cause, t_init, flog.st.file_size, 0, 0);
	if (flog.st.removed_size)
		flog_note("%s was %u bytes (cap %u): deleted, started fresh",
			  (uint32_t)FENYKL_LOG_FILE, flog.st.removed_size, FENYKL_LOG_SIZE_CAP,
			  0, 0, 0);
	else if (removed)
		flog_note("%s is %u bytes (cap %u) but rpc_fremove returned %d: appending anyway",
			  (uint32_t)FENYKL_LOG_FILE, size, FENYKL_LOG_SIZE_CAP, removed, 0, 0);
	flog_out_write();
	fw_rpc_fclose(flog.out_fp);
}

/* The card part of init: size cap, boot header. Runs once, in the first flush the gate allows. */
static int flog_init_card(void)
{
	int size, removed;

	flog.init_pending = false;
	size = flog_init_cap(&removed);
	flog.out_fp = flog_init_open(size);
	if (flog.out_fp) {
		flog_init_header(flog.t_init, size, removed);
		return 0;
	}
	flog.st.open_fail++;
	if (size < 0)
		return FENYKL_LOG_EOPEN;
	flog_append_error();
	return FENYKL_LOG_EAPPEND;
}

int fenykl_log_init(const struct fenykl_log_config *cfg)
{
	if (flog.ready)
		return FENYKL_LOG_EBUSY;
	flog.t_init = flog_init_state(cfg);
	flog.last_flush = flog.t_init;
	flog.init_pending = true;
	flog.ready = true;
	return fenykl_log_flush();
}

/* --- console tee, flusher task --- */

static void flog_tee_text(unsigned int level, const char *text, unsigned int len)
{
	fenykl_log_text(&flog_tee_mod, level, text, len);
	flog.st.tee_records++;
	flog.st.tee_slots += flog_text_slots(len);
}

static void flog_tee_line(void)
{
	flog_tee_text(FENYKL_LOG_INFO, flog.tee_line, flog.tee_len);
	flog.tee_len = 0;
}

/* Copies the capture buffer and restarts it; returns the bytes copied. */
static unsigned int flog_tee_grab(void)
{
	uint32_t psw = flog_irq_save();
	unsigned int n = FLOG_TEE_COUNT;

	if (n > FLOG_TEE_SIZE)
		n = FLOG_TEE_SIZE;
	for (unsigned int i = 0; i < n; i++)
		flog.tee_copy[i] = FLOG_TEE_BUF[i];
	FLOG_TEE_COUNT = 0;
	fw_irq_mask_restore(psw);
	return n;
}

/* Splits n copied bytes into lines; a line longer than a text record allows is cut in pieces. */
static void flog_tee_split(unsigned int n)
{
	for (unsigned int i = 0; i < n; i++) {
		uint8_t c = flog.tee_copy[i];

		if (c == '\r')
			continue;
		if (c == '\n') {
			flog_tee_line();
			continue;
		}
		flog.tee_line[flog.tee_len++] = c;
		if (flog.tee_len == sizeof(flog.tee_line))
			flog_tee_line();
	}
}

void flog_tee_poll(void)
{
	static const char off[] =
		"[tee: console capture is off (uart_state[1] >= 2): UART1 camera?]";
	static const char lost[] = "[tee overflow: console characters lost]";
	unsigned int n;

	if (FLOG_TEE_STATE1 >= 2) {
		if (!flog.tee_off)
			flog_tee_text(FENYKL_LOG_WARN, off, sizeof(off) - 1);
		flog.tee_off = true;
		return;
	}
	n = flog_tee_grab();
	flog.st.tee_bytes += n;
	if (n == FLOG_TEE_SIZE)
		flog.st.tee_overflow++;
	flog_tee_split(n);
	if (n != FLOG_TEE_SIZE)
		return;
	if (flog.tee_len)
		flog_tee_line();
	flog_tee_text(FENYKL_LOG_WARN, lost, sizeof(lost) - 1);
}

/*
 * A flush is due after flush_ms, or early when a ring is more than half full; while the card gate
 * is closed only after flush_ms (the gate is polled at that period).
 */
static bool flog_flush_due(void)
{
	volatile struct flog_ring0 *r0 = &flog.ring0;
	volatile struct flog_ring1 *r1 = flog_ring1();
	uint32_t n0 = r0->head - r0->tail, n1 = r1->head - r1->tail;
	uint32_t elapsed = FENYKL_LOG_ELAPSED(flog.last_flush, FENYKL_LOG_TIMER_NOW());

	if (elapsed >= flog.cfg.flush_ms * 1000u)
		return true;
	return !flog.gate_closed && (n0 > FLOG_RING0_RECORDS / 2 || n1 > FLOG_RING1_RECORDS / 2);
}

FENYKL_TASK(flog_flusher);
void flog_flusher(void)
{
	for (;;) {
		fw_OlyTaskSleep(flog.cfg.tee ? FLOG_TEE_POLL_MS : FLOG_IDLE_POLL_MS);
		if (flog.cfg.tee)
			flog_tee_poll();
		if (flog_flush_due())
			fenykl_log_flush();
	}
}

int fenykl_log_task_start(void)
{
	void *stack = fw_malloc(FLOG_TASK_STACK);
	int id;

	if (!stack)
		return FENYKL_LOG_ETASK;
	id = fw_OlyTaskCreate("FYKLOG", flog_flusher_task, 2, 0, FLOG_TASK_PRIO, FLOG_TASK_STACK,
			      stack);
	return id < 0 ? FENYKL_LOG_ETASK : 0;
}

void fenykl_log_set_level(struct fenykl_log_module *mod, unsigned int level)
{
	mod->level = level;
	*(volatile uint8_t *)FENYKL_UNCACHED(&mod->level) = level;
}

void fenykl_log_get_stats(struct fenykl_log_stats *out)
{
	*out = flog.st;
	out->produced[0] = flog.ring0.head;
	out->produced[1] = flog_ring1()->seq;
	out->overwritten0 = flog.ring0.overwritten;
	out->dropped1 = flog_ring1()->dropped;
	out->queued[0] = flog.ring0.head - flog.ring0.tail;
	out->queued[1] = flog_ring1()->head - flog_ring1()->tail;
	out->early = flog.early;
}
