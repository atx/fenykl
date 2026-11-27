"""Listings of programs, and DSL source (`asm` bundles) for them.

A listing shows per word the fields that differ from the idle word, grouped by unit (strobes
first), under their semantic names or ids; the units of the heads S0..S3, X0 and X1 show their
operand view instead (`fields.HEAD_VIEWS`: op, the registers, an immediate composed and signed as
the machine reads it, each where it differs): the operands the head reads
(`semantics.head_reads`), then each field with a bit outside them that differs, raw; a head whose
reads are not known shows every operand that differs. A mark after the value gives the field's
confidence (`!` verified, `~` inferred, none for a hypothesis). Idle words and runs of identical words are collapsed, and a
word that differs from the one before in at most `DELTA` fields is shown as that delta (`^`).
Every word but the idle word starts with its op's name (`fields.op`) in place of f313_319; a
control op 0..3 follows it with `-> T`, its target word index, in place of T's fields. A data op
follows it with its operands in its family's format in place of their fields: `ra, rb, #imm`, an
immediate in decimal below 10 and in hex above, signed where the op's is two's complement; a field
of the slot outside the format (f120_122 as an operand, imm in a register form) stays a field. A
control op drops f229 while it is clear, as in every stock control word. A `|` after the index
marks the delay slots of a control word.
"""

from collections.abc import Iterator, Mapping

from .asm import EXPORTS, OP, VIEWS, Bundle
from .fields import (
    CONTROL_FLAG,
    HEAD_VIEWS,
    IDLE_OP,
    OP_FIELD,
    TARGET,
    Composite,
    Field,
    Kind,
    Operands,
    op,
)
from .program import Program
from .semantics import OP_SEMANTICS, head_reads
from .units import UNITS
from .word import IDLE, Word

WIDTH = 110
DELTA = 3


def _value(f: Field, value: int) -> str:
    text = f"r{value}" if f.kind is Kind.REG else f"{value:x}"
    return f"{f.label}={text}{f.confidence.mark}"


def _operand(name: str, o: Field | Composite, value: int) -> str:
    """A head view's operand: registers as rN, immediates signed, the others in hex."""
    if isinstance(o, Composite):
        text = _number(value)
    else:
        text = f"r{value}" if o.kind is Kind.REG else f"{value:x}"
    return f"{name}={text}{o.confidence.mark}"


def _number(value: int) -> str:
    return f"{value}" if -10 < value < 10 else f"{value:#x}"


def _imm(value: int) -> str:
    return f"#{_number(value)}"


def operand_text(args: Operands) -> str:
    """`args` as "rA, rB, #imm", leaving out what the format has not."""
    parts = [f"r{args.ra}"]
    if args.rb is not None:
        parts.append(f"r{args.rb}")
    if args.imm is not None:
        parts.append(_imm(args.imm))
    return ", ".join(parts)


def _head(word: Word) -> tuple[str, set[str]]:
    """The op, operands and target part of `word` and the ids of the fields it stands for."""
    meaning = op(word.op)
    sem = OP_SEMANTICS.get(word.op)
    tier = meaning.confidence if sem is None else sem.confidence
    head = meaning.name + tier.mark
    shown = {OP_FIELD.id}
    if meaning.format is not None:
        head += f" {operand_text(meaning.format.decode(word.value))}"
        shown.update(f.id for f in meaning.format.fields)
    if word.target is not None:
        head += f" -> {word.target}{TARGET.confidence.mark}"
        shown.update(f.id for f in TARGET.parts)
    if word.control:
        # f229 is clear in every stock control word; a set one would equal idle and go unseen.
        shown.add(CONTROL_FLAG.id)
        flag = CONTROL_FLAG.get(word.value)
        if flag:
            head += f" {_value(CONTROL_FLAG, flag)}"
    return head, shown


def _op_note(word: Word) -> str:
    """The op's mnemonic (`MNEMONICS.md`), its code where it has none, and a control op's
    target word index."""
    note = OP.names.get(word.op, f"{word.op:#04x}")
    return note if word.target is None else f"{note} -> {word.target}"


