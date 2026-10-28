# Own contrast client (`sdk/lib/afc.c`)

Status: E-PL3: camera-proven in a probe plugin (8 clean attach/detach cycles, S-AF locking every
time afterwards); used by the `main` focus meter in MF. E-M10 II: camera-proven in `main` (4 MF
attach/stop cycles, S-AF locking afterwards).
The pending stop while live view is off (section 3) is simulator-tested only (`tests/sim/afc/`).
Built for targets with the `afc` capability; a plugin that does not call `fenykl_afc_*` gets no byte
of it (`--gc-sections`, including the DMA buffer in `.shared`).

The library is camera-independent: every firmware layout (request, contrast_setting, events, DMA
buffer, the foreign-client and live-view tests, the snapshot conditions) comes from the body's
`fenykl_cnt_*` functions and `FENYKL_CNT_*` constants (`include/body.h`, afc group, implemented in
`arch/<camera>/fenykl_body.h`).

## 1. What it does

In manual focus the firmware runs no contrast client, so the AF statistics block produces nothing.
The library attaches **its own** client with `cam_request` (type 7, asynchronous path) while the
camera is in MF stills live view, so the hardware writes the grid of HPF values into the library's
DMA buffer every frame (~60 per second), and detaches before the firmware could want the block.

- **contrast_setting:** a snapshot of fcs's own, taken whenever through AF runs in S-AF or C-AF
  framing (`fenykl_cnt_fcs_setting`: fcs running, the stills framing grid, number 0xFFFF, fcs's DMA
  buffers, fcs's request pointing at it, no half-press; two copies must agree on the parameter part
  `FENYKL_CNT_SET_INFO`). At each attach the copy gets number = 0xFFFF, the update bits and the
  library's DMA buffer (`fenykl_cnt_setting_prep`). **No snapshot, no attach:** a camera that framed
  only in MF since power-on cannot attach (logged once at INFO). The snapshot is in RAM only.
- **DMA buffer:** `FENYKL_CNT_DMA_WORDS` in `.shared`, used only through the uncached alias, filled
  with `FENYKL_AFC_DMA_FILL` before each attach (the DMA writes from the second frame on).
- **Detach:** `contrast_setting.number = 0`; the driver checks it after the frame-start callback of
  each frame, unlinks the client and calls the done callback (12-18 ms on the E-PL3). The task writes
  it on a gate; the frame callback writes it itself on a half-press (fastest detach) and at every
  frame after a stop.
- **Callbacks** (`cam_cnt_frame_cb`, `cam_req_done_cb`): firmware context on CPU0 (E-PL3: interrupt
  context). Counters, timer stamps, the context (interrupt nesting byte, PSW, A0 of the first frame
  callback) and the number = 0 write only. A call on another core counts itself in an uncached word
  and returns; the task then stops the client for the boot (`core`).

