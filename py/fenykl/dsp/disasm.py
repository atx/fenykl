"""Listings of programs, and DSL source for them.

A listing shows per word the fields that differ from the idle word, grouped by unit, under their
semantic names or ids; a mark after the value gives the field's confidence (`!` verified,
`~` inferred, none for a hypothesis). Idle words and runs of identical words are collapsed, and a
word that differs from the one before in at most `DELTA` fields is shown as that delta (`^`).
Every word but the idle word starts with its op's name (`fields.op`) in place of f313_319; a
control op 0..3 follows it with `-> T`, its target word index, in place of T's fields. A control
op drops f229 while it is clear, as in every stock control word. A `|` after the index marks the
delay slots of a control word.
"""

from collections.abc import Iterator

from .fields import CONTROL_FLAG, IDLE_OP, OP_FIELD, TARGET, Field, Kind, op
from .program import Program
from .units import UNITS
from .word import IDLE, Word

WIDTH = 110
DELTA = 3


def _value(f: Field, value: int) -> str:
    text = f"r{value}" if f.kind is Kind.REG else f"{value:x}"
    return f"{f.label}={text}{f.confidence.mark}"


def _head(word: Word, marks: bool = True) -> tuple[str, set[str]]:
    """The op and target part of `word` and the ids of the fields it stands for."""
    meaning = op(word.op)
    head = meaning.name + (meaning.confidence.mark if marks else "")
    shown = {OP_FIELD.id}
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
    "unit: field=value ..." groups in bit order."""
    head, shown = _head(word)
    by_unit: dict[str, list[str]] = {}
    for f, v in word.changed(base):
        if f.id not in shown:
            by_unit.setdefault(f.unit, []).append(_value(f, v))
    return [head] + [f"{unit}: {' '.join(items)}" for unit, items in by_unit.items()]


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
        "; ops: loop brf br call ret halt, opNN a data op (hex), idle; -> T: the target word "
        "index; | a delay slot"
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


def source(program: Program, name: str = "PROGRAM") -> str:
    """Python source that rebuilds `program` from slot objects (constants left out)."""
    classes = ", ".join(cls for cls in (u.cls_name for u in UNITS.values()))
    lines = [
        f"from fenykl.dsp.units import {classes}",
        "from fenykl.dsp.word import IDLE, Word",
        "",
        f"{name} = [",
    ]
    for index, word in enumerate(program.words):
        note = "" if word.op == IDLE_OP else f" {_head(word, marks=False)[0]}"
        lines.append(f"    {word_source(word)},  # {index}{note}")
    lines.append("]")
    return "\n".join(lines) + "\n"
