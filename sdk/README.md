# fenykl plugin SDK

Write C, run one command, copy `FENYKL.BIN` to the card root. The boot loader (fenykl patch
`em10ii/fenykl-loader`) reads `C:\FENYKL.BIN` into RAM at every power-switch or relay boot, checks it and calls
it. No reflashing per plugin: the firmware is flashed once.

The SDK is split per camera (`arch/<camera>/`, see Layout). The target is `em10ii` (OM-D E-M10 Mark II
v1.4, three MN10300 cores): the runtime, `string`, `log`, `cfg`, the settings tab (`lib/ui.c` +
`lib/ui_em10ii.c`), the HUD library (`lib/hud.c` + `lib/hud_em10ii.c`: GUI widgets), the body API's
`lens`, `batt`, `afc`, `shot` and `wb_base` groups, slots (data pointer words, `lib/slots.c`), code
hooks (`arch/em10ii/hooks.c`), the event layer, Button Function entries, pre-capture and the dev
monitor; `main` and
`hello` build for it.

## Status

| what | state |
|---|---|
| loader firmware (`em10ii/fenykl-loader`, FNL2 header with model check) | **works on the camera**: `main` runs |
| broken images ignored by the loader | simulator only (`tests/test_fenykl_loader.py`) |
| callbacks (`FENYKL_CALLBACK`) | **works on the camera** through `lib/afc.c` (`projects/main`) |
| slots (`FENYKL_SLOT_REPLACE`) | **works on the camera** through the HUD library (`lib/hud_em10ii.c`) |
| CPU1 code (`cpu1_sources`) | does not build: no target has the `cpu1` capability (no CPU1 runtime on the E-M10 II yet) |
| other thunks (`fw_OlyTaskCreate` from C, `fw_malloc`/`fw_free`, `fw_dcache_flush_all`, `fw_dbg_puts`) | used on the camera by the libraries and plugins; `fw_dbg_puts` output is invisible without the internal UART |
| logging library (`lib/log*.c`) | **works on the camera** in `projects/main`: init, flushes. Not run: CPU1 records, wake from standby, the 4 MiB cap; the console tee has nothing to read on this firmware (`docs/logging.md` 7). `fw_malloc` refuses more than 0x4000 bytes |
| E-M10 II settings tab (`lib/ui.c` + `lib/ui_em10ii.c`) and configuration | simulator (`tests/test_sim.py`, `ui_em10ii`); the tab mechanism (Custom tab L, native rows, patched stock pickers) ran on the camera in a probe plugin (2026-11-19); this library **works on the camera** (2026-11-20: the main plugin's tab with sets saved and kept over a reboot; 41 rows on 6 pages, ENUMs up to 26 names, the removal; `docs/ui.md` 7). `cfg` save to `C:\FENYKL.CF0` and reload after a power cycle **work on the camera** (2026-10-23) |
| event layer (`lib/event.c`) | simulator (`tests/test_sim.py`, `fnfunc_em10ii`); a pass-through handler ran on the camera (`docs/fnfunc.md`) |
| Button Function entries (`lib/fnfunc_em10ii.c`) | simulator (`tests/test_sim.py`, `fnfunc_em10ii`); Fn3's entry and press listener ran on the camera, Fn1/Fn2/REC/L-Fn and main's screenshot through it not yet |
| card gate at run time (`fenykl_usb_cable_present`, `fenykl_card_io_allowed`; the logging library holds its flushes) | not run on the camera: once a cable was seen the gate stays closed until power-off (Cable at run time) |
| E-M10 II HUD (`lib/hud.c` + `lib/hud_em10ii.c` + `lib/hud_rle.c`: elements as stock image widgets on the live-view info panel through three GUI slots, INFO-screen property, palette pick below 0x80, camera font and live-view images) | simulator (`tests/test_hud_sim.py`: text with the E-M10 II font and images, the visibility predicate, output device, palette pick, the widget backend against GUI mocks, the encoder); camera: the widget mechanism with one widget (2026-11-17: shown on every still INFO screen on LCD and EVF, gone in movie and the menu, no GUI heap leak over 28 panel builds); the overlay it replaces drew `projects/main`'s blocks (2026-10-23) |
| E-M10 II own contrast client (`lib/afc.c` with `arch/em10ii/fenykl_body.h`) and `projects/main`'s focus meter | **works on the camera** (2026-10-26): the read-only meter in S-AF and C-AF; in MF 4 attaches, callbacks in an ISR at 31 or 64 frames/s, every stop done, S-AF locking after MF, no error, no call on another core. Not run: a half press while attached |
| E-M10 II pre-capture (`lib/precap_em10ii.c`, `precap` capability) and `projects/main`'s option and `PRE` tag | builds; the mechanism (injected press, hold 6, commit, cancel, fail-safes) **works on the camera** in a prototype plugin (2026-10-28: 7 silent H bursts, 46 files matching the log); the library form in `projects/main` tried by hand (2026-10-28), without a log check |
| E-M10 II dev monitor (`lib/devmon_*.c`, opt-in `devmon` capability, `projects/devmon1`, `fenykl dev`, `fenykl tp build`) | **works on the camera** (2026-11-17, relay boot): the access point, protocol v1 PING, READ (505 KiB/s with 8 KiB pieces and a 1 ms pause), WRITE and read-back, a test program, FENYKL.* put/get/del, the name refusals, the power-off sequence; KEY, the FENYKL.BIN refusal, the WRITE deny ranges, the cached WRITE, `fenykl dev screen` (OSD layers to PNG), the memory sampler (`fenykl dev sample`), the read-only photo and screenshot fetch and FLIST: simulator and host tests only (`tests/test_devmon.py`) |
| E-M10 II hooks (`arch/em10ii/hooks.c`, CPU0 I-cache invalidate) | simulator (`tests/test_sim.py`, `hooks_em10ii`); the I-cache invalidate verified on the camera with a probe (2026-10); a hook probe installed its hooks on the camera (2026-10-19) and removed them cleanly, CPU0 only (2026-10-20) |

## Development loop

```sh
cd fenykl
uv run fenykl plugin build sdk/projects/hello            # -> sdk/projects/hello/build/em10ii/FENYKL.BIN
uv run fenykl payload info sdk/projects/hello/build/em10ii/FENYKL.BIN   # must end with "loader accepts"
```

`plugin build` builds every camera listed in the plugin's `plugin.toml`, each into `build/<camera>/`;
`--target em10ii` builds only that one (and fails if `plugin.toml` does not list it). `--isa-check` also
runs the target's ISA check; it is optional and needs Ghidra with an MN10300 extension that is not
part of this repository (see below).

1. Copy `build/em10ii/FENYKL.BIN` to the **card root** (not `DCOLYMP/`). The card must not hold
   `DCOLYMP/E1209999.BIN`, and do not hold OK at power-on: that would start a firmware update.
2. Power on. The loader waits for the card (normally a second or two), runs the payload in its own task,
   and the camera carries on.
3. Power off, read what the payload wrote (`C:\FENYKL.LOG` by convention) on a PC.
4. Next iteration: rebuild, overwrite `FENYKL.BIN`, repeat. Nothing is flashed.

**If a payload crashes or hangs the camera:** power off (battery out if needed), remove the card, delete
or rename `FENYKL.BIN` on a PC. Without the file the camera boots like stock. If it does not boot even
without the card, use the recovery card (`DCOLYMP/E1209999.BIN` = stock 1.4 or the loader build, hold OK
at power-on; `README.md`); the card updater runs before the loader. Read the payload rules below
first: removing the card does not undo anything a payload saved to flash, EEPROM or settings.

**Never run a USB/PC firmware update with `FENYKL.BIN` on the card** (the payload would run while flash
is written).

`plugin build` needs the toolchain from `toolchain/build.sh`, found in `$FENYKL_TOOLCHAIN`
or else `toolchain/install/` (`fenykl toolchain path` prints which; `toolchain/README.md`). The
optional ISA check (`--isa-check`; `toolchain/README.md` explains what it protects against) also needs
`GHIDRA_INSTALL_DIR` and `JAVA_HOME` set, with the ghidra-mn103 extension installed; that extension
is not part of this repository. Nothing else needs Ghidra. Ghidra may
fail on output directories below a dot directory; use the default `DIR/build`
or a path under `/tmp`.

## Writing a plugin

A plugin is a directory of `.c` (and optionally `.S`) files and a `plugin.toml` naming the cameras it
is written for (a plugin calls firmware functions and addresses of exactly these cameras):

```toml
targets = ["em10ii"]
```

One of the source files defines `plugin_main`:

```c
#include <fenykl.h>

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	fw_dbg_puts("[hello] fenykl payload running\n");
	fw_OlyTaskSleep(1000);
}
```

`plugin_main` runs once in the loader's `FENYKL` task (priority 55, 16 KiB stack) after `.bss` has been
cleared (next section for USB boots). Returning ends the task; the camera
carries on as stock. `fenykl.h` provides:

| item | meaning |
|---|---|
| `fw_<name>()` | firmware functions from the target's `firmware.toml` (generated `fenykl_fw.h`; each prototype carries its address, evidence level and where it was read) |
| `FENYKL_LOAD_BASE`, `FENYKL_REGION_END` | `load` and `load + max_size` from `target.toml`; em10ii: 0x60DA5000, 0x60E0C000 |
| `FENYKL_TASK(fn);` | declares `void fn(void)` and defines the task entry `fn_task` to pass to `fw_OlyTaskCreate` (see below); `fenykl_target.h` |
| `FENYKL_UNCACHED(p)` | uncached alias of a DRAM address (`(p & 0x3FFFFFFF) \| 0x80000000`); `fenykl_target.h` |
| `fenykl_rd8/16/32(a)`, `fenykl_wr8/16/32(a, v)`, `fenykl_dram_ptr(a, align)` | volatile reads and writes of firmware memory; `fenykl_dram_ptr`: `a` lies in the cached DRAM window (`firmware.toml` `dram_start`..`dram_end`) or its uncached alias and is aligned |
| `FENYKL_TIMER_NOW()`, `FENYKL_TIMER_ELAPSED(from, to)` | the free-running 1 MHz timer (30 bit, both cores) and the wrap-safe difference of two readings |
| `fenykl_task_sleep`, `fenykl_fopen`, `fenykl_fwrite`, `fenykl_fclose`, `fenykl_sprintf`, `FENYKL_ORIG(name)` | the same firmware services under SDK names (every target has them), for plugins that name no `fw_` symbol (Body API) |
| `fenykl_irq_save`, `_restore`, `fenykl_task_create`, `_wait`, `_wake` | task and interrupt helpers (`fenykl_target.h`; `fenykl_task_wait` returns early on `fenykl_task_wake`, `fenykl_task_sleep` does not) |
| `memcpy`, `memmove`, `memset`, `memcmp` | linked in (GCC emits calls to them even with `-ffreestanding`) |

There is no libc: no `printf`, `malloc` comes from the firmware (`fw_malloc`/`fw_free`), formatted text
via `fw_sprintf` into a buffer. `float`/`double` work through libgcc soft-float but are slow. There is no
`strlen` either; the examples carry a three-line `length()`.

### Startup and USB boots

`crt0.S` -> `lib/start.c` `fenykl_start`: clear `.bss`, then ask the target's `fenykl_usb_attached()`;
if the camera was switched on by plugging in the USB cable it prints `[fenykl] USB boot, plugin
skipped` to the UART (`fw_dbg_puts`, nothing on the card) and returns: no `plugin_main`, no file I/O,
hooks or tasks. Otherwise it calls `plugin_main`.

Why: without a card reader the card is reached through the camera in USB mass-storage mode, and
plugging in the cable boots the camera. A plugin started on that boot writes to the card while the PC
mounts it (two FAT writers; a plugin once overwrote its own log this way).

`fenykl_usb_attached()` in `arch/em10ii/usb.c` always returns 0: the loader runs a payload only on a
power-switch boot or a relay boot without a cable, so a boot started by the cable (power-on factor 0x400) never reaches the payload
(FENYKL.BIN, E-M10 II loader). A power-switch boot with the cable already in still runs
`FENYKL.BIN`, and connecting the cable to a running camera does not stop a plugin either. For both
cases see "Cable at run time" below.

#### Cable at run time: the card gate

Plugging the cable into a running camera switches it to USB **in place** (no reboot): the plugin keeps
running. In Storage mode the PC's sector reads and writes go through the same file server task
(FILECLBK) as `fw_rpc_fopen`/`fw_rpc_fwrite`, and the camera's FAT stays mounted, so a plugin write while
the PC owns the card corrupts the FAT [static analysis]. Two target
functions (`arch/em10ii/usb.c`, CPU0 task context, not CPU1; linked only when used):

- `fenykl_usb_cable_present()`: `fw_ui_key_get_state(0x400) != 1`, the GIO key "UsbC" (1 = inactive,
  no cable; any other value counts as a cable). The camera reads 1 with no cable; the cable-in value
  is not measured.
- `fenykl_card_mounted()`: `fileif_get_media_status(0)` has the FAT-mounted bit 0x100 and not the
  DCF-mount bit 0x4000 (card removal reads as status 1).

The gate itself is shared (`lib/card.c`, CPU0 task context): `fenykl_card_io_allowed()` is false
while the card is not mounted, and **from the first check that sees a cable (or a boot started by the
cable, `fenykl_usb_attached`) until power-off**; `fenykl_card_usb_seen()` tells that case apart.

Why not reopen after the cable is pulled: the camera keeps its FAT state from before the USB
session, so a write after it could put that state back over what the PC wrote. Records and dirty
config values stay in RAM until power-off; the next boot writes nothing of them.

This also covers a camera switched on with the power button while the cable is in (the loader runs
`FENYKL.BIN` then).

**Rule: a plugin that does its own file writes checks `fenykl_card_io_allowed()` before each
open/write/close batch, skips the batch (keeps or drops the data in RAM) while it is false, and never
keeps a file open.** Once it was false because of a cable it stays false until power-off. The remaining window is one batch (milliseconds) against the second or more the
PC needs to enumerate and mount the card. The logging library does this for its flushes
(`docs/logging.md` section 5.1). The example projects that write files themselves do not check it
yet (no migration): do not plug the cable into a camera that runs them.

Status: not run on the camera.

### `plugin.toml`

| key | meaning |
|---|---|
| `targets` | cameras the plugin is written for (required), e.g. `["em10ii"]` |
| `hooks`, `slots` | firmware functions to hook (not the SDK's own, `sdk_hooks` in `target.toml`), pointer slots to replace (not the SDK's own, `sdk_slots`) (Hooks; Callbacks and slots) |
| `cpu1_unproven` | hooks/slots whose CPU1 use is unproven, acknowledged (Hooks) |
| `cpu1_sources` | source files built as CPU1 code (CPU1; needs the target's `cpu1` capability, which no target has yet) |
| `[capabilities.<name>]` | per-capability part of the plugin (Body API): `sources` (plugin files built only for targets with capability `<name>`; files listed nowhere are built for every target), `hooks` and `cpu1_unproven` (added to the top-level lists for those targets only). `<name>` must be a capability of some target; a file or a hook may be listed in one place only |

### Body API (`include/body.h`)

A plugin meant for several cameras names no firmware symbol (`fw_*`, `FW_*`) itself: it uses the
library APIs, the services above and `body.h`, whose functions read the camera's state (GUI, lens,
battery, the firmware's AF statistics, display and OSD buffers, the colour-matrix bases). The API
is the same everywhere; each camera implements it in `arch/<camera>/fenykl_body.h` (static inline).
Every group but the GUI predicates is a capability in `target.toml`, so `FENYKL_HAS_<NAME>` gates the
plugin code that uses it, and `plugin.toml` builds the files that need it only where it exists:

```toml
targets = ["em10ii"]

[capabilities.lens]                     # only targets with lens
sources = ["feat_focus.c", "feat_dof.c"]
```

| capability | API | E-M10 II source |
|---|---|---|
| (all) | `fenykl_gui_live_view`, `fenykl_gui_menu`, `fenykl_gui_playback` | `gui_super`, `gui_state` |
| `lens` | `fenykl_lens_attached`, `_focus_mm`, `_focal_mm`, `_av`, `_iris_av` | `lens_attached`, `lens_obj_dst`, `lens_focal_length_now` (0 without a lens), prop 0xA03A, `av_effect` (of unknown scale) |
| `batt` | `fenykl_batt_read`, `fenykl_power_src`, `fenykl_power_src_batt` | `batt_vbat`, the 1 Hz check's stored value, and `power_src(_check)` |
| `afc` | also `fenykl_af_mf`, `_half`, `_target_area`, `_all_targets`, `_grid`; the contrast driver protocol for `lib/afc.c`: `fenykl_cnt_*`, `FENYKL_CNT_*` | `take_focus`, `trg_half`, `take_fcs_af_mode2fcs`, the `fcs_*` grid words, fcs's contrast context and fc buffer, the driver's client list `cnt_unit` |
| `shot` | `fenykl_disp_*`, `fenykl_osd_*`, `fenykl_frames_*`, `fenykl_shot_cond`, `fenykl_shot_buf_*`, `fenykl_dram`, `fenykl_cached`, `FENYKL_DISP_*`, `FENYKL_OSD_*`, `FENYKL_FRAMES_*` | display state and registers (`disp_*`), `gpi_*`, `osd_shadow`, `osd_palette`, the PRIM image pool (`pool_prim`, `pool_table`, `imgbuff_alloc`/`_addr`/`_free`) |
| `wb_base` | `fenykl_wb_base_slot`, `_base_head`, `_base_entry_ok`, `fenykl_wb_mode`, `fenykl_wb_lv_matrix`, `FENYKL_WB_BASE_N`, `FENYKL_WB_MAT_N` | the registry words of the colour-matrix base properties 0xAA6A/0xAA4A/0xAA5A, WB prop 0x8121, `wb_lv_cmx` |

`projects/main` is written this way (`tests/test_plugin_build.py` fails on any `fw_`/`FW_` name in it);
`hello` keeps using the firmware directly. The test sees main's own files only: SDK headers main
includes still carry target values (`fenykl.h`'s `FENYKL_TIMER_NOW` reads `FW_TIMER_NOW`, `hud.h`'s
`FENYKL_HUD_*` canvas, states and palette indices are per camera). The `afc` capability covers both
the own contrast client (`lib/afc.c`, with the body's request, contrast_setting, event, DMA buffer,
foreign-client and live-view layouts: `fenykl_cnt_*`) and the firmware client's reads (`fenykl_af_*`).

### Getting output: write a file

`fw_dbg_puts` prints to the internal UART, which is not reachable without opening the camera.
Output you can read goes into a file on the card. The pattern:

```c
static char text[256];

void *fp = fw_rpc_fopen("C:\\FENYKL.LOG", "w");    /* 0 if the card is not mounted or busy */
if (fp) {
	fw_sprintf(text, "value %08x\r\n", value);
	fw_rpc_fwrite(text, 1, length(text), fp);
	fw_rpc_fclose(fp);                           /* flushes; the file is consistent after this */
}
```

- Open, write, close per batch; never keep a file open (the card may be unmounted for USB, or pulled).
- Check `fenykl_card_io_allowed()` before each batch and skip it while false: a USB cable plugged in
  at run time hands the card to the PC, and the gate stays closed until power-off after it (Startup
  and USB boots, card gate).
- Mode `"w"` does **not truncate** (`lib/cfg.c` removes its file first): it overwrites from the start
  and an older, longer file keeps its tail. Delete the file first (`fw_rpc_fremove`), or append with
  `"a"` as the logging library does.
- Use `\r\n` line ends if you read the log on Windows; `fw_sprintf` forwards at most 8 variadic words
  and the build refuses a call with more (split a long line into calls appending to the buffer).
- Do not write every few milliseconds: file writes go through the firmware's file server task, and
  writes concurrent with the camera's own image writing are not proven safe. Log in batches, use a
  test card.

Or use the **logging library** (`sdk/docs/logging.md`, linked into every plugin):
`#include "log.h"`, `fenykl_log_info(&module, "fmt %u", x)` from any task or hook
(ISR context by design, untested); a flusher task appends to `C:\FENYKL.LOG` (one file across
boots, boot header, 4 MiB cap at init).
`projects/main` uses it.

Persistent options: the **configuration library** (`sdk/docs/config.md`, linked into every plugin),
`#include "cfg.h"`, `FENYKL_CFG_BOOL/INT/ENUM` declarations, `C:\FENYKL.CF0`/`CF1` with CRC and sequence number, a
debounced saver task. `projects/main` uses it.

Settings tab: the **ui library** (`sdk/docs/ui.md`), `#include "ui.h"`; a native Custom menu tab
"L. Fenykl" with an info row and one stock row per grouped cfg option (BOOL or ENUM with up to 26 names, at most 40),
drawn and edited by the camera's own menu code. `projects/main` uses it.

### Firmware API (`arch/em10ii/firmware.toml`)

`firmware.toml` declares each firmware function a plugin can call as `fw_<name>`: its address,
signature, evidence, the cores that may call it and, in `note`, where they were read. The generated
`fenykl_fw.h` repeats them above each prototype. Commonly used:

- console: `fw_dbg_puts`, `fw_sprintf`
- tasks: `fw_OlyTaskSleep`, `fw_OlyTaskCreate`, `fw_tslp_tsk`, `fw_OlyTaskWakeup`
- files: `fw_rpc_fopen`, `fw_rpc_fclose`, `fw_rpc_fread`, `fw_rpc_fwrite`, `fw_rpc_fremove`,
  `fw_fileif_fseek`, `fw_fileif_findfirst`, `_findnext`, `_findclose`, `fw_fileif_get_media_status`
- memory and cache: `fw_malloc`, `fw_free`, `fw_dcache_flush_all`, `fw_imgbuff_alloc`, `_addr`, `_free`
- GUI: `fw_gui_post`, `fw_gui_widget_create`, `fw_gui_image_set_handle`
- other: `fw_ui_key_get_state`, `fw_cam_request`, `fw_lens_focal_length_now`

Evidence is the confidence of the function's name and signature.
For every entry the CALL register list and frame were read from the function's own `ret` and checked
against all of its callers in the firmware; argument registers come from callers and the function
body (the `note` field says where).

To add a function, add a table to `firmware.toml`:

```toml
[rpc_fread]
address = 0x605649BB          # run address in main-code-01
saves = ["d2", "d3", "a2", "a3"]  # CALL register list (subset of d2, d3, a2, a3) ...
frame = 0x18                  # ... and frame size; both must equal the callee's RET/RETF
evidence = "high"             # confidence of name and signature
returns = "int"
args = ["a void *buf", "d unsigned size", "d unsigned n", "a void *fp"]
note = "ret [d2,d3,a2,a3],24 at 0x60564A47; returns items read"
```

`args` entries are `<class> <C declaration>`: `d` = integer class (D0, D1, then stack), `a` = pointer
class (A0, A1, then stack), only 32-bit types. A trailing `"..."` makes it variadic: the last named
argument goes to the first stack slot and 8 variadic words are forwarded; a macro of the same name
makes a call with more variadic arguments a compile error. The generator refuses
argument layouts the characterised ABI does not cover (a register argument after a stack argument). Only add functions whose CALL encoding and argument registers were read from the
firmware; unknown signatures stay out.

`cores = ["cpu0", "cpu1"]` with `cores_evidence` marks a function CPU1 code may call (of the
above: `fw_dbg_puts`, `fw_OlyTaskSleep`, `fw_malloc`, `fw_free` and, for its own core,
`fw_dcache_flush_all`; the PSW and cache primitives are per core too). Without `cores` a function is CPU0-only, and
calling it from a `cpu1_sources` file is a compile error (CPU1).

#### Firmware data

Firmware addresses the SDK uses as values (globals, tables, MMIO registers, address-range bounds) are
`[data.<name>]` tables in `firmware.toml`, not constants in the C files. Each becomes
`#define FW_<NAME> 0x...u` in `fenykl_fw.h`, so the libraries and `arch/<camera>/fenykl_body.h` name
the address and each camera's `firmware.toml` supplies it:

```toml
[data.gui_super]
address = 0x619A3740          # the address (unsigned 32-bit)
type = "u8"                   # what lives there; "code" for a function address, "bound" for a range limit
evidence = "high"             # high or medium, as for functions
note = "gui_sm super state; ... Live view = super 0x01 state 0x08, 0x11 = main menu (SsMenu)"
```

```c
/* gui_super, u8, evidence high: gui_sm super state (...) */
#define FW_GUI_SUPER 0x619A3740u
```

The layout behind an address stays in C (a struct, or offsets next to the code that reads them).
`FENYKL_CORE_ID()`, `FENYKL_UNCACHED()` and the other memory-map macros are in `fenykl_target.h`.

### Tasks

```c
FENYKL_TASK(worker);
void worker(void) { for (;;) { /* work */ fw_OlyTaskSleep(100); } }

void plugin_main(uint32_t base, uint32_t size)
{
	void *stack = fw_malloc(0x2000);
	fw_OlyTaskCreate("WORKER", worker_task, 2 /* create and start */, 0, 11, 0x2000, stack);
}
```

`worker_task` gives the GCC function its own frame and ignores what the kernel passes in registers
(D0 = exinf). If `worker` returns, the entry sleeps forever (the task slot is not freed).
`stack_base` is the low end of the stack, as the firmware's own dynamic callers pass a malloc'd block.
`OlyTaskCreate` returns the task id or -1; it rewrites priorities 8..11 to 45/46/54/55.

### Hooks

A hook **replaces or wraps a firmware function**: the hook (a C function) runs in place of the
original, can read and change the arguments and the result, and can call the original. It is an
inline five-byte jump at the function's entry to a generated Panasonic→GCC stub, plus a trampoline
that runs the relocated entry instructions and jumps back (so the original is still callable). This
is the reviewed way to patch firmware code in RAM (Payload rules).

A function is hookable only if `arch/<camera>/firmware.toml` gives it a `hook` table (the target's
data decides what may be hooked, with the exact stock entry bytes and a CPU1-safety field). A plugin
then declares the hooks it uses in `plugin.toml` and binds a handler in C:

```toml
# plugin.toml
targets = ["em10ii"]
hooks = ["cw_updateDisplay"]
cpu1_unproven = ["cw_updateDisplay"]   # required to build a target whose CPU1 scope is unproven
```

```c
#include <fenykl.h>

/* The handler has the hook's C signature (fw_hook_cw_updateDisplay_fn). It may change the
   arguments, call the original via FENYKL_ORIG(<name>)(...) (fw_orig_<name>), change the result,
   or skip the original. */
static void on_update(int show_front)
{
	FENYKL_ORIG(cw_updateDisplay)(show_front);   /* transparent wrap */
}
FENYKL_HOOK(cw_updateDisplay, on_update);

void plugin_main(uint32_t base, uint32_t size)
{
	int r = fenykl_hook_install(&fenykl_hook_cw_updateDisplay);   /* 0 = patched; <0 = not touched */
	/* ... */
	fenykl_hook_remove(&fenykl_hook_cw_updateDisplay);            /* restores the original bytes */
}
```

**SDK hooks.** Some entries are bound by the SDK library itself: `target.toml` lists them in
`sdk_hooks`, the generator emits their stubs for every plugin, and a `plugin.toml` that lists one in
`hooks` is refused at build time (binding it with `FENYKL_HOOK` anyway fails to link: duplicate
`fenykl_hookwrap_<name>`). On the E-M10 II that is `appevent_post`, the event layer
(`docs/event.md`): plugins register event handlers with `fenykl_event_add` and call
`fenykl_event_install` instead of hooking it; the Button Function entries listen through it
(`docs/fnfunc.md`). A plugin that never calls the library gets no byte of
the stub (`--gc-sections`).

`fenykl_hook_install` / `..._remove` mask interrupts, write the patch through the uncached alias,
verify the entry bytes before and after, refresh the I-cache, and are idempotent. They return 0, or
`FENYKL_HOOK_EBYTES` (-1, the entry is not the expected stock bytes — nothing was written) or
`FENYKL_HOOK_EVERIFY` (-2). Call them from a task, never from inside a hook. Nothing is patched until
`install`; returning from `plugin_main` does **not** remove a hook, so `remove` it (or leave it
resident for the boot) explicitly.

Rules and limits:

- **CPU1 caveat.** The install publishes on the installing core only: it does **not** stop CPU1 or
  invalidate CPU1's I-cache. A target not proven CPU0-only needs
  `cpu1_unproven` in `plugin.toml`, and the plugin must watch for CPU1 execution (count by core id at
  `0x8050`). Closing the hole needs a cross-core publication protocol, and none was found in the
  firmware.
- **E-M10 II** (`arch/em10ii/hooks.c`, three cores; `cpu1` scope "unproven" covers CPU1 and CPU2). The
  install masks CPU0's interrupts and dispatch (`irq_lock(0)`), writes the D-cache back
  (`dcache_flush_all`), checks and writes the entry through the uncached alias, writes the D-cache back
  again and invalidates CPU0's I-cache (`fenykl_icache_invalidate`: CHCTR 0x8070 ICEN off, wait for
  ICBUSY clear, ICINV, wait, entry value back; explicit values, the INV bits are never written back as
  read; both waits bounded to 20000 reads). If the readback differs (`FENYKL_HOOK_EVERIFY`) or the
  invalidate times out (`FENYKL_HOOK_EICACHE`, -3), the old bytes are put back. ICBUSY (bit 2) is inferred
  (no stock code of this body reads it); the invalidate was verified on the camera with a probe (2026-10). CPU1/CPU2 are not invalidated: a hook may target a function they can run only if
  (a) the target's 5 patched bytes lie in one aligned 8-byte block (one I-cache line; `firmware.toml`
  hook tables are checked by `tests/test_fwapi.py`), so a core with a stale line runs the complete
  stock entry, (b) the plugin installs and removes it while no other core can be running it (the
  write is not atomic), and (c) the hook passes their calls through. Stubs stay resident for the boot,
  so a stale line holding the jump still works after a remove. A CPU0 task preempted between two
  instructions inside the 5 bytes would resume in the middle of the jump: prefer entries without an
  instruction boundary there (`cw_updateDisplay`); `appevent_post` has two (+1, +3).
  `fenykl_code_patch(addr, from, to, n)` (`fenykl_target.h`) is the same write for any n code bytes
  (only the bytes that differ are stored); `tests/sim/hooks_em10ii` covers it on one byte.
