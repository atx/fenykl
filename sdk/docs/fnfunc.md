# Button functions (`sdk/lib/fnfunc_em10ii.c`)

Status: simulator-tested (`tests/sim/fnfunc_em10ii/`). The table install and the press listener
were run on the camera for Fn3 by a probe with the same tables and handler (2026-11-19: the entry
was selectable after Off, the row showed its name, a press reached the handler and did nothing in
the firmware); Fn1, Fn2, REC and L-Fn have not run on the camera yet. Used by main's
"Screenshot". Built for bodies with the `fnfunc` capability; a plugin that does not call
`fenykl_fnfunc_*` gets no byte of it (`--gc-sections`).

## 1. What it does

A plugin function becomes an entry of the camera's own Button Function pickers (MENU > Custom
Menu B > Button Function > Fn1 / Fn2 / Fn3 / REC / L-Fn). The user assigns it like any stock
function; a press of a button that holds it calls the plugin back. The arrow buttons are not
covered.

The entry is function ID **0x3BF**. Every mode rule of the five buttons already lists 0x3BF
wherever it lists Preview (in every remap table, as a `to` in both always-matching conditions), so
the picker offers it in exactly the states where it offers Preview, and by the rule data the
stock re-apply after a mode change should force it where it forces Preview (seen on the camera
only for a Picture Mode ART filter and back, which forces neither). Nothing else on the body knows the ID: it is in no store
value list, no label table and not in the ID-to-function table of the button dispatch, which
therefore resolves a press of a button holding it to "none", like Off. No rule and no code is
patched.

## 2. API

Header `sdk/include/fnfunc.h` (the reference). Errors are `FENYKL_FNFUNC_E*` (negative), each also
logged by module `fnfunc` (`fenykl_fnfunc_log`).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_fnfunc_install(f)` | plugin task | checks every stock word, then swaps the tables (section 3); `EAGAIN` until sections 2..4 are loaded and outside live view, `ESTOCK` on a stock word that differs (nothing left written; retry it for a few seconds after the boot, which builds the record hash), `EARG`, `EBUSY` |
| `fenykl_fnfunc_tick()` | plugin task, every ~100 ms | after the install: installs the event layer once a button holds the entry (one try; its error logged and returned) |

```c
static void feat_press(enum fenykl_fnfunc_button b, void *arg)
{
	feat.req++;			/* the plugin task acts on it */
}

static const struct fenykl_fnfunc feat_func = {
	.name = "Feature", .help = "(Fenykl) Does the feature", .press = feat_press,
};

/* plugin task, every tick until it returns something else than EAGAIN (main also retries
   ESTOCK once a second, five times): */
	r = fenykl_fnfunc_install(&feat_func);
/* then: */
	fenykl_fnfunc_tick();
