# Dev monitor (`sdk/lib/devmon_*.c`)

E-M10 II only. The camera opens its own Wi-Fi access point and runs a TCP monitor on CPU1, so a PC
can read and write RAM, run test programs, move `FENYKL.*` files, fetch the camera's photos and
screenshots and switch the camera off, all without anyone at the camera. The PC side is `fenykl dev` (`py/fenykl/dev.py`, Python standard
library only). The camera stays in shooting mode with its buttons live.

Status:
- **Works on the camera** (2026-11-17, in the probe this library came from): the headless access
  point, the CPU1 server, the text-protocol PING, READ and LOG, the stop-then-Off power-off
  sequence and unchanged shooting mode.
- **Simulator and host tests only:** the framed protocol v1, WRITE, test programs, `FENYKL.*` files,
  the POWEROFF trigger, and the read-only photo and screenshot fetch with FLIST (section 6).

## 1. Use

A plugin opts in. The library is linked only into plugins whose `plugin.toml` lists the table,
because its options and tasks are not for every plugin of the target:

```toml
targets = ["em10ii"]

[capabilities.devmon]
```

Then call `fenykl_devmon_start()` (`include/devmon.h`) once from `plugin_main`, after
`fenykl_log_init` and `fenykl_cfg_init`. `FENYKL_HAS_DEVMON` is defined only in plugins that link
the monitor, so a plugin can start it under `#ifdef`, as `projects/main` does: a demo build of main
with the monitor is `fenykl plugin build sdk/projects/main -t em10ii --with devmon`, without
changing main's plugin.toml. Its options then sit on main's settings page with main's own. To get the settings page, also call `fenykl_ui_install`
and `fenykl_ui_tick`. `projects/devmon1` is the minimal plugin.

The options on the settings page (Custom menu K, the Eye-Fi row):

| option | default | effect |
|---|---|---|
| `devmon.ap` Wi-Fi monitor | on | the access point 15 s after the start |
| `devmon.off` Power-off test | off | the power-off sequence below at 585 s (inject at 590 s) |

`devmon.off` is off by default so that a monitor session is never cut short. The POWEROFF op runs
the same sequence on request.

The WPA2 passphrase is a build setting: `FENYKL_DEVMON_KEY` in the environment of `fenykl plugin
build` (8 to 63 printable ASCII characters, no quote or backslash). Without it the build warns and
uses the public default `fenykl-devmon`.

Camera settings for a session:
- **Sleep Off.** The headless start bypasses the Wi-Fi sub-app and its sleep hold.
- Auto power-off long enough for the session.
- Do not start the camera's own Wi-Fi while the monitor runs. The monitor refuses to start if
  stock Wi-Fi is already up, but nothing guards a stock start that comes later.

## 2. How it runs

- **Monitor task (CPU0, priority 55, `devmon_em10ii.c`):**
  - Logs the boot facts: power-on factor, wake index, off factor, lever and cable levels.
  - Waits for sections 1 to 7 to load.
  - At 15 s calls `wifi_mngr_start` with its own parameter block (SSID `FENYKL-DEV`, WPA2 passphrase
    `fenykl-devmon`) and callbacks, the way the stock debug command `mngr start pri` does.
  - If the start callback reports status 0, posts the server's boot function to CPU1.
  - Polls the job word every 5 ms.
  - Every 100 ms, logs the callback counts and drains the CPU1 records, both to the card log and to
    the text log that LOG returns.
- **Server task (CPU1, priority 0x37, created with `acre_tsk`, `devmon_cpu1_em10ii.c`):**
  - Serves one client at a time on port 5555 (backlog 1), through the olnet socket layer.
  - A client that goes silent without closing (the PC left the network) is dropped by recv's
    timeout: every socket the stack makes, the accepted connection included, gets a 60 s receive
    and send timeout (0x60E1D44B writes 60000 ms at socket +0x3C, +0x40; recv and send wait with
    them; read statically). The next client is served after that. The record "client gone N ms
    after its last request" shows the time on the camera.
  - Runs PING, READ, LOG, TIMING and WRITE of uncached addresses itself.
  - Hands every other op to the monitor task through `struct fdm_job` in the uncached shared
    struct: post, then a bounded wait (30 s, plus the run timeout for TPRUN).
  - Touches only its stack, the uncached `.shared` struct, the uncached job buffer and `.rodata`,
    never cached `.data`/`.bss`. `tests/test_devmon.py` checks the relocations.
  - Its 0x800-byte stack (CPU1 kernel heap) is filled with 0xA5 at the task start, from at least 16 bytes
    above the bottom (the bottom estimate errs upward, so the count understates the headroom) up to just below the filling function. PING's `srv_stack_free` is the number
    of filled bytes from there up that were never overwritten (the server's frames, `send()`,
    `recv()` and any interrupt on that stack), at least the headroom left. The bottom is found as
    128 bytes above the entry function's local minus 0x800 (the kernel enters a task with
    SP = top - 4; `tests/test_devmon.py` checks that the entry takes less).
