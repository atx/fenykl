/*
 * nnbench3: does a trained ternary layer run faster as generated code split into I-cache-sized
 * blocks of output channels than with pair_tab_asm? Same harness, log format and report as nnbench
 * (../nnbench/nnbench.c); the layers and their splits come from nb_gen.h (gen_kernels.py).
 *
 * A layer's generated code (~11 bytes per non-zero weight + ~128 per output channel) is far larger
 * than the ~8 KiB I-cache: run whole, every call of the 4-pixel function misses through all of it.
 * Split, each block is fetched once and then hits for the rest of its calls. The open risk is the
 * D-cache: every block streams the whole input again (L5: 17 KiB of pair words), hence the cold
 * runs and the strided-read sweep (line size, cache size).
 *
 * Camera: copy FENYKL.BIN to the card root, power on in P mode and leave live view idle (as for
 * nnbench, so the numbers compare). CPU1 is done a few seconds after the 3 s start delay; wait a
 * minute, power off, keep FENYKL.LOG (ends with "nnbench done") and run
 * `python3 ../nnbench/nnbench_report.py nnbench3.log`: its "split ternary layers" table gives
 * Mcycles per layer per variant and the speedup over pair_tab_asm.
 */
#include "nb3.h"
#include "nb_gen.h"

const char nb_plan_name[] = "nnbench3";

extern const struct nb_fam nb_fam_tsplit, nb_fam_mem;

const struct nb_fam *const nb_fams[NB_FAMS] = {
	[NB_F_TSPLIT] = &nb_fam_tsplit,
	[NB_F_MEM] = &nb_fam_mem,
};

NB_ARENA(80u << 10);

#define K(fam, m, ...) { fam, m, { __VA_ARGS__ } }
#define LIST(a) a, sizeof(a) / sizeof((a)[0])

/* L5, ternary 28 -> 28 3x3 on 9 x 12 (L5-L8 have this shape). */
static const struct nb_case nb_c_l5[] = {
	K(NB_F_TSPLIT, NB_WARM, NB3_L5),
};

/* L5 with the D-cache purged before every call: the input comes from DRAM. */
static const struct nb_case nb_c_l5cold[] = {
	K(NB_F_TSPLIT, NB_COLD, NB3_L5),
};

/* L2 (12 -> 16 on 18 x 24) and L4 (16 -> 28 on 9 x 12). */
static const struct nb_case nb_c_l2l4[] = {
	K(NB_F_TSPLIT, NB_WARM, NB3_L2),
	K(NB_F_TSPLIT, NB_WARM, NB3_L4),
};

/* Strided reads: the D-cache size (stride 16 over 2..64 KiB) and line size (strides 4..64 over
   64 KiB), and DRAM through the uncached alias. */
static const struct nb_case nb_c_dcache[] = {
	K(NB_F_MEM, NB_WARM, 3, 2048, 16),
	K(NB_F_MEM, NB_WARM, 3, 4096, 16),
	K(NB_F_MEM, NB_WARM, 3, 8192, 16),
	K(NB_F_MEM, NB_WARM, 3, 12288, 16),
	K(NB_F_MEM, NB_WARM, 3, 16384, 16),
	K(NB_F_MEM, NB_WARM, 3, 24576, 16),
	K(NB_F_MEM, NB_WARM, 3, 32768, 16),
	K(NB_F_MEM, NB_WARM, 3, 65536, 16),
	K(NB_F_MEM, NB_WARM, 3, 65536, 4),
	K(NB_F_MEM, NB_WARM, 3, 65536, 8),
	K(NB_F_MEM, NB_WARM, 3, 65536, 32),
	K(NB_F_MEM, NB_WARM, 3, 65536, 64),
	K(NB_F_MEM, NB_UNCACHED, 3, 16384, 4),
	K(NB_F_MEM, NB_UNCACHED, 3, 16384, 16),
};

const struct nb_stage nb_stages[] = {
	{ "calib", NULL, 0, NB_ISA_CALIB + 1 },
	{ "l5", LIST(nb_c_l5), 0 },
	{ "l5cold", LIST(nb_c_l5cold), 0 },
	{ "l2l4", LIST(nb_c_l2l4), 0 },
	{ "dcache", LIST(nb_c_dcache), 0 },
};

const unsigned int nb_nstages = sizeof(nb_stages) / sizeof(nb_stages[0]);