```

`name` is shown in the picker and in the button row's value cell, `help` on the picker's help line.
By convention the help text starts with "(Fenykl) ", so the picker marks the entry as a mod feature
while the name stays short.
Both are kind-5 labels, ASCII drawn verbatim in the stock font, at most 32 characters
(`FENYKL_FNFUNC_TEXT_MAX`; the firmware widens them into a 33-character cell without a bound). The
picker centres the name in a box about 320 px wide, the row draws it right-aligned at its right
edge; a 19-character name fits both (estimated from a 6-character grab at about 13 px per
character). No icon: the picker's icon cell stays empty for the entry.

There is one ghost ID, so one function per boot.

## 3. Install

Data only, all of it sec2 and sec4 tables in DRAM:

- **Store lists.** The records of settings 0x2F (Fn1), 0x31 (Fn2), 0x23B (Fn3), 0x30 (REC) and
  0x38 (L-Fn) get lists with 0x3BF appended: one shared 29-entry copy for the four identical
  28-entry lists, a 30-entry copy for L-Fn's. The picker lists `bytes / width` values from the
  record, so the entry comes after Off, the list's last stock value.
- **Labels.** The function selector's 15 key entries share one name and one help table; the button
  rows share one value-cell table. Each gets a copy with `{0x3BF, kind 5, text}` appended. The
  arrow rows keep the stock table.

Checks before anything is written: the settings map entries of the five settings (and 0x217), each
record's words and the record the firmware's hash lookup finds, the picker init entry of each
setting (the button picker's init, which lists the record's values), no count override for a button
setting, the lists (identical where shared, no 0x3BF yet), the 15 key entries, the five rows, the
first and last entry of each label table and no label entry for 0x3BF yet.

The swaps are 80 words: label pointers, label counts, list pointers, record sizes. Each is a
compare-and-store with the D-cache written back around it under masked interrupts; pointers go
before their counts and labels before lists, so a reader between any two swaps sees a strict
extension of the stock tables, and the install yields every 16 swaps. A word that differs halts
the install and restores what was swapped. The copies sit in `FENYKL_SHARED` lines and the
firmware gets their uncached alias. Everything stays installed for the boot.

## 4. Press

`fenykl_fnfunc_tick` installs an event-layer handler (`docs/event.md`) at prio 0 once one of the
five settings holds 0x3BF, so a plugin whose user never assigns the entry patches no code. The
handler passes every event. On the press (`0x0200cccc`) of a button whose setting holds 0x3BF it
calls `press` in the posting task (UI_IN for the buttons): the event layer's handler rules apply.
The firmware then gets the press too and does what it does for Off.

| button | key code | setting | evidence of the code |
| --- | --- | --- | --- |
| Fn1 | 0x000B | 0x2F | seen through the event hook on the camera |
| Fn2 | 0x1209 | 0x31 | camera |
| Fn3 | 0x1224 | 0x23B | camera; the press with 0x3BF reached the probe's handler |
| REC | 0x120A | 0x30 | camera |
| L-Fn | 0x2202 | 0x38 | static only (a button in the key map), not seen on the camera |

The settings are read like the firmware's getter does: the settings map gives property and
element (all five are u16 elements, Fn1/REC/Fn2/Fn3 of property 0xECC2, L-Fn of 0xECE2). Presses
of REC while setting 0x217 is 3, 5 or 6, and of Fn2 while it is 5 or 6, are skipped: there the
dispatch replaces their stored function with one of its own when an object check of its own
passes. When that check fails the firmware dispatches the stored entry as Off, and the press is
lost to the plugin. Whether the value read in movie mode is the stored or a forced one
is not known; the plugin's own gate (main: live view or the menu) applies on top.

## 5. Without the plugin

The assignment is an ordinary stored setting and outlives the plugin (it survives a power-off:
seen on the camera 2026-11-20 for Fn3, with and without the plugin on the next boot; a Myset saved
with it probably keeps it too, and unload cannot clean those copies). On a boot without fenykl, or with a plugin that does not
install the entry, a button holding 0x3BF does nothing (the dispatch treats it as Off), its row's
value cell is empty, and its picker opens on the list's first entry because the stored value is in
no list; choosing a stock function there clears it. Seen on the camera with the tables removed and
Fn3 still holding 0x3BF: empty cell, picker on its first entry, the press did nothing, no crash.

## 6. Tests

`tests/sim/fnfunc_em10ii/test_fnfunc.c` runs the library on the stock image's bytes of every table
it checks or copies (copied to their run addresses by `tests/test_sim.py`), with the event layer
and the E-M10 II hook runtime posting through the patched `appevent_post` entry onto a recording
mock. It checks the refusals of the stock checks, the install with consistent tables at every swap
and yield, the lists and labels the firmware's lookups find for all five buttons and 15 key
entries, the rollback of a word changed during the install, the lazy event install, and the
presses: a held button calls back and passes, a button holding a stock function, a release and
REC under setting 0x217 = 3 or 5 and Fn2 under 5 do not call back (Fn2 under 3 does).
