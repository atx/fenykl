/*
 * Plugin configuration (sdk/docs/config.md): declaration checks, load and apply (two CRC slots
 * plus the one-shot import, one line parser for all three), carried lines, the save and its
 * saver task. CPU0 task context only; fenykl_cfg_set refuses CPU1.
 */
#include "cfg.h"
#include "log.h"

#define FCFG_VERSION_TAG "fenykl-cfg 1 seq="
#define FCFG_CRC_TAG "crc="
#define FCFG_CRC_LINE 13u		/* "crc=XXXXXXXX\n" */
#define FCFG_CRC_POLY 0xEDB88320u	/* CRC-32 (zlib), reflected */
#define FCFG_MSG_MAX 96u
#define FCFG_DEC_MAX 12u		/* "-2147483648" and NUL */
#define FCFG_TASK_PRIO FENYKL_LIB_TASK_PRIO
#define FCFG_TASK_STACK 0x1000u	/* on_change may run in this task (late load) */
#define FCFG_POLL_MS 100u
#define FCFG_RETRY_MS 10000u		/* after a failed save */
#define FCFG_MS(ms) ((ms) * 1000u)
#define FCFG_READ_NOFILE (-1)		/* fcfg_read: the file cannot be opened */
#define FCFG_READ_EIO (-2)		/* fcfg_read: rpc_fread failed */

struct fcfg_state {
	bool ready;			/* init done, declarations good */
	bool decl_bad;
	bool load_pending;		/* init ran with the card gate closed */
	bool busy;			/* a save or a late load runs */
	bool gate_closed;
	bool failed;			/* the last save failed: retry after FCFG_RETRY_MS */
	bool nospace;			/* a save hit FENYKL_CFG_ENOSPC: the saver task stops */
	uint32_t changes;		/* stores by fenykl_cfg_set */
	uint32_t saved;			/* changes at the last completed save */
	uint32_t changed_at;		/* timer_now of the last store */
	uint32_t tried_at;		/* timer_now of the last failed save */
	unsigned int raw_len;
	unsigned int out_len;		/* format: bytes in buf, > FENYKL_CFG_BUF - 1 once full */
	struct fenykl_cfg_stats st;
	uint8_t touched[FENYKL_CFG_OPTS_MAX];	/* set since boot: the RAM value beats the file */
	char raw[FENYKL_CFG_RAW];	/* carried lines, '\n'-terminated */
	char buf[FENYKL_CFG_BUF];	/* file text */
};

static struct fcfg_state fcfg;

static FENYKL_LOG_MODULE(fcfg_mod, "cfg", FENYKL_LOG_INFO);

static const char *const fcfg_paths[2] = { FENYKL_CFG_SLOT0, FENYKL_CFG_SLOT1 };

static const char *const fcfg_slot_names[] = {
	[FENYKL_CFG_SLOT_MISSING] = "missing",
	[FENYKL_CFG_SLOT_VALID] = "valid",
	[FENYKL_CFG_SLOT_TOO_LONG] = "too long",
	[FENYKL_CFG_SLOT_BAD_HEADER] = "bad header",
	[FENYKL_CFG_SLOT_BAD_CRC] = "bad or missing CRC",
	[FENYKL_CFG_SLOT_UNREADABLE] = "unreadable",
};

/* --- small helpers --- */

static unsigned int fcfg_strlen(const char *s)
{
	unsigned int n = 0;

	while (s[n])
		n++;
	return n;
}

/* s[0..n) equals the C string c. */
static bool fcfg_eq(const char *s, unsigned int n, const char *c)
{
	unsigned int i;

	for (i = 0; i < n; i++)
		if (c[i] != s[i])
			return false;
	return c[n] == 0;
}

static bool fcfg_prefix(const char *s, unsigned int n, const char *c)
{
	unsigned int i;

	for (i = 0; c[i]; i++)
		if (i >= n || c[i] != s[i])
			return false;
	return true;
}

/* line[0..len) is "name=...". */
static bool fcfg_named(const char *line, unsigned int len, const char *name, unsigned int n)
{
	return len > n && line[n] == '=' && !memcmp(line, name, n);
}