- The generator **refuses** at build time: non-`call` entries, variadic targets, a `relocate` span
  shorter than the jump or not ending on an instruction boundary, and any entry instruction with
  control flow (branches, calls, returns, loops) or a PC/special-register operand — it re-encodes
  only position-independent instructions. One binder per entry (no chaining); an entry several
  features share is bound by the SDK and fanned out by its library (SDK hooks above).
- The hook runs on the original caller's stack and core. Keep it short and non-blocking; do not call
  blocking firmware (file RPC, queue waits) from inside it, and never re-enter the hooked function.
- Only verified scalar signatures (32-bit integer/pointer args and results). Structs, 64-bit values
  and variadics are out, as for thunks.
- Inline only, on CALL-entered functions. For function pointers, vtable methods and callbacks the
  firmware enters with `calls`, use a slot (next section).

### Callbacks and slots

The firmware calls C code through a function pointer in three ways. It can call through a **slot**, a
pointer word in RAM such as an event hook or a change callback. It can call a **vtable method** of a
C++ object. Or it calls a pointer that a firmware function was given, such as a PTP op handler. Each
time it uses the Panasonic ABI, usually `calls (An)`: pointer arguments come in A0/A1, there is no
return slot and no GCC home area, and the callee must keep D2/D3/A2/A3. A C function needs a reverse
entry stub for that. The stub gets a 28-byte frame (return slot, the two home slots, a snapshot of
D0/D1/A0/A1) and calls a generated wrapper that passes the arguments to C by class. It returns the
result in D0 (and in A0 for pointers) and leaves with the RET that matches the caller's CALL.

