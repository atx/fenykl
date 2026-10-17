/* macbench stages (plugin.toml cpu1_sources: CPU1 code; the CPU0 control task runs the same stages
   first) and the CPU1 worker. Every row is the fastest of MB_REPS runs with a 1-tick sleep after
   each, so the worker blocks at least every ~15 ms. */
#include "log.h"
#include "macbench.h"

#define MB_REPS 5
#define MB_LOOPS_CALIB 4000u	/* 64 x 4000 one-cycle ops: ~1.6 ms */
#define MB_LOOPS_MUL 500u	/* 64 x 500 pairs, <= ~7 ms at 34 cycles */
#define MB_LOOPS_UDF 1000u	/* <= ~14 ms if udf is as slow as a 4-byte mul */
#define MB_MDR_MARK 0x5A5A5A5Au	/* MDR before a probe: unchanged = the instruction left MDR alone */

FENYKL_SHARED struct mb_shared mb_sh;

/* Test data, filled by the running core before its kernel stages. Both cores write the same bytes,
   so a late write-back of the other core's dirty lines changes nothing. */
static uint8_t mb_a[MB_N_LARGE] __attribute__((aligned(64)));
static int8_t mb_w[MB_N_LARGE] __attribute__((aligned(64)));
static int8_t mb_t[MB_N_LARGE] __attribute__((aligned(64)));
static int32_t mb_p[MB_PACK_N] __attribute__((aligned(64)));

/* --- instruction timing: loops x a block of 64 copies of body --- */

typedef uint32_t (*mb_block_fn)(uint32_t loops, uint32_t m, uint32_t n);

/* body sees [x] (the Dn under test, starts as n), [t] (scratch), [m] and [n]; returns x. The loop
   (3 cycles per block, loops >= 1) is the same in every block, so the empty block measures it. */
#define MB_BLOCK(fn, body)						\
	static uint32_t fn(uint32_t loops, uint32_t m, uint32_t n)	\
	{								\
		uint32_t x, t;						\
									\
		__asm__ volatile("mov %[n],%[x]\n\t"			\
				 "clr %[t]\n\t"				\
				 "setlb\n\t"					\
				 ".rept 64\n\t" body "\n\t.endr\n\t"	\
				 "add -1,%[c]\n\t"			\
				 "cmp 0,%[c]\n\t"			\
				 "lne"					\
				 : [x] "=&d"(x), [t] "=&d"(t), [c] "+a"(loops) \
				 : [m] "d"(m), [n] "d"(n)		\
				 : "mdr", "cc");			\
		return x;						\
	}

#define MB_PAIR_BODY(insn) "mov %[n],%[x]\n\t" insn
#define MB_OVL_BODY(insn) MB_PAIR_BODY(insn) "\n\t.rept 8\n\tadd %[m],%[t]\n\t.endr"

MB_BLOCK(mb_b_empty, "")
MB_BLOCK(mb_b_add, "add %[m],%[x]")
MB_BLOCK(mb_b_movadd, MB_PAIR_BODY("add %[m],%[x]"))
MB_BLOCK(mb_b_mul, MB_PAIR_BODY("mul %[m],%[x]"))
MB_BLOCK(mb_b_mulu, MB_PAIR_BODY("mulu %[m],%[x]"))
MB_BLOCK(mb_b_mul_ovl, MB_OVL_BODY("mul %[m],%[x]"))
MB_BLOCK(mb_b_udf00, MB_PAIR_BODY("udf00 %[m],%[x]"))
MB_BLOCK(mb_b_udf00_i8, MB_PAIR_BODY("udf00 -100,%[x]"))
MB_BLOCK(mb_b_udf00_i16, MB_PAIR_BODY("udf00 -1143,%[x]"))
MB_BLOCK(mb_b_udf00_i32, MB_PAIR_BODY("udf00 0x12345,%[x]"))
MB_BLOCK(mb_b_udfu01_i8, MB_PAIR_BODY("udfu01 200,%[x]"))
MB_BLOCK(mb_b_udfu01_i16, MB_PAIR_BODY("udfu01 40000,%[x]"))
MB_BLOCK(mb_b_udfu01_i32, MB_PAIR_BODY("udfu01 0x12345,%[x]"))
MB_BLOCK(mb_b_udf00_ovl, MB_OVL_BODY("udf00 %[m],%[x]"))
MB_BLOCK(mb_b_udf01, MB_PAIR_BODY("udf01 %[m],%[x]"))

