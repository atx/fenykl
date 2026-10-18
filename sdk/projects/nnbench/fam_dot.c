/*
 * Dot products: u8 activations a[0..n) against two weight vectors w0 = w[0..n), w1 = w[n..2n);
 * the output is { a . w0, a . w1 }. The int8 kernels are macbench's (one pass per vector), the
 * packed ones hold the weights at 4 or 2 bits. Binary dot products: {-1, +1} vectors packed 32 per
 * word, a . w = n - 2 popcount(a ^ w) (no popcount instruction on this core).
 */
#include "nnbench.h"

enum { NB_DOT_N, NB_DOT_WF };	/* params */
#define NB_WF_I8 8u
#define NB_WF_I4 4u
#define NB_WF_I2 2u
#define NB_WF_PAT 3u		/* macbench's ternary pattern, the code kernel */
#define NB_PACK_N 256u		/* dot2 packed: 24-bit lanes */

static const int8_t nb_tern_pat[8] = { 1, -1, 0, 1, 0, -1, 1, 0 };

static void nb_dot_setup(struct nb_run *r)
{
	uint32_t n = r->p[NB_DOT_N], wf = r->p[NB_DOT_WF], x = nb_seed(r->p, NB_F_DOT);

	r->src = nb_alloc(n);
	r->w = nb_alloc(2 * n);
	if (!r->w)
		return;
	nb_fill_u8(r->src, n, x);
	for (uint32_t i = 0; i < 2 * n; i++) {
		uint32_t v = nb_rand(&x);

		if (wf == NB_WF_PAT)
			r->w[i] = nb_tern_pat[i % 8];
		else if (wf == NB_WF_I4)
			r->w[i] = (int8_t)(v % 15) - 7;
		else if (wf == NB_WF_I2)
			r->w[i] = (int8_t)(v % 4) - 2;
		else
			r->w[i] = (int8_t)(v % 255) - 127;
	}
	/* the extremes of the range, as macbench */
	if (wf == NB_WF_I8) {
		r->src[0] = r->src[1] = 255;
		r->w[0] = -127;
		r->w[1] = 127;
	}
	r->n_out = 2;
	r->out_bytes = 8;
	r->ops = 2 * n;
	r->ops2 = 2 * n;
}

static void nb_prep_dot(struct nb_run *r)
{
	r->in = r->src;
	r->aux = r->w;
}

static uint32_t nb_get_i32(const struct nb_run *r, const void *buf, uint32_t i)
{
	(void)r;
	return ((const uint32_t *)buf)[i];
}

/* --- int8: macbench's kernels --- */

#define NB_DOT_C_BODY							\
	{								\
		int32_t s = 0;						\
		for (uint32_t i = 0; i < n; i++)			\
			s += a[i] * w[i];				\
		return s;						\
	}

static int32_t nb_dot_os(const uint8_t *a, const int8_t *w, uint32_t n) NB_DOT_C_BODY
static int32_t NB_HOT nb_dot_o2(const uint8_t *a, const int8_t *w, uint32_t n) NB_DOT_C_BODY

/* One MAC at byte offset off: the product lands in dst, src is the other operand. */
#define NB_MAC(off, op, dst, src)					\
	"movbu (" #off ",%[w]),%[y]\n\t"				\
	"movbu (" #off ",%[a]),%[x]\n\t"				\
	"extb %[y]\n\t"							\
	op " %[" #src "],%[" #dst "]\n\t"				\
	"add %[" #dst "],%[s]\n\t"

