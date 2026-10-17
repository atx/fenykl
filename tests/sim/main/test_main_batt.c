/* The main plugin's battery model (sdk/projects/main/main_batt.c) in the GDB simulator, with
   ../hooks/sim_start.S. plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
     1  a curve point, a midpoint between two points or a clamped voltage gives the wrong charge
     2  sweep: the charge decreases while the voltage rises, or leaves 0..1000
     4  counts to cell mV: wrong scale or rounding
     8  steady noisy live-view reads (+-3 counts) change the shown % after it settled
     16 two AF dips (5 counts lower) change the shown %
     32 a slow discharge raises the shown %, or steps it by more than 1
     64 the first sample is not shown at once, or a reset keeps the old state */
#include "../../../sdk/projects/main/main_batt.h"

uint32_t sim_exit_code = 99;

struct t_case {
	uint32_t mv, permille;
};

static const struct t_case t_soc[] = {
	{ 0, 0 }, { 3000, 0 }, { 3199, 0 }, { 3200, 0 }, { 3350, 25 }, { 3500, 50 },
	{ 3620, 100 }, { 3660, 150 }, { 3700, 200 }, { 3820, 500 }, { 3845, 550 },
	{ 3980, 800 }, { 4060, 900 }, { 4105, 950 }, { 4149, 998 }, { 4150, 1000 },
	{ 4200, 1000 }, { 0xFFFFFFFFu, 1000 },
};

/* Raw live-view counts: the model adds MAIN_BATT_LV_SAG */
static uint32_t t_counts(uint32_t open_circuit)
{
	return open_circuit - MAIN_BATT_LV_SAG;
}

static unsigned int t_curve(void)
{
	unsigned int fail = 0;
	uint32_t last = 0;

	for (unsigned int i = 0; i < sizeof(t_soc) / sizeof(t_soc[0]); i++)
		if (main_batt_soc(t_soc[i].mv) != t_soc[i].permille)
			fail |= 1;
	for (uint32_t mv = 2800; mv <= 4400; mv++) {
		uint32_t p = main_batt_soc(mv);

		if (p < last || p > 1000)
			fail |= 2;
		last = p;
	}
	return fail;
}

static unsigned int t_scale(void)
{
	/* 819 counts = 8001.6 mV per pack */
	if (main_batt_cell_mv(819u << MAIN_BATT_EMA_FRAC) != 4001)
		return 4;
	if (main_batt_cell_mv(0) != 0 || main_batt_cell_mv(1) != 0)
		return 4;
	return 0;
}

static unsigned int t_noise(void)
{
	static const int jitter[] = { 0, 3, -2, 1, -3, 2, 0, -1, 3, -3, 1, 2, -2 };
	struct main_batt b;
	unsigned int fail = 0, first, shown;

	main_batt_reset(&b);
	/* 3.80 V per cell: the plateau, where a count is about 1 % */
	first = main_batt_add(&b, t_counts(778));
	if (first != (main_batt_soc(main_batt_cell_mv(778u << MAIN_BATT_EMA_FRAC)) + 5) / 10)
		fail |= 64;
	for (unsigned int i = 0; i < 40; i++)
		main_batt_add(&b, (uint32_t)((int)t_counts(778) + jitter[i % 13]));
	shown = b.shown;
	for (unsigned int i = 0; i < 400; i++)
		if (main_batt_add(&b, (uint32_t)((int)t_counts(778) + jitter[i % 13])) != shown)
			fail |= 8;
	for (unsigned int i = 0; i < 20; i++) {
		uint32_t v = t_counts(778) - (i % 10 < 2 ? 5 : 0);

		if (main_batt_add(&b, v) != shown)
			fail |= 16;
	}
	main_batt_reset(&b);
	if (b.n || b.shown != MAIN_BATT_NONE || b.ema)
		fail |= 64;
	return fail;
}

static unsigned int t_discharge(void)
{
	struct main_batt b;
	unsigned int fail = 0, last;

	main_batt_reset(&b);
	last = main_batt_add(&b, t_counts(840));
	/* one count per minute, from 8.2 V to 6.4 V */
	for (uint32_t oc = 840; oc >= 655; oc--)
		for (unsigned int s = 0; s < 60; s++) {
			unsigned int p = main_batt_add(&b, t_counts(oc) + (s & 1));

			if (p > last || last - p > 1)
				fail |= 32;
			last = p;
		}
	if (last > 1)
		fail |= 32;
	return fail;
}

void plugin_main(void)
{
	sim_exit_code = t_curve() | t_scale() | t_noise() | t_discharge();
}
