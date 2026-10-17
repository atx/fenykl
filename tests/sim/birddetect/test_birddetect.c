/* birddetect's network (sdk/projects/birddetect/net.c) in the GDB simulator against the fenykl-nn
   test vectors (vectors.json, turned into C by tests/test_birddetect_sim.py). plugin_main sets sim_exit_code
   to the failure bit mask (0 = pass; the simulator's exit status keeps 8 bits):
     1   a layer's checksum differs on some vector
     2   head logits differ on some vector
     4   self-test checksums (bd_selftest_sum) do not match the patterns
     8   point sampling does not take the handoff source pixels
     16  the pause callback saw a group out of order or a wrong number of bands
     32  the injection target or glide (inject.c) is wrong */
#include "../../../sdk/projects/birddetect/net.h"
#include "../../../sdk/projects/birddetect/inject.h"

/* vectors.c (generated) */
extern const unsigned int sim_nvec;
extern const char *const sim_kind[];
extern const uint8_t *const sim_input[];	/* NULL: structured, regenerated here */
extern const uint32_t sim_sums[][10];
extern const int32_t sim_head[][BD_CELLS];
extern const uint16_t sim_xs[BD_IN_W], sim_ys[BD_IN_H];

uint32_t sim_exit_code = 99;
volatile uint32_t mock_args[8];

static struct bd_net net;
/* simulated DRAM above the payload and the mocks: too big for the payload region */
static uint8_t *const frame = (uint8_t *)0x6EC00000u;

struct sim_pause {
	unsigned int calls, last;
	uint32_t fail;
};

static uint32_t mix(uint32_t sum, uint32_t v)
{
	return ((sum << 1) | (sum >> 31)) + v;
}

/* Checksum of c planes w x h, stored with row stride sw, plane stride sp, from p. */
static uint32_t plane_sum(const uint8_t *p, unsigned int c, unsigned int w, unsigned int h,
			  unsigned int sw, unsigned int sp)
{
	uint32_t s = 0;

	for (unsigned int i = 0; i < c; i++)
		for (unsigned int y = 0; y < h; y++)
			for (unsigned int x = 0; x < w; x++)
				s = mix(s, p[i * sp + y * sw + x]);
	return s;
}

static bool streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static void fill_input(unsigned int v)
{
	const char *k = sim_kind[v];

	if (sim_input[v]) {
		for (unsigned int y = 0; y < BD_IN_H; y++)
			for (unsigned int x = 0; x < BD_IN_W; x++)
				*bd_net_px(&net, x, y) = sim_input[v][y * BD_IN_W + x];
	} else if (streq(k, "hgradient")) {
		bd_net_pattern(&net, BD_PAT_HGRAD);
	} else if (streq(k, "vgradient")) {
		bd_net_pattern(&net, BD_PAT_VGRAD);
	} else if (streq(k, "checker")) {
		bd_net_pattern(&net, BD_PAT_CHECKER);
	} else {
		uint8_t fill = streq(k, "full") ? 255 : 0;

		for (unsigned int y = 0; y < BD_IN_H; y++)
			for (unsigned int x = 0; x < BD_IN_W; x++)
				*bd_net_px(&net, x, y) = fill;
	}
}

static void on_pause(void *ctx, enum bd_group g)
{
	struct sim_pause *p = ctx;

	if (g < p->last)
		p->fail = 1;
	p->last = g;
	p->calls++;
}

