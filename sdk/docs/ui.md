# Settings page library (`sdk/lib/ui.c`)

Status: camera-tested: the stock list view draws slot 0xF in super 0x0D,
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
  while a change is dirty and the card gate is closed or a save failed since the page opened. The
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
| `fenykl_ui_install()` | CPU0 task, after `fenykl_cfg_init` | `EAGAIN` until the GUI is in live view or playback with its language; then collects the list (`EOPTS`), checks 31 stock words and the 21 borrowed ids (`ESTOCK`, each mismatch logged) and installs; all or nothing |
| `fenykl_ui_tick()` | CPU0 task, ~100 ms | re-applies the row's strings after a language change; computes the "not saved" state; gives back lang ids left lent outside the page |
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
| dials 0x9031 / 0x9032 | cursor up / down (as the stock menus) | cursor | cursor | -/+ 10 x `step`, clamped |
| MENU | back to the Custom list | back | back | cancel the edit |

- No pop-ups, no help text, no confirmations, no Defaults row: every value is edited in place.
- The cursor is kept when the page is closed and opened again.
- BOOL shows `On`/`Off`, ENUM the name from `names[]` (the file identifiers), INT the decimal, in
  edit mode with the camera font's arrow icons U+A05F / U+A029 around it. Text longer than 14
  characters is cut. The value is stored only by OK in edit mode, so the INT's `on_change` runs
  once; MENU and any other way out of the page drop the edit.
- Dial direction: 0x9032 moves the cursor down and increases a value (camera-accepted with
  `projects/main`, 2026-10-16).
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
