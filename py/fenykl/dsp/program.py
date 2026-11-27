"""Own programs: word sequences with an optional constant block; `assemble` builds them.

`assemble` takes bundles and parts of `asm`, one per word; a bundle's `Label` names its word, a
control op's target may name a label.

Own programs ran on the camera in program 16's frame: its words 0..9 (word 0 calls word 10, its
delay slots set the stack and link registers, word 5 halts), the body from word 10, then a halt
and four idles, of which only three run (a halt's tail). The smallest such image accepted for a
run is 15 words with no body, and it ran (verified on the camera, pre-registered); whether any
of words 0..9 can go is not measured. Program 16's words 10..12 are not needed: the body may
start at 10 or replace them, and sets its own registers (verified on the camera,
pre-registered). Images that change words 0..9 were not run; idling word 0 of the stock copy
ends the job at word 5 without running the routine (verified on the camera).
"""

import warnings
from collections.abc import Iterable
from dataclasses import dataclass

from .asm import Bundle, Part, bundle
from .fields import DELAY_SLOTS, FIELDS, Confidence
from .word import IDLE, Word


class AssemblyError(ValueError):
    """A label defined twice or never."""


class HypothesisFieldWarning(UserWarning):
    """An assembled program sets fields whose decoding is only a hypothesis."""


@dataclass(frozen=True)
class Program:
    words: tuple[Word, ...]
    constants: bytes = b""

    def __len__(self) -> int:
        return len(self.words)

    def delay_slots(self) -> set[int]:
        """The indices of the words in the delay slots of a control word (`DELAY_SLOTS`)."""
        return {
            j
            for i, w in enumerate(self.words)
            if w.control
            for j in range(i + 1, min(i + DELAY_SLOTS + 1, len(self.words)))
        }


def labels(bundles: Iterable[Bundle | Part], origin: int = 0) -> dict[str, int]:
    """Each label's word index: `origin` plus the number of words before its bundle."""
    names: dict[str, int] = {}
    for index, b in enumerate(map(bundle, bundles), origin):
        if b.label is None:
            continue
        if b.label in names:
            raise AssemblyError(f"label {b.label!r} defined twice")
        names[b.label] = index
    return names


def assemble(
    bundles: Iterable[Bundle | Part], constants: bytes = b"", *, origin: int = 0
) -> Program:
    """A program from bundles placed at word index `origin`, in two passes: the labels'
    indices, then the words, each target looked up (one over 13 bits raises ValueError). A
    target given as a word index stays as it is, absolute. Checks every word and warns once
    about hypothesis fields."""
    bundles = tuple(map(bundle, bundles))
    names = labels(bundles, origin)
    words = []
    for b in bundles:
        if b.target is not None and b.target not in names:
            raise AssemblyError(f"label {b.target!r} is not defined")
        words.append(b.word(names))
    weak = set()
    for word in words:
        word.check()
        weak.update(
            f.id for f, _ in word.changed(IDLE) if f.confidence is Confidence.HYPOTHESIS
        )
    if weak:
        ids = [f.id for f in FIELDS if f.id in weak]
        warnings.warn(
            f"the program sets {len(ids)} hypothesis fields: {', '.join(ids)}",
            HypothesisFieldWarning,
            stacklevel=2,
        )
    return Program(tuple(words), constants)


def call_return_errors(program: Program) -> list[str]:
    """Report each op 3 whose return point, i + 5 after its delay slots (the call reading), is
    no word of `program`; the return is implicit, so no label can move it."""
    return [
        f"op 3 at word {i} returns to {i + DELAY_SLOTS + 1}, past the end ({len(program)} words)"
        for i, w in enumerate(program.words)
        if w.op == 3 and i + DELAY_SLOTS + 1 >= len(program)
    ]
