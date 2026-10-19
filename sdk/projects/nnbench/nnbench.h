/*
 * nnbench: neural-network kernel costs on CPU1. Shared by the CPU0 control task (nnbench.c), the
 * CPU1 harness (bench.c, isa.c) and the pure kernel and case code (everything else: no firmware
 * calls, also linked into the GDB simulator test tests/sim/nnbench).
 *
 * A case is a family (the operation, its reference and its variants) plus parameters. For every
 * case the harness builds deterministic data, runs the reference (variant 0, plain C), then times
 * every other variant that supports the parameters and compares its whole output with the
 * reference's.
 */
#ifndef NNBENCH_H
#define NNBENCH_H

#include <fenykl.h>
#include "nb_kern.h"

#define NB_NP 10		/* parameters per case */

/* 32-bit low-word multiply in one cycle (udf00 on this core; GCC's `*` emits the slow mul). */
static inline int32_t nb_mul(int32_t a, int32_t b)
{
	__asm__("udf00 %1,%0" : "+d"(a) : "d"(b));
	return a;
}

enum nb_mode {
	NB_WARM,	/* data in the D-cache after the first call */
	NB_COLD,	/* D-cache purged before every call (not timed) */
	NB_UNCACHED,	/* the kernel's input through the uncached alias */
	NB_LIVE,	/* the live FD frame, D-cache purged before every call (not timed) */
	NB_LIVE_UNC,	/* the live FD frame through the uncached alias */
	NB_MODES,
};

/* Row status. */
enum nb_status {
	NB_OK,
	NB_BAD,		/* output differs from the reference */
	NB_NOCHK,	/* not checked (instruction timing, a live frame that kept changing) */
	NB_SKIP,	/* not run (no live frame) */
};

struct nb_run;

struct nb_var {
	const char *name;
	/* 0: the variant does not support these parameters (no row). NULL: always. */
	int (*ok)(const struct nb_run *r);
	/* Untimed: the input (and tables) in the variant's format; sets r->in. */
	void (*prep)(struct nb_run *r);
	/* One timed call: reads r->rin, writes r->out. */
	void (*run)(struct nb_run *r);
	/* Output element i in the family's canonical order, from buf (r->out or a copy). */
	uint32_t (*get)(const struct nb_run *r, const void *buf, uint32_t i);
};

struct nb_fam {
	const char *name;
	const char *pname[NB_NP];	/* NULL: unused */
	const char *unit, *unit2;	/* what ops and ops2 count */
	/* Logical input and weights, the output size and the op counts. */
	void (*setup)(struct nb_run *r);
	const struct nb_var *var;	/* var[0] is the reference */
	unsigned int nvar;
	/* If set, outputs are checked against this instead of the reference run. */
	uint32_t (*expect)(const struct nb_run *r, uint32_t i);
};

struct nb_case {
	uint8_t fam;		/* enum nb_fam_id */
	uint8_t mode;
	uint32_t p[NB_NP];
};

/* A generated kernel (gen_kernels.py, nb_gen.S / nb_gen.c). */
struct nb_gen {
	uint8_t fam;
	uint8_t px;		/* output pixels per call (ternary: 4) */
	uint32_t p[NB_NP];	/* the case parameters it implements */
	void (*fn)(const void *in, void *out);
	const char *beg, *end;	/* its code */
};
extern const struct nb_gen nb_gens[];
extern const unsigned int nb_ngen;

typedef void nb_tick_fn(struct nb_run *r);

#define NB_BLK_MAX 8u	/* code blocks of a split generated variant */

struct nb_run {
	const struct nb_case *k;
	const struct nb_fam *f;
	const struct nb_var *v;
	const uint32_t *p;
	const struct nb_gen *gen;	/* the generated kernel of a gen variant */
	/* setup: logical data */
	uint8_t *src, *src2;
	int8_t *w;
	int32_t *b;
	uint8_t *s;
	uint32_t n_out, out_bytes;
	uint32_t ops, ops2;
	/* prep: the variant's input and tables */
	void *in;
	const void *rin;		/* in, or its uncached alias */
	void *aux;
	void *out, *ref;
	uint32_t code;			/* code bytes of a generated variant */
	uint32_t nblk;			/* its blocks run one after the other (0: not split) */
	uint16_t blk[NB_BLK_MAX];	/* code bytes per block */
	/* Called by long kernels once per output row; may sleep (bench.c). */
	nb_tick_fn *tick;
	void *tick_ctx;
	uint32_t mark;			/* arena state before prep */
	void *ref_aux;			/* the reference's tables (live check) */
	/* live modes: the frame of the case's setup then of the last check, its hash, the checks
	   tried */
	uint32_t live_addr, live_hash, live_tries;
};

