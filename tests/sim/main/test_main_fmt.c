/* The main plugin's distance formatter (sdk/projects/main/main_fmt.c) in the GDB simulator, with
   ../hooks/sim_start.S. plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
     1  a fixed case (unit bounds, rounding carries, extremes) differs from the expected text
     2  return value is not the length, or a write past MAIN_FMT_DIST_SIZE
     4  sweep: a leading zero, a wrong digit count or a unit that does not fit the value
     8  sweep: the text does not parse back to the input rounded half-up at its last digit
     16 sweep: the shown value decreases while the input increases
     32 a text in the sweep does not parse
     64 sweep: the number line is longer than 4 characters (the HUD block's width) */
#include <stddef.h>
#include "../../../sdk/projects/main/main_fmt.h"

#define T_GUARD 16
#define T_MAX 0xFFFFFFFEu

uint32_t sim_exit_code = 99;

struct t_case {
	uint32_t mm;
	const char *text;
};

static const struct t_case t_cases[] = {
	{ 0, "" },
	{ 1, "1\nmm" },
	{ 5, "5\nmm" },
	{ 9, "9\nmm" },
	{ 10, "10\nmm" },
	{ 35, "35\nmm" },
	{ 99, "99\nmm" },
	{ 100, "10.0\ncm" },
	{ 250, "25.0\ncm" },
	{ 995, "99.5\ncm" },
	{ 999, "99.9\ncm" },
	{ 1000, "1.00\nm" },
	{ 1004, "1.00\nm" },
	{ 1005, "1.01\nm" },
	{ 1400, "1.40\nm" },
	{ 9994, "9.99\nm" },
	{ 9995, "10.0\nm" },
	{ 12345, "12.3\nm" },
	{ 12350, "12.4\nm" },
	{ 99949, "99.9\nm" },
	{ 99950, "100\nm" },
	{ 450000, "450\nm" },
	{ 999499, "999\nm" },
	{ 999500, "1.00\nkm" },
	{ 1250000, "1.25\nkm" },
	{ 9994999, "9.99\nkm" },
	{ 9995000, "10.0\nkm" },
	{ 12300000, "12.3\nkm" },
	{ 99949999, "99.9\nkm" },
	{ 99950000, "100\nkm" },
	{ 450000000, "450\nkm" },
	{ 999499999, "999\nkm" },
	{ 999500000, "1000\nkm" },
	{ 1004999999, "1000\nkm" },
	{ 1005000000, "1010\nkm" },
	{ T_MAX, "4290\nkm" },
	{ MAIN_FMT_DIST_INF, "inf" },
};

/* The parsed text: value and the half-up window width (one unit of the last significant digit). */
struct t_parsed {
	uint64_t value;
	uint32_t step;
	uint32_t unit;
	int digits;		/* significant digits */
};

static int t_streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

static size_t t_strlen(const char *s)
{
	size_t n = 0;

	while (s[n])
		n++;
	return n;
}

/* Format into a guarded buffer; 0 if the length or the guard is wrong. */
static int t_fmt(char *buf, uint32_t mm)
{
	int i, len;

	for (i = 0; i < T_GUARD; i++)
		buf[i] = 0x55;
	len = main_fmt_dist(buf, mm);
	for (i = MAIN_FMT_DIST_SIZE; i < T_GUARD; i++)
		if (buf[i] != 0x55)
			return 0;
	return len >= 0 && len < MAIN_FMT_DIST_SIZE && (size_t)len == t_strlen(buf);
}

static uint32_t t_unit(const char *s)
{
	if (t_streq(s, "mm"))
		return 1;
	if (t_streq(s, "cm"))
		return 10;
	if (t_streq(s, "m"))
		return 1000;
	if (t_streq(s, "km"))
		return 1000000;
	return 0;
}

/* The text after the line break */
static const char *t_unit_line(const char *s)
{
	while (*s && *s != '\n')
		s++;
	return *s ? s + 1 : s;
}

