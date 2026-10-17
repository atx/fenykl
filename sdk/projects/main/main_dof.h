/*
 * main: depth of field from the lens state, free of firmware calls so that the simulator
 * test (tests/sim/main/) runs the real code.
 */
#ifndef _MAIN_DOF_H
#define _MAIN_DOF_H

#include <stdbool.h>
#include <stdint.h>

#define MAIN_DOF_INF 0xFFFFFFFFu	/* distance infinity, as obj_dst */
#define MAIN_DOF_S_INF 3000000000u	/* focus distances from 3000 km count as infinity */
#define MAIN_DOF_F_MAX 1000u		/* focal length mm; longer is taken as a bad read */
#define MAIN_DOF_AV_MAX (16u * 256u)	/* Av APEX x 256: f/256 */

struct main_dof {
	uint32_t n1000;		/* f-number x 1000 */
	uint32_t hyper;		/* f * f / (N * c) mm: the hyperfocal distance minus f */
	uint32_t near, far;	/* limits in mm, far MAIN_DOF_INF past the hyperfocal distance */
	uint32_t depth;		/* far - near in mm, at least 1; MAIN_DOF_INF if far is */
};

/* f-number x 1000 for Av in APEX x 256 (N = 2^(Av / 512)), Av <= MAIN_DOF_AV_MAX. */
uint32_t main_dof_n1000(uint32_t av);

/*
 * Near and far limits, as ExifTool's Composite:DOF: with H = f^2 / (N c),
 * near = s H / (H + s - f), far = s H / (H - s + f), far = inf once s - f >= H; s = inf
 * (>= MAIN_DOF_S_INF) gives near = H. A far limit past 4294 km is inf. s mm (or MAIN_DOF_INF), f mm, Av APEX x 256, c in um. False (d unchanged) unless
 * 1 <= f <= MAIN_DOF_F_MAX, f < s, av <= MAIN_DOF_AV_MAX and c >= 1.
 */
bool main_dof_calc(struct main_dof *d, uint32_t s, uint32_t f, uint32_t av, uint32_t coc_um);

#endif