- **Test program task (CPU0, priority 55):** runs `tp_main` (section 5). It shares priority 55's
  round robin with the watchdog kicker, so a looping program cannot starve the kicker.
- **Exec region (`target.toml [tp]`):** 0x60F8A800..0x61010000, a never-loaded hole of the image
  that is valid once section 5 is loaded.
  - 16 slots of 32 KiB, then the 16 KiB job buffer at 0x6100A800, which is used uncached by both
    cores.
- **Network:** the camera is 192.168.0.10 and gives the PC an address by DHCP (192.168.0.3 in the
  2026-11-17 run).

## 3. Protocol v1

Every message is a 16-byte little-endian header followed by `len` payload bytes:

| field | request | reply |
|---|---|---|
| u32 magic | `FKD1` 0x31444B46 | `FKR1` 0x31524B46 |
| u16 op | the op | the op echoed |
| i16 status | 0 | 0 or an error (below) |
| u32 id | chosen by the PC | echoed |
| u32 len | payload, at most 0x4018 | payload; 0 with an error |

The ops (all words u32 LE; `name` is a 12-byte NUL-padded field, `path` a 32-byte one; FGET takes
either, told apart by the payload length, and `DCIM/...` names need the path):

| op | name | request payload | reply payload | runs on |
|---|---|---|---|---|
| 0x00 | PING | - | text: `devmon proto=1 tp_api=1 up_ms= poweron= wake= off= clients= requests= dropped= exec_busy= srv_stack_free=` | CPU1 |
| 0x01 | READ | addr, len (≤ 1 MiB), [piece, [pace_ms]]; default 8 KiB pieces, 1 ms pause | the bytes | CPU1 |
| 0x02 | WRITE | addr, bytes (≤ 0x4014) | - | CPU1 (uncached alias), CPU0 (cached alias) |
| 0x03 | LOG | since (byte index) | end index, text from max(since, oldest kept) | CPU1 |
| 0x04 | TIMING | - | bytes, us, sends, max_send_us, stalls, stall_us, piece, pace_ms, sock of the last READ | CPU1 |
| 0x10 | TPLOAD | slot, offset, bytes | - | CPU0 |
| 0x11 | TPRUN | slot, arg, timeout_ms (1..600000) | i32 result, u32 us | CPU0 |
| 0x20 | FPUT | offset, total, crc32, name, bytes (≤ 16 KiB) | - | CPU0 |
| 0x21 | FGET | offset (< 2^31), len (≤ 16 KiB), name or path | bytes, fewer at the end | CPU0 |
| 0x22 | FDEL | name | - | CPU0 |
| 0x23 | FLIST | start, path | next, entries (section 6) | CPU0 |
| 0x30 | POWEROFF | - | - (sent before the sequence starts) | CPU0 |
| 0x31 | KEY | key (index, below), hold_ms (1..10000) | - (after the release) | CPU0 |
| 0x32 | DIAL | mode-dial position (index, section 7) | u32 previous dial value | CPU0 |

Errors:

| status | meaning |
|---|---|
| -1 | bad frame: bad magic or too long; the connection is closed |
| -2 | unknown op |
| -3 | bad arguments |
| -4 | address range |
| -5 | a name the op does not allow (section 4, Files) |
| -6 | busy |
| -7 | timeout |
| -8 | card I/O or key inject refused |
| -9 | card gate closed |
| -10 | PUT size or CRC mismatch |
| -11 | bad test program header |
| -12 | I-cache invalidate failed |
| -13 | no such file, or its open failed (section 6) |
| -14 | PUT or DEL of `FENYKL.BIN`: the stable image is installed by hand only |
| -15 | POWEROFF after a Wi-Fi stop that timed out: no inject this boot |

Busy (-6) covers two cases: while a CPU0 job is outstanding, payloads over 32 bytes, WRITE and every
CPU0 op are refused; while a test program runs, TPLOAD and TPRUN are refused.

## 4. Safety rules

- **RAM only.** READ accepts only DRAM 0x60021000..0x80000000 and its uncached alias
  0xA0021000..0xC0000000, never MMIO and never below the image. WRITE accepts the same, less the
  monitor's own payload area 0x60DA5000..0x60E10000 (payload, `.bss`, FENYKL task stack), the
  loader stub 0x6005E000..0x6005E21A and its call patch 0x6018D73A..0x6018D741, in either alias.
- **WRITE and the caches.**
  - An uncached-alias address is written by CPU1 straight to DRAM. A core that holds those lines in
    its D-cache keeps reading its old copy until the line is evicted, and if the line is dirty its
    eviction overwrites the new bytes.
  - A cached-alias address is written by the CPU0 monitor task through CPU0's cache, followed by a
    D-cache write-back. CPU0 sees the bytes at once; CPU1 and CPU2 see them only if they do not hold
    the lines, and CPU0's I-cache is not invalidated (code goes through TPLOAD).
