# fenykl plugin SDK

Write C, run one command, copy `FENYKL.BIN` to the card root. The boot loader (fenykl patch
`epl3/fenykl-loader`, v3) reads `C:\FENYKL.BIN` into RAM at every normal boot, checks it and calls
it. No reflashing per plugin: the firmware is flashed once.

The SDK is split per camera (`arch/<camera>/`, see Layout). The main target is `epl3` (E-PL3,
MN10300); the firmware API, ABI, payload and FENYKL.BIN sections below describe it. `em10ii` (OM-D
E-M10 Mark II v1.4, three MN10300 cores) has the runtime, `string`, `log`, `cfg`, the settings page
(`lib/ui.c` + `lib/ui_em10ii.c`), the HUD library (`lib/hud.c` + `lib/hud_em10ii.c`), the body API's
`batt` group, slots (data pointer words, `lib/slots.c`) and code hooks
(`arch/em10ii/hooks.c`); `main` builds for it with logging, configuration, the settings page and the
HUD's battery percentage (runs on the camera; no Fn override) and `hello` on the log library.

## Status

| what | state |
|---|---|
| loader firmware (`epl3/fenykl-loader`, FNL2 header with model check, sha256 `ac1db3e8…9c32`) | **works on the camera**: stock behaviour without `FENYKL.BIN`; `hello` and `main` run |
| broken images ignored by the loader | simulator only (`tests/test_fenykl_loader.py`) |
| RAM holes the loader and plugins use | clean in two short camera runs; no long run |
| callbacks (`FENYKL_CALLBACK`) | **works on the camera** through `lib/afc.c` (`projects/main`) |
| slots (`FENYKL_SLOT_REPLACE`) | E-PL3: simulator only (`tests/test_sim.py`); E-M10 II: through the settings page below |
| CPU1 worker (`fenykl_cpu1_*`, `cpu1_sources`) | **works on the camera**: clean start/stop in the bird detector (`projects/birddetect6`) |
| other thunks (`fw_OlyTaskCreate` from C, `fw_malloc`/`fw_free`, `fw_dcache_purge_all_irqsafe`, `fw_dbg_puts*`) | used on the camera by the libraries and plugins; `fw_dbg_puts*` output is invisible without the internal UART |
| logging library (`lib/log*.c`) | **works on the camera**: init, `"a"` append, overflow counters, CPU1 ring, hook and console tee, flushes into standby. Not run: wake from standby, the 4 MiB cap. `fw_malloc` refuses more than 0x3FFC bytes |
| configuration, HUD and settings page libraries (`lib/cfg.c`, `lib/hud.c`, `lib/ui.c` + `lib/ui_epl3.c`) | simulator (`tests/`); **on the camera** (E-PL3) through `projects/main` |
| E-M10 II settings page (`lib/ui.c` + `lib/ui_em10ii.c`) and configuration | simulator (`tests/test_sim.py`, `ui_em10ii`); **works on the camera** in `projects/main` (2026-10-21 and 2026-10-23): "Fenykl" row, page open, RIGHT stepping the value, MENU popping the page, half press closing the menu, reopen after a half press, `cfg` save to `C:\FENYKL.CF0` and reload after a power cycle; all on CPU0. Not run: dials, LEFT, several rows |
| event and key layers (`lib/event.c`, `lib/key.c`) | simulator (`tests/test_sim.py`); **on the camera** through `projects/main`, counters clean (`docs/key.md` 4) |
| USB boot skip (`fenykl_usb_attached`, power-on factor 0x60551630 == 0x400) | **works on the camera**: a cable-started boot skips the plugin, a power-button boot runs it |
| card gate at run time (`fenykl_usb_cable_present`, `fenykl_card_io_allowed`; the logging library holds its flushes) | **on the camera**: the gate closed on a cable plugged into a running E-PL3; reopening after the USB session corrupted the FAT (2026-10-19), so the gate now stays closed until power-off once a cable was seen; that rule: simulator (`tests/test_log_sim.py`, `tests/test_cfg_sim.py`) |
| E-M10 II HUD (`lib/hud.c` + `lib/hud_em10ii.c`: INFO-screen property, LCD/EVF region, palette pick below 0x80, camera font and live-view images) | simulator (`tests/test_hud_sim.py`: text with the E-M10 II font and images, the visibility predicate, output device, palette pick); camera (a probe, 2026-10: drawing on LCD and EVF, INFO values, index 0x00 transparent in the EVF); `projects/main`'s battery percentage (2026-10-23: on the basic and histogram INFO screens, hidden in MENU, the Super Control Panel and playback) |
| E-M10 II hooks (`arch/em10ii/hooks.c`, CPU0 I-cache invalidate) | simulator (`tests/test_sim.py`, `hooks_em10ii`); the I-cache invalidate verified on the camera with a probe (2026-10); a hook probe installed its hooks on the camera (2026-10-19) and removed them cleanly, CPU0 only (2026-10-20) |

## Development loop

```sh
cd fenykl
uv run fenykl plugin build sdk/projects/hello            # -> sdk/projects/hello/build/epl3/FENYKL.BIN
uv run fenykl payload info sdk/projects/hello/build/epl3/FENYKL.BIN   # must end with "loader accepts"
```

`plugin build` builds every camera listed in the plugin's `plugin.toml`, each into `build/<camera>/`;
`--target epl3` builds only that one (and fails if `plugin.toml` does not list it). `--isa-check` also
runs the target's ISA check; it is optional and needs Ghidra with an MN10300 extension that is not
part of this repository (see below).

1. Copy `build/epl3/FENYKL.BIN` to the **card root** (not `DCOLYMP/`). The card must not hold
   `DCOLYMP/E0869999.BIN`, and do not hold OK at power-on: that would start a firmware update.
2. Power on. The loader waits for the card (normally a second or two), runs the payload in its own task,
   and the camera carries on.
3. Power off, read what the payload wrote (`C:\FENYKL.LOG` by convention) on a PC.
4. Next iteration: rebuild, overwrite `FENYKL.BIN`, repeat. Nothing is flashed.