/* --- families (fam_*.c) --- */

enum nb_fam_id {
	NB_F_CONV,	/* conv2d: dense, depthwise, pointwise, any kh x kw, int8/ternary/int4 */
	NB_F_DOT,	/* dot product of n u8 x weights (macbench kernels, packed widths) */
	NB_F_BDOT,	/* binary dot product of n {-1,+1} x {-1,+1} */
	NB_F_POOL,	/* maxpool2, avgpool2, global average */
	NB_F_ELEM,	/* element-wise ops, requant, upsample, concat, pair words */
	NB_F_POST,	/* head post-processing on a logit grid */
	NB_F_PREP,	/* input preparation from a 640-wide luma strip */
	NB_F_MEM,	/* memcpy, memset, strided reads */
	NB_F_ICACHE,	/* straight-line code of a given size (nnbench only) */
	NB_F_BDNET,	/* the bird detector network as shipped (birddetect/net.c, nnbench only) */
	NB_F_TSPLIT,	/* a trained ternary layer, generated code split into blocks (nnbench3 only) */
	NB_FAMS,
};
/* The plan (plan.c of each plugin): its name, its families (NULL: not linked), the stages. */
extern const char nb_plan_name[];
extern const struct nb_fam *const nb_fams[NB_FAMS];

/* Conv parameters (p[]) and flags. */
enum { NB_CIN, NB_COUT, NB_H, NB_W, NB_KH, NB_KW, NB_S, NB_D, NB_FL, NB_NZ };
#define NB_FL_DW 1u	/* depthwise: cout = cin, output o reads channel o */
#define NB_FL_TERN 2u	/* weights in {-1, 0, 1}, exactly nz non-zero per output channel */
#define NB_FL_I4 4u	/* weights in [-7, 7] */
#define NB_FL_RAW 8u	/* no requantization: int32 output (head) */

/* Conv geometry derived from the parameters. */
struct nb_geo {
	unsigned int cin, cout, h, w, kh, kw, s, d, fl;
	unsigned int ph, pw;	/* padding */
	unsigned int hp, wp;	/* padded plane */
	unsigned int ho, wo;
	unsigned int taps;	/* weights per output channel */
};
void nb_geo(const uint32_t *p, struct nb_geo *g);
/* Deterministic weights [o][i][ky][kx] (i over 1 channel if depthwise), bias with the rounding
   term, shift; gen_kernels.py computes the same. */
void nb_conv_weights(const uint32_t *p, int8_t *w, int32_t *b, uint8_t *s);
/* conv variants other families reuse on r->w, r->b, r->s: the plain C reference over padded CHW
   planes, the pair words, pair_tab_asm (any non-zero count per output channel). */
void nb_prep_chw(struct nb_run *r);
void nb_conv_c(struct nb_run *r);
uint32_t nb_get_chw(const struct nb_run *r, const void *buf, uint32_t i);
void nb_pair_input(struct nb_run *r);
void nb_prep_pair_tab(struct nb_run *r);
void nb_conv_pair_tab_asm(struct nb_run *r);

/* --- the case runner (cases.c), shared by bench.c and the simulator test --- */

struct nb_stage {
	const char *name;
	const struct nb_case *cases;
	unsigned int ncases;
	unsigned int isa;	/* isa.c stage + 1 instead of cases */
};
extern const struct nb_stage nb_stages[];
extern const unsigned int nb_nstages;

/* Bump allocator over the plan's arena (NB_ARENA(bytes) in plan.c); NULL when it is full. */
#define NB_ARENA(bytes)								\
	uint8_t nb_arena_buf[bytes] __attribute__((aligned(64)));		\
	const uint32_t nb_arena_size = (bytes)
extern uint8_t nb_arena_buf[];
extern const uint32_t nb_arena_size;
void nb_arena_reset(void);
uint32_t nb_arena_mark(void);
void nb_arena_release(uint32_t mark);
void *nb_alloc(uint32_t bytes);
uint32_t nb_rand(uint32_t *x);
void nb_fill_u8(uint8_t *p, uint32_t n, uint32_t seed);
uint32_t nb_seed(const uint32_t *p, unsigned int fam);
uint32_t nb_min(const uint32_t *v, unsigned int n);
/* Sorts v. */
uint32_t nb_median(uint32_t *v, unsigned int n);

