/*
 * The dev monitor's protocol helpers (sdk/lib/devmon_proto.c) in the GDB simulator: frame headers,
 * every op's arguments, the RAM ranges at every edge, the name rule of every file op (a table), the
 * CRC-32, the test program header check, the PING text, the text log's span, the power-off
 * steps, the FENYKL.BIN refusal, the WRITE deny ranges, the bounded formatter, PUT sequencing and
 * the CPU0 GET and LIST jobs against a fake card. plugin_main sets sim_exit_code to 0 or the number
 * of the first failed check; a failed name rule row is also printed.
 */
#include <fenykl.h>
#include "../../../sdk/lib/devmon_int.h"

uint32_t sim_exit_code = 99;
static uint32_t first_fail;

void sim_write(const char *buf, unsigned int len);

/* The wire layout the host client (py/fenykl/dev.py) sends and parses. */
_Static_assert(FDM_NAME_FIELD == 12 && FDM_PATH_FIELD == 32, "name fields");
_Static_assert(FDM_IO_DATA == 0x4000 && FDM_ENTRY_SIZE == 20 && FDM_LIST_MAX == 819, "FLIST");
_Static_assert(FDM_OP_FGET == 0x21 && FDM_OP_FLIST == 0x23, "ops");

#define CHECK(n, cond)                          \
	do {                                    \
		if (!(cond) && !first_fail)     \
			first_fail = (n);       \
	} while (0)

static unsigned int t_len(const char *s)
{
	unsigned int n = 0;

	while (s[n])
		n++;
	return n;
}

static bool t_eq(const char *a, unsigned int n, const char *b)
{
	if (n != t_len(b))
		return false;
	for (unsigned int i = 0; i < n; i++)
		if (a[i] != b[i])
			return false;
	return true;
}

/* A request payload: up to 4 words, then (name) a NUL-padded name field of field bytes, then data
   bytes. */
static uint8_t pay[80];

static uint32_t t_pay(unsigned int words, const uint32_t *w, const char *name, unsigned int field,
		      unsigned int data)
{
	uint32_t n = 0;

	for (unsigned int i = 0; i < words; i++, n += 4)
		fdm_put32(pay + n, w[i]);
	if (name) {
		for (unsigned int i = 0; i < field; i++)
			pay[n + i] = 0;
		for (unsigned int i = 0; name[i] && i < field; i++)
			pay[n + i] = (uint8_t)name[i];
		n += field;
	}
	for (unsigned int i = 0; i < data; i++)
		pay[n++] = 0xA5;
	return n;
}

static int t_parse(uint32_t op, unsigned int words, const uint32_t *w, const char *name,
		   unsigned int data, struct fdm_req *q)
{
	uint32_t len = t_pay(words, w, name, FDM_NAME_FIELD, data);

	return fdm_parse(op, pay, len, q);
}

/* The same with the path field. */
static int t_parse_path(uint32_t op, unsigned int words, const uint32_t *w, const char *name,
			struct fdm_req *q)
{
	uint32_t len = t_pay(words, w, name, FDM_PATH_FIELD, 0);

	return fdm_parse(op, pay, len, q);
}

static void t_frames(void)
{
	uint8_t h[FDM_HDR_SIZE];
	uint32_t op, id, len;

	fdm_put32(h, FDM_REQ_MAGIC);
	fdm_put32(h + 4, 0x21);
	fdm_put32(h + 8, 77);
	fdm_put32(h + 12, FDM_IO_SIZE);
	CHECK(1, fdm_hdr_check(h, &op, &id, &len) == 0 && op == 0x21 && id == 77 &&
			 len == FDM_IO_SIZE);
	fdm_put32(h + 12, FDM_IO_SIZE + 1);
	CHECK(2, fdm_hdr_check(h, &op, &id, &len) == FDM_EFRAME);
	fdm_put32(h + 12, 0);
	fdm_put32(h, FDM_REP_MAGIC);
	CHECK(3, fdm_hdr_check(h, &op, &id, &len) == FDM_EFRAME);
	fdm_hdr_put(h, FDM_OP_FGET, 0x12345678u, FDM_ENAME, 9);
	CHECK(4, fdm_get32(h) == FDM_REP_MAGIC && h[4] == 0x21 && h[5] == 0 &&
			 (int16_t)(h[6] | h[7] << 8) == FDM_ENAME && fdm_get32(h + 8) == 0x12345678u &&
			 fdm_get32(h + 12) == 9);
}