static unsigned int fcfg_put_u32(char *p, uint32_t u)
{
	char tmp[FCFG_DEC_MAX];
	unsigned int n = 0, len = 0;

	do {
		tmp[n++] = '0' + u % 10u;
		u /= 10u;
	} while (u);
	while (n)
		p[len++] = tmp[--n];
	return len;
}

static unsigned int fcfg_put_dec(char *p, int32_t v)
{
	if (v >= 0)
		return fcfg_put_u32(p, v);
	*p = '-';
	return 1 + fcfg_put_u32(p + 1, 0u - (uint32_t)v);
}

static void fcfg_put_hex8(char *p, uint32_t v)
{
	static const char digits[] = "0123456789ABCDEF";

	for (int i = 7; i >= 0; i--, v >>= 4)
		p[i] = digits[v & 15u];
}

/* Decimal digits only, at most `max`. */
static bool fcfg_parse_u32(const char *s, unsigned int n, uint32_t max, uint32_t *out)
{
	uint32_t v = 0;

	if (!n)
		return false;
	for (unsigned int i = 0; i < n; i++) {
		uint32_t d = (uint32_t)(s[i] - '0');

		if (d > 9u || v > (max - d) / 10u)
			return false;
		v = v * 10u + d;
	}
	*out = v;
	return true;
}

static bool fcfg_parse_int(const char *s, unsigned int n, int32_t *out)
{
	bool neg = n && s[0] == '-';
	uint32_t v;

	if (!fcfg_parse_u32(s + neg, n - neg, neg ? 0x80000000u : 0x7FFFFFFFu, &v))
		return false;
	*out = neg ? (int32_t)(0u - v) : (int32_t)v;
	return true;
}

static bool fcfg_parse_hex8(const char *s, uint32_t *out)
{
	uint32_t v = 0;

	for (int i = 0; i < 8; i++) {
		char c = s[i];
		uint32_t d;

		if (c >= '0' && c <= '9')
			d = c - '0';
		else if (c >= 'A' && c <= 'F')
			d = c - 'A' + 10;
		else
			return false;
		v = v << 4 | d;
	}
	*out = v;
	return true;
}

static uint32_t fcfg_crc32(const char *p, unsigned int n)
{
	uint32_t c = 0xFFFFFFFFu;

	while (n--) {
		c ^= (uint8_t)*p++;
		for (int k = 0; k < 8; k++)
			c = (c >> 1) ^ (FCFG_CRC_POLY & (0u - (c & 1u)));
	}
	return ~c;
}

static uint32_t fcfg_now(void)
{
	return FENYKL_TIMER_NOW();
}

/* Logs `what` followed by a line of file text (not NUL-terminated, may change later). */
static void fcfg_log_line(unsigned int level, const char *what, const char *line, unsigned int len)
{
	char msg[FCFG_MSG_MAX];
	unsigned int n = 0;

	if (level > fcfg_mod.level)
		return;
	while (*what && n < sizeof(msg))
		msg[n++] = *what++;
	for (unsigned int i = 0; i < len && n < sizeof(msg); i++)
		msg[n++] = line[i];
	fenykl_log_text(&fcfg_mod, level, msg, n);
}

/* --- options --- */

static unsigned int fcfg_count(void)
{
	return fenykl_cfg_end - fenykl_cfg_start;
}

static bool fcfg_is_opt(const struct fenykl_cfg_opt *o)
{
	return o >= fenykl_cfg_start && o < fenykl_cfg_end;
}

static const struct fenykl_cfg_opt *fcfg_find(const char *name, unsigned int n)
{
	const struct fenykl_cfg_opt *o;

	fenykl_cfg_for_each(o)
		if (fcfg_eq(name, n, o->name))
			return o;
	return NULL;
}

static bool fcfg_in_range(const struct fenykl_cfg_opt *o, int32_t v)
{
	return v >= o->min && v <= o->max;
}

static bool fcfg_parse_value(const struct fenykl_cfg_opt *o, const char *s, unsigned int n,
			     int32_t *v)
{
	switch (o->type) {
	case FENYKL_CFG_BOOL:
		if (fcfg_eq(s, n, "on"))
			*v = 1;
		else if (fcfg_eq(s, n, "off"))
			*v = 0;
		else
			return false;
		return true;
	case FENYKL_CFG_ENUM:
		for (int32_t i = 0; i <= o->max; i++) {
			if (fcfg_eq(s, n, o->names[i])) {
				*v = i;
				return true;
			}
		}
		return false;
	default:
		return fcfg_parse_int(s, n, v) && fcfg_in_range(o, *v);
	}
}