**If a payload crashes or hangs the camera:** power off (battery out if needed), remove the card, delete
or rename `FENYKL.BIN` on a PC. Without the file the camera boots like stock. If it does not boot even
without the card, use the recovery card (`DCOLYMP/E0869999.BIN` = stock 1.6 or the loader build, hold OK
at power-on; `README.md`); the card updater runs before the loader. Read the payload rules below
first: removing the card does not undo anything a payload saved to flash, EEPROM or settings.

**Never run a USB/PC firmware update with `FENYKL.BIN` on the card** (the payload would run while flash
is written).

`plugin build` for `epl3` needs the toolchain from `toolchain/build.sh`, found in `$FENYKL_TOOLCHAIN`
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
targets = ["epl3"]
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

`plugin_main` runs once in the loader's `FENYKL` task (priority 11, 16 KiB stack) after `.bss` has been
cleared, except on a boot started by the USB cable (next section). Returning ends the task; the camera
carries on as stock. `fenykl.h` provides:

| item | meaning |
|---|---|
| `fw_<name>()` | firmware functions from the target's `firmware.toml` (generated `fenykl_fw.h`; each prototype carries its address, evidence level and where it was read) |
| `FENYKL_LOAD_BASE`, `FENYKL_REGION_END` | `load` and `load + max_size` from `target.toml`; epl3: 0x6EA70000, 0x6EAEA000 |
| `FENYKL_TASK(fn);` | declares `void fn(void)` and defines the task entry `fn_task` to pass to `fw_OlyTaskCreate` (see below); epl3 `fenykl_target.h` |
| `FENYKL_UNCACHED(p)` | uncached alias of a DRAM address (`(p & 0x3FFFFFFF) \| 0x80000000`); epl3 `fenykl_target.h` |
| `fenykl_rd8/16/32(a)`, `fenykl_wr8/16/32(a, v)`, `fenykl_dram_ptr(a, align)` | volatile reads and writes of firmware memory; `fenykl_dram_ptr`: `a` lies in the cached DRAM window (`firmware.toml` `dram_start`..`dram_end`) or its uncached alias and is aligned |
| `FENYKL_TIMER_NOW()`, `FENYKL_TIMER_ELAPSED(from, to)` | the free-running 1 MHz timer (30 bit, both cores) and the wrap-safe difference of two readings |
| `fenykl_task_sleep`, `fenykl_fopen`, `fenykl_fwrite`, `fenykl_fclose`, `fenykl_sprintf`, `FENYKL_ORIG(name)` | the same firmware services under SDK names (every target has them), for plugins that name no `fw_` symbol (Body API) |
| `memcpy`, `memmove`, `memset`, `memcmp` | linked in (GCC emits calls to them even with `-ffreestanding`) |

There is no libc: no `printf`, `malloc` comes from the firmware (`fw_malloc`/`fw_free`), formatted text
via `fw_sprintf` into a buffer. `float`/`double` work through libgcc soft-float but are slow. There is no
`strlen` either; the examples carry a three-line `length()`.

### Startup and USB boots

`crt0.S` -> `lib/start.c` `fenykl_start`: clear `.bss`, then, unless `plugin.toml` sets
`run_on_usb = true`, ask the target's `fenykl_usb_attached()`; if the camera was switched on by
plugging in the USB cable it prints `[fenykl] USB boot, plugin skipped` to the UART (`fw_dbg_puts`,
nothing on the card) and returns: no `plugin_main`, no file I/O, hooks or tasks. Otherwise it calls
`plugin_main`.

Why: without a card reader the card is reached through the camera in USB mass-storage mode, and
plugging in the cable boots the camera. A plugin started on that boot writes to the card while the PC
mounts it (two FAT writers; a plugin once overwrote its own log this way).

The epl3 signal (`arch/epl3/usb.c`), static analysis only:

| what | evidence |
|---|---|
| `usr_start_tasks` 0x6E709014 calls the PMU init 0x6E6340C0, which turns the wake register 0x98E00048 into the boot cause index `(u16)0x6044151A` (bit 0x100 -> 9, PMU flag `0x98E00000 & 2` clear -> 0, else the lowest set bit of 0..6 -> 1..7, none -> 10); bit 3 -> index 4 | [verified] decompile |
| `ui_key_gio_init` 0x6E7121D5 (from 0x6E70EEA8, also in `usr_start_tasks`) maps the index through the table 0x6E914248 {0x1000001, 0xC, 0, 0x1506, **0x400**, 0x121C, 0x1000003, 0x1000003, 0x1000002, 0x1000001} into the power-on factor word **0x60551630** (index >= 10 -> 0x1000001, the power key path); getter 0x6E70FEF6 | [verified] decompile, table bytes |
| 0x400 is the GIO key "UsbC" (key kind 1, map 0x6E914270 next to 0xC "Powr", 0x1506 "MdaC", 0x121C "LpSw") | [verified] key code; physical meaning from the debug name [inferred] |
| the firmware treats factor 0x400 as a USB boot itself: `usr_start_tasks` sleeps 1000 ticks when it is 0x400 (0x6E70949D) and tests `ui_key_get_state(0x400)` for other boots; `OlyHfwPower::getWakeupIssue` 0x6EBD95A4 maps 0x400 to wakeup issue 3; 0x6E8702C4 posts it as event 0x2000400 | [verified] decompile |
| valid at plugin start: both writes happen in `usr_start_tasks` before it starts USTART1, whose end starts the FENYKL task. The only other writer is `ui_key_poweron_factor` 0x6E71031E after a standby wake (`[pwr_standby]:end` 0x6E735E1D), not at boot; `api_system_Clear_PowerON_Factor` clears the register, not the word | [verified] decompile, scan of 0x60551630 |

Limits: it detects a boot **started by the cable**. Switching the camera on with the power button while
the cable is already plugged in gives factor 0x1000001 (or 0xC), and the plugin runs. Connecting the cable
to a running camera does not stop a plugin either. For both cases see "Cable at run time" below.

A plugin that wants to run on USB boots too (for example to talk to the PC over USB, without touching
the card) sets `run_on_usb = true` in `plugin.toml`.

**If the check misfires** (a false positive skips every plugin on normal boots): nothing is written and
the camera behaves as stock, so it is safe; `hello` shows it (no `FENYKL.LOG` after a power-button boot).
Work around it with `run_on_usb = true` in the plugin and report it.

**Camera test** (proves both paths with `projects/hello`; the card is reached over USB only):

