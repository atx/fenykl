# Pre-capture (`sdk/lib/precap_em10ii.c`)

E-M10 II only: pre-capture holds frames of the stock **silent sequential H** burst, which needs the
fully electronic shutter; the E-PL3 has no electronic shutter and no such burst. Built for targets with
the `precap` capability (`include/precap.h`); a plugin that does not call `fenykl_precap_*` gets no
byte of it (`--gc-sections`).

Status: the mechanism (key control, hold, commit, cancel, fail-safes) is camera-proven in a prototype
plugin on the E-M10 II v1.4 (2026-10-28, silent seq H JPEG, S-AF, 1/1000 s): 7 bursts, 46 files, the
file count matching the log exactly, consecutive numbers, all files distinct, no fail-safe. This
library is that prototype without its arming window, burst length limit and diagnostics, with the full
settings gate and the slot restore below. Not run on the camera yet in this form; neither RAW,
RAW+JPEG, MF nor the gate's individual props were tried.

## 1. What it does

- **Start.** With the option on and the camera inside the gate (section 2), a half press held
  300 ms past AF ready (TAKE state RdyTrig2Wait, 5) makes the library's CPU0 task inject a shutter
  full press with `ui_key_inject_level(0x101, 0)`. The stock silent H burst starts as for a real
  press. MF reaches RdyTrig2Wait without a scan and uses the same trigger.
- **Hold.** The Base sequence's frame-done method (slot `seq_frame_done`) runs with bit 0x200 of the
  sequence flag word set, which skips only the record handoff: the frame stays in the buffer
  manager's list 1. The last 6 frames are held; the oldest is freed with the stock frame discard
  (`seq_discard_frame`) once TAKE has released its take handle (handle +0x12 == 0), and +0x210 is
  cleared as the stock no-record branch does. 6 leaves one of the 12 PRIM/THUM blocks free for the
  next request.
- **Commit.** A real full press during the burst posts no event (the injected press already holds
  the snapshot down), so the task polls the kind-1 producer shadow. Its first real full press sets
  the commit flag, unless the sequence's continuous stop bit (+0x194 bit 0x8000) is already set:
  then the burst is a cancel. The next frame done records the held frames oldest first with the
  stock handoff `seq_record_frame` (each checked: the head left list 1, its state moved on, +0x190
  went down by one), then runs the stock method, which records its own frame. Every later frame of
  the burst, those after the stop included, is stock. The real release stops the burst.
- **Cancel.** A half release without a full press injects the release (the stock stop). The first
  frame done with the stop bit, and every later one, drops all held frames and itself. A burst that
  recorded nothing leaves the shooting flag (u32 0x615E07A4) at 1, which makes the GUI refuse OK:
  the take-end wrapper (slot `seq_take_end`) calls `seq_end_paint_black` after the stock body when
  nothing was recorded, frames were dropped, the flag is still 1, no recview started (+0x194 bit
  0x10000) and flag 0x100 is clear. OK, Fn1 or a monitor tilt stop the burst the same way (the
  producer resends the real full release); the burst is then a cancel.
- **Take fail.** The take-fail wrapper (slot `seq_take_fail`) drops the held frames before the
  stock body discards the failed request.
- **Direct full press.** A burst started with a full press before the injection is stock.

The wrappers run in the HFW sequence task (HIGHLEVEL_MAIN, CPU0): no log and no blocking call
there. Calls on another core are counted and passed to the stock function (static analysis finds
only CPU0 callers).

## 2. Gate

Checked at the half press and every poll until the injection; outside it the camera is stock.
Props are element 0 read as the firmware's getter does (id & 7: 1 u8, 2 u16, 4 u32); an unregistered
prop fails the gate.