struct mb_op {
	const char *name;
	mb_block_fn fn;
	uint32_t kind;	/* MB_K_EMPTY, MB_K_OP or MB_K_PAIR */
	uint32_t check;	/* the result must be n * k */
	uint32_t loops;
	uint32_t m, n, k;
};

/* A register form: x = n * m. */
#define MB_REG(name, fn, loops, m, n) { name, fn, MB_K_PAIR, 1, loops, m, n, m }
/* An immediate form: x = n * imm (as the assembler encodes it). */
#define MB_IMM(name, fn, loops, n, imm) { name, fn, MB_K_PAIR, 1, loops, 0, n, imm }

#define B1 0x5Au
#define B2 0x5A5Au
#define B3 0x5A5A5Au
#define B4 0x5A5A5A5Au
#define M1 0x3Cu
#define M2 0x3C3Cu
#define M3 0x3C3C3Cu
#define M4 0x3C3C3C3Cu
#define NEG3 0xFFFFFFFDu

static const struct mb_op mb_ops_calib[] = {
	{ "empty loop", mb_b_empty, MB_K_EMPTY, 0, MB_LOOPS_CALIB, 0, 0, 0 },
	{ "add (clock)", mb_b_add, MB_K_OP, 0, MB_LOOPS_CALIB, 1, 0, 0 },
	{ "mov+add (pair method)", mb_b_movadd, MB_K_PAIR, 0, MB_LOOPS_CALIB, 1, 0, 0 },
};

static const struct mb_op mb_ops_mul[] = {
	MB_REG("mul Dn=1B Dm=1B", mb_b_mul, MB_LOOPS_MUL, M1, B1),
	MB_REG("mul Dn=0 Dm=1B", mb_b_mul, MB_LOOPS_MUL, M1, 0),
	MB_REG("mul Dn=1B Dm=0", mb_b_mul, MB_LOOPS_MUL, 0, B1),
	MB_REG("mul Dn=2B Dm=1B", mb_b_mul, MB_LOOPS_MUL, M1, B2),
	MB_REG("mul Dn=3B Dm=1B", mb_b_mul, MB_LOOPS_MUL, M1, B3),
	MB_REG("mul Dn=4B Dm=1B", mb_b_mul, MB_LOOPS_MUL, M1, B4),
	MB_REG("mul Dn=1B Dm=2B", mb_b_mul, MB_LOOPS_MUL, M2, B1),
	MB_REG("mul Dn=1B Dm=3B", mb_b_mul, MB_LOOPS_MUL, M3, B1),
	MB_REG("mul Dn=1B Dm=4B", mb_b_mul, MB_LOOPS_MUL, M4, B1),
	MB_REG("mul Dn=-3 Dm=1B", mb_b_mul, MB_LOOPS_MUL, M1, NEG3),
	MB_REG("mul Dn=1B Dm=-3", mb_b_mul, MB_LOOPS_MUL, NEG3, B1),
	MB_REG("mulu Dn=1B Dm=1B", mb_b_mulu, MB_LOOPS_MUL, M1, B1),
	MB_REG("mulu Dn=4B Dm=4B", mb_b_mulu, MB_LOOPS_MUL, M4, B4),
	MB_REG("mul 1Bx1B +8 add", mb_b_mul_ovl, MB_LOOPS_MUL, M1, B1),
};

