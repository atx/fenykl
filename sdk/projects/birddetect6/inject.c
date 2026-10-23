/* The injected detection (inject.h). Pure code: runs in the control task and the FD task's hook. */
#include "inject.h"
#include "net.h"

#define BD6_CELL_PX 8			/* input pixels per grid cell */

void bd6_post(const int32_t *logit, struct bd6_det *d)
{
	const int32_t *l = logit, *pres = logit + BD6_CELLS;
	unsigned int top = 0, cy, cx;
	int32_t den = 0, nx = 0, ny = 0, score = 0;

	for (unsigned int i = 0; i < BD6_CELLS; i++)
		score += pres[i];
	for (unsigned int i = 1; i < BD6_CELLS; i++)
		if (l[i] > l[top])
			top = i;
	cy = top / BD6_GRID_W;
	cx = top % BD6_GRID_W;
	for (int y = (int)cy - 1; y <= (int)cy + 1; y++) {
		for (int x = (int)cx - 1; x <= (int)cx + 1; x++) {
			uint32_t b;
			int32_t w;

			if (y < 0 || y >= (int)BD6_GRID_H || x < 0 || x >= (int)BD6_GRID_W)
				continue;
			b = (uint32_t)(l[top] - l[y * BD6_GRID_W + x]) >> bd6_centroid_k;
			w = bd6_centroid_table[b < BD6_CENTROID_N ? b : BD6_CENTROID_N - 1];
			den += w;
			nx += w * (2 * x + 1);
			ny += w * (2 * y + 1);
		}
	}
	d->score = score;
	d->fire = score >= bd6_threshold;
	d->row = (uint8_t)cy;
	d->col = (uint8_t)cx;
	d->x_q4 = (uint16_t)((nx * BD6_CELL_PX * 8 + den / 2) / den);
	d->y_q4 = (uint16_t)((ny * BD6_CELL_PX * 8 + den / 2) / den);
}

bool bd6_det_eq(const struct bd6_det *a, const struct bd6_det *b)
{
	return a->score == b->score && a->fire == b->fire && a->row == b->row && a->col == b->col &&
	       a->x_q4 == b->x_q4 && a->y_q4 == b->y_q4;
}

/* The input covers the 640x480 field: 1/16 input pixel = 640 / (96 * 16) = 5 / 12 FD pixel. */
uint32_t bd6_inj_target(unsigned int x_q4, unsigned int y_q4)
{
	int y = (int)(y_q4 * 5 / 12);

	if (y < BD6_INJ_Y_MIN)
		y = BD6_INJ_Y_MIN;
	if (y > BD6_INJ_Y_MAX)
		y = BD6_INJ_Y_MAX;
	return BD6_TGT(x_q4 * 5 / 12, y);
}

bool bd6_inj_pos(uint32_t target, uint32_t age_us, int16_t *x, int16_t *y)
{
	if (!(target & BD6_TGT_VALID) || age_us > BD6_INJ_MAX_AGE_US)
		return false;
	*x = (int16_t)(target & 0xFFFFu);
	*y = (int16_t)((target >> 16) & 0x7FFFu);
	return true;
}
