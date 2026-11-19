/*
 * Dev monitor, the CPU0 side (docs/devmon.md): the monitor task (fenykl_devmon_start) and the
 * test program task. RAM only besides FENYKL.* card files: no hook, no slot, no setting written.
 * - Start: the boot facts (power-on factor, wake index, off factor, lever and cable levels).
 * - 15 s after the start, with option devmon.ap on and the stock Wi-Fi off: the access point
 *   through wifi_mngr_start with our parameter block and callbacks, as the stock debug command
 *   "mngr start pri" does, bypassing the Wi-Fi sub-app (and its sleep hold: Sleep must be Off).
 *   Started once, never retried.
 * - The manager's start callback with status 0: the TCP server task is created on CPU1
 *   (devmon_cpu1_em10ii.c), as the stock Wi-Fi shell starts its server from the same callback.
 * - Every callback call and every CPU1 record is logged, to the card and to the text log that the
 *   LOG op returns.
 * - Jobs the server posts (struct fdm_job): test program upload and run, card files (FENYKL.* read
 *   and write, screenshots and DCIM photos read only, DCIM listings) and POWEROFF, polled every
 *   FDM_POLL_MS.
 * - Power-off (fdm_off_step), by the POWEROFF op or option devmon.off (default off: at 585 s):
 *   wifi_mngr_stop, the stop awaited (stop callback or WLAN state 0, 30 s at most), then (at 590 s
 *   for the timed test) the power switch's Off level injected after a last log flush; the task
 *   logs nothing more unless the inject fails. A stop that does not finish skips the inject.
 */
#include <cfg.h>
#include <log.h>
#include <devmon.h>
#include "devmon_int.h"

#define FDM_SECTIONS 0x7Fu		/* section_loaded bits of sec1..sec7 */
#define FDM_SECTION_TRIES 600
#define FDM_TASK_STACK 0x2000u
#define FDM_EXEC_STACK 0x3000u		/* the test program's stack */
#define FDM_POLL_MS 5			/* job poll period of the monitor task */
#define FDM_START_S 15
#define FDM_START_CB_WARN_S 30
#define FDM_STATUS_TICKS 100
#define FDM_FLUSH_TRIES 50		/* EBUSY retries of the last flush, 100 ms apart */
#define FDM_KEY_LEVER 0x0Cu		/* power switch */
#define FDM_KEY_USB 0x400u		/* UsbC cable */
#define FDM_LEVER_OFF 1u			/* ui_key_inject_level: 1 = inactive = Off */
#define FDM_CB_START 0u
#define FDM_CB_STOP 3u
#define FDM_TEXT_LINE 192u

FENYKL_LOG_MODULE(fdm_log, "devmon", FENYKL_LOG_INFO);

static FENYKL_CFG_GROUP(fdm_grp, "Dev monitor", 1);
FENYKL_CFG_BOOL(fdm_ap_on, "devmon.ap", "Wi-Fi monitor", &fdm_grp, 1);
FENYKL_CFG_BOOL(fdm_off_on, "devmon.off", "Power-off test", &fdm_grp, 0);

enum fdm_state {
	FDM_WAIT,	/* before the start delay */
	FDM_STARTED,	/* wifi_mngr_start queued, waiting for the start callback */
	FDM_SERVING,	/* server posted to CPU1 */
	FDM_IDLE,	/* nothing more to start */
};

struct fdm_task {
	uint32_t ticks;
	enum fdm_state state;
	uint32_t started_at;
	bool wifi_up;
	bool warned;
	uint32_t cb_seen[FDM_NCB];
	int32_t boot_seen;
	uint32_t srv_seen, dropped_seen;
	struct fdm_off off;
	/* FENYKL.* files: an open PUT temp file and an open GET file */
	struct fdm_put put;		/* no file stays open between requests */
	/* the test program task */
	int exec_task;
	uint32_t exec_t0, exec_timeout_ms;
};

/* The test program task's state: the monitor task sets RUN, the test program task DONE. */
#define FDM_EXEC_IDLE 0u
#define FDM_EXEC_RUN 1u
#define FDM_EXEC_DONE 2u

struct fdm_exec {
	volatile uint32_t state;
	uint32_t entry, arg;
	int32_t result;
	uint32_t us;
};

static struct fdm_exec fx;