The target's `firmware.toml` declares what may be used. `[callbacks.<sig>]` is a signature: its
`entry` (`"calls"`, or `"call"` with `saves`/`frame`), `returns`, `args` with register classes as for
functions, `evidence` and `note`. `[slots.<name>]` is a pointer word: its `address`, the `callback`
signature, the `expected` stock value (0 = empty), `cpu1`/`cpu1_evidence` and `note`. Declared on
the E-M10 II:

| name | kind | what |
|---|---|---|
| `menu_row` | `calls` | `int fn(void *actuator)`: row callback of a Custom-menu dispatch or L1 record (`lib/ui_em10ii.c`); 4 = continue with the stock controller |
| `menu_picker` | `calls` | `unsigned fn(void *picker, unsigned code, unsigned param)`: the option picker's +0x48 handler |
| `cam_cnt_frame_cb` | `calls` | `void cb(unsigned unk, unsigned events, void *req)`: contrast client frame callback (request +0x4C, events masked by +0x6C: 1 frame start, 2 data complete, 0x200 error; `lib/afc.c`) |
| `cam_req_done_cb` | `calls` | `void cb(unsigned status, void *req)`: camera request done callback (request +0x38) |

**SDK slots.** The slots of pre-capture and the HUD (`lib/precap_em10ii.c`, `lib/hud_em10ii.c`):
`target.toml` lists them in `sdk_slots`, the generator emits their descriptors for every plugin, and
a `plugin.toml` that lists one in `slots` is refused at build time. An SDK hook or slot whose CPU1 scope is
unproven needs `target.toml`'s acknowledgement, `sdk_cpu1_unproven` (the counterpart of a plugin's
`cpu1_unproven`); the build fails without it.

