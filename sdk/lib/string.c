/* The memory functions GCC may emit calls to in freestanding code. Built with
   -fno-tree-loop-distribute-patterns so that the loops are not turned back into calls to
   themselves. */
#include <stddef.h>

void *memcpy(void *restrict dst, const void *restrict src, size_t n)
{
	char *d = dst;
	const char *s = src;
	while (n--)
		*d++ = *s++;
	return dst;
}

void *memmove(void *dst, const void *src, size_t n)
{
	char *d = dst;
	const char *s = src;
	if (d < s) {
		while (n--)
			*d++ = *s++;
		return dst;
	}
	while (n--)
		d[n] = s[n];
	return dst;
}

void *memset(void *dst, int c, size_t n)
{
	unsigned char *d = dst;
	while (n--)
		*d++ = (unsigned char)c;
	return dst;
}

int memcmp(const void *a, const void *b, size_t n)
{
	const unsigned char *x = a, *y = b;
	for (; n; n--, x++, y++)
		if (*x != *y)
			return *x - *y;
	return 0;
}