static struct fdm_task dt;

static const char *const fdm_cb_names[FDM_NCB] = {
	"start", "connect", "disconnect", "stop", "+0xBC", "+0xC0", "error", "ioreq",
};

static void fdm_text_line(const char *msg, unsigned int len)
{
	char line[FDM_TEXT_LINE];
	unsigned int n = fdm_utoa(line, dt.ticks * FDM_TICK_MS, false);
	uint32_t psw;

	line[n++] = ' ';
	for (unsigned int i = 0; i < len && n < FDM_TEXT_LINE - 1; i++)
		line[n++] = msg[i];
	line[n++] = '\n';
	psw = fenykl_irq_save();	/* the monitor and the test program task both write */
	fdm_text_put(&FDM_SH()->text, line, n);
	fenykl_irq_restore(psw);
}

/* Set right before the power-off inject: nothing is logged after it. */
static bool fdm_quiet;

/* A log record and the same line, formatted, in the text log; text: the card log gets the
   formatted text too (for %s of a string that is gone by the flush). */
static void fdm_say4(unsigned int level, bool text, const char *fmt, uint32_t a0, uint32_t a1,
		     uint32_t a2, uint32_t a3)
{
	const uint32_t args[] = { a0, a1, a2, a3 };
	char msg[FDM_TEXT_LINE];
	unsigned int n;

	if (fdm_quiet)
		return;
	n = fdm_format(msg, sizeof(msg), fmt, args, 4);
	if (text)
		fenykl_log_text(&fdm_log, level, msg, n);
	else
		fenykl_log_rec(&fdm_log, level, fmt, a0, a1, a2, a3);
	fdm_text_line(msg, n);
}

#define fdm_say(fmt, ...) fdm_say4(FENYKL_LOG_INFO, false, fmt, __FENYKL_LOG_ARGS(__VA_ARGS__))
#define fdm_err(fmt, ...) fdm_say4(FENYKL_LOG_ERROR, false, fmt, __FENYKL_LOG_ARGS(__VA_ARGS__))
#define fdm_say_text(fmt, ...) fdm_say4(FENYKL_LOG_INFO, true, fmt, __FENYKL_LOG_ARGS(__VA_ARGS__))
#define fdm_err_text(fmt, ...) fdm_say4(FENYKL_LOG_ERROR, true, fmt, __FENYKL_LOG_ARGS(__VA_ARGS__))

static void fdm_boot_facts(void)
{
	volatile struct fdm_shared *s = FDM_SH();

	s->poweron_factor = fenykl_rd32(FW_POWERON_FACTOR);
	s->wake_index = fenykl_rd16(FW_WAKE_INDEX);
	s->off_factor = fenykl_rd8(FW_OFF_FACTOR);
	fdm_say("boot: power-on factor %x, wake index %u, off factor %u", s->poweron_factor,
	       s->wake_index, s->off_factor);
	fdm_say("boot: reset cause %x, lever word %x", fenykl_rd32(FW_RESET_CAUSE),
	       fenykl_rd32(FW_BOOT_PWR_WORD));
	fdm_say("boot: power switch level %u, cable level %u (0 = active)",
	       fw_ui_key_get_state(FDM_KEY_LEVER), fw_ui_key_get_state(FDM_KEY_USB));
}

/* Our block as the debug command fills it, without its two optional strings. */
static void fdm_fill_prm(volatile struct fdm_wifi_prm *p)
{
	static const char ssid[] = FDM_SSID, key[] = FDM_KEY;
	volatile uint8_t *b = (volatile uint8_t *)p;
	uint32_t cb[FDM_NCB];

	for (unsigned int i = 0; i < sizeof(*p); i++)
		b[i] = 0;
	for (unsigned int i = 0; i < sizeof(ssid); i++)
		p->ssid[i] = ssid[i];
	for (unsigned int i = 0; i < sizeof(key); i++)
		p->key[i] = key[i];
	p->net_mode = 2;
	p->sec_mode = 3;
	p->enc_mode = 1;
	p->conn_mode = 0;
	p->app_mode = 0;
	fdm_cb_addrs(cb);
	for (unsigned int i = 0; i < FDM_NCB; i++)
		p->cb[i] = cb[i];
}

