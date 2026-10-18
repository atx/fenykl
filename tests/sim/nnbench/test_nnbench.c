/* nnbench's kernels (sdk/projects/nnbench) in the GDB simulator, with the plan of nnbench or
   nnbench2 (whichever plan.c is linked): every case whose reference fits NB_SIM_OPS, every variant
   against the reference, bit-exact over the whole output: random data, then all-255 and all-zero
   inputs for the smaller cases. The live FD frame is a fixed buffer filled the same way. Prints one
   FAIL line per mismatching variant and a summary; sim_exit_code is the number of failures (at
   most 250). */
#include "../../../sdk/projects/nnbench/nnbench.h"

#define NB_SIM_OPS 1400000u	/* reference MACs (or elements) per case */
#define NB_SIM_FIXED_OPS 120000u

/* Free simulated DRAM below the payload, for the 640 x 480 live frame. */
#define SIM_FRAME ((uint8_t *)0x6E700000u)

uint32_t sim_exit_code = 99;
volatile uint32_t mock_args[8];
extern uint32_t nb_fill_kind;

static void sim_write(const char *s, unsigned int n)
{
	__asm__ volatile("add -24,sp\n\t"
			 "mov %0,(12,sp)\n\t"
			 "mov %1,(16,sp)\n\t"
			 "mov 5,d0\n\t"
			 "mov 1,d1\n\t"
			 "syscall\n\t"
			 "add 24,sp"
			 :
			 : "a"(s), "a"(n)
			 : "d0", "d1", "memory");
}

static void sim_puts(const char *s)
{
	unsigned int n = 0;

	while (s[n])
		n++;
	sim_write(s, n);
}

static void sim_putu(uint32_t v)
{
	char b[12];
	int i = 11;

	b[i] = 0;
	do {
		b[--i] = (char)('0' + v % 10);
		v /= 10;
	} while (v);
	sim_puts(b + i);
}

const uint8_t *nb_live_frame(void)
{
	return SIM_FRAME;
}

void nb_live_purge(void)
{
}

struct sim_count {
	uint32_t runs, fails, skipped;
};

static void sim_case(const struct nb_stage *st, unsigned int ci, struct sim_count *c)
{
	struct nb_run r = { 0 };

	if (nb_case_begin(&r, &st->cases[ci]) < 0) {
		sim_puts("FAIL no memory ");
		sim_puts(st->name);
		sim_puts("\n");
		c->fails++;
		return;
	}
	for (unsigned int vi = 0; vi < r.f->nvar; vi++) {
		int b = nb_var_begin(&r, vi);
		uint32_t bad;

		if (!b)
			continue;
		bad = b < 0 ? 0xFFFFFFFFu : nb_var_check(&r);
		if (b > 0 && st->cases[ci].mode >= NB_LIVE && !r.live_tries)
			bad = 0xFFFFFFFEu;	/* the frame does not change here */
		c->runs++;
		if (!bad)
			continue;
		c->fails++;
		sim_puts("FAIL s=");
		sim_puts(st->name);
		sim_puts(" ci=");
		sim_putu(ci);
		sim_puts(" k=");
		sim_puts(r.f->name);
		sim_puts(" v=");
		sim_puts(r.v->name);
		sim_puts(" fill=");
		sim_putu(nb_fill_kind);
		sim_puts(" bad=");
		sim_putu(bad);
		sim_puts("\n");
	}
}

/* The shipped network is not linked (too big for the simulated payload; birddetect's own test
   covers it): its cases are skipped. */
const struct nb_fam nb_fam_bdnet = { "bdnet", { 0 }, "", "", NULL, NULL, 0, NULL };

/* Reference ops of a case, from its setup. */
static uint32_t sim_ops(const struct nb_case *k)
{
	struct nb_run r = { .k = k, .f = nb_fams[k->fam], .p = k->p };

	if (k->fam == NB_F_BDNET)
		return 0xFFFFFFFFu;
	nb_arena_reset();
	r.f->setup(&r);
	return r.ops;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	struct sim_count c = { 0 };

	(void)load_base;
	(void)image_size;
	for (nb_fill_kind = 0; nb_fill_kind < 3; nb_fill_kind++) {
		nb_fill_u8(SIM_FRAME, NB_FRAME_W * NB_FRAME_H, 0x51F0u);
		for (unsigned int s = 0; s < nb_nstages; s++) {
			const struct nb_stage *st = &nb_stages[s];

			for (unsigned int ci = 0; ci < st->ncases; ci++) {
				uint32_t ops = sim_ops(&st->cases[ci]);

				if (ops > (nb_fill_kind ? NB_SIM_FIXED_OPS : NB_SIM_OPS)) {
					c.skipped += !nb_fill_kind;
					continue;
				}
				sim_case(st, ci, &c);
			}
		}
	}
	sim_puts("nnbench sim: ");
	sim_putu(c.runs);
	sim_puts(" variant runs, ");
	sim_putu(c.fails);
	sim_puts(" FAIL, ");
	sim_putu(c.skipped);
	sim_puts(" cases skipped (too big)\n");
	sim_exit_code = c.fails > 250 ? 250 : c.fails;
}
