# Settings tab library (`sdk/lib/ui.c`)

The option core (`ui.c`: the option list and its limits, the values, the cfg calls, the start of the
install, the stock word check, the "not saved" state) is shared; the camera builds its tab in
`lib/ui_<camera>.c`. Sections 1 to 6 describe the library, section 7 the E-M10 II tab
(`ui_em10ii.c`). Used by `projects/main` and `projects/devmon1`. Linked into every plugin, like all
of `sdk/lib/`; a plugin that does not call it gets no byte of it (`--gc-sections`).

## 1. What it does

The Custom menu gets a 12th tab, **L. Fenykl**, next to A..K. It is a native tab: the camera's own
menu code draws its rows, value column, option lists, help pop-ups and scrolling (7 rows per page),
and edits the values. The plugin draws no pixel and uses no camera asset; labels are inline ASCII
or stock strings.

- Row 1, **Fenykl** with the value **Loaded**: an info row (help "The Fenykl plugin is running.").
  RIGHT and OK do nothing on it.
- One row per cfg option with a group: title = the option's `label`, value column = its current
  value, help pop-up = the group's label. Options are listed by `fenykl_cfg_group` (sorted by
  `order`, equal orders in link order), each group's options in link order; options without a
  group are not shown.

The plugin passes nothing: the option table is the cfg library's.

## 2. Limits

Checked at install; any violation refuses the whole install (`EOPTS`, each reason logged):

- 1 to 40 options with a group (`FENYKL_UI_OPTS_MAX`): 41 rows, 6 pages, the most run on the camera
  (stock tabs show 4). All option rows share one row-style entry (section 7).
- Types: BOOL and ENUM only. **INT is refused**: an option list of numbers would need more entries
  than the borrowed picker has.
- An ENUM has at most 26 names (`FENYKL_UI_VALUES_MAX`, the most a stock vertical list holds),
  and all options together at most 128 values (`FENYKL_UI_CELLS_MAX`, BOOL 2).
- Labels, ENUM names and group labels have at most 32 characters (the stock inline text cells).
  The cfg library limits labels to 15.

## 3. API

