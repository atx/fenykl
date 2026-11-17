/* E-M10 Mark II v1.4 FENYKL.BIN boot loader.
   The bytes flashed are pinned in patch.toml; tests/test_fenykl_loader.py checks that this source
   still assembles to them.

   .hook replaces the final `call task_self_end,[],8` of the startup task Usr3 (USRSTART_THIRD, CPU0)
   at 0x6018D73A. Usr3 lowers itself to priority 65 at 0x6018D719, so it reaches the hook only after
   Usr1 (priority 55, never blocking after it releases Usr3) has ended: an OK-held card update boot,
   which never reaches the end of Usr1, never runs the loader.

   fenykl_spawn (.text, boot1 padding at 0x6005E000) returns to the stock behaviour at once unless
   the boot is one of:
   - power switch: the card mount gate is 1 (u16 0x61843F64, set by Usr3's mount request only for
     power-on factor 12 with the switch on and its battery test passed);
   - relay (battery connected while the switch is already on): factor 0x1000001, the switch input
     reads on (pwr_wake_input_levels bit 0 clear) and Usr3's battery test passes (u16 0xA1BF8408 ==
     0x100 or u16 0xA1BF8406 >= 50). Usr3 posts no mount on this boot; the application mounts the
     card later, which only the task waits for.
   Every other factor is refused: 0x400 (USB cable), 0x1506 (card insertion), 0x1000002 (wake bits
   7/8, expected for a watchdog reset but not measured on this body; a watchdog reset that read
   0x1000001 with the switch on would load like a relay boot), and a battery insertion with the
   switch off (0x1000001, switch reads off).
   It then waits until section_wait has run for sec4 and sec5 (bits 3 and 4 of the loaded mask
   0xA1C18044): from then on the section load guard 0x99700000 starts at 0x61010000 and no longer
   covers the payload area 0x60DA5000..0x60E10000, which ends where sec5 starts. Only then it starts
   the FENYKL task, whose stack OlyTaskCreate fills at once. The hook never waits for the card.

   The task waits until the card is ready (fileif_get_media_status(0) & 0x4100 == 0x100: FAT
   mounted, no DCF mount running). It then reads ui_key_get_state(0x400) (1 = no USB cable). With
   a cable (or any other value) the card may belong to a PC: a relay boot loads nothing, a power
   switch boot only reads C:\FENYKL.BIN, as before. Without a cable it tries C:\FENYKL.TST first: if it opens, it is read to LOAD, closed, removed
   (rpc_fremove == 0; the directory entry is written when it returns, the freed clusters may reach
   the card only at a later flush, so a crash can leave lost clusters) and
   checked to be gone (fileif_exists == 0: rpc_fremove also returns 0 for a read-only file it
   skips) before any header check, so a test payload runs at most once. A TST that fails to read, to be removed or the checks falls back to
   C:\FENYKL.BIN on the same boot; the BIN is never removed. The image is read through the cached
   alias (the file layer copies with the CPU through a bounce buffer), the header and the word sum
   are checked, the D-cache is written back (dcache_flush_all) and the entry is called with D0 =
   load base, D1 = image size. Nothing is printed: the console path calls into sec5 while muted.

   Register convention (Panasonic ABI as observed): D0/D1 and A0/A1 are scratch, D2/D3/A2/A3 are
   preserved by every callee (the CALL register list is the callee's save list). The register list
   and frame size of each CALL are those of the firmware's own call sites of that function (checked
   by the tests). Firmware addresses are linker symbols (loader.ld). */
	.equ GATE,    0x61843f64	/* u16, 1 = Usr3 posted the card mount */
	.equ FACTOR,  0xa1c17c14	/* u32 power-on factor */
	.equ RELAY,   0x01000001	/* battery connected (relay) */
	.equ BATSRC,  0xa1bf8408	/* u16, Usr3's battery test: == 0x100 */
	.equ BATLVL,  0xa1bf8406	/* u16, or >= 50 */
	.equ LOADED,  0xa1c18044	/* u8, bit n-1 = section_wait(n) done */
	.equ SEC45,   0x18		/* sec4 and sec5 */
	.equ READY,   0x4100		/* media status: FAT mounted | DCF mount running */
	.equ MOUNTED, 0x100
	.equ KEY_USB, 0x400		/* ui_key_get_state: 1 = no cable */
	.equ LOAD,    0x60da5000
	.equ MAXSZ,   0x00067000	/* LOAD..STK */
	.equ STK,     0x60e0c000
	.equ STKSZ,   0x4000		/* the stack ends at sec5 (0x60E10000) */
	.equ MAGIC,   0x324c4e46	/* "FNL2" */
	.equ MODEL0,  0x314d2d45	/* "E-M1": header model, target.toml model zero-padded to 8 */
	.equ MODEL1,  0x00494930	/* "0II\0" */
	.equ HDR,     0x1c		/* header size */
	.equ PRIO,    55		/* PER_WDT's priority: CPU0 rotates it every 5 ticks */
	.equ SECTRIES, 300
	.equ TRIES,   300		/* covers the application's mount on a relay boot */
	.equ TICKS,   100		/* OlyTaskSleep ticks between tries */

	.section .hook,"ax"
	call fenykl_spawn,[d2,d3,a2,a3],0x20

	.text