static uint32_t fdm_wlan_state(void)
{
	return fenykl_rd32(FW_WLAN_STATE) | fenykl_rd32((uint32_t)FENYKL_UNCACHED(FW_WLAN_STATE));
}

static void fdm_wifi_start(void)
{
	volatile struct fdm_shared *s = FDM_SH();
	uint32_t wlan = fdm_wlan_state();
	unsigned int r;

	dt.state = FDM_IDLE;
	if (!fdm_ap_on) {
		fdm_say("access point off (option devmon.ap)");
		return;
	}
	fdm_say("pre-start: wlan state %x", wlan);
	if (wlan) {
		fdm_err("the stock Wi-Fi is up: no start");
		return;
	}
	fdm_fill_prm(&s->prm);
	r = fw_wifi_mngr_start((void *)&s->prm);
	fdm_say("wifi_mngr_start(%x) ssid %s -> %u", (uint32_t)&s->prm, FDM_SSID, r);
	if (r)
		return;
	dt.wifi_up = true;
	dt.started_at = dt.ticks;
	dt.state = FDM_STARTED;
}

/* The ioreq text copied into the record: the shared buffer changes with the next call. */
static void fdm_say_ioreq(volatile const char *what)
{
	static const char pre[] = "callback ioreq text: ";
	char msg[sizeof(pre) - 1 + FDM_IOREQ_MAX];
	unsigned int n = 0;

	for (unsigned int i = 0; pre[i]; i++)
		msg[n++] = pre[i];
	for (unsigned int i = 0; i < FDM_IOREQ_MAX - 1 && what[i]; i++)
		msg[n++] = what[i];
	fenykl_log_text(&fdm_log, FENYKL_LOG_INFO, msg, n);
	fdm_text_line(msg, n);
}

static void fdm_poll_callbacks(void)
{
	volatile struct fdm_shared *s = FDM_SH();

	for (unsigned int i = 0; i < FDM_NCB; i++) {
		volatile struct fdm_cb *cb = &s->cb[i];
		uint32_t calls = cb->calls;

		if (calls == dt.cb_seen[i])
			continue;
		dt.cb_seen[i] = calls;
		fdm_say("callback %s: calls %u, arg %x, core %u", fdm_cb_names[i], calls, cb->arg,
		       cb->core);
		if (i == FDM_CB_IOREQ)
			fdm_say_ioreq(s->ioreq);
	}
}

/* CPU1 records into both logs, in order. */
static void fdm_drain(void)
{
	volatile struct fdm_shared *s = FDM_SH();
	uint32_t head = s->head, tail = s->tail;

	for (; tail != head; tail++) {
		volatile struct fdm_rec *r = &s->rec[tail % FDM_RING_N];

		fdm_say4(FENYKL_LOG_INFO, false, (const char *)r->fmt, r->a[0], r->a[1], r->a[2], 0);
	}
	s->tail = tail;
	if (s->dropped != dt.dropped_seen) {
		dt.dropped_seen = s->dropped;
		fdm_err("cpu1 ring: %u records dropped", dt.dropped_seen);
	}
}

static void fdm_follow(void)
{
	volatile struct fdm_shared *s = FDM_SH();
	uint32_t status;
	int r;

	if (s->boot != dt.boot_seen || s->srv_state != dt.srv_seen) {
		dt.boot_seen = s->boot;
		dt.srv_seen = s->srv_state;
		fdm_say("server: boot %d, state %u", dt.boot_seen, dt.srv_seen);
	}
	if (dt.state != FDM_STARTED)
		return;
	if (!dt.cb_seen[FDM_CB_START]) {
		if (!dt.warned && dt.ticks - dt.started_at >= FDM_TICKS(FDM_START_CB_WARN_S)) {
			dt.warned = true;
			fdm_err("no start callback %u s after wifi_mngr_start", FDM_START_CB_WARN_S);
		}
		return;
	}
	dt.state = FDM_IDLE;
	status = s->cb[FDM_CB_START].arg;
	if (status) {
		fdm_err("start callback status %x (0x20: transport failed): no server", status);
		return;
	}
	r = fw_cpucall_post((void *)fdm_cpu1_boot_addr(), (void *)s, 1);
	fdm_say("start status 0 on core %u: server boot posted to cpu1 -> %d",
	       s->cb[FDM_CB_START].core, r);
	if (!r)
		dt.state = FDM_SERVING;
}

