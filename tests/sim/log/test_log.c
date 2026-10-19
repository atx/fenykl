/*
 * Logging library (sdk/lib/log.c) in the GDB simulator against the mocks in fw.S: a one-file card
 * (C:\FENYKL.LOG) in memory, a C sprintf, a settable timer_now and core id. Boots are simulated
 * by clearing .bss and .shared as crt0 and the loader do; the state below (section .data.sim)
 * survives them. The file is dumped to stdout after every boot
 * for test_log_sim.py; plugin_main sets sim_exit_code to 0 or 1 + the number of the lowest
 * failed check.
 */
#include <fenykl.h>
/* As the build defines it for the library: cpu1.c is a cpu1_sources file. */
#define FENYKL_PLUGIN_CPU1 1
#include "../../../sdk/lib/log_int.h"

#define SIM_PERSIST __attribute__((section(".data.sim")))
#define SIM_FILE_STORE 0x10000u
#define SIM_TIMER (*(volatile uint32_t *)0x98900074u)
#define SIM_CORE (*(volatile uint32_t *)0x8050u)
#define SIM_TEE_BUF ((volatile uint8_t *)0xA07C5E80u)
#define SIM_TEE_COUNT (*(volatile uint16_t *)0x8FF00088u)
#define SIM_TEE_STATE1 (*(volatile uint8_t *)0x8FF00082u)
#define SIM_TEE_SIZE 767u
#define SIM_FAT_MOUNTED (*(volatile uint16_t *)0x6059C978u)
#define SIM_PSW_IE 0x800u
#define SIM_LOG_FP 1u	/* the handle of C:\FENYKL.LOG */
#define SIM_ERR_FP 2u	/* the handle of C:\FENYKL.ERR */
#define SIM_BOOT_GAP_US 1000000u
#define SIM_MALLOC_MAX 0x3FFCu	/* api_memory_alloc 0x6E8639AE: "AllocMemory: Size Over!!!", NULL */

#define SIM_CHECK(n, cond)			\
	do {					\
		if (!(cond))			\
			sim.fail |= 1u << (n);	\
	} while (0)

struct sim_state {
	uint8_t data[SIM_FILE_STORE];	/* the first SIM_FILE_STORE bytes of the file */
	uint32_t exists, size, pos, mode_w; /* size counts all bytes */
	uint32_t a_fails;		/* the next opens with "a" that fail */
	uint32_t opens, opens_w, removes, err_written, sprintfs, boot;
	uint32_t malloc_over;		/* fw_malloc calls the firmware refuses */
	void (*hook)(void);		/* run at the sprintf call number hook_at */
	uint32_t hook_at, hook_result;
	uint32_t now;
	uint32_t fail;
	char err[160];
};

struct sim_spec {
	char pad;
	bool left;
	unsigned int width, prec;
};

volatile uint32_t mreg[5] SIM_PERSIST;
volatile uint32_t mock_psw = SIM_PSW_IE, mock_masks SIM_PERSIST;
volatile uint32_t mock_usb_key = 1; /* ui_key_get_state(0x400): 1 = no USB cable */
uint32_t sim_exit_code = 99;

static struct sim_state sim SIM_PERSIST;
static uint8_t sim_heap[0x9000] SIM_PERSIST;

FENYKL_LOG_MODULE(sim_mod, "test", FENYKL_LOG_INFO);

void sim_write(const void *buf, unsigned int len);
void sim_burst1(unsigned int i), sim_spsc(unsigned int i); /* cpu1.c */
extern char fenykl_bss_start[], fenykl_bss_end[];

static void sim_set_time(uint32_t t)
{
	sim.now = t & FENYKL_LOG_TIMER_MASK;
	SIM_TIMER = FENYKL_LOG_TIMER_MASK - sim.now;
}

static void sim_tick(uint32_t us)
{
	sim_set_time(sim.now + us);
}

static bool sim_streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

/* --- mocks (fw.S forwards here) --- */

static char *sim_put_num(char *o, uint32_t v, unsigned int base, bool upper, unsigned int width,
			 char pad, bool neg)
{
	char tmp[12];
	unsigned int n = 0;

	do {
		unsigned int d = v % base;

		tmp[n++] = d < 10 ? '0' + d : (upper ? 'A' : 'a') + d - 10;
		v /= base;
	} while (v);
	if (neg)
		tmp[n++] = '-';
	while (width-- > n)
		*o++ = pad;
	while (n)
		*o++ = tmp[--n];
	return o;
}

