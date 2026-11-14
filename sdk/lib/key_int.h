/*
 * Internals of the key layer shared by key.c and the camera's key map (key_<camera>.c).
 */
#ifndef _FENYKL_KEY_INT_H
#define _FENYKL_KEY_INT_H

#include <key.h>

/* Low 16 bits of each key's APPEVENT code; 0 = the body has no such key. */
extern const uint16_t fenykl_key_codes[FENYKL_KEY_COUNT];

#endif