/* The last flush, then the inject. Returns only if the inject failed: after a queued inject the
   app unmounts the card, so this task never logs again. */
static void fdm_inject_off(void)
{
	unsigned int i;
	unsigned int r;
	int flush;

	fdm_say("power-off test: inject power switch %x level %u (Off) at timer %u; no log after it",
	       FDM_KEY_LEVER, FDM_LEVER_OFF, FENYKL_TIMER_NOW());
	for (i = 0; i < FDM_FLUSH_TRIES; i++) {
		flush = fenykl_log_flush();
		if (flush != FENYKL_LOG_EBUSY)
			break;
		fenykl_task_sleep(FDM_TICK_MS);
	}
	fdm_quiet = true;
	r = fw_ui_key_inject_level(FDM_KEY_LEVER, FDM_LEVER_OFF);
	if (!r)
		for (;;)
			fenykl_task_sleep(1000);
	fdm_quiet = false;
	fdm_err("power-off test: inject -> %u (log flush %d after %u tries): camera stays on", r,
	       flush, i);
}

static void fdm_power_off_test(void)
{
	uint32_t stop_calls = FDM_SH()->cb[FDM_CB_STOP].calls;

	if (!fdm_off_on && !dt.off.now)
		return;
	switch (fdm_off_step(&dt.off, dt.ticks, dt.wifi_up, stop_calls, fdm_wlan_state())) {
	case FDM_ACT_NONE:
		break;
	case FDM_ACT_STOP:
		fdm_say("wifi_mngr_stop -> %d (before the power-off test)", fw_wifi_mngr_stop());
		break;
	case FDM_ACT_STOPPED:
		fdm_say("Wi-Fi stopped after %u ms: stop callbacks %u, wlan state %x",
		       (dt.ticks - dt.off.stop_tick) * FDM_TICK_MS, stop_calls, fdm_wlan_state());
		break;
	case FDM_ACT_TIMEOUT:
		FDM_SH()->off_failed = 1;
		fdm_err("Wi-Fi not stopped after %u s (wlan state %x): no power-off test",
		       FDM_OFF_STOP_TIMEOUT / (1000 / FDM_TICK_MS), fdm_wlan_state());
		break;
	case FDM_ACT_INJECT:
		fdm_inject_off();
		break;
	}
}

static void fdm_status(void)
{
	volatile struct fdm_shared *s = FDM_SH();

	fdm_say("status: clients %u, requests %u, refused %u, wlan state %x", s->clients,
	       s->requests, s->refused, fdm_wlan_state());
}

/* --- card files: every open, remove and listing checks the name rule first (fdm_fpath here,
   fdm_name_check in fdm_get and fdm_list); each request opens and
   closes its file, so none (FENYKL.LOG above all) stays open --- */

/* The path of a name field, false (and nothing to open) unless the name rule allows op: FGET for
   reads, FDEL for writes and removes (FENYKL.BIN is never created, written or removed), FLIST. */
static bool fdm_fpath(char *out, const char name[FDM_PATH_FIELD], uint32_t op)
{
	if (fdm_name_check(op, name))
		return false;
	fdm_path(out, name);
	return true;
}

static void *fdm_fopen(const char name[FDM_PATH_FIELD], const char *mode)
{
	char path[FDM_PATH_MAX];
	bool read = mode[0] == 'r' && !mode[1];

	if (!fdm_fpath(path, name, read ? FDM_OP_FGET : FDM_OP_FDEL))
		return NULL;
	return fw_rpc_fopen(path, mode);
}

/* Removes the file; 0 if it is gone afterwards ("w" does not truncate: a file must be gone). */
static int fdm_fremove(const char name[FDM_PATH_FIELD])
{
	char path[FDM_PATH_MAX];
	void *fp;
	int r;

	if (!fdm_fpath(path, name, FDM_OP_FDEL))
		return FDM_EPROTECT;
	r = fw_rpc_fremove(path);
	fp = fw_rpc_fopen(path, "r");
	if (!fp)
		return 0;
	fw_rpc_fclose(fp);
	fdm_err_text("%s: rpc_fremove %d and the file is still there", (uint32_t)path, (uint32_t)r);
	return FDM_EIO;
}

