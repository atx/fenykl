/* int8 dot-product kernels for macbench: u8 activations x s8 weights, int32 sums. Pure code, no
   firmware calls. */
#include "macbench.h"

#define MB_DOT_C_BODY							\
	{								\
		int32_t s = 0;						\
		for (uint32_t i = 0; i < n; i++)			\
			s += a[i] * w[i];				\
		return s;						\
	}

int32_t mb_dot_ref(const uint8_t *a, const int8_t *w, uint32_t n) MB_DOT_C_BODY

__attribute__((optimize("O2")))
int32_t mb_dot_o2(const uint8_t *a, const int8_t *w, uint32_t n) MB_DOT_C_BODY

/* One MAC at byte offset off: the product lands in dst, src is the other operand. */
#define MB_MAC(off, op, dst, src)					\
	"movbu (" #off ",%[w]),%[y]\n\t"				\
	"movbu (" #off ",%[a]),%[x]\n\t"				\
	"extb %[y]\n\t"							\
	op " %[" #src "],%[" #dst "]\n\t"				\
	"add %[" #dst "],%[s]\n\t"

/* op Dm,Dn computes Dn *= Dm: x (activation) as Dn is "op y,x", as Dm "op x,y". */
#define MB_DOT_ASM(fn, op, dst, src)					\
	int32_t fn(const uint8_t *a, const int8_t *w, uint32_t n)	\
	{								\
		int32_t s = 0;						\
		uint32_t x, y, k = n / 4;				\
									\
		__asm__ volatile("setlb\n\t"				\
				 MB_MAC(0, op, dst, src)		\
				 MB_MAC(1, op, dst, src)		\
				 MB_MAC(2, op, dst, src)		\
				 MB_MAC(3, op, dst, src)		\
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

MB_DOT_ASM(mb_dot_mul_dn, "mul", x, y)
MB_DOT_ASM(mb_dot_mul_dm, "mul", y, x)
MB_DOT_ASM(mb_dot_udf_dn, "udf00", x, y)
MB_DOT_ASM(mb_dot_udf_dm, "udf00", y, x)

int32_t mb_pack(int8_t w0, int8_t w1)
{
	return (int32_t)((uint32_t)(int32_t)w0 + ((uint32_t)(int32_t)w1 << 24));
}

/* {hi, lo} = S0 + S1 * 2^24 with |S0| < 2^23: S0 is the sign-extended low 24 bits. */
static void mb_unpack(uint32_t lo, uint32_t hi, int32_t out[2])
{
	int32_t s0 = (int32_t)(lo << 8) >> 8;
	uint32_t l = lo - (uint32_t)s0;
	uint32_t h = hi - (s0 < 0 ? 0xFFFFFFFFu : 0) - (lo < (uint32_t)s0);

	out[0] = s0;
	out[1] = (int32_t)((h << 8) | (l >> 24));
}

#define MB_DOT2_ASM(fn, op, dst, src)					\
	void fn(const uint8_t *a, const int32_t *p, uint32_t n, int32_t out[2]) \
	{								\
		uint32_t lo = 0, hi = 0, x, y;				\
		const uint8_t *e = a + n;				\
									\
		__asm__ volatile("setlb\n\t"				\
				 "movbu (%[a]),%[x]\n\t"		\
				 "mov (%[p]),%[y]\n\t"			\
				 "inc %[a]\n\t"				\
				 "inc4 %[p]\n\t"			\
				 op " %[" #src "],%[" #dst "]\n\t"	\
				 "add %[" #dst "],%[lo]\n\t"		\
				 "mov mdr,%[" #dst "]\n\t"		\
				 "addc %[" #dst "],%[hi]\n\t"		\
				 "cmp %[e],%[a]\n\t"			\
				 "lne"					\
				 : [lo] "+d"(lo), [hi] "+d"(hi), [x] "=&d"(x), \
				   [y] "=&d"(y), [a] "+a"(a), [p] "+a"(p) \
				 : [e] "a"(e)				\
				 : "mdr", "cc", "memory");		\
		mb_unpack(lo, hi, out);					\
	}

MB_DOT2_ASM(mb_dot2_mul, "mul", x, y)
MB_DOT2_ASM(mb_dot2_udf, "udf00", x, y)

const int8_t mb_tern_w[8] = { 1, -1, 0, 1, 0, -1, 1, 0 };

/* Eight weights of mb_tern_w at byte offset mb_o: five loads, five add/sub, no load-use stall. */
#define MB_TERN8							\
	"movbu (mb_o,%[a]),%[x]\n\t"					\
	"movbu (mb_o+1,%[a]),%[y]\n\t"					\
	"add %[x],%[s]\n\t"						\
	"movbu (mb_o+3,%[a]),%[x]\n\t"					\
	"sub %[y],%[s]\n\t"						\
	"movbu (mb_o+5,%[a]),%[y]\n\t"					\
	"add %[x],%[s]\n\t"						\
	"movbu (mb_o+6,%[a]),%[x]\n\t"					\
	"sub %[y],%[s]\n\t"						\
	"add %[x],%[s]\n\t"

int32_t mb_dot_tern(const uint8_t *a, uint32_t n)
{
	int32_t s = 0;
	uint32_t x, y, k = n / MB_TERN_BLOCK;

	__asm__ volatile("setlb\n\t"
			 ".set mb_o,0\n\t"
			 ".rept 4\n\t"
			 MB_TERN8
			 ".set mb_o,mb_o+8\n\t"
			 ".endr\n\t"
			 "add 32,%[a]\n\t"
			 "add -1,%[k]\n\t"
			 "lne"
			 : [s] "+d"(s), [x] "=&d"(x), [y] "=&d"(y), [k] "+d"(k), [a] "+a"(a)
			 :
			 : "cc", "memory");
	return s;
}

void mb_fill(uint8_t *a, int8_t *w, uint32_t n, int32_t *p, uint32_t np, int8_t *t)
{
	uint32_t x = 0x2545F491u;

	for (uint32_t i = 0; i < n; i++) {
		x = x * 1664525u + 1013904223u;
		a[i] = x >> 24;
		w[i] = (int8_t)((((x >> 8) & 0xFFu) * 255u >> 8) - 127);
		t[i] = mb_tern_w[i % 8];
	}
	/* The extremes of the range. */
	a[0] = 255;
	a[1] = 255;
	w[0] = -127;
	w[1] = 127;
	for (uint32_t i = 0; i < np; i++)
		p[i] = mb_pack(w[i], w[np + i]);
}
