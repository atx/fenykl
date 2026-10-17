# Key layer (`sdk/lib/key.c`)

Status: simulator-tested (`tests/sim/key/`); two camera runs with a key probe (2026-10-17): every
key code but POWER seen through the hook, all on `ui_key_task`, layer counters clean (section 4).
The rear dial posts no APPEVENT and is not part of the layer (section 5). A claim consumes a key's
events but does **not** hide a hold from the firmware (section 3.1). Used by main's Fn override
(short Fn runs the action, long Fn replays the stock function on release). Linked into every
plugin, like all of `sdk/lib/`; a plugin that does not call `fenykl_key_*` gets no byte of it
(`--gc-sections`).

## 1. What it does

The key layer is one handler of the event layer (`docs/event.md`) at prio
`FENYKL_KEY_EVENT_PRIO` (128). It looks at the key-class APPEVENTs (`0x0200cccc` press,
`0x0201cccc` release, `0x0202` mode dial value), maps the
code to a named key and offers it to the plugin's **key handlers**:

- A handler **claims** a press through its `claim()` (a cheap test, typically the GUI state). The
  press is consumed and held back; its release completes it as **SHORT** (held less than `long_us`)
  or **LONG**, or, with `long_stock`, **replays** the press to the firmware before the release
  passes, so the stock function of a long press still runs. **Only the events are claimed: the firmware's hold behaviour follows
  the physical key and runs anyway, and `long_stock` hands back one late press (section 3.1).**
- An unclaimed press passes to the firmware and every handler listing the key hears **PRESS**, and
  later **RELEASE**.
- With `fenykl_key_tick()` from the plugin task, LONG (or the `long_stock` replay) fires at
  `long_us` while the key is still down instead of at the release.

The firmware's own long press and auto-repeat (HFW `KeyCtrl`) exist only for keys the active screen
registered; the key task posts plain press/release, so the layer times holds itself
with the 1 MHz timer (`FENYKL_LOG_TIMER_NOW`, 30 bit, wrap-safe).

## 2. API

Header `sdk/include/key.h` (the reference). Errors are `FENYKL_KEY_E*` (negative), each also logged
by module `key` (`fenykl_key_log`, default INFO; claims, shorts, longs, replays and busy presses are
DEBUG lines: set `fenykl_key_log.level = FENYKL_LOG_DEBUG` to see them).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_key_add(h)` | any CPU0 context, before or after install | appends `h` (registration order decides between claims); `EARG` without `on`, with no or unknown key bits or `long_us >= FENYKL_KEY_LONG_MAX`; `EBUSY` if already added |
| `fenykl_key_install()` | CPU0 task, not a handler | adds the layer's event handler (once) and calls `fenykl_event_install` (idempotent); `0` or `FENYKL_EVENT_E*` |
| `fenykl_key_tick()` | plugin task, optional, every ~50 ms | LONG / the replay at `long_us` while held (section 3) |
| `fenykl_key_get_stats(st)` | any | layer counters (section 4) |
| `fenykl_key_inject(k)` | CPU0 task or handler | press + release of a button through the stock entry (`fenykl_event_inject`): the firmware acts, `ui_key_get_state` keeps reporting the key up; `EARG` for NONE and the mode dial |
| `fenykl_key_pressed(k)` | any CPU0 context | `fw_ui_key_get_state(code) == 1`; false for NONE and the mode dial |
| `fenykl_key_mode_dial()` | any CPU0 context | `fw_ui_key_get_state(0x02021901)`: the hardware mode dial value (1 P, 2 A, 3 S, 4 M, 0x0A movie, 0x31 iAUTO, 0x33 ART, 0x2A SCN) |
| `fenykl_gui_state()` | any | GUI super state << 8 \| state (u8 bytes at 0x60702278); `FENYKL_GUI_SUPER()`, `FENYKL_GUI_SS_REC` (0x02, every live-view screen), `_MENU` (0x03), `_PLAY` (0x06) |

A handler node is the plugin's static storage and stays added for the boot; `enabled = false`
stops new claims and deliveries (a key it already holds still completes). The fields under
`private` belong to the SDK: the hold (`state`, `held`, the press time, param, flag and GUI state),
a release handed to the tick, and the counters `shorts`, `longs`, `replays`, `busy`.

```c
static bool feat_claim(enum fenykl_key key, uint16_t gui, void *arg)
{
	return FENYKL_GUI_SUPER(gui) == FENYKL_GUI_SS_REC;
}

static void feat_on_key(const struct fenykl_key_event *ev, void *arg)
{
	if (ev->kind == FENYKL_KEY_SHORT)
		feat.req++;		/* the plugin task acts on it */
}