static const struct mb_op mb_ops_udf[] = {
	MB_REG("udf00 Dn=1B Dm=1B", mb_b_udf00, MB_LOOPS_UDF, M1, B1),
	MB_REG("udf00 Dn=0 Dm=1B", mb_b_udf00, MB_LOOPS_UDF, M1, 0),
	MB_REG("udf00 Dn=4B Dm=1B", mb_b_udf00, MB_LOOPS_UDF, M1, B4),
	MB_REG("udf00 Dn=1B Dm=4B", mb_b_udf00, MB_LOOPS_UDF, M4, B1),
	MB_REG("udf00 Dn=4B Dm=4B", mb_b_udf00, MB_LOOPS_UDF, M4, B4),
	MB_REG("udf00 Dn=-3 Dm=1B", mb_b_udf00, MB_LOOPS_UDF, M1, NEG3),
	MB_REG("udf00 Dn=1B Dm=-3", mb_b_udf00, MB_LOOPS_UDF, NEG3, B1),
	MB_IMM("udf00 #-100 (imm8) Dn=1B", mb_b_udf00_i8, MB_LOOPS_UDF, B1, (uint32_t)-100),
	MB_IMM("udf00 #-100 (imm8) Dn=4B", mb_b_udf00_i8, MB_LOOPS_UDF, B4, (uint32_t)-100),
	MB_IMM("udf00 #-1143 (imm16) Dn=1B", mb_b_udf00_i16, MB_LOOPS_UDF, B1, (uint32_t)-1143),
	MB_IMM("udf00 #0x12345 (imm32) Dn=1B", mb_b_udf00_i32, MB_LOOPS_UDF, B1, 0x12345u),
	MB_IMM("udfu01 #200 (imm8) Dn=1B", mb_b_udfu01_i8, MB_LOOPS_UDF, B1, 200u),
	MB_IMM("udfu01 #200 (imm8) Dn=4B", mb_b_udfu01_i8, MB_LOOPS_UDF, B4, 200u),
	MB_IMM("udfu01 #40000 (imm16) Dn=1B", mb_b_udfu01_i16, MB_LOOPS_UDF, B1, 40000u),
	MB_IMM("udfu01 #0x12345 (imm32) Dn=1B", mb_b_udfu01_i32, MB_LOOPS_UDF, B1, 0x12345u),
	MB_REG("udf00 1Bx1B +8 add", mb_b_udf00_ovl, MB_LOOPS_UDF, M1, B1),
};

/* udf01's register form: a multiply only by analogy with udfu01, so the result is not checked. */
static const struct mb_op mb_ops_udf01[] = {
	{ "udf01 Dn=1B Dm=1B", mb_b_udf01, MB_K_PAIR, 0, MB_LOOPS_UDF, M1, B1, M1 },
	{ "udf01 Dn=4B Dm=4B", mb_b_udf01, MB_K_PAIR, 0, MB_LOOPS_UDF, M4, B4, M4 },
};

/* --- semantics probes: one instruction, MDR preset to MB_MDR_MARK --- */

typedef uint32_t (*mb_probe_fn)(uint32_t m, uint32_t n, uint32_t *mdr);

#define MB_PROBE(fn, insn)						\
	static uint32_t fn(uint32_t m, uint32_t n, uint32_t *mdr)	\
	{								\
		uint32_t x = n, h = MB_MDR_MARK;			\
									\
		__asm__ volatile("mov %[h],mdr\n\t" insn "\n\tmov mdr,%[h]" \
				 : [x] "+d"(x), [h] "+d"(h)		\
				 : [m] "d"(m)				\
				 : "mdr", "cc");			\
		*mdr = h;						\
		return x;						\
	}

MB_PROBE(mb_p_mul, "mul %[m],%[x]")
MB_PROBE(mb_p_mulu, "mulu %[m],%[x]")
MB_PROBE(mb_p_udf00, "udf00 %[m],%[x]")
MB_PROBE(mb_p_udf00_i16, "udf00 -1143,%[x]")
MB_PROBE(mb_p_udfu01_i32, "udfu01 0x9ABCDEF0,%[x]")
MB_PROBE(mb_p_udf01, "udf01 %[m],%[x]")

struct mb_probe {
	const char *name;
	mb_probe_fn fn;
	uint32_t check;	/* the low word must be n * k */
	uint32_t m, n, k;
	uint32_t sign;	/* want2: the high word of the signed (1) or unsigned (0) product */
};

#define PA 0x12345678u
#define PB 0x9ABCDEF0u

static const struct mb_probe mb_probes_mul[] = {
	{ "probe mul", mb_p_mul, 1, PB, PA, PB, 1 },
	{ "probe mulu", mb_p_mulu, 1, PB, PA, PB, 0 },
};

static const struct mb_probe mb_probes_udf[] = {
	{ "probe udf00 Dm,Dn", mb_p_udf00, 1, PB, PA, PB, 1 },
	{ "probe udf00 #-1143", mb_p_udf00_i16, 1, 0, PA, (uint32_t)-1143, 1 },
	{ "probe udfu01 #0x9ABCDEF0", mb_p_udfu01_i32, 1, 0, PA, PB, 0 },
};

static const struct mb_probe mb_probes_udf01[] = {
	{ "probe udf01 Dm,Dn", mb_p_udf01, 0, PB, PA, PB, 0 },
};

/* --- dot-product kernels --- */

enum mb_mode {
	MB_WARM,	/* operands in the D-cache (after the first of the runs) */
	MB_COLD,	/* D-cache purged before every run (not timed) */
	MB_UNCACHED,	/* operands through the uncached alias */
};

