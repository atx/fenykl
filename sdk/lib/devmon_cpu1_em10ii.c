/* Dev monitor, the code other cores run: the Wi-Fi manager's callbacks (its task), the boot function
   (CPU1's CPUCALL task) and the TCP server task on CPU1. It touches only its stacks, the uncached
   fdm_sh, .rodata and its core's registers: no cached .data/.bss, which CPU0 wrote. No log library
   call: its ring 1 has no records on this body, so CPU1 records go through fdm_sh.rec. */
#include "devmon_int.h"

FENYKL_SHARED struct fdm_shared fdm_sh;

#define FDM_SRV_PRIO 0x37	/* the stock Wi-Fi shell's server task */
#define FDM_SRV_STACK 0x800u
#define FDM_TA_ACT 2u
#define FDM_AF_INET 2
#define FDM_SOCK_STREAM 1
#define FDM_BACKLOG 1
#define FDM_ACCEPT_TRIES 3u
#define FDM_SMALL 32u		/* payloads up to this size are received on the stack */
#define FDM_JOB_POLL_MS 1
#define FDM_JOB_WAIT_MS 30000u	/* CPU0 ops; TPRUN adds its timeout */
/* The server stack's high-water mark: the kernel enters the task with SP = top - 4; at most
   FDM_STACK_ABOVE bytes lie between the stack top and fdm_srv's marker (its frame is smaller),
   so the fill starts at or above the true bottom. */
#define FDM_STACK_ABOVE 128u
#define FDM_STACK_GUARD 16u	/* the lowest bytes are never written */
#define FDM_STACK_MARGIN 64u	/* kept free below the filling function's own local */
#define FDM_STACK_FILL 0xA5u

/* T_CTSK as acre_tsk reads it (copied into the TCB). */
struct fdm_ctsk {
	uint32_t atr, exinf, entry, prio, stksz, stk;
};

struct fdm_sockaddr {
	uint16_t family, port;	/* port in network order */
	uint32_t addr;
	uint32_t zero[2];
};

/* Ring write: the CPU1 interrupt mask keeps the other CPU1 producer out; a full ring drops. */
static void fdm_put(volatile struct fdm_shared *s, const char *fmt, uint32_t a0, uint32_t a1,
		   uint32_t a2)
{
	volatile struct fdm_rec *r;
	uint32_t psw = fw_psw_get();
	uint32_t h;

	fw_irq_disable_all();
	h = s->head;
	if (h - s->tail >= FDM_RING_N) {
		s->dropped++;
		fw_irq_mask_restore(psw);
		return;
	}
	r = &s->rec[h % FDM_RING_N];
	r->t = FENYKL_TIMER_NOW();
	r->fmt = (uint32_t)fmt;
	r->a[0] = a0;
	r->a[1] = a1;
	r->a[2] = a2;
	s->head = h + 1;
	fw_irq_mask_restore(psw);
}

/* --- callbacks of the parameter block: count, last argument, core; CPU0 logs the changes --- */

static void fdm_cb_note(unsigned int i, uint32_t arg)
{
	volatile struct fdm_cb *cb = &FDM_SH()->cb[i];

	cb->arg = arg;
	cb->core = FENYKL_CORE_ID();
	cb->calls++;
}