/* Size and CRC-32 of a file, read through the job buffer. */
static int fdm_fsum(const char name[FDM_PATH_FIELD], uint32_t *size, uint32_t *crc)
{
	void *fp = fdm_fopen(name, "r");
	int n;

	*size = 0;
	*crc = 0;
	if (!fp)
		return FDM_EIO;
	while ((n = fw_rpc_fread((void *)FDM_IO(), 1, FDM_IO_DATA, fp)) > 0) {
		*crc = fdm_crc32(*crc, FDM_IO(), (uint32_t)n);
		*size += (uint32_t)n;
	}
	return fw_rpc_fclose(fp) ? FDM_EIO : 0;
}

static int fdm_fcopy(const char from[FDM_PATH_FIELD], const char to[FDM_PATH_FIELD])
{
	void *in = fdm_fopen(from, "r"), *out;
	int n, r = 0;

	if (!in)
		return FDM_EIO;
	out = fdm_fopen(to, "w");
	if (!out) {
		fw_rpc_fclose(in);
		return FDM_EIO;
	}
	while (!r && (n = fw_rpc_fread((void *)FDM_IO(), 1, FDM_IO_DATA, in)) > 0)
		if (fw_rpc_fwrite((void *)FDM_IO(), 1, (unsigned int)n, out) != n)
			r = FDM_EIO;
	if (fw_rpc_fclose(out))
		r = FDM_EIO;
	fw_rpc_fclose(in);
	return r;
}

static int fdm_fcheck(const char name[FDM_PATH_FIELD], uint32_t total, uint32_t crc)
{
	uint32_t size, sum;
	int r = fdm_fsum(name, &size, &sum);

	fdm_say_text("put check %s: %d, size %u crc %x", (uint32_t)name, (uint32_t)r, size, sum);
	if (r)
		return r;
	return size == total && sum == crc ? 0 : FDM_ECRC;
}

#define FDM_PUT_TRIES 3

/* The temp file is complete: check it, then replace the target with a copy and check that,
   retried from the checked temp file; drop the temp. Not atomic (the firmware API has no
   rename): until a copy checks out there is no good target, but the checked temp file stays. */
static int fdm_put_commit(const char name[FDM_PATH_FIELD], uint32_t total, uint32_t crc)
{
	static const char tmp[FDM_PATH_FIELD] = FDM_TMP_NAME;
	int r = fdm_fcheck(tmp, total, crc);

	for (unsigned int i = 0; !r && i < FDM_PUT_TRIES; i++) {
		r = fdm_fremove(name);
		if (!r)
			r = fdm_fcopy(tmp, name);
		if (!r)
			r = fdm_fcheck(name, total, crc);
		if (!r)
			break;
		fdm_err_text("put %s: copy %u failed (%d)", (uint32_t)name, i + 1, (uint32_t)r);
		if (i + 1 < FDM_PUT_TRIES)
			r = 0;
	}
	if (r) {
		fdm_say_text("put %s: %d, temp file kept", (uint32_t)name, (uint32_t)r);
		return r;
	}
	fdm_say_text("put %s: done, temp removed %d", (uint32_t)name, (uint32_t)fdm_fremove(tmp));
	return 0;
}

static int fdm_fput(const struct fdm_req *q)
{
	static const char tmp[FDM_PATH_FIELD] = FDM_TMP_NAME;
	int step = fdm_put_chunk(&dt.put, q);
	void *fp;
	int r, w = 0;

	if (step < 0)
		return step;
	if (step == FDM_PUT_FIRST) {
		r = fdm_fremove(tmp);
		if (r) {
			dt.put.open = false;
			return r;
		}
	}
	fp = fdm_fopen(tmp, step == FDM_PUT_FIRST ? "w" : "a");
	if (fp && q->n)
		w = fw_rpc_fwrite((void *)(FDM_IO() + q->data), 1, q->n, fp);
	if (!fp || w != (int)q->n || fw_rpc_fclose(fp)) {
		dt.put.open = false;
		return FDM_EIO;
	}
	dt.put.done += q->n;
	if (dt.put.done < dt.put.total)
		return 0;
	dt.put.open = false;
	return fdm_put_commit(q->name, dt.put.total, dt.put.crc);
}

static uint32_t fdm_now_us(void)
{
	return FENYKL_TIMER_NOW();
}

