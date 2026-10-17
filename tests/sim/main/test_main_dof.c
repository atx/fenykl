/* The main plugin's depth of field (sdk/projects/main/main_dof.c) in the GDB simulator, with
   ../hooks/sim_start.S. plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
     1  a fixed case's near or far limit is off ExifTool's Composite:DOF (exact N = 2^(Av/512),
        c = 15 um) by more than 0.2 % or 1 mm
     2  a fixed case's far limit is infinite in one and finite in the other
     4  N x 1000 is off 1000 * 2^(Av/512) by more than 0.05 % (or 1)
     8  sweep: near <= s <= far or depth = far - near (at least 1) does not hold
     16 sweep: the depth shrinks by more than 1 mm when stopping down by 1/2 EV
     32 sweep: the depth grows by more than 1 mm when zooming in
     64 invalid input accepted (f 0 or too long, s <= f, Av too large, c 0) */
#include "../../../sdk/projects/main/main_dof.h"

#define T_INF MAIN_DOF_INF

uint32_t sim_exit_code = 99;

struct t_case {
	uint32_t s, f, av;
	uint32_t near, far;	/* ExifTool's formula in floating point, rounded (Python) */
};

static const struct t_case t_cases[] = {
	/* kit lens 14 and 42 mm at its distance steps, f/3.5 .. f/5.6 */
	{ 1770, 14, 925, 1204, 3340 },
	{ 2100, 14, 925, 1347, 4756 },
	{ 2350, 14, 1272, 1175, T_INF },
	{ 2920, 14, 1272, 1301, T_INF },
	{ 5380, 42, 1272, 4290, 7212 },
	{ 16900, 42, 1272, 9378, 85430 },
	{ 250, 42, 1272, 248, 252 },
	{ 500, 25, 512, 489, 512 },
	{ 1000, 17, 768, 874, 1169 },
	{ 3000, 300, 1536, 2989, 3011 },
	{ 100000, 150, 2048, 48425, T_INF },
	{ 1400, 45, 0, 1386, 1414 },
	{ 350, 60, 1024, 348, 352 },
	{ 200, 12, 4096, 33, T_INF },
	{ 1000000, 14, 768, 4599, T_INF },
	{ 4000, 1000, 0, 4000, 4000 },
	{ 250, 7, 3072, 43, T_INF },
	/* focused at infinity: near = f^2 / (N c) */
	{ T_INF, 14, 1272, 2335, T_INF },
	{ T_INF, 42, 512, 58800, T_INF },
};

/* Av -> 1000 * 2^(Av/512) (Python) */
static const uint32_t t_n1000[][2] = {
	{ 0, 1000 }, { 43, 1060 }, { 256, 1414 }, { 925, 3498 }, { 1272, 5596 },
	{ 1300, 5812 }, { 2047, 15978 }, { 2560, 32000 }, { 3599, 130626 }, { 4096, 256000 },
};

static int t_near(uint32_t got, uint32_t want)
{
	uint32_t diff = got > want ? got - want : want - got;

	return diff <= 1 || (uint64_t)diff * 500 <= want;
}

static int t_fixed(void)
{
	struct main_dof d;
	int fail = 0;

	for (unsigned int i = 0; i < sizeof(t_cases) / sizeof(t_cases[0]); i++) {
		const struct t_case *c = &t_cases[i];

		if (!main_dof_calc(&d, c->s, c->f, c->av, 15)) {
			fail |= 1;
			continue;
		}
		if (!t_near(d.near, c->near))
			fail |= 1;
		if ((d.far == T_INF) != (c->far == T_INF))
			fail |= 2;
		else if (c->far != T_INF && !t_near(d.far, c->far))
			fail |= 1;
	}
	for (unsigned int i = 0; i < sizeof(t_n1000) / sizeof(t_n1000[0]); i++) {
		uint32_t got = main_dof_n1000(t_n1000[i][0]), want = t_n1000[i][1];
		uint32_t diff = got > want ? got - want : want - got;

		if (diff > 1 && diff * 2000 > want)
			fail |= 4;
	}
	return fail;
}

static int t_invalid(void)
{
	struct main_dof d;

	return main_dof_calc(&d, 1000, 0, 512, 15) || main_dof_calc(&d, 5000, 1001, 512, 15) ||
	       main_dof_calc(&d, 42, 42, 512, 15) || main_dof_calc(&d, 1000, 42, 4097, 15) ||
	       main_dof_calc(&d, 1000, 42, 512, 0) ? 64 : 0;
}

static int t_shape(const struct main_dof *d, uint32_t s)
{
	if (d->near > s || (d->far != T_INF && d->far < s))
		return 8;
	if (d->far == T_INF)
		return d->depth == T_INF ? 0 : 8;
	if (d->depth != (d->far > d->near ? d->far - d->near : 1))
		return 8;
	return 0;
}

/* depth b is not smaller than a, 1 mm of rounding allowed */
static int t_grows(uint32_t a, uint32_t b)
{
	return b == T_INF || (a != T_INF && b + 1 >= a);
}

static int t_sweep(void)
{
	static const uint32_t fs[] = { 7, 14, 25, 42, 75, 150, 300 };
	struct main_dof d, prev;
	int fail = 0;

	for (uint32_t s = 200; s < 2000000; s += s / 4) {
		uint32_t zoom_prev = 0;

		for (unsigned int i = 0; i < sizeof(fs) / sizeof(fs[0]); i++) {
			bool have = false;

			for (uint32_t av = 0; av <= MAIN_DOF_AV_MAX; av += 128) {
				if (!main_dof_calc(&d, s, fs[i], av, 15))
					continue;
				fail |= t_shape(&d, s);
				if (have && !t_grows(prev.depth, d.depth))
					fail |= 16;
				prev = d;
				have = true;
			}
			/* f/4 at each focal length */
			if (main_dof_calc(&d, s, fs[i], 1024, 15)) {
				if (zoom_prev && !t_grows(d.depth, zoom_prev))
					fail |= 32;
				zoom_prev = d.depth;
			}
		}
	}
	return fail;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = t_fixed() | t_invalid() | t_sweep();
}