struct mb_kern {
	const char *name;
	mb_dot_fn dot;		/* scalar kernel, or 0 */
	mb_dot2_fn dot2;	/* packed kernel, or 0 */
	uint32_t n;		/* activations per call */
	uint32_t runs;		/* calls per timed rep */
	uint32_t mode;
	uint32_t tern;		/* weights: mb_tern_w (ternary kernel) instead of mb_w */
};

static int32_t mb_dot_tern_w(const uint8_t *a, const int8_t *w, uint32_t n)
{
	(void)w;
	return mb_dot_tern(a, n);
}

/* 65536 MACs per rep, fewer uncached. */
static const struct mb_kern mb_kerns_mul[] = {
	{ "dot C -Os n=256", mb_dot_ref, 0, MB_N_SMALL, 256, MB_WARM, 0 },
	{ "dot C -O2 n=256", mb_dot_o2, 0, MB_N_SMALL, 256, MB_WARM, 0 },
	{ "dot mul act=Dn n=256", mb_dot_mul_dn, 0, MB_N_SMALL, 256, MB_WARM, 0 },
	{ "dot mul act=Dm n=256", mb_dot_mul_dm, 0, MB_N_SMALL, 256, MB_WARM, 0 },
	{ "dot mul act=Dn n=4096", mb_dot_mul_dn, 0, MB_N_LARGE, 16, MB_WARM, 0 },
	{ "dot mul act=Dm n=4096", mb_dot_mul_dm, 0, MB_N_LARGE, 16, MB_WARM, 0 },
	{ "dot mul act=Dn n=4096 cold", mb_dot_mul_dn, 0, MB_N_LARGE, 16, MB_COLD, 0 },
	{ "dot mul act=Dn n=4096 uncached", mb_dot_mul_dn, 0, MB_N_LARGE, 4, MB_UNCACHED, 0 },
	{ "dot2 mul act=Dn n=2x256", 0, mb_dot2_mul, MB_PACK_N, 128, MB_WARM, 0 },
	{ "dot tern n=256", mb_dot_tern_w, 0, MB_N_SMALL, 256, MB_WARM, 1 },
	{ "dot tern n=4096", mb_dot_tern_w, 0, MB_N_LARGE, 16, MB_WARM, 1 },
};

static const struct mb_kern mb_kerns_udf[] = {
	{ "dot udf00 act=Dn n=256", mb_dot_udf_dn, 0, MB_N_SMALL, 256, MB_WARM, 0 },
	{ "dot udf00 act=Dm n=256", mb_dot_udf_dm, 0, MB_N_SMALL, 256, MB_WARM, 0 },
	{ "dot udf00 act=Dn n=4096", mb_dot_udf_dn, 0, MB_N_LARGE, 16, MB_WARM, 0 },
	{ "dot2 udf00 act=Dn n=2x256 (MDR?)", 0, mb_dot2_udf, MB_PACK_N, 128, MB_WARM, 0 },
};

/* --- stages --- */

struct mb_stage_def {
	const char *name;
	const struct mb_op *ops;
	unsigned int nops;
	const struct mb_probe *probes;
	unsigned int nprobes;
	const struct mb_kern *kerns;
	unsigned int nkerns;
};

#define MB_LIST(a) a, sizeof(a) / sizeof((a)[0])

static const struct mb_stage_def mb_stages[MB_STAGES] = {
	[MB_ST_CALIB] = { "calib", MB_LIST(mb_ops_calib), 0, 0, 0, 0 },
	[MB_ST_MUL] = { "mul", MB_LIST(mb_ops_mul), MB_LIST(mb_probes_mul), 0, 0 },
	[MB_ST_KMUL] = { "kernels without udf", 0, 0, 0, 0, MB_LIST(mb_kerns_mul) },
	[MB_ST_UDF] = { "udf00/udfu01", MB_LIST(mb_ops_udf), MB_LIST(mb_probes_udf), 0, 0 },
	[MB_ST_KUDF] = { "kernels with udf00", 0, 0, 0, 0, MB_LIST(mb_kerns_udf) },
	[MB_ST_UDF01] = { "udf01 Dm,Dn", MB_LIST(mb_ops_udf01), MB_LIST(mb_probes_udf01), 0, 0 },
};

const char *mb_stage_name(unsigned int stage)
{
	return mb_stages[stage].name;
}

