/* Dev monitor internals: shared between the CPU0 tasks (devmon_em10ii.c), the CPU1 code
   (devmon_cpu1_em10ii.c) and the protocol helpers (devmon_proto.c, pure code, also run in the
   simulator). Protocol and rules: docs/devmon.md. */
#ifndef FENYKL_DEVMON_INT_H
#define FENYKL_DEVMON_INT_H

#include <stdbool.h>
#include <stddef.h>
#include <fenykl.h>
#include <tp.h>

#define FDM_MAGIC 0x444D4F4Eu	/* "DMON" */
#define FDM_PORT 5555u

/* The access point: the network name and the WPA2 passphrase, FENYKL_DEVMON_KEY at build time
   (`fenykl plugin build` warns when it falls back to this public default). */
#define FDM_SSID "FENYKL-DEV"
#ifdef FENYKL_DEVMON_KEY
#define FDM_KEY FENYKL_DEVMON_KEY
#else
#define FDM_KEY "fenykl-devmon"
#endif

#define FDM_PROTO 1u
#define FDM_RING_N 64u		/* CPU1 records */
#define FDM_TEXT_SIZE 8192u	/* text log the LOG op returns */
#define FDM_TEXT_SLACK 1024u	/* LOG returns at most SIZE - SLACK: room for CPU0 to append meanwhile */
#define FDM_NCB 8u		/* wifi_mngr_start callbacks, +0xAC..+0xC8 */
#define FDM_CB_IOREQ 7u
#define FDM_IOREQ_MAX 32u

/* The exec region (target.toml [tp]): FENYKL_TP_SLOTS slots, then the job buffer. */
#define FDM_SLOT_ADDR(n) (FENYKL_TP_BASE + (n) * FENYKL_TP_SLOT_SIZE)
#define FDM_IO_ADDR FDM_SLOT_ADDR(FENYKL_TP_SLOTS)
#define FDM_IO_DATA 0x4000u	/* file and upload bytes per request */
#define FDM_DATA_ARGS 24u	/* fixed arguments before the data (FPUT's) */
#define FDM_IO_SIZE (FDM_DATA_ARGS + FDM_IO_DATA)
#define FDM_ARGS_MAX 40u	/* fixed arguments of any op: FGET with a path field */
#define FDM_IO() ((volatile uint8_t *)FENYKL_UNCACHED(FDM_IO_ADDR))
_Static_assert(FDM_IO_ADDR + FDM_IO_SIZE <= FENYKL_TP_END, "job buffer past the exec region");

/* Frames, little-endian: a 16-byte header and len payload bytes. A request has status 0; the reply
   echoes op and id. */
#define FDM_REQ_MAGIC 0x31444B46u	/* "FKD1" */
#define FDM_REP_MAGIC 0x31524B46u	/* "FKR1" */
#define FDM_HDR_SIZE 16u
#define FDM_READ_MAX 0x100000u		/* bytes per READ */
/* READ defaults, measured on the camera: pieces of the 8 KiB send ring and a 1 ms pause reach
   ~500 KiB/s without a stall; 16 KiB pieces stall for seconds (docs/devmon.md 9). */
#define FDM_PIECE 0x2000u
#define FDM_PACE_MS 1u
#define FDM_NAME_FIELD 12u		/* file name field: NUL-padded */
#define FDM_PATH_FIELD 32u		/* FGET's long form and FLIST: a card path, NUL-padded */