/* The value as the file shows it: "on"/"off", the enum name, decimal. */
static const char *fcfg_str(const struct fenykl_cfg_opt *o, char buf[FCFG_DEC_MAX])
{
	int32_t v = *o->value;

	if (o->type == FENYKL_CFG_BOOL && (v == 0 || v == 1))
		return v ? "on" : "off";
	if (o->type == FENYKL_CFG_ENUM && fcfg_in_range(o, v))
		return o->names[v];
	buf[fcfg_put_dec(buf, v)] = 0;
	return buf;
}

static void fcfg_store(const struct fenykl_cfg_opt *o, int32_t v)
{
	int32_t old = *o->value;

	*o->value = v;
	if (old != v && o->on_change)
		o->on_change(o, old);
}

/* --- declarations --- */

static bool fcfg_name_ok(const char *s)
{
	unsigned int n = fcfg_strlen(s);

	if (!n || n > FENYKL_CFG_NAME_MAX)
		return false;
	for (unsigned int i = 0; i < n; i++)
		if (s[i] <= ' ' || s[i] == '=' || s[i] > '~')
			return false;
	return true;
}

static bool fcfg_opt_ok(const struct fenykl_cfg_opt *o)
{
	if (!o->name || !fcfg_name_ok(o->name) || !o->value)
		return false;
	if (o->label && fcfg_strlen(o->label) > FENYKL_CFG_LABEL_MAX)
		return false;
	if (o->type > FENYKL_CFG_ENUM || o->min > o->max || !fcfg_in_range(o, o->def))
		return false;
	if (o->type != FENYKL_CFG_ENUM)
		return true;
	if (!o->names)
		return false;
	for (int32_t i = 0; i <= o->max; i++)
		if (!o->names[i] || !fcfg_name_ok(o->names[i]))
			return false;
	return true;
}

/* Logs every bad declaration; 0 or FENYKL_CFG_EDECL. */
static int fcfg_check_decls(void)
{
	const struct fenykl_cfg_opt *o, *p;
	int ret = 0;

	if (fcfg_count() > FENYKL_CFG_OPTS_MAX) {
		fenykl_log_err(&fcfg_mod, "%u options, at most %u", fcfg_count(), FENYKL_CFG_OPTS_MAX);
		return FENYKL_CFG_EDECL;
	}
	fenykl_cfg_for_each(o) {
		if (!fcfg_opt_ok(o)) {
			fenykl_log_err(&fcfg_mod, "option %u (%s): bad declaration (name <= %u, "
				       "label <= %u, def in range, enum names)",
				       o - fenykl_cfg_start, o->name ? o->name : "?",
				       FENYKL_CFG_NAME_MAX, FENYKL_CFG_LABEL_MAX);
			ret = FENYKL_CFG_EDECL;
			continue;
		}
		for (p = fenykl_cfg_start; p < o; p++) {
			if (p->name && fcfg_eq(o->name, fcfg_strlen(o->name), p->name)) {
				fenykl_log_err(&fcfg_mod, "option %s declared twice", o->name);
				ret = FENYKL_CFG_EDECL;
			}
		}
	}
	return ret;
}

/* --- carried lines (unknown names, invalid values) --- */

/* Offset of the carried line for name, its length (without '\n') in *len; -1 if none. */
static int fcfg_raw_find(const char *name, unsigned int n, unsigned int *len)
{
	unsigned int p = 0, q;

	while (p < fcfg.raw_len) {
		for (q = p; fcfg.raw[q] != '\n'; q++)
			;
		if (fcfg_named(fcfg.raw + p, q - p, name, n)) {
			*len = q - p;
			return p;
		}
		p = q + 1;
	}
	return -1;
}

static void fcfg_raw_drop(const char *name, unsigned int n)
{
	unsigned int len;
	int p = fcfg_raw_find(name, n, &len);

	if (p < 0)
		return;
	memmove(fcfg.raw + p, fcfg.raw + p + len + 1, fcfg.raw_len - (p + len + 1));
	fcfg.raw_len -= len + 1;
}

