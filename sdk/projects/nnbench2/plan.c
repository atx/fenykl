/*
 * nnbench2: the second camera run of nnbench (same harness, log format and report; see
 * ../nnbench/nnbench.c). Input preparation on the live FD frame, the int8 kernels generated with 4
 * pixels per call, softmax, and the hand-written kernels on the shapes that matter most. The
 * arena and the generated code are small so the payload stays small.
 */
#include "../nnbench/nnbench.h"

const char nb_plan_name[] = "nnbench2";

extern const struct nb_fam nb_fam_conv, nb_fam_dot, nb_fam_bdot, nb_fam_pool, nb_fam_elem,
	nb_fam_post, nb_fam_prep, nb_fam_mem;

const struct nb_fam *const nb_fams[NB_FAMS] = {
	[NB_F_CONV] = &nb_fam_conv,
	[NB_F_DOT] = &nb_fam_dot,
	[NB_F_BDOT] = &nb_fam_bdot,
	[NB_F_POOL] = &nb_fam_pool,
	[NB_F_ELEM] = &nb_fam_elem,
	[NB_F_POST] = &nb_fam_post,
	[NB_F_PREP] = &nb_fam_prep,
	[NB_F_MEM] = &nb_fam_mem,
};

NB_ARENA(64u << 10);

#define TERN NB_FL_TERN
#define DW NB_FL_DW
#define CV(m, cin, cout, h, w, kh, kw, s, d, fl, nz) \
	{ NB_F_CONV, m, { cin, cout, h, w, kh, kw, s, d, fl, nz } }
#define C3(cin, cout, h, w, s, fl, nz) CV(NB_WARM, cin, cout, h, w, 3, 3, s, 1, fl, nz)
#define C1(cin, cout, h, w, fl, nz) CV(NB_WARM, cin, cout, h, w, 1, 1, 1, 1, fl, nz)
#define K(fam, m, ...) { fam, m, { __VA_ARGS__ } }
#define LIST(a) a, sizeof(a) / sizeof((a)[0])
#define LIVE(op, ow, oh) K(NB_F_PREP, NB_LIVE, op, ow, oh, NB_FRAME_H)

/* The live 640 x 480 FD frame: point (birddetect's 96 x 72 input), box2 / box4, boxN, bilinear;
   cached after a purge (as birddetect reads it) and uncached. Outputs up to 160 x 120 (ref + out
   in the 64 KiB arena). */
static const struct nb_case nb_c_live[] = {
	LIVE(0, 96, 72),
	K(NB_F_PREP, NB_LIVE_UNC, 0, 96, 72, NB_FRAME_H),
	LIVE(0, 160, 120),
	LIVE(2, 160, 120),
	K(NB_F_PREP, NB_LIVE_UNC, 2, 160, 120, NB_FRAME_H),
	K(NB_F_PREP, NB_LIVE, 1, 320, 60, NB_FRAME_H),	/* the top 120 rows (arena) */
	LIVE(3, 96, 72),
	LIVE(4, 96, 72),
	LIVE(4, 160, 120),
};

/* Ternary pair_tab (C and asm) on the shipped network's shapes (gen: nnbench's run). */
static const struct nb_case nb_c_tern[] = {
	C3(12, 16, 18, 24, 1, TERN, 38),
	C3(32, 32, 9, 12, 1, TERN, 40),
	C3(16, 16, 12, 12, 1, TERN, 48),
	C1(32, 32, 12, 12, TERN, 11),
};

/* int8 generated with 4 pixels per call (gen and gen1 of the same cases: nnbench's run). */
static const struct nb_case nb_c_gen4[] = {
	C3(1, 12, 72, 96, 2, 0, 0),
	C3(4, 8, 16, 16, 1, 0, 0),
	C3(16, 16, 16, 16, 1, DW, 0),
	C1(8, 16, 16, 16, 0, 0),
};

/* Binary, int4 and int2 dot products; softmax and sigmoid on the head's grid; pooling. */
static const struct nb_case nb_c_small[] = {
	K(NB_F_BDOT, NB_WARM, 256),
	K(NB_F_BDOT, NB_WARM, 4096),
	K(NB_F_BDOT, NB_WARM, 32768),
	K(NB_F_DOT, NB_WARM, 256, 4),
	K(NB_F_DOT, NB_WARM, 4096, 4),
	K(NB_F_DOT, NB_WARM, 256, 2),
	K(NB_F_DOT, NB_WARM, 4096, 2),
	K(NB_F_POST, NB_WARM, 4, 108, 12),
	K(NB_F_POST, NB_WARM, 4, 300, 20),
	K(NB_F_POST, NB_WARM, 0, 300, 20),
	K(NB_F_POOL, NB_WARM, 0, 12, 36, 48),
	K(NB_F_POOL, NB_WARM, 0, 16, 18, 24),
	K(NB_F_POOL, NB_WARM, 1, 16, 18, 24),
};

const struct nb_stage nb_stages[] = {
	{ "calib", NULL, 0, NB_ISA_CALIB + 1 },
	{ "live", LIST(nb_c_live), 0 },
	{ "tern", LIST(nb_c_tern), 0 },
	{ "gen4", LIST(nb_c_gen4), 0 },
	{ "small", LIST(nb_c_small), 0 },
};

const unsigned int nb_nstages = sizeof(nb_stages) / sizeof(nb_stages[0]);