Header `sdk/include/ui.h` (the reference). Errors are `FENYKL_UI_E*` (negative).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_ui_install()` | CPU0 task, after `fenykl_cfg_init` | `EBUSY` once installed; collects the list (`EOPTS`, logged); `EAGAIN` until the menu code is loaded or while the menu is open; checks the stock words (`ESTOCK`, each mismatch logged) and installs; all or nothing |
| `fenykl_ui_install_opts(begin, end)` | as `fenykl_ui_install` | the same with the caller's option table instead of the cfg library's: a set stores the value and calls `on_change`, nothing is saved (tests, experiments) |
| `fenykl_ui_remove()` | CPU0 task, menu closed | `ENOTINST`, `EAGAIN` (menu open); puts every stock word back, last installed first; `ESTOCK` when a word no longer held ours (left, logged). The tables stay resident: a page actuator built while installed still points at them |
| `fenykl_ui_tick()` | CPU0 task, ~100 ms | rewrites value cells that changed outside the option lists (a late cfg load); computes the "not saved" state (logged when it starts) |
| `fenykl_ui_get_stats(st)` | any | counters (`struct fenykl_ui_stats`) |
| `fenykl_ui_log` | any | the log module `ui`: INFO logs the install, option lists, sets and MENU in a list; DEBUG adds every picker event |

No hook, no slot. Startup: `fenykl_log_init` -> `fenykl_cfg_init` -> `fenykl_cfg_task_start` ->
`fenykl_ui_install` (retried every 100 ms while `EAGAIN`) -> task loop with `fenykl_ui_tick`.
**Install before the first MENU** (section 7): at boot, the plugin starts before the menu code is
loaded, so the retry ends within ~100 ms of it, long before the user can press MENU. Do not wait
for live view first. `projects/main` is the reference user.

## 4. Keys

All stock: the tab behaves as any Custom tab.

- Main menu: Custom page, letter row **L** (below K), RIGHT or OK: the tab. In a tab, DOWN from
  K's last row also continues into L.
- UP / DOWN: rows; the list pages by 7 rows with the stock scroll bar.
- OK or RIGHT on an option row: its option list (BOOL: Off / On, ENUM: the names), the cursor on
  the current value. UP / DOWN, then OK stores it (`fenykl_cfg_set`); MENU leaves the list without
  a change.
- RIGHT and OK on the info row: nothing. LEFT: the tab column; MENU: back.
- BOOL shows the stock `Off` / `On`, ENUM the name from `names[]` (the file identifiers).

## 5. Persistence and context

OK in an option list calls `fenykl_cfg_set` in the GUI task on CPU0: the row shows the stored value,
an error is logged at once (`set ... failed`). **`on_change` handlers therefore run in the GUI
task**: they must be cheap and must not render. The cfg saver task writes 2 s after the last change.
A value changed elsewhere (the saver task's late load when the card mounts after the install) reaches
the value column at the next `fenykl_ui_tick`.

## 6. Counters and log

`struct fenykl_ui_stats`: `opens` (option lists opened), `sets`, `set_fail`, `refused` (a picker
left stock or not filled, an OK on a picker that is not ours), `refreshes` (value cells rewritten
by the tick), `late` (1: installed after the first MENU), `reallocs` (picker arrays replaced for
an ENUM above 5 names), `cpu1_actions` (callbacks on another core, must stay 0).

## 7. E-M10 II (`ui_em10ii.c`)

Firmware v1.4 addresses are in `firmware.toml` (the Custom menu tab L section) and the library. All
are data writes into tables the menu code reads; no code is patched.

- **The tab.** It borrows tab id 0x8F1, the Accessory Port menu's tab C, whose main-menu page this
  body never shows. That id already has an entry in every fixed-count by-id table a tab needs (tab
  record, tab -> config 0x40, icons, row-id list 0x4D). An id without a ModeCheck rule and a
  setting-rule record is enabled, so nothing grays the tab or the rows.
- **Install** (with a check of every stock word first, nothing written on a mismatch), one checked
  word store per pointer or count, pointer before count, so no count ever exceeds the table behind
  its pointer:
  1. re-keys row-style entry 79 (a second 0x28C, which entry 60 always matches first) to {first
     option row id, style 1, the vertical option list};
  2. config 0x40: our L1 table (one record per option row) and our RIGHT / OK tables (the info
     row's record);
  3. row-id list 0x4D: our ids;
  4. the tab record: our rows, title "L. Fenykl";
  5. the tab column's green row mark;
  6. the letter list's Custom page record: 12 titles and helps (L: "Fenykl", "Fenykl plugin
     settings.");
  7. the letter list's RIGHT / OK records: 12, L opening the tab like A..K;
  8. the image-table words of images 2043 and 2040: our icon records (see Icons);
  9. the Custom tab list: 12 ids, last, so that the tab appears only with everything behind it in
     place.

  A store that finds a changed word rolls the install back. The tab stays installed for the boot;
  the tables live in the plugin's static RAM. `fenykl_ui_remove` unwinds it: entry 79 back to
  0x0001028C from whichever row key it holds.
- **Row ids** 0xF40 (info) and 0xF41.. (options): fresh, no table of the firmware holds them, and
  the controller reads 0 for them. So each row has one value cell for 0, whose label the plugin
  rewrites (BOOL the lang id word, ENUM the text pointer word).
- **Option list.** The option row's L1 record has a callback, which the stock code calls instead of
  creating the level-2 picker; level 2 follows whatever the callback does. The style table's only
  reader (0x609F9558) looks a row's style up once, when level 2 builds the option view, so the
  callback first re-keys entry 79 to its own row id (one shared entry for every row). It then
  creates the stock option picker of Warning Level (0x115, 5 entries, plain stock init), checks
  that it looks stock, replaces its two arrays by `fw_malloc` blocks of n entries when the option
  has more than 5 values (the picker's destructor frees them with the same allocator; no memory:
  refused), clears its wrap and dial flags,
  installs our init (+0x2C: our values, nothing selectable grayed, the cursor on the current value)
  and our handler (+0x48: OK calls `fenykl_cfg_set`, every other event goes to the stock handler),
  and hands it to the tab's sub-actuator. No setting of the camera is written. A picker that does
  not look stock is refused (logged, counted): with the stock handler in +0x48 it gets a handler
  that cancels every event, so its OK never sets Warning Level; with another handler it is left as
  it is.
- **Install timing.** The main menu's page actuator copies the letter list's open records once,
  when the first MENU of a boot builds the menu tree, and keeps them for the boot. Installed before
  that, letter row L opens the tab with RIGHT or OK. Installed later (`late` = 1, a warning in the
  log), letter row L is listed but RIGHT and OK on it do nothing until LEFT to the top tab column
  and RIGHT rebuild that actuator. The submenu's tab column also keeps the tab list of its last
  build: L shows there (and DOWN from tab K's last row reaches it) only after LEFT to the letter list
  and RIGHT into a tab.
- **Menu left on tab L, next boot without the plugin.** The camera saves the menu position at a
  soft power-off, and the submenu restore (0x609F3235) reads the saved tab back at the next MENU.
  Without the plugin there is no tab L. Not yet checked on the camera. Statically there are two
  outcomes: the restore finds tab id 0x8F1 by id and opens the donor tab with its stock content
  (header "C. Electronic Viewfinder", rows EVF Adjust and EVF Auto Switch, which lead to EVF
  screens on a body without the Accessory Port menu), or it takes index 11 of the 11-entry tab
  list and its id lookup misses. Until checked, leave the menu on another tab before a boot without
  the plugin.
- **Icons.** The tab has its own icon, a fennel bulb with fronds and an "L" on the Custom tabs'
  plate (38 x 40, the menu's GUI palette). The stock image widgets draw it: the borrowed id's icon
  entry {0xE, 0x1C} selects images 2043 (bright) and 2040 (dim) of the common resource, and the
  install re-points those two image-table words (0xA2BD861C, 0xA2BD8610) from the stock records
  (an Accessory Port glyph with a small "C") to ours, uncached like the stock ones. The tab column
  and the letter list are those images' only users (a scan of every image-list record of the
  menu's widgets; no code operand names them), so both show the icon and nothing else changes.
  The pixels are an index grid (`lib/ui_icon_em10ii.txt`); `python3 -m fenykl.icon` encodes it,
  bright and a dim remap, into the 8-bpp run-length format of the stock images
  (`lib/ui_icon_em10ii.h`, committed; a test checks that it is up to date and that it decodes back
  to the grid). The encoder reproduces every 8-bpp stock image byte for byte. Seen on the camera
  (2026-11-20): bright in the open tab and its letter row, dim otherwise, index 0 transparent.
- **Eye-Fi**: untouched, tab K is stock.
- Status: the mechanism ran on the camera in a probe plugin (2026-11-19, installed before and after
  the first MENU): tab and letter row L, kind-5 titles and helps, two option rows (an On/Off and a
  3-way list) with patched stock pickers, a row with RIGHT/OK records swallowed, no camera setting
  written. This library ran on the camera with the main plugin's options (2026-11-20: sets through
  the picker saved by the cfg library and kept over a reboot) and with a 40-option RAM table
  (2026-11-20, `fenykl_ui_install_opts`): 41 rows on 6 pages with the stock scroll bar, ENUMs with
  5, 12 and 26 names (12 and 26 on reallocated pickers, with option-list pages and wrap), entry 79
  re-keyed at every open, MENU out of a list without a set, the removal (entry 79 back to stock, no
  L in the letter list), Warning Level never written, no warning or error logged.
  Simulator: `tests/test_sim.py`, `test_em10ii_ui_runs_in_the_simulator`.