- **READ** of a cached address shows CPU1's view. Use the uncached alias for data that CPU0 changes.
- **Settings.** No op writes camera settings, props or firmware files. But a WRITE or a test
  program's `wr32` can change the RAM settings store, a DIAL changes the shooting mode in it as a
  dial turn does, and a POWEROFF afterwards runs the stock shutdown, which saves it. **After any
  WRITE, TPRUN or DIAL, end the session by cutting power with the relay (`fenykl dev power off`),
  not with POWEROFF**, unless the pokes are known harmless. A `dial P` brings the mode back only
  for the rest of the session: a scene or art filter chosen in the picker, or a recalled Myset,
  stays in the settings store.
- **Test programs** are arbitrary CPU0 code: `wr32` writes any address, MMIO included, by design.
- **Files:** one allow-list rule (`fdm_name_check`) for every file op:

  | name | GET | PUT | DEL | LIST |
  |---|---|---|---|---|
  | `FENYKL.<1-3 of A-Z 0-9>` at the card root | yes | yes | yes | - |
  | `FENYKL.TMP` | yes | - | yes | - |
  | `FENYKL.BIN` | yes | -14 | -14 | - |
  | `SHOT<4 digits>.PPM` at the card root (the screenshot library's files) | yes | - | - | - |
  | `DCIM/<3 digits, 5 of A-Z 0-9>/<1-8 of A-Z 0-9 _>.JPG`, `.ORF` or `.MOV` | yes | - | - | - |
  | `DCIM` and `DCIM/<3 digits, 5 of A-Z 0-9>` | - | - | - | yes |

  - The PC sends the name with `/` separators; the camera adds `C:\` and turns each `/` into a
    backslash.
  - Anything else is refused (-5): backslashes, empty or doubled separators, `.` and `..`, colons,
    wildcards, lower case, other prefixes and extensions (`E*.BIN`, `.WAV`), the `DCOLYMP` folder
    and bytes hidden after a NUL.
  - It runs when the request is parsed and again right before every open, remove and listing.
  - Photos and screenshots are never written or removed: no op opens them for writing.
  - `FENYKL.BIN`, the loader's stable image, is read only: PUT and DEL refuse it (-14), and the
    CPU0 file layer refuses to open it for writing or remove it. With one loader file name and no
    rename, no sequence of card operations replaces it safely across a power cut, so it changes
    only by hand. Test builds go as `FENYKL.TST`, which the loader runs once and deletes; a torn
    TST fails the loader's checks and the boot falls back to `FENYKL.BIN`.
  - `FENYKL.TMP` is PUT's temporary file. It can be read and deleted but is never a PUT target.
  - Every file op first checks the card gate (`fenykl_card_io_allowed`).

## 5. Test programs

ABI (`include/tp.h`):
- A program is linked at its slot by `fenykl tp build -t em10ii --slot N src.c [-o DIR]`, using
  `sdk/tp/tp.ld` and `tp_crt.c`. It includes only `tp.h`: no `fenykl.h` and no firmware thunks.
- The 32-byte header at the slot start holds magic `FKTP`, api 1, base (the slot), size (the file),
  mem (size plus `.bss`), entry, and the CRC-32 of bytes 32..size.
- The entry is `int tp_main(const struct fenykl_tp_api *api, uint32_t arg)` (D0, D1). It runs on
  the test program task's 12 KiB stack.
- The function table is version 1 (`version`, `size`, `log`, `sleep_ms`, `timer_us`, `rd32`,
  `wr32`). Later versions only append members, so a program checks `api->size` before using a newer
  one.
- `log` formats at once (`%x %X %u %d %c %s`, a width zero-pads, cut at 191 characters) into the
  card log and the LOG text.
- `rd32` and `wr32` are plain accesses to any address, MMIO included.
- `sdk/tp/examples/peek.c` reads the word at `arg`, logs it and returns its low 31 bits. It runs in
  the simulator in `tests/test_devmon.py`.
- `sdk/tp/sampler/` is the memory sampler (section 12).

TPLOAD and TPRUN:
- TPLOAD copies the bytes into the slot through the cached alias, on CPU0.
- TPRUN checks the header against the slot, clears `.bss`, writes back the D-cache, and invalidates
  CPU0's I-cache, as code hooks do. Then it wakes the test program task.
- If the I-cache step fails, the program is not run (-12). The reply carries the result and the run
  time.
- **A program that does not return in time gets a timeout reply (-7), and it keeps running: the
  monitor cannot kill it.** PING shows `exec_busy=1` until it returns, and TPLOAD and TPRUN answer
  busy. POWEROFF still works while the monitor task runs.
- A crash or a hang of CPU0 ends in a watchdog reset. That is a stock boot without the payload.
  Then power-cycle the camera.
- Writing a slot with WRITE (uncached) instead of TPLOAD can be undone by the D-cache write-back if
  CPU0 holds dirty lines there from an earlier program. Use TPLOAD.
- After a TPRUN, end the session with the relay, not POWEROFF (section 4, Settings).

## 6. Files

Every request opens and closes its file, so no file (`FENYKL.LOG` above all) stays open between
requests.

- **FPUT** sends chunks in order (offset = bytes sent so far). Offset 0 removes the temp file and
  writes the first chunk to it; later chunks append.
  - On the last chunk the monitor reads the temp file back to check its size and CRC-32.
  - It then removes the target, checks that it is gone, copies the temp file to the target and
    checks the target the same way. A failed copy or check is retried from the checked temp file,
    3 tries in all. The temp file is removed only after a good copy.
  - The window: between the remove of the target and a good copy there is no valid target. A power
    cut there leaves the checked `FENYKL.TMP`, which `fenykl dev get FENYKL.TMP` recovers. This is
    why `FENYKL.BIN` is never a PUT target.
  - `"w"` does not truncate, which is why every remove is checked.
  - The last chunk carries the commit: the client waits at least 35 s for its reply. A timeout or a
    lost connection there is reported as "card state unknown". The loop then waits and checks the
    file with GET before anything else, and **never power-cycles on a PUT timeout**.
- **FGET** opens, seeks to `offset` with `fileif_fseek` (past the end: the end), reads `len` bytes
  and closes. The client asks for 16 KiB at a time until a short chunk.
  - The replies of the CPU0 ops, PING and TIMING go out in READ's default pieces (8 KiB, 1 ms
    pause), so a 16 KiB FGET reply does not stall (section 9). READ uses its own piece and pause;
    LOG sends its header and at most two runs of the text ring unpaced (at most 7 KiB). The
    camera's memory is the job buffer and the file layer's own 16 KiB bounce buffer, whatever the
    file size.
  - A 5-10 MB JPEG takes 320-640 requests, a 15-20 MB ORF about 1000-1300; each is short, so the
    60 s socket timeouts do not apply to the whole file. Offsets stop below 2 GiB (`fileif_fseek`
    takes an int), so a MOV past 2 GiB cannot be read to its end.
  - `rpc_fread` returns 0 both at the end and on an error, so a read error looks like the end of
    the file. `fenykl dev get` therefore checks a photo against the size in its folder's listing
    (one FLIST of the folder; `--latest` has it already) and fails on a mismatch. Root files
    (`FENYKL.*`, `SHOTnnnn.PPM`) have no size source: their size is unchecked, and the client says
    so.
  - Every failed open answers -13, a card error on an existing file too. `--latest-shot` and the
    client's `exists` treat -13 as absent, so a card error there ends the search early.
- **FLIST** lists `DCIM` (its `<3 digits, 5 of A-Z 0-9>` folders) or one such folder (its JPG, ORF
  and MOV files) through `fileif_findfirst`/`findnext`/`findclose` with the pattern `*`. Entries
  that the read rule would refuse are left out, `.` and `..` among them. The card root cannot be
  listed (the firmware's search needs a folder).
  - Reply: u32 next, then entries of 20 bytes: the name (12 bytes, NUL-padded), u32 size, u32 FAT
    write time (date << 16 | time). At most 819 per reply; next is the start for the next request
    (counting listed entries only), 0 at the end.
  - The firmware rescans the folder from its start for every entry, so a listing costs entries² / 2
    entry visits. The listing runs in the CPU0 monitor task, which does nothing else meanwhile: no
    other CPU0 op (POWEROFF included), no log drain, no status. It gives up after 25 s (-7, logged
    with the number of listed entries reached), under the server's 30 s wait, so the monitor task
    is free again for the next op; such a folder cannot be listed, its files can still be fetched
    by name (without the size check).
  - A folder that does not exist answers -13.
- **FDEL** succeeds when the file is gone afterwards, including when it never existed.

```sh
uv run fenykl dev ls                                # DCIM's folders
uv run fenykl dev ls DCIM/100OLYMP                  # name, size, write time
uv run fenykl dev get DCIM/100OLYMP/P1180001.JPG -o x.jpg
uv run fenykl dev get --latest [--ext ORF]          # the newest photo of the last folder
uv run fenykl dev get --latest-shot                 # the last SHOTnnnn.PPM, found by probing
```

`--latest` picks, in the last folder with a match, the latest write time, then the file number.
`--latest-shot` searches the run of `SHOTnnnn.PPM` from `SHOT0000` the way the screenshot library
numbers its next file (doubling, then bisection; FGET of 1 byte, -13 = absent). A monitor build
without these ops answers -2, -3 or -5; the client says so.

## 7. Keys

KEY presses a camera button for hold_ms and releases it, through `ui_key_inject_level` (level 0
pressed, 1 released), the injection the firmware's own press-then-release callers use and the one
pre-capture's full press and the power-off's lever use on the camera. It runs in the CPU0 monitor
task; nothing stays pressed after the reply (a failed press is released at once), and nothing is
injected unless a client asks.

| index | key | code | index | key | code |
|---|---|---|---|---|---|
| 0 | OK | 0x0004 | 8 | DEL | 0x0084 |
| 1 | UP | 0x0005 | 9 | FN | 0x000B |
| 2 | DOWN | 0x0006 | 10 | FN2 | 0x1209 |
| 3 | LEFT | 0x0007 | 11 | FN3 | 0x1224 |
| 4 | RIGHT | 0x0008 | 12 | REC | 0x120A |
| 5 | INFO | 0x1205 | 13 | HALF | 0x0100 |
| 6 | MENU | 0x0089 | 14 | FULL | 0x0101 |
| 7 | PLAY | 0x0087 | | | |

The codes are the key layer's (`lib/key_em10ii.c`). FULL is a half press, 300 ms, the full press
for hold_ms, then both released. The power switch is not offered. Only the lever (0x0C) and the
full press are proven by injection on the camera so far; the others are camera-seen key codes.

```sh
uv run fenykl dev key MENU
uv run fenykl dev key HALF --hold 1500
uv run fenykl dev keys "MENU 300 DOWN 200 DOWN OK HALF:1500 FULL"   # pauses in ms, NAME:hold
```

DIAL sets the effective mode-dial position: `ui_key_inject_level(0x02021901, value)` in the CPU0
monitor task pushes the value as the dial's own ladder does, UI_IN posts the dial event and the
camera changes mode (mode guide, then the scene or art picker, as for a turn). The reply is the
value `ui_key_get_state(0x02021901)` read before. The wire carries an index, never a raw value:

| index | position | value | index | position | value |
|---|---|---|---|---|---|
| 0 | P | 0x01 | 5 | STORY (Photo Story) | 0x39 |
| 1 | A | 0x02 | 6 | SCN | 0x2A |
| 2 | S | 0x03 | 7 | ART | 0x33 |
| 3 | M | 0x04 | 8 | IAUTO | 0x31 |
| 4 | MOVIE | 0x0A | | | |

- **Lifetime:** the position lasts until the next edge on the dial ladder's GPIO, which re-reads the
  dial and pushes the real position, or the next boot. A dial movement makes such an edge, and so
  does line noise. How often noise reverts a position is unmeasured.
- **When:** in live view, with the menu closed and no capture or burst running.
- **Myset:** if one of settings 0x1E6..0x1E9 holds the target mode, the camera recalls that Myset
  (1..4). Read them before the first DIAL.
- **End** at the physical position: the `prev` of the session's first DIAL. Then cut power with the
  relay (section 4).
- Run on the camera once: SCN and back to P from a probe boot. SCN showed the mode guide, then the
  scene picker (OK keeps the stored scene); no revert in 4.4 min; P restored the previous state.

```sh
uv run fenykl dev dial SCN    # mode dial 0x1 -> SCN
uv run fenykl dev dial P
```

## 8. Power

POWEROFF and the timed test share one sequence (`fdm_off_step`):
1. `wifi_mngr_stop`.
2. Wait for the stop: the stop callback, or the WLAN state reading 0, for at most 30 s. If the stop
   does not finish, there is no inject and the camera stays on.
3. Wait 5 s after the stop (the gap the probe was proven with), then flush the log.
4. Inject the power switch's Off level (`ui_key_inject_level(0x0C, 1)`). The app then runs its normal
   power-off.

No `FENYKL.*` file is open at that point (section 6). Nothing is logged after the inject, not
even by a test program that is still running.

A POWEROFF after a stop that timed out answers -15: there is no second attempt this boot.

There is no REBOOT op. The software reboot is a watchdog reset: no settings save, no FAT unmount, and
it boots stock without the payload. Restart the monitor with a power cycle instead.

`fenykl dev power on|off|cycle` runs shell commands from environment variables, for example a relay
or a switched dummy battery:
- `FENYKL_DEV_POWER_ON`
- `FENYKL_DEV_POWER_OFF`

`cycle` is off, then `--delay` seconds (3 by default), then on. `fenykl dev wait [--wait-timeout s]`
polls PING until the monitor answers.

## 9. Throughput

Static reading of the olnet send path (firmware v1.4):
- `send()` 0x60E1CF27 cuts the buffer into MSS pieces (0x60E1D9E7). Each piece waits for room in a
  fixed 8 KiB send ring per TCP socket (0x60E261D0; rings set up by 0x60E1D3CA) and kicks the
  output task. No Nagle, no wait for an ACK per segment, no timer-paced output.
- No fast retransmit; the retransmit timeout has a 500 ms floor on a 100 ms TCP tick and resets
  cwnd to 1 MSS. The IP output queue (depth 16, shared with received packets) frees a packet
  silently when it is full. There is no buffer-size or NODELAY option.
- Flags bit 0 of `send()` is urgent data. The server passes 0.

Measured on the camera (1 MiB READs unless noted, `--timing`):

| piece | pause | rate | stalls (send() of 300 ms or more) |
|---|---|---|---|
| 16 KiB (the first default) | none | 23.4 KiB/s | 6, 41.2 s in all, the longest 24.6 s |
| 1460 B (256 KiB) | 2 ms | 10 KiB/s | 21 |
| 1460 B (256 KiB) | none | 6 KiB/s | 20 |
| 4096 B | 1 ms | 115-298 KiB/s | 1-2 |
| 4096 B | 3 ms | 359 KiB/s | 1 |
| **8192 B** | **1 ms** | **505 KiB/s** | **0** |

So the default is now 8 KiB pieces, the size of the send ring, with a 1 ms pause after each. Pieces
larger than the ring stall for seconds, small ones stall often; the stalls fit retransmit timeouts
after local drops (an inference). `fenykl dev read ... --piece P --pace MS --timing` still tries
other settings; TIMING reports total time, send() calls, the longest call and the stalls.

The connection's TCB is at the socket slot 0x619B811C + 0x68 * sock, pointer at +0x30 (`sock` from
TIMING; the mapping and the offsets are read statically): +0x40 snd_una, +0x44 snd_nxt, +0x4C peer
window, +0x5C cwnd, +0x60 ssthresh, +0x64 MSS, +0x7C RTO, +0x80 retransmit count.

## 10. Joining the access point from a PC with only systemd-networkd

iwd associates. systemd-networkd takes the address from the camera's DHCP server without a default
route or DNS, so the PC's other networks are untouched. Replace `wlan0` with the PC's Wi-Fi
interface.

```sh
sudo apt install iwd
sudo systemctl enable --now iwd
sudo tee /etc/systemd/network/50-devmon.network >/dev/null <<'EOF'
[Match]
Name=wlan0

[Network]
DHCP=ipv4
LinkLocalAddressing=no
IPv6AcceptRA=no

[DHCPv4]
UseGateway=no
UseDNS=no
EOF
sudo networkctl reload
sudo tee /var/lib/iwd/FENYKL-DEV.psk >/dev/null <<'EOF'
[Security]
Passphrase=fenykl-devmon
EOF
iwctl station wlan0 scan && sleep 3 && iwctl station wlan0 get-networks
iwctl station wlan0 connect FENYKL-DEV
uv run fenykl dev wait --connect wlan0      # rejoins (iwctl) while disconnected
```

- iwd does not always rejoin by itself after the camera reboots: `fenykl dev wait --connect IFACE
  [--ssid NAME]` runs `iwctl station IFACE connect NAME` while the monitor does not answer, at most
  every 3 s, and only while `iwctl station IFACE show` reports the State `disconnected`, or
  `connected` to a network other than NAME.
  - A connect during an association aborts it ("deauthenticating by local choice" in the
    journal). The access point appears about 15 s after the monitor starts, and an association
    with DHCP takes several seconds, so a connect is never issued while the station is connecting.
  - The iwctl calls are bounded by the wait's own deadline.
  - iwctl failures are printed and the wait goes on.
- If no address arrives: `sudo ip addr add 192.168.0.2/24 dev wlan0`.
- A wired LAN in 192.168.0.0/24 on the same PC clashes with the camera's subnet.

## 11. Screen

`fenykl dev screen -o OUT.png` (`py/fenykl/screen.py`) grabs the GUI's three OSD layers with READ
alone, nothing runs on the camera for it. It writes `OUT.png`, the layers blended over black, and
`OUT-L<n>.png` for each layer read: an indexed PNG holding the camera's palette indices, with that
layer's palette (alpha 0..32 scaled to 0..255).

How a grab works (status: host tests only, `tests/test_devmon.py`):
- Every read goes through the uncached alias, so it sees DRAM as the display controller does.
- `gpi_layer_bpp` must read 04 08 04 and `disp_state` 2. `disp_device` picks the size: LCD (1)
  720 x 480, EVF (6) 640 x 480. Other outputs are refused.
- The buffer read for each layer is the one in the display driver's shadow `osd_shadow` (slots L1,
  L0, L2). A layer hidden in `gpi_hide_mask`, or with a zero shadow, is not read.
- Each layer is 479 x 896 + w bytes (rows of 896 bytes, w used), read in READs of 32 rows
  (28 KiB). The palettes are one READ of 0x500 bytes at `osd_palette`: 256 entries for L1, 32 each
  for L0 and L2, {R, G, B, alpha 0..32}. Indices past a layer's entries are transparent.
- Three layers take 1 290 992 bytes, 2.5 to 4.8 s at 265 to 505 KiB/s, plus two small READs per
  chunk.
- The flip state is `gpi_draw_buf`, the shadows, `disp_state` and `disp_device`. It is read before
  the layers and after every chunk. If any sample differs, the GUI flipped (or the output changed),
  and the grab runs once more. If the second try also sees a flip, its result is saved with a
  warning. If the display state or output changed in it, the grab fails.
- The composite stacks L2, L0, L1, bottom to top, at the layers' palette alpha.

Limits:
- **Live view flips the GUI about 8.7 times a second**, about every 115 ms. A grab of a few
  seconds there sees flips in both tries: it is saved with the warning, and its rows can come
  from different GUI frames. Menus and other still screens do not flip and give a whole grab.
- **Two flips between samples go unseen**, as each flip state field takes only two values. A chunk
  takes 55 to 105 ms, under the time between flips. The command prints the longest time between
  samples; past ~115 ms a grab can be torn without the warning.
- **The CPU0 data can lag.** `gpi_hide_mask`, `gpi_draw_buf` and `osd_palette` are CPU0 data that
  is cached, so DRAM holds them only after CPU0's D-cache writes them back. The shadow is
  uncached, and the OSD buffers are what the controller scans out of DRAM.
- **Display registers are out of reach.** The OSD mix order is assumed to be the mix code 3
  seen in every live-view and menu screen. The EVF's global alpha 0xCC and its 1.6x scaling are not
  applied. Each lives in a display register, MMIO that READ refuses.
- **The display holds only 128 L1 palette entries.** If L1 uses an index of 0x80 or more, the
  grab still decodes it from the RAM palette and warns.
- **Not included:** the live view and the face/AF frames. The live-view frame (YCbCr 4:2:2, 3840-byte rows)
  would be 1.8 MB, 3.6 to 7 s per grab. The firmware rewrites the shown buffer every two flips at
  about 15 flips a second, so a READ that long is always torn. Its address is in a display
  register, or in a driver shadow that is not yet in `firmware.toml`. An untorn copy needs code on
  the camera, as `fenykl_shot_start` does (`docs/shot.md`).

## 12. Memory sampler

`fenykl dev sample CONFIG.toml [--duration S] -o OUT.csv [--keys SEQ] [--image BIN]` reads up to
32 words on the camera at a fixed period and writes them to a CSV. The test program is
`sdk/tp/sampler/` (control block `sampler.h`); the PC side is `py/fenykl/sampler.py`. It is built for
slot 0 on every run unless `--image` names a build of `fenykl tp build -t em10ii --slot N
sdk/tp/sampler/sampler.c`. Status: simulator and host tests only.

```toml
mode = "change"          # or "periodic" (default)
period_us = 1000
duration_s = 20          # --duration overrides; one of them is required, at most 3600 s

[[word]]
name = "qptr"            # CSV column: letters, digits, _ and .
addr = 0x8FF00194        # the value at addr; width 8, 16 or 32 (default 32)
trigger = true           # change mode: a change of (value & mask) makes a record

[[word]]
name = "cmd"
ptr = 0x8FF00194         # one level of pointer: the u32 at ptr, then that + offset
offset = 0x44
width = 8
trigger = true
mask = 0xFF              # default: the whole width
```

Modes:
- **periodic:** a record every `period_us`.
- **change:** all words are read every `period_us`. A record is made when a `trigger` word's masked
  value, or whether its pointer was valid, differs from the last pass. The first pass is always
  recorded. Any number of words can be triggers, so a pointer word and a word behind it record
  when either changes. A `[trigger]` table (`addr` or `ptr` and `offset`, `width`, `mask`) is
  short for one more trigger word, in the CSV column `trigger`.

Pointer words:
- The pointer is read with a 32-bit access in the same pass as the other words, once per pass. Every
  later word with the same `ptr`, and a 32-bit plain word at that address listed before them, use
  that one read, so the words behind one pointer always come from one target.
- The value at pointer + offset is read only if `[p + offset, + width/8)` lies in the RAM the READ op
  accepts and is aligned to the width. That RAM is DRAM 0x60021000..0x80000000 and its uncached
  alias 0xA0021000..0xC0000000; DSP local memory is in neither.
- Otherwise the record holds the pointer in that word's slot, sets the word's bit in its `bad` mask
  (CSV `bad:0x<pointer>`), and the control block counts the read as invalid.

CSV columns: `timestamp_us` (microseconds since the run's start on the camera's 1 MHz timer, a u32
the 3600 s limit keeps from wrapping), `lost` (passes lost just before this record, overruns and
drops together), then one hex column per word.

How it runs:
- **Memory:** the control block (0x280 bytes) sits at the start of the slot after the sampler's, and
  the ring follows it up to the job buffer at 0x6100A800. From slot 0 that is about 480 KiB:
  about 3400 records of 32 words, 24500 of 1. The program refuses a block or a ring outside the
  slots after its own image, up to the job buffer (state -6, or no state when `arg` itself is
  outside: then the block is not the program's to write).
- **Caches:**
  - The PC writes the control block through the cached alias, so the WRITE runs on CPU0, through
    its cache, and ends with a D-cache write-back and invalidate. No CPU0 line of the block is
    left that a later write-back could put over newer bytes (the hazard of section 5). TPRUN writes
    back and invalidates the whole D-cache again before the start.
  - `arg` is the block's uncached alias. The program reads the config, `stop` and `tail`, and
    writes the records, `head` and the counters, only through that alias, so DRAM always holds
    them, no line of the region is made dirty during the run, and CPU1's READ sees them.
  - The PC READs the block and the ring, and WRITEs `stop` and `tail`, at the uncached alias; CPU1
    serves both directly.
- **TPRUN and the server:** TPRUN is synchronous.
  - The CPU1 server waits inside the request (`fdm_job`) until the result or `timeout_ms` and reads
    no other frame meanwhile. The job slot stays busy too, so READ, KEY and every other op wait.
  - The PC therefore starts the sampler with `timeout_ms` 1. The CPU0 monitor task answers TIMEOUT
    at its next 5 ms poll, the job slot is free again, and the program keeps running in its own
    task (PING `exec_busy=1`).
  - From then on READ, PING, LOG and uncached WRITE run on CPU1. KEY, DIAL and cached WRITE run in
    the monitor task, which still runs: TPRUN does not block the CPU0 job runner, only the request
    that waits for it. TPLOAD and TPRUN answer busy until the sampler returns.
  - The monitor logs its "tp still running ... timeout reply" line on every run; this is expected.
    The program's return value is lost, so the `state` word is the result.
- **Keys:** the server takes one client at a time, so a `fenykl dev key` in another shell waits
  until the sample ends. Give `--keys "MENU 300 DOWN OK"` (the `dev keys` syntax) instead: the
  steps run on the sampler's connection during the run. A KEY holds the connection for its hold
  time; the ring takes up the records meanwhile.
- **Ring:**
  - The program publishes `head` after each complete record. The PC takes the records from `tail`
    to `head`, then WRITEs `tail`.
  - A record with no room is a drop and is never overwritten. In change mode, the first pass with
    room after a drop records the state as it is then, even without a new change. A change
    dropped is counted once, however long the ring stays full.
  - Each record starts with its index since the start, `seq`. The program writes `seq` last.
  - READ copies the uncached ring and block while the program changes them, so one copy of `head`
    can be torn. The PC takes a record only when its `seq` is the index it expects; it stops at
    the first that is not and takes it at the next fetch.
- **Loss:**
  - A pass that starts one period or more late counts the missed periods as overruns.
  - Drops and overruns are totalled in the control block and also go into the next record's
    `lost`. `fenykl dev sample` prints both totals and the count of invalid pointers.
- **Period:**
  - Before the run, the program times 16 full passes. The second longest, plus 1 us for the
    timer's resolution, is `loop_us`, and a `period_us` below it is refused. The longest is left
    out, because an interrupt may have hit it.
  - These 16 passes read every word, so a register cleared by its read is read 16 times before
    the first record.
  - During the run, the program records the longest pass in `loop_max_us`.
  - While 2 ms or more are left to the next pass, the program sleeps, in pieces of at most 100 ms
    that check `stop` and the duration in between. It spins through the last 1 to 2 ms before each
    pass, and through all of a period shorter than 2 ms.
  - The program task has priority 55. It shares its round robin, rotated every 5 ticks, with the
    monitor task, the watchdog kicker and the log and cfg tasks, so a spinning sampler shows a gap
    (overruns) when they run. Tasks of a lower priority (above 55) do not run while it spins.
- **Throughput:**
  - A run keeps up when its records arrive more slowly than READ fetches them: 265-505 KiB/s, less
    a READ, a WRITE and a status READ per fetch. For 32 words (144-byte records) that means periods
    of about 0.5 ms or more.
  - Shorter periods work for bursts up to the ring's size.
- **Safety:**
  - Plain words are read with exact 8, 16 or 32-bit accesses, MMIO included.
  - Both the PC and the program refuse a word or pointer address in DSP local memory, 0x9D000000..
    0x9DFFFFFF, and at the bases that differ from it only in the top two address bits (0x1D, 0x5D,
    0xDD), in case those alias it. A CPU0 read there outside a DSP job hangs the bus, because the
    DSP's clock is gated.
  - Both also refuse an address that is not aligned to its width.
  - The program always ends: `duration_ms` is 1..3600000.
  - The PC sets `stop` at the duration plus 5 s, on Ctrl-C (from the start of the command), and on
    any error once TPRUN has gone out. On an error the connection may be gone, so that last
    `stop` is a best effort and its failure is printed.
  - The PC reports a program that does not stop within 5 s after `stop`.
