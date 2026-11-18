# Key layer (`sdk/lib/key.c`)

Status: simulator-tested with the E-M10 II key map (`tests/sim/key_em10ii/`); the key map's codes are
camera-seen through a hook probe (section 5), the layer itself has not run on the camera. A claim
consumes a key's events only (section 3.1). Used by main's Fn override (short Fn runs the action; a
held Fn is replayed to the firmware by `fenykl_key_tick` at `long_us` while still held, so the stock
function runs late), not yet run on the camera. The library is built for targets with the `key` capability; a plugin that does not call
`fenykl_key_*` gets no byte of it (`--gc-sections`).

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

The key-class events are plain press and release, so the layer times holds itself with the 1 MHz timer (`FENYKL_TIMER_NOW`, 30 bit, wrap-safe).

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
| `fenykl_gui_state()` | any | from `body.h` (key.h includes it): GUI super state << 8 \| state, the camera's own encoding: u8 bytes at 0x619A3740 (`data.gui_super`, the state at +1), `FENYKL_GUI_SUPER()`, `FENYKL_GUI_SS_LV` (0x01, state `FENYKL_GUI_ST_LV` 0x08 while live view is shown), `_MENU` (0x11), `_PLAY` (0x06) from `arch/em10ii/fenykl_body.h`. A plugin asks `fenykl_gui_live_view()` / `fenykl_gui_menu()` / `fenykl_gui_playback()` instead |

A handler node is the plugin's static storage and stays added for the boot; `enabled = false`
stops new claims and deliveries (a key it already holds still completes). The fields under
`private` belong to the SDK: the hold (`state`, `held`, the press time, param, flag and GUI state),
a release handed to the tick, and the counters `shorts`, `longs`, `replays`, `busy`.

```c
/* claim runs in the event hook call that read gui, so the GUI now is the GUI at the press */
static bool feat_claim(enum fenykl_key key, uint16_t gui, void *arg)
{
	return fenykl_gui_live_view();		/* <body.h> */
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

**Hook and tick.** The hook runs on the posting task (UI_IN for most keys, section 4), the tick on the
plugin task, and the key task can preempt the tick. Both change a handler's state under the CPU0 interrupt mask; deliveries and
injections run unmasked. While the tick delivers LONG or injects the replayed press, a release that
arrives is not passed (that would reach the firmware before the replayed press) but consumed and
handed to the tick, which then delivers RELEASE after the LONG, or injects the release after the
press. The firmware therefore always sees press before release.

### 3.1 Claiming vs hold behaviour

A claim consumes the press and release APPEVENTs of a key. Whether the firmware's stock hold
behaviour (menu auto-repeat, playback scroll) reads the event stream or the physical key state, and
so still runs for a claimed key, is not tested on this camera; `long_stock` hands back one late
press, not the hold behaviour. Until it is checked, claim only keys whose stock function on the
claimed screens is a single action with no hold behaviour, and not the arrows in playback or in
menus.

## 4. Handler rules and counters

`claim()` and `on()` run in the hook or, with `from_tick`, in the plugin task. Handlers in the hook get its limits; the event layer's handler rules apply (`docs/event.md` 4): no
blocking firmware calls, no `fenykl_hud_text`/`draw`, no `fenykl_cfg_set`, no `fenykl_key_install`;
small frames; state shared with the plugin task in single word stores (e.g. a `req++`).
`fenykl_log_*`, `fenykl_event_inject` and `fenykl_key_inject` are allowed.

Hook context (static, not measured): key-class events come from UI_IN (priority 44, stack 0x800),
PWR_STANDBY (wake keys, stack 0x800) and PER_EYE_SENSOR (0x02022301 only, stack 0x400). The last
one pays the layer's lookup on a 1 KiB stack; its code is in no map, so nothing is delivered there.

Layer counters (`struct fenykl_key_stats`): `presses`, `releases`, `values` (mode dial), `claimed`
(presses and mode dial values a handler claimed), `unknown` and `last_unknown`, `ticks_long` (LONGs and
replays the tick decided). Per handler, under `private`: `shorts`, `longs`, `replays`, `busy`.
Plain increments, like the event layer's.

## 5. Keys

Each camera has its key map, `fenykl_key_codes[]` in `lib/key_<camera>.c` (built with `key.c` for that
camera only). Adding a key is one line there, plus an enum entry before `FENYKL_KEY_MODE_DIAL`
for a new button (`key_is_button` is `k < MODE_DIAL`); the enum numbers shift, so nothing may
store them. A key the body lacks has code 0: `key_lookup` never returns it, `fenykl_key_add`
refuses a handler listing it and `fenykl_key_inject` refuses it (`FENYKL_KEY_EARG`). "camera-seen" =
seen through the hook on the camera (2026-10-19/20).

| key | code | evidence |
| --- | --- | --- |
| OK, UP, DOWN, LEFT | 0x0004..0x0007 | camera-seen |
| RIGHT | 0x0008 | key state seen on the camera, the map entry is a button (static), no event seen yet |
| INFO, MENU, PLAY, DEL | 0x1205, 0x0089, 0x0087, 0x0084 | camera-seen |
| FN | 0x000B (Fn1) | camera-seen |
| REC | 0x120A | camera-seen: started and stopped recording |
| HALF, FULL | 0x0100, 0x0101 | camera-seen |
| POWER | 0 | the lever is a switch posting the value 0x0202000C (the power-off event): mapped, a claimer could consume it |
| FN2, FN3 | 0x1209, 0x1224 | camera-seen |
| LFN | 0x2202 | the lens's L-Fn: a button in the static key map, its level set by the lens driver; no event seen yet |
| MODE_DIAL | 0x1901 (class 0x0202) | camera-seen: one VALUE per position |

Not in the table, passed as `unknown`: the dials' 0x02031903 (front) and 0x02031902 (rear), one per
detent and without a delta, the eye sensor's 0x02022301 and switch values such as 0x02021215,
0x02022402 and 0x02021519.

Design notes: there is no REPEAT kind, no `repeat_us` or repeat counters and no observer mode; the
SDK fields sit in `private`. A released key replayed by `long_stock` is reported as RELEASE to
every handler listing it, in the tick path and the release path alike. `fenykl_key_add` takes any
CPU0 context, like `fenykl_event_add`.

## 6. Tests

`tests/sim/key_em10ii/test_key.c` links the key layer with the E-M10 II key map over the event layer
and the E-M10 II hook runtime and posts through the patched `appevent_post` entry onto a recording
mock (`tests/sim/key_em10ii/fw.S`), writing the timer word and the GUI state bytes between posts. It
checks: `add` refusing a handler listing POWER; a claimed Fn1 consumed and completing as SHORT in live
view and the menu, passed with RELEASE in playback; Fn2, Fn3, REC, L-Fn and RIGHT passed with
PRESS/RELEASE; 0x000C, the power lever's value 0x0202000C and code 0 passed as unknown;
`fenykl_key_inject` refusing POWER and posting Fn2's codes.
