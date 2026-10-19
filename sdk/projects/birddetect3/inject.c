/* The injected detection (inject.h). Pure code: runs in the control task and the FD task's hook. */
#include "inject.h"
#include "net.h"

#define BD3_CELL_PX 8			/* input pixels per grid cell */

void bd3_post(const int32_t *l, struct bd3_det *d)
{
	unsigned int top = 0, cy, cx;
	int32_t den = 0, nx = 0, ny = 0;

	for (unsigned int i = 1; i < BD3_CELLS; i++)
		if (l[i] > l[top])
			top = i;
	cy = top / BD3_GRID_W;
	cx = top % BD3_GRID_W;
	for (int y = (int)cy - 1; y <= (int)cy + 1; y++) {
		for (int x = (int)cx - 1; x <= (int)cx + 1; x++) {
			uint32_t b;
			int32_t w;

			if (y < 0 || y >= (int)BD3_GRID_H || x < 0 || x >= (int)BD3_GRID_W)
				continue;
			b = (uint32_t)(l[top] - l[y * BD3_GRID_W + x]) >> bd3_centroid_k;
			w = bd3_centroid_table[b < BD3_CENTROID_N ? b : BD3_CENTROID_N - 1];
			den += w;
			nx += w * (2 * x + 1);
			ny += w * (2 * y + 1);
		}
	}
	d->score = l[top];
	d->fire = l[top] >= bd3_threshold;
	d->row = (uint8_t)cy;
	d->col = (uint8_t)cx;
	d->x_q4 = (uint16_t)((nx * BD3_CELL_PX * 8 + den / 2) / den);
	d->y_q4 = (uint16_t)((ny * BD3_CELL_PX * 8 + den / 2) / den);
}

/* The input covers the 640x480 field: 1/16 input pixel = 640 / (96 * 16) = 5 / 12 FD pixel. */
uint32_t bd3_inj_target(unsigned int x_q4, unsigned int y_q4)
{
	int y = (int)(y_q4 * 5 / 12);

	if (y < BD3_INJ_Y_MIN)
		y = BD3_INJ_Y_MIN;
	if (y > BD3_INJ_Y_MAX)
		y = BD3_INJ_Y_MAX;
	return BD3_TGT(x_q4 * 5 / 12, y);
}

bool bd3_inj_pos(uint32_t target, uint32_t age_us, int16_t *x, int16_t *y)
{
	if (!(target & BD3_TGT_VALID) || age_us > BD3_INJ_MAX_AGE_US)
		return false;
	*x = (int16_t)(target & 0xFFFFu);
	*y = (int16_t)((target >> 16) & 0x7FFFu);
	return true;
}
