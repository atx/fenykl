"""Listings of programs, and DSL source for them.

A listing shows per word the fields that differ from the idle word, grouped by unit, under their
semantic names or ids; a mark after the value gives the field's confidence (`!` verified,
`~` inferred, none for a hypothesis). Idle words and runs of identical words are collapsed, and a
word that differs from the one before in at most `DELTA` fields is shown as that delta (`^`).
A control word starts with `-> T`, its target word index, in place of f229 and T's fields; the end
op starts with `end t=...`, T's bits as a raw value, in place of f229, T's fields and f313_319.
"""

from collections.abc import Iterator

from .fields import CONTROL_FLAG, END_FIELD, TARGET, Field, Kind
from .program import Program
from .units import UNITS
from .word import IDLE, Word

WIDTH = 110
DELTA = 3


def _value(f: Field, value: int) -> str:
    text = f"r{value}" if f.kind is Kind.REG else f"{value:x}"
    return f"{f.label}={text}{f.confidence.mark}"


def _control(word: Word) -> tuple[list[str], set[str]]:
    """A control word's leading part and the ids of the fields it shows."""
    if not word.control:
        return [], set()
    shown = {CONTROL_FLAG.id, *(f.id for f in TARGET.parts)}
    if word.end:
        return [f"end t={TARGET.get(word.value):x}"], shown | {END_FIELD.id}
    return [f"-> {word.target}{TARGET.confidence.mark}"], shown


def word_parts(word: Word, base: Word = IDLE) -> list[str]:
    """The fields that differ from `base` as "unit: field=value ..." groups in bit order,
    after a control word's target or end mark."""
    head, shown = _control(word)
    by_unit: dict[str, list[str]] = {}
    for f, v in word.changed(base):
        if f.id not in shown:
            by_unit.setdefault(f.unit, []).append(_value(f, v))
    return head + [f"{unit}: {' '.join(items)}" for unit, items in by_unit.items()]


def _runs(words: tuple[Word, ...]) -> Iterator[tuple[int, int]]:
    """(start, count) of runs of identical words."""
    start = 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i] != words[start]:
            yield start, i - start
            start = i


def listing(program: Program, title: str = "") -> str:
    lines = [f"; {title}"] if title else []
    lines.append(f"; {len(program)} words, {len(program.constants)} constant bytes")
    lines.append(
        "; marks: ! verified, ~ inferred, none a hypothesis; ^ = the word before, changed"
    )
    lines.append("; -> T: a control word and its target word index; end: the end op")
    prev = IDLE
    for start, count in _runs(program.words):
        word = program.words[start]
        index = f"{start}" if count == 1 else f"{start}..{start + count - 1}"
        head = f"{index:>9}  "
        if word == IDLE:
            lines.append(head + ("idle" if count == 1 else f"idle x{count}"))
            prev = IDLE
            continue
        line = head + (f"x{count} " if count > 1 else "")
        parts = word_parts(word)
        delta = len(word.changed(prev))
        if prev != IDLE and delta <= DELTA and delta < len(word.changed()):
            parts = ["^", *word_parts(word, prev)]
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
        note = (
            " end" if word.end else "" if word.target is None else f" -> {word.target}"
        )
        lines.append(f"    {word_source(word)},  # {index}{note}")
    lines.append("]")
    return "\n".join(lines) + "\n"