1. Install: with the camera off, plug in the cable (this boot runs whatever `FENYKL.BIN` is on the card;
   for this one boot make sure that is `hello` or nothing). On the PC delete `FENYKL.LOG`, copy the new
   `hello` `build/epl3/FENYKL.BIN` to the card root, eject, unplug.
2. USB path: with the camera off, plug in the cable, wait 10 s, unplug, plug in again (the PC rereads
   the card). Expected: no `FENYKL.LOG`. Unplug.
3. Normal path: switch on with the power button (no cable), wait 10 s, switch off. Plug in the cable.
   Expected: `FENYKL.LOG` with `fenykl hello` (written by step 3, not by this USB boot).

#### Cable at run time: the card gate

Plugging the cable into a running camera switches it to USB **in place** (no reboot): the plugin keeps
running. In Storage mode the PC's sector reads and writes go through the same file server task
(FILECLBK) as `fw_rpc_fopen`/`fw_rpc_fwrite`, and the camera's FAT stays mounted, so a plugin write while
the PC owns the card corrupts the FAT [static analysis]. Two target
functions (`arch/epl3/usb.c`, CPU0 task context, not CPU1; linked only when used):

- `fenykl_usb_cable_present()`: `fw_ui_key_get_state(0x400) == 0`, the test the firmware itself
  uses (`pcsub_is_usb_connected` 0x6EE194E8). The key state is set by the GIO interrupt, before USB
  enumeration and long before the PC can mount the card. A function call, not a RAM read: the byte and
  bit of 0x400 in the key state bytes (0x605519BC/BD) are not established.
- `fenykl_card_mounted()`: `fat_mounted[0] == 1` (`data.fat_mounted` in `firmware.toml`).

The gate itself is shared (`lib/card.c`, CPU0 task context): `fenykl_card_io_allowed()` is false
while the card is not mounted, and **from the first check that sees a cable (or a boot started by the
cable, `fenykl_usb_attached`) until power-off**; `fenykl_card_usb_seen()` tells that case apart.

Why not reopen after the cable is pulled: on the E-PL3 (2026-10-19) the gate closed for a USB session
in which the PC deleted `FENYKL.LOG`, wrote a new `FENYKL.BIN` and unmounted the card cleanly. After
the cable was pulled `fat_mounted[0]` read 1 again, the gate reopened and the logging library wrote
`FENYKL.LOG`. The camera wrote back its FAT state from before the session: `fsck.fat` found the
directory entry of `FENYKL.BIN` pointing at a cluster the FAT marks free (chain length 0) and the
dirty bit set. `fat_mounted[0] == 1` does not mean the camera re-read the card after the PC changed
it. Records and dirty config values stay in RAM until power-off; the next boot writes nothing of them.

This also covers a camera switched on with the power button while the cable is in (the boot check
above lets the plugin run).

**Rule: a plugin that does its own file writes checks `fenykl_card_io_allowed()` before each
open/write/close batch, skips the batch (keeps or drops the data in RAM) while it is false, and never
keeps a file open.** Once it was false because of a cable it stays false until power-off. The remaining window is one batch (milliseconds) against the second or more the
PC needs to enumerate and mount the card. The logging library does this for its flushes
(`docs/logging.md` section 5.1). The example projects that write files themselves do not check it
yet (no migration): do not plug the cable into a camera that runs them.

Status: the closing on a cable works on the E-PL3 (2026-10-19); the reopening corrupted the FAT and
is gone. The stay-closed rule is simulator-tested only.

### `plugin.toml`

