/* nnbench's plan: the families it links, the arena and the stages in run order, the most
   important first. */
#include "nnbench.h"

const char nb_plan_name[] = "nnbench";

extern const struct nb_fam nb_fam_conv, nb_fam_dot, nb_fam_bdot, nb_fam_pool, nb_fam_elem,
	nb_fam_post, nb_fam_prep, nb_fam_mem, nb_fam_icache, nb_fam_bdnet;

const struct nb_fam *const nb_fams[NB_FAMS] = {
	[NB_F_CONV] = &nb_fam_conv,
	[NB_F_DOT] = &nb_fam_dot,
	[NB_F_BDOT] = &nb_fam_bdot,
	[NB_F_POOL] = &nb_fam_pool,
	[NB_F_ELEM] = &nb_fam_elem,
	[NB_F_POST] = &nb_fam_post,
	[NB_F_PREP] = &nb_fam_prep,
	[NB_F_MEM] = &nb_fam_mem,
	[NB_F_ICACHE] = &nb_fam_icache,
	[NB_F_BDNET] = &nb_fam_bdnet,
};

NB_ARENA(128u << 10);

/* --- the plan --- */

#define TERN NB_FL_TERN
#define DW NB_FL_DW
#define I4 NB_FL_I4
#define RAW NB_FL_RAW
#define CV(m, cin, cout, h, w, kh, kw, s, d, fl, nz) \
	{ NB_F_CONV, m, { cin, cout, h, w, kh, kw, s, d, fl, nz } }
#define C3(cin, cout, h, w, s, fl, nz) CV(NB_WARM, cin, cout, h, w, 3, 3, s, 1, fl, nz)
#define C1(cin, cout, h, w, fl, nz) CV(NB_WARM, cin, cout, h, w, 1, 1, 1, 1, fl, nz)
#define K(fam, m, ...) { fam, m, { __VA_ARGS__ } }
#define LIST(a) a, sizeof(a) / sizeof((a)[0])

static const struct nb_case nb_c_bdnet[] = {
	K(NB_F_BDNET, NB_WARM, 0),
	K(NB_F_BDNET, NB_WARM, 1),
	K(NB_F_BDNET, NB_WARM, 2),
};

/* int8 3x3: the stem, a channel grid, a spatial sweep, stride 2, cold caches. */
static const struct nb_case nb_c_conv3[] = {
	C3(1, 12, 72, 96, 2, 0, 0),
	C3(4, 8, 16, 16, 1, 0, 0),
	C3(8, 8, 16, 16, 1, 0, 0),
	C3(8, 16, 16, 16, 1, 0, 0),
	C3(16, 16, 16, 16, 1, 0, 0),
	C3(16, 32, 16, 16, 1, 0, 0),
	C3(32, 32, 8, 8, 1, 0, 0),
	C3(32, 64, 8, 8, 1, 0, 0),
	C3(64, 64, 8, 8, 1, 0, 0),
	C3(8, 8, 4, 4, 1, 0, 0),
	C3(8, 8, 8, 8, 1, 0, 0),
	C3(8, 8, 32, 32, 1, 0, 0),
	C3(8, 16, 32, 32, 2, 0, 0),
	C3(16, 32, 16, 16, 2, 0, 0),
	CV(NB_COLD, 16, 16, 16, 16, 3, 3, 1, 1, 0, 0),
	CV(NB_COLD, 32, 64, 8, 8, 3, 3, 1, 1, 0, 0),
};

/* Ternary 3x3: the shipped network's shapes, the generated-code size sweep (cout 16, cin 4..64:
   ~2..30 KiB of code), weight density. */
static const struct nb_case nb_c_tern3[] = {
	C3(12, 16, 18, 24, 1, TERN, 38),
	C3(32, 32, 9, 12, 1, TERN, 40),
	C3(4, 16, 12, 12, 1, TERN, 12),
	C3(8, 16, 12, 12, 1, TERN, 24),
	C3(16, 16, 12, 12, 1, TERN, 48),
	C3(32, 16, 12, 12, 1, TERN, 96),
	C3(64, 16, 12, 12, 1, TERN, 120),
	C3(32, 32, 12, 12, 1, TERN, 96),
	C3(16, 16, 12, 12, 1, TERN, 16),
	C3(16, 16, 12, 12, 1, TERN, 96),
	CV(NB_COLD, 32, 32, 9, 12, 3, 3, 1, 1, TERN, 40),
};

