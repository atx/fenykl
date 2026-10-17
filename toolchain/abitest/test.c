/* ABI bridge test, run in the GDB simulator: exit status 0 = all checks passed, otherwise the
   bit mask of the failed checks. */
#include <stdint.h>

int fw_mix(const int *p, int a, int b, const int *q);
int fw_stack(const char *name, int mode, int exinf, void *entry, int prio, int size, void *base);
int *fw_vsum(int *out, int n, ...);
int fw_callcb(int (*fn)(void *, int), void *ctx, int v);
int cb_thunk(void *ctx, int v);

int cb(void *ctx, int v) { return *(int *)ctx * 10 + v; }

static uint32_t expect_stack(const char *name, int mode, int exinf, void *entry, int prio, int size,
                             void *base)
{
	return (uint8_t)*name ^ (uint32_t)mode << 1 ^ (uint32_t)exinf << 2 ^ (uint32_t)entry << 3 ^
	       (uint32_t)prio << 4 ^ (uint32_t)size << 5 ^ (uint32_t)base << 6;
}

int main(void)
{
	static int x = 7, y = 11, sum, ctx = 4;
	static char name[] = "FENYKL";
	int fail = 0;
	/* Values kept live across the calls, so GCC holds them in D2/D3/A2/A3. */
	volatile int seed = 3;
	int k1 = seed * 5, k2 = seed + 100, k3 = seed << 4;
	int *kp = &x;

	if (fw_mix(&x, 2, 3, &y) != 7 + 4 + 9 + 55)
		fail |= 1;
	if ((uint32_t)fw_stack(name, 2, 0x1234, (void *)cb, 11, 0x4000, (void *)0x6eaea000) !=
	    expect_stack(name, 2, 0x1234, (void *)cb, 11, 0x4000, (void *)0x6eaea000))
		fail |= 2;
	if (fw_vsum(&sum, 8, 1, 2, 3, 4, 5, 6, 7, 8) != &sum + 1 || sum != 36)
		fail |= 4;
	if (fw_vsum(&sum, 3, 100, 200, 300) != &sum + 1 || sum != 600)
		fail |= 8;
	if (fw_callcb(cb_thunk, &ctx, 2) != 42)
		fail |= 16;
	if (k1 != 15 || k2 != 103 || k3 != 48 || kp != &x || *kp != 7)
		fail |= 32;
	return fail;
}