| prop / word | must be | why |
|---|---|---|
| 0x8391, 0x85B1, 0xF101 | 1, 2, 1 | silent sequential H (the drive menu's composite) |
| 0x83A1 | 0 | flash off; flash turns H into L |
| 0xF121 | 0 | long-exposure NR off: on or Auto allocate three PRIM blocks per handle (12 blocks, 4 handles) |
| 0xC021 | != 2 | S-AF, S-AF+MF or MF; not C-AF or C-AF+TR |
| 0x85F1, 0x96C1, 0x96E2 | 0, 0, 0 | no AE/HDR, flash or focus bracketing (own take paths) |
| 0xDE11 | 0 | interval off |
| (s16) 0xA02A | not -0x8000, -0x7FFF | not live bulb, time or composite |
| 0xE951 | 0 | the 12-block PRIM pool (else the 8-block one) |
| 0xC222 | != 2 | not handle alloc mode 1 (other pools) |
| u16 0x615E4520 | 0 or 1 | the Base sequence (0: its initial value, written only when a take starts); other kinds (ART/WB/ISO bracketing, multiple exposure, panorama, ...) select another sequence class. It keeps the last take's kind, so after leaving such a mode one stock shot re-enables pre-capture |

Image quality is free: JPEG, RAW and RAW+JPEG use one take handle and one list-1 buffer per frame
and the same record handoff. The frame-done wrapper also checks the take context of each burst:
drive type 3 (H), shock mode 2 (silent), NR prop 0.

## 3. Slots

The three slot words are installed while the option is on and silent sequential H is selected
(0x8391, 0x85B1, 0xF101), and restored when either stops being true or after a fail-safe. Both
happen only once nothing is in flight: no held frame, no injection outstanding (key control idle,
waiting or passing), no wrapper call in progress (a depth counter the wrappers raise on entry and
lower on exit: HIGHLEVEL_MAIN can block inside the stock call and let the task run), TAKE state
u16 0x6184409C == 0, frames in flight u16 0x615E4530 == 0 and the shooting flag == 0. The shooting
flag goes to 0 only in or after the take-end call, the wrappers' last call of a burst. Installing
also needs code section 4 loaded and the Base vtable in the singleton; the fail and end wrappers
go in first. While installed, bursts outside the gate pass straight to stock.

## 4. Fail-safes

Any of these disarms the wrappers (their next call drops the held frames and passes to stock),
releases the injected press once the real one is up, restores the slots once nothing is in flight
and stops injecting. Pre-capture then stays off (`FENYKL_PRECAP_FAILED`, ERR line with the cause)
until `fenykl_precap_enable(false)` and `(true)` or a power cycle.

- wrapper checks: flag word not as set, side list changed, list-1 head changed by the stock call,
  list 1 not a chain of buffers in state 3, a head without its handle or picture, discard that did
  not free, handle not freed, more than 7 held, frames left where none may be, a pass with held
  frames, a failed commit record (the rest is dropped);
- key control: key ring full or not draining for 200 ms while our press is held, an injection the
  firmware refused, no HsContTake1 1 s after the press, an unexpected take state while our press is
  held, frames held before an injection, the snapshot not following the shadow 200 ms after our
  release, the take not ending 2 s after the stop;
- burst: a take state outside the silent H path while frames are held, HsContWait with no free PRIM
  block for 2 s while frames are held;
- after a pre-capture burst: held frames, used take handles, pools not full, +0x190 not 0 or the
  shooting flag not 0, 30 s after a commit or 5 s after a cancel;
- slots: a slot word changed by someone else, slot words not stock at install, a failed install or
  restore.

An injection is refused (WARN, no fail-safe) while the key ring is not empty or the snapshot and
the shadow differ on another kind-1 key: the half press then gives a stock burst.

## 5. API (`include/precap.h`)

```c
enum fenykl_precap_state { FENYKL_PRECAP_OFF, FENYKL_PRECAP_ARMED, FENYKL_PRECAP_RING,
			   FENYKL_PRECAP_SAVE, FENYKL_PRECAP_FAILED };
extern struct fenykl_log_module fenykl_precap_log;
int fenykl_precap_start(void);			/* CPU0: the task, priority 60, 10 ms poll */
void fenykl_precap_enable(bool on);		/* any task */
enum fenykl_precap_state fenykl_precap_state(void);
void fenykl_precap_status(void);
```

`ARMED`: option on, slots in, gate true, no burst; `RING`: a pre-capture burst holds frames;
`SAVE`: it was committed. The task runs below the HFW, REC and library tasks (priority 60; the
library tasks are 55).

## 6. Log lines (module `precap`)

- INFO, one per pre-capture burst: `commit: 28 frames, saved 17, dropped 11, fps x100 640`
  (`cancel:`, `other end:` likewise); slot install and restore.
- WARN: a refused injection, a deferred install, a burst end without a take-end call.
- ERR: `fail-safe: <cause> (<where>, <value>), off until re-enabled`.
- DEBUG: key control transitions, the injection, the commit time, gate changes, stock bursts.
- `fenykl_precap_status`: bursts, commits, cancels, races (full press after the stop), frames saved
  and dropped, half presses, injections, refusals, wrapper holds/drops/defers/passes, take fails,
  calls on other cores, installs, restores, fail-safes.

## 7. Firmware data (`arch/em10ii/firmware.toml`)

`key_shadow1`, `key_state1`, `key_state0`, `key_ring_wr`, `key_ring_rd` (UI_IN), `seq_base`,
`seq_base_vtable`, `seq_flags`, `seq_kind`, `seq_inflight`, `shooting` (HFW sequence), `take_state`,
`take_hist*`, `take_handles`, `take_drive`, `take_shock` (TAKE_MAIN), `pool_prim/thum/exif`; functions
`ui_key_inject_level`, `seq_discard_frame`, `seq_record_frame`, `seq_end_paint_black`; slots
`seq_frame_done`, `seq_take_fail`, `seq_take_end` (target.toml `sdk_slots`). Each entry's note holds
its evidence.

## 8. Risks and open points

- Restore: if a burst ends with frames still held and no further wrapper call, the slots stay in
  (pass-through) until the next burst's first frame done drops them.
- RAW and RAW+JPEG: REC develops the held frames after a commit for 2.5-3.3 s each, so the burst
  after the commit stalls on handles sooner (a stock stall).
- The gate's kind word 0x615E4520 for each excluded feature, and the digital tele-converter and
  keystone (no prop located; neither is read by the frame path) are inferred.
- The HUD draws only on the INFO screens (GUI display 0xF001); during the burst the display request
  is 0xF712, so `main` shows no ring state.