enum fdm_op {
	FDM_OP_PING = 0x00,	/* - -> text */
	FDM_OP_READ = 0x01,	/* addr, len[, piece[, pace_ms]] -> bytes */
	FDM_OP_WRITE = 0x02,	/* addr, bytes -> - */
	FDM_OP_LOG = 0x03,	/* since -> end, text */
	FDM_OP_TIMING = 0x04,	/* - -> struct fdm_timing of the last bulk reply */
	FDM_OP_TPLOAD = 0x10,	/* slot, offset, bytes -> - */
	FDM_OP_TPRUN = 0x11,	/* slot, arg, timeout_ms -> result, us */
	FDM_OP_FPUT = 0x20,	/* offset, total, crc, name, bytes -> - */
	FDM_OP_FGET = 0x21,	/* offset, len, name -> bytes (short at the end) */
	FDM_OP_FDEL = 0x22,	/* name -> - */
	FDM_OP_FLIST = 0x23,	/* start, path -> next, entries */
	FDM_OP_POWEROFF = 0x30,	/* - -> - (then Wi-Fi stop and the lever Off) */
	FDM_OP_KEY = 0x31,	/* key, hold_ms -> - (pressed, held, released) */
	FDM_OP_DIAL = 0x32,	/* position -> previous value */
};

/* Reply status: 0 or one of these. */
#define FDM_EFRAME (-1)		/* bad magic or too long: the connection is closed */
#define FDM_EOP (-2)		/* unknown op */
#define FDM_EARGS (-3)		/* missing, malformed or extra arguments */
#define FDM_ERANGE (-4)		/* address outside the RAM ranges, or too long */
#define FDM_ENAME (-5)		/* a name the op does not allow (docs/devmon.md 4) */
#define FDM_EBUSY (-6)		/* a CPU0 job or a test program is still running */
#define FDM_ETIMEOUT (-7)	/* no result in time (a test program cannot be killed) */
#define FDM_EIO (-8)		/* card I/O failed, or the key layer refused an inject */
#define FDM_ECARD (-9)		/* card gate closed (no card, or a cable was seen) */
#define FDM_ECRC (-10)		/* PUT: size or CRC-32 of the written file differs */
#define FDM_ETP (-11)		/* test program header invalid */
#define FDM_EICACHE (-12)	/* the I-cache invalidate failed: not run */
#define FDM_ENOENT (-13)	/* GET: the open failed (no such file, or a card error); LIST: no folder */
#define FDM_EPROTECT (-14)	/* PUT or DEL of FENYKL.BIN: the stable image is installed by hand */
#define FDM_EOFF (-15)		/* POWEROFF: an earlier Wi-Fi stop timed out, no inject this boot */

struct fdm_req {
	enum fdm_op op;
	uint32_t a[4];		/* fixed arguments */
	char name[FDM_PATH_FIELD];	/* the name or path field, NUL-padded to FDM_PATH_FIELD */
	uint32_t data;		/* offset of the data bytes in the payload */
	uint32_t n;		/* data bytes */
};

/* Last READ reply, camera side: send() calls of piece bytes with pace_ms pauses; a stall is a
   send() of FDM_STALL_US or more (a retransmit timeout is 500 ms or more). sock: the descriptor. */
#define FDM_STALL_US 300000u
struct fdm_timing {
	uint32_t bytes, us, sends, max_send_us, stalls, stall_us, piece, pace_ms, sock;
};

/* The 0xCC-byte parameter block of wifi_mngr_start (firmware.toml). */
struct fdm_wifi_prm {
	char ssid[0x24];
	char key[0x40];
	char opt1[0x24];	/* +0x64: empty keeps the manager's default */
	char opt2[0x10];	/* +0x88: empty keeps the manager's default */
	uint16_t net_mode;	/* +0x98 */
	uint16_t sec_mode;	/* +0x9A */
	uint16_t enc_mode;	/* +0x9C */
	uint16_t pad0;
	uint32_t conn_mode;	/* +0xA0: 0 private */
	uint32_t app_mode;	/* +0xA4 */
	uint8_t raw_sec;	/* +0xA8: 0 maps security 0/1/other to 0/2/3 */
	uint8_t pad1[3];
	uint32_t cb[FDM_NCB];	/* +0xAC */
};

_Static_assert(offsetof(struct fdm_wifi_prm, net_mode) == 0x98, "net_mode");
_Static_assert(offsetof(struct fdm_wifi_prm, conn_mode) == 0xA0, "conn_mode");
_Static_assert(offsetof(struct fdm_wifi_prm, cb) == 0xAC, "callbacks");
_Static_assert(sizeof(struct fdm_wifi_prm) == 0xCC, "block size");

