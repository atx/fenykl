/* Loads/stores of all widths and addressing modes, structs, bitfields, MMIO. */
#include <stdint.h>

struct s { uint8_t a; uint16_t b; uint32_t c; int8_t d; int16_t e; uint32_t arr[20]; };
struct bf { unsigned x : 3; unsigned y : 11; signed z : 7; unsigned w : 11; };
struct __attribute__((packed)) pk { uint8_t a; uint32_t b; uint16_t c; };

uint8_t g8;
int16_t g16;
uint32_t g32;
struct s gs;
static uint32_t table[256];
const uint16_t ctab[8] = { 1, 2, 3, 5, 8, 13, 21, 34 };

uint32_t load_all(const struct s *p) { return p->a + p->b + p->c + p->d + p->e + p->arr[7]; }
void store_all(struct s *p, uint32_t v) { p->a = v; p->b = v; p->c = v; p->d = v; p->e = v; p->arr[19] = v; }
uint32_t index_load(const uint32_t *p, int i) { return p[i]; }
uint8_t index_load8(const uint8_t *p, int i) { return p[i]; }
int16_t index_load16(const int16_t *p, int i) { return p[i]; }
void index_store(uint32_t *p, int i, uint32_t v) { p[i] = v; }
void index_store8(uint8_t *p, int i, uint8_t v) { p[i] = v; }
void index_store16(uint16_t *p, int i, uint16_t v) { p[i] = v; }
uint32_t globals(void) { g8++; g16--; g32 ^= 5; return g8 + g16 + g32 + table[g8] + ctab[g8 & 7]; }
void copy_struct(struct s *d, const struct s *s) { *d = *s; }
struct s ret_struct(void) { return gs; }
void zero_struct(struct s *d) { *d = (struct s){0}; }
unsigned bf_get(const struct bf *p) { return p->x + p->y + p->z + p->w; }
void bf_set(struct bf *p, unsigned v) { p->y = v; p->z = -v; }
uint32_t packed_get(const struct pk *p) { return p->b + p->c; }
void packed_set(struct pk *p, uint32_t v) { p->b = v; }
void mmio(void) {
	volatile uint32_t *r32 = (volatile uint32_t *)0xA0000000;
	volatile uint16_t *r16 = (volatile uint16_t *)0x8040;
	volatile uint8_t *r8 = (volatile uint8_t *)0x8050;
	r32[3] |= 0x10;
	*r16 &= ~0x4;
	*r8 = *r8 ^ 1;
	while (!(r32[1] & 0x80000000u)) {}
}
void bit_set_clear(volatile uint8_t *p) { *p |= 0x20; *p &= ~0x40; }
void fill(uint32_t *p, int n, uint32_t v) { for (int i = 0; i < n; i++) p[i] = v; }
void copy(uint8_t *d, const uint8_t *s, unsigned n) { while (n--) *d++ = *s++; }
int strcmp_like(const char *a, const char *b) { while (*a && *a == *b) { a++; b++; } return (unsigned char)*a - (unsigned char)*b; }