The C API (`fenykl.h`):

- `FENYKL_CALLBACK(sig, fn)` at file scope, after `fn`, emits the stub for `fn`. The build fails if
  `fn` does not have the signature `fw_cb_<sig>_fn`. `FENYKL_CB(sig, fn)` is then the firmware-callable
  address (`uint32_t`), for a slot or as an argument to a firmware function.
- `fw_call_<sig>(addr, args...)` calls a firmware function pointer with the convention of `sig`.
- For each slot listed in `plugin.toml` `slots`:
  - `FENYKL_SLOT_REPLACE(slot, fn)` stores `FENYKL_CB(<its sig>, fn)`. It works only if the word
    still holds `expected`. Binding `fn` to another signature is a compile error.
  - `fenykl_slot_restore(&fenykl_slot_<slot>)` puts `expected` back, but only if the word still holds
    the replacement.
  - `fw_orig_<slot>(args...)` calls the stock value (not generated for an empty slot).
- Both return 0 on success or if already in that state. Otherwise they return
  `FENYKL_SLOT_EVALUE` (-1: the word did not hold the expected value, nothing was written),
  `FENYKL_SLOT_EVERIFY` (-2) or `FENYKL_SLOT_EBUSY` (-3: already replaced by something else). Call
  them from a task.

A slot write is a compare-and-swap of one aligned word, with interrupts masked on the calling core.
The D-cache is written back before and after it. No code is written, so there is no I-cache step.
The stubs were loaded with the payload.