/* One CPU1 record: a format of this image, the timer stamp and three arguments. */
struct fdm_rec {
	uint32_t fmt, t, a[3];
};

struct fdm_cb {
	uint32_t calls, arg, core;
};

/* Text log: CPU0 appends, the CPU1 server reads; wr counts every byte ever written. */
struct fdm_text {
	uint32_t wr;
	char buf[FDM_TEXT_SIZE];
};

/* CPU1 server states (srv_state). */
#define FDM_SRV_NONE 0
#define FDM_SRV_BOOT 1		/* boot function ran, task created */
#define FDM_SRV_LISTEN 2
#define FDM_SRV_CLIENT 3
#define FDM_SRV_DEAD 4		/* gave up, the task ended */

#define FDM_BOOT_ECORE (-1000)	/* the boot function did not run on CPU1 */

/* A request for the CPU0 task: CPU1 fills it and the payload in FDM_IO() and sets POSTED; CPU0
   puts the reply payload at FDM_IO() and sets DONE; CPU1 sends it and sets IDLE. POWEROFF, already
   answered, CPU0 sets IDLE itself. A job CPU1 gave up waiting for stays until CPU0 sets DONE; CPU1
   drops that reply at the next request. */
#define FDM_JOB_IDLE 0u
#define FDM_JOB_POSTED 1u
#define FDM_JOB_RUNNING 2u	/* CPU0: a test program runs */
#define FDM_JOB_DONE 3u

struct fdm_job {
	uint32_t state;
	struct fdm_req req;
	int32_t status;
	uint32_t out_len;
};

/* Everything two cores touch, used through the uncached alias only (FENYKL_SHARED_PTR). */
struct fdm_shared {
	struct fdm_wifi_prm prm;	/* handed to the manager by its uncached address */
	/* CPU0 */
	uint32_t magic;
	uint32_t uptime_ms;
	uint32_t poweron_factor, wake_index, off_factor;
	uint32_t tail;
	uint32_t exec_busy;		/* a test program runs (or never returned) */
	uint32_t off_failed;		/* a power-off's Wi-Fi stop timed out */
	/* callbacks (the manager's task) */
	struct fdm_cb cb[FDM_NCB];
	char ioreq[FDM_IOREQ_MAX];
	/* both: see struct fdm_job */
	struct fdm_job job;
	/* CPU1 */
	int32_t boot;			/* acre_tsk result, FDM_BOOT_E* */
	uint32_t srv_state;
	uint32_t clients, requests, refused;
	uint32_t head, dropped;
	uint32_t stack_lo, stack_hi;	/* the server stack's filled range */
	uint32_t stack_free;		/* bytes from stack_lo never used, at the last PING */
	struct fdm_timing timing;
	struct fdm_rec rec[FDM_RING_N];
	struct fdm_text text;
};

extern struct fdm_shared fdm_sh;

#define FDM_SH() FENYKL_SHARED_PTR(fdm_sh)

/* --- devmon_proto.c --- */

uint32_t fdm_get32(const volatile uint8_t *p);
void fdm_put32(volatile uint8_t *p, uint32_t v);
/* Request header check: 0, FDM_EFRAME (bad magic or len > FDM_IO_SIZE). */
int fdm_hdr_check(const uint8_t hdr[FDM_HDR_SIZE], uint32_t *op, uint32_t *id, uint32_t *len);
/* Reply header. */
void fdm_hdr_put(uint8_t hdr[FDM_HDR_SIZE], uint32_t op, uint32_t id, int status, uint32_t len);
/* The arguments of a request with len payload bytes, of which the first min(len, FDM_ARGS_MAX)
   are at args: 0 or FDM_E* (op, arguments, the RAM ranges and the file name guard). */