def word_parts(word: Word, base: Word = IDLE) -> list[str]:
    """The op, a control op's target, then the fields that differ from `base` as
    "unit: field=value ..." groups in unit order, each with its strobes first."""
    head, shown = _head(word)
    changed = {f.id: v for f, v in word.changed(base) if f.id not in shown}
    parts = [head]
    for name, unit in UNITS.items():
        if name in HEAD_VIEWS:
            items = _head_items(name, word, base)
        else:
            items = [_value(f, changed[f.id]) for f in unit.fields if f.id in changed]
        if items:
            parts.append(f"{name}: {' '.join(items)}")
    return parts


def _head_items(unit: str, word: Word, base: Word) -> list[str]:
    """The head view's operands of `unit` in `word` that differ from `base`: those the head
    reads and the fields with other bits that differ, or every operand where not known."""
    reads = head_reads(unit, word.value)
    if reads is None:
        # Without the operands asm adds, which repeat bits of others, so each bit shows once.
        operands = HEAD_VIEWS[unit].operands
    else:
        operands = {
            k: o for k, o in VIEWS[unit].operands.items() if k in reads or k == "op"
        }
    items, span = [], 0
    for k, o in operands.items():
        span |= o.span
        if o.get(word.value) != o.get(base.value):
            items.append(_operand(k, o, o.get(word.value)))
    if reads is not None:
        changed = word.value ^ base.value
        items += [
            _value(f, f.get(word.value))
            for f in UNITS[unit].fields
            if changed & f.span & ~span
        ]
    return items


def _runs(words: tuple[Word, ...], slots: set[int]) -> Iterator[tuple[int, int]]:
    """(start, count) of runs of identical words, all in delay slots or none."""
    start = 0
    for i in range(1, len(words) + 1):
        if (
            i == len(words)
            or words[i] != words[start]
            or (i in slots) != (start in slots)
        ):
            yield start, i - start
            start = i


def listing(program: Program, title: str = "") -> str:
    lines = [f"; {title}"] if title else []
    lines.append(f"; {len(program)} words, {len(program.constants)} constant bytes")
    lines.append(
        "; marks: ! verified, ~ inferred, none a hypothesis; ^ = the word before, changed"
    )
    lines.append(
        "; ops: loop brf br call ret halt, opNN a data op (hex) with ra, rb, #imm by its family, "
        "idle; -> T: the target word index; | a delay slot"
    )
    slots = program.delay_slots()
    prev = IDLE
    for start, count in _runs(program.words, slots):
        word = program.words[start]
        index = f"{start}" if count == 1 else f"{start}..{start + count - 1}"
        head = f"{index:>9} {'|' if start in slots else ' '} "
        if word == IDLE:
            lines.append(head + ("idle" if count == 1 else f"idle x{count}"))
            prev = IDLE
            continue
        line = head + (f"x{count} " if count > 1 else "")
        parts = word_parts(word)
        delta = len(word.changed(prev))
        if prev != IDLE and delta <= DELTA and delta < len(word.changed()):
            name, *changed = word_parts(word, prev)
            parts = [name, "^", *changed]
        prev = word
        for part in parts:
            if len(line) + len(part) > WIDTH and line.strip():
                lines.append(line.rstrip())
                line = " " * len(head)
            line += part + "  "
        lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def source(
    program: Program,
    name: str = "PROGRAM",
    labels: Mapping[str, int] | None = None,
    origin: int = 0,
) -> str:
    """Python source that rebuilds `program` as bundles of `asm` (constants left out).

    With `labels` (`labels(bundles, origin)` of `program.py`) the source is a bundle list for
    `assemble(..., origin=origin)`: a labelled word starts with its `Label`, a control op whose
    target has a label names it; the comments give word indices from `origin` and the op's
    mnemonic with a control op's target index."""
    at: dict[int, str] = {}
    for label, index in (labels or {}).items():
        if not origin <= index < origin + len(program):
            raise ValueError(f"label {label!r} at {index}, outside the program")
        if index in at:
            raise ValueError(f"labels {at[index]!r} and {label!r} on one word")
        at[index] = label
    lines = [f"from fenykl.dsp import {', '.join(EXPORTS)}", "", f"{name} = ["]
    for index, word in enumerate(program.words, origin):
        note = "" if word.op == IDLE_OP else f" {_op_note(word)}"
        lines.append(f"    {Bundle.of(word, at, index)!r},  # {index}{note}")
    lines.append("]")
    return "\n".join(lines) + "\n"
