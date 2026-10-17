/*
 * main: the focus meter's area choice and absolute log2 scale.
 */
#include "main_fmeter.h"

unsigned int main_fmeter_word(unsigned int area, unsigned int cols)
{
	return area / cols * MAIN_FMETER_ROW_WORDS + area % cols;
}

unsigned int main_fmeter_area(const uint32_t *h3, unsigned int cols, unsigned int rows, bool all,
			      int area)
{
	unsigned int areas = cols * rows, best = 0;
	uint32_t max = 0;

	if (!all) {
		if (area >= 0 && (unsigned int)area < areas)
			return (unsigned int)area;
		return rows / 2 * cols + cols / 2;
	}
	for (unsigned int i = 0; i < areas; i++) {
		uint32_t x = h3[main_fmeter_word(i, cols)];

		if (x > max) {
			max = x;
			best = i;
		}
	}
	return best;
}

int32_t main_fmeter_log2(uint32_t v)
{
	int32_t e = 0;
	uint32_t frac;

	if (!v)
		return 0;
	for (uint32_t x = v; x > 1; x >>= 1)
		e++;
	frac = e >= 8 ? v >> (e - 8) : v << (8 - e);
	return e * MAIN_FMETER_LOG2_ONE + (int32_t)(frac & 0xFFu);
}

static bool main_fmeter_half_height(uint8_t pattern)
{
	return (pattern >= 0x4u && pattern <= 0x7u) || (pattern >= 0x13u && pattern <= 0x16u);
}

static int32_t main_fmeter_level(uint32_t v, uint8_t pattern)
{
	int32_t l = main_fmeter_log2(v);

	return main_fmeter_half_height(pattern) ? l + MAIN_FMETER_HALF_OFFSET : l;
}

static uint8_t main_fmeter_lit(int32_t level)
{
	const uint32_t span = MAIN_FMETER_HI - MAIN_FMETER_LO;

	if (level <= MAIN_FMETER_LO)
		return 0;
	if (level >= MAIN_FMETER_HI)
		return MAIN_FMETER_CELLS;
	return (uint8_t)(((uint32_t)(level - MAIN_FMETER_LO) * MAIN_FMETER_CELLS + span / 2) / span);
}

uint8_t main_fmeter_cells(uint32_t v, uint8_t pattern)
{
	return main_fmeter_lit(main_fmeter_level(v, pattern));
}

static void main_fmeter_sample(struct main_fmeter *m, const struct main_fmeter_in *in, uint32_t now)
{
	struct main_fmeter_bar *b = &m->bar;

	m->live = true;
	m->last_fresh = now;
	m->level = main_fmeter_level(in->v, in->pattern);
	b->shown = true;
	b->lit = main_fmeter_lit(m->level);
	b->green = b->lit == MAIN_FMETER_CELLS;
	if (b->lit >= m->hold || now - m->hold_at >= MAIN_FMETER_PEAK_MS) {
		m->hold = b->lit;
		m->hold_at = now;
	}
	b->peak = m->hold > b->lit ? m->hold : 0;
}

void main_fmeter_step(struct main_fmeter *m, const struct main_fmeter_in *in, uint32_t now)
{
	if (in->hide) {
		m->live = false;
		m->bar.shown = false;
		return;
	}
	if (in->fresh || in->half)
		m->last_busy = now;
	if (in->fresh) {
		main_fmeter_sample(m, in, now);
		return;
	}
	if (m->live && now - m->last_fresh >= MAIN_FMETER_STALE_MS)
		m->live = false;
	/* S-AF stops the statistics at lock until ~1 s after the release: the last bar stays */
	if (!m->live && now - m->last_busy >= MAIN_FMETER_HOLD_MS)
		m->bar.shown = false;
}
