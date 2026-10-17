/*
 * main: HUD text formatting. Integer-only; 32-bit division is inline on the MN103.
 */
#include "main_fmt.h"

struct main_fmt_unit {
	const char *name;
	int8_t dec;		/* digits after the point, -1: append a zero */
};

/* Indexed by the decimal exponent of the last shown digit in mm, for 3 digits. */
static const struct main_fmt_unit main_fmt_units[] = {
	{ "\ncm", 1 }, { "\nm", 2 }, { "\nm", 1 }, { "\nm", 0 },
	{ "\nkm", 2 }, { "\nkm", 1 }, { "\nkm", 0 }, { "\nkm", -1 },
};

static char *main_fmt_str(char *p, const char *s)
{
	while (*s)
		*p++ = *s++;
	return p;
}

static int main_fmt_end(char *buf, char *p)
{
	*p = 0;
	return p - buf;
}

/* q in 100..999 with the point placed before the last dec digits */
static char *main_fmt_digits(char *p, uint32_t q, int dec)
{
	char d[3];
	int i;

	for (i = 2; i >= 0; i--) {
		d[i] = '0' + q % 10;
		q /= 10;
	}
	for (i = 0; i < 3; i++) {
		if (i == 3 - dec)
			*p++ = '.';
		*p++ = d[i];
	}
	if (dec < 0)
		*p++ = '0';
	return p;
}

int main_fmt_dist(char *buf, uint32_t mm)
{
	const struct main_fmt_unit *unit;
	uint32_t step = 1, q, r;
	int e = 0;
	char *p = buf;

	if (mm == 0)
		return main_fmt_end(buf, p);
	if (mm == MAIN_FMT_DIST_INF)
		return main_fmt_end(buf, main_fmt_str(p, "inf"));
	if (mm < 100) {
		if (mm >= 10)
			*p++ = '0' + mm / 10;
		*p++ = '0' + mm % 10;
		return main_fmt_end(buf, main_fmt_str(p, "\nmm"));
	}
	while (mm / step >= 1000) {
		step *= 10;
		e++;
	}
	q = mm / step;
	r = mm % step;
	if (2 * r >= step)
		q++;
	if (q == 1000) {
		q = 100;
		e++;
	}
	unit = &main_fmt_units[e];
	p = main_fmt_digits(p, q, unit->dec);
	return main_fmt_end(buf, main_fmt_str(p, unit->name));
}