/* Carries line (replacing a carried line of the same name). Full: logged, saving refused. */
static void fcfg_raw_put(const char *line, unsigned int len, unsigned int name_len)
{
	fcfg_raw_drop(line, name_len);
	if (fcfg.raw_len + len + 1 > FENYKL_CFG_RAW) {
		if (!fcfg.st.raw_overflow)
			fenykl_log_err(&fcfg_mod, "carried lines exceed %u bytes: saving is refused "
				       "in this boot (nothing is dropped)", FENYKL_CFG_RAW);
		fcfg.st.raw_overflow = true;
		fcfg_log_line(FENYKL_LOG_ERROR, "does not fit: ", line, len);
		return;
	}
	memcpy(fcfg.raw + fcfg.raw_len, line, len);
	fcfg.raw[fcfg.raw_len + len] = '\n';
	fcfg.raw_len += len + 1;
}

/* --- apply (slot bodies and the import) --- */

/* An earlier line of the same text in buf[start..end) has this name. */
static bool fcfg_seen(unsigned int start, unsigned int end, const char *name, unsigned int n)
{
	const char *b = fcfg.buf;

	for (unsigned int p = start; p < end; p++) {
		if (p != start && b[p - 1] != '\n')
			continue;
		if (fcfg_named(b + p, end - p, name, n))
			return true;
	}
	return false;
}

/* Applies one line buf[p..q) of the text that starts at `start`. */
static void fcfg_apply_line(unsigned int start, unsigned int p, unsigned int q)
{
	const char *line = fcfg.buf + p;
	const struct fenykl_cfg_opt *o;
	unsigned int len = q - p, eq;
	int32_t v;

	if (len && line[len - 1] == '\r')
		len--;
	if (!len)
		return;
	for (eq = 0; eq < len && line[eq] != '='; eq++)
		;
	if (eq == 0 || eq == len) {
		fcfg.st.malformed++;
		fcfg_log_line(FENYKL_LOG_WARN, "malformed line dropped: ", line, len);
		return;
	}
	if (fcfg_seen(start, p, line, eq)) {
		fcfg.st.duplicates++;
		fcfg_log_line(FENYKL_LOG_WARN, "name repeated, the last wins: ", line, len);
	}
	o = fcfg_find(line, eq);
	if (!o) {
		fcfg.st.unknown++;
		fcfg_log_line(FENYKL_LOG_INFO, "unknown name, carried: ", line, len);
		fcfg_raw_put(line, len, eq);
		return;
	}
	if (fcfg.touched[o - fenykl_cfg_start]) {
		fcfg_log_line(FENYKL_LOG_INFO, "set since boot, file value ignored: ", line, len);
		return;
	}
	if (!fcfg_parse_value(o, line + eq + 1, len - eq - 1, &v)) {
		fcfg.st.invalid++;
		fcfg_log_line(FENYKL_LOG_WARN, "invalid value, default used, line carried: ", line,
			      len);
		fcfg_raw_put(line, len, eq);
		fcfg_store(o, o->def);
		return;
	}
	fcfg_raw_drop(line, eq);
	fcfg.st.applied++;
	fcfg_store(o, v);
}

static void fcfg_apply_text(unsigned int start, unsigned int end)
{
	unsigned int p, q;

	for (p = start; p < end; p = q + 1) {
		for (q = p; q < end && fcfg.buf[q] != '\n'; q++)
			;
		fcfg_apply_line(start, p, q);
	}
}

/* --- load --- */

/* Reads a file into buf: its length, FCFG_READ_NOFILE or FCFG_READ_EIO (logged). */
static int fcfg_read(const char *path)
{
	void *fp = fw_rpc_fopen(path, "r");
	int n;

	if (!fp)
		return FCFG_READ_NOFILE;
	/* No dirty line of buf may be written back over what the file server stores. */
	fenykl_dcache_flush();
	n = fw_rpc_fread(FENYKL_UNCACHED(fcfg.buf), 1, FENYKL_CFG_BUF, fp);
	fw_rpc_fclose(fp);
	if (n < 0) {
		fenykl_log_err(&fcfg_mod, "%s: rpc_fread returned %d", path, n);
		return FCFG_READ_EIO;
	}
	return n;
}