static void t_ops(void)
{
	struct fdm_req q;
	uint32_t w[4];

	CHECK(10, t_parse(FDM_OP_PING, 0, w, NULL, 0, &q) == 0);
	CHECK(11, t_parse(FDM_OP_PING, 0, w, NULL, 1, &q) == FDM_EARGS);
	CHECK(12, t_parse(0x05, 0, w, NULL, 0, &q) == FDM_EOP &&
			  t_parse(0xFFFF, 0, w, NULL, 0, &q) == FDM_EOP);
	w[0] = 0x60021020;
	w[1] = 64;
	CHECK(13, t_parse(FDM_OP_READ, 2, w, NULL, 0, &q) == 0 && q.a[0] == 0x60021020 &&
			  q.a[1] == 64 && q.a[2] == 0x2000 && q.a[3] == 1);
	w[2] = 1460;
	w[3] = 2;
	CHECK(14, t_parse(FDM_OP_READ, 4, w, NULL, 0, &q) == 0 && q.a[2] == 1460 && q.a[3] == 2);
	CHECK(15, t_parse(FDM_OP_READ, 1, w, NULL, 0, &q) == FDM_EARGS &&
			  t_parse(FDM_OP_READ, 2, w, NULL, 1, &q) == FDM_EARGS);
	w[3] = 1001;
	CHECK(16, t_parse(FDM_OP_READ, 4, w, NULL, 0, &q) == FDM_EARGS);
	w[0] = 0x98000000;
	CHECK(17, t_parse(FDM_OP_READ, 2, w, NULL, 0, &q) == FDM_ERANGE);
	/* WRITE: RAM only, data needed */
	w[0] = 0xA0F8A800;
	CHECK(18, t_parse(FDM_OP_WRITE, 1, w, NULL, 4, &q) == 0 && q.data == 4 && q.n == 4);
	CHECK(19, t_parse(FDM_OP_WRITE, 1, w, NULL, 0, &q) == FDM_EARGS);
	w[0] = 0x98E00000;
	CHECK(20, t_parse(FDM_OP_WRITE, 1, w, NULL, 4, &q) == FDM_ERANGE);
	w[0] = 0x7FFFFFFE;
	CHECK(21, t_parse(FDM_OP_WRITE, 1, w, NULL, 2, &q) == 0 &&
			  t_parse(FDM_OP_WRITE, 1, w, NULL, 3, &q) == FDM_ERANGE);
	w[0] = 0x6001FFFC;
	CHECK(22, t_parse(FDM_OP_WRITE, 1, w, NULL, 4, &q) == FDM_ERANGE);
	/* LOG, TIMING, POWEROFF */
	CHECK(23, t_parse(FDM_OP_LOG, 1, w, NULL, 0, &q) == 0 &&
			  t_parse(FDM_OP_LOG, 0, w, NULL, 0, &q) == FDM_EARGS);
	CHECK(24, t_parse(FDM_OP_TIMING, 0, w, NULL, 0, &q) == 0 &&
			  t_parse(FDM_OP_POWEROFF, 0, w, NULL, 0, &q) == 0 &&
			  t_parse(FDM_OP_POWEROFF, 1, w, NULL, 0, &q) == FDM_EARGS);
	/* TPLOAD: inside one slot */
	w[0] = FENYKL_TP_SLOTS - 1;
	w[1] = FENYKL_TP_SLOT_SIZE - 8;
	CHECK(25, t_parse(FDM_OP_TPLOAD, 2, w, NULL, 8, &q) == 0 && q.data == 8 && q.n == 8);
	CHECK(26, t_parse(FDM_OP_TPLOAD, 2, w, NULL, 9, &q) == FDM_ERANGE);
	w[0] = FENYKL_TP_SLOTS;
	w[1] = 0;
	CHECK(27, t_parse(FDM_OP_TPLOAD, 2, w, NULL, 8, &q) == FDM_ERANGE);
	w[0] = 0;
	w[1] = 0xFFFFFFF0u;
	CHECK(28, t_parse(FDM_OP_TPLOAD, 2, w, NULL, 0x20, &q) == FDM_ERANGE);
	/* TPRUN */
	w[0] = 3;
	w[1] = 0xABCD;
	w[2] = 5000;
	CHECK(29, t_parse(FDM_OP_TPRUN, 3, w, NULL, 0, &q) == 0 && q.a[1] == 0xABCD);
	w[2] = 0;
	CHECK(30, t_parse(FDM_OP_TPRUN, 3, w, NULL, 0, &q) == FDM_EARGS);
	w[2] = 600001;
	CHECK(31, t_parse(FDM_OP_TPRUN, 3, w, NULL, 0, &q) == FDM_EARGS);
	w[0] = FENYKL_TP_SLOTS;
	w[2] = 1;
	CHECK(32, t_parse(FDM_OP_TPRUN, 3, w, NULL, 0, &q) == FDM_ERANGE);
	/* FPUT: offset, total, crc, name, data */
	w[0] = 0;
	w[1] = 10;
	w[2] = 0x1234;
	CHECK(33, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.DAT", 10, &q) == 0 && q.data == 24 &&
			  q.n == 10 && t_eq(q.name, t_len(q.name), "FENYKL.DAT"));
	CHECK(34, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.DAT", 11, &q) == FDM_EARGS);
	w[0] = 4;
	CHECK(35, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.DAT", 6, &q) == 0 &&
			  t_parse(FDM_OP_FPUT, 3, w, "FENYKL.DAT", 7, &q) == FDM_EARGS);
	w[0] = 0;
	w[1] = 0;
	CHECK(36, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.TST", 0, &q) == 0 && q.n == 0);
	CHECK(37, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.TMP", 0, &q) == FDM_ENAME);
	CHECK(38, t_parse(FDM_OP_FPUT, 3, w, "DCOLYMP", 0, &q) == FDM_ENAME);
	w[1] = 0x1000001;
	CHECK(39, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.DAT", 1, &q) == FDM_EARGS);
	/* FGET: offset, len, name; FDEL: name */
	w[0] = 0;
	w[1] = FDM_IO_DATA;
	CHECK(40, t_parse(FDM_OP_FGET, 2, w, "FENYKL.LOG", 0, &q) == 0);
	w[1] = FDM_IO_DATA + 1;
	CHECK(41, t_parse(FDM_OP_FGET, 2, w, "FENYKL.LOG", 0, &q) == FDM_EARGS);
	w[1] = 0;
	CHECK(42, t_parse(FDM_OP_FGET, 2, w, "FENYKL.LOG", 0, &q) == FDM_EARGS);
	w[1] = 16;
	CHECK(43, t_parse(FDM_OP_FGET, 2, w, "OLYMPUS.BIN", 0, &q) == FDM_ENAME);
	w[0] = 0x7FFFFFFFu;
	CHECK(46, t_parse(FDM_OP_FGET, 2, w, "FENYKL.LOG", 0, &q) == 0);
	w[0] = 0x80000000u;
	CHECK(47, t_parse(FDM_OP_FGET, 2, w, "FENYKL.LOG", 0, &q) == FDM_EARGS);
	/* FGET's path field: DCIM names need it, short names work in either field */
	w[0] = 0x123456;
	CHECK(48, t_parse_path(FDM_OP_FGET, 2, w, "DCIM/100OLYMP/P1180001.JPG", &q) == 0 &&
			  q.a[0] == 0x123456 && q.a[1] == 16 &&
			  t_eq(q.name, t_len(q.name), "DCIM/100OLYMP/P1180001.JPG"));
	CHECK(49, t_parse(FDM_OP_FGET, 2, w, "DCIM/100OLYMP/P1180001.JPG", 0, &q) == FDM_ENAME &&
			  t_parse(FDM_OP_FGET, 2, w, "SHOT0001.PPM", 0, &q) == 0 &&
			  t_parse_path(FDM_OP_FGET, 2, w, "FENYKL.LOG", &q) == 0 &&
			  t_parse_path(FDM_OP_FGET, 2, w, "DCIM/100OLYMP", &q) == FDM_ENAME);
	CHECK(44, t_parse(FDM_OP_FDEL, 0, w, "FENYKL.TMP", 0, &q) == 0 &&
			  t_parse(FDM_OP_FDEL, 0, w, "FENYKL.CF0", 0, &q) == 0);
	CHECK(45, t_parse(FDM_OP_FDEL, 0, w, "FENYKL.CF0", 1, &q) == FDM_EARGS &&
			  fdm_parse(FDM_OP_FDEL, pay, 4, &q) == FDM_EARGS &&
			  t_parse_path(FDM_OP_FDEL, 0, w, "FENYKL.CF0", &q) == FDM_EARGS);
	/* FLIST: start, path field */
	w[0] = 819;
	CHECK(60, t_parse_path(FDM_OP_FLIST, 1, w, "DCIM", &q) == 0 && q.a[0] == 819 &&
			  t_parse_path(FDM_OP_FLIST, 1, w, "DCIM/100OLYMP", &q) == 0);
	CHECK(61, t_parse(FDM_OP_FLIST, 1, w, "DCIM", 0, &q) == FDM_EARGS &&
			  t_parse_path(FDM_OP_FLIST, 0, w, "DCIM", &q) == FDM_EARGS &&
			  t_parse_path(FDM_OP_FLIST, 1, w, "DCIM/100OLYMP/P1180001.JPG", &q) == FDM_ENAME &&
			  t_parse_path(FDM_OP_FLIST, 1, w, "DCOLYMP", &q) == FDM_ENAME);
}

static void t_ranges(void)
{
	CHECK(50, fdm_ram_ok(0x60021000u, 1) && !fdm_ram_ok(0x60020FFFu, 1));
	CHECK(51, !fdm_ram_ok(0x60020FFFu, 2));
	CHECK(52, fdm_ram_ok(0x7FFFFFFFu, 1) && !fdm_ram_ok(0x7FFFFFFFu, 2));
	CHECK(53, fdm_ram_ok(0xA0021000u, 4) && !fdm_ram_ok(0xA0020FFCu, 4));
	CHECK(54, fdm_ram_ok(0xBFFFFFF0u, 0x10) && !fdm_ram_ok(0xBFFFFFF0u, 0x11));
	CHECK(55, !fdm_ram_ok(0x98000000u, 4) && !fdm_ram_ok(0x8FF00000u, 4));
	CHECK(56, !fdm_ram_ok(0x00008000u, 4) && !fdm_ram_ok(0x40000000u, 4) &&
			  !fdm_ram_ok(0x47000000u, 4));
	CHECK(57, !fdm_ram_ok(0xC0000000u, 4) && !fdm_ram_ok(0xFFFFFFF0u, 0x20));
	CHECK(58, !fdm_ram_ok(0x60021000u, 0));
	CHECK(59, fdm_ram_ok(0x60021000u, FDM_READ_MAX) &&
			  !fdm_ram_ok(0x60021000u, FDM_READ_MAX + 1));
}

/* The name rule: a name of n bytes (NULs allowed) and the status of each file op for it. */
struct t_name {
	const char *s;
	unsigned int n;
	int get, put, del, list;
};

#define N_ (FDM_ENAME)
#define P_ (FDM_EPROTECT)
#define T(s) s, sizeof(s) - 1

static const struct t_name t_name_rows[] = {
	/* FENYKL.*: read and write, the stable image and the temp file excepted */
	{ T("FENYKL.BIN"), 0, P_, P_, N_ },
	{ T("FENYKL.TST"), 0, 0, 0, N_ },
	{ T("FENYKL.LOG"), 0, 0, 0, N_ },
	{ T("FENYKL.CF0"), 0, 0, 0, N_ },
	{ T("FENYKL.A"), 0, 0, 0, N_ },
	{ T("FENYKL.TMP"), 0, N_, 0, N_ },
	{ T("FENYKL.BI"), 0, 0, 0, N_ },
	{ T("fenykl.bin"), N_, N_, N_, N_ },
	{ T("FENYKL.bin"), N_, N_, N_, N_ },
	{ T("Fenykl.BIN"), N_, N_, N_, N_ },
	{ T("FENYKL."), N_, N_, N_, N_ },
	{ T("FENYKL"), N_, N_, N_, N_ },
	{ T(""), N_, N_, N_, N_ },
	{ T("FENYKL.BINX"), N_, N_, N_, N_ },
	{ T("FENYKLX.BIN"), N_, N_, N_, N_ },
	{ T("XFENYKL.BIN"), N_, N_, N_, N_ },
	{ T("FENYKL_.BIN"), N_, N_, N_, N_ },
	{ T("\\FENYKL.BIN"), N_, N_, N_, N_ },
	{ T("C:\\FENYKL.BI"), N_, N_, N_, N_ },
	{ T("FENYKL.B\\N"), N_, N_, N_, N_ },
	{ T("FENYKL.B/N"), N_, N_, N_, N_ },
	{ T("FENYKL.B:N"), N_, N_, N_, N_ },
	{ T("FENYKL.."), N_, N_, N_, N_ },
	{ T("FENYKL..."), N_, N_, N_, N_ },
	{ T("FENYKL.*"), N_, N_, N_, N_ },
	{ T("FENYKL.?"), N_, N_, N_, N_ },
	{ T("FENYKL.B N"), N_, N_, N_, N_ },
	{ T("FENYKL.B.N"), N_, N_, N_, N_ },
	{ T("DCOLYMP\\X"), N_, N_, N_, N_ },
	{ T("..\\FENYKL.B"), N_, N_, N_, N_ },
	{ T("FENYKL.\\.."), N_, N_, N_, N_ },
	{ T("OLYMPUS.BIN"), N_, N_, N_, N_ },
	/* an embedded NUL hides nothing: every byte after the first NUL must be NUL */
	{ T("FENYKL.B\0N"), N_, N_, N_, N_ },
	{ T("FENYKL.BIN\0X"), N_, N_, N_, N_ },
	{ T("FENYKL.\0BIN"), N_, N_, N_, N_ },
	{ T("FENYKL.BINXY"), N_, N_, N_, N_ },
	{ T("FENYKL.ABCDE"), N_, N_, N_, N_ },
	{ T("FENYKL.BI\x80"), N_, N_, N_, N_ },
	{ T("FENYKL.B\xFF" "N"), N_, N_, N_, N_ },
	/* screenshots: read only */
	{ T("SHOT0000.PPM"), 0, N_, N_, N_ },
	{ T("SHOT9999.PPM"), 0, N_, N_, N_ },
	{ T("SHOT001.PPM"), N_, N_, N_, N_ },
	{ T("SHOT00001.PPM"), N_, N_, N_, N_ },
	{ T("SHOT000A.PPM"), N_, N_, N_, N_ },
	{ T("SHOT0001.ppm"), N_, N_, N_, N_ },
	{ T("SHOT0001.PP"), N_, N_, N_, N_ },
	{ T("SHOT0001.PPMX"), N_, N_, N_, N_ },
	{ T("SHOT0001.LV"), N_, N_, N_, N_ },
	{ T("SHOT0001.JPG"), N_, N_, N_, N_ },
	{ T("shot0001.ppm"), N_, N_, N_, N_ },
	/* photos: read only */
	{ T("DCIM/100OLYMP/P1180001.JPG"), 0, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.ORF"), 0, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.MOV"), 0, N_, N_, N_ },
	{ T("DCIM/100OLYMP/_1180001.JPG"), 0, N_, N_, N_ },
	{ T("DCIM/999ABCDE/A.JPG"), 0, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.jpg"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.BIN"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/E1200000.BIN"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.WAV"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.JPEG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.JP"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P11800012.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.JPG/"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P1180001.JPG\0X"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P118-001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYM/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMPX/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/A00OLYMP/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100__TSB/FA000001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100olymp/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("dcim/100olymp/p1180001.jpg"), N_, N_, N_, N_ },
	/* separators: '/' only, one between the parts */
	{ T("DCIM\\100OLYMP\\P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP\\P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM\\100OLYMP/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM//100OLYMP/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("/DCIM/100OLYMP/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("C:/DCIM/100OLYMP/P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/../P1180001.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/../FENYKL.BIN"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/./P1.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/*.JPG"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/P118000?.JPG"), N_, N_, N_, N_ },
	/* the firmware updates and their folder are never touched */
	{ T("DCOLYMP"), N_, N_, N_, N_ },
	{ T("DCOLYMP/E1200000.BIN"), N_, N_, N_, N_ },
	{ T("E1200000.BIN"), N_, N_, N_, N_ },
	{ T("DCIM/DCOLYMP"), N_, N_, N_, N_ },
	/* directories: listed only */
	{ T("DCIM"), N_, N_, N_, 0 },
	{ T("DCIM/100OLYMP"), N_, N_, N_, 0 },
	{ T("DCIM/"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP/"), N_, N_, N_, N_ },
	{ T("DCIMX"), N_, N_, N_, N_ },
	{ T("DCIM/100OLYMP\0X"), N_, N_, N_, N_ },
	/* no terminator in the whole field */
	{ T("DCIM/100OLYMP/P1180001.JPGXXXXXX"), N_, N_, N_, N_ },
};

/* The kind of a string in a path field. */
static enum fdm_name t_kind(const char *s)
{
	char f[FDM_PATH_FIELD] = { 0 };

	for (unsigned int i = 0; s[i] && i < FDM_PATH_FIELD; i++)
		f[i] = s[i];
	return fdm_name_kind(f);
}

static void t_names(void)
{
	char path[FDM_PATH_MAX];
	char f[FDM_PATH_FIELD];

	/* exit status 210, and each failed row printed */
	for (unsigned int r = 0; r < sizeof(t_name_rows) / sizeof(t_name_rows[0]); r++) {
		const struct t_name *t = &t_name_rows[r];
		char num[12];

		for (unsigned int i = 0; i < FDM_PATH_FIELD; i++)
			f[i] = i < t->n ? t->s[i] : '\0';
		if (fdm_name_check(FDM_OP_FGET, f) == t->get &&
		    fdm_name_check(FDM_OP_FPUT, f) == t->put &&
		    fdm_name_check(FDM_OP_FDEL, f) == t->del &&
		    fdm_name_check(FDM_OP_FLIST, f) == t->list)
			continue;
		CHECK(210, false);
		sim_write("name rule row ", 14);
		sim_write(num, fdm_utoa(num, r, false));
		sim_write(": ", 2);
		sim_write(t->s, t_len(t->s));
		sim_write("\n", 1);
	}
	for (unsigned int i = 0; i < FDM_PATH_FIELD; i++)
		f[i] = '\0';
	CHECK(70, fdm_name_check(FDM_OP_READ, f) == FDM_ENAME);
	fdm_path(path, "FENYKL.BIN");
	CHECK(71, t_eq(path, t_len(path), "C:\\FENYKL.BIN"));
	fdm_path(path, "DCIM/100OLYMP/P1180001.JPG");
	CHECK(72, t_eq(path, t_len(path), "C:\\DCIM\\100OLYMP\\P1180001.JPG"));
	CHECK(73, t_kind("SHOT0001.PPM") == FDM_NAME_SHOT && t_kind("DCIM/100OLYMP") == FDM_NAME_DIR &&
			  t_kind("DCIM/100OLYMP/P1180001.ORF") == FDM_NAME_PHOTO &&
			  t_kind("FENYKL.TMP") == FDM_NAME_TMP && t_kind("FENYKL.BIN") == FDM_NAME_BIN &&
			  t_kind("FENYKL.TST") == FDM_NAME_FENYKL && t_kind("DCOLYMP") == FDM_NAME_BAD);
}

/* A fake test program for slot 3 in a buffer: header, code bytes, crc. */
static uint32_t prog[64];

static struct fenykl_tp_hdr *t_prog(void)
{
	struct fenykl_tp_hdr *h = (struct fenykl_tp_hdr *)prog;
	uint32_t base = FDM_SLOT_ADDR(3);

	for (unsigned int i = 8; i < 64; i++)
		prog[i] = i * 0x01010101u;
	h->magic = FENYKL_TP_MAGIC;
	h->api = FENYKL_TP_API;
	h->base = base;
	h->size = sizeof(prog);
	h->mem = sizeof(prog) + 0x100;
	h->entry = base + 0x20;
	h->crc = fdm_crc32(0, (const uint8_t *)prog + 32, sizeof(prog) - 32);
	return h;
}

static void t_tp(void)
{
	struct fenykl_tp_hdr *h;

	CHECK(80, fdm_crc32(0, (const uint8_t *)"123456789", 9) == 0xCBF43926u);
	CHECK(81, fdm_crc32(fdm_crc32(0, (const uint8_t *)"1234", 4), (const uint8_t *)"56789", 5) ==
			  0xCBF43926u);
	h = t_prog();
	CHECK(82, fdm_tp_check(h, 3) == 0);
	CHECK(83, fdm_tp_check(h, 4) == FDM_ETP);
	h->magic++;
	CHECK(84, fdm_tp_check(h, 3) == FDM_ETP);
	h = t_prog();
	h->api = FENYKL_TP_API + 1;
	CHECK(85, fdm_tp_check(h, 3) == FDM_ETP);
	h = t_prog();
	h->mem = FENYKL_TP_SLOT_SIZE + 4;
	CHECK(86, fdm_tp_check(h, 3) == FDM_ETP);
	h = t_prog();
	h->mem = h->size - 4;
	CHECK(87, fdm_tp_check(h, 3) == FDM_ETP);
	h = t_prog();
	h->entry = h->base + 0x1C;
	CHECK(88, fdm_tp_check(h, 3) == FDM_ETP);
	h->entry = h->base + h->size;
	CHECK(89, fdm_tp_check(h, 3) == FDM_ETP);
	h = t_prog();
	prog[40] ^= 1;
	CHECK(90, fdm_tp_check(h, 3) == FDM_ETP);
	h = t_prog();
	h->size = 16;
	CHECK(91, fdm_tp_check(h, 3) == FDM_ETP);
}

static struct fdm_shared sh;

static void t_ping(void)
{
	char buf[FDM_PING_MAX];
	unsigned int n;

	sh.uptime_ms = 1234;
	sh.poweron_factor = 0x1000001;
	sh.wake_index = 9;
	sh.off_factor = 10;
	sh.clients = 2;
	sh.requests = 0xFFFFFFFFu;
	sh.dropped = 0xFFFFFFFFu;
	sh.exec_busy = 1;
	sh.stack_free = 1234;
	n = fdm_ping_text(buf, &sh);
	CHECK(100, n < FDM_PING_MAX && buf[n - 1] == '\n');
	CHECK(101, t_eq(buf, n, "devmon proto=1 tp_api=1 up_ms=1234 poweron=0x1000001 wake=9 off=10 "
				"clients=2 requests=4294967295 dropped=4294967295 exec_busy=1 "
				"srv_stack_free=1234\n"));
	n = fdm_utoa(buf, 0xDEADBEEFu, true);
	CHECK(102, t_eq(buf, n, "0xdeadbeef"));
}

static void t_log_span(void)
{
	uint32_t keep = FDM_TEXT_SIZE - FDM_TEXT_SLACK, first;

	CHECK(110, fdm_text_span(100, 0, &first) == 100 && first == 0);
	CHECK(111, fdm_text_span(100, 40, &first) == 60 && first == 40);
	CHECK(112, fdm_text_span(100, 100, &first) == 0 && first == 100);
	CHECK(113, fdm_text_span(100, 500, &first) == 0 && first == 100);
	CHECK(114, fdm_text_span(keep + 10, 0, &first) == keep && first == 10);
	CHECK(115, fdm_text_span(3 * FDM_TEXT_SIZE + 5, 3 * FDM_TEXT_SIZE, &first) == 5 &&
			   first == 3 * FDM_TEXT_SIZE);
	CHECK(116, fdm_text_span(0, 0, &first) == 0);
}

static void t_protect(void)
{
	struct fdm_req q;
	uint32_t w[4] = { 0, 4, 0 };

	/* FENYKL.BIN: readable, never written or removed */
	CHECK(150, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.BIN", 4, &q) == FDM_EPROTECT);
	w[0] = 2;
	CHECK(151, t_parse(FDM_OP_FPUT, 3, w, "FENYKL.BIN", 2, &q) == FDM_EPROTECT);
	CHECK(152, t_parse(FDM_OP_FDEL, 0, w, "FENYKL.BIN", 0, &q) == FDM_EPROTECT);
	w[0] = 0;
	w[1] = 16;
	CHECK(153, t_parse(FDM_OP_FGET, 2, w, "FENYKL.BIN", 0, &q) == 0);
	/* a bad name stays ENAME, also for BIN look-alikes */
	CHECK(155, t_parse(FDM_OP_FDEL, 0, w, "\\FENYKL.BIN", 0, &q) == FDM_ENAME);
}

static void t_deny(void)
{
	struct fdm_req q;
	uint32_t w[2];

	CHECK(160, fdm_write_ok(0x60DA4FFCu, 4) && !fdm_write_ok(0x60DA4FFCu, 5));
	CHECK(161, !fdm_write_ok(0x60DA5000u, 1) && !fdm_write_ok(0x60E0FFFFu, 1) &&
			   fdm_write_ok(0x60E10000u, 4));
	CHECK(162, !fdm_write_ok(0xA0DA5000u, 4) && !fdm_write_ok(0xA0E0FFF0u, 0x20) &&
			   fdm_write_ok(0xA0E10000u, 4) && fdm_write_ok(0xA0DA4000u, 0x1000));
	CHECK(163, !fdm_write_ok(0x60DA0000u, FDM_READ_MAX) && fdm_ram_ok(0x60DA0000u, FDM_READ_MAX));
	CHECK(164, !fdm_write_ok(0x6005E000u, 1) && !fdm_write_ok(0x6005E219u, 1) &&
			   fdm_write_ok(0x6005E21Au, 4) && fdm_write_ok(0x6005DFFCu, 4) &&
			   !fdm_write_ok(0x6005DFFCu, 5) && !fdm_write_ok(0xA005E100u, 4));
	CHECK(165, !fdm_write_ok(0x6018D73Au, 1) && !fdm_write_ok(0x6018D740u, 1) &&
			   fdm_write_ok(0x6018D741u, 1) && !fdm_write_ok(0xA018D738u, 4));
	CHECK(166, !fdm_write_ok(0x98000000u, 4) && !fdm_write_ok(0x60020FFCu, 4));
	w[0] = 0xA0DA6000u;
	CHECK(167, t_parse(FDM_OP_WRITE, 1, w, NULL, 4, &q) == FDM_ERANGE);
	w[0] = 0x6005E004u;
	CHECK(168, t_parse(FDM_OP_WRITE, 1, w, NULL, 4, &q) == FDM_ERANGE);
	w[1] = 4;
	CHECK(169, t_parse(FDM_OP_READ, 2, w, NULL, 0, &q) == 0 && q.a[1] == 4);
	CHECK(170, fdm_uncached(0xA0021000u) && fdm_uncached(0xBFFFFFFFu) &&
			   !fdm_uncached(0x7FFFFFFFu) && !fdm_uncached(0x60021000u));
}

static unsigned int t_fmt(char *out, unsigned int size, const char *fmt, uint32_t a0, uint32_t a1)
{
	const uint32_t args[] = { a0, a1 };

	return fdm_format(out, size, fmt, args, 2);
}

static void t_format(void)
{
	static const char long_s[] = "0123456789abcdefghijklmnopqrstuvwxyz";
	char b[32];
	unsigned int n;

	n = t_fmt(b, sizeof(b), "a %x b %u", 0xBEEFu, 42);
	CHECK(180, t_eq(b, n, "a beef b 42") && b[n] == '\0');
	n = t_fmt(b, sizeof(b), "%d %d", (uint32_t)-17, 0x80000000u);
	CHECK(181, t_eq(b, n, "-17 -2147483648"));
	n = t_fmt(b, sizeof(b), "%08x|%X|%4u", 0xABu, 0xABu);
	CHECK(182, t_eq(b, n, "000000ab|AB|0000"));
	n = t_fmt(b, sizeof(b), "[%s]", (uint32_t)long_s, 0);
	CHECK(183, n == sizeof(b) - 1 && b[n] == '\0' && b[0] == '[' && b[1] == '0');
	n = t_fmt(b, 8, "%u%u", 4000000000u, 4000000000u);
	CHECK(184, t_eq(b, n, "4000000"));
	n = t_fmt(b, sizeof(b), "100%% %c%p %", 'z', 0);
	CHECK(185, t_eq(b, n, "100% zp "));
	n = t_fmt(b, sizeof(b), "%x %x %x", 1, 2);
	CHECK(186, t_eq(b, n, "1 2 0"));
	n = t_fmt(b, 1, "abc", 0, 0);
	CHECK(187, n == 0 && b[0] == '\0');
	n = t_fmt(b, sizeof(b), "%s", 0, 0);
	CHECK(188, t_eq(b, n, "(null)"));
}

static void t_put(void)
{
	struct fdm_put st = { 0 };
	struct fdm_req q = { .op = FDM_OP_FPUT, .a = { 0, 10, 0x55 }, .name = "FENYKL.DAT", .n = 4 };

	CHECK(190, fdm_put_chunk(&st, &q) == FDM_PUT_FIRST && st.open && st.total == 10 &&
			   st.done == 0);
	st.done += q.n;
	q.a[0] = 4;
	CHECK(191, fdm_put_chunk(&st, &q) == FDM_PUT_NEXT);
	st.done += q.n;
	q.a[0] = 4;	/* a repeated chunk */
	CHECK(192, fdm_put_chunk(&st, &q) == FDM_EARGS && !st.open);
	q.a[0] = 8;	/* the state is gone: the right offset no longer helps */
	CHECK(193, fdm_put_chunk(&st, &q) == FDM_EARGS);
	q.a[0] = 0;
	CHECK(194, fdm_put_chunk(&st, &q) == FDM_PUT_FIRST);
	st.done = 4;
	q.a[0] = 4;
	q.name[7] = 'X';	/* another file */
	CHECK(195, fdm_put_chunk(&st, &q) == FDM_EARGS);
	q.name[7] = 'D';
	q.a[0] = 0;
	fdm_put_chunk(&st, &q);
	st.done = 4;
	q.a[0] = 4;
	q.a[1] = 11;	/* total changed */
	CHECK(196, fdm_put_chunk(&st, &q) == FDM_EARGS);
	q.a[1] = 10;
	q.a[0] = 0;
	fdm_put_chunk(&st, &q);
	st.done = 4;
	q.a[0] = 4;
	q.a[2] = 0x56;	/* crc changed */
	CHECK(197, fdm_put_chunk(&st, &q) == FDM_EARGS);
}

static void t_keys(void)
{
	struct fdm_req q;
	uint32_t w[2] = { FDM_KEY_MENU, 200 };

	CHECK(200, t_parse(FDM_OP_KEY, 2, w, NULL, 0, &q) == 0 && q.a[0] == FDM_KEY_MENU &&
			   q.a[1] == 200);
	w[0] = FDM_KEY_N;
	CHECK(201, t_parse(FDM_OP_KEY, 2, w, NULL, 0, &q) == FDM_EARGS);
	w[0] = FDM_KEY_FULL;
	w[1] = 0;
	CHECK(202, t_parse(FDM_OP_KEY, 2, w, NULL, 0, &q) == FDM_EARGS);
	w[1] = FDM_HOLD_MAX + 1;
	CHECK(203, t_parse(FDM_OP_KEY, 2, w, NULL, 0, &q) == FDM_EARGS);
	w[1] = FDM_HOLD_MAX;
	CHECK(204, t_parse(FDM_OP_KEY, 2, w, NULL, 0, &q) == 0 &&
			   t_parse(FDM_OP_KEY, 1, w, NULL, 0, &q) == FDM_EARGS);
	/* the key layer's codes; never the power switch 0x0C or the mode dial */
	CHECK(205, fdm_key_code(FDM_KEY_OK) == 0x0004 && fdm_key_code(FDM_KEY_MENU) == 0x0089 &&
			   fdm_key_code(FDM_KEY_INFO) == 0x1205 && fdm_key_code(FDM_KEY_HALF) == 0x0100 &&
			   fdm_key_code(FDM_KEY_FULL) == 0x0101 && fdm_key_code(FDM_KEY_N) == 0);
	for (uint32_t k = 0; k < FDM_KEY_N; k++)
		CHECK(206, fdm_key_code(k) && fdm_key_code(k) != 0x0C && fdm_key_code(k) != 0x1901);
}

static void t_dial(void)
{
	struct fdm_req q;
	uint32_t w[1] = { FDM_DIAL_SCN };

	CHECK(250, t_parse(FDM_OP_DIAL, 1, w, NULL, 0, &q) == 0 && q.a[0] == FDM_DIAL_SCN);
	w[0] = FDM_DIAL_N;
	CHECK(251, t_parse(FDM_OP_DIAL, 1, w, NULL, 0, &q) == FDM_EARGS);
	w[0] = FDM_DIAL_P;
	CHECK(252, t_parse(FDM_OP_DIAL, 0, w, NULL, 0, &q) == FDM_EARGS &&
			   t_parse(FDM_OP_DIAL, 0, w, NULL, 2, &q) == FDM_EARGS &&
			   t_parse(FDM_OP_DIAL, 1, w, NULL, 1, &q) == FDM_EARGS);
	/* the ladder's values: P..M 1..4, movie 0x0A, Photo Story 0x39, SCN 0x2A, ART 0x33,
	   iAUTO 0x31 */
	CHECK(253, fdm_dial_value(FDM_DIAL_P) == 0x01 && fdm_dial_value(FDM_DIAL_M) == 0x04 &&
			   fdm_dial_value(FDM_DIAL_MOVIE) == 0x0A && fdm_dial_value(FDM_DIAL_STORY) == 0x39 &&
			   fdm_dial_value(FDM_DIAL_SCN) == 0x2A && fdm_dial_value(FDM_DIAL_ART) == 0x33 &&
			   fdm_dial_value(FDM_DIAL_IAUTO) == 0x31 && fdm_dial_value(FDM_DIAL_N) == 0);
}

static uint32_t t_inj_code, t_inj_level, t_injects, t_inj_result;

static unsigned int t_get_state(unsigned int code)
{
	return code == FDM_DIAL_CODE ? 0x01 : 0xFFFF;
}

static unsigned int t_inject(unsigned int code, unsigned int level)
{
	t_injects++;
	t_inj_code = code;
	t_inj_level = level;
	return t_inj_result;
}

static const struct fdm_keyops t_keyops = { .get_state = t_get_state, .inject_level = t_inject };

/* DIAL injects the dial code with the position's value once, and nothing for a bad index; it runs
   in the CPU0 task like KEY. */
static void t_dial_set(void)
{
	uint8_t out[4] = { 0 };

	CHECK(254, fdm_dial_set(&t_keyops, FDM_DIAL_SCN, out) == 0 && t_injects == 1 &&
			   t_inj_code == 0x02021901u && t_inj_level == 0x2A && fdm_get32(out) == 0x01 &&
			   fdm_dial_set(&t_keyops, FDM_DIAL_N, out) != 0 && t_injects == 1);
	t_inj_result = 3;
	CHECK(254, fdm_dial_set(&t_keyops, FDM_DIAL_P, out) == 3 && t_injects == 2 &&
			   t_inj_level == 0x01);
	CHECK(255, fdm_op_is_job(FDM_OP_DIAL) && fdm_op_is_job(FDM_OP_KEY) &&
			   !fdm_op_is_job(FDM_OP_READ) && !fdm_op_is_job(FDM_OP_POWEROFF));
}

static void t_off(void)
{
	struct fdm_off o = { 0 };
	uint32_t t = FDM_OFF_STOP_TICK;

	/* timed: stop at 585 s, inject at 590 s or the gap after the stop, whichever is later */
	CHECK(120, fdm_off_step(&o, t - 1, true, 0, 1) == FDM_ACT_NONE);
	CHECK(121, fdm_off_step(&o, t, true, 5, 1) == FDM_ACT_STOP);
	CHECK(122, fdm_off_step(&o, t + 1, true, 5, 1) == FDM_ACT_NONE);
	CHECK(123, fdm_off_step(&o, t + 2, true, 6, 1) == FDM_ACT_STOPPED);
	CHECK(124, fdm_off_step(&o, FDM_OFF_INJECT_TICK, true, 6, 0) == FDM_ACT_NONE);
	CHECK(125, fdm_off_step(&o, t + 2 + FDM_OFF_GAP - 1, true, 6, 0) == FDM_ACT_NONE);
	CHECK(126, fdm_off_step(&o, t + 2 + FDM_OFF_GAP, true, 6, 0) == FDM_ACT_INJECT);
	CHECK(127, fdm_off_step(&o, t + 3 + FDM_OFF_GAP, true, 6, 0) == FDM_ACT_NONE);
	/* the stop never finishes: no inject, failed */
	o = (struct fdm_off){ 0 };
	CHECK(128, fdm_off_step(&o, t, true, 0, 1) == FDM_ACT_STOP);
	CHECK(129, fdm_off_step(&o, t + FDM_OFF_STOP_TIMEOUT - 1, true, 0, 1) == FDM_ACT_NONE);
	CHECK(130, fdm_off_step(&o, t + FDM_OFF_STOP_TIMEOUT, true, 0, 1) == FDM_ACT_TIMEOUT &&
			   o.failed);
	CHECK(131, fdm_off_step(&o, FDM_OFF_INJECT_TICK + FDM_OFF_STOP_TIMEOUT, true, 7, 0) ==
			   FDM_ACT_NONE);
	/* POWEROFF at tick 50: stop at once, wait for it, inject the gap later, never before */
	o = (struct fdm_off){ 0 };
	CHECK(132, fdm_off_step(&o, 50, true, 0, 1) == FDM_ACT_NONE);
	fdm_off_request(&o);
	CHECK(133, fdm_off_step(&o, 51, true, 0, 1) == FDM_ACT_STOP);
	CHECK(134, fdm_off_step(&o, 52, true, 0, 1) == FDM_ACT_NONE);
	CHECK(135, fdm_off_step(&o, 53, true, 0, 0) == FDM_ACT_STOPPED);
	CHECK(136, fdm_off_step(&o, 53 + FDM_OFF_GAP - 1, true, 0, 0) == FDM_ACT_NONE);
	CHECK(137, fdm_off_step(&o, 53 + FDM_OFF_GAP, true, 0, 0) == FDM_ACT_INJECT && !o.failed);
	/* POWEROFF with the stop hanging: timeout, no inject, failed; a second request changes
	   nothing */
	o = (struct fdm_off){ 0 };
	fdm_off_request(&o);
	CHECK(138, fdm_off_step(&o, 10, true, 0, 1) == FDM_ACT_STOP);
	CHECK(139, fdm_off_step(&o, 10 + FDM_OFF_STOP_TIMEOUT, true, 0, 1) == FDM_ACT_TIMEOUT);
	fdm_off_request(&o);
	CHECK(140, fdm_off_step(&o, 11 + FDM_OFF_STOP_TIMEOUT, true, 0, 0) == FDM_ACT_NONE &&
			   o.failed);
	/* Wi-Fi never started: no stop; timed: inject at the inject tick; requested: the gap */
	o = (struct fdm_off){ 0 };
	CHECK(141, fdm_off_step(&o, t, false, 0, 0) == FDM_ACT_NONE);
	CHECK(142, fdm_off_step(&o, FDM_OFF_INJECT_TICK, false, 0, 0) == FDM_ACT_INJECT);
	o = (struct fdm_off){ 0 };
	fdm_off_request(&o);
	CHECK(143, fdm_off_step(&o, 3, false, 0, 0) == FDM_ACT_NONE);
	CHECK(144, fdm_off_step(&o, 3 + FDM_OFF_GAP - 1, false, 0, 0) == FDM_ACT_NONE);
	CHECK(145, fdm_off_step(&o, 3 + FDM_OFF_GAP, false, 0, 0) == FDM_ACT_INJECT);
}

/* --- the CPU0 file jobs against a fake card --- */

static uint8_t t_file[40000];
static uint8_t t_out[FDM_IO_DATA];
static char t_opened[FDM_PATH_MAX], t_mode[4], t_pattern[FDM_PATH_MAX];
static int t_fp, t_opens, t_closes, t_reads, t_seek_r, t_fclose_r, t_close_r, t_handles;
static int t_firsts, t_errs;
static bool t_open_fail;
static uint32_t t_pos, t_seek_off, t_whence, t_clock, t_tick;
static unsigned int t_dir_n, t_next;
static void (*t_dir)(unsigned int i, struct fdm_find *f);

static void t_copy(char *out, const char *in, unsigned int max)
{
	unsigned int i;

	for (i = 0; in[i] && i < max - 1; i++)
		out[i] = in[i];
	out[i] = '\0';
}

static void *t_fopen(const char *path, const char *mode)
{
	t_copy(t_opened, path, sizeof(t_opened));
	t_copy(t_mode, mode, sizeof(t_mode));
	if (t_open_fail)
		return NULL;
	t_opens++;
	t_pos = 0;
	return &t_fp;
}

static int t_fclose(void *fp)
{
	if (fp == &t_fp)
		t_closes++;
	return t_fclose_r;
}

static int t_fread(void *buf, unsigned int size, unsigned int n, void *fp)
{
	uint8_t *b = buf;
	unsigned int k = 0;

	t_reads++;
	if (fp != &t_fp || size != 1)
		return 0;
	for (; k < n && t_pos < sizeof(t_file); k++)
		b[k] = t_file[t_pos++];
	return (int)k;
}

static int t_fseek(void *fp, int offset, unsigned int whence)
{
	t_seek_off = (uint32_t)offset;
	t_whence = whence;
	if (fp != &t_fp)
		return -1;
	if (t_seek_r)
		return t_seek_r;
	t_pos = (uint32_t)offset < sizeof(t_file) ? (uint32_t)offset : sizeof(t_file);
	return 0;
}

static int t_findfirst(const char *pattern, void *info)
{
	t_copy(t_pattern, pattern, sizeof(t_pattern));
	t_firsts++;
	if (!t_dir_n)
		return -1;
	t_handles++;
	t_next = 0;
	t_dir(0, info);
	return 0x1234;
}

static int t_findnext(void *info, int h)
{
	if (h != 0x1234 || ++t_next >= t_dir_n)
		return -1;
	t_dir(t_next, info);
	return 0;
}

static int t_findclose(int h)
{
	if (h == 0x1234)
		t_handles--;
	return t_close_r;
}

static uint32_t t_now(void)
{
	t_clock = (t_clock + t_tick) & FENYKL_TIMER_MASK;
	return t_clock;
}

static void t_err(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2)
{
	(void)fmt;
	(void)a0;
	(void)a1;
	(void)a2;
	t_errs++;
}

static const struct fdm_fileops t_card = {
	.fopen = t_fopen,
	.fclose = t_fclose,
	.fread = t_fread,
	.fseek = t_fseek,
	.findfirst = t_findfirst,
	.findnext = t_findnext,
	.findclose = t_findclose,
	.now_us = t_now,
	.err = t_err,
};

static void t_files_reset(void)
{
	t_opens = t_closes = t_reads = t_seek_r = t_fclose_r = t_close_r = t_handles = 0;
	t_firsts = t_errs = 0;
	t_open_fail = false;
	t_clock = 0;
	t_tick = 1000;
}

static void t_req(struct fdm_req *q, enum fdm_op op, uint32_t a0, uint32_t a1, const char *name)
{
	q->op = op;
	q->a[0] = a0;
	q->a[1] = a1;
	for (unsigned int i = 0; i < FDM_PATH_FIELD; i++)
		q->name[i] = '\0';
	t_copy(q->name, name, FDM_PATH_FIELD);
}

static void t_get(void)
{
	struct fdm_req q;
	uint32_t n = 99;
	bool same = true;

	for (unsigned int i = 0; i < sizeof(t_file); i++)
		t_file[i] = (uint8_t)(i * 7 + (i >> 8));
	t_files_reset();
	t_req(&q, FDM_OP_FGET, 0x5000, FDM_IO_DATA, "DCIM/100OLYMP/P1180001.JPG");
	CHECK(230, fdm_get(&t_card, &q, t_out, &n) == 0 && n == FDM_IO_DATA);
	for (unsigned int i = 0; i < FDM_IO_DATA; i++)
		same = same && t_out[i] == t_file[0x5000 + i];
	CHECK(231, same && t_seek_off == 0x5000 && t_whence == 2 && t_opens == 1 && t_closes == 1 &&
			   t_eq(t_opened, t_len(t_opened), "C:\\DCIM\\100OLYMP\\P1180001.JPG") &&
			   t_eq(t_mode, t_len(t_mode), "r"));
	/* the end: fewer bytes, then none */
	q.a[0] = sizeof(t_file) - 1000;
	CHECK(232, fdm_get(&t_card, &q, t_out, &n) == 0 && n == 1000 && t_out[0] == t_file[39000]);
	q.a[0] = sizeof(t_file) + 5;
	CHECK(233, fdm_get(&t_card, &q, t_out, &n) == 0 && n == 0 && t_opens == t_closes);
	/* a failed seek reads nothing, closes and says so */
	t_files_reset();
	t_seek_r = -1;
	q.a[0] = 16;
	CHECK(234, fdm_get(&t_card, &q, t_out, &n) == FDM_EIO && n == 0 && t_reads == 0 &&
			   t_closes == 1 && t_errs == 1);
	t_files_reset();
	t_fclose_r = -1;
	CHECK(235, fdm_get(&t_card, &q, t_out, &n) == FDM_EIO && t_closes == 1);
	t_files_reset();
	t_open_fail = true;
	CHECK(236, fdm_get(&t_card, &q, t_out, &n) == FDM_ENOENT && n == 0);
	/* the rule first: nothing is opened for a name GET may not read */
	t_files_reset();
	t_req(&q, FDM_OP_FGET, 0, 16, "DCIM/100OLYMP");
	t_opened[0] = '\0';
	CHECK(237, fdm_get(&t_card, &q, t_out, &n) == FDM_ENAME && !t_opened[0]);
}

static void t_ent(struct fdm_find *f, const char *name, uint32_t attr, uint32_t size)
{
	t_copy(f->name, name, sizeof(f->name));
	f->attr = attr;
	f->size = size;
	f->mtime = size * 7;
}

/* DCIM: two folders worth listing among others. */
static void t_dcim(unsigned int i, struct fdm_find *f)
{
	static const char *const names[] = {
		".", "..", "100OLYMP", "101OLYMP", "100__TSB", "DCOLYMP", "999ZZZZZ",
	};
	static const uint32_t attrs[] = { 0x10, 0x10, 0x10, 0x20, 0x10, 0x10, 0x10 };

	t_ent(f, names[i], attrs[i], i);
}

/* A folder: five entries not worth listing, then P1180000.JPG.. with size = index. */
static void t_folder(unsigned int i, struct fdm_find *f)
{
	static const char *const odd[] = {
		".", "..", "P1180001.WAV", "P1.JPG", "P1180001.JPG-A-LONG-NAME-OF-A-COPIED-FILE",
	};
	char name[13] = "P118nnnn.JPG";
	unsigned int k = i - 5;

	if (i < 5) {
		t_ent(f, odd[i], i == 3 ? 0x10 : 0x20, 0);
		return;
	}
	for (unsigned int d = 0; d < 4; d++, k /= 10)
		name[7 - d] = (char)('0' + k % 10);
	t_ent(f, name, 0x20, i);
}

static bool t_entry(unsigned int n, const char *name, uint32_t size)
{
	const uint8_t *e = t_out + 4 + n * FDM_ENTRY_SIZE;
	unsigned int i;

	for (i = 0; i < FDM_NAME_FIELD && name[i]; i++)
		if (e[i] != (uint8_t)name[i])
			return false;
	for (; i < FDM_NAME_FIELD; i++)
		if (e[i])
			return false;
	return fdm_get32(e + 12) == size && fdm_get32(e + 16) == size * 7;
}

static void t_list(void)
{
	struct fdm_req q;
	uint32_t n = 0;

	t_files_reset();
	t_dir = t_dcim;
	t_dir_n = 7;
	t_req(&q, FDM_OP_FLIST, 0, 0, "DCIM");
	CHECK(240, fdm_list(&t_card, &q, t_out, &n) == 0 && n == 4 + 2 * FDM_ENTRY_SIZE &&
			   fdm_get32(t_out) == 0 && t_entry(0, "100OLYMP", 2) &&
			   t_entry(1, "999ZZZZZ", 6) && t_handles == 0 &&
			   t_eq(t_pattern, t_len(t_pattern), "C:\\DCIM\\*"));
	/* 900 photos: a full page of 819, next 819, then the rest */
	t_dir = t_folder;
	t_dir_n = 905;
	t_req(&q, FDM_OP_FLIST, 0, 0, "DCIM/100OLYMP");
	CHECK(241, fdm_list(&t_card, &q, t_out, &n) == 0 && n == 4 + 819 * FDM_ENTRY_SIZE &&
			   fdm_get32(t_out) == 819 && t_entry(0, "P1180000.JPG", 5) &&
			   t_entry(818, "P1180818.JPG", 823) && t_handles == 0 &&
			   t_eq(t_pattern, t_len(t_pattern), "C:\\DCIM\\100OLYMP\\*"));
	q.a[0] = 819;
	CHECK(242, fdm_list(&t_card, &q, t_out, &n) == 0 && n == 4 + 81 * FDM_ENTRY_SIZE &&
			   fdm_get32(t_out) == 0 && t_entry(0, "P1180819.JPG", 824) &&
			   t_entry(80, "P1180899.JPG", 904) && t_handles == 0);
	/* exactly a page: next 0 */
	t_dir_n = 5 + 819;
	q.a[0] = 0;
	CHECK(243, fdm_list(&t_card, &q, t_out, &n) == 0 && n == 4 + 819 * FDM_ENTRY_SIZE &&
			   fdm_get32(t_out) == 0);
	/* a start past the end */
	q.a[0] = 2000;
	CHECK(244, fdm_list(&t_card, &q, t_out, &n) == 0 && n == 4 && fdm_get32(t_out) == 0);
	/* no such folder, a failed close, the rule */
	t_files_reset();
	t_dir_n = 0;
	q.a[0] = 0;
	CHECK(245, fdm_list(&t_card, &q, t_out, &n) == FDM_ENOENT && n == 0 && t_firsts == 1 &&
			   t_handles == 0);
	t_dir_n = 905;
	t_close_r = -1;
	CHECK(246, fdm_list(&t_card, &q, t_out, &n) == FDM_EIO && n == 0 && t_handles == 0 &&
			   t_errs == 1);
	t_files_reset();
	t_req(&q, FDM_OP_FLIST, 0, 0, "DCIM/100OLYMP/P1180001.JPG");
	CHECK(247, fdm_list(&t_card, &q, t_out, &n) == FDM_ENAME && t_firsts == 0);
	t_req(&q, FDM_OP_FLIST, 0, 0, "DCOLYMP");
	CHECK(248, fdm_list(&t_card, &q, t_out, &n) == FDM_ENAME && t_firsts == 0);
	/* the deadline: 1 s per findnext, given up at 25 s with the handle closed */
	t_files_reset();
	t_tick = 1000000;
	t_clock = FENYKL_TIMER_MASK - 10000000;	/* the timer wraps 10 s into the listing */
	t_req(&q, FDM_OP_FLIST, 0, 0, "DCIM/100OLYMP");
	CHECK(249, fdm_list(&t_card, &q, t_out, &n) == FDM_ETIMEOUT && n == 0 && t_handles == 0 &&
			   t_errs == 1 && t_next == FDM_LIST_MS / 1000);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	t_frames();
	t_ops();
	t_ranges();
	t_names();
	t_tp();
	t_ping();
	t_log_span();
	t_off();
	t_protect();
	t_deny();
	t_format();
	t_put();
	t_keys();
	t_dial();
	t_dial_set();
	t_get();
	t_list();
	sim_exit_code = first_fail;
}
