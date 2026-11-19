/* Dev monitor helpers: frame headers, request arguments and their guards, CRC-32, the test program
   header check, PING text, the text log and the power-off steps. Pure code on stack, arguments and
   .rodata only: the CPU1 server runs it (no cached .data/.bss there). */
#include "devmon_int.h"

/* RAM: the code image and every RAM above it, both aliases. Below 0x60021000 lie the boot ROM's
   area and a guard_init range; 0x80000000.. and 0xC0000000.. are no DRAM. */
#define FDM_DRAM_LO 0x60021000u
#define FDM_DRAM_HI 0x80000000u
#define FDM_UNCACHED_OFF 0x40000000u
#define FDM_TIMEOUT_MAX 600000u		/* TPRUN, ms */
#define FDM_FILE_MAX 0x1000000u		/* PUT total */
#define FDM_PACE_MAX 1000u		/* READ pause after each piece, ms */
#define FDM_PAYLOAD_END 0x60E10000u	/* sec5: the end of the payload gap and the FENYKL task stack */
#define FDM_GET_OFF_MAX 0x7FFFFFFFu	/* fileif_fseek's absolute offset is an int */

uint32_t fdm_get32(const volatile uint8_t *p)
{
	return p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

void fdm_put32(volatile uint8_t *p, uint32_t v)
{
	p[0] = (uint8_t)v;
	p[1] = (uint8_t)(v >> 8);
	p[2] = (uint8_t)(v >> 16);
	p[3] = (uint8_t)(v >> 24);
}

int fdm_hdr_check(const uint8_t hdr[FDM_HDR_SIZE], uint32_t *op, uint32_t *id, uint32_t *len)
{
	*op = hdr[4] | (uint32_t)hdr[5] << 8;
	*id = fdm_get32(hdr + 8);
	*len = fdm_get32(hdr + 12);
	if (fdm_get32(hdr) != FDM_REQ_MAGIC || *len > FDM_IO_SIZE)
		return FDM_EFRAME;
	return 0;
}

void fdm_hdr_put(uint8_t hdr[FDM_HDR_SIZE], uint32_t op, uint32_t id, int status, uint32_t len)
{
	fdm_put32(hdr, FDM_REP_MAGIC);
	fdm_put32(hdr + 4, (op & 0xFFFFu) | (uint32_t)status << 16);
	fdm_put32(hdr + 8, id);
	fdm_put32(hdr + 12, len);
}

bool fdm_ram_ok(uint32_t addr, uint32_t len)
{
	uint32_t a = addr;

	if (len == 0 || len > FDM_READ_MAX)
		return false;
	if (a >= FDM_DRAM_LO + FDM_UNCACHED_OFF && a < FDM_DRAM_HI + FDM_UNCACHED_OFF)
		a -= FDM_UNCACHED_OFF;
	if (a < FDM_DRAM_LO || a >= FDM_DRAM_HI)
		return false;
	return len <= FDM_DRAM_HI - a;
}

bool fdm_uncached(uint32_t addr)
{
	return addr >= FDM_DRAM_LO + FDM_UNCACHED_OFF;
}

/* [lo, hi) cached run addresses WRITE never touches. */
static const uint32_t fdm_deny[][2] = {
	{ FENYKL_LOAD_BASE, FDM_PAYLOAD_END },	/* the monitor's payload, .bss, task stack */
	{ 0x6005E000u, 0x6005E21Au },		/* the loader stub (loader/patch.toml) */
	{ 0x6018D73Au, 0x6018D741u },		/* its call patch in Usr3 */
};

bool fdm_write_ok(uint32_t addr, uint32_t len)
{
	uint32_t a = addr;

	if (!fdm_ram_ok(addr, len))
		return false;
	if (fdm_uncached(a))
		a -= FDM_UNCACHED_OFF;
	for (unsigned int i = 0; i < sizeof(fdm_deny) / sizeof(fdm_deny[0]); i++)
		if (a < fdm_deny[i][1] && fdm_deny[i][0] < a + len)
			return false;
	return true;
}

static bool fdm_digit(char c)
{
	return c >= '0' && c <= '9';
}

static bool fdm_alnum(char c, bool under)
{
	return (c >= 'A' && c <= 'Z') || fdm_digit(c) || (under && c == '_');
}

/* The length of lit if s starts with it, else 0. */
static unsigned int fdm_lit(const char *s, const char *lit)
{
	unsigned int i;

	for (i = 0; lit[i]; i++)
		if (s[i] != lit[i])
			return 0;
	return i;
}

/* The run of name characters at s: 1 to max of them, else 0. */
static unsigned int fdm_run(const char *s, unsigned int max, bool under)
{
	unsigned int i;

	for (i = 0; fdm_alnum(s[i], under); i++)
		if (i == max)
			return 0;
	return i;
}

/* n digits at s. */
static bool fdm_digits_at(const char *s, unsigned int n)
{
	for (unsigned int i = 0; i < n; i++)
		if (!fdm_digit(s[i]))
			return false;
	return true;
}

/* DCIM/<dir>/<file> after "DCIM/<dir>/" at s (*end: past the name). */
static enum fdm_name fdm_photo(const char *s, unsigned int *end)
{
	static const char ext[][4] = { "JPG", "ORF", "MOV" };
	unsigned int k = fdm_run(s, 8, true);

	if (!k || s[k] != '.')
		return FDM_NAME_BAD;
	for (unsigned int i = 0; i < sizeof(ext) / sizeof(ext[0]); i++) {
		if (fdm_lit(s + k + 1, ext[i])) {
			*end = k + 4;
			return FDM_NAME_PHOTO;
		}
	}
	return FDM_NAME_BAD;
}

/* The kind of the name at the start of f; *end: its length. */
static enum fdm_name fdm_name_parse(const char *f, unsigned int *end)
{
	unsigned int i = fdm_lit(f, "FENYKL."), k;
	enum fdm_name kind;

	if (i) {
		k = fdm_run(f + i, 3, false);
		*end = i + k;
		if (!k)
			return FDM_NAME_BAD;
		if (*end == sizeof(FDM_TMP_NAME) - 1 && fdm_lit(f, FDM_TMP_NAME))
			return FDM_NAME_TMP;
		if (*end == sizeof(FDM_BIN_NAME) - 1 && fdm_lit(f, FDM_BIN_NAME))
			return FDM_NAME_BIN;
		return FDM_NAME_FENYKL;
	}
	i = fdm_lit(f, "SHOT");
	if (i) {
		if (!fdm_digits_at(f + i, 4) || !fdm_lit(f + i + 4, ".PPM"))
			return FDM_NAME_BAD;
		*end = i + 8;
		return FDM_NAME_SHOT;
	}
	i = fdm_lit(f, "DCIM");
	*end = i;
	if (!i || !f[i])
		return i ? FDM_NAME_DIR : FDM_NAME_BAD;
	if (f[i++] != '/' || !fdm_digits_at(f + i, 3) || fdm_run(f + i + 3, 5, false) != 5)
		return FDM_NAME_BAD;
	i += 8;
	*end = i;
	if (!f[i])
		return FDM_NAME_DIR;
	if (f[i++] != '/')
		return FDM_NAME_BAD;
	kind = fdm_photo(f + i, end);
	*end += i;
	return kind;
}

enum fdm_name fdm_name_kind(const char name[FDM_PATH_FIELD])
{
	unsigned int end = 0;
	enum fdm_name kind;

	/* the scans stop at this NUL at the latest */
	if (name[FDM_PATH_FIELD - 1])
		return FDM_NAME_BAD;
	kind = fdm_name_parse(name, &end);
	for (unsigned int i = end; i < FDM_PATH_FIELD; i++)
		if (name[i])
			return FDM_NAME_BAD;
	return kind;
}

int fdm_name_check(uint32_t op, const char name[FDM_PATH_FIELD])
{
	enum fdm_name kind = fdm_name_kind(name);

	switch (op) {
	case FDM_OP_FGET:
		return kind == FDM_NAME_BAD || kind == FDM_NAME_DIR ? FDM_ENAME : 0;
	case FDM_OP_FLIST:
		return kind == FDM_NAME_DIR ? 0 : FDM_ENAME;
	case FDM_OP_FPUT:
	case FDM_OP_FDEL:
		if (kind == FDM_NAME_BIN)
			return FDM_EPROTECT;
		if (kind == FDM_NAME_FENYKL || (kind == FDM_NAME_TMP && op == FDM_OP_FDEL))
			return 0;
		return FDM_ENAME;
	}
	return FDM_ENAME;
}

void fdm_path(char *out, const char name[FDM_PATH_FIELD])
{
	unsigned int i;

	out[0] = 'C';
	out[1] = ':';
	out[2] = '\\';
	for (i = 0; i < FDM_PATH_FIELD - 1 && name[i]; i++)
		out[3 + i] = name[i] == '/' ? '\\' : name[i];
	out[3 + i] = '\0';
}

/* --- CPU0 file jobs --- */

int fdm_get(const struct fdm_fileops *ops, const struct fdm_req *q, volatile uint8_t *out,
	    uint32_t *out_len)
{
	char path[FDM_PATH_MAX];
	void *fp;
	int n = 0, seek, r;

	*out_len = 0;
	r = fdm_name_check(FDM_OP_FGET, q->name);
	if (r)
		return r;
	fdm_path(path, q->name);
	fp = ops->fopen(path, "r");
	if (!fp)
		return FDM_ENOENT;
	seek = ops->fseek(fp, (int)q->a[0], FDM_SEEK_SET);
	if (seek)
		ops->err("get %s: fseek %u -> %d", (uint32_t)path, q->a[0], (uint32_t)seek);
	else
		n = ops->fread((void *)out, 1, q->a[1], fp);
	*out_len = n > 0 ? (uint32_t)n : 0;
	return ops->fclose(fp) || seek ? FDM_EIO : 0;
}

/* The path field of the entry name in dir, if it fits: the name rule then decides. */
static bool fdm_child(char out[FDM_PATH_FIELD], const char dir[FDM_PATH_FIELD], const char *name)
{
	unsigned int i, k;

	for (i = 0; i < FDM_PATH_FIELD; i++)
		out[i] = '\0';
	for (i = 0; dir[i]; i++)
		out[i] = dir[i];
	out[i++] = '/';
	for (k = 0; name[k]; k++, i++) {
		if (i >= FDM_PATH_FIELD - 1)
			return false;
		out[i] = name[k];
	}
	return true;
}

/* An entry worth listing: a DCIM/<dir> directory in DCIM, a photo file in DCIM/<dir>. */
static bool fdm_list_entry(const char dir[FDM_PATH_FIELD], const struct fdm_find *f)
{
	char path[FDM_PATH_FIELD];
	enum fdm_name kind;

	if (!fdm_child(path, dir, f->name))
		return false;
	kind = fdm_name_kind(path);
	if (kind == FDM_NAME_DIR)
		return f->attr & FDM_ATTR_DIR;
	return kind == FDM_NAME_PHOTO && !(f->attr & (FDM_ATTR_DIR | FDM_ATTR_VOLUME));
}

static void fdm_find_clear(struct fdm_find *f)
{
	uint8_t *b = (uint8_t *)f;

	for (unsigned int i = 0; i < sizeof(*f); i++)
		b[i] = 0;
}

int fdm_list(const struct fdm_fileops *ops, const struct fdm_req *q, volatile uint8_t *out,
	     uint32_t *out_len)
{
	char path[FDM_PATH_MAX];
	struct fdm_find f;
	uint32_t idx = 0, n = 0, next = 0, t0 = ops->now_us();
	unsigned int len;
	int h, r;
	bool more;

	*out_len = 0;
	r = fdm_name_check(FDM_OP_FLIST, q->name);
	if (r)
		return r;
	fdm_path(path, q->name);
	for (len = 0; path[len]; len++)
		;
	path[len] = '\\';
	path[len + 1] = '*';
	path[len + 2] = '\0';
	fdm_find_clear(&f);
	h = ops->findfirst(path, &f);
	if (h == -1)
		return FDM_ENOENT;	/* not even "." in it: no such folder */
	do {
		f.name[sizeof(f.name) - 1] = '\0';
		if (fdm_list_entry(q->name, &f)) {
			if (idx >= q->a[0]) {
				volatile uint8_t *e;

				if (n == FDM_LIST_MAX) {
					next = idx;
					break;
				}
				e = out + 4 + n * FDM_ENTRY_SIZE;
				for (unsigned int i = 0; i < FDM_NAME_FIELD; i++)
					e[i] = (uint8_t)f.name[i];
				fdm_put32(e + FDM_NAME_FIELD, f.size);
				fdm_put32(e + FDM_NAME_FIELD + 4, f.mtime);
				n++;
			}
			idx++;
		}
		fdm_find_clear(&f);
		more = ops->findnext(&f, h) == 0;
		if (more && FENYKL_TIMER_ELAPSED(t0, ops->now_us()) / 1000u >= FDM_LIST_MS) {
			r = ops->findclose(h);
			ops->err("list %s: given up after %u listed entries (findclose %d)",
				 (uint32_t)path, idx, (uint32_t)r);
			return FDM_ETIMEOUT;
		}
	} while (more);
	r = ops->findclose(h);
	if (r) {
		ops->err("list %s: findclose %d", (uint32_t)path, (uint32_t)r, 0);
		return FDM_EIO;
	}
	fdm_put32(out, next);
	*out_len = 4 + n * FDM_ENTRY_SIZE;
	return 0;
}

static const uint32_t fdm_crc_nib[16] = {
	0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C,
	0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C, 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C,
};

uint32_t fdm_crc32(uint32_t crc, const volatile uint8_t *p, uint32_t n)
{
	crc = ~crc;
	while (n--) {
		crc ^= *p++;
		crc = (crc >> 4) ^ fdm_crc_nib[crc & 0xF];
		crc = (crc >> 4) ^ fdm_crc_nib[crc & 0xF];
	}
	return ~crc;
}

int fdm_tp_check(const volatile struct fenykl_tp_hdr *h, uint32_t slot)
{
	uint32_t base = FDM_SLOT_ADDR(slot);
	uint32_t hdr = sizeof(struct fenykl_tp_hdr);

	if (h->magic != FENYKL_TP_MAGIC || h->api != FENYKL_TP_API || h->base != base)
		return FDM_ETP;
	if (h->size < hdr || h->size > h->mem || h->mem > FENYKL_TP_SLOT_SIZE)
		return FDM_ETP;
	if (h->entry < base + hdr || h->entry >= base + h->size)
		return FDM_ETP;
	if (fdm_crc32(0, (const volatile uint8_t *)h + hdr, h->size - hdr) != h->crc)
		return FDM_ETP;
	return 0;
}

/* Fixed arguments: words words of 4 bytes, then a name field of field bytes (0: none), copied
   NUL-padded into req->name and checked for the op; the rest is data (at least one byte if data,
   none otherwise). */
static int fdm_layout(struct fdm_req *req, const uint8_t *args, uint32_t len, unsigned int words,
		      unsigned int field, bool data)
{
	uint32_t fixed = words * 4 + field;

	if (words > 4 || len < fixed || (data ? len == fixed : len != fixed))
		return FDM_EARGS;
	for (unsigned int i = 0; i < words; i++)
		req->a[i] = fdm_get32(args + 4 * i);
	req->data = fixed;
	req->n = len - fixed;
	if (!field)
		return 0;
	for (unsigned int i = 0; i < FDM_PATH_FIELD; i++)
		req->name[i] = i < field ? (char)args[words * 4 + i] : '\0';
	return fdm_name_check(req->op, req->name);
}

int fdm_parse(uint32_t op, const uint8_t *args, uint32_t len, struct fdm_req *req)
{
	int r;

	req->op = (enum fdm_op)op;
	req->a[0] = req->a[1] = req->a[2] = req->a[3] = 0;
	req->name[0] = '\0';
	switch (op) {
	case FDM_OP_PING:
	case FDM_OP_TIMING:
	case FDM_OP_POWEROFF:
		return fdm_layout(req, args, len, 0, 0, false);
	case FDM_OP_READ:
		r = fdm_layout(req, args, len, len >= 12 ? len / 4 : 2, 0, false);
		if (r)
			return r;
		if (req->a[2] > FDM_READ_MAX || req->a[3] > FDM_PACE_MAX)
			return FDM_EARGS;
		if (!req->a[2]) {
			req->a[2] = FDM_PIECE;
			if (len == 8)
				req->a[3] = FDM_PACE_MS;
		}
		return fdm_ram_ok(req->a[0], req->a[1]) ? 0 : FDM_ERANGE;
	case FDM_OP_WRITE:
		r = fdm_layout(req, args, len, 1, 0, true);
		if (r)
			return r;
		return fdm_write_ok(req->a[0], req->n) ? 0 : FDM_ERANGE;
	case FDM_OP_LOG:
		return fdm_layout(req, args, len, 1, 0, false);
	case FDM_OP_TPLOAD:
		r = fdm_layout(req, args, len, 2, 0, true);
		if (r)
			return r;
		if (req->a[0] >= FENYKL_TP_SLOTS || req->a[1] > FENYKL_TP_SLOT_SIZE ||
		    req->n > FENYKL_TP_SLOT_SIZE - req->a[1])
			return FDM_ERANGE;
		return 0;
	case FDM_OP_TPRUN:
		r = fdm_layout(req, args, len, 3, 0, false);
		if (r)
			return r;
		if (req->a[0] >= FENYKL_TP_SLOTS)
			return FDM_ERANGE;
		return req->a[2] && req->a[2] <= FDM_TIMEOUT_MAX ? 0 : FDM_EARGS;
	case FDM_OP_FPUT:
		/* an empty file is one request without data */
		r = fdm_layout(req, args, len, 3, FDM_NAME_FIELD, len > 3 * 4 + FDM_NAME_FIELD);
		if (r)
			return r;
		if (req->a[1] > FDM_FILE_MAX || req->a[0] > req->a[1] ||
		    req->n > req->a[1] - req->a[0])
			return FDM_EARGS;
		return 0;
	case FDM_OP_FGET:
		/* the 12-byte name field, or the path field for DCIM names */
		r = fdm_layout(req, args, len, 2,
			       len == 2 * 4 + FDM_PATH_FIELD ? FDM_PATH_FIELD : FDM_NAME_FIELD, false);
		if (r)
			return r;
		if (req->a[0] > FDM_GET_OFF_MAX || !req->a[1] || req->a[1] > FDM_IO_DATA)
			return FDM_EARGS;
		return 0;
	case FDM_OP_FLIST:
		return fdm_layout(req, args, len, 1, FDM_PATH_FIELD, false);
	case FDM_OP_KEY:
		r = fdm_layout(req, args, len, 2, 0, false);
		if (r)
			return r;
		return req->a[0] < FDM_KEY_N && req->a[1] && req->a[1] <= FDM_HOLD_MAX ? 0 : FDM_EARGS;
	case FDM_OP_DIAL:
		r = fdm_layout(req, args, len, 1, 0, false);
		if (r)
			return r;
		return req->a[0] < FDM_DIAL_N ? 0 : FDM_EARGS;
	case FDM_OP_FDEL:
		return fdm_layout(req, args, len, 0, FDM_NAME_FIELD, false);
	}
	return FDM_EOP;
}

bool fdm_op_is_job(uint32_t op)
{
	return op == FDM_OP_WRITE || op == FDM_OP_KEY || op == FDM_OP_DIAL || op == FDM_OP_TPLOAD ||
	       op == FDM_OP_TPRUN || op == FDM_OP_FPUT || op == FDM_OP_FGET || op == FDM_OP_FDEL ||
	       op == FDM_OP_FLIST;
}

static const uint16_t fdm_key_codes[FDM_KEY_N] = {
	[FDM_KEY_OK] = 0x0004,	[FDM_KEY_UP] = 0x0005,	 [FDM_KEY_DOWN] = 0x0006,
	[FDM_KEY_LEFT] = 0x0007, [FDM_KEY_RIGHT] = 0x0008, [FDM_KEY_INFO] = 0x1205,
	[FDM_KEY_MENU] = 0x0089, [FDM_KEY_PLAY] = 0x0087, [FDM_KEY_DEL] = 0x0084,
	[FDM_KEY_FN] = 0x000B,	[FDM_KEY_FN2] = 0x1209,	 [FDM_KEY_FN3] = 0x1224,
	[FDM_KEY_REC] = 0x120A,	[FDM_KEY_HALF] = 0x0100, [FDM_KEY_FULL] = 0x0101,
};

uint32_t fdm_key_code(uint32_t key)
{
	return key < FDM_KEY_N ? fdm_key_codes[key] : 0;
}

static const uint8_t fdm_dial_values[FDM_DIAL_N] = {
	[FDM_DIAL_P] = 0x01,	 [FDM_DIAL_A] = 0x02,	 [FDM_DIAL_S] = 0x03,
	[FDM_DIAL_M] = 0x04,	 [FDM_DIAL_MOVIE] = 0x0A, [FDM_DIAL_STORY] = 0x39,
	[FDM_DIAL_SCN] = 0x2A,	 [FDM_DIAL_ART] = 0x33,	 [FDM_DIAL_IAUTO] = 0x31,
};

uint32_t fdm_dial_value(uint32_t pos)
{
	return pos < FDM_DIAL_N ? fdm_dial_values[pos] : 0;
}

unsigned int fdm_dial_set(const struct fdm_keyops *k, uint32_t pos, volatile uint8_t *out)
{
	if (pos >= FDM_DIAL_N)
		return 1;
	fdm_put32(out, k->get_state(FDM_DIAL_CODE));
	return k->inject_level(FDM_DIAL_CODE, fdm_dial_values[pos]);
}

int fdm_put_chunk(struct fdm_put *st, const struct fdm_req *q)
{
	if (q->a[0] == 0) {
		st->open = true;
		for (unsigned int i = 0; i < FDM_PATH_FIELD; i++)
			st->name[i] = q->name[i];
		st->total = q->a[1];
		st->crc = q->a[2];
		st->done = 0;
		return FDM_PUT_FIRST;
	}
	if (st->open && q->a[0] == st->done && q->a[1] == st->total && q->a[2] == st->crc) {
		unsigned int i;

		for (i = 0; i < FDM_PATH_FIELD && q->name[i] == st->name[i]; i++)
			;
		if (i == FDM_PATH_FIELD)
			return FDM_PUT_NEXT;
	}
	st->open = false;
	return FDM_EARGS;
}

/* v in base 16 or 10 into out (at most 10 digits), zero-padded to width; the length. */
static unsigned int fdm_digits(char *out, uint32_t v, uint32_t base, bool upper, unsigned int width)
{
	char tmp[10];
	unsigned int n = 0, i = 0;

	do {
		uint32_t d = v % base;

		tmp[n++] = (char)(d < 10 ? '0' + d : (upper ? 'A' : 'a') + d - 10);
		v /= base;
	} while (v);
	while (width > n && i < width - n && i < 10)
		out[i++] = '0';
	while (n)
		out[i++] = tmp[--n];
	return i;
}

unsigned int fdm_format(char *out, unsigned int size, const char *fmt, const uint32_t *args,
			unsigned int n)
{
	unsigned int o = 0, k = 0;
	char num[21];

	if (!size)
		return 0;
	for (const char *f = fmt; *f && o < size - 1; f++) {
		const char *s = num;
		unsigned int len = 0, width = 0;
		uint32_t v;

		if (*f != '%') {
			out[o++] = *f;
			continue;
		}
		f++;
		while (*f >= '0' && *f <= '9')
			width = width * 10 + (unsigned int)(*f++ - '0');
		if (*f == '%' || !*f || !(*f == 'd' || *f == 'u' || *f == 'x' || *f == 'X' ||
					  *f == 'c' || *f == 's')) {
			if (!*f)
				break;
			out[o++] = *f;
			continue;
		}
		v = k < n ? args[k++] : 0;
		switch (*f) {
		case 'd':
			if ((int32_t)v < 0) {
				num[len++] = '-';
				v = 0u - v;
			}
			len += fdm_digits(num + len, v, 10, false, width);
			break;
		case 'u':
			len = fdm_digits(num, v, 10, false, width);
			break;
		case 'x':
		case 'X':
			len = fdm_digits(num, v, 16, *f == 'X', width);
			break;
		case 'c':
			num[len++] = (char)v;
			break;
		case 's':
			s = v ? (const char *)v : "(null)";
			len = 0xFFFFFFFFu;
			break;
		}
		for (unsigned int i = 0; i < len && s[i] && o < size - 1; i++)
			out[o++] = s[i];
	}
	out[o] = '\0';
	return o;
}

unsigned int fdm_utoa(char *out, uint32_t v, bool hex)
{
	char tmp[10];
	unsigned int n = 0, i = 0;
	uint32_t base = hex ? 16 : 10;

	do {
		uint32_t d = v % base;

		tmp[n++] = (char)(d < 10 ? '0' + d : 'a' + d - 10);
		v /= base;
	} while (v);
	if (hex) {
		out[i++] = '0';
		out[i++] = 'x';
	}
	while (n)
		out[i++] = tmp[--n];
	return i;
}

static unsigned int fdm_str(char *out, const char *s)
{
	unsigned int i;

	for (i = 0; s[i]; i++)
		out[i] = s[i];
	return i;
}

static unsigned int fdm_field(char *out, const char *name, uint32_t v, bool hex)
{
	unsigned int i = fdm_str(out, name);

	return i + fdm_utoa(out + i, v, hex);
}

unsigned int fdm_ping_text(char *out, const volatile struct fdm_shared *s)
{
	unsigned int i = fdm_str(out, "devmon");

	i += fdm_field(out + i, " proto=", FDM_PROTO, false);
	i += fdm_field(out + i, " tp_api=", FENYKL_TP_API, false);
	i += fdm_field(out + i, " up_ms=", s->uptime_ms, false);
	i += fdm_field(out + i, " poweron=", s->poweron_factor, true);
	i += fdm_field(out + i, " wake=", s->wake_index, false);
	i += fdm_field(out + i, " off=", s->off_factor, false);
	i += fdm_field(out + i, " clients=", s->clients, false);
	i += fdm_field(out + i, " requests=", s->requests, false);
	i += fdm_field(out + i, " dropped=", s->dropped, false);
	i += fdm_field(out + i, " exec_busy=", s->exec_busy, false);
	i += fdm_field(out + i, " srv_stack_free=", s->stack_free, false);
	out[i++] = '\n';
	return i;
}

void fdm_text_put(volatile struct fdm_text *t, const char *s, unsigned int n)
{
	uint32_t wr = t->wr;

	for (unsigned int i = 0; i < n; i++)
		t->buf[(wr + i) % FDM_TEXT_SIZE] = s[i];
	t->wr = wr + n;
}

uint32_t fdm_text_span(uint32_t wr, uint32_t since, uint32_t *first)
{
	uint32_t keep = FDM_TEXT_SIZE - FDM_TEXT_SLACK;
	uint32_t lo = wr > keep ? wr - keep : 0;

	if (since > wr)
		since = wr;
	*first = since > lo ? since : lo;
	return wr - *first;
}

enum fdm_off_act fdm_off_step(struct fdm_off *o, uint32_t tick, bool wifi_up, uint32_t stop_calls,
			      uint32_t wlan)
{
	switch (o->state) {
	case FDM_OFF_WAIT:
		if (tick < FDM_OFF_STOP_TICK && !o->now)
			return FDM_ACT_NONE;
		if (!wifi_up) {
			o->state = FDM_OFF_READY;
			o->ready_tick = tick;
			return FDM_ACT_NONE;
		}
		o->state = FDM_OFF_STOPPING;
		o->stop_tick = tick;
		o->stop_calls = stop_calls;
		return FDM_ACT_STOP;
	case FDM_OFF_STOPPING:
		if (stop_calls != o->stop_calls || !wlan) {
			o->state = FDM_OFF_READY;
			o->ready_tick = tick;
			return FDM_ACT_STOPPED;
		}
		if (tick - o->stop_tick < FDM_OFF_STOP_TIMEOUT)
			return FDM_ACT_NONE;
		o->state = FDM_OFF_DONE;
		o->failed = true;
		return FDM_ACT_TIMEOUT;
	case FDM_OFF_READY:
		if (tick < FDM_OFF_INJECT_TICK && !o->now)
			return FDM_ACT_NONE;
		if (tick - o->ready_tick < FDM_OFF_GAP)
			return FDM_ACT_NONE;
		o->state = FDM_OFF_DONE;
		return FDM_ACT_INJECT;
	case FDM_OFF_DONE:
		break;
	}
	return FDM_ACT_NONE;
}

void fdm_off_request(struct fdm_off *o)
{
	o->now = true;
}
