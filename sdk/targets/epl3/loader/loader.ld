/* Links loader.S at its run addresses in main-code-01. */

OlyTaskCreate = 0x6e62158d;
OlyTaskSleep = 0x6e6214fc;
OlyTaskSelfEnd = 0x6e621518;
rpc_fopen = 0x6e7b817d;
rpc_fread = 0x6e7b823d;
rpc_fclose = 0x6e7b81a5;
dcache_purge_all_irqsafe = 0x6e8ecce4;
dbg_puts_hex = 0x6e70aa34;

SECTIONS
{
	/* usr1_task: call OlyTaskSelfEnd,[],8 */
	.hook 0x6e70a0ab : { *(.hook) }
	/* fileiftest_write_rec, 410 bytes, debug shell only */
	.text 0x6e7ba37b : { *(.text) *(.data) *(.bss) }
}

ASSERT(SIZEOF(.hook) == 7, "the hook must replace exactly the 7-byte CALL")
ASSERT(SIZEOF(.text) <= 410, "the stub must fit into fileiftest_write_rec")
