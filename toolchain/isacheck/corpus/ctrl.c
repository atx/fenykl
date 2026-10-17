/* Control flow, calls, frames, varargs, function pointers, register pressure. */
#include <stdarg.h>
#include <stdint.h>

extern int ext(int);
extern void *extp(void *, void *);

int dense_switch(int x) {
	switch (x) {
	case 0: return ext(10);
	case 1: return ext(11) + 1;
	case 2: return ext(12) * 3;
	case 3: return 44;
	case 4: return ext(14) - 2;
	case 5: return 66;
	case 6: return ext(x);
	case 7: return 99;
	default: return -1;
	}
}
int sparse_switch(int x) {
	switch (x) {
	case 3: return 1;
	case 1000: return 2;
	case 70000: return 3;
	case -5: return 4;
	default: return 0;
	}
}
int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
int sum_va(int n, ...) {
	va_list ap;
	va_start(ap, n);
	int s = 0;
	for (int i = 0; i < n; i++) s += va_arg(ap, int);
	va_end(ap);
	return s;
}
int call_va(void) { return sum_va(5, 1, 2, 3, 4, 5); }
int many_args(int a, int b, int c, int d, int e, int f) { return a - b + c - d + e - f; }
int call_many(void) { return many_args(1, 2, 3, 4, 5, 6); }
void *ptr_ret(void *a, void *b) { return (char *)extp(a, b) + 4; }
int indirect(int (*f)(int), int x) { return f(x) + f(x + 1); }
static int (*const fns[])(int) = { ext, fib };
int table_call(int i, int x) { return fns[i & 1](x); }
int big_frame(int x) {
	volatile uint8_t buf[600];
	for (int i = 0; i < 600; i++) buf[i] = x + i;
	return buf[x & 511] + ext(buf[3]);
}
int vla(int n) { volatile int a[n]; for (int i = 0; i < n; i++) a[i] = i; return a[n / 2]; }
int pressure(int *p) {
	int a = p[0], b = p[1], c = p[2], d = p[3], e = p[4], f = p[5], g = p[6], h = p[7];
	ext(a);
	return a * b + c * d + e * f + g * h + ext(a + b + c + d + e + f + g + h);
}
int64_t ret64(int64_t a) { return a + ext((int)a); }
struct pair { int a, b; };
struct pair ret_pair(int x) { struct pair p = { x, x + 1 }; return p; }
struct big { int v[5]; };
int byval(struct big b) { return b.v[0] + b.v[4]; }
int call_byval(void) { struct big b = { { 1, 2, 3, 4, 5 } }; return byval(b); }
void loop_nest(int *m, int n) { for (int i = 0; i < n; i++) for (int j = 0; j < n; j++) m[i * n + j] = i ^ j; }
int __attribute__((noinline)) leaf(int a) { return a * 3 + 1; }
int tail(int a) { return leaf(a + 1); }