static uint32_t layer_sum(unsigned int l)
{
	const struct bd_net *n = &net;

	switch (l) {
	case 0:
		return plane_sum(n->l0, BD_L0_C, BD_L0_W, BD_L0_H, BD_L0_W, BD_L0_W * BD_L0_H);
	case 1:
		return plane_sum(n->l1 + BD_P1_W + 1, BD_L0_C, BD_L0_W / 2, BD_L0_H / 2, BD_P1_W,
				 BD_P1_W * BD_P1_H);
	case 2:
		return plane_sum(n->l2, BD_L2_C, BD_L2_W, BD_L2_H, BD_L2_W, BD_L2_W * BD_L2_H);
	case 3:
		return plane_sum(n->l3 + BD_P3_W + 1, BD_L2_C, BD_GRID_W, BD_GRID_H, BD_P3_W,
				 BD_P3_PLANE);
	case 9:
		return bd_logit_sum(n->logit);
	default:
		return plane_sum(n->trunk[l - 4] + BD_P3_W + 1, BD_TRUNK_C, BD_GRID_W, BD_GRID_H,
				 BD_P3_W, BD_P3_PLANE);
	}
}

static uint32_t check_vectors(void)
{
	uint32_t fail = 0;

	for (unsigned int v = 0; v < sim_nvec; v++) {
		struct sim_pause p = { 0 };

		bd_net_init(&net);
		fill_input(v);
		bd_net_run(&net, on_pause, &p);
		/* 9 stem bands, 9 L2 bands, 5 x 9 trunk rows and the head */
		if (p.fail || p.calls != 9 + 9 + 45 + 1)
			fail |= 16;
		for (unsigned int l = 0; l < 10; l++) {
			if (layer_sum(l) != sim_sums[v][l])
				fail |= 1;
		}
		for (unsigned int i = 0; i < BD_CELLS; i++)
			if (net.logit[i] != sim_head[v][i])
				fail |= 2;
	}
	return fail;
}

static uint32_t check_selftest(void)
{
	for (unsigned int p = 0; p < BD_PATTERNS; p++) {
		bd_net_init(&net);
		bd_net_pattern(&net, p);
		bd_net_run(&net, 0, 0);
		if (bd_logit_sum(net.logit) != bd_selftest_sum[p])
			return 4;
	}
	return 0;
}

static uint8_t frame_px(unsigned int x, unsigned int y)
{
	return (uint8_t)(x * 7 + y * 13 + (x >> 3));
}

static uint32_t check_sample(void)
{
	for (unsigned int y = 0; y < BD_SRC_H; y++)
		for (unsigned int x = 0; x < BD_SRC_W; x++)
			frame[y * BD_SRC_W + x] = frame_px(x, y);
	bd_net_init(&net);
	bd_net_sample(&net, frame);
	for (unsigned int y = 0; y < BD_IN_H; y++)
		for (unsigned int x = 0; x < BD_IN_W; x++)
			if (*bd_net_px(&net, x, y) != frame_px(sim_xs[x], sim_ys[y]))
				return 8;
	return 0;
}

static uint32_t check_inject(void)
{
	struct bd_inj s = { 0 };
	uint32_t far = bd_inj_target(11, 8);
	unsigned int steps = 0;

	if (bd_inj_target(0, 0) != BD_TGT(26, 34) || far != BD_TGT(613, 445) ||
	    bd_inj_target(5, 4) != BD_TGT(293, 240))
		return 32;
	if (!bd_inj_step(&s, bd_inj_target(0, 0), 0) || s.cx != 26 || s.cy != 34)
		return 32;
	/* glides at most BD_INJ_STEP per axis and arrives */
	while (s.cx != 613 || s.cy != 445) {
		int16_t x = s.cx, y = s.cy;

		if (!bd_inj_step(&s, far, BD_INJ_MAX_AGE_US) || s.cx - x > BD_INJ_STEP ||
		    s.cy - y > BD_INJ_STEP || s.cx < x || s.cy < y || ++steps > 100)
			return 32;
	}
	if (bd_inj_step(&s, far, BD_INJ_MAX_AGE_US + 1) || s.valid)
		return 32;
	/* after a gap it jumps; no target clears */
	if (!bd_inj_step(&s, bd_inj_target(5, 4), 0) || s.cx != 293 || s.cy != 240)
		return 32;
	if (bd_inj_step(&s, 0, 0) || s.valid)
		return 32;
	return 0;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = check_vectors() | check_selftest() | check_sample() |
			check_inject();
}
