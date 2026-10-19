/*
 * The tsplit family: one trained ternary 3x3 layer (nb3_layers, weights from the model) on pair
 * words, as generated code. One call of every variant is the whole layer.
 *
 *   c             the plain C reference over padded CHW planes (fam_conv.c)
 *   pair_tab_asm  the offset-table kernel (fam_conv.c, nb_kern.S)
 *   whole         the layer as one generated function, called once per group of 4 output pixels
 *   splitN        split into N blocks of output channels: all calls of a block, then the next
 *                 block, so each block's code is fetched once per layer instead of once per call
 *   split7k       blocks of at most ~7168 bytes of code
 *
 * Output channels are independent, so every variant is bit-identical to the reference.
 * Parameters: the conv family's (fam_conv.c nb_geo), the last one the model layer number.
 */
#include "nb3.h"

static int nb3_streq(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++;
		b++;
	}
	return *a == *b;
}

static const struct nb3_split *nb3_find_split(const struct nb_run *r)
{
	for (unsigned int i = 0; i < nb3_nsplit; i++) {
		const struct nb3_split *sp = &nb3_splits[i];
		unsigned int j = 0;

		while (j < NB_NP && sp->p[j] == r->p[j])
			j++;
		if (j == NB_NP && nb3_streq(sp->var, r->v->name))
			return sp;
	}
	return NULL;
}

static void nb3_setup(struct nb_run *r)
{
	const struct nb3_layer *l = &nb3_layers[r->p[NB_NZ]];
	struct nb_geo g;
	uint32_t nz = 0;

	nb_geo(r->p, &g);
	r->src = nb_alloc(g.cin * g.h * g.w);
	r->w = nb_alloc(g.cout * g.taps);
	r->b = nb_alloc(4 * g.cout);
	r->s = nb_alloc(g.cout);
	if (!r->s)
		return;
	nb_fill_u8(r->src, g.cin * g.h * g.w, nb_seed(r->p, NB_F_TSPLIT));
	for (uint32_t i = 0; i < g.cout * g.taps; i++) {
		r->w[i] = l->w[i];
		nz += l->w[i] != 0;
	}
	for (unsigned int o = 0; o < g.cout; o++) {
		r->b[o] = l->b[o];
		r->s[o] = l->s[o];
	}
	r->n_out = r->out_bytes = g.cout * g.ho * g.wo;
	r->ops = r->n_out * g.taps;
	r->ops2 = nz * g.ho * g.wo;
}

static int nb3_ok_split(const struct nb_run *r)
{
	return nb3_find_split(r) != NULL;
}

static void nb3_prep_split(struct nb_run *r)
{
	const struct nb3_split *sp = nb3_find_split(r);

	r->gen = sp->blk;
	r->nblk = sp->nblk;
	for (uint32_t i = 0; i < sp->nblk; i++) {
		r->blk[i] = (uint16_t)(sp->blk[i].end - sp->blk[i].beg);
		r->code += r->blk[i];
	}
	nb_pair_input(r);
}

static void NB_HOT nb3_run_split(struct nb_run *r)
{
	struct nb_geo g;
	const uint32_t *in = r->rin;
	uint8_t *out = r->out;

	nb_geo(r->p, &g);
	for (uint32_t b = 0; b < r->nblk; b++) {
		void (*fn)(const void *, void *) = r->gen[b].fn;

		for (unsigned int y = 0; y < g.ho; y++) {
			for (unsigned int x = 0; x < g.wo; x += 4)
				fn(in + y * g.wp + x, out + y * g.wo + x);
			if (r->tick)
				r->tick(r);
		}
	}
}

#define NB3_SPLIT(name) { name, nb3_ok_split, nb3_prep_split, nb3_run_split, nb_get_chw }

static const struct nb_var nb3_vars[] = {
	{ "c", NULL, nb_prep_chw, nb_conv_c, nb_get_chw },
	{ "pair_tab_asm", NULL, nb_prep_pair_tab, nb_conv_pair_tab_asm, nb_get_chw },
	NB3_SPLIT("whole"),
	NB3_SPLIT("split2"),
	NB3_SPLIT("split3"),
	NB3_SPLIT("split5"),
	NB3_SPLIT("split7"),
	NB3_SPLIT("split7k"),
};

const struct nb_fam nb_fam_tsplit = {
	"tsplit",
	{ "cin", "cout", "h", "w", "kh", "kw", "s", "d", "fl", "layer" },
	"mac",
	"nzw",	/* non-zero weight applications */
	nb3_setup,
	nb3_vars,
	sizeof(nb3_vars) / sizeof(nb3_vars[0]),
	NULL,
};