/* Builds the case data and the reference output: 0, -1 if it does not fit the arena, -2 if a
   live case has no frame. */
int nb_case_begin(struct nb_run *r, const struct nb_case *k);
/* Prepares variant vi (not the reference): 1 to measure, 0 not supported, <0 no memory. */
int nb_var_begin(struct nb_run *r, unsigned int vi);
/* The generated kernel for the case's parameters with px pixels per call, or NULL. */
const struct nb_gen *nb_find_gen(const struct nb_run *r, unsigned int px);
/* Mismatching output elements of the last run against the reference. Live modes: the reference
   and the variant on the newest frame, kept only if the frame's hash did not change around them
   (up to NB_LIVE_TRIES frames; r->live_tries = 0 if it changed every time). */
uint32_t nb_var_check(struct nb_run *r);
void nb_var_end(struct nb_run *r);

#define NB_LIVE_TRIES 4u
#define NB_FRAME_W 640u		/* the live FD frame: 640 x 480 Y8 */
#define NB_FRAME_H 480u
/* The newest live FD frame (cached alias), or NULL: bench.c on the camera, the test in the
   simulator. nb_live_purge drops this core's stale cached lines of it. */
const uint8_t *nb_live_frame(void);
void nb_live_purge(void);
uint32_t nb_hash_words(const uint32_t *p, uint32_t n);

/* --- logging rows: CPU1 produces them, CPU0 formats them --- */

#define NB_ISA_VAR 0xFFu

struct nb_row {
	uint16_t stage, ci;	/* stage, case index (isa: row index) */
	uint8_t var;		/* variant index, NB_ISA_VAR for instruction rows */
	uint8_t status;
	uint8_t kind;		/* isa rows: enum nb_isa_kind */
	uint8_t reps;
	uint32_t iters;		/* calls per rep */
	uint32_t us_min, us_med;	/* per rep */
	uint32_t ops, ops2;	/* per call */
	uint32_t bad;		/* mismatching outputs */
	uint32_t code;
	uint32_t nblk;
	uint16_t blk[NB_BLK_MAX];
	const char *name;	/* isa rows (rodata) */
	uint32_t got, want, got2, want2;	/* live rows: frame, hash, tries */
};

/* --- isa.c: the instruction timings and probes taken over from macbench --- */

enum nb_isa_stage {
	NB_ISA_CALIB,	/* empty loop, add: clock check */
	NB_ISA_MUL,	/* mul/mulu cycles, MDR probe */
	NB_ISA_UDF,	/* udf00/udfu01 forms the firmware runs on CPU1 */
	NB_ISA_UDF01,	/* udf01 Dm,Dn */
	NB_ISA_STAGES,
};

enum nb_isa_kind {
	NB_K_EMPTY,	/* the empty block loop: subtracted from the op rows */
	NB_K_OP,	/* a block of 64 instructions: ops = 64 x loops */
	NB_K_PAIR,	/* 64 x (mov Dn + the instruction): the mov is subtracted */
	NB_K_PROBE,	/* one instruction, not timed: got = Dn, got2 = MDR, want2 = 64-bit high word */
};

#define NB_ISA_ROWS 24
unsigned int nb_isa_stage(unsigned int stage, struct nb_row *rows);

/* --- CPU0 <-> CPU1 --- */

#define NB_RING 32u

/* Written by both sides through FENYKL_SHARED_PTR only. */
struct nb_shared {
	uint32_t cmd;		/* CPU0: stage + 1 to run */
	uint32_t done;		/* CPU1: cmd of the finished stage */
	uint32_t core;		/* FENYKL_CORE_ID() on the worker */
	uint32_t head;		/* CPU1: rows produced */
	uint32_t tail;		/* CPU0: rows consumed */
	uint32_t ci;		/* CPU1: case being run */
	uint32_t fd_addr;	/* CPU0: the newest FD frame slot (uncached), 0 = none */
	struct nb_row ring[NB_RING];
};
extern struct nb_shared nb_sh;

void nb_worker_cpu1(void);	/* FENYKL_CPU1_WORKER(nb_worker) in bench.c */

#endif
