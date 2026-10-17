/* First visible proof that the loader works: writes C:\FENYKL.LOG
   with the arguments the loader passed and the FENYKL task's stack use, then returns (the task ends). */
#include <fenykl.h>

#define STACK_SIZE 0x4000u /* FENYKL task stack above FENYKL_REGION_END, 0xDEADBEEF-filled by the kernel */

static char text[256];

static unsigned length(const char *s)
{
	unsigned n = 0;
	while (s[n])
		n++;
	return n;
}

static unsigned stack_untouched(void)
{
	const uint32_t *p = (const uint32_t *)FENYKL_REGION_END;
	while (p < (const uint32_t *)(FENYKL_REGION_END + STACK_SIZE) && *p == 0xDEADBEEFu)
		p++;
	return (uint32_t)p - FENYKL_REGION_END;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	fw_dbg_puts("[hello] fenykl payload running\n");
	void *fp = fw_rpc_fopen("C:\\FENYKL.LOG", "w");
	if (!fp) {
		fw_dbg_puts("[hello] cannot open C:\\FENYKL.LOG\n");
		return;
	}
	fw_sprintf(text, "fenykl hello\r\nload base %08x\r\nimage size %u\r\nstack untouched %u of %u\r\n",
		   load_base, image_size, stack_untouched(), STACK_SIZE);
	fw_rpc_fwrite(text, 1, length(text), fp);
	fw_rpc_fclose(fp);
}
