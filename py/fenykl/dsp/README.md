# fenykl.dsp

Encoder, decoder, assembler (an embedded DSL) and listings for the E-M10 Mark II DSP: the engine
at 0x9D000000 that runs 320-bit horizontal microcode words from DRAM. Stdlib only. Stock programs
are read from the user's own firmware image at runtime; no stock microcode is part of this code.

## Layout

| module | contents |
|---|---|
| `fields.py` | the field table, the single source of truth: bits, kind, unit, idle value, confidence, fact |
| `units.py` | units (contiguous groups of fields) and their slot classes, derived from the table |
| `word.py` | `Word`: an immutable 320-bit value with views by field (`word["xreg"]`, `changed()`) and by unit (`slots()`); `IDLE` |
| `program.py` | `Program`, the item layout, extraction of the five stock programs, `assemble()` |
| `disasm.py` | listings and DSL source of a program |
| `memcpy.py` | `Copy16Model`: the model of the stock copy (item 16), built from the firmware's program: what each word does or does without, the transfer channels and roles, the resolution of a job into one load and one store |
| `state.py` | tri-state bytes with provenance and taint; the spaces (local memory, buffer, DRAM view); the register facts and boot parameters |
| `semantics.py` | word classes, parts, roles and channels, the hidden sequencer, `UnknownSemantics` |
| `emu.py` | `Boot`, `Job`, `Emulator`: runs a job word by word and applies its load and store |
| `commands.py` | the CPU setups of the commands as jobs (cmd 0xF) |
| `cli.py` | `fenykl dsp disasm ITEM`, `fenykl dsp source ITEM`, `fenykl dsp fields` |

Bit k of a word is bit (k % 8) of byte k // 8 of its 40 bytes. A word decodes from any 320-bit
value, so bytes -> Word -> bytes is exact whatever the table says; the tests check that the table
covers every bit once and that every stock word rebuilds from its field values alone.

The DSL builds a word from slot objects; unset fields keep the idle word's value, which is itself
composed from the table's idle values:

```python
from fenykl.dsp.units import CtlHi, RegsA
from fenykl.dsp.word import Word

Word(regs_a=RegsA(xreg=3, f123_127=2), ctl_hi=CtlHi(f308_309=0))
```

## Fields

Each field has a permanent positional id from its bits (`f115_119`, `f229`)
and optionally a semantic name (`xreg`). Ids never change for the same bits; names are renamed
freely, without aliases. Slots, `Word[...]` and `Word.from_fields` accept either.

To add or refine a field, edit `FIELDS`: split or merge entries so that the bits stay covered
exactly once (the import raises otherwise), and give the new entries their idle value, kind,
confidence and a one-line fact. An id names both ends of the field, so it changes exactly when the
bits change: DSL source naming the old id fails loudly. Moving a field to another unit is an edit
of its `unit`.

Kinds: `reg` (register specifier r0..r31), `opcode`, `route` (source or path select), `flag`
(one control bit), `const` (the same value in every stock word), `raw` (not decoded).

## Confidence

`verified` (read on the camera or in the bytes), `inferred` (one step beyond), `hypothesis`. A
field's confidence qualifies the field itself, its bits and kind; facts about single values or
words carry their own wording in `fact`. The rules:

- `assemble()` rejects words whose constant fields are off, and emits one
  `HypothesisFieldWarning` that lists every hypothesis field the program moves off idle;
