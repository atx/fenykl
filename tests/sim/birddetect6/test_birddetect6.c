/* birddetect6's network (sdk/projects/birddetect6/net.c, generated code) in the GDB simulator
   against the fenykl-nn test vectors (vectors.json, turned into C by tests/test_birddetect6_sim.py).
   Prints one FAIL line per mismatch and the per-layer count of matching vectors. plugin_main sets
   sim_exit_code to the failure bit mask (0 = pass):
     1   a layer's checksum differs on some vector
     2   head logits differ on some vector
     4   self-test checksums (bd6_selftest_sum) or post-processing (bd6_selftest_det) do not match
         the patterns
     16  the pause callback saw a stage out of order or a wrong number of bands
     32  the injection target is wrong
     64  the post-processing (score, fire, cell, point) differs on a network vector
     128 the post-processing differs on a synthetic head */
#include "../../../sdk/projects/birddetect6/net.h"
#include "../../../sdk/projects/birddetect6/inject.h"

#define SIM_LAYERS 11u

/* vectors.c (generated) */
extern const unsigned int sim_nvec;
extern const char *const sim_kind[];
extern const uint8_t *const sim_frame[];
extern const int sim_pattern[];		/* input bd6_net_pattern(this), -1 = sim_frame */
extern const uint32_t sim_sums[][SIM_LAYERS];
extern const int32_t sim_head[][BD6_LOGITS];
/* score, fire, row, col, x_q4, y_q4 per network vector, then per synthetic head */
extern const int32_t sim_post[][6];
extern const unsigned int sim_npost;
extern const int32_t sim_post_head[][BD6_LOGITS];
extern const int32_t sim_post_want[][6];

uint32_t sim_exit_code = 99;
volatile uint32_t mock_args[8];

static struct bd6_net net;

struct sim_run {
	unsigned int v;
	unsigned int calls, last;
	uint32_t fail;
	uint32_t sum[SIM_LAYERS];
};

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

static uint32_t mix(uint32_t sum, uint32_t v)
{
	return ((sum << 1) | (sum >> 31)) + v;
}

static uint32_t bytes_sum(const uint8_t *p, unsigned int n)
{
	uint32_t s = 0;

	for (unsigned int i = 0; i < n; i++)
		s = mix(s, p[i]);
	return s;
}

/* Checksum of c planes w x h stored as pair words of padded planes (pixel = low half). */
static uint32_t pairs_sum(const uint32_t *p, unsigned int c, unsigned int w, unsigned int h)
{
	uint32_t s = 0;

	for (unsigned int i = 0; i < c; i++)
		for (unsigned int y = 0; y < h; y++)
			for (unsigned int x = 0; x < w; x++)
				s = mix(s, p[(i * (h + 2) + y + 1) * (w + 2) + x + 1] & 0xFFFFu);
	return s;
}

/* The finished layer's output, while it is still in its buffer. */
static uint32_t layer_sum(unsigned int l)
{
	const struct bd6_net *n = &net;

	switch (l) {
	case 0:
		return bytes_sum(n->l0, sizeof(n->l0));
	case 1:
		return pairs_sum(n->p1, BD6_L0_C, BD6_L2_W, BD6_L2_H);
	case 2:
		return bytes_sum(n->l2, sizeof(n->l2));
	case 3:
		return pairs_sum(n->pa, BD6_L2_C, BD6_GRID_W, BD6_GRID_H);
	case 9:
		return bytes_sum(n->l9, sizeof(n->l9));
	case 10:
		return bd6_logit_sum(n->logit);
	default:
		return pairs_sum(l % 2 ? n->pa : n->pb, BD6_TRUNK_C, BD6_GRID_W, BD6_GRID_H);
	}
}

/* L0 36 rows, L1, L2 and L4..L9 an output row per block, L3, L10. */
static unsigned int sim_bands(void)
{
	unsigned int n = BD6_L0_H + 1 + bd6_l2.nblk * BD6_L2_H + 1 + 1;

	for (unsigned int l = 0; l < BD6_TRUNK; l++)
		n += bd6_trunk[l].nblk * BD6_GRID_H;
	return n;
}

static void on_pause(void *ctx, enum bd6_stage st, bool end)
{
	struct sim_run *r = ctx;

	if (st < r->last || st < BD6_ST_L0)
		r->fail = 1;
	r->last = st;
	r->calls++;
	if (end)
		r->sum[st - BD6_ST_L0] = layer_sum(st - BD6_ST_L0);
}

static bool post_ok(const int32_t *logit, const int32_t *want)
{
	struct bd6_det d, w = {
		.score = want[0],
		.fire = want[1] != 0,
		.row = (uint8_t)want[2],
		.col = (uint8_t)want[3],
		.x_q4 = (uint16_t)want[4],
		.y_q4 = (uint16_t)want[5],
	};

	bd6_post(logit, &d);
	return bd6_det_eq(&d, &w);
}