static void fdm_ops_err(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2)
{
	fdm_say4(FENYKL_LOG_ERROR, true, fmt, a0, a1, a2, 0);
}

/* GET and LIST (devmon_proto.c) through the firmware's file layer. */
static const struct fdm_fileops fdm_fw_files = {
	.fopen = fw_rpc_fopen,
	.fclose = fw_rpc_fclose,
	.fread = fw_rpc_fread,
	.fseek = fw_fileif_fseek,
	.findfirst = fw_fileif_findfirst,
	.findnext = fw_fileif_findnext,
	.findclose = fw_fileif_findclose,
	.now_us = fdm_now_us,
	.err = fdm_ops_err,
};

static int fdm_file_job(const struct fdm_req *q, uint32_t *out_len)
{
	if (!fenykl_card_io_allowed())
		return FDM_ECARD;
	switch (q->op) {
	case FDM_OP_FPUT:
		return fdm_fput(q);
	case FDM_OP_FGET:
		return fdm_get(&fdm_fw_files, q, FDM_IO(), out_len);
	case FDM_OP_FDEL:
		return fdm_fremove(q->name);
	case FDM_OP_FLIST:
		return fdm_list(&fdm_fw_files, q, FDM_IO(), out_len);
	default:
		return FDM_EOP;
	}
}

/* KEY: press (level 0), hold, release (level 1), as the firmware's own press-then-release
   callers of ui_key_inject_level do; FULL with the half press around it. Nothing stays pressed:
   a failed press is released at once. */
#define FDM_LEVEL_ON 0u
#define FDM_LEVEL_OFF 1u
#define FDM_HALF_LEAD_MS 300	/* half press before the full press (AF) */

static int fdm_key(const struct fdm_req *q)
{
	uint32_t code = fdm_key_code(q->a[0]), half = fdm_key_code(FDM_KEY_HALF);
	bool full = q->a[0] == FDM_KEY_FULL;
	unsigned int r = 0;

	if (full) {
		r = fw_ui_key_inject_level(half, FDM_LEVEL_ON);
		if (!r)
			fenykl_task_sleep(FDM_HALF_LEAD_MS);
	}
	if (!r)
		r = fw_ui_key_inject_level(code, FDM_LEVEL_ON);
	if (!r)
		fenykl_task_sleep((int)q->a[1]);
	fw_ui_key_inject_level(code, FDM_LEVEL_OFF);
	if (full)
		fw_ui_key_inject_level(half, FDM_LEVEL_OFF);
	if (r)
		fdm_err("key %x: inject %u", code, r);
	return r ? FDM_EIO : 0;
}

/* DIAL: the position's value pushed as the dial's ladder pushes it; UI_IN posts the dial event
   and the app changes mode. It lasts until the next edge of the ladder's interrupt (a dial
   movement) or the next boot. The reply is the previous value. */
static const struct fdm_keyops fdm_fw_keys = {
	.get_state = fw_ui_key_get_state,
	.inject_level = fw_ui_key_inject_level,
};

static int fdm_dial(const struct fdm_req *q, uint32_t *out_len)
{
	unsigned int r = fdm_dial_set(&fdm_fw_keys, q->a[0], FDM_IO());

	if (r) {
		fdm_err("dial %u: inject %u", q->a[0], r);
		return FDM_EIO;
	}
	fdm_say("dial %x -> %x", fdm_get32(FDM_IO()), fdm_dial_value(q->a[0]));
	*out_len = 4;
	return 0;
}

/* WRITE of a cached address: CPU0's cache gets the bytes and goes to DRAM. */
static void fdm_write_cached(const struct fdm_req *q)
{
	uint8_t *dst = (uint8_t *)q->a[0];
	const volatile uint8_t *src = FDM_IO() + q->data;

	for (uint32_t i = 0; i < q->n; i++)
		dst[i] = src[i];
	fenykl_dcache_flush();
}

/* --- test programs --- */

static void fdm_tp_log(const char *fmt, uint32_t a0, uint32_t a1, uint32_t a2)
{
	const uint32_t args[] = { a0, a1, a2 };
	char msg[FDM_TEXT_LINE];
	unsigned int n;

	if (fdm_quiet)
		return;
	n = fdm_format(msg, sizeof(msg), fmt, args, 3);
	fenykl_log_text(&fdm_log, FENYKL_LOG_INFO, msg, n);
	fdm_text_line(msg, n);
}