/* Parses the flags 0 and -, width, .prec and h/l after a '%'; returns the conversion. */
static const char *sim_fmt_spec(const char *f, struct sim_spec *sp)
{
	*sp = (struct sim_spec){ .pad = ' ', .prec = ~0u };
	for (; *f == '0' || *f == '-'; f++) {
		if (*f == '0')
			sp->pad = '0';
		else
			sp->left = true;
	}
	while (*f >= '0' && *f <= '9')
		sp->width = sp->width * 10 + *f++ - '0';
	if (*f == '.')
		for (sp->prec = 0, f++; *f >= '0' && *f <= '9'; f++)
			sp->prec = sp->prec * 10 + *f - '0';
	while (*f == 'l' || *f == 'h')
		f++;
	return f;
}

/* Conversion c of v into o: d i u x X c s p; returns the end. */
static char *sim_fmt_conv(char *o, char c, uint32_t v, const struct sim_spec *sp)
{
	unsigned int width = sp->left ? 0 : sp->width;
	bool neg = (int32_t)v < 0;
	char *start = o;

	switch (c) {
	case 'd':
	case 'i':
		return sim_put_num(o, neg ? -v : v, 10, false, width, sp->pad, neg);
	case 'u':
		return sim_put_num(o, v, 10, false, width, sp->pad, false);
	case 'x':
	case 'X':
	case 'p':
		return sim_put_num(o, v, 16, c == 'X', width, sp->pad, false);
	case 'c':
		*o++ = v;
		return o;
	case 's':
		for (const char *s = (const char *)v; *s; s++) {
			if ((unsigned int)(o - start) >= sp->prec)
				break;
			*o++ = *s;
		}
		return o;
	default:
		*o++ = '?';
		return o;
	}
}

static void sim_run_hook(void)
{
	void (*h)(void) = sim.hook;

	if (!h || sim.sprintfs != sim.hook_at)
		return;
	sim.hook = NULL;
	h();
}

/* sprintf(buf@A0, fmt@(16,SP), args@(20,SP)...) */
uint32_t c_sprintf(void)
{
	char *o = (char *)mreg[0];
	const uint32_t *sp = (const uint32_t *)mreg[4];
	const char *f = (const char *)sp[4];
	const uint32_t *arg = sp + 5;

	sim.sprintfs++;
	sim_run_hook();
	for (; *f; f++) {
		struct sim_spec spec;
		char *start = o;

		if (*f != '%') {
			*o++ = *f;
			continue;
		}
		f = sim_fmt_spec(f + 1, &spec);
		if (*f == '%') {
			*o++ = '%';
			continue;
		}
		o = sim_fmt_conv(o, *f, *arg++, &spec);
		while ((unsigned int)(o - start) < spec.width)
			*o++ = ' ';
	}
	*o = 0;
	return 0;
}

static uint32_t sim_open_log(const char *mode)
{
	if (sim_streq(mode, "r")) {
		sim.pos = 0;
		return sim.exists ? SIM_LOG_FP : 0;
	}
	if (sim_streq(mode, "w")) {
		sim.opens_w++;
		sim.exists = 1;
		sim.pos = 0; /* the firmware's "w" does not truncate */
		return SIM_LOG_FP;
	}
	if (!sim_streq(mode, "a"))
		return 0;
	if (sim.a_fails) {
		sim.a_fails--;
		return 0;
	}
	sim.exists = 1;
	sim.pos = sim.size;
	return SIM_LOG_FP;
}

uint32_t c_rpc_fopen(void)
{
	const char *path = (const char *)mreg[0], *mode = (const char *)mreg[1];

	sim.opens++;
	if (sim_streq(path, "C:\\FENYKL.ERR"))
		return sim_streq(mode, "w") ? SIM_ERR_FP : 0;
	if (!sim_streq(path, FENYKL_LOG_FILE))
		return 0;
	return sim_open_log(mode);
}

uint32_t c_rpc_fclose(void)
{
	return 0;
}