/* "NN.N\nunit" -> t_parsed; 0 if malformed */
static int t_parse(const char *s, struct t_parsed *out)
{
	uint64_t n = 0;
	int digits = 0, dec = -1;

	for (; *s != '\n'; s++) {
		if (*s == '.' && dec < 0) {
			dec = 0;
			continue;
		}
		if (*s < '0' || *s > '9')
			return 0;
		n = n * 10 + (*s - '0');
		digits++;
		if (dec >= 0)
			dec++;
	}
	if (!digits || dec == 0)
		return 0;
	out->unit = t_unit(s + 1);
	if (!out->unit)
		return 0;
	out->step = out->unit;
	for (; dec > 0; dec--) {
		if (out->step % 10)
			return 0;
		out->step /= 10;
	}
	out->value = n * out->step;
	out->digits = digits;
	if (digits == 4 && n % 10 == 0) {
		out->digits = 3;
		out->step *= 10;
	}
	return 1;
}

/* Shape: no leading zero, 3 significant digits (whole mm below 100 mm), the largest fitting unit. */
static int t_shape_ok(const char *text, uint32_t mm, const struct t_parsed *p)
{
	if (text[0] < '1' || text[0] > '9')
		return 0;
	if (mm < 100)
		return p->unit == 1 && p->digits == (mm < 10 ? 1 : 2);
	if (p->digits != 3 || p->unit == 1)
		return 0;
	if (p->unit == 10)
		return p->value < 1000;
	if (p->unit == 1000)
		return p->value < 1000000;
	return 1;
}

/* Half-up rounding at the last digit: value - step/2 <= mm < value + step/2. */
static int t_round_ok(uint32_t mm, const struct t_parsed *p)
{
	uint64_t twice = 2 * (uint64_t)mm;

	return 2 * p->value <= twice + p->step && twice < 2 * p->value + p->step;
}

/* Checks one sweep value; *last is the previous shown value for the monotonic check. */
static int t_sweep_one(uint32_t mm, uint64_t *last)
{
	char buf[T_GUARD];
	struct t_parsed p;
	int fail = 0;

	if (!t_fmt(buf, mm))
		fail |= 2;
	if (!t_parse(buf, &p))
		return fail | 32;
	if (t_strlen(buf) - t_strlen(t_unit_line(buf)) - 1 > 4)
		fail |= 64;
	if (!t_shape_ok(buf, mm, &p))
		fail |= 4;
	if (!t_round_ok(mm, &p))
		fail |= 8;
	if (last) {
		if (p.value < *last)
			fail |= 16;
		*last = p.value;
	}
	return fail;
}

static int t_fixed(void)
{
	char buf[T_GUARD];
	unsigned i;
	int fail = 0;

	for (i = 0; i < sizeof(t_cases) / sizeof(t_cases[0]); i++) {
		if (!t_fmt(buf, t_cases[i].mm))
			fail |= 2;
		if (!t_streq(buf, t_cases[i].text))
			fail |= 1;
	}
	return fail;
}

/* Every mm up to 100 m, then geometric steps of about 0.025 % up to the maximum. */
static int t_sweep(void)
{
	uint64_t last = 0, mm;
	int fail = 0;

	for (mm = 1; mm <= 100000; mm++)
		fail |= t_sweep_one(mm, &last);
	for (; mm < T_MAX; mm += (uint32_t)mm / 4096 + 1)
		fail |= t_sweep_one(mm, &last);
	return fail | t_sweep_one(T_MAX, &last);
}

/* Around every rounding threshold of 100, 101, 999 and 1000 units of each digit position. */
static int t_thresholds(void)
{
	static const uint32_t qs[] = { 100, 101, 999, 1000 };
	uint32_t step;
	uint64_t c;
	unsigned i;
	int d, fail = 0;

	for (step = 10; step <= 10000000; step *= 10) {
		for (i = 0; i < sizeof(qs) / sizeof(qs[0]); i++) {
			c = (uint64_t)qs[i] * step - step / 2;
			for (d = -1; d <= 1; d++)
				if (c + d <= T_MAX)
					fail |= t_sweep_one(c + d, NULL);
		}
	}
	return fail;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	sim_exit_code = t_fixed() | t_thresholds() | t_sweep();
}