int fdm_parse(uint32_t op, const uint8_t *args, uint32_t len, struct fdm_req *req);
/* [addr, addr + len) lies in the DRAM above the boot area (image and RAM, either alias). */
bool fdm_ram_ok(uint32_t addr, uint32_t len);
/* WRITE: fdm_ram_ok and no byte in the monitor's payload area (payload, .bss, FENYKL task stack)
   or the loader's stub and call patch, in either alias. */
bool fdm_write_ok(uint32_t addr, uint32_t len);
/* An address of the uncached alias. */
bool fdm_uncached(uint32_t addr);
/* PUT's temporary file, never a PUT target. */
#define FDM_TMP_NAME "FENYKL.TMP"
/* The loader's stable image: read only for the monitor. */
#define FDM_BIN_NAME "FENYKL.BIN"
/* The card names the monitor knows; everything else is FDM_NAME_BAD. */
enum fdm_name {
	FDM_NAME_BAD,
	FDM_NAME_FENYKL,	/* FENYKL.<1-3 of A-Z 0-9> but the two below */
	FDM_NAME_TMP,		/* FENYKL.TMP */
	FDM_NAME_BIN,		/* FENYKL.BIN */
	FDM_NAME_SHOT,		/* SHOT<4 digits>.PPM, the screenshot library's files */
	FDM_NAME_PHOTO,		/* DCIM/<3 digits, 5 of A-Z 0-9>/<1-8 of A-Z 0-9 _>.JPG, .ORF or .MOV */
	FDM_NAME_DIR,		/* DCIM or DCIM/<3 digits, 5 of A-Z 0-9> */
};
/* The kind of a path field: '/' separators, upper case, NUL-padded to the end of the field. */
enum fdm_name fdm_name_kind(const char name[FDM_PATH_FIELD]);
/* The one name rule of every file op (and of every open and remove): 0, FDM_ENAME or
   FDM_EPROTECT (PUT or DEL of FENYKL.BIN). FGET reads FENYKL.*, SHOT and PHOTO names, FLIST lists
   DIR names, FPUT writes FENYKL.* but TMP and BIN, FDEL removes FENYKL.* and TMP. */
int fdm_name_check(uint32_t op, const char name[FDM_PATH_FIELD]);
/* "C:\" and the name, each '/' turned into a backslash; out has room for FDM_PATH_MAX bytes (a
   listing appends "\*"). */
#define FDM_PATH_MAX (3 + FDM_PATH_FIELD + 2)
void fdm_path(char *out, const char name[FDM_PATH_FIELD]);
/* FLIST reply: u32 next (the start of the next page, 0 at the end), then entries of the name field
   (FDM_NAME_FIELD), u32 size and u32 write time (FAT: date << 16 | time). */
#define FDM_ENTRY_SIZE (FDM_NAME_FIELD + 8u)
#define FDM_LIST_MAX ((FDM_IO_DATA - 4u) / FDM_ENTRY_SIZE)

/* --- CPU0 file jobs GET and LIST, pure: the firmware's file layer comes in struct fdm_fileops
   (the fw_rpc_* and fw_fileif_* calls on the camera, mocks in the simulator) --- */

/* The find record of fileif_findfirst/findnext (firmware.toml). */
struct fdm_find {
	uint32_t attr;		/* FAT attribute */
	uint32_t ctime;
	uint32_t mtime;		/* last write: date << 16 | time */
	uint32_t mtime2;
	uint32_t size, size_hi;
	char name[0x100];
};

_Static_assert(sizeof(struct fdm_find) == 0x118, "find record");

#define FDM_ATTR_DIR 0x10u
#define FDM_ATTR_VOLUME 0x08u
#define FDM_SEEK_SET 2u		/* fileif_fseek: from the start */
#define FDM_LIST_MS 25000u	/* LIST gives up: under the server's 30 s wait */

struct fdm_fileops {
	void *(*fopen)(const char *path, const char *mode);
	int (*fclose)(void *fp);
	int (*fread)(void *buf, unsigned int size, unsigned int n, void *fp);
	int (*fseek)(void *fp, int offset, unsigned int whence);
	int (*findfirst)(const char *pattern, void *info);
	int (*findnext)(void *info, int handle);
	int (*findclose)(int handle);
	uint32_t (*now_us)(void);	/* FENYKL_TIMER_NOW */
	void (*err)(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2);	/* %s: text now */
};

