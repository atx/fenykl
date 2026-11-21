# fenykl.dsp

Encoder, decoder, assembler (an embedded DSL) and listings for the E-M10 Mark II DSP: the engine
at 0x9D000000 that runs 320-bit horizontal microcode words from DRAM. Stdlib only. Stock programs
are read from the user's own firmware image at runtime; no stock microcode is part of this code.

## Layout

| module | contents |
|---|---|
| `fields.py` | the field table, the single source of truth: bits, kind, unit, idle value, confidence, fact; the op meanings `OPS`, the target `TARGET`, the delay slots `DELAY_SLOTS` |
| `units.py` | units (contiguous groups of fields) and their slot classes, derived from the table |
| `word.py` | `Word`: an immutable 320-bit value with views by field (`word["xreg"]`, `changed()`), by unit (`slots()`) and as a control word (`control`, `end`, `target`); `IDLE` |
| `program.py` | `Program`, the item layout, extraction of the five stock programs, `assemble()` |
| `disasm.py` | listings and DSL source of a program |
| `memcpy.py` | `Copy16Model`: the model of the stock copy (item 16), built from the firmware's program: what each word does or does without, the transfer channels and roles, the resolution of a job into one load and one store and their application |
| `state.py` | tri-state bytes with provenance and taint; the spaces (local memory, buffer, DRAM view); the register facts and boot parameters |
| `semantics.py` | word classes, parts, roles and channels, the hidden sequencer, `UnknownSemantics` |
| `emu.py` | `Boot`, `Job`, `Emulator`: runs a job word by word and has its model apply the trace |
| `resample.py` | `Resample13Model`: the model of the stock resampler (item 13), built from the firmware's program: its arithmetic measured from outside, the column blocks and their held line pairs; `model.job` (the stock words with own taps) and `predict` |
| `resample_edits.py` | the edits of program 13 measured on the camera: edit kinds, job classes and per plane what each does |
| `taps.py` | `Taps`: the resampler's 16 phases of 4 s16 taps, checked, packed as the 32 tap words, from floats with an explicit rounding |
| `commands.py` | the CPU setups of the commands as jobs (cmd 0xF, cmd 0xC) |
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
(one control bit), `const` (the same value in every stock word), `imm` (an immediate operand),
`raw` (not decoded).

### The op and control words

Bits 313..319 are one 7-bit op field, `op`, in every word, and f15_22 (`imm`) is that op's 8-bit
immediate, 0 whenever the op is idle (both verified in the bytes of the five stock programs and
across later firmware versions of the same family). `OPS` in the table gives each value its name,
confidence and fact; `fields.op(value)` is total:

| op | name | meaning | confidence |
|---|---|---|---|
| 0 | `loop` | loop end, back to T | inferred, strong |
| 1 | `brf` | forward conditional branch, the guard of a loop | inferred |
| 2 | `br` | conditional branch either way | inferred |
| 3 | `call` | call (when a return lies before T; the other op 3 words are open) | inferred, strong |
| 4 | `ret` | return to the word after the call's delay slots | inferred, strong |
| 7 | `halt` | ends the job; word 5 of every program, where the call at word 0 returns | inferred |
| 8..73 | `opNN` | a data op (NN in hex), what it does open | inferred |
| 127 | `idle` | no op | inferred |

5, 6, 74..126 and the data op values 0x22, 0x26, 0x27, 0x2d, 0x39, 0x3b and 0x45 never occur.
Ops 0..4 are the control ops (`CONTROL_OPS`, `word.control`), and
f229 (`ctl_n`) is clear exactly in them. Ops 0..3 name a target T = bits 15..22 | bits 128..132 <<
8, an absolute word index into the program (`word.target`, None for other words): T follows its
target word wherever code moved across later firmware versions of the same family, and lies inside
its program in every op 0..3 word (verified in the bytes). In those words `imm` is T's low byte and
f128_132 holds T's high bits, not a register. The conditions of `brf` and `br` have no field of
their own; the candidate is the register ra (f123_127), which ops 0..2 write and calls hold
(hypothesis).