/* Checks a slot text of n bytes in buf: header, CRC; *body.. *end is the option text. */
static enum fenykl_cfg_slot_state fcfg_check(int n, uint32_t *seq, unsigned int *body,
					     unsigned int *end)
{
	const char *b = fcfg.buf;
	unsigned int tag = sizeof(FCFG_VERSION_TAG) - 1, crc_at, h;
	uint32_t crc;

	if (n == FCFG_READ_NOFILE)
		return FENYKL_CFG_SLOT_MISSING;
	if (n == FCFG_READ_EIO)
		return FENYKL_CFG_SLOT_UNREADABLE;
	if (n >= (int)FENYKL_CFG_BUF)
		return FENYKL_CFG_SLOT_TOO_LONG;
	crc_at = n - FCFG_CRC_LINE;
	if (n < (int)FCFG_CRC_LINE || b[n - 1] != '\n' || (crc_at && b[crc_at - 1] != '\n') ||
	    !fcfg_prefix(b + crc_at, FCFG_CRC_LINE, FCFG_CRC_TAG) ||
	    !fcfg_parse_hex8(b + crc_at + 4, &crc) || crc != fcfg_crc32(b, crc_at))
		return FENYKL_CFG_SLOT_BAD_CRC;
	for (h = 0; h < crc_at && b[h] != '\n'; h++)
		;
	if (h == crc_at || !fcfg_prefix(b, h, FCFG_VERSION_TAG) ||
	    !fcfg_parse_u32(b + tag, h - tag, 0xFFFFFFFFu, seq))
		return FENYKL_CFG_SLOT_BAD_HEADER;
	*body = h + 1;
	*end = crc_at;
	return FENYKL_CFG_SLOT_VALID;
}

static enum fenykl_cfg_slot_state fcfg_read_slot(int s, uint32_t *seq, unsigned int *body,
						 unsigned int *end)
{
	int n = fcfg_read(fcfg_paths[s]);
	enum fenykl_cfg_slot_state st = fcfg_check(n, seq, body, end);

	fcfg.st.slot_state[s] = st;
	fcfg.st.slot_seq[s] = st == FENYKL_CFG_SLOT_VALID ? *seq : 0;
	fenykl_log_info(&fcfg_mod, "%s: %d bytes, %s, seq %u", fcfg_paths[s], n,
			fcfg_slot_names[st], fcfg.st.slot_seq[s]);
	return st;
}

/* The valid slot with the higher seq (wrap-safe), -1 if none. */
static int fcfg_pick(void)
{
	const struct fenykl_cfg_stats *s = &fcfg.st;
	bool v0 = s->slot_state[0] == FENYKL_CFG_SLOT_VALID;
	bool v1 = s->slot_state[1] == FENYKL_CFG_SLOT_VALID;

	if (v0 && v1)
		return (int32_t)(s->slot_seq[1] - s->slot_seq[0]) > 0 ? 1 : 0;
	if (v0)
		return 0;
	return v1 ? 1 : -1;
}

static void fcfg_mark_dirty(void)
{
	fcfg.changed_at = fcfg_now();
	fcfg.changes++;
}

static void fcfg_import(void)
{
	int n = fcfg_read(FENYKL_CFG_IMPORT);

	if (n == FCFG_READ_NOFILE)
		return;
	if (n == FCFG_READ_EIO) {
		fenykl_log_err(&fcfg_mod, "%s: read failed: not imported, not deleted",
			       FENYKL_CFG_IMPORT);
		return;
	}
	if (n >= (int)FENYKL_CFG_BUF) {
		fenykl_log_err(&fcfg_mod, "%s is %u bytes or more: not imported, not deleted",
			       FENYKL_CFG_IMPORT, FENYKL_CFG_BUF);
		return;
	}
	fenykl_log_info(&fcfg_mod, "%s: %d bytes, importing", FENYKL_CFG_IMPORT, n);
	fcfg_apply_text(0, n);
	fcfg.st.imported = true;
	fcfg.st.import_pending = true;
	fcfg_mark_dirty();
}

static void fcfg_log_counts(void)
{
	fenykl_log_info(&fcfg_mod, "applied %u, invalid %u, unknown %u, duplicates %u",
			fcfg.st.applied, fcfg.st.invalid, fcfg.st.unknown, fcfg.st.duplicates);
	fenykl_log_info(&fcfg_mod, "malformed %u, carried %u bytes%s", fcfg.st.malformed,
			fcfg.raw_len, fcfg.st.raw_overflow ? " (overflow: saving refused)" : "");
}