/* op Dm,Dn computes Dn *= Dm: x (activation) as Dn is "op y,x", as Dm "op x,y". 4 MACs per pass. */
#define NB_DOT_ASM(fn, op, dst, src)					\
	static int32_t fn(const uint8_t *a, const int8_t *w, uint32_t n) \
	{								\
		int32_t s = 0;						\
		uint32_t x, y, k = n / 4;				\
									\
		__asm__ volatile("setlb\n\t"				\
				 NB_MAC(0, op, dst, src)		\
				 NB_MAC(1, op, dst, src)		\
				 NB_MAC(2, op, dst, src)		\
				 NB_MAC(3, op, dst, src)		\
				 "add 4,%[a]\n\t"			\
				 "add 4,%[w]\n\t"			\
				 "add -1,%[k]\n\t"			\
				 "lne"					\
				 : [s] "+d"(s), [x] "=&d"(x), [y] "=&d"(y), \
				   [k] "+d"(k), [a] "+a"(a), [w] "+a"(w) \
				 :					\
				 : "mdr", "cc", "memory");		\
		return s;						\
	}

NB_DOT_ASM(nb_dot_mul_dn, "mul", x, y)
NB_DOT_ASM(nb_dot_mul_dm, "mul", y, x)
NB_DOT_ASM(nb_dot_udf_dn, "udf00", x, y)
NB_DOT_ASM(nb_dot_udf_dm, "udf00", y, x)

/* Eight weights of nb_tern_pat at byte offset nb_o: five loads, five add/sub. */
#define NB_TERN8							\
	"movbu (nb_o,%[a]),%[x]\n\t"					\
	"movbu (nb_o+1,%[a]),%[y]\n\t"					\
	"add %[x],%[s]\n\t"						\
	"movbu (nb_o+3,%[a]),%[x]\n\t"					\
	"sub %[y],%[s]\n\t"						\
	"movbu (nb_o+5,%[a]),%[y]\n\t"					\
	"add %[x],%[s]\n\t"						\
	"movbu (nb_o+6,%[a]),%[x]\n\t"					\
	"sub %[y],%[s]\n\t"						\
	"add %[x],%[s]\n\t"

/* Ternary weights compiled into code, 32 per pass (n % 32 == 0). */
static int32_t nb_dot_tern(const uint8_t *a, const int8_t *w, uint32_t n)
{
	int32_t s = 0;
	uint32_t x, y, k = n / 32;

	(void)w;
	__asm__ volatile("setlb\n\t"
			 ".set nb_o,0\n\t"
			 ".rept 4\n\t"
			 NB_TERN8
			 ".set nb_o,nb_o+8\n\t"
			 ".endr\n\t"
			 "add 32,%[a]\n\t"
			 "add -1,%[k]\n\t"
			 "lne"
			 : [s] "+d"(s), [x] "=&d"(x), [y] "=&d"(y), [k] "+d"(k), [a] "+a"(a)
			 :
			 : "cc", "memory");
	return s;
}

typedef int32_t nb_dot_fn(const uint8_t *a, const int8_t *w, uint32_t n);

static void nb_dot_two(struct nb_run *r, nb_dot_fn *fn)
{
	uint32_t n = r->p[NB_DOT_N];
	int32_t *out = r->out;
	const int8_t *w = r->aux;

	out[0] = fn(r->rin, w, n);
	out[1] = fn(r->rin, w + n, n);
}

static void nb_run_dot_os(struct nb_run *r) { nb_dot_two(r, nb_dot_os); }
static void nb_run_dot_o2(struct nb_run *r) { nb_dot_two(r, nb_dot_o2); }
static void nb_run_mul_dn(struct nb_run *r) { nb_dot_two(r, nb_dot_mul_dn); }
static void nb_run_mul_dm(struct nb_run *r) { nb_dot_two(r, nb_dot_mul_dm); }
static void nb_run_udf_dn(struct nb_run *r) { nb_dot_two(r, nb_dot_udf_dn); }
static void nb_run_udf_dm(struct nb_run *r) { nb_dot_two(r, nb_dot_udf_dm); }
static void nb_run_tern(struct nb_run *r) { nb_dot_two(r, nb_dot_tern); }

static int nb_ok_asm4(const struct nb_run *r)
{
	return r->p[NB_DOT_N] % 4 == 0;
}

static int nb_ok_tern(const struct nb_run *r)
{
	return r->p[NB_DOT_WF] == NB_WF_PAT && r->p[NB_DOT_N] % 32 == 0;
}

