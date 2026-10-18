/* The bird detector as shipped: birddetect's network code and model built into nnbench, run on its
   self-test patterns and checked against the model's self-test checksums. */
#include "nnbench.h"
#include "../birddetect/net.c"
#include "../birddetect/bd_model.c"

static uint32_t nb_tern_nz(const struct bd_tern *t)
{
	uint32_t n = 0;

	for (unsigned int o = 0; o < t->cout; o++)
		n += t->npos[o] + t->nneg[o];
	return n;
}

static void nb_bdnet_setup(struct nb_run *r)
{
	uint32_t nz = 0;

	r->aux = nb_alloc(sizeof(struct bd_net));
	r->n_out = 1;
	r->out_bytes = 4;
	r->ops = BD_L0_C * BD_L0_H * BD_L0_W * 9 + BD_L2_C * BD_L2_H * BD_L2_W * BD_L0_C * 9 +
		 BD_TRUNK_LAYERS * BD_TRUNK_C * BD_CELLS * BD_TRUNK_C * 9 + BD_CELLS * BD_TRUNK_C;
	for (unsigned int l = 0; l < BD_TRUNK_LAYERS; l++)
		nz += nb_tern_nz(&bd_trunk[l]);
	r->ops2 = BD_L0_C * BD_L0_H * BD_L0_W * 9 + nb_tern_nz(&bd_l2) * BD_L2_H * BD_L2_W +
		  nz * BD_CELLS + BD_CELLS * BD_TRUNK_C;
}

static void nb_prep_bdnet(struct nb_run *r)
{
	struct bd_net *n = r->aux;

	r->in = n;
	if (!n)
		return;
	bd_net_init(n);
	bd_net_pattern(n, r->p[0]);
}

static void nb_bdnet_pause(void *ctx, enum bd_group g)
{
	struct nb_run *r = ctx;

	(void)g;
	if (r->tick)
		r->tick(r);
}

static void nb_run_bdnet(struct nb_run *r)
{
	struct bd_net *n = r->aux;

	bd_net_run(n, nb_bdnet_pause, r);
	*(uint32_t *)r->out = bd_logit_sum(n->logit);
}

static uint32_t nb_get_bdnet(const struct nb_run *r, const void *buf, uint32_t i)
{
	(void)r;
	return ((const uint32_t *)buf)[i];
}

static uint32_t nb_bdnet_expect(const struct nb_run *r, uint32_t i)
{
	(void)i;
	return bd_selftest_sum[r->p[0]];
}

static const struct nb_var nb_bdnet_vars[] = {
	{ "c", NULL, nb_prep_bdnet, nb_run_bdnet, nb_get_bdnet },
};

const struct nb_fam nb_fam_bdnet = {
	"bdnet", { "pat" }, "mac", "weight", nb_bdnet_setup,
	nb_bdnet_vars, 1, nb_bdnet_expect,
};
