/* Instruction timings and semantics probes, taken over from macbench: blocks of 64 copies of one
   instruction, timed as the fastest of NB_ISA_REPS runs with a 1-tick sleep after each (CPU1 code that
   the CPU0 control task also runs). */
#include "log.h"
#include "nnbench.h"

#define NB_ISA_REPS 5
#define NB_LOOPS_CALIB 4000u	/* 64 x 4000 one-cycle ops: ~1.6 ms */
#define NB_LOOPS_MUL 500u	/* 64 x 500 pairs, <= ~7 ms at 34 cycles */
#define NB_LOOPS_UDF 1000u	/* <= ~14 ms if udf is as slow as a 4-byte mul */
#define NB_MDR_MARK 0x5A5A5A5Au	/* MDR before a probe: unchanged = the instruction left MDR alone */

/* --- instruction timing: loops x a block of 64 copies of body --- */

typedef uint32_t (*nb_block_fn)(uint32_t loops, uint32_t m, uint32_t n);

/* body sees [x] (the Dn under test, starts as n), [t] (scratch), [m] and [n]; returns x. The loop
   (3 cycles per block, loops >= 1) is the same in every block, so the empty block measures it. */
#define NB_BLOCK(fn, body)						\
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

#define NB_PAIR_BODY(insn) "mov %[n],%[x]\n\t" insn
#define NB_OVL_BODY(insn) NB_PAIR_BODY(insn) "\n\t.rept 8\n\tadd %[m],%[t]\n\t.endr"

NB_BLOCK(nb_b_empty, "")
NB_BLOCK(nb_b_add, "add %[m],%[x]")
NB_BLOCK(nb_b_movadd, NB_PAIR_BODY("add %[m],%[x]"))
NB_BLOCK(nb_b_mul, NB_PAIR_BODY("mul %[m],%[x]"))
NB_BLOCK(nb_b_mulu, NB_PAIR_BODY("mulu %[m],%[x]"))
NB_BLOCK(nb_b_mul_ovl, NB_OVL_BODY("mul %[m],%[x]"))
NB_BLOCK(nb_b_udf00, NB_PAIR_BODY("udf00 %[m],%[x]"))
NB_BLOCK(nb_b_udf00_i8, NB_PAIR_BODY("udf00 -100,%[x]"))
NB_BLOCK(nb_b_udf00_i16, NB_PAIR_BODY("udf00 -1143,%[x]"))
NB_BLOCK(nb_b_udf00_i32, NB_PAIR_BODY("udf00 0x12345,%[x]"))
NB_BLOCK(nb_b_udfu01_i8, NB_PAIR_BODY("udfu01 200,%[x]"))
NB_BLOCK(nb_b_udfu01_i16, NB_PAIR_BODY("udfu01 40000,%[x]"))
NB_BLOCK(nb_b_udfu01_i32, NB_PAIR_BODY("udfu01 0x12345,%[x]"))
NB_BLOCK(nb_b_udf00_ovl, NB_OVL_BODY("udf00 %[m],%[x]"))
NB_BLOCK(nb_b_udf01, NB_PAIR_BODY("udf01 %[m],%[x]"))

struct nb_op {
	const char *name;
	nb_block_fn fn;
	uint32_t kind;	/* NB_K_EMPTY, NB_K_OP or NB_K_PAIR */
	uint32_t check;	/* the result must be n * k */
	uint32_t loops;
	uint32_t m, n, k;
};

/* A register form: x = n * m. */
#define NB_REG(name, fn, loops, m, n) { name, fn, NB_K_PAIR, 1, loops, m, n, m }
/* An immediate form: x = n * imm (as the assembler encodes it). */
#define NB_IMM(name, fn, loops, n, imm) { name, fn, NB_K_PAIR, 1, loops, 0, n, imm }

#define B1 0x5Au
#define B2 0x5A5Au
#define B3 0x5A5A5Au
#define B4 0x5A5A5A5Au
#define M1 0x3Cu
#define M2 0x3C3Cu
#define M3 0x3C3C3Cu
#define M4 0x3C3C3C3Cu
#define NEG3 0xFFFFFFFDu

static const struct nb_op nb_ops_calib[] = {
	{ "empty loop", nb_b_empty, NB_K_EMPTY, 0, NB_LOOPS_CALIB, 0, 0, 0 },
	{ "add (clock)", nb_b_add, NB_K_OP, 0, NB_LOOPS_CALIB, 1, 0, 0 },
	{ "mov+add (pair method)", nb_b_movadd, NB_K_PAIR, 0, NB_LOOPS_CALIB, 1, 0, 0 },
};