/* --- dot2: p[i] = w0[i] + w1[i] * 2^24, the 64-bit mul product summed in {MDR, Dn} --- */

static int nb_ok_dot2(const struct nb_run *r)
{
	return r->p[NB_DOT_N] <= NB_PACK_N;
}

static void nb_prep_dot2(struct nb_run *r)
{
	uint32_t n = r->p[NB_DOT_N];
	int32_t *p = r->aux = nb_alloc(4 * n);

	r->in = r->src;
	if (!p)
		return;
	for (uint32_t i = 0; i < n; i++)
		p[i] = (int32_t)((uint32_t)(int32_t)r->w[i] + ((uint32_t)(int32_t)r->w[n + i] << 24));
}

/* {hi, lo} = S0 + S1 * 2^24 with |S0| < 2^23: S0 is the sign-extended low 24 bits. */
static void nb_unpack24(uint32_t lo, uint32_t hi, int32_t out[2])
{
	int32_t s0 = (int32_t)(lo << 8) >> 8;
	uint32_t l = lo - (uint32_t)s0;
	uint32_t h = hi - (s0 < 0 ? 0xFFFFFFFFu : 0) - (lo < (uint32_t)s0);

	out[0] = s0;
	out[1] = (int32_t)((h << 8) | (l >> 24));
}

static void nb_run_dot2(struct nb_run *r)
{
	const uint8_t *a = r->rin, *e = a + r->p[NB_DOT_N];
	const int32_t *p = r->aux;
	uint32_t lo = 0, hi = 0, x, y;

	__asm__ volatile("setlb\n\t"
			 "movbu (%[a]),%[x]\n\t"
			 "mov (%[p]),%[y]\n\t"
			 "inc %[a]\n\t"
			 "inc4 %[p]\n\t"
			 "mul %[y],%[x]\n\t"
			 "add %[x],%[lo]\n\t"
			 "mov mdr,%[x]\n\t"
			 "addc %[x],%[hi]\n\t"
			 "cmp %[e],%[a]\n\t"
			 "lne"
			 : [lo] "+d"(lo), [hi] "+d"(hi), [x] "=&d"(x), [y] "=&d"(y), [a] "+a"(a),
			   [p] "+a"(p)
			 : [e] "a"(e)
			 : "mdr", "cc", "memory");
	nb_unpack24(lo, hi, r->out);
}

/* --- packed int4 (two's complement nibbles, low nibble first) and int2 (four per byte) --- */

static int nb_ok_i4(const struct nb_run *r)
{
	return r->p[NB_DOT_WF] == NB_WF_I4 || r->p[NB_DOT_WF] == NB_WF_I2;
}

static int nb_ok_i2(const struct nb_run *r)
{
	return r->p[NB_DOT_WF] == NB_WF_I2 && r->p[NB_DOT_N] % 4 == 0;
}

static void nb_prep_pack(struct nb_run *r, unsigned int bits)
{
	uint32_t n = 2 * r->p[NB_DOT_N], per = 8 / bits;
	uint8_t *q = r->aux = nb_alloc(n / per);

	r->in = r->src;
	if (!q)
		return;
	for (uint32_t i = 0; i < n / per; i++) {
		uint32_t v = 0;

		for (uint32_t j = 0; j < per; j++)
			v |= ((uint32_t)r->w[i * per + j] & ((1u << bits) - 1)) << (j * bits);
		q[i] = (uint8_t)v;
	}
}

static void nb_prep_i4(struct nb_run *r) { nb_prep_pack(r, 4); }

static int nb_ok_i4_s(const struct nb_run *r)
{
	return nb_ok_i4(r) && r->p[NB_DOT_N] % 4 == 0;
}
static void nb_prep_i2(struct nb_run *r) { nb_prep_pack(r, 2); }