/* Depthwise and pointwise (the separable alternative), int8, ternary and int4. */
static const struct nb_case nb_c_dwpw[] = {
	C3(16, 16, 16, 16, 1, DW, 0),
	C3(32, 32, 16, 16, 1, DW, 0),
	C3(64, 64, 8, 8, 1, DW, 0),
	C3(32, 32, 32, 32, 2, DW, 0),
	C3(32, 32, 16, 16, 1, DW | TERN, 6),
	C1(8, 16, 16, 16, 0, 0),
	C1(16, 32, 16, 16, 0, 0),
	C1(32, 32, 12, 12, 0, 0),
	C1(32, 64, 12, 12, 0, 0),
	C1(64, 64, 8, 8, 0, 0),
	C1(32, 32, 12, 12, TERN, 11),
	C1(64, 64, 8, 8, TERN, 21),
	C3(8, 8, 16, 16, 1, I4, 0),
	C3(16, 16, 12, 12, 1, I4, 0),
};

/* The 1x1 head on the 12x9 grid (int32 logits) and its post-processing. */
static const struct nb_case nb_c_head[] = {
	C1(32, 1, 9, 12, RAW, 0),
	C1(16, 1, 9, 12, RAW, 0),
	C3(32, 1, 9, 12, 1, RAW, 0),
	K(NB_F_POST, NB_WARM, 0, 108, 12),
	K(NB_F_POST, NB_WARM, 1, 108, 12),
	K(NB_F_POST, NB_WARM, 2, 108, 12),
	K(NB_F_POST, NB_WARM, 3, 108, 12),
	K(NB_F_POST, NB_WARM, 0, 300, 20),
	K(NB_F_POST, NB_WARM, 3, 300, 20),
};

/* Pooling and element-wise ops: [c][h][w]. */
static const struct nb_case nb_c_elem[] = {
	K(NB_F_POOL, NB_WARM, 0, 12, 36, 48),
	K(NB_F_POOL, NB_WARM, 0, 16, 18, 24),
	K(NB_F_POOL, NB_WARM, 1, 16, 18, 24),
	K(NB_F_POOL, NB_WARM, 2, 32, 9, 12),
	K(NB_F_ELEM, NB_WARM, 0, 16, 16, 16),
	K(NB_F_ELEM, NB_WARM, 1, 16, 16, 16),
	K(NB_F_ELEM, NB_WARM, 2, 16, 16, 16),
	K(NB_F_ELEM, NB_WARM, 3, 16, 16, 16),
	K(NB_F_ELEM, NB_WARM, 4, 16, 8, 8),
	K(NB_F_ELEM, NB_WARM, 5, 16, 12, 12),
	K(NB_F_ELEM, NB_WARM, 6, 16, 12, 12),
	K(NB_F_ELEM, NB_WARM, 7, 12, 20, 26),
};

/* Input preparation from a 640 x 120 strip (x4 for a frame): cached after a purge and uncached. */
static const struct nb_case nb_c_prep[] = {
	K(NB_F_PREP, NB_COLD, 0, 96, 18, 120),
	K(NB_F_PREP, NB_UNCACHED, 0, 96, 18, 120),
	K(NB_F_PREP, NB_COLD, 0, 160, 30, 120),
	K(NB_F_PREP, NB_UNCACHED, 0, 160, 30, 120),
	K(NB_F_PREP, NB_COLD, 1, 320, 60, 120),
	K(NB_F_PREP, NB_UNCACHED, 1, 320, 60, 120),
	K(NB_F_PREP, NB_COLD, 2, 160, 30, 120),
	K(NB_F_PREP, NB_UNCACHED, 2, 160, 30, 120),
	K(NB_F_PREP, NB_COLD, 3, 96, 18, 120),
	K(NB_F_PREP, NB_UNCACHED, 3, 96, 18, 120),
	K(NB_F_PREP, NB_COLD, 4, 96, 18, 120),
	K(NB_F_PREP, NB_UNCACHED, 4, 96, 18, 120),
};