| | E-PL3 v1.6 | E-M10 II v1.4 |
|---|---|---|
| request | 0x40 bytes: +0x14 setting, +0x28 done, +0x2C frame, +0x30 events, +0x34 argument | 0x88 bytes: +0x1C setting, +0x38 done, +0x4C frame, +0x6C events; no argument |
| events | 0x400 frame start, 0x800 data complete | 0x001 frame start, 0x002 data complete, 0x200 error |
| contrast_setting | 0x104 bytes (fcs ctx +0x378), DMA +0x88, update u16 0x1FFF | 0x228 bytes (fcs ctx +0xD24), cnt DMA +0x8C and fc DMA +0x90 (both set to the library's buffer), update u32 0x60FFF |
| snapshot grid | pattern 0 or 0xD, 7 x 5 | pattern 0, 9 x 9 (vdiv - area_add = 9 rows) |
| DMA buffer | 2 KiB: HPF1, HPF2, HPF3, HPFV, 5 rows each | 4.5 KiB, 128-byte aligned: cnt (HPF1..3, HPFV) at 0, fc (HPF1..3) at 2.5 KiB, 9 rows each; `fenykl_afc_dma` returns fc |
| foreign client | slot words 0x605AEFA0/A4, client +8 = setting | list `*(*(u32 *)0x618485BC + 4)`, node +0 setting, +0xC next, pool of 2 |
| live view | GUI super 0x02, states 0x03..0x05, 0x09..0x0B | GUI super 0x01, state 0x08 |
| movie | take_focus +7, lv_mode +0xD4 | take_focus +8 |
| half press | `trg_snapshot` bit 0 clear | u8 0xA1C1801C == 1 |
| driver debug | u16 0x605AEFD4 | none found |

E-M10 II update bits: `cam_cnt_apply` 0x60243E6F applies bits 0x1..0x40000 of the u32 at +8 and
clears it; fcs only ever sets 0x1..0x800, 0x20000 and 0x40000, so the copy sets those (0x60FFF) and
leaves the five register groups fcs never programs (0x1000, 0x2000..0x10000) alone.

## 2. API

Header `sdk/include/afc.h` (the reference).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_afc_init()` | CPU0 task, once | starts the clock (no attach in the first 5 s) |
| `fenykl_afc_poll(enable)` | plugin task, every 10..50 ms | snapshot, gates (`enable` false = gate `G_OFF`), callback errors, state machine, attach or stop |
| `fenykl_afc_end()` | CPU0 task | stop for good (err `END`); no poll needed afterwards |
| `fenykl_afc_get()` | any | the library state (read-only): state machine, callback counters, gates, grid, snapshot |
| `fenykl_afc_dma()` | any | the statistics buffer (`FENYKL_CNT_STATS_CHANNELS` from HPF1): channel c, area i at `c * rows * 16 + i / cols * 16 + i % cols` |
| `fenykl_afc_failed()` | any | an error stopped the client for the boot |
| `fenykl_afc_other_core()` | any | callbacks that ran on another core |

Data is valid while the state is `ATTACHED` and the area's words are not `FENYKL_AFC_DMA_FILL`.

## 3. State machine (`afc_sm.c`)

idle -> attaching (`cam_request`) -> attached (first callback) -> detaching (number = 0, by the task or
the frame callback) -> idle (done callback) or off.

- **Attach** only when every gate is clear for 0.5 s, 5 s after `fenykl_afc_init` and 1 s after the
  last done callback. Gates (`gates`, `FENYKL_AFC_G_*`): 001 no snapshot, 002 not MF, 004 GUI not on a
  stills live-view screen (`fenykl_cnt_lv`), 008 movie (`fenykl_cnt_movie`), 010 half or full press,
  020 fcs contrast state not idle, 040 a foreign client linked in the driver (`fenykl_cnt_foreign`),
  080 driver debug mode (`fenykl_cnt_drv_dbg`), 100 disabled by the caller.
- **Stop** at once when any gate fails (`task`), when the frame callback wrote number = 0 on a
  half-press (`cb`; its done may come before the task notices: a normal detach), or when no frame
  came for 0.5 s while attached (`stall`, why 0x200: live view went away before the GUI state did).
- **Pending stop:** the driver detaches at the next frame, so with live view off (MENU, playback: no
  frames) the done callback comes only when frames resume, seconds later (6.5 s in an E-PL3 run). The
  stop then stays pending with no timeout (`stop #n pending: no frames`), and no attach happens
  before its done. The 0.5 s done timeout runs only from the first frame after the stop.
- **Errors, then off for the boot** (one ERR line `off for the boot: <err>`): `req` (`cam_request`
  nonzero), `noframe` (no callback within 0.5 s of the attach), `nodone` (frames after the stop but no
  done within 0.5 s), `early` (done while attached, no stop requested: someone else detached it;
  number = 0 is written anyway),
  `event` (the frame callback got the error event), `core` (a callback ran on another core). Normal
  cycles are at least 1.5 s apart (settle and cooldown), so a gate that flips cannot make a storm.

## 4. Log lines (module `afc`)

| line | meaning |
|---|---|
| `snap #n: pat P, CxR` | snapshot taken (INFO for the first, DEBUG for later ones) |
| `attach #n: cam_request R, pat P` | request sent (R is 0 on the asynchronous path) |
| `attached #n: first cb U us after the request` | the first frame callback came |
| `  cb nest N psw P a0 A req Q` | its context: interrupt nesting byte, PSW, A0 and our request |
| `stop #n: B, gates G, F frames` | B = `task`, `cb` or `stall`; F = frame-start callbacks of this attach |
| `stop #n pending: no frames` | the stop waits for live view (section 3) |
| `done #n: U us after the stop, status S, nest N` | detached |
| `MF without a snapshot: frame in S-AF or C-AF first` | once: MF wanted, nothing to copy |
| `off for the boot: E (attach #n, sm S)` (ERR) | error, passive from now on |

## 5. Risks

- The driver keeps one current and one queued client (E-M10 II: a node pool of 2); a third gets an
  error and fcs's AF would time out once (2000 ticks) [inferred]. The gates (fcs idle, no foreign
  client, no half-press, the frame callback's own stop) keep fcs from wanting the block while
  attached; E-PL3 camera runs saw no error.
- Wrong setting fields (a snapshot from another sensor mode): wrong windows, but the DMA writes only
  the snapshot's rows, which the buffer holds with slack.
- E-M10 II: the callbacks run in an ISR with A0 = the client's request (camera run). MF assist
  magnification may change the sensor readout under the copied windows.
- Nothing is written to firmware settings, flash or properties; fcs's own client is never touched.