/* fread(buf@A0, size@D0, n@D1, fp@A1) */
uint32_t c_rpc_fread(void)
{
	uint32_t n = mreg[2] * mreg[3];

	if (mreg[1] != SIM_LOG_FP)
		return 0;
	if (n > sim.size - sim.pos)
		n = sim.size - sim.pos;
	sim.pos += n;
	return n;
}

/* fwrite(buf@A0, size@D0, n@D1, fp@A1) */
uint32_t c_rpc_fwrite(void)
{
	const uint8_t *b = (const uint8_t *)mreg[0];
	uint32_t n = mreg[2] * mreg[3];

	if (mreg[1] == SIM_ERR_FP) {
		for (uint32_t i = 0; i < n && i < sizeof(sim.err) - 1; i++)
			sim.err[i] = b[i];
		sim.err_written = 1;
		return n;
	}
	for (uint32_t i = 0; i < n; i++, sim.pos++)
		if (sim.pos < SIM_FILE_STORE)
			sim.data[sim.pos] = b[i];
	if (sim.pos > sim.size)
		sim.size = sim.pos;
	return n;
}

uint32_t c_rpc_fremove(void)
{
	if (!sim_streq((const char *)mreg[0], FENYKL_LOG_FILE) || !sim.exists)
		return (uint32_t)-1;
	sim.exists = sim.size = 0;
	sim.removes++;
	return 0;
}

uint32_t c_malloc(void)
{
	if (mreg[2] > SIM_MALLOC_MAX) {
		sim.malloc_over++;
		return 0;
	}
	return mreg[2] <= sizeof(sim_heap) ? (uint32_t)sim_heap : 0;
}

uint32_t c_free(void)
{
	return 0;
}

uint32_t c_OlyTaskSleep(void)
{
	sim_tick(mreg[2] * 1000u);
	return 0;
}

/* --- helpers --- */

static void sim_dump(void)
{
	static const char head[] = "\n#### dump\n";
	uint32_t n = sim.exists ? sim.size : 0;

	sim_write(head, sizeof(head) - 1);
	sim_write(sim.data, n < SIM_FILE_STORE ? n : SIM_FILE_STORE);
}

/* A new boot: what crt0 (.bss) and the loader (.shared) clear. */
static void sim_reboot(uint32_t us)
{
	volatile uint8_t *r1 = (volatile uint8_t *)FENYKL_UNCACHED(&fenykl_log_ring1);

	sim_dump();
	for (char *p = fenykl_bss_start; p < fenykl_bss_end; p++)
		*p = 0;
	for (unsigned int i = 0; i < sizeof(fenykl_log_ring1); i++)
		r1[i] = 0;
	sim.boot++;
	sim_tick(us);
}

static struct fenykl_log_stats sim_stats(void)
{
	struct fenykl_log_stats s;

	fenykl_log_get_stats(&s);
	return s;
}

static void sim_on_core1(void (*f)(unsigned int), unsigned int n)
{
	SIM_CORE = 1;
	for (unsigned int i = 0; i < n; i++)
		f(i);
	SIM_CORE = 0;
}

static void sim_burst0(unsigned int i)
{
	fenykl_log_info(&sim_mod, "burst0 %u", i);
}

static void sim_during(unsigned int i)
{
	fenykl_log_info(&sim_mod, "during %u", i);
}

static void sim_hook_spsc(void)
{
	sim.hook_result = fenykl_log_flush();
	sim_on_core1(sim_spsc, 60);
}

static void sim_hook_evict(void)
{
	for (unsigned int i = 0; i < 600; i++)
		sim_during(i);
}

/* Once both rings are drained. A ring 1 gap shows as [lost N] only with the next ring 1 record. */
static bool sim_balanced(const struct fenykl_log_stats *s)
{
	return s->produced[0] == s->written[0] + s->lost[0] && s->lost[0] == s->overwritten0 &&
	       s->produced[1] == s->written[1] + s->dropped1 && s->lost[1] <= s->dropped1;
}

static void sim_tee_put(const char *s)
{
	unsigned int n = SIM_TEE_COUNT;

	for (; *s; s++)
		SIM_TEE_BUF[n++] = *s;
	SIM_TEE_COUNT = n;
}

/* --- boot 1, step by step --- */