- listings mark verified values `!` and inferred ones `~`; unmarked values are hypotheses;
- the emulator gives every output byte the weakest confidence of the semantics that produced or
  moved it (the field's own confidence does not enter); see the rule below.

### The emulator's confidence rule

Every semantic carries a confidence: each word class (`WordClass.confidence`), each part's
outcomes (`Part.seen`), each role's table and use (`Role.confidence`, `seen`, `beyond`), each
register fact. What was seen on the camera exactly is verified; what the model extends from it
is weaker:

- **Positions.** A semantic at its stock position is at most inferred, at any other position a
  hypothesis: an undecoded stock word runs as its class, inferred at its stock indices and a
  hypothesis elsewhere; a transfer whose role the sequencer assigns away from the role's stock
  word is a hypothesis; the idle word is verified anywhere.
- **Roles.** A role's outcome table and a role's use at a register other than its stock one
  are verified at the lengths they ran at on the camera (`seen`, 1 KiB unless stated) and
  inferred at others (word 76's use: a hypothesis); a use is never stronger than its register's
  fact.
- **Combinations.** A job that differs from the stock program in two or more words is at most
  inferred: no combination of edits is registered as seen together.
- **Lenient mode.** An ignored field, or a route nibble off the stock transfers' values, makes
  the job a hypothesis: its loaded, stored and zero-filled bytes alike.

Per output byte the emulator takes the minimum: the loaded bytes get the weakest of the classes
that ran, the parts' outcomes, the caps above and the load's roles (66, 67, 68); the stored bytes
the weakest of the buffer bytes they come from and the same with the store's roles. Since no word
of program 16 is decoded, every byte it copies is at most inferred, the stock copy's too.
`JobResult.taint` qualifies the status and whether and what the job stored: the weakest of the
outcomes that decided it (an idled word, a role's register) under the caps, else the load's
and the store's.

## The emulator

```python
from fenykl.dsp import commands
from fenykl.dsp.emu import Boot, Emulator
from fenykl.dsp.memcpy import Copy16Model

model = Copy16Model.from_image(image)  # the stock E-M10 II .BIN
boot = Boot()  # BootParams: r14, r29, r21's format; None = unknown
boot.put(src, data, "src")  # known DRAM bytes, with a source name
result = Emulator(boot, model).run(commands.copy(model.program, src, dst, len(data)))
out = boot.read(dst, len(data))  # Bytes: data, known, taint, src (provenance), off
```

A `Job` is a program plus the bytes its command's CPU setup writes to local memory
(`commands.copy` for cmd 0xF: src, dst and len at 0x9D000000..08). State persists across the jobs
of a `Boot`: local memory, the buffer between load and store (position-addressed, reaching below
index 0), the register file and the DRAM view. `back_firmware(boot, image)` maps the main code
of a stock `.BIN` for loads from firmware text. A byte is known or UNKNOWN, and its source says
where it came from or why it is unknown; the emulator never guesses a byte.

Each word gets the semantics of its class (`model.classes`): the idle word, the register
transfer (matched by its signature, every field idle but the register groups, the route nibbles
and f308_309) or an undecoded stock word of program 16 matched by its value. The model derives
these classes, the transfers' register contexts and their reference values from the stock
program it is built from, so no stock word is part of the code. At the end the sequencer gives
the transfers their roles (the stock word 66 loads, 67 addresses, 68 offsets, 73 counts, 76
offsets the store, 47 formats it, ...) and the job resolves into a status, one load and one store.

Strict mode (the default) raises `UnknownSemantics` for a word that matches no class, naming the
fields that differ from the nearest class, and for a role at a register without registered
semantics, and for a transfer whose route nibbles differ from the stock transfers' values.
`Emulator(boot, model, strict=False)` runs such a word as its nearest class, ignores the fields
and taints every byte the job loads or stores "hypothesis".

### Adding a semantic

- **A register value**: add or refine a `RegisterFact` in `state.py` (an exact value or an
  interval, from the job's request or the boot parameters, with its confidence). A role that
  reads the value then works at that register without further change.
- **A role's outcome at a register** (seen on the camera): add the register to the role's
  `table` in `memcpy._CHANNELS` (and the job lengths it ran at to `seen`), or to
  `memcpy._FORMATS` for word 47.
- **A new use of the register value**: a new `semantics.Use`, handled in `Copy16Model._role`.
- **What a stock word does without it**: its `Part` in `memcpy._PARTS` (`token`, `missing`
  outcome and the fact); a swept register field goes in `operand`.
- **A new word class**: a `WordClass` subclass with `match` (field values that identify it),
  `masks` (the bits of each matched field it fixes, default all), `operands` (fields it reads
  or ignores) and `act`, which records into the job's `Trace`; add it to the model's
  `classes`. Every field outside `match` and `operands` must be idle.

Each semantic carries its confidence, and a test vector should come with it: the curated camera
vectors live in `tests/data/dsp/` (jobs, expected status and dst with UNKNOWN regions).