static const struct nb_op nb_ops_mul[] = {
	NB_REG("mul Dn=1B Dm=1B", nb_b_mul, NB_LOOPS_MUL, M1, B1),
	NB_REG("mul Dn=0 Dm=1B", nb_b_mul, NB_LOOPS_MUL, M1, 0),
	NB_REG("mul Dn=1B Dm=0", nb_b_mul, NB_LOOPS_MUL, 0, B1),
	NB_REG("mul Dn=2B Dm=1B", nb_b_mul, NB_LOOPS_MUL, M1, B2),
	NB_REG("mul Dn=3B Dm=1B", nb_b_mul, NB_LOOPS_MUL, M1, B3),
	NB_REG("mul Dn=4B Dm=1B", nb_b_mul, NB_LOOPS_MUL, M1, B4),
	NB_REG("mul Dn=1B Dm=2B", nb_b_mul, NB_LOOPS_MUL, M2, B1),
	NB_REG("mul Dn=1B Dm=3B", nb_b_mul, NB_LOOPS_MUL, M3, B1),
	NB_REG("mul Dn=1B Dm=4B", nb_b_mul, NB_LOOPS_MUL, M4, B1),
	NB_REG("mul Dn=-3 Dm=1B", nb_b_mul, NB_LOOPS_MUL, M1, NEG3),
	NB_REG("mul Dn=1B Dm=-3", nb_b_mul, NB_LOOPS_MUL, NEG3, B1),
	NB_REG("mulu Dn=1B Dm=1B", nb_b_mulu, NB_LOOPS_MUL, M1, B1),
	NB_REG("mulu Dn=4B Dm=4B", nb_b_mulu, NB_LOOPS_MUL, M4, B4),
	NB_REG("mul 1Bx1B +8 add", nb_b_mul_ovl, NB_LOOPS_MUL, M1, B1),
};

static const struct nb_op nb_ops_udf[] = {
	NB_REG("udf00 Dn=1B Dm=1B", nb_b_udf00, NB_LOOPS_UDF, M1, B1),
	NB_REG("udf00 Dn=0 Dm=1B", nb_b_udf00, NB_LOOPS_UDF, M1, 0),
	NB_REG("udf00 Dn=4B Dm=1B", nb_b_udf00, NB_LOOPS_UDF, M1, B4),
	NB_REG("udf00 Dn=1B Dm=4B", nb_b_udf00, NB_LOOPS_UDF, M4, B1),
	NB_REG("udf00 Dn=4B Dm=4B", nb_b_udf00, NB_LOOPS_UDF, M4, B4),
	NB_REG("udf00 Dn=-3 Dm=1B", nb_b_udf00, NB_LOOPS_UDF, M1, NEG3),
	NB_REG("udf00 Dn=1B Dm=-3", nb_b_udf00, NB_LOOPS_UDF, NEG3, B1),
	NB_IMM("udf00 #-100 (imm8) Dn=1B", nb_b_udf00_i8, NB_LOOPS_UDF, B1, (uint32_t)-100),
	NB_IMM("udf00 #-100 (imm8) Dn=4B", nb_b_udf00_i8, NB_LOOPS_UDF, B4, (uint32_t)-100),
	NB_IMM("udf00 #-1143 (imm16) Dn=1B", nb_b_udf00_i16, NB_LOOPS_UDF, B1, (uint32_t)-1143),
	NB_IMM("udf00 #0x12345 (imm32) Dn=1B", nb_b_udf00_i32, NB_LOOPS_UDF, B1, 0x12345u),
	NB_IMM("udfu01 #200 (imm8) Dn=1B", nb_b_udfu01_i8, NB_LOOPS_UDF, B1, 200u),
	NB_IMM("udfu01 #200 (imm8) Dn=4B", nb_b_udfu01_i8, NB_LOOPS_UDF, B4, 200u),
	NB_IMM("udfu01 #40000 (imm16) Dn=1B", nb_b_udfu01_i16, NB_LOOPS_UDF, B1, 40000u),
	NB_IMM("udfu01 #0x12345 (imm32) Dn=1B", nb_b_udfu01_i32, NB_LOOPS_UDF, B1, 0x12345u),
	NB_REG("udf00 1Bx1B +8 add", nb_b_udf00_ovl, NB_LOOPS_UDF, M1, B1),
};

/* udf01's register form: a multiply only by analogy with udfu01, so the result is not checked. */
static const struct nb_op nb_ops_udf01[] = {
	{ "udf01 Dn=1B Dm=1B", nb_b_udf01, NB_K_PAIR, 0, NB_LOOPS_UDF, M1, B1, M1 },
	{ "udf01 Dn=4B Dm=4B", nb_b_udf01, NB_K_PAIR, 0, NB_LOOPS_UDF, M4, B4, M4 },
};

/* --- semantics probes: one instruction, MDR preset to NB_MDR_MARK --- */

typedef uint32_t (*nb_probe_fn)(uint32_t m, uint32_t n, uint32_t *mdr);

#define NB_PROBE(fn, insn)						\
	static uint32_t fn(uint32_t m, uint32_t n, uint32_t *mdr)	\
	{								\
		uint32_t x = n, h = NB_MDR_MARK;			\
									\
		__asm__ volatile("mov %[h],mdr\n\t" insn "\n\tmov mdr,%[h]" \
				 : [x] "+d"(x), [h] "+d"(h)		\
				 : [m] "d"(m)				\
				 : "mdr", "cc");			\
		*mdr = h;						\
		return x;						\
	}

