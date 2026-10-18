/* The case runner and the arena (the plan, the families and the arena buffer: plan.c). */
#include "nnbench.h"

/* --- arena --- */

struct nb_arena {
	uint32_t used;
	uint32_t fail;
};

static struct nb_arena nb_ar;

/* Input fill for the simulator test's fixed vectors: 0 random, 1 all 255, 2 all 0. */
uint32_t nb_fill_kind;

void nb_arena_reset(void)
{
	nb_ar = (struct nb_arena){ 0 };
}

uint32_t nb_arena_mark(void)
{
	return nb_ar.used;
}

void nb_arena_release(uint32_t mark)
{
	nb_ar.used = mark;
}

void *nb_alloc(uint32_t bytes)
{
	uint32_t at = nb_ar.used;

	if (bytes > nb_arena_size - at) {
		nb_ar.fail = 1;
		return NULL;
	}
	nb_ar.used = (at + bytes + 63) & ~63u;
	return nb_arena_buf + at;
}

uint32_t nb_rand(uint32_t *x)
{
	*x = *x * 1664525u + 1013904223u;
	return *x >> 8;
}

void nb_fill_u8(uint8_t *p, uint32_t n, uint32_t seed)
{
	for (uint32_t i = 0; i < n; i++)
		p[i] = nb_fill_kind == 1 ? 255 : nb_fill_kind == 2 ? 0 : (uint8_t)nb_rand(&seed);
}

uint32_t nb_seed(const uint16_t *p, unsigned int fam)
{
	uint32_t x = 0x9E3779B9u ^ fam;

	for (unsigned int i = 0; i < NB_NP; i++)
		x = (x ^ p[i]) * 0x01000193u;
	return x;
}

uint32_t nb_min(const uint32_t *v, unsigned int n)
{
	uint32_t m = v[0];

	for (unsigned int i = 1; i < n; i++)
		m = v[i] < m ? v[i] : m;
	return m;
}

uint32_t nb_median(uint32_t *v, unsigned int n)
{
	for (unsigned int i = 1; i < n; i++)
		for (unsigned int j = i; j > 0 && v[j - 1] > v[j]; j--) {
			uint32_t t = v[j];

			v[j] = v[j - 1];
			v[j - 1] = t;
		}
	return v[n / 2];
}

/* --- the runner --- */

int nb_case_begin(struct nb_run *r, const struct nb_case *k)
{
	nb_tick_fn *tick = r->tick;
	void *tick_ctx = r->tick_ctx;

	*r = (struct nb_run){ .k = k, .f = nb_fams[k->fam], .p = k->p, .tick = tick,
			      .tick_ctx = tick_ctx };
	nb_arena_reset();
	if (k->mode >= NB_LIVE) {
		r->live_addr = (uint32_t)nb_live_frame();
		if (!r->live_addr)
			return -2;
	}
	r->f->setup(r);
	if (nb_ar.fail)
		return -1;
	if (!r->f->expect) {
		r->v = &r->f->var[0];
		r->v->prep(r);
		r->rin = r->in;
		r->out = r->ref = nb_alloc(r->out_bytes);
		if (nb_ar.fail)
			return -1;
		r->v->run(r);
		r->ref_aux = r->aux;
	}
	r->mark = nb_arena_mark();
	return 0;
}

int nb_var_begin(struct nb_run *r, unsigned int vi)
{
	nb_arena_release(r->mark);
	r->v = &r->f->var[vi];
	r->gen = NULL;
	r->aux = NULL;
	r->code = 0;
	if (r->v->ok && !r->v->ok(r))
		return 0;
	r->v->prep(r);
	r->rin = r->in;
	r->out = nb_alloc(r->out_bytes);
	return nb_ar.fail ? -1 : 1;
}

uint32_t nb_hash_words(const uint32_t *p, uint32_t n)
{
	uint32_t h = 0x811C9DC5u;

	for (uint32_t i = 0; i < n; i++)
		h = (uint32_t)nb_mul((int32_t)(h ^ p[i]), 0x01000193);
	return h;
}

static uint32_t nb_frame_hash(const uint8_t *f)
{
	return nb_hash_words((const uint32_t *)f, NB_FRAME_W * NB_FRAME_H / 4);
}

static void nb_live_tick(struct nb_run *r)
{
	if (r->tick)
		r->tick(r);
}

/* The reference (with its own tables) then the variant on the newest frame; 1 if the frame's
   hash stayed the same around both. Ticks between the steps (each a few ms): CPU1 must block. */
static int nb_live_pair(struct nb_run *r)
{
	const uint8_t *f = nb_live_frame();
	void *aux = r->aux, *out = r->out;
	uint32_t h;

	if (!f)
		return 0;
	nb_live_purge();
	h = nb_frame_hash(f);
	nb_live_tick(r);
	r->rin = f;
	r->aux = r->ref_aux;
	r->out = r->ref;
	r->f->var[0].run(r);
	nb_live_tick(r);
	r->aux = aux;
	r->out = out;
	r->v->run(r);
	nb_live_tick(r);
	nb_live_purge();
	r->live_addr = (uint32_t)f;
	r->live_hash = h;
	return nb_frame_hash(f) == h;
}

uint32_t nb_var_check(struct nb_run *r)
{
	const struct nb_var *ref = &r->f->var[0];
	uint32_t bad = 0;

	for (uint32_t i = 0; i < r->out_bytes; i++)
		((uint8_t *)r->out)[i] = 0xA5;
	if (r->k->mode >= NB_LIVE) {
		for (r->live_tries = 1; !nb_live_pair(r); r->live_tries++)
			if (r->live_tries == NB_LIVE_TRIES) {
				r->live_tries = 0;
				return 0;
			}
	} else {
		r->v->run(r);
	}
	for (uint32_t i = 0; i < r->n_out; i++) {
		uint32_t want = r->f->expect ? r->f->expect(r, i) : ref->get(r, r->ref, i);

		bad += r->v->get(r, r->out, i) != want;
	}
	return bad;
}

const struct nb_gen *nb_find_gen(const struct nb_run *r, unsigned int px)
{
	for (unsigned int i = 0; i < nb_ngen; i++) {
		const struct nb_gen *g = &nb_gens[i];
		unsigned int j = 0;

		if (g->fam != r->k->fam || g->px != px)
			continue;
		while (j < NB_NP && g->p[j] == r->p[j])
			j++;
		if (j == NB_NP)
			return g;
	}
	return NULL;
}
