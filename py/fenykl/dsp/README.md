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
| `memcpy.py` | the stock copy program (item 16) written in the DSL |
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
- the emulator (to come) raises on an undecoded field that is off idle, unless told to ignore
  it, and gives every output byte the weakest confidence of the fields it depends on.

## Next

The emulator works per word on a state of DSP local memory, an internal buffer, the register
file and a sparse DRAM view (`state.py`, `semantics.py`, `emu.py`). A job is a `Program` plus a
local-memory image; `commands.py` will rebuild the stock CPU setups that write that image. A
`Program` already carries its constant block, which the setups copy to local memory.
