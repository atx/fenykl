# Shared plugin logging (`sdk/lib/log*.c`)

Status: works on the camera (init, `"a"` append, overflow counters, CPU1 ring, logging from a hook,
console tee, flushes into standby, the card gate closing on a USB cable on the E-PL3); not run on the
camera: wake from standby, the 4 MiB cap, the rest of the card gate (section 5.1), logging from ISR
context. Simulator tests: `tests/test_log_sim.py`. Evidence tags:
[verified] read in a listing/decompile or seen in a camera log; [camera] seen on the camera; [sim]
measured in the GDB mn10300 simulator running the firmware's own code (section 9); [inferred].

## 1. Overview

- One API for plugin features: levels, modules, no copied buffer code.
- Callable from any context on either core: a plugin task, a firmware hook on any task, an ISR-context
  hook, a CPU1 worker. No file I/O, no blocking, no formatting outside the flusher.
- One file `C:\FENYKL.LOG`, appended across boots with a boot header; a 4 MiB size cap applied at init
  only (deleted and started fresh). No rotation, no numbered files.
- Timestamps from `timer_now` (1 MHz, section 6.1). No RTC.
- No crash survival beyond a short flush interval (section 5, "Lost on a hang").
- Optional tee of the firmware console (section 7).

Design rules:

- **No text formatting at the call site, and no SDK formatter in the library.** Records are binary and
  formatted by the flusher with `fw_sprintf` (section 3). `fw_sprintf` costs ~4600 instructions for a
  five-argument line [sim], a 48-instruction record write is 100x cheaper, and the record format is what
  makes CPU1 and ISR producers trivial. An SDK formatter (~1000 instructions, 350 bytes [sim]) buys
  nothing once formatting is out of the producers.
- **No host-side decoder.** The file is plain text; the fenykl CLI is not involved.
- **No tee by default.** The tee is opt-in and polls a buffer, it patches no code (section 7).
- **No cross-core lock.** Each core has its own ring; the CPU0 flusher is the only consumer. The
  firmware's `bset` lock word 0x8FF00100 is the IPU's, not ours, and a hook must never spin.
- The entry point picks the ring by core id (section 4.2), so a hook that may run on CPU1 needs no
  special code, but a `cpu1_unproven` hook stays `cpu1_unproven`.
- The E-PL3 build of `hello` does not use the library, so it validates the SDK without its failure
  modes.

## 2. API

Header `sdk/include/log.h`, sources `sdk/lib/log.c` (CPU0: init, flush, file, tee) and
`sdk/lib/log_rec.c` (record writers, both cores: it defines `FENYKL_CPU1` itself, so it can only
call CPU1-safe firmware). Every plugin links the library; `--gc-sections` drops it from plugins that
do not log, whose images are unchanged.

