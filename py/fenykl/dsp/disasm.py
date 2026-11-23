"""Listings of programs, and DSL source for them.

A listing shows per word the fields that differ from the idle word, grouped by unit (strobes
first), under their semantic names or ids; a mark after the value gives the field's confidence
(`!` verified, `~` inferred, none for a hypothesis). Idle words and runs of identical words are collapsed, and a
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

from .fields import CONTROL_FLAG, IDLE_OP, OP_FIELD, TARGET, Field, Kind, Operands, op
from .program import Program
from .units import UNITS
from .word import IDLE, Word

WIDTH = 110
DELTA = 3


def _value(f: Field, value: int) -> str:
    text = f"r{value}" if f.kind is Kind.REG else f"{value:x}"
    return f"{f.label}={text}{f.confidence.mark}"


def _imm(value: int) -> str:
    return f"#{value}" if -10 < value < 10 else f"#{value:#x}"


def operand_text(args: Operands) -> str:
    """`args` as "rA, rB, #imm", leaving out what the format has not."""
    parts = [f"r{args.ra}"]
    if args.rb is not None:
        parts.append(f"r{args.rb}")
    if args.imm is not None:
        parts.append(_imm(args.imm))
    return ", ".join(parts)


def _head(word: Word, marks: bool = True) -> tuple[str, set[str]]:
    """The op, operands and target part of `word` and the ids of the fields it stands for."""
    meaning = op(word.op)
    head = meaning.name + (meaning.confidence.mark if marks else "")
    shown = {OP_FIELD.id}
    if meaning.format is not None:
        head += f" {operand_text(meaning.format.decode(word.value))}"
        shown.update(f.id for f in meaning.format.fields)
    if word.target is not None:
        head += f" -> {word.target}" + (TARGET.confidence.mark if marks else "")
        shown.update(f.id for f in TARGET.parts)
    if word.control:
        # f229 is clear in every stock control word; a set one would equal idle and go unseen.
        shown.add(CONTROL_FLAG.id)
        flag = CONTROL_FLAG.get(word.value)
        if flag:
            head += f" {_value(CONTROL_FLAG, flag)}"
    return head, shown


def word_parts(word: Word, base: Word = IDLE) -> list[str]:
    """The op, a control op's target, then the fields that differ from `base` as
    "unit: field=value ..." groups in unit order, each with its strobes first."""
    head, shown = _head(word)
    changed = {f.id: v for f, v in word.changed(base) if f.id not in shown}
    parts = [head]
    for name, unit in UNITS.items():
        items = [_value(f, changed[f.id]) for f in unit.fields if f.id in changed]
        if items:
            parts.append(f"{name}: {' '.join(items)}")
    return parts


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


def word_source(word: Word) -> str:
    """`word` as a DSL expression."""
    if word == IDLE:
        return "IDLE"
    slots = word.slots()
    return f"Word({', '.join(f'{unit}={slot!r}' for unit, slot in slots.items())})"


def _ctl_source(word: Word, label: str) -> str:
    """Control word `word` as a `Ctl` to `label`, its other fields as the base."""
    value = word.value
    for f in (OP_FIELD, CONTROL_FLAG, *TARGET.parts):
        value = f.put(value, f.get(IDLE.value))
    base = Word.from_int(value)
    args = f"{word.op}, {label!r}" + ("" if base == IDLE else f", {word_source(base)}")
    return f"Ctl({args})"


def source(
    program: Program,
    name: str = "PROGRAM",
    labels: Mapping[str, int] | None = None,
    origin: int = 0,
) -> str:
    """Python source that rebuilds `program` from slot objects (constants left out).

    With `labels` (`labels(statements, origin)` of `program.py`) the source is a statement
    list for `assemble(..., origin=origin)`: each label before its word, a control op whose
    target has a label as a `Ctl`; the comments give word indices from `origin`."""
    classes = ", ".join(cls for cls in (u.cls_name for u in UNITS.values()))
    lines = [
        f"from fenykl.dsp.units import {classes}",
        "from fenykl.dsp.word import IDLE, Word",
    ]
    at: dict[int, list[str]] = {}
    for label, index in (labels or {}).items():
        if not origin <= index <= origin + len(program):
            raise ValueError(f"label {label!r} at {index}, outside the program")
        at.setdefault(index, []).append(label)
    if labels:
        lines.append("from fenykl.dsp.program import Ctl, Label")
    lines += ["", f"{name} = ["]
    for index, word in enumerate(program.words, origin):
        lines += [f"    Label({label!r})," for label in at.get(index, [])]
        note = "" if word.op == IDLE_OP else f" {_head(word, marks=False)[0]}"
        named = at.get(word.target) if word.target is not None else None
        text = _ctl_source(word, named[0]) if named else word_source(word)
        lines.append(f"    {text},  # {index}{note}")
    lines += [f"    Label({label!r})," for label in at.get(origin + len(program), [])]
    lines.append("]")
    return "\n".join(lines) + "\n"
