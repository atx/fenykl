/*
 * main: battery charge model (main_batt.h). A median over MAIN_BATT_MEDIAN samples, an
 * exponential filter, the generic open-circuit curve and a 1 % hysteresis on the shown value.
 */
#include "main_batt.h"

struct main_batt_point {
	uint16_t mv;		/* open-circuit cell voltage */
	uint16_t permille;
};

/*
 * Rest voltage of one Li-ion cell (LiCoO2 or NMC on graphite) over its charge, rounded to the
 * shape of the generic curve that battery-management application notes publish: about 4.15 V at
 * rest after a full charge, a flat plateau from 3.7 to 3.9 V over most of the charge and a steep
 * knee below 3.6 V. Generic values, no specific cell's data. 0 % is 3.2 V (6.4 V per pack at rest):
 * under the camera's load the pack is then at the 6.2-6.4 V where the camera shuts down.
 */
static const struct main_batt_point main_batt_curve[] = {
	{ 3200, 0 },
	{ 3500, 50 },
	{ 3620, 100 },
	{ 3700, 200 },
	{ 3740, 300 },
	{ 3780, 400 },
	{ 3820, 500 },
	{ 3870, 600 },
	{ 3920, 700 },
	{ 3980, 800 },
	{ 4060, 900 },
	{ 4150, 1000 },
};

#define MAIN_BATT_POINTS (sizeof(main_batt_curve) / sizeof(main_batt_curve[0]))

uint32_t main_batt_cell_mv(uint32_t ema)
{
	uint32_t div = (1000u << MAIN_BATT_EMA_FRAC) * MAIN_BATT_CELLS;

	return (ema * MAIN_BATT_UV_PER_COUNT + div / 2) / div;
}

uint32_t main_batt_soc(uint32_t cell_mv)
{
	const struct main_batt_point *lo, *hi;
	unsigned int i;

	if (cell_mv <= main_batt_curve[0].mv)
		return main_batt_curve[0].permille;
	for (i = 1; i < MAIN_BATT_POINTS; i++)
		if (cell_mv < main_batt_curve[i].mv)
			break;
	if (i == MAIN_BATT_POINTS)
		return main_batt_curve[MAIN_BATT_POINTS - 1].permille;
	lo = &main_batt_curve[i - 1];
	hi = &main_batt_curve[i];
	return lo->permille + (cell_mv - lo->mv) * (uint32_t)(hi->permille - lo->permille) /
	       (uint32_t)(hi->mv - lo->mv);
}

void main_batt_reset(struct main_batt *b)
{
	b->n = 0;
	b->next = 0;
	b->shown = MAIN_BATT_NONE;
	b->ema = 0;
}

/* Median of the samples in win (insertion sort of a copy). */
static uint32_t main_batt_median(const struct main_batt *b)
{
	uint16_t s[MAIN_BATT_MEDIAN];

	for (unsigned int i = 0; i < b->n; i++) {
		unsigned int j = i;

		for (; j > 0 && s[j - 1] > b->win[i]; j--)
			s[j] = s[j - 1];
		s[j] = b->win[i];
	}
	return s[b->n / 2];
}

unsigned int main_batt_add(struct main_batt *b, uint32_t counts)
{
	uint32_t m, permille, at;

	b->win[b->next] = (uint16_t)(counts + MAIN_BATT_LV_SAG);
	b->next = (uint8_t)((b->next + 1) % MAIN_BATT_MEDIAN);
	if (b->n < MAIN_BATT_MEDIAN)
		b->n++;
	m = main_batt_median(b) << MAIN_BATT_EMA_FRAC;
	if (b->shown == MAIN_BATT_NONE)
		b->ema = m;
	else if (m >= b->ema)
		b->ema += (m - b->ema) >> MAIN_BATT_EMA_SHIFT;
	else
		b->ema -= (b->ema - m) >> MAIN_BATT_EMA_SHIFT;
	permille = main_batt_soc(main_batt_cell_mv(b->ema));
	at = b->shown * 10u;
	if (b->shown == MAIN_BATT_NONE || permille > at + MAIN_BATT_HYST ||
	    permille + MAIN_BATT_HYST < at)
		b->shown = (uint8_t)((permille + 5) / 10);
	return b->shown;
}
