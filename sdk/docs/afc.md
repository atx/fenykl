# Own contrast client (`sdk/lib/afc.c`)

Status: camera-proven in the `mfstats` probe (8 clean attach/detach cycles, S-AF locking every time
afterwards); used by the `main` focus meter in MF. The pending stop while live view is off
(section 3) is simulator-tested only (`tests/sim/afc/`). Linked into every plugin, like all of
`sdk/lib/`; a plugin that does not call `fenykl_afc_*` gets no byte of it (`--gc-sections`, including
the 2 KiB DMA buffer in `.shared`). Firmware facts are for v1.6.

## 1. What it does

In manual focus the firmware runs no contrast client, so the AF statistics block (0x99C00000)
produces nothing. The library attaches **its own** client with `cam_request` (type 7, asynchronous
path) while the camera is in MF stills live view, so the hardware writes the 7 x 5 grid of HPF1/2/3/V
values into the library's DMA buffer every frame (~60 per second), and detaches before the firmware
could want the block.

- **contrast_setting:** a snapshot of fcs's own (ctx+0x378), taken whenever through AF runs in S-AF or
  C-AF framing (fcs state 2, pattern 0 or 0xD = 7 x 5, number 0xFFFF, fcs's DMA buffer, no half-press;
  two copies must agree). At each attach the copy gets number = 0xFFFF, update = 0x1FFF and the
  library's DMA buffer. **No snapshot, no attach:** a camera that framed only in MF since power-on
  cannot attach (logged once at INFO). The snapshot is in RAM only.
- **DMA buffer:** 2 KiB in `.shared`, used only through the uncached alias, filled with
  `FENYKL_AFC_DMA_FILL` before each attach (the DMA writes from the second frame on).
- **Detach:** `contrast_setting.number = 0`; the driver checks it after the AF_ST callback of each frame,
  unlinks the client and calls the done callback (12-18 ms on the camera). The task writes it on a
  gate; the frame callback writes it itself on a half-press (fastest detach) and at every frame after a
  stop.
- **Callbacks** (`cam_cnt_frame_cb`, `cam_req_done_cb`): interrupt context on CPU0; counters, timer
  stamps and the number = 0 write only.

## 2. API

Header `sdk/include/afc.h` (the reference).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_afc_init()` | CPU0 task, once | starts the clock (no attach in the first 5 s) |
| `fenykl_afc_poll(enable)` | plugin task, every 10..50 ms | snapshot, gates (`enable` false = gate `G_OFF`), state machine, attach or stop |
| `fenykl_afc_end()` | CPU0 task | stop for good (err `END`); no poll needed afterwards |
| `fenykl_afc_get()` | any | the library state (read-only): state machine, callback counters, gates, grid, snapshot |
| `fenykl_afc_dma()` | any | the statistics buffer: channel c, area i at `c * rows * 16 + i / cols * 16 + i % cols` |
| `fenykl_afc_failed()` | any | an error stopped the client for the boot |
| `fenykl_afc_slot_owner(slot)`, `fenykl_afc_take_gap()` | any | probe helpers (mfstats) |

Data is valid while the state is `ATTACHED` and the area's words are not `FENYKL_AFC_DMA_FILL`.

## 3. State machine (`afc_sm.c`)

idle -> attaching (`cam_request`) -> attached (first callback) -> detaching (number = 0, by the task or
the frame callback) -> idle (done callback) or off.

- **Attach** only when every gate is clear for 0.5 s, 5 s after `fenykl_afc_init` and 1 s after the
  last done callback. Gates (`gates`, `FENYKL_AFC_G_*`): 001 no snapshot, 002 not MF (take_focus +5 != 1),
  004 GUI not a stills live-view screen (super 0x02, state 0x03..0x05 or 0x09..0x0B: no menu, playback,
  Live Control, magnified view, shooting, record view), 008 movie (take_focus +7) or lv_mode (+0xD4),
  010 half or full press, 020 fcs contrast state 0x605A8C38 not idle, 040 a foreign client in the driver
  slots 0x605AEFA0/A4, 080 driver debug mode 0x605AEFD4, 100 disabled by the caller.
- **Stop** at once when any gate fails (`task`), when the frame callback wrote number = 0 on a
  half-press (`cb`; its done may come before the task notices: a normal detach), or when no frame
  came for 0.5 s while attached (`stall`, why 0x200: live view went away before the GUI state did).
- **Pending stop:** the driver detaches at the next frame, so with live view off (MENU, playback: no
  frames) the done callback comes only when frames resume, seconds later (6.5 s in a camera run). The stop then
  stays pending with no timeout (`stop #n pending: no frames`), and no attach happens before its done.
  The 0.5 s done timeout runs only from the first frame after the stop.
- **Errors, then off for the boot** (one ERR line `off for the boot: <err>`): `req` (`cam_request`
  nonzero), `noframe` (no callback within 0.5 s of the attach), `nodone` (frames after the stop but no
  done within 0.5 s), `early` (done while attached, no stop requested: someone else detached it).

## 4. Log lines (module `afc`)

| line | meaning |
|---|---|
| `snap #n: pat P` | snapshot taken (INFO for the first, DEBUG for later ones) |
| `attach #n: cam_request R, pat P` | request sent (R is always 0 on the asynchronous path) |
| `attached #n: first cb U us after the request` | the first frame callback came |
| `stop #n: B, gates G, F frames` | B = `task`, `cb` or `stall`; F = AF_ST frames of this attach |
| `stop #n pending: no frames` | the stop waits for live view (section 3) |
| `done #n: U us after the stop, status S` | detached |
| `MF without a snapshot: frame in S-AF or C-AF first` | once: MF wanted, nothing to copy |
| `off for the boot: E (attach #n, sm S)` (ERR) | error, passive from now on |

## 5. Risks

- The driver keeps one current and one queued client; a third gets error 0x4585 and fcs's AF would
  time out once (2000 ticks) [inferred]. The gates (fcs idle, slots free, no half-press, the frame
  callback's own stop) keep fcs from wanting the block while attached; camera runs saw no 0x4585.
- Wrong setting fields (a snapshot from another sensor mode): wrong windows or a DMA write past the
  used 4 x 5 x 16 words; the buffer has 768 bytes of slack (mfstats checks it).
- Nothing is written to firmware settings, flash or properties; fcs's own client is never touched.