Rules and pitfalls:

- **CPU1 gate.** A slot whose `cpu1` is `"unproven"` builds only if `plugin.toml` lists it
  in `cpu1_unproven`, as for hooks. A pointer swap is safer across cores than an inline patch: every
  value a reader can see is a complete, valid target. But the masking and the cache write-back act on
  this core only. Count calls by core (`0x8050`) and report any CPU1 call.
- **Direct calls bypass a slot.** A slot only catches calls made through that word. A direct
  `call` to the stock target, or a caller that loaded the pointer before the swap, is not
  intercepted. Choose the slot from the call sites (`note`).
- **A vtable word is shared.** A slot inside a class vtable changes the method for every object of
  the class.
- **Retain everything.** Stubs, wrappers and descriptors stay in the payload for the whole boot. A
  call that loaded the old pointer can still be running after a restore.
- **Keep replacements short.** The replacement runs on the firmware caller's task and stack, and
  can run often. No file I/O, no blocking calls, and no calls back into the function you replaced,
  except through `fw_orig_*`.
- **Settings persist.** Changing a setting or a stored value in RAM can be saved at power-off
  (Payload rules). A test replacement must forward everything unchanged.
- Only scalar signatures (32-bit integer or pointer arguments and results), as for thunks. Variadic
  callbacks are refused at build time.

### CPU1

`plugin.toml` `cpu1_sources` lists files built as CPU1 code (`-DFENYKL_CPU1`: CPU0-only firmware calls
do not compile; the `lib/*.c` files then get `-DFENYKL_PLUGIN_CPU1=1`, which gives the logging library
its CPU1 ring). Such a plugin builds only for a target with the `cpu1` capability, and no target has
it: the E-M10 II has no CPU1 runtime yet. Data both cores use is `FENYKL_SHARED` (section `.shared`,
whole 64-byte lines, zeros in the file), accessed only through `FENYKL_SHARED_PTR(var)`: the cores
have no cache coherency. `fenykl plugin build` and `fenykl payload pack` print the `.shared`
placement, and so does `fenykl payload info FENYKL.BIN --elf plugin.elf`.

## ABI rules

