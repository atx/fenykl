/* Integer arithmetic of all widths, including 64-bit operations that go through libgcc. */
#include <stdint.h>

int8_t add8(int8_t a, int8_t b) { return a + b; }
uint16_t mul16(uint16_t a, uint16_t b) { return a * b; }
int32_t mul32(int32_t a, int32_t b) { return a * b; }
int32_t mulc(int32_t a) { return a * 1234567; }
int64_t mulwide(int32_t a, int32_t b) { return (int64_t)a * b; }
uint64_t umulwide(uint32_t a, uint32_t b) { return (uint64_t)a * b; }
int32_t div32(int32_t a, int32_t b) { return a / b; }
uint32_t udiv32(uint32_t a, uint32_t b) { return a / b; }
int32_t mod32(int32_t a, int32_t b) { return a % b; }
uint32_t umod32(uint32_t a, uint32_t b) { return a % b; }
uint32_t divc(uint32_t a) { return a / 10; }
int32_t sdivc(int32_t a) { return a / 7; }
int64_t add64(int64_t a, int64_t b) { return a + b; }
int64_t sub64(int64_t a, int64_t b) { return a - b; }
int64_t mul64(int64_t a, int64_t b) { return a * b; }
int64_t div64(int64_t a, int64_t b) { return a / b; }
uint64_t udiv64(uint64_t a, uint64_t b) { return a / b; }
uint64_t umod64(uint64_t a, uint64_t b) { return a % b; }
int64_t shl64(int64_t a, int n) { return a << n; }
int64_t sar64(int64_t a, int n) { return a >> n; }
uint64_t shr64(uint64_t a, int n) { return a >> n; }
int cmp64(int64_t a, int64_t b) { return a < b; }
int32_t shl(int32_t a, int n) { return a << n; }
int32_t sar(int32_t a, int n) { return a >> n; }
uint32_t shr(uint32_t a, int n) { return a >> n; }
uint32_t rotl(uint32_t a, int n) { return (a << (n & 31)) | (a >> (-n & 31)); }
uint32_t rotr1(uint32_t a) { return (a >> 1) | (a << 31); }
int32_t neg(int32_t a) { return -a; }
int32_t absv(int32_t a) { return a < 0 ? -a : a; }
int32_t minv(int32_t a, int32_t b) { return a < b ? a : b; }
uint32_t umaxv(uint32_t a, uint32_t b) { return a > b ? a : b; }
uint32_t bits(uint32_t a, uint32_t b) { return (a & b) ^ (a | ~b); }
int clz(uint32_t a) { return __builtin_clz(a); }
int ctz(uint32_t a) { return __builtin_ctz(a); }
int popc(uint32_t a) { return __builtin_popcount(a); }
uint32_t bswap(uint32_t a) { return __builtin_bswap32(a); }
int parity(uint32_t a) { return __builtin_parity(a); }
int ovf(int32_t a, int32_t b, int32_t *r) { return __builtin_add_overflow(a, b, r); }
int sext8(int32_t a) { return (int8_t)a; }
unsigned zext16(int32_t a) { return (uint16_t)a; }
int bool_ops(int a, int b) { return (a > b) + (a == b) * 2 + (a != 0 && b != 0) * 4; }