static int32_t NB_HOT nb_dot_i4(const uint8_t *a, const uint8_t *q, uint32_t n)
{
	int32_t s = 0;

	for (uint32_t i = 0; i < n; i += 2, q++) {
		int32_t lo = (int32_t)((uint32_t)*q << 28) >> 28, hi = (int32_t)((uint32_t)*q << 24) >> 28;

		s += nb_mul(a[i], lo) + nb_mul(a[i + 1], hi);
	}
	return s;
}

static void NB_HOT nb_run_i4(struct nb_run *r)
{
	uint32_t n = r->p[NB_DOT_N];
	const uint8_t *q = r->aux;
	int32_t *out = r->out;

	out[0] = nb_dot_i4(r->rin, q, n);
	out[1] = nb_dot_i4(r->rin, q + n / 2, n);
}

/* int2 {-2..1}, four per byte, low bits first. */
static int32_t NB_HOT nb_dot_i2(const uint8_t *a, const uint8_t *q, uint32_t n)
{
	int32_t s = 0;

	for (uint32_t i = 0; i < n; i += 4, q++) {
		uint32_t v = *q;

		for (uint32_t j = 0; j < 4; j++, v >>= 2)
			s += nb_mul(a[i + j], (int32_t)(v << 30) >> 30);
	}
	return s;
}

static void NB_HOT nb_run_i2(struct nb_run *r)
{
	uint32_t n = r->p[NB_DOT_N];
	const uint8_t *q = r->aux;
	int32_t *out = r->out;

	out[0] = nb_dot_i2(r->rin, q, n);
	out[1] = nb_dot_i2(r->rin, q + n / 4, n);
}

/* nb_kern.S: the job layouts there. */
struct nb_dot_job {
	const uint8_t *a;
	const uint8_t *q;
	uint32_t n;		/* q bytes (i4s, i2_lut) or taps (i4_lut) */
	const uint32_t *lut;
	int32_t s0, s1;		/* out: the two sums (i4_lut, i2_lut) */
};
int32_t nb_dot_i4s(struct nb_dot_job *j);
void nb_dot_i4_lut(struct nb_dot_job *j);
void nb_dot_i2_lut(struct nb_dot_job *j);

static void NB_HOT nb_run_i4s(struct nb_run *r)
{
	uint32_t n = r->p[NB_DOT_N];
	const uint8_t *q = r->aux;
	int32_t *out = r->out;
	struct nb_dot_job j = { r->rin, q, n / 2, NULL, 0, 0 };

	out[0] = nb_dot_i4s(&j);
	j.q = q + n / 2;
	out[1] = nb_dot_i4s(&j);
}

static int nb_ok_i4_lut(const struct nb_run *r)
{
	return nb_ok_i4(r) && r->p[NB_DOT_N] % 16 == 0;
}

static int nb_ok_i2_lut(const struct nb_run *r)
{
	return r->p[NB_DOT_WF] == NB_WF_I2 && r->p[NB_DOT_N] % 32 == 0;
}

/*
 * aux: the lut (2^bits squared entries: w0 + w1 * 2^16 of the two outputs' weights in one index),
 * then per tap the index w0 | w1 << bits, two taps per byte for int2.
 */
static void nb_prep_spread(struct nb_run *r, unsigned int bits)
{
	uint32_t n = r->p[NB_DOT_N], m = (1u << bits) - 1, nl = 1u << (2 * bits);
	uint32_t *lut = r->aux = nb_alloc(4 * nl + n);
	uint8_t *q = (uint8_t *)(lut + nl);

	r->in = r->src;
	if (!lut)
		return;
	for (uint32_t v = 0; v < nl; v++) {
		int32_t w0 = (int32_t)((v & m) << (32 - bits)) >> (32 - bits);
		int32_t w1 = (int32_t)((v >> bits) << (32 - bits)) >> (32 - bits);

		lut[v] = (uint32_t)(w0 + w1 * 65536);
	}
	for (uint32_t i = 0; i < n; i++) {
		uint32_t v = ((uint32_t)r->w[i] & m) | ((uint32_t)r->w[n + i] & m) << bits;

		if (bits == 4)
			q[i] = (uint8_t)v;
		else if (i % 2)
			q[i / 2] |= (uint8_t)(v << 4);
		else
			q[i / 2] = (uint8_t)v;
	}
}