```c
enum fenykl_log_level { FENYKL_LOG_OFF, FENYKL_LOG_ERROR, FENYKL_LOG_WARN, FENYKL_LOG_INFO,
                        FENYKL_LOG_DEBUG, FENYKL_LOG_TRACE };

/* A module: a name of at most 7 characters and its runtime level. One per feature, file scope. */
struct fenykl_log_module { const char *name; volatile uint8_t level; };
#define FENYKL_LOG_MODULE(sym, name, level) struct fenykl_log_module sym = { name, level }

/* Compile-time ceiling per translation unit (define before including log.h), default DEBUG.
   Calls above it compile to nothing (the arguments are not evaluated). */
#define FENYKL_LOG_COMPILE_LEVEL FENYKL_LOG_DEBUG

/* fmt: a string literal, a fw_sprintf format with at most 4 conversions of d i u x X c s p (flags,
   width, precision, h/l); up to 4 32-bit arguments, more is a compile error (_Static_assert).
   Any context, either core, never blocks. fenykl_log(mod, level, fmt, ...) takes the level. */
fenykl_log_err(mod, fmt, ...)  fenykl_log_warn(...)  fenykl_log_info(...)  fenykl_log_dbg(...)
fenykl_log_trace(...)  fenykl_log(mod, level, fmt, ...)

void fenykl_log_rec(const struct fenykl_log_module *mod, unsigned level, const char *fmt,
                    uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3);
/* Copies len bytes (at most FENYKL_LOG_TEXT_MAX = 255, longer is cut and marked [truncated]),
   20 per record, continuation records for the rest, all in one locked section. */
void fenykl_log_text(const struct fenykl_log_module *mod, unsigned level, const char *text, unsigned len);

#define FENYKL_LOG_FILE "C:\\FENYKL.LOG"
#define FENYKL_LOG_SIZE_CAP (4u << 20)   /* compile-time; applied at init only */
#define FENYKL_LOG_SLOW_US 500000u       /* a flush longer than this is logged and counted */

/* CPU0 task context only (file RPC). Zero fields take the defaults. */
struct fenykl_log_config {
	unsigned int flush_ms; /* flusher period, default 2000 */
	bool tee;              /* the flusher task polls the firmware console capture (section 7) */
};
int fenykl_log_init(const struct fenykl_log_config *cfg); /* size cap, boot header; 0 or
                        FENYKL_LOG_EOPEN / EAPPEND (C:\FENYKL.ERR written) / EBUSY (second call) /
                        ENOCARD (card gate closed: card work deferred, section 5.1) */
int fenykl_log_flush(void);      /* drains both rings into the file and closes it before it
                                    returns: 0, EOPEN (records stay queued), EBUSY (another
                                    flush is running, or before init) or ENOCARD
                                    (card gate closed, section 5.1) */
int fenykl_log_task_start(void); /* "FYKLOG", priority FENYKL_LIB_TASK_PRIO (E-PL3 12, E-M10 II
                                    55), 4 KiB stack: flushes every flush_ms or when a ring is
                                    half full; polls the tee every 20 ms */
void fenykl_log_set_level(struct fenykl_log_module *mod, unsigned level);
void fenykl_log_get_stats(struct fenykl_log_stats *out);
```

`struct fenykl_log_stats` (`log.h`) has per ring `produced`, `written`, `lost`, `queued`, the
producer counters `overwritten0` and `dropped1`, `early` (records before init), `bad`, `truncated`,
the flush counters (`flushes`, `open_fail`, `short_write`, `flush_max_us`,
`flush_slow`, `bytes`), the card gate counters (`gate_skipped`,
`gate_max_ms`, section 5.1), the init results (`file_size`, `removed_size`,
`boot_cause`) and the tee counters. Every 20th flush
writes two to four `status:` lines with them (tee and card gate lines only when used).

Context rules (what each entry point may do):

| context | `fenykl_log_*` macros / `fenykl_log_text` | `fenykl_log_flush`, `_init`, `_task_start` |
|---|---|---|
| plugin task on CPU0 (`plugin_main`, `FENYKL_TASK`) | yes | yes |
| hook or slot replacement on a firmware task (CPU0) | yes (masks CPU0 interrupts for ~40 instructions) | **no** (file RPC blocks; `fenykl_hook_install` rules) |
| hook in ISR context (CPU0) | yes (same path: `irq_disable_all`/`irq_mask_restore` keep the PSW) | no |
| CPU1 worker, CPU1 hook | yes (CPU1 ring, uncached) | no (`rpc_*` are CPU0-only; compile error in `cpu1_sources`) |

Integration with the task model: `plugin_main` calls `fenykl_log_init` once the card is there (it is:
the loader waited for it), then either calls `fenykl_log_flush` from its own loop or
`fenykl_log_task_start` once. The main plugin uses the task. A plugin that returns from
`plugin_main` without a task must call `fenykl_log_flush` before returning. **Prefer the task:** a
flush can block in the file RPC; in the task that stalls only the flusher, the producers keep going
and the rings count what they lose. A plugin that flushes from its own loop stalls with it.

Example feature module:

