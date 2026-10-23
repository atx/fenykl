# Settings page library (`sdk/lib/ui.c`)

The option core (`ui.c`: list, keys, value text, cfg calls, and the page lifecycle: open and close
bookkeeping, the start of the install, the stock word check, the "not saved" state) is shared; each
camera patches and draws its page in `lib/ui_<camera>.c`. Sections 1 to 6 describe the E-PL3 page
(`ui_epl3.c`), section 7 the E-M10 II page (`ui_em10ii.c`).

Status (E-PL3): camera-tested: the stock list view draws slot 0xF in super 0x0D,
borrowed titles show and are given back (`stray 0`), and `fenykl_cfg_set` from GUI actions saves
(58 sets, 214 publishes, 0 failures). Simulator tests in `tests/test_sim.py` (program `ui`). Used by
`projects/main`. Linked into every plugin, like all of `sdk/lib/`; a plugin that does not call it
gets no byte of it (`--gc-sections`).

## 1. What it does

The last row of Custom menu tab J (Eye-Fi) becomes **Fenykl**. OK or RIGHT on it opens the settings
page: **one scrolling list of all options**, looking exactly like a stock menu list because the
stock view draws it. The plugin draws no pixel and uses no camera asset. Options are listed by
`fenykl_cfg_group` (sorted by `order`, equal orders in link order), each group's options in link
order; options without a group are not shown. At most 20 options (`FENYKL_UI_OPTS_MAX`, the rows of
the stock list slot); more is refused at install (`EOPTS`). The plugin passes nothing: the option
table is the cfg library's.

```
          Fenykl                          header (stock widget)
  [H]  | Peaking                 Off      7 rows per screen, gold bar, values right-aligned
  [I]  |[Peak colour             red ]    selection frame (stock image)
  [J]  | Level                 ◀55▶       INT in edit mode: arrow icons around the value
       | ...                          ▮   stock scrollbar when there is a second screen
  Back MENU                    Set OK     stock footer
```

How:

- The page stays on the panel of the Custom list. Entry, every key that changes something, and
  nothing else: the library builds the item list `struct fui_slot` (`ui_int.h`, 0x2D8 bytes, the
  layout `custom_menu_build` writes) and publishes it with `fw_gui_data_set(&slot, 0xF, 0x2D8, 1)`.
  The view redraws when it handles the posted event 0x1900 in the GUI loop. `count` = options,
  `page` = cursor / 7, `visible` = min(7, count - 7 page); each row: state 1, the cursor row 2,
  value kind 9 (a UTF-16 string of at most 14 characters), title = a borrowed lang id, help ids as
  the stock J list (0x455, 0x45B on the cursor row).
- **Borrowed lang ids:** the view takes titles by lang id. Row k's title is the title id of
  `custom_menu_items[k]` (Custom tabs A..C, never on screen with the page); entry points those 20
  entries of the language array at UTF-16 copies of the option labels, every way out puts the
  stock pointers back (MENU, a system event, the exit action of super 0x0D; and the task tick if
  the GUI has been outside 0x0D for 3 ticks with ids still lent, logged as an error). Item 20's
  title (tab D) carries the header "Fenykl: not saved". All 21 ids are checked against the
  firmware's item table at install.
- **Header:** lang id 0x122 ("Fenykl", the overridden Eye-Fi title), or "Fenykl: not saved"
  while a change is dirty and the card gate is closed (after a USB cable: until power-off) or a
  save failed since the page opened. The
  header follows at the next key (the slot is published only from GUI actions).
- **Tab column and footer** stay as the stock J list wrote them (tabs H, I, J; "Back MENU",
  "Set OK").
- **View check:** OK/RIGHT on the Fenykl row opens the page only when the panel object on screen
  has the menu list view's class vtable (0x6F150894) and the language table is sane; otherwise it
  logs `NOT opened: panel P vtable V ...`, counts `refused` and stays in the stock list.

## 2. API

