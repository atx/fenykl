/* The injected candidate (inject.h). Pure code: runs in the FD task's hook. */
#include "inject.h"

/* fenykl-nn report 4.2 step 5: centre of cell (cx, cy) = ((2 cx + 1) * 80 / 3, (2 cy + 1) * 80 / 3). */
uint32_t bd_inj_target(unsigned int cx, unsigned int cy)
{
	int y = (int)(80 * (2 * cy + 1) / 3);

	if (y < BD_INJ_Y_MIN)
		y = BD_INJ_Y_MIN;
	if (y > BD_INJ_Y_MAX)
		y = BD_INJ_Y_MAX;
	return BD_TGT(80 * (2 * cx + 1) / 3, y);
}

static int16_t bd_inj_toward(int16_t from, int to)
{
	if (to > from + BD_INJ_STEP)
		return (int16_t)(from + BD_INJ_STEP);
	if (to < from - BD_INJ_STEP)
		return (int16_t)(from - BD_INJ_STEP);
	return (int16_t)to;
}

bool bd_inj_step(struct bd_inj *s, uint32_t target, uint32_t age_us)
{
	int x = (int)(target & 0xFFFFu), y = (int)((target >> 16) & 0x7FFFu);

	if (!(target & BD_TGT_VALID) || age_us > BD_INJ_MAX_AGE_US) {
		s->valid = false;
		return false;
	}
	if (!s->valid) {
		s->cx = (int16_t)x;
		s->cy = (int16_t)y;
		s->valid = true;
		return true;
	}
	s->cx = bd_inj_toward(s->cx, x);
	s->cy = bd_inj_toward(s->cy, y);
	return true;
}