static void nb_prep_i4_lut(struct nb_run *r) { nb_prep_spread(r, 4); }
static void nb_prep_i2_lut(struct nb_run *r) { nb_prep_spread(r, 2); }

static void NB_HOT nb_run_spread(struct nb_run *r)
{
	unsigned int i4 = r->v->name[1] == '4';
	uint32_t n = r->p[NB_DOT_N], nl = i4 ? 256 : 16;
	const uint32_t *lut = r->aux;
	struct nb_dot_job j = { r->rin, (const uint8_t *)(lut + nl), i4 ? n : n / 2, lut, 0, 0 };
	int32_t *out = r->out;

	if (i4)
		nb_dot_i4_lut(&j);
	else
		nb_dot_i2_lut(&j);
	out[0] = j.s0;
	out[1] = j.s1;
}

static const struct nb_var nb_dot_vars[] = {
	{ "c_os", NULL, nb_prep_dot, nb_run_dot_os, nb_get_i32 },
	{ "c", NULL, nb_prep_dot, nb_run_dot_o2, nb_get_i32 },
	{ "mul_dn", nb_ok_asm4, nb_prep_dot, nb_run_mul_dn, nb_get_i32 },
	{ "mul_dm", nb_ok_asm4, nb_prep_dot, nb_run_mul_dm, nb_get_i32 },
	{ "udf_dn", nb_ok_asm4, nb_prep_dot, nb_run_udf_dn, nb_get_i32 },
	{ "udf_dm", nb_ok_asm4, nb_prep_dot, nb_run_udf_dm, nb_get_i32 },
	{ "dot2_mul", nb_ok_dot2, nb_prep_dot2, nb_run_dot2, nb_get_i32 },
	{ "tern_code", nb_ok_tern, nb_prep_dot, nb_run_tern, nb_get_i32 },
	{ "i4_cu", nb_ok_i4, nb_prep_i4, nb_run_i4, nb_get_i32 },
	{ "i2_cu", nb_ok_i2, nb_prep_i2, nb_run_i2, nb_get_i32 },
	{ "i4_asm", nb_ok_i4_s, nb_prep_i4, nb_run_i4s, nb_get_i32 },
	{ "i4_lut", nb_ok_i4_lut, nb_prep_i4_lut, nb_run_spread, nb_get_i32 },
	{ "i2_lut", nb_ok_i2_lut, nb_prep_i2_lut, nb_run_spread, nb_get_i32 },
};

const struct nb_fam nb_fam_dot = {
	"dot",
	{ "n", "wf" },
	"mac",
	"mac",
	nb_dot_setup,
	nb_dot_vars,
	sizeof(nb_dot_vars) / sizeof(nb_dot_vars[0]),
	NULL,
};

/* --- binary --- */

static void nb_bdot_setup(struct nb_run *r)
{
	uint32_t n = r->p[0];

	r->src = nb_alloc(n / 8);
	r->w = nb_alloc(n / 8);
	if (!r->w)
		return;
	nb_fill_u8(r->src, n / 8, nb_seed(r->p, NB_F_BDOT));
	nb_fill_u8((uint8_t *)r->w, n / 8, nb_seed(r->p, NB_F_BDOT) ^ 0x1234);
	r->n_out = 1;
	r->out_bytes = 4;
	r->ops = n;
	r->ops2 = n / 32;
}

/* The reference: one bit at a time, +1 where the bits agree. */
static void nb_run_bdot_c(struct nb_run *r)
{
	const uint8_t *a = r->rin, *w = r->aux;
	int32_t s = 0;

	for (uint32_t i = 0; i < r->p[0]; i++)
		s += ((a[i / 8] ^ w[i / 8]) >> (i % 8) & 1) ? -1 : 1;
	*(int32_t *)r->out = s;
}