static void fdm_tp_sleep(uint32_t ms)
{
	fenykl_task_sleep((int)ms);
}

static uint32_t fdm_tp_timer(void)
{
	return FENYKL_TIMER_NOW();
}

static uint32_t fdm_tp_rd32(uint32_t addr)
{
	return *(volatile uint32_t *)addr;
}

static void fdm_tp_wr32(uint32_t addr, uint32_t v)
{
	*(volatile uint32_t *)addr = v;
}

static const struct fenykl_tp_api fdm_tp_api = {
	.version = FENYKL_TP_API,
	.size = sizeof(struct fenykl_tp_api),
	.log = fdm_tp_log,
	.sleep_ms = fdm_tp_sleep,
	.timer_us = fdm_tp_timer,
	.rd32 = fdm_tp_rd32,
	.wr32 = fdm_tp_wr32,
};

FENYKL_TASK(fdm_exec);

void fdm_exec(void)
{
	for (;;) {
		int (*entry)(const struct fenykl_tp_api *, uint32_t);
		uint32_t t0;

		fenykl_task_wait(-1);
		if (fx.state != FDM_EXEC_RUN)
			continue;
		entry = (int (*)(const struct fenykl_tp_api *, uint32_t))fx.entry;
		t0 = FENYKL_TIMER_NOW();
		fx.result = entry(&fdm_tp_api, fx.arg);
		fx.us = (FENYKL_TIMER_NOW() - t0) & FENYKL_TIMER_MASK;
		fx.state = FDM_EXEC_DONE;
	}
}

static int fdm_tpload(const struct fdm_req *q)
{
	uint8_t *dst = (uint8_t *)(FDM_SLOT_ADDR(q->a[0]) + q->a[1]);
	const volatile uint8_t *src = FDM_IO() + q->data;

	if (fx.state != FDM_EXEC_IDLE)
		return FDM_EBUSY;
	for (uint32_t i = 0; i < q->n; i++)
		dst[i] = src[i];
	return 0;
}

/* Checks the slot, clears .bss, writes the D-cache back and invalidates the I-cache (the upload
   went through the cached alias), then wakes the test program task. */
static int fdm_tprun(const struct fdm_req *q)
{
	const struct fenykl_tp_hdr *h = (const struct fenykl_tp_hdr *)FDM_SLOT_ADDR(q->a[0]);
	int r;

	if (fx.state != FDM_EXEC_IDLE || dt.exec_task < 0)
		return FDM_EBUSY;
	r = fdm_tp_check(h, q->a[0]);
	if (r)
		return r;
	for (uint32_t a = h->base + h->size; a < h->base + h->mem; a++)
		*(volatile uint8_t *)a = 0;
	fenykl_dcache_flush();
	r = fenykl_icache_invalidate();
	if (r) {
		fdm_err("tp slot %u: icache invalidate %d: not run", q->a[0], (uint32_t)r);
		return FDM_EICACHE;
	}
	fx.entry = h->entry;
	fx.arg = q->a[1];
	fx.state = FDM_EXEC_RUN;
	FDM_SH()->exec_busy = 1;
	dt.exec_t0 = FENYKL_TIMER_NOW();
	dt.exec_timeout_ms = q->a[2];
	fdm_say("tp slot %u: run entry %x arg %x", q->a[0], h->entry, q->a[1]);
	fenykl_task_wake(dt.exec_task);
	return 0;
}

static void fdm_job_done(volatile struct fdm_job *j, int status, uint32_t out_len)
{
	j->status = status;
	j->out_len = out_len;
	j->state = FDM_JOB_DONE;
}

/* A running test program: its result, or the timeout (the program keeps running; exec_busy
   stays until it returns). */