| key | meaning |
|---|---|
| `targets` | cameras the plugin is written for (required), e.g. `["epl3"]` |
| `hooks`, `slots` | firmware functions to hook (not the SDK's own, `sdk_hooks` in `target.toml`), pointer slots to replace (not the SDK's own, `sdk_slots`) (Hooks; Callbacks and slots) |
| `cpu1_unproven` | hooks/slots whose CPU1 use is unproven, acknowledged (Hooks) |
| `cpu1_sources` | source files built as CPU1 code (CPU1; needs the target's `cpu1` capability) |
| `run_on_usb` | `true`: also run on a boot started by the USB cable (default `false`, Startup and USB boots) |
| `[capabilities.<name>]` | per-capability part of the plugin (Body API): `sources` (plugin files built only for targets with capability `<name>`; files listed nowhere are built for every target), `hooks` and `cpu1_unproven` (added to the top-level lists for those targets only). `<name>` must be a capability of some target; a file or a hook may be listed in one place only |

### Body API (`include/body.h`)

A plugin meant for several cameras names no firmware symbol (`fw_*`, `FW_*`) itself: it uses the
library APIs, the services above and `body.h`, whose functions read the camera's state (GUI, lens,
battery, the firmware's AF statistics, display and OSD buffers, the `img_exec` pass layout). The API
is the same everywhere; each camera implements it in `arch/<camera>/fenykl_body.h` (static inline).
Every group but the GUI predicates is a capability in `target.toml`, so `FENYKL_HAS_<NAME>` gates the
plugin code that uses it, and `plugin.toml` builds the files that need it only where it exists:

```toml
targets = ["epl3"]
hooks = ["cw_updateDisplay"]            # every target

[capabilities.img_exec]                 # only targets with img_exec
sources = ["rbswap.c", "rbswap_hook.c"]
hooks = ["img_exec"]
cpu1_unproven = ["img_exec"]
```

| capability | API | E-PL3 source |
|---|---|---|
| (all) | `fenykl_gui_live_view`, `fenykl_gui_playback` | `gui_super` |
| `lens` | `fenykl_lens_attached`, `_focus_mm`, `_focal_mm`, `_av`, `_iris_av` | `lens_attached`, `lens_obj_dst`, `lens_focal_length_now`, prop 0xA03A, `av_effect` |
| `batt` | `fenykl_batt_read`, `fenykl_power_src`, `fenykl_power_src_batt` | `bchk_ad_read` under semaphore 5, `power_src(_check)` (E-M10 II: `batt_vbat`, the 1 Hz check's stored value, and `power_src(_check)`) |
| `afc` | also `fenykl_af_mf`, `_half`, `_target_area`, `_all_targets`, `_grid` | `take_focus`, `trg_snapshot`, props 0x88B2/0x84D1, `take_fcs_af_mode2fcs`, fcs's contrast context and DMA buffer |
| `shot` | `fenykl_disp_*`, `fenykl_osd_*`, `fenykl_frames_*`, `fenykl_recfile_*`, `fenykl_dram`, `fenykl_cached`, `FENYKL_DISP_*`, `FENYKL_OSD_*`, `FENYKL_FRAMES_*` | `mov_img1_shadow`, `mov_disp_flip_tbl`, display registers, `disp_img3_*`, `frame_palette`, `gpi_*`, `osd_shadow`, `osd_palettes`, `recfile_*` |
| `img_exec` | `FENYKL_IMG_*` (IMG_CTRL, IMG_PARAM offsets), `fenykl_img_mode_class` | img_mode_class's table |

`projects/main` is written this way (`tests/test_plugin_build.py` fails on any `fw_`/`FW_` name in it);
`hello` and the bird detector keep using the firmware directly. The test sees main's own files only: SDK headers main
includes still carry target values (`fenykl.h`'s `FENYKL_TIMER_NOW` reads `FW_TIMER_NOW`, `hud.h`'s
`FENYKL_HUD_*` canvas, states and palette indices are per camera). The `afc` capability covers both
the own contrast client (`lib/afc.c`) and the firmware client's reads (`fenykl_af_*`). The `shot`,
`img_exec` and AF grid groups mirror the E-PL3's encodings and will be reshaped when a second camera
implements them.

### Getting output: write a file

`fw_dbg_puts` prints to the internal UART, which is not reachable without opening the camera.
Output you can read goes into a file on the card. The pattern proven on the camera (`projects/hello`):

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
- Mode `"w"` is the one proven on the camera, but it does **not truncate**: it overwrites from the start
  and an older, longer file keeps its tail (seen 2026-10-13: one probe's log followed by the rest
  of an earlier, longer log). Delete `FENYKL.LOG` on the PC before a run, or end the log with a clear marker.
  `"a"` (append) exists in the firmware's mode table but has not been tried.
- Use `\r\n` line ends if you read the log on Windows; `fw_sprintf` forwards at most 8 variadic words
  and the build refuses a call with more (split a long line into calls appending to the buffer).
- Do not write every few milliseconds: file writes go through the firmware's file server task, and
  writes concurrent with the camera's own image writing are not proven safe. Log in batches, use a
  test card.

Or use the **logging library** (`sdk/docs/logging.md`, linked into every plugin):
`#include "log.h"`, `fenykl_log_info(&module, "fmt %u", x)` from any task, hook or CPU1 worker
(ISR context by design, untested); a flusher task appends to `C:\FENYKL.LOG` (one file across
boots, boot header, 4 MiB cap at init).
`projects/main` uses it.

Persistent options: the **configuration library** (`sdk/docs/config.md`, linked into every plugin),
`#include "cfg.h"`, `FENYKL_CFG_BOOL/INT/ENUM` declarations, `C:\FENYKL.CF0`/`CF1` with CRC and sequence number, a
debounced saver task. `projects/main` uses it.

Settings page: the **ui library** (`sdk/docs/ui.md`), `#include "ui.h"`; the "Fenykl" row of Custom
menu tab J opens one scrolling list of every grouped cfg option (at most 20), drawn by the camera's
own menu list view. `projects/main` uses it.

### Firmware API (`arch/epl3/firmware.toml`)

`firmware.toml` declares each firmware function a plugin can call as `fw_<name>`: its address,
signature, evidence, the cores that may call it and, in `note`, where they were read. The generated
`fenykl_fw.h` repeats them above each prototype. Commonly used:

- console: `fw_dbg_puts`, `fw_dbg_puts_hex`, `fw_sprintf`
- tasks: `fw_OlyTaskSleep`, `fw_OlyTaskCreate`, `fw_OlyTaskStart`, `fw_OlyTaskStop`,
  `fw_OlySemaphoreWait`, `fw_OlySemaphoreSignal`
- files: `fw_rpc_fopen`, `fw_rpc_fclose`, `fw_rpc_fread`, `fw_rpc_fwrite`, `fw_rpc_fremove`
- memory and cache: `fw_malloc`, `fw_free`, `fw_dcache_purge_all_irqsafe`, `fw_recfile_alloc`,
  `fw_recfile_free`
- GUI: `fw_gui_post_event`, `fw_gui_show_panel_ex`, `fw_gui_data_set`, `fw_gui_data_get`
- other: `fw_prop_get_obj`, `fw_ui_key_get_state`, `fw_cam_request`, `fw_bchk_ad_read`

Evidence is the confidence of the function's name and signature.
For every entry the CALL register list and frame were read from the function's own `ret` and checked
against all of its callers in the firmware; argument registers come from callers and the function
body (the `note` field says where).

To add a function, add a table to `firmware.toml`:

```toml
[rpc_fread]
address = 0x6E7B823D          # run address in main-code-01
saves = []                    # CALL register list (subset of d2, d3, a2, a3) ...
frame = 0x18                  # ... and frame size; both must equal the callee's RET/RETF
evidence = "high"             # confidence of name and signature
returns = "int"
args = ["a void *buf", "d unsigned size", "d unsigned n", "a void *fp"]
note = "ret [],24 at 0x6E7B8270; returns items read"
```

`args` entries are `<class> <C declaration>`: `d` = integer class (D0, D1, then stack), `a` = pointer
class (A0, A1, then stack), only 32-bit types. A trailing `"..."` makes it variadic: the last named
argument goes to the first stack slot and 8 variadic words are forwarded; a macro of the same name
makes a call with more variadic arguments a compile error. The generator refuses
argument layouts the characterised ABI does not cover (a register argument after a stack argument). The
simulator test (`tests/test_sim.py`, `api`) then calls the new thunk against the mock of its ABI
shape: `tests/sim/mocks.S` has one `ARGS_<pattern>` macro per argument pattern (the classes of
`args` in order, e.g. `adda`), written from the firmware calling convention; the return kind and the CALL operands are
taken from the declaration. Only a function with a new argument pattern needs a new macro (the test
does not assemble without it). Only add functions whose CALL encoding and argument registers were read from the
firmware; unknown signatures stay out.

`cores = ["cpu0", "cpu1"]` with `cores_evidence` marks a function CPU1 code may call (of the
above: `fw_dbg_puts`, `fw_dbg_puts_hex`, `fw_sprintf`, `fw_OlyTaskSleep` and, for its own core,
`fw_dcache_purge_all_irqsafe`; the PSW and cache primitives are per core too). Without `cores` a function is CPU0-only, and
calling it from a `cpu1_sources` file is a compile error (CPU1).

#### Firmware data

Firmware addresses the SDK uses as values (globals, tables, MMIO registers, address-range bounds) are
`[data.<name>]` tables in `firmware.toml`, not constants in the C files. Each becomes
`#define FW_<NAME> 0x...u` in `fenykl_fw.h`, so the libraries and `arch/<camera>/fenykl_body.h` name
the address and each camera's `firmware.toml` supplies it:

```toml
[data.gui_super]
address = 0x60702278          # the address (unsigned 32-bit)
type = "u8"                   # what lives there; "code" for a function address, "bound" for a range limit
evidence = "high"             # high or medium, as for functions
note = "gui_sm super state (0x02 every live-view screen, 0x03 menu, 0x06 playback); ..."
```

```c
/* gui_super, u8, evidence high: gui_sm super state (...) */
#define FW_GUI_SUPER 0x60702278u
```

The layout behind an address stays in C (a struct, or offsets next to the code that reads them).
`FENYKL_CORE_ID()`, `FENYKL_UNCACHED()` and the other memory-map macros are in `fenykl_target.h`. The
bird detector (`projects/birddetect6`, E-PL3-only) keeps its addresses inline.

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
(not characterised). If `worker` returns, the entry sleeps forever (the task slot is not freed).
`stack_base` is the low end of the stack, as the firmware's own dynamic callers pass a malloc'd block.
The 64-slot task table can be full (`OlyTaskCreate` returns -1).

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
targets = ["epl3"]
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
`fenykl_hookwrap_<name>`). On the E-PL3 that is `appevent_post`, the event layer
(`docs/event.md`): plugins register event handlers with `fenykl_event_add` and call
`fenykl_event_install` instead of hooking it; key handlers sit on top (`docs/key.md`,
`fenykl_key_add`, `fenykl_key_install`). A plugin that never calls the library gets no byte of
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
  from the E-PL3 cache driver and was verified on the camera with a probe (2026-10). CPU1/CPU2 are not invalidated: a hook may target a function they can run only if
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
the E-PL3:

| name | kind | what |
|---|---|---|
| `appevent_hook1` / slot 0x605FD840 | `calls`, empty | `unsigned hook(unsigned event)`: returns the event, 0 swallows |
| `usrset_change_cb` / slot 0x6034CCB4 | `calls`, stock 0x6EC0A5DB | `void cb(unsigned id)` after every setting change |
| `gui_action` | `calls` | `unsigned action(void *sm, unsigned code, unsigned p, void *p2)`: GUI state-machine action/entry/exit |
| `cam_cnt_frame_cb` | `calls` | `void cb(unsigned unk, unsigned events, void *req)`: contrast client frame callback (request +0x2C; `lib/afc.c`) |
| `cam_req_done_cb` | `calls` | `void cb(unsigned status, void *req)`: camera request done callback (request +0x28) |

Declared on the E-M10 II:

| name | kind | what |
|---|---|---|
| `mode_cond` / slot `mode_cond_eyefi` 0x60AFF404 | `calls`, stock 0x6071DDE0 | `unsigned cond(void)`: mode-check condition 113 of setting 0x193 (Eye-Fi); 0 = allowed |
| `menu_row` / slots `eyefi_ok_fn` 0x60D2F5A4, `eyefi_right_fn` 0x60D2F484 | `calls`, empty | `int fn(void *actuator)`: row callback of the Eye-Fi row's OK/RIGHT dispatch records in Custom menu tab K; 4 = continue with the stock pop-up |

**SDK slots.** These three slots are the settings page's (`lib/ui_em10ii.c`): `target.toml` lists
them in `sdk_slots`, the generator emits their descriptors for every plugin, and a `plugin.toml` that
lists one in `slots` is refused at build time. A probe may still replace them with
`FENYKL_SLOT_REPLACE` if it does not install the settings page. An SDK hook or slot whose CPU1 scope is
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

```toml
# plugin.toml
targets = ["epl3"]
slots = ["usrset_change_cb"]
cpu1_unproven = ["usrset_change_cb"]
```

```c
static volatile unsigned changes;
static void on_change(unsigned id)
{
	changes++;
	fw_orig_usrset_change_cb(id);                   /* the stock callback */
}
FENYKL_CALLBACK(usrset_change_cb, on_change);

	int r = FENYKL_SLOT_REPLACE(usrset_change_cb, on_change);   /* in a task */
	/* ... */
	fenykl_slot_restore(&fenykl_slot_usrset_change_cb);
```

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
  except through `fw_orig_*`. Pass `id`s through unchanged: some are u16, and the original masks
  them (`exthu`).
- **Settings persist.** Changing a setting or a stored value in RAM can be saved at power-off
  (Payload rules). A test replacement must forward everything unchanged.
- Only scalar signatures (32-bit integer or pointer arguments and results), as for thunks. Variadic
  callbacks are refused at build time.

### CPU1

The E-PL3 has two cores. In still modes CPU1 is nearly idle (eye detection and an idle loop). A
plugin can run one worker there by borrowing the dormant CPU1 task 0x1002 (SCHEAP_DEBUG). CPU1 code
lives in the same image, in the files `plugin.toml` lists:

```toml
cpu1_sources = ["worker.c"]   # built with -DFENYKL_CPU1: CPU0-only firmware calls do not compile
```

```c
/* worker.c (CPU1) */
struct state { uint32_t beats; };
extern struct state st;                     /* FENYKL_SHARED struct state st; in a CPU0 file */
FENYKL_CPU1_WORKER(worker);                 /* defines the task entry worker_cpu1 */
void worker(void *arg)
{
	while (!fenykl_cpu1_stop_requested()) {
		/* one bounded chunk of work, then block */
		FENYKL_SHARED_PTR(st)->beats++;
		fw_OlyTaskSleep(1);
	}
}

/* control.c (CPU0), in a FENYKL_TASK created with priority 12 */
int r = fenykl_cpu1_start(worker_cpu1, 0);  /* 0 or FENYKL_CPU1_ETCB/EBUSY */
...
r = fenykl_cpu1_stop(1000);                 /* 0 = worker returned, task dormant (0x10) again */
```

`fenykl_cpu1_start` checks task 0x1002's TCB first (id, dormant state 0x10, stock entry 0x6E70D51A,
priority 12 and stack 0x800, or exactly what an earlier start of this plugin wrote) and refuses
without writing anything otherwise. It then gives the task the entry, `arg` (D0 of the worker),
priority 15 and an 8 KiB stack of the runtime, and starts it through `fw_OlyTaskStart`.
`fenykl_cpu1_stop` sets the shared stop flag and waits for the task to become dormant. After
`timeout_ticks` it ends the task with `fw_OlyTaskStop` (`FENYKL_CPU1_ETIMEOUT`).