```c
#define FENYKL_LOG_COMPILE_LEVEL FENYKL_LOG_TRACE   /* this file keeps its TRACE lines */
#include "log.h"                                    /* includes fenykl.h */

FENYKL_LOG_MODULE(hud, "hud", FENYKL_LOG_INFO);     /* runtime level, settable from a menu later */

static int on_update(unsigned mode)                 /* cw_updateDisplay hook, any task, maybe CPU1 */
{
	fenykl_log_trace(&hud, "update mode=%u core=%u", mode, FENYKL_CORE_ID());
	return fw_orig_cw_updateDisplay(mode);
}
FENYKL_HOOK(cw_updateDisplay, on_update);

void hud_poll(void)                                 /* from the feature's task loop */
{
	uint32_t mm = read_distance();
	if (mm != shown) {
		fenykl_log_info(&hud, "distance %u mm (was %u)", mm, shown);
		shown = mm;
	}
}
```

Output lines (section 6.2 for the stamp): `[    12.345678] I hud    : distance 250 mm (was 300)`.

## 3. Record format: binary records, formatted by the flusher

Choice: **binary in RAM, text in the file.** The producer stores a 32-byte record (timestamp, level,
module pointer, format pointer, up to four 32-bit arguments). The flusher, a task, turns each record
into a text line with `fw_sprintf(line, fmt, a0, a1, a2, a3)` and writes text to the card.

| | text at the call (`fw_sprintf` in the producer) | SDK formatter at the call | binary record, format at flush |
|---|---|---|---|
| instructions per 5-argument line, producer side | 4586 + copy into the ring (~700 for 42 bytes with the byte `memcpy`) [sim] | 1002 + copy [sim] | 48 incl. the IRQ mask calls [sim] |
| producer stack | ~370 bytes below the caller [sim] | ~250 bytes [sim] | ~32 bytes |
| ring bytes per line | ~45-80 | ~45-80 | 32 |
| CPU1 | `fw_sprintf` is callable on CPU1, but the line still needs a ring | same | same path as CPU0 |
| host decoding | none | none | none (text file) |
| code | 0 (firmware) | ~350 bytes [sim] | ~100 bytes writer + the flusher's format loop |
| risk | formatting inside a hook: 370 B of stack on an unknown firmware stack (`usr1` has 2 KiB), `fw_sprintf` reentrancy only [inferred] (section 9) | own code, reentrant by construction | `%s` arguments must still be valid at flush time |

At 162 MHz and ~1.3 cycles/instruction, a `fw_sprintf` line costs ~35-40 us, the record ~0.4 us
[inferred from sim counts; the sim has no cache or DRAM stalls]. Per-frame logging at 60 fps from a hook
would spend 2.4 ms/s on text, 25 us/s on records; what matters more is hook latency and stack, and
that CPU1 and ISR producers need no formatter at all. The flusher pays the same `fw_sprintf` cost at
`FENYKL_LIB_TASK_PRIO` (12 on the E-PL3, 55 on the E-M10 II) where it is harmless (1000 lines/s =
4 % of CPU0).

`%s` rule: the pointer must be valid at flush time: a string literal, or a static buffer whose content
the reader accepts as "latest". The flusher prints `<%08x>` instead of dereferencing a pointer outside
the payload image and the firmware image ranges (a stack string from a hook would be garbage, and
without a NUL it could run `fw_sprintf` off the end of the line buffer). Dynamic text goes through
`fenykl_log_text`, which copies.

Record layout (one ring slot, 32 bytes, word-aligned; all fields written as 32-bit words so that each
store is atomic on both cores [inferred, standard MN103]):

```
word 0  t_us      timer_now, read inside the locked section (30-bit, 1 MHz; section 6.1)
word 1  meta      seq 0-15 | len 16-20 (text bytes) | core 21 | kind 22-23 (0 fmt, 1 text,
                  2 text continuation) | level 24-26 | truncated 27
word 2  mod       const struct fenykl_log_module *   (name + level; the flusher reads the name)
word 3  fmt       const char *                       (kind 0) / first 4 text bytes (kind 1, 2)
word 4-7 args[4]                                     (kind 0) / 16 more text bytes (kind 1, 2)
```

`seq` is a per-ring 16-bit producer counter; the flusher reports gaps as `[lost N]`. Text records carry
20 bytes each; a 100-byte firmware console line is five records (section 7). The flusher checks every
record before it dereferences anything: the module and format pointers must lie in the payload image
(`FENYKL_LOAD_BASE` .. end of `.bss`), the module name must end within 64 bytes, the format must end
within 160 bytes with at most 4 conversions of `d i u x X c s p`, and the line's worst-case length must
fit the 320-byte line buffer; otherwise it prints `<bad record ...>` or `<bad format ...>` and counts
`bad`. A `%s` argument outside the payload and firmware images, or without a NUL within 128 bytes, is
printed as `<%08x>`.