static void fdm_exec_follow(volatile struct fdm_job *j)
{
	uint32_t ms = ((FENYKL_TIMER_NOW() - dt.exec_t0) & FENYKL_TIMER_MASK) / 1000;

	if (fx.state == FDM_EXEC_DONE) {
		fdm_say("tp returned %d after %u us", (uint32_t)fx.result, fx.us);
		fx.state = FDM_EXEC_IDLE;
		FDM_SH()->exec_busy = 0;
		if (j->state != FDM_JOB_RUNNING)
			return;
		fdm_put32(FDM_IO(), (uint32_t)fx.result);
		fdm_put32(FDM_IO() + 4, fx.us);
		fdm_job_done(j, 0, 8);
		return;
	}
	if (j->state == FDM_JOB_RUNNING && ms >= dt.exec_timeout_ms) {
		fdm_err("tp still running after %u ms: timeout reply, the program cannot be stopped",
			ms);
		fdm_job_done(j, FDM_ETIMEOUT, 0);
	}
}

static void fdm_jobs(void)
{
	volatile struct fdm_job *j = &FDM_SH()->job;
	struct fdm_req q;
	uint32_t out_len = 0;
	int r;

	if (fx.state != FDM_EXEC_IDLE)
		fdm_exec_follow(j);
	if (j->state != FDM_JOB_POSTED)
		return;
	q = j->req;
	switch (q.op) {
	case FDM_OP_POWEROFF:
		fdm_say("POWEROFF requested");
		fdm_off_request(&dt.off);
		j->state = FDM_JOB_IDLE;	/* already answered */
		return;
	case FDM_OP_TPLOAD:
		r = fdm_tpload(&q);
		break;
	case FDM_OP_WRITE:
		fdm_write_cached(&q);
		r = 0;
		break;
	case FDM_OP_KEY:
		r = fdm_key(&q);
		break;
	case FDM_OP_DIAL:
		r = fdm_dial(&q, &out_len);
		break;
	case FDM_OP_TPRUN:
		r = fdm_tprun(&q);
		if (!r) {
			j->state = FDM_JOB_RUNNING;
			return;
		}
		break;
	default:
		r = fdm_file_job(&q, &out_len);
		break;
	}
	if (r)
		fdm_say("job op %x: %d", q.op, (uint32_t)r);
	fdm_job_done(j, r, r ? 0 : out_len);
}

FENYKL_TASK(fdm_main);

void fdm_main(void)
{
	volatile struct fdm_shared *s = FDM_SH();
	unsigned int i;

	fdm_say("devmon: ap %d, power-off test %d, exec task %d", fdm_ap_on, fdm_off_on,
		(uint32_t)dt.exec_task);
	fdm_boot_facts();
	for (i = 0; i < FDM_SECTION_TRIES; i++) {
		if ((fenykl_rd8(FW_SECTION_LOADED) & FDM_SECTIONS) == FDM_SECTIONS)
			break;
		fenykl_task_sleep(FDM_TICK_MS);
	}
	fdm_say("sections %x after %u polls", fenykl_rd8(FW_SECTION_LOADED), i);
	if (i == FDM_SECTION_TRIES) {
		fdm_err("sections not loaded: no Wi-Fi this boot");
		dt.state = FDM_IDLE;
	}
	/* ticks from the timer: a sleep lasts a tick longer than asked, and jobs take time */
	for (uint32_t last = FENYKL_TIMER_NOW(), us = 0;;) {
		uint32_t now;

		fenykl_task_sleep(FDM_POLL_MS);
		fdm_jobs();
		now = FENYKL_TIMER_NOW();
		us += (now - last) & FENYKL_TIMER_MASK;
		last = now;
		if (us < FDM_TICK_MS * 1000u)
			continue;
		us -= FDM_TICK_MS * 1000u;
		dt.ticks++;
		s->uptime_ms = dt.ticks * FDM_TICK_MS;
		fdm_poll_callbacks();
		fdm_drain();
		if (dt.state == FDM_WAIT && dt.ticks >= FDM_TICKS(FDM_START_S))
			fdm_wifi_start();
		fdm_follow();
		fdm_power_off_test();
		if (dt.ticks % FDM_STATUS_TICKS == 0)
			fdm_status();
	}
}

int fenykl_devmon_start(void)
{
	FDM_SH()->magic = FDM_MAGIC;
	dt.exec_task = fenykl_task_create("FENYKL_DEVMON_TP", fdm_exec_task, FENYKL_LIB_TASK_PRIO,
					  FDM_EXEC_STACK);
	return fenykl_task_create("FENYKL_DEVMON", fdm_main_task, FENYKL_LIB_TASK_PRIO,
				  FDM_TASK_STACK);
}