/* GET: opens the name (the read rule), seeks to offset a[0], reads a[1] bytes to out, closes;
   0, FDM_ENAME, FDM_ENOENT (the open failed: no such file, or a card error the open hides) or
   FDM_EIO. *out_len: the bytes read, fewer at the end. */
int fdm_get(const struct fdm_fileops *ops, const struct fdm_req *q, volatile uint8_t *out,
	    uint32_t *out_len);
/* LIST of the DIR name: u32 next and the entries from index a[0] (counting listed entries only)
   to out, at most FDM_LIST_MAX; next = the index after the last, 0 at the end. Every findnext
   rescans the folder from its start: FDM_ETIMEOUT after FDM_LIST_MS. The handle is closed on
   every path; 0, FDM_ENAME, FDM_ENOENT (no such folder), FDM_EIO (findclose failed). */
int fdm_list(const struct fdm_fileops *ops, const struct fdm_req *q, volatile uint8_t *out,
	     uint32_t *out_len);

/* zlib CRC-32: crc of the bytes so far (0 to start). */
uint32_t fdm_crc32(uint32_t crc, const volatile uint8_t *p, uint32_t n);
/* A test program header at the start of slot: 0 or FDM_ETP. */
int fdm_tp_check(const volatile struct fenykl_tp_hdr *h, uint32_t slot);
/* fw_sprintf's subset, bounded: %d %u %x %X %c %s %% (a width zero-pads numbers; other
   conversions are printed as their letter) of up to n arguments into out[size], always
   NUL-terminated; the length. */
unsigned int fdm_format(char *out, unsigned int size, const char *fmt, const uint32_t *args,
			unsigned int n);
/* PUT sequencing (CPU0): the chunks of one file in order. */
struct fdm_put {
	bool open;		/* the temp file holds the first done bytes of name */
	char name[FDM_PATH_FIELD];
	uint32_t total, crc, done;
};

#define FDM_PUT_FIRST 1		/* offset 0: start the temp file (the state restarts) */
#define FDM_PUT_NEXT 2		/* append to it */
/* The chunk q against st: FDM_PUT_FIRST, FDM_PUT_NEXT, or FDM_EARGS (out of order, another file
   or total/crc changed; st is closed). The caller adds q->n to done once the bytes are written. */
int fdm_put_chunk(struct fdm_put *st, const struct fdm_req *q);
/* Decimal or (hex) 0x digits of v at out, no terminator; the length. */
unsigned int fdm_utoa(char *out, uint32_t v, bool hex);
/* PING payload from the shared facts; the length (< FDM_PING_MAX). */
#define FDM_PING_MAX 224u
unsigned int fdm_ping_text(char *out, const volatile struct fdm_shared *s);
/* Appends n bytes to the text log (one writer at a time). */
void fdm_text_put(volatile struct fdm_text *t, const char *s, unsigned int n);
/* The bytes LOG returns for a log with wr bytes written, starting at byte since or later: their
   count, the first one's absolute index in *first (its ring offset is *first % FDM_TEXT_SIZE). */
uint32_t fdm_text_span(uint32_t wr, uint32_t since, uint32_t *first);

/* The power-off sequence, in CPU0 task ticks: by the timed test (option devmon.off) or at once
   on request (fdm_off_request, the POWEROFF op). */
#define FDM_TICK_MS 100
#define FDM_TICKS(s) ((s) * (1000 / FDM_TICK_MS))
#define FDM_OFF_STOP_TICK FDM_TICKS(585)
#define FDM_OFF_INJECT_TICK FDM_TICKS(590)
#define FDM_OFF_STOP_TIMEOUT FDM_TICKS(30)

enum fdm_off_state {
	FDM_OFF_WAIT,		/* before the stop tick */
	FDM_OFF_STOPPING,	/* wifi_mngr_stop called, waiting for the stop */
	FDM_OFF_READY,		/* stopped (or never started): inject at the inject tick */
	FDM_OFF_DONE,
};