## 4. Buffering

### 4.1 Rings and memory

Two rings of 32-byte slots, each with a header of counters (`head`, `tail`, `overwritten`/`dropped`):

- **Ring 0 (CPU0 producers):** a 12-byte header and 512 records (16 KiB), a member of the library's
  state `flog` in `.bss`, accessed through the **cached alias**: its producers (tasks, hooks, ISR
  context) and its only consumer, the flusher, all run on CPU0, which sees its own D-cache. Nothing
  reads it from CPU1 or through the uncached alias.
- **Ring 1 (CPU1 producers):** a 64-byte header (CPU1 writes `head`, `seq`, `dropped`; CPU0 writes `tail`)
  and 128 records = 4 KiB, `FENYKL_SHARED` (section `.shared`, 64-byte lines, zeroed by the loader),
  accessed through `FENYKL_SHARED_PTR` on both sides. A
  plugin without `cpu1_sources` gets the header only (the build passes `FENYKL_PLUGIN_CPU1=0` to the
  library): a record from CPU1 (a `cpu1_unproven` hook that did run there) is dropped and counted.

Staging: the flusher copies up to 32 records out of each ring (plus the continuation records of a text
message the batch would cut, at most 48), ring 0 under the lock, merges the two batches by stamp, formats
them into a 4 KiB text buffer and writes it when full; repeat until both rings are empty (bounded to
one ring's worth of rounds per flush). Line buffer 320 bytes. Head, tail and the other counters are
free-running 32-bit counters; a record's slot is `counter % records`.

### 4.2 Producer rules

- `fenykl_log_rec` reads `FENYKL_CORE_ID()` (`0x8050`) and picks the ring: core 0 -> ring 0, core 1 ->
  ring 1. One function for every context; hooks that may run on CPU1 need no special code.
- **Ring 0 is multi-producer under the CPU0 interrupt mask**: `psw = fw_psw_get(); fw_irq_disable_all();
  ... fw_irq_mask_restore(psw)` (the same primitives `hooks.c` uses; all three are per-core, `[],0`
  CALLs [verified firmware.toml]). This serialises tasks, hooks on other tasks and ISR-context hooks on
  CPU0 (a task switch needs an interrupt or a service call, and the section makes none). The section is
  the 8 stores plus the head/tail update, ~40 instructions [sim]. Whether level-0 interrupts bypass
  `irq_disable_all` is open; the record stores are idempotent per slot, so the
  worst case is one torn record, caught by `seq`.
- **Ring 1 is multi-producer under the CPU1 interrupt mask** (the same calls act on CPU1's PSW) and
  single-consumer: only CPU1 writes `head`, only CPU0 writes `tail`, both uncached, so no cross-core lock
  exists. The CPU0 flusher reads ring 1 only through the uncached alias.
- A text message's records are written in one locked section, so no other producer's record lands
  between them (255 bytes = 13 records, a few microseconds with interrupts masked).
- The consumer takes ring 0's lock only to copy out a batch (up to 48 records). Ring 1 needs no lock on
  the consumer side: it reads `head`, copies, then writes `tail`.
- Nothing in the producer path calls the firmware except the three PSW primitives; nothing loops over
  an unbounded input (`fenykl_log_text` caps at 255 bytes).

### 4.3 Overflow policy and counters

- **Ring 0 keeps the newest records**: when full, the producer advances `tail` (drops the oldest) and
  increments `overwritten`. Rationale: before a failure the last records are the ones worth keeping. The lock makes the overwrite safe against the consumer.
- **Ring 1 drops the newest** (classic SPSC: the producer cannot move `tail`) and counts `dropped`. A
  dropped record still uses up its `seq`, so the flusher prints the gap as `[lost N]`, but only once the
  next ring 1 record arrives; `dropped` counts at once.
  With a 2 ms CPU1 sleep granularity and a flush every 2 s, 128 slots hold ~60 records/s sustained;
  a CPU1 worker that logs per frame uses `fenykl_log_trace` and expects drops.
- The flusher writes `[lost N]` lines from `seq` gaps and the counters in the periodic status line.
- Records queued while `rpc_fopen` fails (card unmounted for USB) stay in the rings until they overflow.
- Accounting, once both rings are drained: ring 0 `produced = written + lost`, `lost = overwritten`;
  ring 1 `produced = written + dropped` [camera].

## 5. Persistence

Firmware facts [verified in the E-PL3 1.6 decompile, section 9 for addresses]:

- Mode letters are flag bits: `r` = 1, `w` = 2, `a` = 0x0A, `b`, `+`, `d` = 0x10, `e`, `c`, `h`
  (`f_open_mode_table` 0x6E979758). After the directory lookup `f_open` sets the position: **not append:
  position = 0, size field untouched; append (flag 8): position = the entry's size**. So `"w"` does
  not truncate (matches the camera observation) and `"a"` positions at the end; `"a"` appends
  on the camera [camera].
- `f_close` calls `f_flush` only when the file was written (flags & 2 and the dirty mark); `f_flush`
  writes the pending sector (one read-modify-write of 512 bytes) and rewrites the directory entry. So a
  batch costs: `open` (directory lookup; for `"a"` the FAT chain walk to the end, linear in the file
  size [inferred]), the data sectors, one directory update. `rpc_fflush` 0x6E7B820C exists (cmd
  0x10004, no firmware caller) but is not needed with open/close per batch.
- **Delete exists**: `rpc_fremove(path)` 0x6E7B82D5 (cmd 0x1000A -> `f_remove_entry(path, 0)`, `ret
  [],12`, a firmware caller logs `fileif_remove error`). **No rename found**: the file-server
  dispatch (0x6E7B7FC5) has commands 0x10001-5, 7-0xD, 0x11-0x13; the string `rename %s : %s` has no
  code reference. Bounded negative: the handlers for 0xB, 0xC, 0x11-0x13 were not decompiled.
- `rpc_fseek` 0x6E7B82A2 exists (cmd 0x10008 -> `f_seek`, registers not characterised): a fallback if
  `"a"` fails (open `"w"`, seek to the end, write), untested either way.

Strategy:

- **One file, `C:\FENYKL.LOG`, appended across boots.** `fenykl_log_init` measures it: `rpc_fopen(..,
  "r")` and `rpc_fread` through it in 8 KiB chunks (from `fw_malloc`, which refuses more than 0x3FFC
  bytes [camera]; else the 4 KiB text buffer):
  the file RPC has no stat. The cost is linear in the size, at most the 4 MiB cap.
- **Size cap at init only:** a file larger than `FENYKL_LOG_SIZE_CAP` (4 MiB, compile time) is deleted
  with `rpc_fremove` and started fresh; the new file's first lines say how large the old one was. If
  the delete fails, the log says so and appends anyway. Nothing checks the size during a boot.
- **Boot header:** a line of `=` and `fenykl log boot: build <FENYKL.BIN sum>, boot cause <E-PL3: byte
  0x6044151A; E-M10 II: the u32 power-on factor>, timer <timer_now>, file <n> bytes before this boot`.
- **"w" only for a file that does not exist** (the `"r"` probe failed, or the cap deleted it): `"w"`
  does not truncate, but on a new file it is exact. Every other open is `"a"`.
- Each flush: `fp = rpc_fopen(name, "a")`, `rpc_fwrite` per 4 KiB of text, `rpc_fclose(fp)`. Never keep
  the file open (USB unmount, card pull). A flush with nothing queued does no file I/O.
- **If `"a"` fails** on an existing file at init (three tries, 100 ms apart), init returns
  `FENYKL_LOG_EAPPEND` and writes `C:\FENYKL.ERR` (a padded one-line error, `"w"` of a different
  name). There is no fallback (`rpc_fseek`, padded rewrite): a clear error instead of a silent change
  of strategy. Later flushes keep trying `"a"`; records queue meanwhile.
- **Cadence**: `fenykl_log_flush` every `flush_ms` (2 s) or early when a ring is more than half full
  (the task polls every 100 ms, 20 ms with the tee). Measured cost of a 4 KiB `"w"` rewrite: 0-100 ms
  [camera]; the cost of `"a"` (its FAT chain walk grows with the file) is not measured; every
  flush over 500 ms is logged and counted (`flush_slow`), the slowest is `flush_max_us`.
- **Lost on a hang**: the records since the last flush (up to `flush_ms`). Lost on a card pull or USB:
  records that overflow the rings while opens fail or the card gate is closed (section 5.1). There is no crash recovery;
  for crash hunting use a short flush interval and the synchronous flush below.
- **Synchronous flush**: `fenykl_log_flush()` returns after `rpc_fclose`; when it returns 0, the
  records queued at the call are in the file. A CPU0 task calls it on an error path before it gives up
  or hangs; `FENYKL_LOG_EBUSY` means the flusher task is in a flush (sleep and call it again) or init
  has not run.
- Firmware function added to `firmware.toml`: `rpc_fremove` (`[],12`, A0 = path; read from its `ret`
  and its one caller). `rpc_fseek` stays out (registers not characterised, not needed).

### 5.1 Card gate (USB cable at run time)

A cable plugged into a running camera hands the card to the PC in place, while the camera's FAT stays
mounted [static analysis]; a log write then corrupts the card. Every flush first
asks `fenykl_card_io_allowed()` (card mounted and no cable seen this boot; README, Startup and USB
boots). Once a cable was seen the gate stays closed until power-off: on the E-PL3 (2026-10-19) a
reopened gate after a USB session wrote `FENYKL.LOG` with the camera's FAT state from before the
session, and the `FENYKL.BIN` the PC had written lost its cluster chain (`fsck.fat`: directory entry
on a free cluster, dirty bit set).

- **Closed:** no file RPC at all. `fenykl_log_flush` returns `FENYKL_LOG_ENOCARD`, counts
  `gate_skipped` (not `open_fail`) and leaves the records in the rings; overflow is counted as usual
  (`overwritten0`, `dropped1`, `[lost N]` lines later). The flusher polls the gate every `flush_ms`
  only (a half-full ring does not trigger extra checks). After a cable, `gate_skipped` keeps counting
  until power-off and the queued records are lost with it.
- **Transitions:** closing for an unmounted card queues the record `W log : card gate closed (card
  not mounted): log on hold`; the mount reopening it queues `I log : card gate open again after <ms>
  ms, <n> flushes skipped`. Both reach the file with the first flush after reopening, in stamp order
  with the other records. A cable queues no record: none could reach the file. `gate_max_ms` is the
  longest closed period (resolution: the flush period); status blocks add a `card gate` line once a
  flush was skipped.
- **Init with the gate closed:** `fenykl_log_init` takes the configuration, starts the streams and
  accepts records, but does not touch the card and returns `FENYKL_LOG_ENOCARD` (not an error to
  give up on). The size cap and the boot header run in the first flush with the gate open, before
  the queued records; the header's line carries that flush's stamp, the records keep theirs.
- **Stamps** of records queued over a closed period of more than 1073 s: section 6.1.
- On the camera: the cable closes the gate (E-PL3, 2026-10-19). The stay-closed rule is
  simulator-tested only (README status table).

## 6. Timestamps

### 6.1 Source

`timer_now` = `0x3FFFFFFF - *(u32 *)0x98900074`, a free-running 1 MHz 30-bit counter, wraps after
2^30 us = 1073.74 s [inferred, strong; the RTOS tick is 1 ms]. The producer stores the
raw 30-bit value (one load, no arithmetic). The flusher extends it to seconds since init:

- **Clock:** every `fenykl_log_flush` extends `timer_now`, card gate open or not. It must run at
  least once per wrap; the flusher task runs it every `flush_ms`. The library's own lines (boot
  header, status, slow flush) are stamped on it.
- **Per ring, a lower bound:** a record gets the first stamp at or after the bound with its raw
  bits and becomes the next bound (records are in order per ring). The end of every flush notes the
  clock and the ring heads; a record queued after that is later than that time less 1 s (a CPU1
  record's stamp precedes its head update), and the ring's bound rises to it once all older records
  of the ring are formatted or evicted.
- **Exact** is every record less than 1073 s after its bound: one drained by the first flush after
  it (however long its ring was quiet), and one that waited in its ring (card gate closed,
  `rpc_fopen` failing) less than 1073 s after the previous record of its ring.
- **Ambiguous** is a record that waited in its ring across flushes and came more than 1073 s after
  the previous record of its ring: its raw stamp cannot tell how many wraps passed. It is stamped as
  if less than one, so short by whole wraps of 1073.74 s, still in order within its ring. The
  `card gate open again after <ms> ms` line gives the length of a closed period.

Across rings, records are merged by stamp since both cores
read the same timer [inferred: one timer block, read by both cores].

### 6.2 Format

`[ssssss.uuuuuu] L module : text`, seconds since `fenykl_log_init` with six decimals, `L` one of
`E W I D T`, module name padded to 7. CPU1 records get `L` in lower case. The header line
carries the absolute `timer_now` at init so that two logs of one boot can be aligned. The flusher keeps
seconds and microseconds as two 32-bit words (no 64-bit division, section 10).

## 7. Optional firmware console tee

The firmware's `dbg_puts` traffic goes into the log, opt-in with `cfg.tee = 1`, by polling the boot
capture buffer (no code patch). `uart_putchar` appends every character to `uart_boot_capture`
0xA07C5E80 (767 bytes, count u16 at 0x8FF00088) while `uart_state[1]` 0x8FF00082 is below 2, which
on a UART0 camera is forever [verified code path; the camera's variant: runtime]. These are the
E-PL3 addresses; both bodies list theirs as `uart_tee_*` in `firmware.toml`.

Every 20 ms the flusher task copies and resets the capture under the CPU0 interrupt mask, splits it
into lines (`\r` dropped), and logs each line as ring 0 text records of module `fwcon` (a line
longer than 255 bytes is split). Cost: one load when nothing arrived, a byte copy of what arrived; no
hook, no firmware code touched. More than 767 characters within 20 ms overflow the buffer and log
`[tee overflow: console characters lost]`; `uart_state[1] >= 2` logs once that the capture is off.
Downsides: the plugin's own `fw_dbg_puts` lands there too (harmless), characters from CPU1 come
through the DBG_MESSAGE_CPU1 queue in firmware order, and it does not work on a UART1 camera.

## 8. Size and cost

Code (mn10300 `-Os`, `size` of the objects): `log.c`
5.2 KiB text incl. its strings, `log_rec.c` 1.1 KiB, `.bss` 24.7 KiB incl. ring 0, ring 1 4 KiB + 64 B
of `.shared` (only with `cpu1_sources`). Per part (estimates):

| part | bytes |
|---|---|
| `fenykl_log_rec` + `fenykl_log_text` (both rings, core switch, IRQ mask) | ~300 |
| init: file size probe, size cap, header line | ~500 |
| flush: drain both rings, stamps, `fw_sprintf` per record, `%s` pointer check, text buffer, file open/append/close, status line | ~900 |
| flusher task, stats, level setter | ~150 |
| console tee (poll + copy) | ~150 |
| total | ~2.0 KiB (the examples' hand-rolled logging is ~0.6-1 KiB each) |

RAM: ring 0 16 KiB, ring 1 4 KiB `.shared` (only with a CPU1 worker), staging 1 KiB, text buffer
4 KiB, line 256 B, state ~200 B: **~21.5 KiB of payload `.bss`**. Flusher task stack 4 KiB from `fw_malloc` (`fw_sprintf` needs ~370 B, the file RPC a few
hundred).

CPU per call [sim instruction counts; cycles inferred at ~1.3/instruction, 162 MHz, no stall model]:

| path | instructions | time |
|---|---|---|
| `fenykl_log_*` filtered out at runtime (level check) | ~6 | 50 ns |
| record from a CPU0 task or hook (cached code and ring) | 48 | ~0.4 us |
| record from an ISR-context hook | same | same; interrupts are already masked, the PSW round trip is harmless |
| record from CPU1 | 48 | ~0.4 us + 8 uncached stores (CPU1 at 162 MHz) |
| `fenykl_log_text`, 100 bytes | ~1500 | ~12 us (5 records; the byte copy dominates) |
| flusher per record | ~4600 `fw_sprintf` + ~700 copy/stamp | ~45 us; 1000 records/s = 4-5 % of CPU0 at `FENYKL_LIB_TASK_PRIO` |
| flusher per flush, file part | one `rpc_fopen` + write + `rpc_fclose` | 0-100 ms observed for 4 KiB `"w"`; append not measured |

## 9. Firmware facts (E-PL3)

| fact | answer | evidence |
|---|---|---|
| `"a"` | mode flags 0x0A; `f_open` sets position = size when flag 8 is set, else position 0 with the size kept (no truncation) | [verified] `f_open` 0x6E7BBC77 mode dispatch, handlers 0x6E7BBCDD (`w`: flags \| 2) and 0x6E7BBCE3 (`a`: flags \| 10), then the common open code of 0x6E7BBCDD (position from the size field only when flag 8 is set); camera: `"w"` keeps the tail (README) |
| truncation | none in `f_open`; a new file name is the only exact way | [verified] as above; `f_flush` 0x6E7BC5EA rewrites the directory entry from the FILE fields |
| remove / rename | `rpc_fremove` 0x6E7B82D5 (cmd 0x1000A) yes; rename not found (dispatch 0x6E7B7FC5 cmds 1-5, 7-D, 11-13; `rename %s : %s` has no xref) | [verified] decompile; rename [bounded negative] |
| flush / close cost | `f_close` -> `f_flush` only if written: pending sector RMW + directory entry; `rpc_fflush` 0x6E7B820C exists | [verified] decompile 0x6E7BBE7C, 0x6E7BC5EA |
| boot cause | byte 0x6044151A from 0x98E00048 / 0x98E00000; watchdog code unknown | [verified] decompile 0x6E6340C0 |
| locking primitives | `psw_get` 0x6E623B47, `irq_disable_all` 0x6E623B17, `irq_mask_restore` 0x6E623B40, per core, in `firmware.toml`; `irq_mask_raise(level)` 0x6E623B1F not yet; cross-core: only the firmware's `bset` word 0x8FF00100 (IPU), no general lock | [verified] firmware.toml |
| `fw_sprintf` reentrancy | `vformat_core` 0x6E8F6683 stores `FUN_6e8f6b79()` into the global 0x60610C70, read by its helpers only as `**ptr` compared with characters: the locale decimal point, constant across callers | [inferred] decompile; the firmware calls it from many tasks |
| `fw_sprintf` cost | 4586 instructions for `"%s t=%u phase=%u heartbeat=%u state=%x"`, 1930 for `"flip %u fc %u"`; output byte-exact; ~370 B of stack | [sim] firmware text2 + `.data` image loaded into `mn10300-elf-run`, `--trace-insn` line counts minus a 10-instruction baseline |
| formatting in hook context | possible (pure, 370 B stack) but not needed: records are binary | design |

## 10. Implementation notes

- The file size is measured by reading the file through at init: the file RPC has no stat, and
  `rpc_fseek`'s registers are not characterised.
- `"w"` only for a missing file. If the `"r"` probe fails while the file exists (card busy), `"w"`
  overwrites from the start; the loader waits for the card before the payload runs, so the case should
  not occur. `"a"` on a missing file is untested.
- Ring 1's header is 64 bytes, its own cache line: CPU1 writes `head`, CPU0 `tail`, each in its own
  half. Plugins without `cpu1_sources` get no ring 1 slots (build-time switch `FENYKL_PLUGIN_CPU1`).
- Ring 1 drops use up their `seq`, so they show as `[lost N]` like ring 0 evictions (with the next
  ring 1 record).
- Stamps are seconds + microseconds, no 64-bit count: libgcc's 64-bit division objects carry debug
  sections that the link (`--orphan-handling=error`) refuses.
- Records are merged by stamp per batch of 32 per ring: the order across rings is exact within a batch,
  approximate across batches; per ring it is exact.
- Status lines are written as text by the flusher, not as records, so they do not disturb the
  accounting.
- Records before `fenykl_log_init` are counted (`early`), not stored: the stamps count from init.