static uint32_t check_vectors(unsigned int *ok, unsigned int *post)
{
	uint32_t fail = 0;

	for (unsigned int v = 0; v < sim_nvec; v++) {
		struct sim_run r = { .v = v };
		bool head = true;

		bd6_net_init(&net);
		if (sim_pattern[v] >= 0)
			bd6_net_pattern(&net, sim_pattern[v]);
		else
			bd6_net_input(&net, sim_frame[v]);
		bd6_net_run(&net, on_pause, &r);
		if (r.fail || r.calls != sim_bands())
			fail |= 16;
		for (unsigned int l = 0; l < SIM_LAYERS; l++) {
			if (r.sum[l] == sim_sums[v][l]) {
				ok[l]++;
				continue;
			}
			fail |= 1;
			sim_puts("FAIL ");
			sim_puts(sim_kind[v]);
			sim_puts(" L");
			sim_putu(l);
			sim_puts("\n");
		}
		for (unsigned int i = 0; i < BD6_LOGITS; i++)
			head = head && net.logit[i] == sim_head[v][i];
		if (!head) {
			fail |= 2;
			sim_puts("FAIL head ");
			sim_puts(sim_kind[v]);
			sim_puts("\n");
		}
		if (post_ok(net.logit, sim_post[v])) {
			(*post)++;
			continue;
		}
		fail |= 64;
		sim_puts("FAIL post ");
		sim_puts(sim_kind[v]);
		sim_puts("\n");
	}
	return fail;
}

static uint32_t check_post(unsigned int *ok)
{
	uint32_t fail = 0;

	for (unsigned int i = 0; i < sim_npost; i++) {
		if (post_ok(sim_post_head[i], sim_post_want[i])) {
			(*ok)++;
			continue;
		}
		fail |= 128;
		sim_puts("FAIL synthetic head ");
		sim_putu(i);
		sim_puts("\n");
	}
	return fail;
}

static uint32_t check_selftest(void)
{
	uint32_t fail = 0;

	for (unsigned int p = 0; p < BD6_PATTERNS; p++) {
		struct bd6_det d;

		bd6_net_init(&net);
		bd6_net_pattern(&net, p);
		bd6_net_run(&net, 0, 0);
		bd6_post(net.logit, &d);
		if (bd6_logit_sum(net.logit) != bd6_selftest_sum[p] ||
		    !bd6_det_eq(&d, &bd6_selftest_det[p]))
			fail |= 4;
	}
	return fail;
}

static uint32_t check_inject(void)
{
	int16_t x = 0, y = 0;

	/* cell centres (1/16 input pixel (2 c + 1) * 64): corners clamped into the track window */
	if (bd6_inj_target(64, 64) != BD6_TGT(26, 34) ||
	    bd6_inj_target(1472, 1088) != BD6_TGT(613, 445) ||
	    bd6_inj_target(704, 576) != BD6_TGT(293, 240) || bd6_inj_target(1535, 0) != BD6_TGT(639, 34))
		return 32;
	/* the position as is, every time: no slew */
	if (!bd6_inj_pos(bd6_inj_target(64, 64), 0, &x, &y) || x != 26 || y != 34)
		return 32;
	if (!bd6_inj_pos(bd6_inj_target(1472, 1088), BD6_INJ_MAX_AGE_US, &x, &y) || x != 613 ||
	    y != 445)
		return 32;
	if (bd6_inj_pos(bd6_inj_target(704, 576), BD6_INJ_MAX_AGE_US + 1, &x, &y) ||
	    bd6_inj_pos(0, 0, &x, &y))
		return 32;
	return 0;
}

static void put_count(const char *what, unsigned int ok, unsigned int n)
{
	sim_puts(what);
	sim_putu(ok);
	sim_puts("/");
	sim_putu(n);
	sim_puts("\n");
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	unsigned int ok[SIM_LAYERS] = { 0 }, post = 0, synth = 0;
	uint32_t fail;

	(void)load_base;
	(void)image_size;
	fail = check_vectors(ok, &post) | check_post(&synth) | check_selftest() | check_inject();
	for (unsigned int l = 0; l < SIM_LAYERS; l++) {
		sim_puts("L");
		sim_putu(l);
		sim_puts(" ");
		sim_putu(ok[l]);
		sim_puts("/");
		sim_putu(sim_nvec);
		sim_puts(l + 1 < SIM_LAYERS ? ", " : "\n");
	}
	put_count("post ", post, sim_nvec);
	put_count("synthetic post ", synth, sim_npost);
	if (!(fail & 3))
		sim_puts("all layers bit-exact\n");
	if (!(fail & (64 | 128)))
		sim_puts("post-processing bit-exact\n");
	sim_exit_code = fail;
}