/* Loads the newer valid slot and the import: 0, or FENYKL_CFG_ENOFILE without a valid slot. */
static int fcfg_load(void)
{
	uint32_t seq[2];
	unsigned int body[2], end[2];
	int best, ret = 0;

	fcfg_read_slot(0, &seq[0], &body[0], &end[0]);
	fcfg_read_slot(1, &seq[1], &body[1], &end[1]);
	best = fcfg_pick();
	if (best == 0 && fcfg_read_slot(0, &seq[0], &body[0], &end[0]) != FENYKL_CFG_SLOT_VALID) {
		fenykl_log_err(&fcfg_mod, "%s changed between two reads", fcfg_paths[0]);
		best = -1;
	}
	if (best < 0) {
		fenykl_log_info(&fcfg_mod, "no valid file: defaults");
		ret = FENYKL_CFG_ENOFILE;
	} else {
		fcfg.st.slot = best;
		fcfg.st.seq = seq[best];
		fenykl_log_info(&fcfg_mod, "loading %s seq %u, CRC ok", fcfg_paths[best], seq[best]);
		fcfg_apply_text(body[best], end[best]);
	}
	fcfg_import();
	fcfg_log_counts();
	return ret;
}

/* --- card gate --- */

/* Logs the transitions; once a cable was seen the gate stays closed until power-off. */
static bool fcfg_gate_open(void)
{
	if (fenykl_card_io_allowed()) {
		if (fcfg.gate_closed)
			fenykl_log_info(&fcfg_mod, "card gate open again");
		fcfg.gate_closed = false;
		return true;
	}
	if (!fcfg.gate_closed) {
		fcfg.st.gate_holds++;
		if (!fenykl_card_usb_seen())
			fenykl_log_warn(&fcfg_mod, "card gate closed (card not mounted): "
					"config load/save on hold");
	}
	fcfg.gate_closed = true;
	return false;
}

/* --- save --- */

static void fcfg_out(const char *s, unsigned int n)
{
	if (fcfg.out_len + n > FENYKL_CFG_BUF - 1) {
		fcfg.out_len = FENYKL_CFG_BUF;
		return;
	}
	memcpy(fcfg.buf + fcfg.out_len, s, n);
	fcfg.out_len += n;
}

static void fcfg_out_str(const char *s)
{
	fcfg_out(s, fcfg_strlen(s));
}

/* An option with a carried line keeps that line until it is set. */
static bool fcfg_carried(const struct fenykl_cfg_opt *o)
{
	unsigned int len;

	if (fcfg.touched[o - fenykl_cfg_start])
		return false;
	return fcfg_raw_find(o->name, fcfg_strlen(o->name), &len) >= 0;
}

static void fcfg_out_raw(void)
{
	unsigned int p, q, eq;

	for (p = 0; p < fcfg.raw_len; p = q + 1) {
		const struct fenykl_cfg_opt *o;

		for (q = p; fcfg.raw[q] != '\n'; q++)
			;
		for (eq = p; fcfg.raw[eq] != '='; eq++)
			;
		o = fcfg_find(fcfg.raw + p, eq - p);
		if (o && fcfg.touched[o - fenykl_cfg_start])
			continue;
		fcfg_out(fcfg.raw + p, q - p + 1);
	}
}

/* Formats the file with seq into buf: its length, or 0 if it does not fit. */
static unsigned int fcfg_format(uint32_t seq)
{
	const struct fenykl_cfg_opt *o;
	char s[FCFG_DEC_MAX], crc[8];

	fcfg.out_len = 0;
	fcfg_out_str(FCFG_VERSION_TAG);
	fcfg_out(s, fcfg_put_u32(s, seq));
	fcfg_out("\n", 1);
	fenykl_cfg_for_each(o) {
		if (fcfg_carried(o))
			continue;
		fcfg_out_str(o->name);
		fcfg_out("=", 1);
		fcfg_out_str(fcfg_str(o, s));
		fcfg_out("\n", 1);
	}
	fcfg_out_raw();
	if (fcfg.out_len + FCFG_CRC_LINE > FENYKL_CFG_BUF - 1)
		return 0;
	fcfg_put_hex8(crc, fcfg_crc32(fcfg.buf, fcfg.out_len));
	fcfg_out_str(FCFG_CRC_TAG);
	fcfg_out(crc, 8);
	fcfg_out("\n", 1);
	return fcfg.out_len;
}

