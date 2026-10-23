# Screenshot (`sdk/lib/shot.c`)

Status: the pixel work (`shot_pix.c`) is simulator-tested (`tests/sim/shot/`). Used by main's Fn
override ("screenshot") and by `projects/birddetect6` (short Fn in live view or the menu). Built for
bodies with the `shot` capability (`include/body.h`; the E-PL3); a plugin that does not call
`fenykl_shot_*` gets no byte of it (`--gc-sections`).

## 1. What it does

Saves what the panel shows, the live view, the firmware's face/AF frames and the GUI's three OSD
layers (the camera's own icons, AF targets and any HUD), as `C:\SHOTnnnn.PPM`: binary PPM, 853 x 480 (`FENYKL_DISP_SQUARE_W` x
`FENYKL_DISP_H`), 1 228 335 bytes.

- **Snapshot** (`fenykl_shot_start`): borrows a RECFILE block run of 1 551 336 bytes (refused, not
  waited for, when no run is free) and copies into it the live-view buffer the display shows, placed
  by its flip-table entry, the shown face/AF frame buffer, and the shown buffer of each OSD layer with
  its palettes. The live-view copy starts at a flip and is retried (3 tries) if more than one flip
  passed during it; the frame copy (3 tries) and the OSD copy (5 tries) are retried until no flip
  happened during them. A torn copy is still saved and logged.
- **Face/AF frames:** the firmware's frame task (`TSK_DISP_FRAME`) draws the face-detection frames,
  and so any face a plugin injects, not on the OSD but into two ping-pong buffers it flips onto the
  display's third image layer (img3): 8 bpp palette indices, 448-byte pitch, 426 x 240 on the LCD
  (320 x 240 elsewhere), scaled by the display to 720 x 480, above the live view and below the OSD.
  Index 0 is transparent; the 8 colours (white, green, orange, ...) come from the frame task's RGB
  table. The shot reads the shown buffer from the display's img3 shadow and the size from the
  firmware's img3 layer setup, and scales it the same way (nearest pixel).
- **Write** (`fenykl_shot_step`): each call composes 24 canvas rows (live view YUYV to RGB, the
  face/AF frames, the OSD layers blended bottom to top with their 4-bit alpha), stretches them to 16:9 and writes them. 20
  calls finish the file; at one call per 50 ms tick that takes 8-9 s on the camera, during which the calling task
  keeps working. The RECFILE run is given back when the file is closed or a step fails.
- **Numbering:** the first shot of a boot finds the end of the run of files from `SHOT0000.PPM`
  (doubling, then bisection); each shot then takes the next free number, up to `SHOT9999.PPM`.

## 2. API

Header `sdk/include/shot.h` (the reference). Log module `shot` (`fenykl_shot_log`).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_shot_start()` | CPU0 task, in live view | snapshot, file open and header (up to ~0.3 s: it waits for a live-view flip); `true` = busy, `false` = no screenshot (logged) |
| `fenykl_shot_step()` | the same task, every ~50 ms while busy | one strip; `false` once the file is written or a write failed |
| `fenykl_shot_status()` | any task | logs `shots N failed N, torn lv N osd N` (and torn frame copies, shots without live view) |

The caller decides when to shoot and serialises: no `fenykl_shot_start` while busy. Both plugins take
the press through the key layer (`docs/key.md`): a claimed short Fn in live view or the menu (the
live view keeps running behind it), `long_stock` for the stock Fn on a long press, presses while busy
ignored and logged.

## 3. Resources

- **RECFILE** is the still pipeline's buffer: a still started during the write finds 1.5 MB less
  free (the pool is 0x1E3F480 bytes).
- **Card:** the file goes through the firmware's file server like the log; nothing is written while
  `fenykl_card_io_allowed()` is false (a shot in progress then fails with `card gone while writing`).
- **CPU0:** the copies and the strips run in the caller's task. A plugin with time-critical work
  runs the shot in a task of lower priority (birddetect6: 13, below its control task at 12).

## 4. Log lines (module `shot`)

| line | meaning |
|---|---|
| `SHOTnnnn.PPM written in N ms (capture N ms)` | done |
| `card not available: no screenshot`, `SHOT9999.PPM reached: no screenshot`, `RECFILE has no free run of N bytes: no screenshot` (WARN) | refused |
| `lv torn: N flips in a N us copy`, `osd torn: the GUI flipped during every copy`, `frames torn: flipped during every copy` (WARN) | saved anyway |
| `frames X not a DRAM buffer: not drawn`, `frames WxH -> WxH: not drawn` (WARN) | no face/AF frames in the file |
| `img1 X not a DRAM buffer: OSD only` (WARN) | no live view in the file |
| `SHOTnnnn.PPM: <what>` (ERR) | failed: `no buffer`, `no OSD planes`, `cannot open`, `write failed`, `card gone while writing` |
| `lv ...`, `osd ...`, `frames ...`, `img1 regs ...`, `no frames layer` (DEBUG) | placement, copy times and retries |

## 5. Tests

`tests/test_sim.py` (`shot`, `tests/sim/shot/test_shot.c`): YCbCr to RGB against a BT.601
reference, palettes, live-view placement, alpha blending, 4-bpp nibble order, layer order, the
face/AF frame layer (scaling, transparent indices, stacking), the 16:9 stretch and the PPM header.