/* Dot products: macbench's int8 kernels, packed widths, binary. */
static const struct nb_case nb_c_dot[] = {
	K(NB_F_DOT, NB_WARM, 256, 8),
	K(NB_F_DOT, NB_WARM, 4096, 8),
	K(NB_F_DOT, NB_COLD, 4096, 8),
	K(NB_F_DOT, NB_UNCACHED, 4096, 8),
	K(NB_F_DOT, NB_WARM, 256, 3),
	K(NB_F_DOT, NB_WARM, 4096, 3),
	K(NB_F_DOT, NB_WARM, 256, 4),
	K(NB_F_DOT, NB_WARM, 4096, 4),
	K(NB_F_DOT, NB_WARM, 4096, 2),
	K(NB_F_BDOT, NB_WARM, 256),
	K(NB_F_BDOT, NB_WARM, 4096),
	K(NB_F_BDOT, NB_WARM, 32768),
};

/* Other kernel shapes: 5x5, 1x3 / 3x1, dilated 3x3. */
static const struct nb_case nb_c_shapes[] = {
	CV(NB_WARM, 4, 8, 16, 16, 5, 5, 1, 1, 0, 0),
	CV(NB_WARM, 8, 8, 16, 16, 5, 5, 1, 1, 0, 0),
	CV(NB_WARM, 8, 8, 16, 16, 1, 3, 1, 1, 0, 0),
	CV(NB_WARM, 8, 8, 16, 16, 3, 1, 1, 1, 0, 0),
	CV(NB_WARM, 8, 8, 16, 16, 3, 3, 1, 2, 0, 0),
	CV(NB_WARM, 16, 16, 12, 12, 5, 5, 1, 1, TERN, 64),
	CV(NB_WARM, 16, 16, 12, 12, 1, 3, 1, 1, TERN, 16),
};

/* Memory: strided reads (cache size and line), copies, sets, sums; straight-line code. */
static const struct nb_case nb_c_mem[] = {
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
	K(NB_F_MEM, NB_UNCACHED, 3, 16384, 16),
	K(NB_F_MEM, NB_WARM, 0, 4096, 0),
	K(NB_F_MEM, NB_WARM, 0, 32768, 0),
	K(NB_F_MEM, NB_COLD, 0, 32768, 0),
	K(NB_F_MEM, NB_UNCACHED, 0, 32768, 0),
	K(NB_F_MEM, NB_WARM, 1, 32768, 0),
	K(NB_F_MEM, NB_WARM, 2, 4096, 0),
	K(NB_F_MEM, NB_WARM, 2, 32768, 0),
	K(NB_F_MEM, NB_COLD, 2, 32768, 0),
	K(NB_F_MEM, NB_UNCACHED, 2, 32768, 0),
	K(NB_F_ICACHE, NB_WARM, 512),
	K(NB_F_ICACHE, NB_WARM, 2048),
	K(NB_F_ICACHE, NB_WARM, 4096),
	K(NB_F_ICACHE, NB_WARM, 8192),
	K(NB_F_ICACHE, NB_WARM, 12288),
	K(NB_F_ICACHE, NB_WARM, 16384),
	K(NB_F_ICACHE, NB_WARM, 20480),
	K(NB_F_ICACHE, NB_WARM, 24576),
	K(NB_F_ICACHE, NB_WARM, 32768),
};

const struct nb_stage nb_stages[] = {
	{ "calib", NULL, 0, NB_ISA_CALIB + 1 },
	{ "conv3", LIST(nb_c_conv3), 0 },
	{ "tern3", LIST(nb_c_tern3), 0 },
	{ "dwpw", LIST(nb_c_dwpw), 0 },
	{ "head", LIST(nb_c_head), 0 },
	{ "elem", LIST(nb_c_elem), 0 },
	{ "prep", LIST(nb_c_prep), 0 },
	{ "dot", LIST(nb_c_dot), 0 },
	{ "shapes", LIST(nb_c_shapes), 0 },
	{ "mem", LIST(nb_c_mem), 0 },
	{ "mul", NULL, 0, NB_ISA_MUL + 1 },
	{ "udf", NULL, 0, NB_ISA_UDF + 1 },
	{ "udf01", NULL, 0, NB_ISA_UDF01 + 1 },
	/* last: it hangs CPU1 on the camera (cause open) */
	{ "bdnet", LIST(nb_c_bdnet), 0 },
};

const unsigned int nb_nstages = sizeof(nb_stages) / sizeof(nb_stages[0]);
