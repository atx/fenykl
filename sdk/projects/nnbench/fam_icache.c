/* The instruction cache: straight-line code of a given size (a 32 KiB block, nnbench only). */
#include "nnbench.h"

static uint32_t nb_icache_get(const struct nb_run *r, const void *buf, uint32_t i)
{
	(void)r;
	return ((const uint32_t *)buf)[i];
}

static void nb_icache_prep(struct nb_run *r)
{
	r->in = r->src;
}

/* --- icache: the last `size` bytes of a block of one-byte `add d0,d1` --- */

#define NB_ICODE_SIZE 32768u

__asm__("\t.section .text.nb_icode,\"ax\"\n"
	"\t.global _nb_icode\n"
	"_nb_icode:\n"
	"\t.rept 32768\n"
	"\tadd d0,d1\n"
	"\t.endr\n"
	"\trets\n");
extern const char nb_icode[];

static void nb_icache_setup(struct nb_run *r)
{
	r->n_out = 1;
	r->out_bytes = 4;
	r->ops = r->p[0];
	r->ops2 = r->p[0];
}

static void nb_run_icache(struct nb_run *r)
{
	const void *entry = nb_icode + NB_ICODE_SIZE - r->p[0];
	register uint32_t x __asm__("d0") = 3;
	register uint32_t y __asm__("d1") = 7;

	/* calls stores the return address at (SP): keep it off this frame */
	__asm__ volatile("add -8,sp\n\tcalls (%[e])\n\tadd 8,sp"
			 : "+d"(x), "+d"(y)
			 : [e] "a"(entry)
			 : "mdr", "cc", "memory");
	*(uint32_t *)r->out = y;
}

static uint32_t nb_icache_expect(const struct nb_run *r, uint32_t i)
{
	(void)i;
	return 7u + 3u * r->p[0];
}

static const struct nb_var nb_icache_vars[] = {
	{ "code", NULL, nb_icache_prep, nb_run_icache, nb_icache_get },
};

const struct nb_fam nb_fam_icache = {
	"icache", { "size" }, "insn", "byte", nb_icache_setup,
	nb_icache_vars, 1, nb_icache_expect,
};