fenykl_spawn:			/* entered by the hook: no prologue */
	movhu (GATE),d0
	cmp 1,d0
	beq 5f			/* power switch */
	mov (FACTOR),d0
	cmp RELAY,d0
	bne 2f
	call pwr_wake_input_levels,[d2,d3],8
	btst 1,d0
	bne 2f			/* switch reads off */
	movhu (BATSRC),d0
	cmp 0x100,d0
	beq 5f
	movhu (BATLVL),d0
	cmp 50,d0
	blt 2f
5:	mov SECTRIES,d2
1:	movbu (LOADED),d0
	and SEC45,d0
	cmp SEC45,d0
	beq 3f
	mov TICKS,d0
	call OlyTaskSleep,[d2],8
	add -1,d2
	bne 1b
	bra 2f
3:	mov PRIO,d0
	movhu d0,(4,sp)		/* OlyTaskCreate stack args: (4) pri u16, (8) stack size, (12) stack base */
	mov STKSZ,d0
	mov d0,(8,sp)
	mov STK,d0
	mov d0,(12,sp)
	mov tname,a0		/* A0 = name, A1 = entry, D0 = 2 (create and start), D1 = exinf */
	mov fenykl_task,a1
	mov 2,d0
	clr d1
	call OlyTaskCreate,[d2,d3,a2,a3],0x34
2:	call task_self_end,[],8	/* what the replaced instruction did */
	ret [d2,d3,a2,a3],0x20

fenykl_task:			/* started by the kernel: explicit prologue */
	movm [d2,d3,a2,a3],(sp)
	add -16,sp
	mov TRIES,d2
1:	clr d0				/* drive 0 = SD */
	call fileif_get_media_status,[d2,a2],0xc
	and READY,d0
	cmp MOUNTED,d0
	beq 2f
	mov TICKS,d0
	call OlyTaskSleep,[d2],8
	add -1,d2
	bne 1b
	bra done			/* no card */
2:	mov KEY_USB,d0
	call ui_key_get_state,[d2,d3,a2],0x18
	cmp 1,d0
	beq 6f				/* no cable: TST, then BIN */
	movhu (GATE),d0			/* a cable (or an unknown key): the PC may own the card */
	cmp 1,d0
	bne done			/* relay boot: nothing */
	mov path_bin,a2			/* power switch: BIN only, read-only, as before */
	bra try
6:	mov path_tst,a2			/* A2 = path being tried */
try:	mov a2,a0
	mov mode_r,a1
	call rpc_fopen,[],4		/* A0 = FILE* or 0 */
	mov a0,a3
	cmp 0,a3
	beq next			/* mounted, so the file is missing */
	mov LOAD,a0			/* rpc_fread(buf A0, size D0 = 1, n D1, fp A1) -> D0 bytes */
	mov 1,d0
	mov MAXSZ,d1
	mov a3,a1
	call rpc_fread,[d2,d3,a2,a3],0x18
	mov d0,d3			/* D3 = bytes read */
	mov a3,a0
	call rpc_fclose,[d2,a2,a3],0x10
	mov path_bin,a0
	cmp a0,a2
	beq check
	mov a2,a0			/* FENYKL.TST: never run it unless it is gone from the card */
	call rpc_fremove,[d2,a2,a3],0x18
	cmp 0,d0
	bne next
	mov a2,a0
	call fileif_exists,[],8
	cmp 0,d0
	bne next
check:	mov LOAD,a0
	mov (a0),d0
	cmp MAGIC,d0
	bne next
	mov (0x14,a0),d0
	cmp MODEL0,d0
	bne next
	mov (0x18,a0),d0
	cmp MODEL1,d0
	bne next
	mov (4,a0),d0
	cmp LOAD,d0
	bne next
	mov (8,a0),d1			/* D1 = image size */
	cmp d3,d1
	bhi next			/* size > bytes read */
	cmp HDR,d1
	bcs next			/* size < header */
	btst 3,d1
	bne next			/* size not a multiple of 4 */
	mov (0xc,a0),d0
	cmp d1,d0
	bcc next			/* entry offset >= size */
	cmp HDR,d0
	bcs next			/* entry offset in the header */
	mov LOAD+HDR,a1			/* sum32 of words [HDR, size) == hdr[4] */
	mov LOAD,d0
	add d1,d0
	mov d0,a3
	clr d0
3:	cmp a3,a1
	bcc 4f
	mov (a1),d1
	add d1,d0
	inc4 a1
	bra 3b
4:	mov (0x10,a0),d1
	cmp d1,d0
	bne next
	clr d0				/* the firmware's callers mask all levels around the flush */
	call irq_lock,[d2],8
	mov d0,d2			/* D2 = cookie */
	call dcache_flush_all,[d2],8	/* no I-cache step: nothing ran from LOAD earlier in this boot */
	mov d2,d0
	call irq_unlock,[d2],8
	mov LOAD,a0
	mov (8,a0),d1			/* D1 = image size */
	mov (0xc,a0),d0
	add d0,a0			/* A0 = entry */
	mov LOAD,d0			/* D0 = load base */
	calls (a0)
	bra done
next:	mov path_bin,a0			/* the TST is missing or rejected: try the BIN once */
	cmp a0,a2
	beq done
	mov a0,a2
	bra try
done:	call task_self_end,[],8		/* ext_tsk: the task becomes dormant and is not restarted */
7:	mov 1000,d0
	call OlyTaskSleep,[d2],8
	bra 7b
tname:	.asciz "FENYKL"
path_tst: .asciz "C:\\FENYKL.TST"
path_bin: .asciz "C:\\FENYKL.BIN"
mode_r:	.asciz "r"