T's bits stay in the positional fields, so every word has the same partition and the codec stays
exact; `TARGET` in the table composes T from `imm` and f128_132.

### Data ops: the op slot

A data op holds its operands in five fields, the op slot: `op`, `imm`, the register `ra`
(f123_127), f128_132 and f120_122. `ra` changes only in a word that carries an op (a data op or a
control op 0..2; a call holds it), verified in the bytes across later firmware versions of the
same family; whether it is a destination, a source or a base is open. The data ops fall into
families by the layout of the slot (`FAMILIES`; the formats are verified in the bytes, every
meaning is a hypothesis unless tagged):

| family | ops | operands | f120_122 |
|---|---|---|---|
| A | 0x0c..0x1a even | ra, imm13 | an operand |
| A' | 0x0d..0x1b odd | ra, rb | an operand |
| B | 0x0a 0x0b 0x37 0x38 | ra, imm16 (imm15 in 0x37) | immediate bits 13..15 |
| C | 0x3e 0x3f 0x40 | ra, rb, imm5 | an operand |
| D | 0x1c..0x25 but 0x22, 0x36 | ra, imm13 | 0 in 99 % of the words |
| E | 0x08 0x09 0x28..0x35 but 0x2d, 0x3a 0x3c 0x3d 0x41..0x43 0x47..0x49 | ra, rb | an operand |
| F | 0x44 0x46 | ra, imm13 | open |

rb is f128_132; imm13 is `imm` | f128_132 << 8 and imm16 adds f120_122 << 13. 0x0c's and 0x10's
immediates are two's complement (inferred: their 0x1fxx values read as small negative numbers).
The A ops are the immediate forms of the A' ops at op + 1. The strongest meaning is 0x1a, a read
of the local word at its immediate (inferred, strong): program 16's three 0x1a words name the
local words of src, dst and len. `word.operands` decodes a data op's operands (`Operands`), and
`Word.data(op, Operands(...))` assembles one, rejecting operands outside the format; listings show
`op1a~ r5, #1` and `op3f~ r2, r2, #5`, and a slot field outside the format stays a field.

Every control op has four delay slots (`DELAY_SLOTS`, `Program.delay_slots()`): in the five stock
programs words i+1..i+4 of a control word at i are never a target or a control word (verified in
the bytes; across later firmware versions of the same family only short loop ends break it). The
reading, inferred strong, is that the slots always run and the transfer takes effect after them,
so a call returns to i+5. A program is "call 10, halt at 5": word 0 calls the job routine, whose
return comes back to word 5.

Listings start every non-idle word with its op name and confidence mark, follow a control op 0..3
with `-> T`, and mark delay slots with `|` after the index; `fenykl dsp fields` prints T's
definition, the op meanings and the delay-slot rule after the table.

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
- **Outcome tables.** What a role's table (or a part's swept register, or an idled part) gives
  is verified at the job lengths it ran at on the camera (`seen`, 1 KiB unless stated) and
  inferred at others.
- **Uses.** A role's use of its register's value (address, offset, count, format) is inferred,
  at its stock register too, and never stronger than the register's fact; word 76's use at
  another register is a hypothesis beyond the lengths it ran at.
- **Combinations.** A job that differs from the stock program in two or more words is at most
  inferred: no combination of edits is registered as seen together.
- **Lenient mode.** An ignored field, or a route nibble off the stock transfers' values, makes
  the job a hypothesis: its loaded, stored and zero-filled bytes alike.

Per output byte the emulator takes the minimum: the loaded bytes get the weakest of the classes
that ran, the parts' outcomes, the caps above and the load's roles (66, 67, 68); the stored bytes
the weakest of the buffer bytes they come from and the same with the store's roles. Since no word
of program 16 is decoded, every byte it copies is at most inferred, the stock copy's too.
`JobResult.taint` qualifies what this job did: the weakest of the outcomes that decided its
status and whether it stored (an idled word, a role's register) under the caps, and of the store
when it stored; with no deciding outcome, the load's and the store's. Stored bytes can be weaker
still through the buffer they come from (a held buffer of an older job).

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
index 0), the resampler's held line pairs, the register file and the DRAM view. `back_firmware(boot, image)` maps the main code
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

