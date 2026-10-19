# Plugin configuration (`sdk/lib/cfg.c`)

Status: simulator-tested (`tests/test_cfg_sim.py`); used on the camera by `projects/main`. Linked
into every plugin, like all of `sdk/lib/` (cfg logs through the logging library, `logging.md`,
module `cfg`).

## 1. Goals and non-goals

Goals: each feature module declares its options next to its code; values survive reboots and
plugin updates; a torn SD write never loses more than the last change batch; reads from hooks cost
one load; the declarations carry everything a settings UI (Fn panel, settings page) needs; the file on
the card is readable on a PC.

Non-goals: no UI (only its data), no strings or lists, no history or wear levelling, no crash
recovery. Options are scalars: bool, int, enum.

## 2. API

Header `sdk/include/cfg.h` (the reference), source `sdk/lib/cfg.c`. `link.ld` has the output section
`.fenykl_cfg` (`_fenykl_cfg_start`/`_fenykl_cfg_end`) after `.rodata`; it is empty and changes no
other plugin's image (build hashes compared). Descriptors carry `no_reorder`, so link order is
declaration order within a file and file order across files.

```c
FENYKL_CFG_GROUP(sym, label, order);                              /* a UI page */
FENYKL_CFG_BOOL(sym, name, label, group, def, ...);
FENYKL_CFG_INT(sym, name, label, group, def, min, max, step, ...);
FENYKL_CFG_ENUM(sym, name, label, group, def, names, ...);        /* names: const char *const[] */
```

`sym` becomes `int32_t sym` (the live value) and `const struct fenykl_cfg_opt fenykl_cfg_<sym>`
(the descriptor). Trailing arguments are designated initializers, e.g. `.on_change = fn`;
`on_change(o, old)` gets the previous value. The descriptor has a reserved `flags` byte (UI
levels later, 0 now).

| Call | Context | Result |
| --- | --- | --- |
| `fenykl_cfg_init()` | CPU0 task, once, before hooks that read options | 0, `ENOFILE` (defaults), `ENOCARD` (gate closed: loaded later), `EDECL` |
| `fenykl_cfg_task_start()` | CPU0 task | saver task `FYKCFG` (priority 12, 4 KiB `fw_malloc` stack) |
| `fenykl_cfg_set(o, v)` / `fenykl_cfg_reset(o)` | CPU0 task only | 0, `EINVAL` (range), `ECONTEXT` (CPU1) |
| `fenykl_cfg_save()` | CPU0 task | write now if dirty: 0, `ENOCARD`, `EIO`, `ENOSPC`, `EBUSY` |
| `fenykl_cfg_str(o, buf, n)`, `fenykl_cfg_dirty()`, `fenykl_cfg_for_each(o)`, `fenykl_cfg_get_stats()` | any CPU0 context | |

Reading: code reads the `int32_t` directly, any context, either core. CPU1 sees a change late
(own cache); a module that needs it there promptly copies it into `FENYKL_SHARED` state in
`on_change`. `fenykl_cfg_set` is refused on CPU1 (`FENYKL_CORE_ID()`, logged, `ECONTEXT`) and must
not be called from a hook (not detectable; hooks run in foreign tasks): a hook posts the request to
a plugin task. `on_change` runs in the caller of `set`, at init for every loaded value that differs
from the default, and in the saver task when the load was delayed by the gate. Cross-option rules
(near <= far) are the module's job: `on_change` may call `fenykl_cfg_set`.

All mutable state is one `struct fcfg_state`: dirty counters, slot and seq of the current file, a
`touched` byte per option, the carried lines (`FENYKL_CFG_RAW`, 1 KiB) and the text buffer
(`FENYKL_CFG_BUF`, 4 KiB). At most `FENYKL_CFG_OPTS_MAX` (128) options; names <= 23 printable
ASCII characters without `=`, labels <= 15; duplicates, ranges, defaults and enum names are checked
at init (`EDECL`, logged; nothing is loaded or saved then).

Usage: `sdk/projects/main`.

## 3. Storage

Two alternating files, `C:\FENYKL.CF0` and `C:\FENYKL.CF1`:

```
fenykl-cfg 1 seq=17
main.dof.show=on
main.fn.action=screenshot
main.log.level=info
crc=8A7B3C1D
```

- Line 1: format version and sequence number. Then `name=value` lines: bool `on`/`off`, enum by
  name, int in decimal. Last line: CRC-32 (zlib) of every byte before it. `\n` line ends.