static void nb_prep_bdot_lut(struct nb_run *r)
{
	uint8_t *t = nb_alloc(256);

	r->in = r->src;
	r->aux = t;
	if (!t)
		return;
	for (unsigned int i = 0; i < 256; i++) {
		unsigned int c = 0;

		for (unsigned int v = i; v; v >>= 1)
			c += v & 1;
		t[i] = (uint8_t)c;
	}
}

static void NB_HOT nb_run_bdot_lut(struct nb_run *r)
{
	const uint32_t *a = r->rin, *w = (const uint32_t *)r->w;
	const uint8_t *t = r->aux;
	uint32_t n = r->p[0], c = 0;

	for (uint32_t i = 0; i < n / 32; i++) {
		uint32_t x = a[i] ^ w[i];

		c += t[x & 0xFF] + t[x >> 8 & 0xFF] + t[x >> 16 & 0xFF] + t[x >> 24];
	}
	*(int32_t *)r->out = (int32_t)(n - 2 * c);
}

static void NB_HOT nb_run_bdot_swar(struct nb_run *r)
{
	const uint32_t *a = r->rin, *w = (const uint32_t *)r->w;
	uint32_t n = r->p[0], c = 0;

	for (uint32_t i = 0; i < n / 32; i++) {
		uint32_t x = a[i] ^ w[i];

		x -= x >> 1 & 0x55555555u;
		x = (x & 0x33333333u) + (x >> 2 & 0x33333333u);
		x = (x + (x >> 4)) & 0x0F0F0F0Fu;
		x += x >> 8;
		c += (x + (x >> 16)) & 0x3F;
	}
	*(int32_t *)r->out = (int32_t)(n - 2 * c);
}

static void nb_prep_bdot(struct nb_run *r)
{
	r->in = r->src;
	r->aux = r->w;
}

/* nb_kern.S */
struct nb_bdot_job {
	const void *a, *w;
	uint32_t nwords;
	const uint8_t *lut;
};
uint32_t nb_bdot_swar(const struct nb_bdot_job *j);
uint32_t nb_bdot_lut8(const struct nb_bdot_job *j);

static int nb_ok_bdot_swar(const struct nb_run *r)
{
	return r->p[0] % 64 == 0 && r->p[0] >= 64;
}

static void NB_HOT nb_run_bdot_swar_asm(struct nb_run *r)
{
	struct nb_bdot_job j = { r->rin, r->w, r->p[0] / 32u, NULL };

	*(int32_t *)r->out = (int32_t)(r->p[0] - 2 * nb_bdot_swar(&j));
}

static void NB_HOT nb_run_bdot_lut_asm(struct nb_run *r)
{
	struct nb_bdot_job j = { r->rin, r->w, r->p[0] / 32u, r->aux };

	*(int32_t *)r->out = (int32_t)(r->p[0] - 2 * nb_bdot_lut8(&j));
}

static const struct nb_var nb_bdot_vars[] = {
	{ "c", NULL, nb_prep_bdot, nb_run_bdot_c, nb_get_i32 },
	{ "lut8", NULL, nb_prep_bdot_lut, nb_run_bdot_lut, nb_get_i32 },
	{ "swar", NULL, nb_prep_bdot, nb_run_bdot_swar, nb_get_i32 },
	{ "lut8_asm", NULL, nb_prep_bdot_lut, nb_run_bdot_lut_asm, nb_get_i32 },
	{ "swar_asm", nb_ok_bdot_swar, nb_prep_bdot, nb_run_bdot_swar_asm, nb_get_i32 },
};

const struct nb_fam nb_fam_bdot = {
	"bdot",
	{ "n" },
	"mac",
	"word",
	nb_bdot_setup,
	nb_bdot_vars,
	sizeof(nb_bdot_vars) / sizeof(nb_bdot_vars[0]),
	NULL,
};