- Plugin code is GCC mn10300 ABI; the firmware uses the Panasonic ABI. **Every call into the firmware
  goes through a generated thunk** (`fw_*`); never call a firmware address through a C function pointer.
- The firmware passes integers in D0/D1 and pointers in A0/A1, extra arguments from (4,SP); the CALL
  instruction carries the callee's register list and frame size. The thunks handle both.
- **Never CALL C code with a non-empty register list or frame** (e.g. from a firmware patch): use
  `call x,[],0` or `calls`, from a stub that provides 12 bytes (return slot and GCC's home area for
  D0/D1), as `arch/em10ii/crt0.S` does.
- Callbacks from the firmware into C need a reverse entry stub, `FENYKL_CALLBACK` (Callbacks and
  slots), even with integer arguments only: a `calls` caller provides no GCC home area.
- Struct and 64-bit arguments to firmware functions are not characterised.
- Do not use `-mrelax`, `-mam33` or any CPU option; the build uses `toolchain/cflags` unchanged.

## Payload rules

- **Block, never spin.** `FENYKL` runs at priority 55, like the watchdog task PER_WDT. A busy loop
  in a task that outranks PER_WDT starves it and the watchdog resets the camera. Wait with `fw_OlyTaskSleep` (or a queue/semaphore
  wait), also in your own tasks. Recovery from a bad payload: remove the card or delete the file.
- **Nothing persistent.** Removing the card undoes only what does not persist. A payload runs with full
  firmware rights; corrupted settings in RAM can be saved to flash/EEPROM at power-off, and boot code
  that runs before the card-updater decision could then fail on every boot (a brick the card updater
  cannot fix). Payloads must not write flash, EEPROM or settings, nor touch the camera's setting
  structures, without a reviewed design.
- **Memory:** code, data and `.bss` live in 0x60DA5000..0x60E0C000 (412 KiB incl. the 28-byte
  header); the link fails if they do not fit. The `FENYKL` task stack follows at 0x60E0C000..0x60E10000.
  Bigger buffers: `fw_malloc`, at most 0x4000 bytes per call (larger requests return NULL).
- **Caches:** buffers handed to file or DMA code should be uncached addresses
  (`FENYKL_UNCACHED(buf)`); read results back through the uncached alias, or call
  `fenykl_dcache_flush()` first.
- **No interrupt context, no long work in callbacks:** the payload runs as a normal task, started at the end of the
  startup task Usr3.
- Files: drive 0 is the SD card, paths `"C:\\NAME.EXT"`, `fw_rpc_*` go through the firmware's file
  server task (synchronous, safe from any task).
- Patching firmware code in RAM is allowed **only through the hook mechanism** (Hooks above), which
  is the reviewed design for it: a declared, byte-verified entry,
  masked IRQs, cache handling and an explicit CPU1-safety field. Hand-writing to firmware code
  otherwise is not allowed, nor is changing cache/interrupt control registers directly or using AM33
  instructions (`--isa-check` rejects them), nor writing to flash.

## What the build does

`fenykl plugin build DIR [--target CAMERA] [-o OUTDIR]` (default `DIR/build`), for each camera in
`DIR/plugin.toml` (or only `CAMERA`), into `OUTDIR/<camera>/`, with the files of `arch/<camera>/`:

1. generates `gen/fenykl_fw.h`, `gen/fw_thunks.S`, `gen/fw_symbols.ld` from `firmware.toml`
   (function addresses are link-time symbols, as an assembler `.equ` would encode the CALLs wrongly;
   `[data]` addresses are `FW_<NAME>` constants in the header);
2. compiles `crt0.S`, `arch/<camera>/*.c`, the `lib/*.c` files of its capabilities (a
   `lib/<name>_<camera>.c` only for that camera), the thunks and `DIR/*.c`, `DIR/*.S` (less those under a `[capabilities.<name>]` the camera lacks) with the target's `cflags`
   (em10ii: `toolchain/cflags`), `-D FENYKL_LOAD_BASE/FENYKL_REGION_END` from `target.toml`,
   `-DFENYKL_TARGET_<CAMERA>=1` and
   `-DFENYKL_HAS_<CAPABILITY>=1` for each of the target's `capabilities`, and
   `include/`, `arch/<camera>/` on the include path; the files in `cpu1_sources` also get
   `-DFENYKL_CPU1`; the `lib/*.c` files get `-DFENYKL_PLUGIN_CPU1=0|1` (whether the plugin has
   `cpu1_sources`);
3. links with `link.ld` (`--gc-sections`, which drops the library code a plugin does not use,
   `--orphan-handling=error`, map file `plugin.map`);
4. with `--isa-check`, runs the target's `isa_check` (em10ii: `toolchain/isacheck/check_isa.py`) on
   `plugin.elf` and fails on any error;
5. packs `FENYKL.BIN` (`fenykl payload pack plugin.elf FENYKL.BIN` does only this step; it finds the
   camera by the ELF's link address).

A Python command rather than a Makefile: the generator and the packer are Python anyway, the build is
a fixed sequence over a handful of files (well under a second, plus the ISA check if asked for), and it is tested
like the rest of fenykl.

### FENYKL.BIN

```
0x00 u32     magic  0x324C4E46 ("FNL2")
0x04 u32     load   0x60DA5000
0x08 u32     size   whole image incl. header, multiple of 4, 0x1C <= size <= 0x67000
0x0C u32     entry  offset of the entry point from load (0x1C = _start), 0x1C <= entry < size
0x10 u32     sum    sum (mod 2^32) of the little-endian words in [0x1C, size)
0x14 char[8] model  "E-M10II", zero-padded
0x1C ...            code and data, linked at 0x60DA501C
```

#### E-M10 II loader: which boots, which file

The `em10ii/fenykl-loader` patch hooks the end of the startup task Usr3 and starts the `FENYKL` task
(priority 55, 16 KiB stack) only on these boots:

| boot | power-on factor (`*(u32 *)0xA1C17C14`) | conditions |
|---|---|---|
| power switch | 12 | Usr3 posted the card mount (`*(u16 *)0x61843F64 == 1`) |
| relay: battery connected with the switch already On | 0x1000001 | switch input On, Usr3's battery test passed, no USB cable (`ui_key_get_state(0x400) == 1`) |

All other boots run no payload: OK held (card update), the USB cable (0x400), card insertion
(0x1506), a battery insertion with the switch Off, and 0x1000002 (expected for a watchdog reset, not
measured on the camera). On a relay boot the gate word is 0 and Usr3 did not mount the card: the
application did, so its power-off does not send Usr3's unmount either (stock behaviour on that
boot). A payload that needs to know which boot it runs on reads the factor word: 12 or 0x1000001.

The task waits up to 30 s until the card is ready (`fileif_get_media_status(0) & 0x4100 == 0x100`),
then:

1. With a USB cable in (or an unknown cable state) a relay boot loads nothing; a power-switch boot
   loads only `C:\FENYKL.BIN`, as earlier loaders did.
2. Otherwise `C:\FENYKL.TST` first: if it opens, it is read, closed and deleted (`rpc_fremove`: the
   directory entry is written when it returns; the freed clusters may reach the card only at a later
   flush, so a crash can leave lost clusters), and the loader checks with
   `fileif_exists` that it is gone. Only then the header checks run. A TST that cannot be deleted
   (read-only attribute, locked card) is never run.
3. If the TST is absent, could not be deleted or fails a check, `C:\FENYKL.BIN` is tried on the same
   boot. `FENYKL.BIN` is never deleted.