/*
 * Removes the slot file and writes buf[0..len) as a new one. "w" does not truncate an existing
 * file, so a file that is still there after the remove aborts the save.
 */
static int fcfg_write_slot(int slot, unsigned int len)
{
	const char *path = fcfg_paths[slot];
	int r = fw_rpc_fremove(path), n, c;
	void *fp;

	fcfg.st.last_remove = r;
	if (r) {
		fp = fw_rpc_fopen(path, "r");
		if (fp) {
			fw_rpc_fclose(fp);
			fenykl_log_err(&fcfg_mod, "rpc_fremove(%s) returned %d and the file is still "
				       "there: not written", path, r);
			return FENYKL_CFG_EIO;
		}
	}
	fenykl_log_info(&fcfg_mod, "rpc_fremove(%s) returned %d%s", path, r,
			r ? " (no file)" : " (removed)");
	fp = fw_rpc_fopen(path, "w");
	if (!fp) {
		fenykl_log_err(&fcfg_mod, "rpc_fopen(%s, \"w\") failed", path);
		return FENYKL_CFG_EIO;
	}
	/* The file server reads DRAM: write buf back first. */
	fenykl_dcache_flush();
	n = fw_rpc_fwrite(FENYKL_UNCACHED(fcfg.buf), 1, len, fp);
	c = fw_rpc_fclose(fp);
	if (n != (int)len || c) {
		fenykl_log_err(&fcfg_mod, "%s: rpc_fwrite %d of %u bytes, rpc_fclose %d", path, n,
			       len, c);
		return FENYKL_CFG_EIO;
	}
	return 0;
}

static void fcfg_import_remove(void)
{
	int r = fw_rpc_fremove(FENYKL_CFG_IMPORT);

	if (r) {
		fenykl_log_err(&fcfg_mod, "rpc_fremove(%s) returned %d: kept, imported again at "
			       "the next boot", FENYKL_CFG_IMPORT, r);
		return;
	}
	fcfg.st.import_pending = false;
	fenykl_log_info(&fcfg_mod, "%s deleted after the save", FENYKL_CFG_IMPORT);
}

static int fcfg_save_dirty(void)
{
	uint32_t changes = fcfg.changes;
	uint32_t seq = fcfg.st.seq + 1;
	int slot = fcfg.st.slot == 0 ? 1 : 0;
	unsigned int len;
	int r;

	if (fcfg.st.raw_overflow)
		return FENYKL_CFG_ENOSPC;
	len = fcfg_format(seq);
	if (!len) {
		fenykl_log_err(&fcfg_mod, "the options do not fit %u bytes: not saved",
			       FENYKL_CFG_BUF);
		return FENYKL_CFG_ENOSPC;
	}
	r = fcfg_write_slot(slot, len);
	if (r)
		return r;
	fcfg.saved = changes;
	fcfg.st.slot = slot;
	fcfg.st.seq = seq;
	fcfg.st.saves++;
	fcfg.st.last_bytes = len;
	fenykl_log_info(&fcfg_mod, "saved %s seq %u, %u bytes, remove %d", fcfg_paths[slot], seq,
			len, fcfg.st.last_remove);
	if (fcfg.st.import_pending)
		fcfg_import_remove();
	return 0;
}

static int fcfg_save_claimed(void)
{
	int r;

	if (!fcfg_gate_open())
		return FENYKL_CFG_ENOCARD;
	if (fcfg.load_pending) {
		fcfg.load_pending = false;
		fenykl_log_info(&fcfg_mod, "loading late (card gate was closed at init)");
		fcfg_load();
	}
	if (!fenykl_cfg_dirty())
		return 0;
	r = fcfg_save_dirty();
	if (r) {
		fcfg.st.save_fail++;
		fcfg.failed = true;
		fcfg.tried_at = fcfg_now();
		fcfg.nospace = r == FENYKL_CFG_ENOSPC;
		return r;
	}
	fcfg.failed = false;
	return 0;
}

