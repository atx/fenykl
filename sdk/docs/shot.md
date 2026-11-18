# Screenshot (`sdk/lib/shot.c`)

Status: the pixel work and the start/abort rules (`shot_pix.c`) are simulator-tested
(`tests/sim/shot/`, `tests/sim/shot_em10ii/`). Used by main's Fn override ("screenshot"). Built for
bodies with the `shot` capability (`include/body.h`); the PRIM block 0 lend has not yet run on the
camera. A
plugin that does not call `fenykl_shot_*` gets no byte of it (`--gc-sections`).

## 1. What it does

Saves what the panel shows, the live view and the GUI's three OSD
layers (the camera's own icons, AF targets and any HUD), as `C:\SHOTnnnn.PPM`: binary PPM at square
pixels.

| | LCD | EVF |
|---|---|---|
| panel (canvas) | 720 x 480 | 1024 x 768 |
| file | 720 x 480, 1 036 815 bytes | 1024 x 768, 2 359 312 bytes |
| OSD | 720 x 480 at 1:1; 8 bpp | 640 x 480 scaled 1.6x (nearest pixel); 8 bpp; at the EVF's global alpha 0xCC |
| palettes | {R, G, B, alpha 0..32} | as LCD |
| lent buffer | PRIM block 0 (25 776 128 bytes), 1 728 000 used | 2 494 464 used |
| strip | 16 rows in `.bss` (49 152 bytes) | as LCD |

- **Snapshot** (`fenykl_shot_start`): opens the PPM, flushes the log, reads the output and the
  rules (3), borrows the buffer (`fenykl_shot_buf_alloc`, refused, never waited for, when none is
  free; the PPM is then removed) and copies into it the live-view buffer the display shows, the
  shown face/AF frame buffer (none on this body, see Face/AF frames below), and the shown buffer of each OSD layer with its palettes. The
  live-view copy starts at a flip and is retried (3 tries) if more than one flip passed during it;
  the frame copy (3 tries) and the OSD copy (5 tries) are retried until no flip happened during
  them. A torn copy is still saved and logged. The OSD copy first waits (up to
  100 ms) until the display shadows hold `b[draw ^ 1]`: between the GUI's flip and the DISPLAY
  task's commit the panel still shows the old buffer, which the GUI may already draw into. A 32-bit
  sum of the snapshot is kept. The rules are checked again after the snapshot and after the header,
  so a rule broken during the copies (up to ~1.5 s with every wait and retry) aborts at once.
- **Live-view source:** img2 when the display enables it (the menu points it at the
  live-view block shown when it opens), else img1, else black.
- **Face/AF frames:** img3 is not read (`fenykl_frames_shown` is 0: img3's source format is unknown),
  so the file has none.
- **Write** (`fenykl_shot_step`): each call checks the rules (3), composes one strip (live view YUYV
  to RGB, the OSD layers scaled to the canvas and blended bottom to top with their palette alpha)
  and writes it: 30 (LCD) or 48 (EVF) calls.
- **Raw dump** (`fenykl_shot_raw(first)`: the first `first` snapshots of a boot, 0 by default;
  main sets 2): after the PPM, the snapshot itself, in 64 KiB chunks per call: `SHOTnnnn.LV` (the live view as copied,
  YUYV, `2 * w` bytes per row), `.L0`, `.L1`, `.L2` (each layer's copied rows, `w * bpp / 8` bytes
  each), `.PAL` (the firmware's palette block as read, 0x500 bytes) and `.REG` (geometry,
  sources, the lent block and the body's display registers as text), for checking the byte order,
  palettes, alpha and layout against the PPM off the camera. A raw file that fails is logged and
  counted (`raw failed`); the PPM still counts as written.
- **The end:** the snapshot's sum is checked (`snapshot overwritten during the write` on a
  mismatch), then the buffer is given back and the time it was lent is logged. A shot that ends
  before the PPM's last row is written and closed (refused, aborted or failed) gives the buffer
  back, closes the file and removes the PPM unless the card gate is closed; raw pieces already
  written stay.
- **Numbering:** the first shot of a boot finds the end of the run of files from `SHOT0000.PPM`
  (doubling, then bisection); each shot then takes the next free number, up to `SHOT9999.PPM`.

## 2. API

Header `sdk/include/shot.h` (the reference). Log module `shot` (`fenykl_shot_log`).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_shot_start()` | CPU0 task, in live view or the menu | file open, snapshot and header (typically ~0.1-0.3 s for the live-view flip; up to ~1.5 s with every flip and commit wait and retry, and the first shot of a boot also scans the card for the number); `true` = busy, `false` = no screenshot (logged) |
| `fenykl_shot_step()` | the same task, every ~50 ms while busy | one strip or raw chunk; `false` once the files are written, a write failed or the shot was aborted; `false` at once while idle |
| `fenykl_shot_raw(first)` | the same task | the first `first` snapshots of the boot also write the raw pieces |
| `fenykl_shot_status()` | any task | logs `shots N failed N, torn lv N osd N` (and aborted, overwritten and raw-failed shots, torn frame copies, shots without live view) |

The caller decides when to shoot and serialises: no `fenykl_shot_start` while busy. It must keep
calling `fenykl_shot_step` until it returns `false`: the buffer is lent until then. main takes
the press through the key layer (`docs/key.md`): a claimed short Fn in live view or the menu (the
live view keeps running behind it), `long_stock` for the stock Fn on a long press, presses while busy
ignored and logged.

## 3. Resources and rules

- **PRIM block 0** (pool id 0x0000, twelve 0x1895000-byte blocks from 0xA33E8C00): the
  still path's own accounted pool. A still's take handle gets its frame from
  it (`take_buf_alloc` 0x6022DF9D -> `imgbuff_alloc(&id, 0)`), so a still taken during the write
  uses the next block and a burst is one frame shorter; the next burst frame waits for a free block
  (`Get_NumberFree` 0x607EAAF9). Block 0 overlaps no heap (the lowest heap base is 0xA963CC00) and
  never falls in the imgwork reserve (blocks 4..11 or 9..11 per imgwork mode u16 0xA1C2B048, mask
  at descriptor +0x40). The body lends it through the firmware's image-pool API (`imgbuff_alloc` /
  `imgbuff_addr` / `imgbuff_free`, task context: the alloc may sleep on the pool lock):
  `imgbuff_alloc` never waits and takes the lowest free block (id = 1 + index); any id but 1 or an
  address other than 0xA33E8C00 is given back at once. The same path serves live view and the menu.
  Thirteen pools are registered over block 0's bytes with their own accounting (0x0200, 0x0300,
  0x0400, 0x0600, 0x0800, 0x1F00, 0x2000, 0x2700, 0x2A00, 0x3A00, 0x3E00, 0x4200, 0x4F00): the
  body reads their descriptors through the pool lookup table 0x6061858C and counts one as busy when
  its free count (+0x14) differs from its total (+0x18). Pre-capture also reads PRIM: while block 0
  is lent its burst has one block less, and its after-burst check (PRIM free == total within 5 s of
  a cancel or 30 s of a commit) fails safe if a shot still holds the block then.
- **Rules** (`fenykl_shot_gate`, on the body's `fenykl_shot_cond`): a shot starts only with the
  display live (display state 2) on a supported output (LCD or EVF), no frame in the lent
  pool (PRIM in-use mask +4 without the reserve +0x40 is 0), not in movie mode, not in
  playback (GUI super state 6), with prop 0xE951 at 0 (otherwise takes go to pool 0x2700, over
  block 0; unregistered counts as set) and every pool over block 0 idle. It is aborted, the buffer
  given back first, then the file closed and the partial PPM removed, when movie mode, playback,
  prop 0xE951 or a busy pool over block 0 comes up, when the output device changes (checked after
  the snapshot, after the header and at every step) or when the card gate closes. Half-press and
  takes are no rule: a still while the block is held uses the next block. A shot that has held the buffer for 30 s is
  aborted. The log is flushed right before the buffer is lent.
- **Card:** the files go through the firmware's file server like the log; nothing is written while
  `fenykl_card_io_allowed()` is false (a shot in progress then fails with `card gone while
  writing`). A `.bss` strip is written back from the D-cache before each write.
- **CPU0:** the copies and the strips run in the caller's task. A plugin with time-critical work
  runs the shot in a task of lower priority.

## 4. Log lines (module `shot`)

| line | meaning |
|---|---|
| `shot: N free, used X, other pools X`, `  stills shared N, mode X` | each attempt that reaches the lend: PRIM's free count, its in-use mask without the reserve, the busy pools over block 0 (bit i = entry i of the list in 3), prop 0xE951 set, the output device |
| `raw pieces too: shot N of the first N` | this shot writes the raw pieces |
| `snapshot WxH in X, N free after the lend`, `  lv flips N, capture N ms`, `LN largest index X of N entries` | the snapshot: lent address, live-view flips during the copy, the largest palette index in each 8-bpp layer of fewer than 256 entries |
| `snapshot sum X unchanged`, `lent N bytes for N ms`, `  now N free, used X, other pools X`, `SHOTnnnn.PPM written in N ms (capture N ms), N bytes` | done (bytes of every file written) |
| `card not available: no screenshot`, `SHOT9999.PPM reached: no screenshot`, `output not supported: no screenshot`, `<rule>: no screenshot`, `no buffer of N bytes (N free): no screenshot` (WARN) | refused; rules: `movie mode`, `playback`, `stills go to the lent memory`, `a pool over the lent memory in use`, `display not ready`, `a picture in the lent pool` |
| `SHOTnnnn aborted: <rule>` (WARN) | aborted; also `output changed`, `lent too long` (after `lent for N ms: strip N of N, raw piece N`) |
| `SHOTnnnn.PPM partial: removed`, `SHOTnnnn.PPM partial, remove: N` (WARN) | a PPM not written to its end (refused, aborted, failed) removed, or the remove failed; kept when the card gate is closed (`SHOTnnnn.PPM partial, card not available: kept`, WARN) |
| `snapshot overwritten during the write: X, was X` (WARN) | the lent buffer changed while lent |
| `lv torn: N flips in a N us copy`, `osd torn: the GUI flipped during every copy`, `frames torn: flipped during every copy`, `osd flip not committed in N ms` (WARN) | saved anyway |
| `frames X not a DRAM buffer: not drawn`, `frames WxH -> WxH: not drawn` (WARN) | no face/AF frames in the file |
| `live view X not a DRAM buffer: OSD only` (WARN) | no live view in the file |
| `SHOTnnnn: <what>` (ERR) | failed: `no buffer`, `no OSD planes`, `cannot open`, `write failed`, `close failed`, `card gone while writing`; `buffer X not given back: X` |
| `SHOTnnnn.EXT: <what>` (ERR) | a raw file: `cannot open`, `write failed`, `close failed` |
| `lv ...`, `osd ...`, `frames ...`, `img1 regs ...`, `reg NAME ADDR = X`, `no frames layer` (DEBUG) | placement, copy times, retries and the body's registers at each attempt (display, GUI and take state, PRIM mask/free/total/reserve, imgwork mode, pool 0x0E00) |

## 5. Tests

`tests/test_sim.py`:

- `shot_em10ii` (`tests/sim/shot/test_shot_em10ii.c`): the EVF's 640 x 480 OSD scaled
  1.6x by nearest pixel and the LCD's 1:1, RGB palettes (alpha clamped to 32, the global alpha
  folded in), blending at alpha 0..32, the start and abort rules (which refuse a start, which abort
  a held shot, no free-count rule) and the LCD and EVF layouts.
- `shot_em10ii run` (`tests/sim/shot_em10ii/test_shot_run.c`): `shot.c` and the body's lend on a
  fake LCD display against mocks of the image pool and the file server: the lend of PRIM block 0
  (another block given back, an alloc error, an oversized request), the start refusals (a PRIM
  frame in flight, prop 0xE951 set or unregistered, a pool over block 0 busy, movie, playback; the
  empty PPM removed), a whole shot with the raw pieces while the imgwork reserve is in use and a
  still takes block 1 with the shutter button down, an abort during the PPM (block 0 back first,
  the partial PPM removed) and one during the raw pieces (the PPM kept).