Rules:

- **Block regularly.** The worker runs at priority 15 and every firmware task preempts it. Only
  BOOT_CPU1's idle loop (16) serves CPU0's requests to start or stop CPU1 tasks (movie audio), and it
  runs only while the worker blocks. Call `fw_OlyTaskSleep(1)` at least every ~10 ms. A worker that
  never blocks hangs the next CPU1 task start, which can end in a watchdog reset.
- **Start and stop from a task of priority 12** (`OlyTaskCreate`, never `plugin_main`):
  `OlyTaskStart/Stop` of a CPU1 task spins on CPU0 until CPU1 answers, and at priority 11 or higher
  the spin could starve the watchdog task PER_WDT (11).
- **CPU1-safe calls only.** The build enforces it for `fw_*` (column CPU1). No file I/O, `malloc` or
  task creation on CPU1. The worker reports through shared memory and a CPU0 task logs.
- **Shared data is uncached.** The cores have no cache coherency. Data both use is `FENYKL_SHARED`
  (section `.shared`, whole 64-byte lines, zeroed by the loader through the uncached alias). Both
  sides access it only through `FENYKL_SHARED_PTR(var)`. Never touch it through its plain name. The
  worker's other data and its stack are CPU1-private: CPU0 must not touch them after the start.
  Neither may they share a 64-byte line with CPU0 data (a write-back of one core's copy of the line
  undoes the other core's writes): give the first object after each boundary between the two in
  the link map `__attribute__((aligned(64)))`, as the worker stack has (check with `nm -n`).
- **Not while recording a movie.** CPU1 runs the AVCHD encoder then. Hooks on CPU1 code are not
  supported yet.

`fenykl plugin build` and `fenykl payload pack` print the `.shared` placement, and so does
`fenykl payload info FENYKL.BIN --elf plugin.elf`. `projects/birddetect6/` is the CPU1
example: `birddetect6.c` starts and stops the worker, `worker.c` runs on CPU1.

## ABI rules

- Plugin code is GCC mn10300 ABI; the firmware uses the Panasonic ABI. **Every call into the firmware
  goes through a generated thunk** (`fw_*`); never call a firmware address through a C function pointer.
- The firmware passes integers in D0/D1 and pointers in A0/A1, extra arguments from (4,SP); the CALL
  instruction carries the callee's register list and frame size. The thunks handle both.
- **Never CALL C code with a non-empty register list or frame** (e.g. from a firmware patch): use
  `call x,[],0` or `calls`, from a stub that provides 12 bytes (return slot and GCC's home area for
  D0/D1), as `arch/epl3/crt0.S` does.
- Callbacks from the firmware into C need a reverse entry stub, `FENYKL_CALLBACK` (Callbacks and
  slots), even with integer arguments only: a `calls` caller provides no GCC home area.
- Struct and 64-bit arguments to firmware functions are not characterised.
- Do not use `-mrelax`, `-mam33` or any CPU option; the build uses `toolchain/cflags` unchanged.

## Payload rules

- **Block, never spin.** `FENYKL` runs at priority 11, like the watchdog task PER_WDT. A busy loop
  starves it and the watchdog resets the camera. Wait with `fw_OlyTaskSleep` (or a queue/semaphore
  wait), also in your own tasks. Recovery from a bad payload: remove the card or delete the file.
- **Nothing persistent.** Removing the card undoes only what does not persist. A payload runs with full
  firmware rights; corrupted settings in RAM can be saved to flash/EEPROM at power-off, and boot code
  that runs before the card-updater decision could then fail on every boot (a brick the card updater
  cannot fix). Payloads must not write flash, EEPROM or settings, nor touch the camera's setting
  structures, without a reviewed design.
- **Memory:** code, data and `.bss` live in 0x6EA70000..0x6EAEA000 (488 KiB incl. the 28-byte
  header); the link fails if they do not fit. The loader stack follows at 0x6EAEA000..0x6EAEE000.
  Bigger buffers: `fw_malloc`, at most 0x3FFC bytes per call (larger requests return NULL).
- **Caches:** buffers handed to file or DMA code should be uncached addresses
  (`FENYKL_UNCACHED(buf)`); read results back through the uncached alias, or call
  `fw_dcache_purge_all_irqsafe()` first.
- **No interrupt context, no long work in callbacks:** the payload runs as a normal task after
  USTART1 has finished.
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
   (epl3: `toolchain/cflags`), `-D FENYKL_LOAD_BASE/FENYKL_REGION_END` from `target.toml`,
   `-DFENYKL_RUN_ON_USB=0|1` from `plugin.toml` `run_on_usb`, `-DFENYKL_TARGET_<CAMERA>=1` and
   `-DFENYKL_HAS_<CAPABILITY>=1` for each of the target's `capabilities`, and
   `include/`, `arch/<camera>/` on the include path; the files in `cpu1_sources` also get
   `-DFENYKL_CPU1`; the `lib/*.c` files get `-DFENYKL_PLUGIN_CPU1=0|1` (whether the plugin has
   `cpu1_sources`);
3. links with `link.ld` (`--gc-sections`, which drops the library code a plugin does not use,
   `--orphan-handling=error`, map file `plugin.map`);
4. with `--isa-check`, runs the target's `isa_check` (epl3: `toolchain/isacheck/check_isa.py`) on
   `plugin.elf` and fails on any error;
5. packs `FENYKL.BIN` (`fenykl payload pack plugin.elf FENYKL.BIN` does only this step; it finds the
   camera by the ELF's link address).

A Python command rather than a Makefile: the generator and the packer are Python anyway, the build is
a fixed sequence over a handful of files (well under a second, plus the ISA check if asked for), and it is tested
like the rest of fenykl.

### FENYKL.BIN

```
0x00 u32     magic  0x324C4E46 ("FNL2")
0x04 u32     load   0x6EA70000
0x08 u32     size   whole image incl. header, multiple of 4, 0x1C <= size <= 0x7A000
0x0C u32     entry  offset of the entry point from load (0x1C = _start), 0x1C <= entry < size
0x10 u32     sum    sum (mod 2^32) of the little-endian words in [0x1C, size)
0x14 char[8] model  "E-PL3", zero-padded
0x1C ...            code and data, linked at 0x6EA7001C
```

The format is the same for every camera; `load`, the size limit and `model` are the epl3 values
(`load`, `max_size`, `model` in `target.toml`). The loader rejects a file whose model field is not
exactly its own camera's (all 8 bytes, padding included), so a payload built for another camera never
runs. The loader reads at most 0x7A000 bytes and calls `load + entry` with `calls`, D0 = load base,
D1 = image size. `fenykl payload info` finds the camera by the header's `model`, shows the header and
applies that loader's checks in the loader's order (`payload.check_image`). The old `FNL1` format
(0x14-byte header, no model; loaders flashed before 2026-10-15) is rejected by its magic: rebuild the
payload. Loaders older than this format reject `FNL2` files as well.

## Layout

| path | purpose |
|---|---|
| `include/fenykl.h` | SDK header, camera-independent; includes `fenykl_fw.h` and `fenykl_target.h` |
| `include/log.h`, `include/cfg.h`, `include/event.h`, `include/key.h`, `include/hud.h`, `include/ui.h`, `include/shot.h` | logging, configuration, event layer, key layer, HUD, settings page and screenshot library APIs |
| `include/body.h` | body API: camera state by capability, implemented per camera in `arch/<camera>/fenykl_body.h` (Body API) |
| `lib/` | SDK library, linked into every plugin (`--gc-sections` drops what is unused) |
| `lib/start.c` | clears `.bss`, skips a USB boot (`fenykl_usb_attached`, `run_on_usb`), calls `plugin_main` |
| `lib/string.c` | `memcpy`, `memmove`, `memset`, `memcmp` |
| `lib/slots.c` | `fenykl_slot_replace` / `..._restore`, the word written with the D-cache written back before and after (`fenykl_dcache_flush`) |
| `lib/card.c` | card gate `fenykl_card_io_allowed`: closed while the card is not mounted and until power-off once a cable was seen |
| `lib/log.c`, `lib/log_rec.c`, `lib/log_int.h` | logging library: `log_rec.c` record writers (CPU1 code, both cores), `log.c` flusher, init, tee; design `docs/logging.md` |
| `docs/logging.md` | logging library reference |
| `lib/cfg.c` | configuration library: load, parser, save, saver task; doc `docs/config.md` |
| `lib/event.c` | event layer: the SDK's `appevent_post` hook runs the plugin's handlers by priority; doc `docs/event.md` |
| `lib/key.c` | key layer: named keys, claimed presses completed as SHORT/LONG or replayed to the firmware, optional tick; doc `docs/key.md` |
| `lib/hud.c` | HUD library: live-view text elements on OSD layer 1, drawn from the plugin's `cw_updateDisplay` hook; doc `docs/hud.md` |
| `lib/hud_int.h`, `lib/hud_em10ii.c` | its per-camera parts: layer-1 handle layout, DRAM alias rules and live-view image ids per camera; the E-M10 II's visibility (INFO-screen property), output device and palette pick |
| `lib/shot.c`, `lib/shot_pix.c`, `lib/shot_int.h` | screenshot library: the panel (live view and OSD layers) to `C:\SHOTnnnn.PPM` in strips, `shot_pix.c` the firmware-free pixel work; doc `docs/shot.md` |
| `lib/ui.c`, `lib/ui_int.h` | settings page library, option core: one list of the cfg options, key and dial semantics, value text, and the page lifecycle both bodies share (open and close bookkeeping, install start, stock word check, "not saved" state); doc `docs/ui.md` |
| `lib/ui_epl3.c` | its E-PL3 page: Custom J "Fenykl" row, super state 0x0D, drawn by the stock menu list view from GUI data slot 0xF |
| `lib/ui_em10ii.c` | its E-M10 II page: Custom K Eye-Fi row as "Fenykl" (`sdk_slots`), a screen pushed on the main menu app, drawn by the stock Custom list view from GUI data slot 0x38 |
| `arch/epl3/target.toml` | model (FENYKL.BIN header), toolchain, cflags file, ISA check, ELF machine, payload `load` and `max_size`, `sdk_hooks`, `sdk_slots`, `sdk_cpu1_unproven`, `capabilities`, `[stock]` firmware identity, `[assets]` GUI asset layout of the stock image |
| `arch/epl3/loader/` | the camera's boot loader patch: `loader.S`, `loader.ld`, `loader.lst`, `patch.toml` (pinned bytes) |
| `arch/epl3/firmware.toml` | hand-curated firmware API: functions, hooks, callbacks, slots, data addresses |
| `arch/epl3/link.ld` | payload linker script (must agree with `load`/`max_size`; packing checks it) |
| `arch/epl3/crt0.S` | entry `_start`: frame for C, calls `fenykl_start` |
| `arch/epl3/fenykl_body.h` | the E-PL3 body API (`include/body.h`) on `firmware.toml` data and functions |
| `arch/epl3/fenykl_target.h` | `FENYKL_UNCACHED`, `FENYKL_TASK`, `struct fenykl_slot`, the CPU1 API (`FENYKL_SHARED`, `FENYKL_CPU1_WORKER`) |
| `arch/epl3/hooks.c` | `fenykl_hook_install` / `..._remove` (compiled into every plugin) |
| `arch/epl3/cpu1.c` | `fenykl_cpu1_start` / `..._stop`, the worker stack and the shared stop flag (compiled into every plugin) |
| `arch/epl3/usb.c` | `fenykl_usb_attached`: power-on factor (`data.poweron_factor`) == 0x400; `fenykl_usb_cable_present`, `fenykl_card_mounted` for the card gate (compiled into every plugin) |
| `projects/*/plugin.toml` | cameras the example is written for (`birddetect6`: `epl3`; `hello`, `main`: both) |
| `arch/em10ii/` | E-M10 II: `target.toml` (with `[assets]`; the image table spans two resource slots), `firmware.toml`, `loader/`, `crt0.S`, `link.ld` (0x60DA501C, 0x67000 incl. header), `fenykl_target.h` (`FENYKL_LIB_TASK_PRIO` 55, `fenykl_dcache_flush`, `struct fenykl_slot`, the I-cache API), `fenykl_body.h` (the GUI group of `include/body.h`), `usb.c` (card gate), `hooks.c` (hook install/remove, code byte patch, CPU0 I-cache invalidate) |
| `projects/` | the example plugins, indexed in `projects/README.md` |

### Adding a camera

1. Create `arch/<camera>/` with the five files above; `<camera>` is the short id (`epl3`, ...).
   `load` must differ from every other target's (`payload info`/`pack` identify the camera by it).
2. A different CPU also needs its toolchain (`toolchain = "<prefix>"`), its own `crt0.S`,
   `FENYKL_TASK`, `hooks.c` and a thunk/hook generator: `py/fenykl/fwapi.py` only knows MN10300 GCC->Panasonic thunks.
   Omit `isa_check` if there is none.
3. Implement `int fenykl_usb_attached(void)` (`include/fenykl.h`) in `arch/<camera>/`; it returns
   0 if the camera cannot tell a boot started by the USB cable. Implement `fenykl_usb_cable_present`
   and `fenykl_card_mounted` there too: the shared card gate (`lib/card.c`) is built on them.
4. List the SDK features it supports in `capabilities` (`target.toml`). Each becomes
   `FENYKL_HAS_<NAME>=1` in every compile, next to `FENYKL_TARGET_<CAMERA>=1`. epl3 has `cpu1` (the
   CPU1 worker runtime, `cpu1.c`); a plugin with `cpu1_sources` does not build for a camera without it.
   `event`, `key`, `hud`, `ui`, `afc` and `shot` select library files (`LIB_CAPABILITIES` in
   `py/fenykl/plugin.py`): without them `lib/event.c`, `key.c` (its key map is the E-PL3's),
   `hud.c`, `ui.c`, `afc.c` + `afc_sm.c`, `shot.c` + `shot_pix.c` are not built. `ui` also needs the
   camera's page, `lib/ui_<camera>.c`.
   `afc`, `lens`, `batt`, `shot` and `img_exec` are groups of `include/body.h`: list one once
   `arch/<camera>/fenykl_body.h` implements it (the file must exist, with the GUI predicates, before
   a plugin including `body.h` builds). Plugin files under `[capabilities.<name>]` follow them.
5. Give `firmware.toml` a `[data.<name>]` entry for every `FW_<NAME>` the libraries and
   `arch/<camera>/fenykl_body.h` use (the build fails on a missing one), and `target.toml` an `[assets]` table if
   `fenykl assets dump` should read its stock image.
6. Add `<camera>` to the `targets` of the plugins that were written for it.

Tests: `tests/test_payload.py` (format, packing, loader checks), `tests/test_fwapi.py` (argument
placement, declaration checks), `tests/test_sim.py` + `tests/sim/` (in `mn10300-elf-run`:
crt0 entry contract, `.bss` clearing, every thunk and `FENYKL_TASK` against one mock per ABI shape at
the real firmware addresses; hook stub/trampoline/call-original, argument and result modification, callee-saved
registers and SP, stack arguments, install/remove restoring the bytes; callbacks, slots, the
CPU1 worker's API contract, USB boot skip and `run_on_usb`), `tests/test_log_sim.py` + `tests/sim/log/` (logging
library: rings, overflow, cross-core refill, formatting, size cap), `tests/test_cfg_sim.py` +
`tests/sim/cfg/` (configuration library), `tests/test_plugin_build.py` (example builds; their ISA check is skipped without Ghidra).

## Firmware tools

The `fenykl` CLI also inspects, unpacks and patches the firmware itself.

### Tests

`uv run pytest -q`. The tests need the mn10300-elf toolchain. The tests that read the stock
firmware (unpack format, patching, assets, HUD, the loader build that is byte-identical to the one
flashed on the camera) need `firmware/OLY_E_086_1600_0000_0000.BIN` and are skipped without it; a
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
them. Without `FILE.BIN` it reads the stock image of `-t CAMERA` (default `epl3`; `em10ii`) from
`firmware/`; any other image is identified by sha256 and rejected unless its camera's `target.toml`
has an `[assets]` layout (E-PL3 v1.6, E-M10 II v1.4). Output, by default in
the git-ignored `out/assets/`:

```
icons/A034.png                  inline icons by char code (`@A034@` in UI strings), 2 bpp
images/0059.png                 GUI images by id 1..2193, 4 or 8 bpp
fonts/enu_id15_k24/U+0041.png   glyphs per language, font id and size key, 2 bpp
index.csv                       kind, id, font, address, width, height, fmt, file
```

The PNGs are indexed: pixel values are the firmware's raw colour indices. 2-bpp glyphs and icons
get a grey ramp (0 transparent, 3 white); images get the GUI palette the firmware most likely
uses (palette 2 for 4 bpp, 7 for 8 bpp), so some selected/gradient variants show odd colours.
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
(e.g. `epl3/fenykl-loader`). Everything camera-specific about it is in `sdk/arch/<camera>/`:
`loader/loader.S`, `loader/loader.ld` and `loader/loader.lst` (source, linker script with the hook site,
cave and firmware symbols, disassembly), `loader/patch.toml` (description and the pinned edits: block,
run address, sha256 of the stock bytes, replacement bytes), and `target.toml` (`model` checked by the loader in the
FENYKL.BIN header, payload `load`/`max_size`, `[stock]` firmware identity and block load addresses).
A second camera is a new `sdk/arch/<camera>/` with these files; no Python changes. Its loader test
(`tests/test_fenykl_loader.py` and the simulator harness in `tests/loader_sim/`) is E-PL3-specific.

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