static void mb_run_op(const struct mb_op *o, struct mb_row *r)
{
	uint32_t best = 0xFFFFFFFFu, got = 0;

	for (int i = 0; i < MB_REPS; i++) {
		uint32_t t0 = FENYKL_LOG_TIMER_NOW();
		got = o->fn(o->loops, o->m, o->n);
		uint32_t us = FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());
		if (us < best)
			best = us;
		fw_OlyTaskSleep(1);
	}
	*r = (struct mb_row){ .name = o->name, .kind = o->kind, .check = o->check, .us = best,
			      .ops = 64 * o->loops, .got = got, .want = o->n * o->k };
}

static void mb_run_probe(const struct mb_probe *p, struct mb_row *r)
{
	uint32_t mdr;
	uint32_t got = p->fn(p->m, p->n, &mdr);
	uint32_t hi = p->sign ? (uint32_t)((int64_t)(int32_t)p->n * (int32_t)p->k >> 32)
			      : (uint32_t)((uint64_t)p->n * p->k >> 32);

	*r = (struct mb_row){ .name = p->name, .kind = MB_K_PROBE, .check = p->check, .got = got,
			      .want = p->n * p->k, .got2 = mdr, .want2 = hi };
}

static void mb_run_kern(const struct mb_kern *k, struct mb_row *r)
{
	const int8_t *wc = k->tern ? mb_t : mb_w;
	const uint8_t *a = mb_a;
	const int8_t *w = wc;
	uint32_t best = 0xFFFFFFFFu;
	int32_t out[2] = { 0, 0 };

	if (k->mode == MB_UNCACHED) {
		fw_dcache_purge_all_irqsafe(); /* writes the test data back to DRAM */
		a = FENYKL_UNCACHED(mb_a);
		w = FENYKL_UNCACHED(wc);
	}
	for (int i = 0; i < MB_REPS; i++) {
		uint32_t us = 0, t0 = FENYKL_LOG_TIMER_NOW();
		for (uint32_t j = 0; j < k->runs; j++) {
			if (k->mode == MB_COLD) {
				fw_dcache_purge_all_irqsafe();
				t0 = FENYKL_LOG_TIMER_NOW();
			}
			if (k->dot2)
				k->dot2(a, mb_p, k->n, out);
			else
				out[0] = k->dot(a, w, k->n);
			if (k->mode == MB_COLD)
				us += FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());
		}
		if (k->mode != MB_COLD)
			us = FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());
		if (us < best)
			best = us;
		fw_OlyTaskSleep(1);
	}
	*r = (struct mb_row){ .name = k->name, .kind = MB_K_KERNEL, .check = 1, .us = best,
			      .ops = k->n * k->runs * (k->dot2 ? 2 : 1), .got = out[0],
			      .want = mb_dot_ref(mb_a, wc, k->n) };
	if (k->dot2) {
		r->got2 = out[1];
		r->want2 = mb_dot_ref(mb_a, mb_w + MB_PACK_N, k->n);
	}
}

unsigned int mb_stage(unsigned int stage, struct mb_row *rows)
{
	const struct mb_stage_def *s = &mb_stages[stage];
	unsigned int n = 0;

	if (s->nkerns)
		mb_fill(mb_a, mb_w, MB_N_LARGE, mb_p, MB_PACK_N, mb_t);
	for (unsigned int i = 0; i < s->nops; i++)
		mb_run_op(&s->ops[i], &rows[n++]);
	for (unsigned int i = 0; i < s->nprobes; i++)
		mb_run_probe(&s->probes[i], &rows[n++]);
	for (unsigned int i = 0; i < s->nkerns; i++)
		mb_run_kern(&s->kerns[i], &rows[n++]);
	return n;
}

/* --- CPU1 worker: runs the stage CPU0 asks for, rows into mb_sh --- */

FENYKL_CPU1_WORKER(mb_worker);
void mb_worker(void *arg)
{
	volatile struct mb_shared *sh = FENYKL_SHARED_PTR(mb_sh);

	(void)arg;
	sh->core = FENYKL_CORE_ID();
	while (!fenykl_cpu1_stop_requested()) {
		uint32_t cmd = sh->cmd;

		if (cmd == sh->done || cmd == 0 || cmd > MB_STAGES) {
			fw_OlyTaskSleep(1);
			continue;
		}
		sh->count = mb_stage(cmd - 1, (struct mb_row *)sh->rows);
		__asm__ volatile("" ::: "memory");
		sh->done = cmd;
	}
}