/* 1: init on a card without the file */
static void sim_check_init(void)
{
	struct fenykl_log_stats s;

	sim_set_time(5000000);
	SIM_CHECK(1, fenykl_log_init(NULL) == 0);
	SIM_CHECK(1, fenykl_log_init(NULL) == FENYKL_LOG_EBUSY);
	s = sim_stats();
	SIM_CHECK(1, sim.opens_w == 1 && s.file_size == 0);
}

/* 2: merge by stamp, text continuation, filtered level, core 1 lower case */
static void sim_check_merge(void)
{
	static const char text45[] = "text-0123456789abcdefghijklmnopqrstuvwxyz-end";
	static const char text25[] = "cpu1-text-0123456789-end!";
	struct fenykl_log_stats s;

	sim_tick(100);
	fenykl_log_info(&sim_mod, "fmt %u %x %s %c", 7, 0xab, "str", 'z');
	sim_tick(50);
	SIM_CORE = 1;
	fenykl_log_info(&sim_mod, "c1 %u", 1);
	SIM_CORE = 0;
	sim_tick(50);
	fenykl_log_text(&sim_mod, FENYKL_LOG_INFO, text45, sizeof(text45) - 1);
	fenykl_log_dbg(&sim_mod, "filtered");
	sim_tick(50);
	SIM_CORE = 1;
	fenykl_log_text(&sim_mod, FENYKL_LOG_ERROR, text25, sizeof(text25) - 1);
	SIM_CORE = 0;
	sim_tick(50);
	fenykl_log_warn(&sim_mod, "after %u", 2);
	SIM_CHECK(2, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(2, s.written[0] == 5 && s.written[1] == 3 && s.lost[0] == 0 && s.lost[1] == 0);
}

/* 3: truncated text; 4: bad records */
static void sim_check_bad(void)
{
	static char long_text[300];
	struct fenykl_log_stats s;

	for (unsigned int i = 0; i < sizeof(long_text); i++)
		long_text[i] = 'a' + i % 26;
	fenykl_log_text(&sim_mod, FENYKL_LOG_INFO, long_text, sizeof(long_text));
	fenykl_log_info(&sim_mod, "%s|", 0x12345678);
	fenykl_log_info(&sim_mod, "%u %u %u %u %u", 1, 2, 3, 4);
	fenykl_log_rec(&sim_mod, FENYKL_LOG_INFO, (const char *)0x100, 1, 2, 3, 4);
	fenykl_log_rec((const struct fenykl_log_module *)0x200, FENYKL_LOG_INFO, "x", 0, 0, 0, 0);
	SIM_CHECK(3, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(3, s.truncated == 1 && s.bad == 3);
}

/* 5: a jump of more than 2^29 us, then the 30-bit wrap */
static void sim_check_wrap(void)
{
	sim_set_time(0x3FFFFF00);
	fenykl_log_info(&sim_mod, "pre-wrap");
	sim_set_time(0x100);
	fenykl_log_info(&sim_mod, "post-wrap");
	SIM_CHECK(5, fenykl_log_flush() == 0);
	SIM_CHECK(5, sim_stats().stamp_jumps == 1);
}

/* 6, 7: ring 0 keeps the newest, ring 1 drops the newest */
static void sim_check_overflow(void)
{
	struct fenykl_log_stats s;

	for (unsigned int i = 0; i < 600; i++)
		sim_burst0(i);
	sim_on_core1(sim_burst1, 200);
	SIM_CHECK(6, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(6, s.overwritten0 == 600 - FLOG_RING0_RECORDS && s.lost[0] == s.overwritten0);
	SIM_CHECK(7, s.dropped1 == 200 - FLOG_RING1_RECORDS && s.lost[1] == 0 && sim_balanced(&s));
}

/* 8: CPU1 refills ring 1 while the flusher drains it; a nested flush is refused */
static void sim_check_spsc(void)
{
	struct fenykl_log_stats s;

	sim_on_core1(sim_spsc, 100);
	sim.hook = sim_hook_spsc;
	sim.hook_at = sim.sprintfs + 5;
	SIM_CHECK(8, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(8, sim.hook_result == (uint32_t)FENYKL_LOG_EBUSY &&
		     s.dropped1 == 200 - FLOG_RING1_RECORDS && s.lost[1] == s.dropped1 &&
		     sim_balanced(&s));
}

/* 9: producers evict ring 0 records while the flusher drains it */
static void sim_check_evict(void)
{
	struct fenykl_log_stats s;

	for (unsigned int i = 0; i < 100; i++)
		fenykl_log_info(&sim_mod, "pre %u", i);
	sim.hook = sim_hook_evict;
	sim.hook_at = sim.sprintfs + 3;
	SIM_CHECK(9, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(9, sim_balanced(&s) && s.overwritten0 == 600 - FLOG_RING0_RECORDS + 156);
}

/* 10: console tee */
static void sim_check_tee(void)
{
	struct fenykl_log_stats s;

	SIM_TEE_STATE1 = 1;
	SIM_TEE_COUNT = 0;
	sim_tee_put("abc\r\ndef\nxy");
	flog_tee_poll();
	SIM_CHECK(10, SIM_TEE_COUNT == 0);
	sim_tee_put("z\n");
	flog_tee_poll();
	for (unsigned int i = 0; i < SIM_TEE_SIZE; i++)
		SIM_TEE_BUF[i] = 'q';
	SIM_TEE_COUNT = SIM_TEE_SIZE;
	flog_tee_poll();
	SIM_TEE_STATE1 = 2;
	flog_tee_poll();
	SIM_CHECK(10, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(10, s.tee_bytes == 11 + 2 + SIM_TEE_SIZE && s.tee_overflow == 1 &&
		      s.tee_records == 3 + 4 + 1 + 1);
	/* the 255-byte lines of the overflow take 13 slots each */
	SIM_CHECK(10, s.tee_slots > s.tee_records && s.produced[0] - s.written[0] == s.lost[0]);
	SIM_CHECK(10, s.queued[0] == 0 && s.queued[1] == 0);

	/* 11: everything accounted for, PSW restored */
	SIM_CHECK(11, sim_balanced(&s) && s.bad == 3 && s.early == 0);
	SIM_CHECK(11, mock_psw == SIM_PSW_IE && mock_masks > 0);
}

/* --- boots --- */

static void sim_boot1(void)
{
	sim_check_init();
	sim_check_merge();
	sim_check_bad();
	sim_check_wrap();
	sim_check_overflow();
	sim_check_spsc();
	sim_check_evict();
	sim_check_tee();
}

/* 12: a record before init only counts; init appends to the file */
static void sim_boot2(void)
{
	struct fenykl_log_stats s;

	fenykl_log_info(&sim_mod, "early");
	SIM_CHECK(12, fenykl_log_init(NULL) == 0);
	s = sim_stats();
	SIM_CHECK(12, s.early == 1 && s.file_size > 0 && s.removed_size == 0 && sim.opens_w == 1);
	sim.size = FENYKL_LOG_SIZE_CAP;
}

static void sim_boot3(void)
{
	struct fenykl_log_stats s;

	SIM_CHECK(13, fenykl_log_init(NULL) == 0);
	s = sim_stats();
	SIM_CHECK(13, s.removed_size == 0 && sim.removes == 0);
	SIM_CHECK(13, s.file_size == FENYKL_LOG_SIZE_CAP && sim.size > FENYKL_LOG_SIZE_CAP);
	sim.size = FENYKL_LOG_SIZE_CAP + 1;
}

static void sim_boot4(void)
{
	struct fenykl_log_stats s;

	SIM_CHECK(14, fenykl_log_init(NULL) == 0);
	s = sim_stats();
	SIM_CHECK(14, s.removed_size == FENYKL_LOG_SIZE_CAP + 1 && sim.removes == 1 &&
		      s.file_size == 0);
	SIM_CHECK(14, sim.opens_w == 2);
	sim.a_fails = 3;
}

static void sim_boot5(void)
{
	uint32_t size = sim.size;

	SIM_CHECK(15, fenykl_log_init(NULL) == FENYKL_LOG_EAPPEND);
	SIM_CHECK(15, sim.err_written && sim.size == size && sim_stats().open_fail == 1);
	fenykl_log_info(&sim_mod, "after a failed init");
	SIM_CHECK(15, fenykl_log_flush() == 0 && sim.size > size);
	/* 16: no fw_malloc request the firmware refuses */
	SIM_CHECK(16, sim.malloc_over == 0);
}

static void sim_log_gate(unsigned int from, unsigned int to)
{
	for (unsigned int i = from; i < to; i++)
		fenykl_log_info(&sim_mod, "gate %u", i);
}

/* 17: init with the card not mounted takes records but does not touch the card */
static void sim_boot6(void)
{
	uint32_t opens = sim.opens, size = sim.size;
	struct fenykl_log_stats s;

	SIM_FAT_MOUNTED = 0;
	SIM_CHECK(17, fenykl_log_init(NULL) == FENYKL_LOG_ENOCARD);
	sim_log_gate(0, 1);
	sim_tick(2000000);
	SIM_CHECK(17, fenykl_log_flush() == FENYKL_LOG_ENOCARD);
	sim_log_gate(1, 2);
	sim_tick(2000000);
	SIM_CHECK(17, fenykl_log_flush() == FENYKL_LOG_ENOCARD);
	s = sim_stats();
	SIM_CHECK(17, sim.opens == opens && sim.size == size && s.queued[0] == 3);
	SIM_CHECK(17, s.gate_skipped == 3 && s.gate_max_ms == 4000);
	/* 18: the first flush after the mount does the init and writes everything */
	SIM_FAT_MOUNTED = 1;
	sim_tick(1000000);
	SIM_CHECK(18, fenykl_log_flush() == 0);
	s = sim_stats();
	SIM_CHECK(18, s.queued[0] == 0 && sim_balanced(&s) && s.file_size == size);
	SIM_CHECK(18, s.gate_skipped == 3 && s.gate_max_ms == 5000);
}

/* 19: a cable closes the gate after init: no open while closed, ring 0 overflows as usual */
static void sim_boot7(void)
{
	uint32_t opens, size;
	struct fenykl_log_stats s;

	SIM_CHECK(19, fenykl_log_init(NULL) == 0);
	sim_log_gate(0, 100);
	SIM_CHECK(19, fenykl_log_flush() == 0);
	mock_usb_key = 0;
	opens = sim.opens;
	size = sim.size;
	sim_log_gate(100, 200);
	SIM_CHECK(19, fenykl_log_flush() == FENYKL_LOG_ENOCARD);
	sim_log_gate(200, 700);
	sim_tick(2000000);
	SIM_CHECK(19, fenykl_log_flush() == FENYKL_LOG_ENOCARD);
	s = sim_stats();
	SIM_CHECK(19, sim.opens == opens && sim.size == size && s.flushes == 1);
	SIM_CHECK(19, s.queued[0] == FLOG_RING0_RECORDS && s.overwritten0 == 89);
	SIM_CHECK(19, s.gate_skipped == 2 && s.gate_max_ms == 2000);
	/* 20: cable out and the card mounted: the gate stays closed until power-off */
	mock_usb_key = 1;
	for (unsigned int i = 0; i < 2; i++) {
		sim_tick(500000);
		SIM_CHECK(20, fenykl_log_flush() == FENYKL_LOG_ENOCARD);
	}
	s = sim_stats();
	SIM_CHECK(20, !fenykl_card_io_allowed() && fenykl_card_usb_seen());
	SIM_CHECK(20, sim.opens == opens && sim.size == size && s.flushes == 1);
	SIM_CHECK(20, s.queued[0] == FLOG_RING0_RECORDS && s.overwritten0 == 89);
	SIM_CHECK(20, s.gate_skipped == 4 && s.gate_max_ms == 3000);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	static const char err_head[] = "\n#### err\n";

	(void)load_base;
	(void)image_size;

	SIM_FAT_MOUNTED = 1;
	sim_boot1();
	sim_reboot(SIM_BOOT_GAP_US);
	sim_boot2();
	sim_reboot(SIM_BOOT_GAP_US);
	sim_boot3();
	sim_reboot(SIM_BOOT_GAP_US);
	sim_boot4();
	sim_reboot(SIM_BOOT_GAP_US);
	sim_boot5();
	sim_reboot(SIM_BOOT_GAP_US);
	sim_boot6();
	sim_reboot(SIM_BOOT_GAP_US);
	sim_boot7();
	sim_dump();
	sim_write(err_head, sizeof(err_head) - 1);
	sim_write(sim.err, sizeof(sim.err) - 1);
	sim_exit_code = sim.fail ? 1 + __builtin_ctz(sim.fail) : 0;
}