enum fdm_off_act {
	FDM_ACT_NONE,
	FDM_ACT_STOP,		/* call wifi_mngr_stop now */
	FDM_ACT_STOPPED,	/* the stop finished */
	FDM_ACT_TIMEOUT,	/* the stop did not finish in time: no inject */
	FDM_ACT_INJECT,		/* inject the Off level now */
};

/* Between the finished stop and the inject: the probe this came from was proven with 5 s. */
#define FDM_OFF_GAP FDM_TICKS(5)

struct fdm_off {
	enum fdm_off_state state;
	uint32_t stop_tick, stop_calls, ready_tick;
	bool now;		/* requested: no waiting for the stop and inject ticks */
	bool failed;		/* the stop timed out: never an inject this boot */
};

/* One step at tick: wifi_up = wifi_mngr_start was accepted, stop_calls = the stop callback's
   count, wlan = the WLAN state word. The stop is done when the count moves or wlan reads 0. */
enum fdm_off_act fdm_off_step(struct fdm_off *o, uint32_t tick, bool wifi_up, uint32_t stop_calls,
			      uint32_t wlan);
/* The POWEROFF op: run the sequence from its current state without the tick waits. */
void fdm_off_request(struct fdm_off *o);

/* KEY: the buttons the monitor may press (enum fdm_key, the index on the wire), their codes
   for ui_key_inject_level (the key layer's map, lib/key_em10ii.c). FULL is pressed as a half
   press, then the full press, released in reverse. */
enum fdm_key {
	FDM_KEY_OK, FDM_KEY_UP, FDM_KEY_DOWN, FDM_KEY_LEFT, FDM_KEY_RIGHT, FDM_KEY_INFO,
	FDM_KEY_MENU, FDM_KEY_PLAY, FDM_KEY_DEL, FDM_KEY_FN, FDM_KEY_FN2, FDM_KEY_FN3,
	FDM_KEY_REC, FDM_KEY_HALF, FDM_KEY_FULL, FDM_KEY_N,
};
#define FDM_HOLD_MAX 10000u	/* ms */
uint32_t fdm_key_code(uint32_t key);

/* DIAL: the mode-dial positions the monitor may set (enum fdm_dial, the index on the wire) and
   their values for ui_key_inject_level(0x02021901, value), the values the dial's ladder gives. */
enum fdm_dial {
	FDM_DIAL_P, FDM_DIAL_A, FDM_DIAL_S, FDM_DIAL_M, FDM_DIAL_MOVIE, FDM_DIAL_STORY,
	FDM_DIAL_SCN, FDM_DIAL_ART, FDM_DIAL_IAUTO, FDM_DIAL_N,
};
uint32_t fdm_dial_value(uint32_t pos);

/* The key layer for DIAL: fw_ui_key_get_state and fw_ui_key_inject_level on the camera, a
   recording stub in the simulator. */
struct fdm_keyops {
	unsigned int (*get_state)(unsigned int code);
	unsigned int (*inject_level)(unsigned int code, unsigned int level);
};
#define FDM_DIAL_CODE 0x02021901u

/* DIAL: the previous dial value to out, then the position's value injected once, never retried
   (a full key ring loses its records). 0 queued; else the inject's result, or 1 for a position
   outside the table (nothing injected). */
unsigned int fdm_dial_set(const struct fdm_keyops *k, uint32_t pos, volatile uint8_t *out);

/* Ops whose payload must sit in the job buffer: the CPU0 ops and WRITE (CPU0 writes cached
   addresses). */
bool fdm_op_is_job(uint32_t op);

/* --- devmon_cpu1_em10ii.c: the callback addresses in block order, and the cpucall_post function
   that starts the server task on CPU1 (argument: the uncached fdm_sh). --- */
void fdm_cb_addrs(uint32_t out[FDM_NCB]);
uint32_t fdm_cpu1_boot_addr(void);

#endif