Header `sdk/include/ui.h` (the reference). Errors are `FENYKL_UI_E*` (negative).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_ui_install()` | CPU0 task, after `fenykl_cfg_init` | `EBUSY` once installed; collects the list (`EOPTS`, logged); `EAGAIN` until the GUI is in live view or playback with its language; checks 31 stock words and the 21 borrowed ids (`ESTOCK`, each mismatch logged) and installs; all or nothing |
| `fenykl_ui_tick()` | CPU0 task, ~100 ms | re-applies the row's strings after a language change; computes the "not saved" state (logged when it starts); gives back lang ids left lent outside the page |
| `fenykl_ui_get_stats(st)` | any | counters (`struct fenykl_ui_stats`) |
| `fenykl_ui_log` | any | the log module `ui`: INFO logs install, open, entry, exit, sets, system events; DEBUG adds every key and ignored event |

No hook. Startup: `fenykl_log_init` -> `fenykl_cfg_init` -> `fenykl_cfg_task_start` ->
`fenykl_ui_install` every 100 ms while `EAGAIN` -> task loop with `fenykl_ui_tick`. A plugin only
declares its options and calls install and tick. `projects/main` is the
reference user.

## 3. Keys

| key | BOOL row | ENUM row | INT row | INT in edit mode |
| --- | --- | --- | --- | --- |
| UP / DOWN | cursor (wraps last <-> first) | | | ignored |
| OK | toggle | next name (wraps) | enter edit mode | leave, `cfg_set` |
| RIGHT | toggle | next name (wraps) | enter edit mode | + `step`, clamped to min..max |
| LEFT | toggle | previous name (wraps) | - | - `step`, clamped |
| dials 0x9031 / 0x9032, n steps | cursor by n (wraps) | cursor | cursor | + n x 10 x `step`, clamped |
| MENU | back to the Custom list | back | back | cancel the edit |

- No pop-ups, no help text, no confirmations, no Defaults row: every value is edited in place.
- The cursor is kept when the page is closed and opened again.
- BOOL shows `On`/`Off`, ENUM the name from `names[]` (the file identifiers), INT the decimal, in
  edit mode with the camera font's arrow icons U+A05F / U+A029 around it. Text longer than 14
  characters is cut. The value is stored only by OK in edit mode, so the INT's `on_change` runs
  once; MENU and any other way out of the page drop the edit.
- Dial direction: the rear wheel posts 0x9032 (EVT_ST_CHANGE_CTRL_RING) for both directions;
  0x9031 (EVT_ST_CHANGE_JOG_DIAL) was never seen on the camera. The direction is the sign of the
  event's step count, the low 16 bits of the action's `p2` (the stock menu actions read it with
  `exth`, e.g. StListMenu at 0x6F081D5D). The page negates it, so that the wheel turns the same way
  as in the stock menus (checked on the camera): step n > 0 moves down and increases. The user
  setting 0x1BC = 1, which reverses the stock menus, is not read. The page logs each step as
  `dial 9032 p2 <raw> step <n> -> cursor <c>` (or `value`).
- The cfg library has no string type (`config.md` 1, "no strings or lists"), so there is nothing to
  edit or show.

## 4. Persistence and context

Every change calls `fenykl_cfg_set` from the GUI action, in the GUI task on CPU0: the row
shows the stored value and an error is logged at once (`set ... failed`). The settings page is the only writer while it is open; the cfg library has no locks.
**`on_change` handlers therefore run in the GUI task**: they must be cheap and must not render. The
cfg saver task writes 2 s after the last change; `fenykl_ui_tick` computes the "not saved" header
state from `fenykl_cfg_dirty()`, the card gate and the save counters.

## 5. Counters and log

`struct fenykl_ui_stats`: `wrap_in`/`wrap_pass`/`refused` (OK/RIGHT on the Fenykl row / elsewhere
in the menu / on the row but the view check failed), `entries`, `exits`, `keys`, `backs`, `sys`,
`ignored`, `sets`, `set_fail`, `publishes`/`publish_fail` (`gui_data_set` calls / returned 0),
`lends`, `returns`, `lang_stray` (ids given back by the tick), `lang_reapply`, `lang_unstable`,
`flag_missing` (the menu flags object was absent on a way back) and `cpu1_actions`, which must stay
0. With the page closed, `lends == returns`.

## 6. Limits and open points

- Firmware v1.6 addresses are in the library; a firmware change needs them checked.
- At most 20 options, labels up to 15 characters (the cfg limit), values up to 14.
- Not proven on the camera: the arrow icons around an INT value in edit mode (main has no INT
  option).
- If the camera resets while the page is open nothing persists: the language array is a RAM copy
  loaded at boot. If the GUI left the page without any exit path the library sees, tabs A..C show
  option labels until the tick gives the ids back (~300 ms, logged).

## 7. E-M10 II (`ui_em10ii.c`)

Status: simulator (`tests/test_sim.py`, `test_em10ii_ui_runs_in_the_simulator`). The mechanism ran
on the camera in a probe plugin (2026-10-21): the kind-5 row title in the stock font, OK
pushing a screen at depth 3, UP/DOWN/OK presses and releases reaching it, `gui_data_set` of slot
0x38 returning 1, half press ending the menu (0x9000 -> default 0x9625, 0xAF00, screen deleted),
all on CPU0. This library works on the camera in `projects/main` (2026-10-21 and 2026-10-23): the
"Fenykl" row, page open, RIGHT stepping the value, MENU popping the page, half press closing the
menu, reopen after a half press, `cfg` save to `C:\FENYKL.CF0` and reload after a power cycle; all
on CPU0. Not run on the camera: dials, LEFT, several rows.

- Entry: the Eye-Fi row of Custom menu tab K (Utility). The slots of `target.toml` `sdk_slots`:
  condition 113 of setting 0x193's rule answers "allowed" (the row is enabled), the OK record's
  row callback opens the page, the RIGHT record's swallows the key. Both return 1 ("handled"), so
  the stock On/Off pop-up, whose OK writes setting 0x193, never opens. The OK and RIGHT slots are
  replaced before the condition; any failure restores all three (`ESTOCK`, logged). The row's
  title descriptor (`data.custom_row_eyefi` +8) becomes {kind 5, "Fenykl"}. Everything stays
  installed for the boot. Install needs no GUI state: no `EAGAIN`.
- Checked before anything is written: the menu app vtable's event and pop methods, the row's
  setting id 0x193 and its stock title words; the slots' stock values (`fenykl_slot_replace`).
- Open: the OK callback pushes a static screen {event, delete} as entry depth + 1 of the main menu
  app (`data.menu_app`), kind and group 0xF0, if the app's vtable, wanted == current depth (< 7)
  and the top screen's class (`menu_screen_vt_list0` / `_list5`) are as expected; else logged and
  refused. The list republishes its own slot 0x38 right after the callback returns, so the page
  copies that slot and publishes its own on the screen's first event (normally the OK release).
- Drawing: a copy of the list's slot 0x38 with 7 rows per page: title cells (+0x3E) and value
  cells (+0x244, the stock value column; the probe showed a title text there in the value column)
  as kind 5 text, cursor u16 +6 + 2k = 2 on the cursor row, row marks +0x22 + 2k = 0xB on filled
  rows, the 0x94-byte cell sets at +0x44A and +0x856 cleared, scroll bar +0/+2/+4 = pages > 1,
  pages, page. Header, tab column and help line stay the stock list's.
- Keys: the screen gets 0x9004..0x900E and the dials 0x9031/0x9032 (param as on the E-PL3, logged
  at DEBUG); releases are ignored; 0x971D republishes; MENU (outside an edit) calls the app's pop
  method with reason 2, and the app deletes the screen and re-shows the list. Every other event goes
  to `menu_app_default` (half press, mode dial: menu end) and is logged with its result.
- Close: every way the app drops the screen calls its delete: a pending edit is dropped, the page
  is closed (`close (n)` in the log).
- Not shown: the "not saved" state (only logged by `fenykl_ui_tick`); the arrow icons around an
  INT in edit mode (U+A050 / U+A03F) are not proven in kind-5 text.
- Counters: `entries` (opened), `wrap_pass` (RIGHT swallowed), `refused`, `exits`,
  `keys`, `backs` (MENU pops), `sys` (events to the default handler), `ignored` (releases),
  `sets`, `set_fail`, `publishes`, `publish_fail`, `cpu1_actions` (callbacks on another core:
  counted, nothing touched; the delete still closes the page and keeps the static screen from
  being freed).