The payload is entered the same way for both files (D0 = load base, D1 = image size) and cannot tell
them apart; a test build that must know embeds it itself. Use `FENYKL.TST` for untested code: a crash
or hang costs one boot, and the next boot runs the stable `FENYKL.BIN`. Whether a watchdog reset after a crash is refused like the
other non-payload boots is not yet measured on the camera; until it is, a `FENYKL.BIN` that crashes at
boot may reboot-loop on a relay-powered camera (switch Off or remove the card to stop it).

The format is the same for every camera; `load`, the size limit and `model` are the em10ii values
(`load`, `max_size`, `model` in `target.toml`). The loader rejects a file whose model field is not
exactly its own camera's (all 8 bytes, padding included), so a payload built for another camera never
runs. The loader reads at most 0x67000 bytes and calls `load + entry` with `calls`, D0 = load base,
D1 = image size. `fenykl payload info` finds the camera by the header's `model`, shows the header and
applies that loader's checks in the loader's order (`payload.check_image`).

## Layout

| path | purpose |
|---|---|
| `include/fenykl.h` | SDK header, camera-independent; includes `fenykl_fw.h` and `fenykl_target.h` |
| `include/log.h`, `include/cfg.h`, `include/event.h`, `include/fnfunc.h`, `include/hud.h`, `include/ui.h`, `include/shot.h`, `include/precap.h`, `include/devmon.h` | logging, configuration, event layer, Button Function entries, HUD, settings tab, screenshot, pre-capture and dev monitor library APIs |
| `include/body.h` | body API: camera state by capability, implemented per camera in `arch/<camera>/fenykl_body.h` (Body API) |
| `lib/` | SDK library, linked into every plugin (`--gc-sections` drops what is unused) |
| `lib/start.c` | clears `.bss`, skips a USB boot (`fenykl_usb_attached`), calls `plugin_main` |
| `lib/string.c` | `memcpy`, `memmove`, `memset`, `memcmp` |
| `lib/slots.c` | `fenykl_slot_replace` / `..._restore`, the word written with the D-cache written back before and after (`fenykl_dcache_flush`) |
| `lib/card.c` | card gate `fenykl_card_io_allowed`: closed while the card is not mounted and until power-off once a cable was seen |
| `lib/log.c`, `lib/log_rec.c`, `lib/log_int.h` | logging library: `log_rec.c` record writers (CPU1 code, both cores), `log.c` flusher, init, tee; design `docs/logging.md` |
| `docs/logging.md` | logging library reference |
| `lib/cfg.c` | configuration library: load, parser, save, saver task; doc `docs/config.md` |
| `lib/event.c` | event layer: the SDK's `appevent_post` hook runs the plugin's handlers by priority; doc `docs/event.md` |
| `lib/fnfunc_em10ii.c` | Button Function entries: a plugin function in the stock function pickers of Fn1, Fn2, Fn3, REC and L-Fn, with a press callback; doc `docs/fnfunc.md` |
| `lib/hud.c` | HUD library: live-view text elements and their rendering; doc `docs/hud.md` |
| `lib/hud_int.h`, `lib/hud_em10ii.c`, `lib/hud_rle.c` | its camera parts: the GUI image widgets (three slots), visibility (INFO-screen property), output device and palette pick, and the fmt 0x03 encoder of their bitmaps |
| `lib/shot.c`, `lib/shot_pix.c`, `lib/shot_int.h` | screenshot library: the panel (live view and OSD layers) to `C:\SHOTnnnn.PPM` in strips, `shot_pix.c` the firmware-free pixel work; doc `docs/shot.md` |
| `lib/precap_em10ii.c` | pre-capture, E-M10 II only (silent sequential H): injected full press, frames held in the Base sequence's slot wrappers, commit and cancel, slot install and restore, fail-safes; doc `docs/precap.md` |
| `lib/devmon_em10ii.c`, `lib/devmon_cpu1_em10ii.c`, `lib/devmon_proto.c`, `lib/devmon_int.h` | dev monitor, opt-in (`[capabilities.devmon]` in plugin.toml): CPU0 monitor and test program tasks, the CPU1 server, the protocol helpers; doc `docs/devmon.md` |
| `tp/` | test programs of the dev monitor: `tp_crt.c` (header), `tp.ld` (slot link), `examples/`, `sampler/` (the memory sampler of `fenykl dev sample`); API `include/tp.h` |
| `lib/ui.c`, `lib/ui_int.h` | settings tab library, option core: the list of the cfg options and its limits, the values and cfg calls, install start, stock word check, "not saved" state; doc `docs/ui.md` |
| `lib/ui_em10ii.c` | its E-M10 II tab: Custom tab "L. Fenykl" on the borrowed tab id 0x8F1, native rows, the stock option picker patched per row |
| `arch/em10ii/target.toml` | model (FENYKL.BIN header), toolchain, cflags file, ISA check, ELF machine, payload `load` and `max_size`, `sdk_hooks`, `sdk_slots`, `sdk_cpu1_unproven`, `capabilities`, `[stock]` firmware identity, `[assets]` GUI asset layout of the stock image (the image table spans two resource slots) |
| `arch/em10ii/loader/` | the camera's boot loader patch: `loader.S`, `loader.ld`, `loader.lst`, `patch.toml` (pinned bytes) |
| `arch/em10ii/firmware.toml` | hand-curated firmware API: functions, hooks, callbacks, slots, data addresses |
| `arch/em10ii/link.ld` | payload linker script (0x60DA501C, 0x67000 incl. header; must agree with `load`/`max_size`; packing checks it) |
| `arch/em10ii/crt0.S` | entry `_start`: frame for C, calls `fenykl_start` |
| `arch/em10ii/fenykl_body.h` | the body API (`include/body.h`) on `firmware.toml` data and functions |
| `arch/em10ii/fenykl_target.h` | `FENYKL_UNCACHED`, `FENYKL_TASK`, `FENYKL_LIB_TASK_PRIO` 55, `fenykl_dcache_flush`, `struct fenykl_slot`, the I-cache API, `FENYKL_SHARED` |
| `arch/em10ii/hooks.c` | `fenykl_hook_install` / `..._remove`, `fenykl_code_patch`, CPU0 I-cache invalidate (compiled into every plugin) |
| `arch/em10ii/usb.c` | `fenykl_usb_attached` (0: the loader runs no payload on a cable boot); `fenykl_usb_cable_present`, `fenykl_card_mounted` for the card gate (compiled into every plugin) |
| `projects/*/plugin.toml` | cameras the example is written for (all: `em10ii`) |
| `projects/` | the example plugins, indexed in `projects/README.md` |

### Adding a camera

1. Create `arch/<camera>/` with the five files above; `<camera>` is the short id (`em10ii`, ...).
   `load` must differ from every other target's (`payload info`/`pack` identify the camera by it).
2. A different CPU also needs its toolchain (`toolchain = "<prefix>"`), its own `crt0.S`,
   `FENYKL_TASK`, `hooks.c` and a thunk/hook generator: `py/fenykl/fwapi.py` only knows MN10300 GCC->Panasonic thunks.
   Omit `isa_check` if there is none.
3. Implement `int fenykl_usb_attached(void)` (`include/fenykl.h`) in `arch/<camera>/`; it returns
   0 if the camera cannot tell a boot started by the USB cable. Implement `fenykl_usb_cable_present`
   and `fenykl_card_mounted` there too: the shared card gate (`lib/card.c`) is built on them.