NB_PROBE(nb_p_mul, "mul %[m],%[x]")
NB_PROBE(nb_p_mulu, "mulu %[m],%[x]")
NB_PROBE(nb_p_udf00, "udf00 %[m],%[x]")
NB_PROBE(nb_p_udf00_i16, "udf00 -1143,%[x]")
NB_PROBE(nb_p_udfu01_i32, "udfu01 0x9ABCDEF0,%[x]")
NB_PROBE(nb_p_udf01, "udf01 %[m],%[x]")

struct nb_probe {
	const char *name;
	nb_probe_fn fn;
	uint32_t check;	/* the low word must be n * k */
	uint32_t m, n, k;
	uint32_t sign;	/* want2: the high word of the signed (1) or unsigned (0) product */
};

#define PA 0x12345678u
#define PB 0x9ABCDEF0u

static const struct nb_probe nb_probes_mul[] = {
	{ "probe mul", nb_p_mul, 1, PB, PA, PB, 1 },
	{ "probe mulu", nb_p_mulu, 1, PB, PA, PB, 0 },
};

static const struct nb_probe nb_probes_udf[] = {
	{ "probe udf00 Dm,Dn", nb_p_udf00, 1, PB, PA, PB, 1 },
	{ "probe udf00 #-1143", nb_p_udf00_i16, 1, 0, PA, (uint32_t)-1143, 1 },
	{ "probe udfu01 #0x9ABCDEF0", nb_p_udfu01_i32, 1, 0, PA, PB, 0 },
};

static const struct nb_probe nb_probes_udf01[] = {
	{ "probe udf01 Dm,Dn", nb_p_udf01, 0, PB, PA, PB, 0 },
};

struct nb_isa_def {
	const struct nb_op *ops;
	unsigned int nops;
	const struct nb_probe *probes;
	unsigned int nprobes;
};

#define NB_LIST(a) a, sizeof(a) / sizeof((a)[0])

static const struct nb_isa_def nb_isa_defs[NB_ISA_STAGES] = {
	[NB_ISA_CALIB] = { NB_LIST(nb_ops_calib), 0, 0 },
	[NB_ISA_MUL] = { NB_LIST(nb_ops_mul), NB_LIST(nb_probes_mul) },
	[NB_ISA_UDF] = { NB_LIST(nb_ops_udf), NB_LIST(nb_probes_udf) },
	[NB_ISA_UDF01] = { NB_LIST(nb_ops_udf01), NB_LIST(nb_probes_udf01) },
};

static void nb_run_op(const struct nb_op *o, struct nb_row *r)
{
	uint32_t us[NB_ISA_REPS], got = 0;

	for (int i = 0; i < NB_ISA_REPS; i++) {
		uint32_t t0 = FENYKL_LOG_TIMER_NOW();
		got = o->fn(o->loops, o->m, o->n);
		us[i] = FENYKL_LOG_ELAPSED(t0, FENYKL_LOG_TIMER_NOW());
		fw_OlyTaskSleep(1);
	}
	*r = (struct nb_row){ .var = NB_ISA_VAR, .kind = o->kind, .reps = NB_ISA_REPS, .iters = 1,
			      .us_min = nb_min(us, NB_ISA_REPS), .us_med = nb_median(us, NB_ISA_REPS),
			      .ops = 64 * o->loops, .name = o->name, .got = got, .want = o->n * o->k,
			      .status = !o->check ? NB_NOCHK : got == o->n * o->k ? NB_OK : NB_BAD };
}

static void nb_run_probe(const struct nb_probe *p, struct nb_row *r)
{
	uint32_t mdr;
	uint32_t got = p->fn(p->m, p->n, &mdr);
	uint32_t hi = p->sign ? (uint32_t)((int64_t)(int32_t)p->n * (int32_t)p->k >> 32)
			      : (uint32_t)((uint64_t)p->n * p->k >> 32);

	/* The MDR word is information (udf semantics), not checked. */
	*r = (struct nb_row){ .var = NB_ISA_VAR, .kind = NB_K_PROBE, .name = p->name, .got = got,
			      .want = p->n * p->k, .got2 = mdr, .want2 = hi,
			      .status = !p->check ? NB_NOCHK : got == p->n * p->k ? NB_OK : NB_BAD };
}

unsigned int nb_isa_stage(unsigned int stage, struct nb_row *rows)
{
	const struct nb_isa_def *s = &nb_isa_defs[stage];
	unsigned int n = 0;

	for (unsigned int i = 0; i < s->nops; i++)
		nb_run_op(&s->ops[i], &rows[n++]);
	for (unsigned int i = 0; i < s->nprobes; i++)
		nb_run_probe(&s->probes[i], &rows[n++]);
	return n;
}
