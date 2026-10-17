/* E-PL3 FENYKL.BIN boot loader v3.
   The bytes flashed are pinned in patch.toml; tests/test_fenykl_loader.py checks that this source
   still assembles to them.

   .hook replaces the final `call OlyTaskSelfEnd,[],8` of usr1_task at 0x6E70A0AB. fenykl_spawn
   (.text, over fileiftest_write_rec at 0x6E7BA37B) starts the FENYKL task and ends USTART1 as before.
   The task retries opening C:\FENYKL.BIN until the card is mounted, reads it to LOAD, checks it and
   calls its entry with D0 = load base, D1 = image size. An open that fails although fat_mounted[0] was
   already set before it means the file is missing: FILECLBK serves opens only after the mount and the
   DCF scan, so retrying would only add directory searches.
   Status printed via dbg_puts_hex: 0 payload returned, 1 no file (or no card within TRIES), 2 bad image.

   Register convention (Panasonic ABI as observed): D0/D1 and A0/A1 are scratch, D2/D3/A2/A3 are
   preserved by every callee (the CALL register list is the callee's save list). The register list
   and frame size of each CALL are those of the firmware's own call sites of that function (checked
   by the tests). Firmware addresses are linker symbols (loader.ld): with .equ, as emits the
   absolute address as the CALL displacement. */
	.equ MOUNTED, 0x6059c978	/* fat_mounted: u16[2], [0] = SD card, 1 = FAT mounted */
	.equ LOAD,    0x6ea70000
	.equ LOAD_UC, 0xaea70000	/* uncached alias */
	.equ MAXSZ,   0x0007a000	/* LOAD..STK */
	.equ STK,     0x6eaea000
	.equ STKSZ,   0x4000
	.equ MAGIC,   0x324c4e46	/* "FNL2" */
	.equ MODEL0,  0x4c502d45	/* "E-PL": header model, target.toml model zero-padded to 8 */
	.equ MODEL1,  0x00000033	/* "3\0\0\0" */
	.equ HDR,     0x1c		/* header size */
	.equ PRIO,    11
	.equ TRIES,   100
	.equ TICKS,   100		/* OlyTaskSleep ticks between tries */

	.section .hook,"ax"
	call fenykl_spawn,[d2,d3,a2,a3],0x20

	.text
fenykl_spawn:			/* entered by the hook: no prologue */
	mov PRIO,d0
	movhu d0,(4,sp)		/* OlyTaskCreate stack args: (4) pri u16, (8) stack size, (12) stack base */
	mov STKSZ,d0
	mov d0,(8,sp)
	mov STK,d0
	mov d0,(12,sp)
	mov tname,a0		/* A0 = name, A1 = entry, D0 = 2 (create and start), D1 = exinf */
	mov fenykl_task,a1
	mov 2,d0
	clr d1
	call OlyTaskCreate,[d2,d3,a2,a3],0x30
	call OlyTaskSelfEnd,[],8	/* what the replaced instruction did */
	ret [d2,d3,a2,a3],0x20

fenykl_task:			/* started by the kernel: explicit prologue */
	movm [d2,d3,a2,a3],(sp)
	add -16,sp
	mov TRIES,d2		/* fails cleanly while the card is not mounted */
1:	mov MOUNTED,a0
	movhu (a0),d3			/* D3 = mounted before the open */
	mov path,a0
	mov mode_r,a1
	call rpc_fopen,[],0x10		/* A0 = FILE* or 0 */
	mov a0,a2
	cmp 0,a2
	bne 2f
	cmp 0,d3
	bne 6f				/* mounted, so the file is missing */
	mov TICKS,d0
	call OlyTaskSleep,[d2],8
	add -1,d2
	bne 1b
6:	mov 1,d0
	bra done
2:	mov LOAD_UC,a0			/* rpc_fread(buf A0, size D0 = 1, n D1, fp A1) -> D0 bytes */
	mov 1,d0
	mov MAXSZ,d1
	mov a2,a1
	call rpc_fread,[],0x18
	mov d0,d3			/* D3 = bytes read */
	mov a2,a0
	call rpc_fclose,[],0xc
	call dcache_purge_all_irqsafe,[d2],8
	mov LOAD,a0			/* check the header through the cached alias */
	mov (a0),d0
	cmp MAGIC,d0
	bne bad
	mov (0x14,a0),d0
	cmp MODEL0,d0
	bne bad
	mov (0x18,a0),d0
	cmp MODEL1,d0
	bne bad
	mov (4,a0),d0
	cmp LOAD,d0
	bne bad
	mov (8,a0),d1			/* D1 = image size */
	cmp d3,d1
	bhi bad				/* size > bytes read */
	cmp HDR,d1
	bcs bad				/* size < header */
	btst 3,d1
	bne bad				/* size not a multiple of 4 */
	mov (0xc,a0),d0
	cmp d1,d0
	bcc bad				/* entry offset >= size */
	cmp HDR,d0
	bcs bad				/* entry offset in the header */
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
	bne bad
	mov (8,a0),d1			/* D1 = image size */
	mov (0xc,a0),d0
	add d0,a0			/* A0 = entry */
	mov LOAD,d0			/* D0 = load base */
	calls (a0)
	clr d0
	bra done
bad:	mov 2,d0
done:	mov msg,a0
	call dbg_puts_hex,[d2,a2],0x18
	call OlyTaskSelfEnd,[],8	/* ext_tsk: the task becomes dormant and is not restarted */
5:	mov 1000,d0
	call OlyTaskSleep,[d2],8
	bra 5b
tname:	.asciz "FENYKL"
path:	.asciz "C:\\FENYKL.BIN"
mode_r:	.asciz "r"
msg:	.asciz "[fenykl] status"