4. List the SDK features it supports in `capabilities` (`target.toml`). Each becomes
   `FENYKL_HAS_<NAME>=1` in every compile, next to `FENYKL_TARGET_<CAMERA>=1`. `cpu1` marks a
   CPU1 runtime, which no target has yet; a plugin with `cpu1_sources` does not build for a camera without it.
   `event`, `fnfunc`, `hud`, `ui`, `afc`, `shot` and `precap` select library files (`LIB_CAPABILITIES`
   in `py/fenykl/plugin.py`): without them `lib/event.c`, `fnfunc_em10ii.c`, `hud.c` + `hud_rle.c`, `ui.c`, `afc.c` +
   `afc_sm.c`, `shot.c` + `shot_pix.c`, `precap_em10ii.c` are not built. `ui` also needs the
   camera's page, `lib/ui_<camera>.c`.
   `afc`, `lens`, `batt`, `shot` and `wb_base` are groups of `include/body.h`: list one once
   `arch/<camera>/fenykl_body.h` implements it (the file must exist, with the GUI predicates, before
   a plugin including `body.h` builds). Plugin files under `[capabilities.<name>]` follow them.
5. Give `firmware.toml` a `[data.<name>]` entry for every `FW_<NAME>` the libraries and
   `arch/<camera>/fenykl_body.h` use (the build fails on a missing one), and `target.toml` an `[assets]` table if
   `fenykl assets dump` should read its stock image.
6. Add `<camera>` to the `targets` of the plugins that were written for it.

Tests: `tests/test_payload.py` (format, packing, loader checks), `tests/test_fwapi.py` (argument
placement, declaration checks), `tests/test_sim.py` + `tests/sim/` (in `mn10300-elf-run`, against
mocks at the real firmware addresses: the hook runtime, the Button Function entries, the settings tab, the
screenshot, the contrast client's state machine and the main plugin's pure parts),
`tests/test_plugin_build.py` (example builds; their ISA check is skipped without Ghidra).

## Firmware tools

The `fenykl` CLI also inspects, unpacks and patches the firmware itself.

### Tests

`uv run pytest -q`. The tests need the mn10300-elf toolchain. The tests that read the stock
firmware (unpack format, patching, assets, HUD, the loader build that is byte-identical to the one
flashed on the camera) need `firmware/OLY_E_120_1400_0000_0000.BIN` and are skipped without it; a
file there with the wrong sha256 fails them. The ISA check tests of the example plugins run only
with Ghidra (`GHIDRA_INSTALL_DIR`) and are skipped otherwise.

### CLI

Every command that reads a `.BIN` also accepts the `.BIN.lzma` files the stock
firmwares are stored as.

```sh
fenykl info FILE.BIN              # size, sha256, known target, per-block header fields and checksums
fenykl unpack FILE.BIN OUTDIR     # one unscrambled body per block + manifest.json
fenykl pack DIR OUT.BIN           # inverse of unpack, checksums recomputed
fenykl patch list                 # registered patches (<camera>/fenykl-loader: the FENYKL.BIN boot loader)
fenykl patch apply PATCH... --output OUT.BIN [--input STOCK.BIN]  # default input: firmware/
fenykl plugin build DIR [--target CAMERA] [-o OUTDIR] [--isa-check]  # C plugin -> OUTDIR/CAMERA/FENYKL.BIN per camera in DIR/plugin.toml
fenykl payload pack PAYLOAD.ELF FENYKL.BIN
fenykl payload info FENYKL.BIN    # FNL2 header, camera, and whether its boot loader would accept it
fenykl assets dump [FILE.BIN] [-t CAMERA] [-o DIR]  # built-in fonts, icons, GUI images as PNGs
```

### Firmware assets

`fenykl assets dump` decodes the GUI graphics built into a stock firmware, so plugins can reuse
them. Without `FILE.BIN` it reads the stock image of `-t CAMERA` (default `em10ii`) from
`firmware/`; any other image is identified by sha256 and rejected unless its camera's `target.toml`
has an `[assets]` layout (E-M10 II v1.4). Output, by default in
the git-ignored `out/assets/`:

```
icons/A034.png                  inline icons by char code (`@A034@` in UI strings), 2 bpp
images/0059.png                 GUI images by id, 4 or 8 bpp
fonts/enu_id15_k24/U+0041.png   glyphs per language, font id and size key, 2 bpp
index.csv                       kind, id, font, address, width, height, fmt, file
```

The PNGs are indexed: pixel values are the firmware's raw colour indices. 2-bpp glyphs and icons
get a grey ramp (0 transparent, 3 white); images get the GUI palette the firmware most likely
uses (`palette_4bpp`, `palette_8bpp` of `[assets]`), so some selected/gradient variants show odd colours.
`address` is where the encoded bitmap sits in camera DRAM, so a plugin can draw the firmware's
own copy instead of embedding one. Glyph addresses are valid only while that language is
selected; the font resource is reloaded on a language change.

**The output is copyrighted firmware content: never commit it.** The bitmap formats, tables and
palettes are decoded by `py/fenykl/assets.py`; the per-firmware addresses are the `[assets]` table
of `sdk/arch/<camera>/target.toml`.

### Patches

A patch targets one exact stock firmware (`fenykl.firmware.Target`: model,
version, file name, sha256 and size of the decompressed `.BIN` and the real load address of each
block once known, from the `[stock]` table of `sdk/arch/<camera>/target.toml`).
`patch apply`:

1. refuses an input whose sha256 is not the patch target's,
2. resolves every edit against the unpacked stock firmware and refuses any edit
   whose original bytes do not match its sha256,
3. refuses overlapping edits, also across patches,
4. writes the edits into the unscrambled block bodies, repacks with fresh
   checksums, and verifies that the output unpacks to exactly the patched blocks.

Patches are `Patch` objects in `py/fenykl/patching.py`, registered in `PATCHES`. An edit is a
`ByteEdit(block, offset, original_sha256, replacement)`: the block by kind name as printed by
`fenykl info` (`main-code-01`, `main-lang`, ...), the offset into the unscrambled body.

The only patch is the FENYKL.BIN boot loader, one per camera, registered as `<camera>/fenykl-loader`
(`em10ii/fenykl-loader`). Everything camera-specific about it is in `sdk/arch/<camera>/`:
`loader/loader.S`, `loader/loader.ld` and `loader/loader.lst` (source, linker script with the hook site,
cave and firmware symbols, disassembly), `loader/patch.toml` (description and the pinned edits: block,
run address, sha256 of the stock bytes, replacement bytes), and `target.toml` (`model` checked by the loader in the
FENYKL.BIN header, payload `load`/`max_size`, `[stock]` firmware identity and block load addresses).
A second camera is a new `sdk/arch/<camera>/` with these files; no Python changes. Its loader test is
`tests/test_fenykl_loader.py` with the simulator harness in `tests/loader_sim_em10ii/`.

### Python layout

```
py/fenykl/
  cli.py            fenykl command
  firmware.py       .BIN block format, (un)scrambling, checksums, (un)packing, unpacked
                    directory format, known stock firmwares (sdk/arch/*/target.toml [stock])
  patching.py       ByteEdit, Patch, apply_patches, the per-camera fenykl-loader patches
  payload.py        FENYKL.BIN format, ELF packing, loader checks
  sdk.py            SDK cameras (sdk/arch/<camera>/target.toml)
  fwapi.py          firmware.toml -> C header, MN10300 thunks, address symbols
  plugin.py         plugin build (plugin.toml, per camera)
  assets.py         GUI font/icon/image decoder and PNG dump (assets dump)
firmware/           drop-in dir for stock images (git-ignored)
sdk/                C plugin SDK, camera parts in sdk/arch/<camera>/ (this file)
toolchain/          mn10300-elf toolchain build and checks (toolchain/README.md)
tests/
```
