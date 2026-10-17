/*
 * main: depth of field. 32-bit integers only: libgcc's 64-bit shift and division objects
 * do not link into a plugin (sdk/docs/logging.md), so main_dof_muldiv divides bit by bit.
 */
#include "main_dof.h"

/* 2^(j / 16) in Q16, j = 0..16 */
static const uint32_t main_dof_exp2[17] = {
	65536, 68438, 71468, 74632, 77936, 81386, 84990, 88752, 92682,
	96785, 101070, 105545, 110218, 115098, 120194, 125515, 131072,
};

/* a * b / d rounded half-up, d > 0; MAIN_DOF_INF if the quotient does not fit 32 bits */
static uint32_t main_dof_muldiv(uint32_t a, uint32_t b, uint32_t d)
{
	uint32_t al = a & 0xFFFF, ah = a >> 16, bl = b & 0xFFFF, bh = b >> 16;
	uint32_t ll = al * bl, lh = al * bh, hl = ah * bl;
	uint32_t mid = (ll >> 16) + (lh & 0xFFFF) + (hl & 0xFFFF);
	uint32_t lo = (ll & 0xFFFF) | mid << 16;
	uint32_t hi = ah * bh + (lh >> 16) + (hl >> 16) + (mid >> 16);
	uint32_t q = 0, r;

	lo += d / 2;
	if (lo < d / 2)
		hi++;
	if (hi >= d)
		return MAIN_DOF_INF;
	r = hi;
	for (int i = 31; i >= 0; i--) {
		bool carry = r >> 31;

		r = r << 1 | (lo >> i & 1);
		if (carry || r >= d) {
			r -= d;
			q |= 1u << i;
		}
	}
	return q;
}

uint32_t main_dof_n1000(uint32_t av)
{
	uint32_t k = av >> 9, j = (av >> 5) & 15, frac = av & 31;
	uint32_t m = main_dof_exp2[j] + ((main_dof_exp2[j + 1] - main_dof_exp2[j]) * frac + 16) / 32;

	/* 1000 * m / 2^(16 - k), k <= 8 */
	return (1000 * m + (1u << (15 - k))) >> (16 - k);
}

bool main_dof_calc(struct main_dof *d, uint32_t s, uint32_t f, uint32_t av, uint32_t coc_um)
{
	uint32_t hyper, x;

	if (f < 1 || f > MAIN_DOF_F_MAX || s <= f || av > MAIN_DOF_AV_MAX || coc_um < 1)
		return false;
	d->n1000 = main_dof_n1000(av);
	/* f^2 / (N c) mm = f^2 * 1000 * 1000 / (n1000 * c_um), at most 1e9 */
	hyper = main_dof_muldiv(f * f, 1000000, d->n1000 * coc_um);
	d->hyper = hyper;
	if (s >= MAIN_DOF_S_INF) {
		d->near = hyper;
		d->far = d->depth = MAIN_DOF_INF;
		return true;
	}
	x = s - f;
	d->near = main_dof_muldiv(s, hyper, hyper + x);
	d->far = x < hyper ? main_dof_muldiv(s, hyper, hyper - x) : MAIN_DOF_INF;
	d->depth = d->far == MAIN_DOF_INF ? MAIN_DOF_INF : d->far - d->near;
	/* below 1 mm */
	if (!d->depth)
		d->depth = 1;
	return true;
}