- **Load:** both files are read (one `"r"` open each into the 4 KiB buffer; a file of 4 KiB or more
  is invalid). Valid = CRC line at the very end matches and the header parses. The valid file with
  the higher seq wins (wrap-safe compare). No valid file: defaults, `ENOFILE`.
- **Apply** (one parser for the slots and the import): a line with a known name and a good value
  sets it. A known name with a bad or out-of-range value uses the default (logged W) and the
  **line is carried** verbatim. An **unknown name is carried** verbatim (logged I): switching
  between builds with different module sets loses nothing. A carried line is written back on every
  save until its option is set with `fenykl_cfg_set`/`reset`. A name repeated in one file: the last
  line wins (logged W). Lines without `=` or with an empty name are dropped (logged W). Carried
  lines that do not fit `FENYKL_CFG_RAW`: logged E, saving is refused for that boot (nothing is
  dropped silently).
- **Save** (saver task `FENYKL_CFG_SAVE_MS` = 2 s after the last change, or `fenykl_cfg_save`):
  format all options (except those with a carried line) plus the carried lines with `seq + 1` into
  the buffer, `rpc_fremove` the *other* slot, `rpc_fopen(.., "w")`, one `rpc_fwrite`,
  `rpc_fclose`. `"w"` does not truncate an existing file, so a non-zero remove result is checked
  with an `"r"` open: a file still there aborts the save (`EIO`). The remove result is always
  logged; open, short write and a non-zero close are logged and fail the save (`EIO`, retried 10 s
  later). On success the other slot becomes current. A text that does not fit 4 KiB fails with
  `ENOSPC` instead of writing a truncated file; the saver task then stops for that boot.
- **Torn write:** power loss between remove and close leaves the written slot missing, short or
  with a wrong CRC; the untouched slot still holds the previous generation. Worst case lost: the
  changes since the last completed save.
- **Card gate**: saves and loads check `fenykl_card_io_allowed()`; closed means dirty values
  stay in RAM (closures are logged and counted). Init with a closed gate applies defaults; the
  saver loads at the first open gate, and options set meanwhile keep their RAM value. Once a USB
  cable was seen the gate stays closed until power-off (README, card gate): changes made after
  that, or still unsaved then, are lost at power-off.
- **Import:** `C:\FENYKL.CFG` (plain `name=value` lines, no header or CRC, `\r\n` allowed) is read
  after the slots and applied with the same parser (unknown names are carried as in a slot), marks
  the config dirty and is deleted after the next completed save. Hand-editing CF0/CF1 is not
  supported (the CRC rejects it).

Cost (a minimal demo build): `cfg.c` 5.6 KiB code, 5.3 KiB `.bss` (4 KiB text buffer, 1 KiB carried lines,
128 B `touched`), 44 B `.fenykl_cfg` per option plus its strings, the saver's 4 KiB stack from
`fw_malloc`. The load is at most three file reads and their CRC passes, once per boot.

## 4. Decisions

- **Identity = the namespaced name string**, stored as text; the name is never renamed, the label
  may change. Name collisions are caught at init.
- **Text, not binary:** readable on the card, diffable, no struct layout to version. Resilience
  comes from the CRC and the two slots.
- **Enums by name**, ints in decimal: reordering an enum or changing a range never mis-applies an
  old value; it degrades to the default and the old line is kept.
- **Two slots with seq + CRC:** there is no rename in the firmware API and `"w"` does not truncate,
  so one file cannot be replaced atomically; the reader decides which file is current.
- **Carry unknown and invalid lines**: dev builds with different module sets
  must not lose configuration. Costs a 1 KiB buffer and one lookup per line on save.
- **One-shot import** `FENYKL.CFG` for PC edits instead of a PC tool.
- **Debounced save in an own task**: plugins without a main loop still save; a
  user stepping through values does not cause a card write per step. The log flusher and the cfg
  saver could later share one task to save a stack.
- **No UI level** for now; the reserved `flags` byte leaves room.
- **`set` is CPU0-task only**: no locks; the dirty state is a change counter
  compared with the counter at the last completed save, so a change during a save stays dirty.
- **Live value is a plain global**, the descriptor points to it: one load in hooks, no lookup.

## 5. Unproven on the camera

- `rpc_fremove` has never run on the camera (the log's 4 MiB cap delete has not triggered); its
  return value for a missing file is unknown (the save handles any value by probing with `"r"`).
- `rpc_fclose`'s return value on success is assumed to be 0 (a non-zero one fails the save).
- Reading a file back into a buffer through the uncached alias after a cache purge
  for parsing.
