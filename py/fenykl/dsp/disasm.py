"""Listings of programs, and DSL source for them.

A listing shows per word the fields that differ from the idle word, grouped by unit, under their
semantic names or ids; a mark after the value gives the field's confidence (`!` verified,
`~` inferred, none for a hypothesis). Idle words and runs of identical words are collapsed, and a
word that differs from the one before in at most `DELTA` fields is shown as that delta (`^`).
"""

from collections.abc import Iterator

from .fields import Field, Kind
from .program import Program
from .units import UNITS
from .word import IDLE, Word

WIDTH = 110
DELTA = 3


def _value(f: Field, value: int) -> str:
    text = f"r{value}" if f.kind is Kind.REG else f"{value:x}"
    return f"{f.label}={text}{f.confidence.mark}"


def word_parts(word: Word, base: Word = IDLE) -> list[str]:
    """The fields that differ from `base` as "unit: field=value ..." groups in bit order."""
    by_unit: dict[str, list[str]] = {}
    for f, v in word.changed(base):
        by_unit.setdefault(f.unit, []).append(_value(f, v))
    return [f"{unit}: {' '.join(items)}" for unit, items in by_unit.items()]


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
        lines.append(f"    {word_source(word)},  # {index}")
    lines.append("]")
    return "\n".join(lines) + "\n"
