/* birddetect3's network (sdk/projects/birddetect3/net.c, generated code) in the GDB simulator
   against the fenykl-nn test vectors (vectors.json, turned into C by tests/test_birddetect3_sim.py).
   Prints one FAIL line per mismatch and the per-layer count of matching vectors. plugin_main sets
   sim_exit_code to the failure bit mask (0 = pass):
     1   a layer's checksum differs on some vector
     2   head logits differ on some vector
     4   self-test checksums (bd3_selftest_sum) do not match the patterns
     8   a self-test pattern's input differs from its test vector
     16  the pause callback saw a stage out of order or a wrong number of bands
     32  the injection target is wrong
     64  the post-processing (score, fire, cell, point) differs on a network vector
     128 the post-processing differs on a synthetic head */
#include "../../../sdk/projects/birddetect3/net.h"
#include "../../../sdk/projects/birddetect3/inject.h"

#define SIM_LAYERS 10u

/* vectors.c (generated) */
extern const unsigned int sim_nvec;
extern const char *const sim_kind[];
extern const uint8_t *const sim_frame[];
extern const int sim_pattern[];		/* enum bd3_pattern of the vector, -1 = none */
extern const uint32_t sim_sums[][SIM_LAYERS];
extern const int32_t sim_head[][BD3_CELLS];
/* score, fire, row, col, x_q4, y_q4 per network vector, then per synthetic head */
extern const int32_t sim_post[][6];
extern const unsigned int sim_npost;
extern const int32_t sim_post_head[][BD3_CELLS];
extern const int32_t sim_post_want[][6];

uint32_t sim_exit_code = 99;
volatile uint32_t mock_args[8];

static struct bd3_net net;

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
	const struct bd3_net *n = &net;

	switch (l) {
	case 0:
		return bytes_sum(n->l0, sizeof(n->l0));
	case 1:
		return pairs_sum(n->p1, BD3_L0_C, BD3_L2_W, BD3_L2_H);
	case 2:
		return bytes_sum(n->l2, sizeof(n->l2));
	case 3:
		return pairs_sum(n->pa, BD3_L2_C, BD3_GRID_W, BD3_GRID_H);
	case 8:
		return bytes_sum(n->l8, sizeof(n->l8));
	case 9:
		return bd3_logit_sum(n->logit);
	default:
		return pairs_sum(l % 2 ? n->pa : n->pb, BD3_TRUNK_C, BD3_GRID_W, BD3_GRID_H);
	}
}

/* L0 36 rows, L1, L2 and L4..L8 an output row per block, L3, L9. */
static unsigned int sim_bands(void)
{
	unsigned int n = BD3_L0_H + 1 + bd3_l2.nblk * BD3_L2_H + 1 + 1;

	for (unsigned int l = 0; l < BD3_TRUNK; l++)
		n += bd3_trunk[l].nblk * BD3_GRID_H;
	return n;
}

static void on_pause(void *ctx, enum bd3_stage st, bool end)
{
	struct sim_run *r = ctx;

	if (st < r->last || st < BD3_ST_L0)
		r->fail = 1;
	r->last = st;
	r->calls++;
	if (end)
		r->sum[st - BD3_ST_L0] = layer_sum(st - BD3_ST_L0);
}

static bool post_ok(const int32_t *logit, const int32_t *want)
{
	struct bd3_det d;

	bd3_post(logit, &d);
	return d.score == want[0] && d.fire == (want[1] != 0) && d.row == want[2] &&
	       d.col == want[3] && d.x_q4 == want[4] && d.y_q4 == want[5];
}

static uint32_t check_vectors(unsigned int *ok, unsigned int *post)
{
	uint32_t fail = 0;

	for (unsigned int v = 0; v < sim_nvec; v++) {
		struct sim_run r = { .v = v };
		bool head = true;

		bd3_net_init(&net);
		bd3_net_input(&net, sim_frame[v]);
		bd3_net_run(&net, on_pause, &r);
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
		for (unsigned int i = 0; i < BD3_CELLS; i++)
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

	for (unsigned int p = 0; p < BD3_PATTERNS; p++) {
		bd3_net_init(&net);
		bd3_net_pattern(&net, p);
		bd3_net_run(&net, 0, 0);
		if (bd3_logit_sum(net.logit) != bd3_selftest_sum[p])
			fail |= 4;
	}
	for (unsigned int v = 0; v < sim_nvec; v++) {
		if (sim_pattern[v] < 0)
			continue;
		bd3_net_init(&net);
		bd3_net_pattern(&net, sim_pattern[v]);
		for (unsigned int y = 0; y < BD3_IN_H; y++)
			for (unsigned int i = 0; i < BD3_FRAME_PITCH; i++)
				if (net.in[(y + 1) * BD3_PIN_PITCH + 4 + i] !=
				    sim_frame[v][y * BD3_FRAME_PITCH + i])
					fail |= 8;
	}
	return fail;
}

static uint32_t check_inject(void)
{
	int16_t x = 0, y = 0;

	/* cell centres (1/16 input pixel (2 c + 1) * 64): corners clamped into the track window */
	if (bd3_inj_target(64, 64) != BD3_TGT(26, 34) ||
	    bd3_inj_target(1472, 1088) != BD3_TGT(613, 445) ||
	    bd3_inj_target(704, 576) != BD3_TGT(293, 240) || bd3_inj_target(1535, 0) != BD3_TGT(639, 34))
		return 32;
	/* the position as is, every time: no slew */
	if (!bd3_inj_pos(bd3_inj_target(64, 64), 0, &x, &y) || x != 26 || y != 34)
		return 32;
	if (!bd3_inj_pos(bd3_inj_target(1472, 1088), BD3_INJ_MAX_AGE_US, &x, &y) || x != 613 ||
	    y != 445)
		return 32;
	if (bd3_inj_pos(bd3_inj_target(704, 576), BD3_INJ_MAX_AGE_US + 1, &x, &y) ||
	    bd3_inj_pos(0, 0, &x, &y))
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
