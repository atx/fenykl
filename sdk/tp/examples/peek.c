/* Example test program: reads the word at arg, logs it and returns it. */
#include <tp.h>

static uint32_t peek_count;

int tp_main(const struct fenykl_tp_api *api, uint32_t arg)
{
	uint32_t v = api->rd32(arg);

	peek_count++;
	api->log("peek %x = %x (run %u)", arg, v, peek_count);
	return (int)v;
}
