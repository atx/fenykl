/* Shared by the CPU0 control task (macbench.c), the benchmark (bench.c, CPU1 code that CPU0 also
   runs) and the kernels (kernels.c, no firmware calls). */
#ifndef MACBENCH_H
#define MACBENCH_H

#include <fenykl.h>

/* Stages, in run order; the udf stages come after everything else. */
enum mb_stage {
	MB_ST_CALIB,	/* empty loop, add: clock check */
	MB_ST_MUL,	/* mul/mulu cycles, MDR probe */
	MB_ST_KMUL,	/* dot-product kernels without udf */
	MB_ST_UDF,	/* udf00/udfu01 forms the firmware runs on CPU1 */
	MB_ST_KUDF,	/* dot-product kernels with udf00 */
	MB_ST_UDF01,	/* udf01 Dm,Dn: in the firmware, not proven on CPU1 */
	MB_STAGES,
};

enum mb_kind {
	MB_K_EMPTY,	/* the empty block loop: subtracted from the op rows */
	MB_K_OP,	/* a block of 64 instructions: ops = 64 x loops */
	MB_K_PAIR,	/* 64 x (mov Dn + the instruction): the mov is subtracted */
	MB_K_PROBE,	/* one instruction, not timed: got = Dn, got2 = MDR, want2 = 64-bit high word */
	MB_K_KERNEL,	/* a dot-product kernel: ops = MACs */
};

#define MB_ROWS_MAX 24

/* One result. us is the fastest of MB_REPS runs in timer counts (1 MHz). */
struct mb_row {
	const char *name;	/* rodata: valid on both cores and at log flush time */
	uint32_t kind;
	uint32_t check;		/* got/want (and got2/want2 for packed kernels) must match */
	uint32_t us;
	uint32_t ops;
	uint32_t got, want, got2, want2;
};

/* Written by both sides through FENYKL_SHARED_PTR only. */
struct mb_shared {
	uint32_t cmd;	/* CPU0: stage + 1 to run */
	uint32_t done;	/* CPU1: cmd of the stage whose rows are ready */
	uint32_t count;	/* rows of that stage */
	uint32_t core;	/* FENYKL_CORE_ID() on the worker */
	struct mb_row rows[MB_ROWS_MAX];
};
extern struct mb_shared mb_sh;

void mb_worker_cpu1(void); /* FENYKL_CPU1_WORKER(mb_worker) in bench.c */

const char *mb_stage_name(unsigned int stage);
/* Runs one stage on the calling core and fills rows; returns the row count. */
unsigned int mb_stage(unsigned int stage, struct mb_row *rows);

/* --- kernels.c: u8 activations a[], s8 weights w[] in [-127, 127] --- */

#define MB_N_SMALL 256u		/* 256 + 256 B: cache resident */
#define MB_N_LARGE 4096u	/* 4 + 4 KiB */
#define MB_PACK_N 256u		/* packed kernels: at most 256 per call (24-bit lane) */
#define MB_TERN_BLOCK 32u	/* ternary kernel: n is a multiple of this */

typedef int32_t (*mb_dot_fn)(const uint8_t *a, const int8_t *w, uint32_t n);
typedef void (*mb_dot2_fn)(const uint8_t *a, const int32_t *p, uint32_t n, int32_t out[2]);

/* Plain C references, -Os (the build default) and -O2. */
int32_t mb_dot_ref(const uint8_t *a, const int8_t *w, uint32_t n);
int32_t mb_dot_o2(const uint8_t *a, const int8_t *w, uint32_t n);
/* Inline asm, SETLB/LNE, 4 MACs per pass (n % 4 == 0). _dn: the activation is the Dn operand of
   the multiply, _dm: the Dm operand. */
int32_t mb_dot_mul_dn(const uint8_t *a, const int8_t *w, uint32_t n);
int32_t mb_dot_mul_dm(const uint8_t *a, const int8_t *w, uint32_t n);
int32_t mb_dot_udf_dn(const uint8_t *a, const int8_t *w, uint32_t n);
int32_t mb_dot_udf_dm(const uint8_t *a, const int8_t *w, uint32_t n);
/* Two dot products sharing a[]: p[i] = w0[i] + w1[i] * 2^24, one multiply per two MACs, the
   64-bit product summed in {MDR, Dn} (n <= MB_PACK_N). The udf variant is right only if udf00
   writes MDR like mul. */
void mb_dot2_mul(const uint8_t *a, const int32_t *p, uint32_t n, int32_t out[2]);
void mb_dot2_udf(const uint8_t *a, const int32_t *p, uint32_t n, int32_t out[2]);
int32_t mb_pack(int8_t w0, int8_t w1);
/* Ternary weights compiled into code: mb_tern_w[i % 8], zeros cost nothing (n % 32 == 0). */
extern const int8_t mb_tern_w[8];
int32_t mb_dot_tern(const uint8_t *a, uint32_t n);

/* Deterministic test data: a[] over 0..255, w[] over -127..127, p[] packed from w[0..n) and
   w[n..2n), t[] the ternary pattern. */
void mb_fill(uint8_t *a, int8_t *w, uint32_t n, int32_t *p, uint32_t np, int8_t *t);

#endif