#define FDM_CB(i)                                          \
	static int fdm_cb##i(int status)                   \
	{                                                 \
		fdm_cb_note(i, (uint32_t)status);          \
		return 0;                                 \
	}                                                 \
	FENYKL_CALLBACK(wifi_mngr_cb, fdm_cb##i)

FDM_CB(0);
FDM_CB(1);
FDM_CB(2);
FDM_CB(3);
FDM_CB(4);
FDM_CB(5);
FDM_CB(6);

static int fdm_cb7(unsigned int prm, const char *what)
{
	volatile char *dst = FDM_SH()->ioreq;
	unsigned int i = 0;

	if (what)
		for (; i < FDM_IOREQ_MAX - 1 && what[i]; i++)
			dst[i] = what[i];
	dst[i] = '\0';
	fdm_cb_note(FDM_CB_IOREQ, prm);
	return 0;
}
FENYKL_CALLBACK(wifi_mngr_ioreq_cb, fdm_cb7);

void fdm_cb_addrs(uint32_t out[FDM_NCB])
{
	out[0] = FENYKL_CB(wifi_mngr_cb, fdm_cb0);
	out[1] = FENYKL_CB(wifi_mngr_cb, fdm_cb1);
	out[2] = FENYKL_CB(wifi_mngr_cb, fdm_cb2);
	out[3] = FENYKL_CB(wifi_mngr_cb, fdm_cb3);
	out[4] = FENYKL_CB(wifi_mngr_cb, fdm_cb4);
	out[5] = FENYKL_CB(wifi_mngr_cb, fdm_cb5);
	out[6] = FENYKL_CB(wifi_mngr_cb, fdm_cb6);
	out[7] = FENYKL_CB(wifi_mngr_ioreq_cb, fdm_cb7);
}

/* --- the server --- */

static uint32_t fdm_us_since(uint32_t t0)
{
	return (FENYKL_TIMER_NOW() - t0) & FENYKL_TIMER_MASK;
}

static bool fdm_recv_all(int c, volatile void *buf, uint32_t n)
{
	uint8_t *p = (uint8_t *)(uintptr_t)buf;

	while (n) {
		int r = fw_olnet_recv(c, p, (int)n, 0);

		if (r <= 0)
			return false;
		p += r;
		n -= (uint32_t)r;
	}
	return true;
}

/* Flags 0: bit 0 of send() queues urgent data. */
static bool fdm_send_all(int c, const volatile void *buf, uint32_t n)
{
	return n == 0 || fw_olnet_send(c, (const void *)(uintptr_t)buf, (int)n, 0) == (int)n;
}

/* In READ's default pieces and pauses: one send() larger than the 8 KiB send ring stalls for
   seconds (docs/devmon.md 9). */
static bool fdm_send_paced(int c, const volatile uint8_t *p, uint32_t n)
{
	while (n) {
		uint32_t k = n < FDM_PIECE ? n : FDM_PIECE;

		if (!fdm_send_all(c, p, k))
			return false;
		p += k;
		n -= k;
		if (n)
			fw_OlyTaskSleep(FDM_PACE_MS);
	}
	return true;
}

static bool fdm_reply(int c, uint32_t op, uint32_t id, int status, const volatile void *p,
		      uint32_t n)
{
	uint8_t hdr[FDM_HDR_SIZE];

	fdm_hdr_put(hdr, op, id, status, status ? 0 : n);
	return fdm_send_all(c, hdr, sizeof(hdr)) &&
	       (status || fdm_send_paced(c, (const volatile uint8_t *)p, n));
}

/* READ: the bytes in pieces, timed into s->timing. */
static bool fdm_read(volatile struct fdm_shared *s, int c, uint32_t id, const struct fdm_req *q)
{
	struct fdm_timing t = { .piece = q->a[2], .pace_ms = q->a[3], .sock = (uint32_t)c };
	const uint8_t *p = (const uint8_t *)(uintptr_t)q->a[0];
	uint32_t left = q->a[1];
	uint32_t t0 = FENYKL_TIMER_NOW();
	uint8_t hdr[FDM_HDR_SIZE];
	bool ok;

	fdm_hdr_put(hdr, q->op, id, 0, left);
	ok = fdm_send_all(c, hdr, sizeof(hdr));
	while (ok && left) {
		uint32_t k = left < t.piece ? left : t.piece;
		uint32_t t1 = FENYKL_TIMER_NOW(), us;

		ok = fdm_send_all(c, p, k);
		us = fdm_us_since(t1);
		t.sends++;
		if (us > t.max_send_us)
			t.max_send_us = us;
		if (us >= FDM_STALL_US) {
			t.stalls++;
			t.stall_us += us;
		}
		if (ok)
			t.bytes += k;
		p += k;
		left -= k;
		if (t.pace_ms && left)
			fw_OlyTaskSleep(t.pace_ms);
	}
	t.us = fdm_us_since(t0);
	s->timing = t;
	return ok;
}

/* WRITE of an uncached address: DRAM holds the bytes when the reply goes out. */
static void fdm_write(const struct fdm_req *q, const volatile uint8_t *src)
{
	volatile uint8_t *dst = (volatile uint8_t *)q->a[0];

	for (uint32_t i = 0; i < q->n; i++)
		dst[i] = src[i];
}

/* Fills the unused server stack below this function's frame: from the bottom estimated from
   top (FDM_SRV_STACK below it) up to FDM_STACK_MARGIN below its own local. */
static __attribute__((noinline)) void fdm_stack_fill(volatile struct fdm_shared *s, uint32_t top)
{
	volatile uint8_t here = 0;
	uint32_t lo = top - FDM_SRV_STACK + FDM_STACK_GUARD;
	uint32_t hi = (uint32_t)&here - FDM_STACK_MARGIN;

	for (uint32_t a = lo; a < hi; a++)
		*(volatile uint8_t *)a = FDM_STACK_FILL;
	s->stack_lo = lo;
	s->stack_hi = hi;
}

/* The bytes from stack_lo up that still hold the fill: the stack's headroom (at least). */
static uint32_t fdm_stack_free(volatile struct fdm_shared *s)
{
	uint32_t a = s->stack_lo;

	while (a < s->stack_hi && *(volatile uint8_t *)a == FDM_STACK_FILL)
		a++;
	return a - s->stack_lo;
}

/* noinline: its buffer stays out of the READ and job paths' frames (0x800-byte stack). */
static __attribute__((noinline)) bool fdm_ping(volatile struct fdm_shared *s, int c, uint32_t id)
{
	char ping[FDM_PING_MAX];

	s->stack_free = fdm_stack_free(s);

	return fdm_reply(c, FDM_OP_PING, id, 0, ping, fdm_ping_text(ping, s));
}

static __attribute__((noinline)) bool fdm_reply_log(volatile struct fdm_shared *s, int c,
						    uint32_t id, const struct fdm_req *q)
{
	volatile struct fdm_text *t = &s->text;
	uint32_t wr = t->wr, first;
	uint32_t n = fdm_text_span(wr, q->a[0], &first);
	uint32_t off = first % FDM_TEXT_SIZE;
	uint32_t k = n < FDM_TEXT_SIZE - off ? n : FDM_TEXT_SIZE - off;
	uint8_t head[FDM_HDR_SIZE + 4];

	fdm_hdr_put(head, q->op, id, 0, 4 + n);
	fdm_put32(head + FDM_HDR_SIZE, first + n);
	return fdm_send_all(c, head, sizeof(head)) && fdm_send_all(c, &t->buf[off], k) &&
	       fdm_send_all(c, t->buf, n - k);
}

/* The CPU0 task's ops: post, wait (bounded), reply. A job given up stays posted until CPU0
   finishes it; until then CPU0 ops are refused (FDM_EBUSY). */
static bool fdm_job(volatile struct fdm_shared *s, int c, uint32_t id, const struct fdm_req *q)
{
	volatile struct fdm_job *j = &s->job;
	uint32_t wait_ms = FDM_JOB_WAIT_MS;
	uint32_t t0 = FENYKL_TIMER_NOW();

	if (q->op == FDM_OP_TPRUN)
		wait_ms += q->a[2];
	j->req = *q;
	j->status = 0;
	j->out_len = 0;
	j->state = FDM_JOB_POSTED;
	while (j->state != FDM_JOB_DONE) {
		if (fdm_us_since(t0) / 1000 >= wait_ms) {
			fdm_put(s, "op %x: no CPU0 result after %u ms", q->op, wait_ms, 0);
			return fdm_reply(c, q->op, id, FDM_ETIMEOUT, NULL, 0);
		}
		fw_OlyTaskSleep(FDM_JOB_POLL_MS);
	}
	j->state = FDM_JOB_IDLE;
	return fdm_reply(c, q->op, id, j->status, FDM_IO(), j->out_len);
}

/* Ops whose payload must sit in the job buffer: the CPU0 ops and WRITE (CPU0 writes cached
   addresses). */
static bool fdm_op_is_job(uint32_t op)
{
	return op == FDM_OP_WRITE || op == FDM_OP_KEY || op == FDM_OP_TPLOAD || op == FDM_OP_TPRUN ||
	       op == FDM_OP_FPUT || op == FDM_OP_FGET || op == FDM_OP_FDEL || op == FDM_OP_FLIST;
}

/* One request whose payload is in pay (len bytes); false: the connection is lost. */
static bool fdm_request(volatile struct fdm_shared *s, int c, uint32_t op, uint32_t id,
			const volatile uint8_t *pay, uint32_t len)
{
	uint8_t args[FDM_ARGS_MAX];
	struct fdm_req q;
	int r;

	for (uint32_t i = 0; i < len && i < FDM_ARGS_MAX; i++)
		args[i] = pay[i];
	r = fdm_parse(op, args, len, &q);
	s->requests++;
	if (r || fdm_op_is_job(op))
		fdm_put(s, "request %x id %u: %d", op, id, (uint32_t)r);
	if (r) {
		s->refused++;
		return fdm_reply(c, op, id, r, NULL, 0);
	}
	switch (q.op) {
	case FDM_OP_PING:
		return fdm_ping(s, c, id);
	case FDM_OP_READ:
		return fdm_read(s, c, id, &q);
	case FDM_OP_WRITE:
		if (!fdm_uncached(q.a[0]))
			return fdm_job(s, c, id, &q);
		fdm_write(&q, pay + q.data);
		return fdm_reply(c, op, id, 0, NULL, 0);
	case FDM_OP_LOG:
		return fdm_reply_log(s, c, id, &q);
	case FDM_OP_TIMING:
		return fdm_reply(c, op, id, 0, &s->timing, sizeof(s->timing));
	case FDM_OP_POWEROFF:
		/* the reply first: the sequence stops Wi-Fi */
		if (s->off_failed)
			return fdm_reply(c, op, id, FDM_EOFF, NULL, 0);
		if (s->job.state != FDM_JOB_IDLE)
			return fdm_reply(c, op, id, FDM_EBUSY, NULL, 0);
		if (!fdm_reply(c, op, id, 0, NULL, 0))
			return false;
		s->job.req = q;
		s->job.state = FDM_JOB_POSTED;
		return true;
	case FDM_OP_KEY:
	case FDM_OP_TPLOAD:
	case FDM_OP_TPRUN:
	case FDM_OP_FPUT:
	case FDM_OP_FGET:
	case FDM_OP_FDEL:
	case FDM_OP_FLIST:
		return fdm_job(s, c, id, &q);
	}
	return false;
}

/* Frames until the peer closes, an error or a bad frame. Small payloads land on the stack, the
   rest in the job buffer, which is free only while no job is outstanding. A silent peer (a PC
   gone without a FIN) ends in recv's timeout: 60 s, the default every socket gets (0x60E1D44B
   sets sock +0x3C = 60000; recv waits with it), so the next client is served after it. */
static void fdm_client(volatile struct fdm_shared *s, int c)
{
	uint8_t hdr[FDM_HDR_SIZE];
	uint8_t small[FDM_SMALL];
	uint32_t t_last = FENYKL_TIMER_NOW();

	for (;;) {
		volatile uint8_t *pay = small;
		uint32_t op, id, len;
		bool busy;

		if (!fdm_recv_all(c, hdr, sizeof(hdr))) {
			fdm_put(s, "client gone %u ms after its last request", fdm_us_since(t_last) / 1000,
				0, 0);
			return;
		}
		if (fdm_hdr_check(hdr, &op, &id, &len)) {
			fdm_put(s, "bad frame %x len %u: client dropped", fdm_get32(hdr), len, 0);
			fdm_reply(c, op, id, FDM_EFRAME, NULL, 0);
			return;
		}
		if (s->job.state == FDM_JOB_DONE)
			s->job.state = FDM_JOB_IDLE;	/* the late result of a job given up */
		busy = s->job.state != FDM_JOB_IDLE;
		if (len > FDM_SMALL && busy) {
			for (uint32_t k; len; len -= k) {
				k = len < FDM_SMALL ? len : FDM_SMALL;
				if (!fdm_recv_all(c, small, k))
					return;
			}
			if (!fdm_reply(c, op, id, FDM_EBUSY, NULL, 0))
				return;
			continue;
		}
		if (len > FDM_SMALL)
			pay = FDM_IO();
		if (!fdm_recv_all(c, pay, len))
			return;
		if (fdm_op_is_job(op) && busy) {
			if (!fdm_reply(c, op, id, FDM_EBUSY, NULL, 0))
				return;
			continue;
		}
		if (fdm_op_is_job(op) && pay == small) {
			for (uint32_t i = 0; i < len; i++)
				FDM_IO()[i] = small[i];
			pay = FDM_IO();
		}
		if (!fdm_request(s, c, op, id, pay, len)) {
			fdm_put(s, "send failed: client dropped", 0, 0, 0);
			return;
		}
		t_last = FENYKL_TIMER_NOW();
	}
}

static int fdm_listen(volatile struct fdm_shared *s)
{
	struct fdm_sockaddr sa = {
		.family = FDM_AF_INET,
		.port = (uint16_t)((FDM_PORT >> 8) | (FDM_PORT & 0xFFu) << 8),
	};
	int fd = fw_olnet_socket(FDM_AF_INET, FDM_SOCK_STREAM, 0);
	int r;

	fdm_put(s, "socket -> %d", (uint32_t)fd, 0, 0);
	if (fd < 0)
		return -1;
	r = fw_olnet_bind(fd, &sa, sizeof(sa));
	fdm_put(s, "bind port %u -> %d", FDM_PORT, (uint32_t)r, 0);
	if (r == 0) {
		r = fw_olnet_listen(fd, FDM_BACKLOG);
		fdm_put(s, "listen -> %d", (uint32_t)r, 0, 0);
	}
	if (r == 0)
		return fd;
	fdm_put(s, "close %d -> %d", (uint32_t)fd, (uint32_t)fw_olnet_close(fd), 0);
	return -1;
}

/* noinline: the task entry's frame stays small, under FDM_STACK_ABOVE (tests/test_devmon.py). */
static __attribute__((noinline)) void fdm_serve(volatile struct fdm_shared *s)
{
	int fd = fdm_listen(s);
	unsigned int fails = 0;

	if (fd < 0)
		return;
	s->srv_state = FDM_SRV_LISTEN;
	while (fails < FDM_ACCEPT_TRIES) {
		struct fdm_sockaddr peer = { 0 };
		unsigned int len = sizeof(peer);
		int c = fw_olnet_accept(fd, &peer, &len);

		if (c < 0) {
			fails++;
			fdm_put(s, "accept -> %d (failure %u of %u)", (uint32_t)c, fails,
			       FDM_ACCEPT_TRIES);
			fw_OlyTaskSleep(1000);
			continue;
		}
		fails = 0;
		s->clients++;
		s->srv_state = FDM_SRV_CLIENT;
		fdm_put(s, "client %u: %x port %u", s->clients, peer.addr,
		       (uint32_t)((peer.port >> 8) | (peer.port & 0xFFu) << 8));
		fdm_client(s, c);
		fdm_put(s, "client %u: close -> %d", s->clients, (uint32_t)fw_olnet_close(c), 0);
		s->srv_state = FDM_SRV_LISTEN;
	}
	fdm_put(s, "server gives up; close -> %d", (uint32_t)fw_olnet_close(fd), 0, 0);
}

static void fdm_srv(unsigned int exinf)
{
	volatile struct fdm_shared *s = (volatile struct fdm_shared *)exinf;
	volatile uint8_t marker = 0;

	fdm_stack_fill(s, (uint32_t)&marker + FDM_STACK_ABOVE);
	fdm_put(s, "server task on core %u, stack fill %x..%x", FENYKL_CORE_ID(), s->stack_lo,
		s->stack_hi);
	fdm_serve(s);
	s->srv_state = FDM_SRV_DEAD;
	fw_exd_tsk();
}
FENYKL_CALLBACK(kernel_task, fdm_srv);

/* CPUCALL_CPU1 task context: create the server task and return at once. */
static void fdm_boot(void *arg)
{
	volatile struct fdm_shared *s = arg;
	struct fdm_ctsk ct = {
		.atr = FDM_TA_ACT,
		.exinf = (uint32_t)arg,
		.entry = FENYKL_CB(kernel_task, fdm_srv),
		.prio = FDM_SRV_PRIO,
		.stksz = FDM_SRV_STACK,
		.stk = 0,
	};
	int id;

	if (s->magic != FDM_MAGIC)
		return;
	if (FENYKL_CORE_ID() != 1) {
		s->boot = FDM_BOOT_ECORE;
		return;
	}
	id = fw_acre_tsk(&ct);
	fdm_put(s, "boot on core 1: acre_tsk prio %x stack %x -> %x", FDM_SRV_PRIO, FDM_SRV_STACK,
	       (uint32_t)id);
	if (id > 0)
		s->srv_state = FDM_SRV_BOOT;
	s->boot = id;
}
FENYKL_CALLBACK(cpucall_fn, fdm_boot);

uint32_t fdm_cpu1_boot_addr(void)
{
	return FENYKL_CB(cpucall_fn, fdm_boot);
}
