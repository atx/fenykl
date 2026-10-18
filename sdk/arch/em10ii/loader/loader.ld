/* Links loader.S at its run addresses in main-code-01 (E-M10 Mark II v1.4). */

OlyTaskCreate = 0x60069cad;
OlyTaskSleep = 0x6006992f;
task_self_end = 0x6006994b;
irq_lock = 0x60088483;
irq_unlock = 0x600884cc;
dcache_flush_all = 0x6007f577;
rpc_fopen = 0x60564829;
rpc_fread = 0x605649bb;
rpc_fclose = 0x60564832;

SECTIONS
{
	/* Usr3 (USRSTART_THIRD): call task_self_end,[],8 */
	.hook 0x6018d73a : { *(.hook) }
	/* boot1 padding 0x6005B7C0..0x60060000, loaded by main_init, unreferenced */
	.text 0x6005e000 : { *(.text) *(.data) *(.bss) }
}

ASSERT(SIZEOF(.hook) == 7, "the hook must replace exactly the 7-byte CALL")
ASSERT(SIZEOF(.text) <= 0x2000, "the stub must end before 0x60060000")