static struct fenykl_key_handler feat_key = {
	.name = "feat", .keys = FENYKL_KEY_BIT(FENYKL_KEY_FN), .claim = feat_claim,
	.on = feat_on_key, .long_stock = true, .enabled = true,
};

/* plugin_main: */
	r = fenykl_key_add(&feat_key);
	if (!r)
		r = fenykl_key_install();
```

## 3. Semantics

`struct fenykl_key_event` carries `key`, `kind`, `held_us` (RELEASE/SHORT/LONG: time since the
press), `now_us`, `gui` (at the press for a completed hold, else at the event) and `from_tick`.
Each handler holds at most one key.

| incoming | state of the handler | result |
| --- | --- | --- |
| press of a key in `keys`, `claim()` true | nothing held | **CONSUME**; held with its time, param, flag and GUI state; no delivery yet |
| press, no handler claims it | | PASS; PRESS to every enabled handler listing the key |
| press of another key | holding a key | not asked to claim (`busy++`, DEBUG line); a later handler may claim, else PASS + PRESS |
| press of the held key again | holding it | as above with a WARN line: its release was lost (a handler below 128 consumed it) |
| release, `held_us < long_us` | holding it | CONSUME; SHORT |
| release, `held_us >= long_us`, `long_stock` | holding it | `fenykl_event_inject(press, saved param, saved flag)`, then the release PASSes and RELEASE goes to every enabled handler listing the key (`replays++`) |
| release, `held_us >= long_us`, `!long_stock` | holding it | CONSUME; LONG, then RELEASE |
| tick, `held_us >= long_us`, `long_stock` | holding it | the press is injected now (`replays++`); the hold ends, the real release passes later as an ordinary release |
| tick, `held_us >= long_us`, `!long_stock` | holding it | LONG (`from_tick`); the release is then consumed with RELEASE only |
| release of a key nobody holds | | PASS; RELEASE (`held_us` since the last press of that key the layer saw) |
| mode dial value | | the first claimer gets VALUE and the event is CONSUMEd; else PASS and every handler listing MODE_DIAL gets it. Whether a consumed value hides the mode change from the firmware is untested (it reads the dial through `api_modedial_get`) |
| key-class event whose code is not in the table, or class above 0x0202 (e.g. 0x02031902) | | PASS; `unknown++`, `last_unknown` keeps the event word |

Several handlers may list the same key: the first (registration order) whose `claim()` returns true
holds it, and no other handler hears that press or its release (the event is consumed). The holder
completes even when it was disabled meanwhile, so a key is never left held back.

**Hook and tick.** The hook runs on `ui_key_task`, the tick on the plugin task, and the key task can
preempt the tick. Both change a handler's state under the CPU0 interrupt mask; deliveries and
injections run unmasked. While the tick delivers LONG or injects the replayed press, a release that
arrives is not passed (that would reach the firmware before the replayed press) but consumed and
handed to the tick, which then delivers RELEASE after the LONG, or injects the release after the
press. The firmware therefore always sees press before release.

### 3.1 Claiming vs hold behaviour (camera, 2026-10-17)

A claim consumes the press and release APPEVENTs of a key. It does **not** hide the key from the
firmware: the stock hold behaviour (menu auto-repeat, playback scroll) reads the physical key state
(or the key driver below `appevent_post`), not the event stream. Two camera cases:

- **Playback, RIGHT claimed, held 3.84 s** (recheck, release path): the press was consumed at
  50.04 s, yet from about 480 ms after it the GUI scrolled pictures for the whole hold (GUI state
  0x0608 / 0x0609 <-> 0x061F, every 100-250 ms), like the stock LEFT hold. The press replayed at the
  release, then the release, changed nothing (GUI 0x0608 for the 1 s trace). On screen: pictures move
  a lot, very fast, while held; none move after the release; LEFT and RIGHT about the same. The first
  run's "several pictures" was the same scroll.
- **Menu, DOWN claimed, held 5.3 s** (first run, tick path): the press replayed at 835 ms while
  still held moved the cursor one step, no repeat (UP held as the stock baseline repeated): the
  late press event did not start the repeat although the key was down. How the menu decides is not
  traced.

A stock hold posts only press and release (UP held 4.3 s: two events), so neither behaviour is made
from the event stream [inferred]. Consequences:

- A plugin **cannot fully take over a key** that has stock hold behaviour on the claimed screen: in
  playback the scroll runs whatever the layer does with the events.
- `long_stock` hands back **one press**, not the hold behaviour (none in the menu, the scroll already
  ran in playback).

Safe to claim: keys whose stock function on the claimed screens is a single action with no hold
behaviour. Fn in live view is camera-proven (`long_stock`). Do not claim the arrows in
playback or in menus; other keys and screens are untested: check the stock hold first.

## 4. Handler rules and counters

`claim()` and `on()` run in the hook or, with `from_tick`, in the plugin task. Hook context on the
camera: every key event was posted by `ui_key_task` (id 0x4E, priority 7, entry 0x6E70EEC7,
TCB 0xA07DF528; stack 0x6049E550, size 0x800 read from the TCB), SP 0x6049EBFC at the probe's handler
(340 bytes below the stack top) for all of them, PSW 0x0F00 (task, not interrupt context). Free stack
at least 1452 of 2048 bytes with the event chain, the key layer and the probe's handlers: the probe
filled up to SP - 256 and never saw a word below that touched, so 596 bytes used is an upper bound.
Handlers there get the hook's limits; the event layer's handler rules apply (`docs/event.md` 4): no
blocking firmware calls, no `fenykl_hud_text`/`draw`, no `fenykl_cfg_set`, no `fenykl_key_install`;
small frames; state shared with the plugin task in single word stores (e.g. a `req++`).
`fenykl_log_*`, `fenykl_event_inject` and `fenykl_key_inject` are allowed.

Layer counters (`struct fenykl_key_stats`): `presses`, `releases`, `values` (mode dial), `claimed`
(presses and mode dial values a handler claimed), `unknown` and `last_unknown`, `ticks_long` (LONGs and
replays the tick decided). Per handler, under `private`: `shorts`, `longs`, `replays`, `busy`.
Plain increments, like the event layer's. Camera run (895 events, 80 key presses and
releases, 6 mode dial values): event layer `nested`, `bypassed`, `cpu1` 0; `injected` 12 = 5
shorts x 2 + 2 replays. Recheck (209 events in 70 s): the same, `injected` 1 (the replay), stack
margin unchanged.

## 5. Keys

Adding a key is one line in `key_codes[]`.

| key | code | evidence |
| --- | --- | --- |
| OK, UP, DOWN, LEFT, RIGHT | 0x0004..0x0008 | camera-seen |
| INFO, MENU, PLAY, DEL | 0x1205, 0x0089, 0x0087, 0x0084 | camera-seen |
| FN | 0x000B | camera-seen (2026-10-14) |
| REC | 0x1209 | camera-seen: two R presses started and stopped recording (GUI 0x020F -> 0x0212) |
| HALF, FULL | 0x0100, 0x0101 | camera-seen; a shot posts HALF press, FULL press, FULL release, HALF release |
| POWER | 0x000C | code verified; not exercised |
| MODE_DIAL | 0x1901 (class 0x0202) | camera-seen: one VALUE per step, `fenykl_key_mode_dial()` agreed every time |

Not in the table, passed as `unknown`: 0x0202121C (VALUE, twice while the lens release button was
held; LpSw [inferred]) and 0x02031902, the rear dial event of the static analysis.

**No rear dial.** In the recheck the rear dial was turned (the shutter speed / exposure values
on screen changed), but the hook saw no APPEVENT at all in that window (only the periodic
0x04030020), and the step counters at 0x60551610/12, polled every 50 ms, stayed 0. The dial input
takes another path (unknown), so the layer offers no rear dial key.

Design notes: there is no REPEAT kind, no `repeat_us` or repeat counters and no observer mode; the
SDK fields sit in `private`. A released key replayed by `long_stock` is reported as RELEASE to
every handler listing it, in the tick path and the release path alike. `fenykl_key_add` takes any
CPU0 context, like `fenykl_event_add`.

## 6. Tests

`tests/sim/key/test_key.c` links the real key and event layers with a recording `appevent_post`
mock at its real address with its real entry bytes (`tests/sim/key/appevent.S`: every post the
firmware gets, in order, with param and flag) and posts through the patched entry, writing the timer
word and the GUI state bytes between posts. It checks every row of the table in section 3: claim
and SHORT with the held time and the GUI state of the press; unclaimed PRESS/RELEASE to every
listing handler; the `long_stock` replay with the press's own param and flag before the release;
LONG + RELEASE; SHORT and replay across the 30-bit timer wrap; first claimer wins and the others hear
nothing; busy handlers; the tick before and at `long_us` for both kinds, and a release posted while
the tick delivers LONG arriving after it; claimed and passed mode dial values; unknown codes and
classes (0x02031902 among them);
a disabled holder completing; `add` errors and install idempotence; `fenykl_key_inject`; the
counters.