### The resampler

```python
from fenykl.dsp.resample import Resample13Model
from fenykl.dsp.taps import Rounding, Taps

model = Resample13Model.from_image(image)
taps = Taps.of([(-8, 264, 0, 0)] * 16)  # phase p = taps 0..3, s16, 256 = 1.0
taps = Taps.from_floats([(-0.03, 1.03, 0, 0)] * 16, Rounding.HALF_EVEN)  # each tap rounded
boot.put(src, planes, "src")  # three planes of 64 x H bytes at stride 64 (format 0x306)
boot.put(table, entries, "table")  # H x {s32 a, u32 b}: source line and step in 16.16
job = model.job(taps, src, dst, table, w, h)  # the stock words, these tap words
result = Emulator(boot, model).run(job)
```

`predict(model, taps, image, [(a, b), ...], w)` runs that job on a fresh boot and returns a
`Prediction`: `dst` (the footprint as `Bytes`), `confidence()` per byte (None if unknown) and
`unknown()`, a mask per reason (the source's name: `resampler 2d`, `resampler bottom`,
`resampler right`, `not written`, ...; the source's fact says why).

`commands.resample` writes what cmd 0xC's setup writes: the program's constant block (the 32 tap
words the setup copies from DRAM 0xA324BC00, the item's constants) to local words 0x00..0x1F and
the request to 0x742..0x747; the table stays in DRAM. Own taps are a program with other constants
(`model.job` builds it from the stock words). Word 2p of the tap words is phase p's tap 0 | tap 1
<< 16, word 2p + 1 tap 2 | tap 3 << 16. `Taps.from_floats` rounds each tap on its own and does not
renormalise a phase to 256. No word of program 13 is decoded: its classes are the idle word and the
stock words matched by value, and the law (`resample.py`'s docstring) applies to the stock program
as it is. Every byte the law computes is at most inferred, a law measured from outside;
hypotheses: u0 held into [-16, 16) (b 2), a phase between the 16, a tap outside -637..2048 (the taps
that ran) on the plane-0 bytes it reaches, the held lines after a job that is not an H 16 identity.
A partial column block is unknown unless the job is an H 16 identity with a unit phase-0 kernel.
Bytes the law does not give (the bottom and right edges, plane 0 at b != 1 where the vertical pass
is not exact, a fractional a with b != 1, W < 64 with b != 1, an odd H above 1) are UNKNOWN with a
source that says why. The curated camera vectors are in `tests/data/dsp/resample/` (jobs with their
taps, table and source image, an edit as {word, kind}; dst segments).

#### Edits

`resample_edits.FACTS` holds the edits measured on the camera: a stock word with some bits set to
the idle word's (`Idle`: the grid and C2's free bits, the grid, C2's free bits, the op bits, one
bit) or one field set to a value (`FieldValue("f211_215", 4)`: a register renamed), and per
job class (`Probe`: a flat source or not, the step b, W; H 16) what each plane does: the stock law
(`Stock`), unknown (`Changed`), or a law (`Operands`: plane 0 with each tap multiplying another
tap's line, nothing or a bounded value; `Phase`: a chroma plane sampled at another horizontal
phase). The model matches an edited word by its value, so the classes hold the measured edited
words too. `model.job(..., edits=[(439, GRID_C2)])` and `predict(..., edits=...)` apply them;
`Resample13Model(program, facts)` takes another outcome table.

- one measured edit on a class it ran on: its planes; `JobResult.edits` names it, verified (the
  bytes stay at most inferred, through the law);
- the same edit on another class, or two edits together: dst unknown ("resampler edit") in strict
  mode, the stock law as a hypothesis in lenient mode;
- any other edited word: `UnknownSemantics` (strict), the stock law as a hypothesis (lenient).

An edited job forgets the held lines, and its partial column block is unknown.

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