static bool fcfg_claim(void)
{
	uint32_t psw = fw_psw_get();
	bool taken;

	fw_irq_disable_all();
	taken = fcfg.ready && !fcfg.busy;
	if (taken)
		fcfg.busy = true;
	fw_irq_mask_restore(psw);
	return taken;
}

int fenykl_cfg_save(void)
{
	int r;

	if (fcfg.decl_bad)
		return FENYKL_CFG_EDECL;
	if (!fcfg_claim())
		return FENYKL_CFG_EBUSY;
	r = fcfg_save_claimed();
	fcfg.busy = false;
	return r;
}

bool fenykl_cfg_dirty(void)
{
	return fcfg.changes != fcfg.saved;
}

/* --- set --- */

int fenykl_cfg_set(const struct fenykl_cfg_opt *o, int32_t v)
{
	unsigned int len;
	bool carried;

	if (FENYKL_CORE_ID() != 0) {
		fenykl_log_err(&fcfg_mod, "fenykl_cfg_set on CPU1 refused (%08x = %d)", (uint32_t)o, v);
		return FENYKL_CFG_ECONTEXT;
	}
	if (!fcfg_is_opt(o)) {
		fenykl_log_err(&fcfg_mod, "fenykl_cfg_set: %08x is no option", (uint32_t)o);
		return FENYKL_CFG_EINVAL;
	}
	if (!fcfg_in_range(o, v)) {
		fenykl_log_warn(&fcfg_mod, "%s: %d outside %d..%d", o->name, v, o->min, o->max);
		return FENYKL_CFG_EINVAL;
	}
	carried = fcfg_raw_find(o->name, fcfg_strlen(o->name), &len) >= 0;
	fcfg.touched[o - fenykl_cfg_start] = 1;
	if (*o->value == v && !carried)
		return 0;
	fcfg_mark_dirty();
	fcfg_store(o, v);
	return 0;
}

/* --- init and the saver task --- */

int fenykl_cfg_init(void)
{
	if (fcfg.ready || fcfg.decl_bad)
		return FENYKL_CFG_EBUSY;
	fcfg.st.slot = -1;
	if (fcfg_check_decls()) {
		fcfg.decl_bad = true;
		fenykl_log_err(&fcfg_mod, "bad declarations: nothing is loaded or saved");
		return FENYKL_CFG_EDECL;
	}
	fcfg.ready = true;
	fenykl_log_info(&fcfg_mod, "%u options", fcfg_count());
	if (!fcfg_gate_open()) {
		fcfg.load_pending = true;
		return FENYKL_CFG_ENOCARD;
	}
	return fcfg_load();
}

static bool fcfg_save_due(void)
{
	uint32_t now = fcfg_now();

	if (fcfg.load_pending)
		return true;
	if (fcfg.nospace || !fenykl_cfg_dirty())
		return false;
	if (FENYKL_TIMER_ELAPSED(fcfg.changed_at, now) < FCFG_MS(FENYKL_CFG_SAVE_MS))
		return false;
	return !fcfg.failed || FENYKL_TIMER_ELAPSED(fcfg.tried_at, now) >= FCFG_MS(FCFG_RETRY_MS);
}

FENYKL_TASK(fcfg_saver);
void fcfg_saver(void)
{
	for (;;) {
		fw_OlyTaskSleep(FCFG_POLL_MS);
		if (!fcfg_save_due())
			continue;
		if (fenykl_cfg_save() == FENYKL_CFG_ENOSPC)
			fenykl_log_err(&fcfg_mod, "not saving any more in this boot (no space)");
	}
}

int fenykl_cfg_task_start(void)
{
	void *stack = fw_malloc(FCFG_TASK_STACK);
	int id;

	if (!stack)
		return FENYKL_CFG_ETASK;
	id = fw_OlyTaskCreate("FYKCFG", fcfg_saver_task, 2, 0, FCFG_TASK_PRIO, FCFG_TASK_STACK,
			      stack);
	if (id < 0) {
		fw_free(stack);
		return FENYKL_CFG_ETASK;
	}
	return 0;
}

void fenykl_cfg_get_stats(struct fenykl_cfg_stats *out)
{
	*out = fcfg.st;
	out->raw_bytes = fcfg.raw_len;
}
