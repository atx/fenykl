"""A job and the flow of its stepping: `Job`, the program and its request; `Flow`, the words
`emu.Executor.step` executed (`Step`) and why it stopped (`Stop`)."""

from dataclasses import dataclass
from enum import Enum

from .fields import Confidence, Operands, op
from .program import Program


@dataclass(frozen=True)
class Job:
    """A program and its request: (offset, bytes) writes to DSP local memory."""

    program: Program
    local: tuple[tuple[int, bytes], ...]


class Stop(Enum):
    HALT = "halt"
    OPEN_END = "the halt, but whether the job ends is not known"
    UNDECIDED = "a conditional control op the branching hypothesis does not decide"
    STEP_LIMIT = "the step limit"


@dataclass(frozen=True)
class Step:
    """One executed word."""

    index: int
    op: int
    slot: bool = False
    """In the delay slots of a control op."""
    operands: Operands | None = None
    target: int | None = None
    taken: bool | None = None
    """For a control op: whether it transfers, None when undecided."""
    note: str = ""
    """The control decision and why, or what the data op's semantics did."""
    unknown: bool = False
    """A word the stepping does not cover, recorded (lenient mode): a data op without
    semantics, or a word in which the datapath lapsed (`emu.Datapath.lapse`)."""
    confidence: Confidence | None = None

    def __str__(self) -> str:
        args = ""
        if self.operands is not None:
            a = self.operands
            parts = [f"r{a.ra}"] + ([f"r{a.rb}"] if a.rb is not None else [])
            parts += [f"#{a.imm:#x}"] if a.imm is not None else []
            args = " " + ", ".join(parts)
        if self.target is not None:
            args += f" -> {self.target}"
        text = f"{self.index:5}{'|' if self.slot else ' '} {op(self.op).name}{args}"
        if self.note:
            text += f": {self.note}"
        if self.confidence is not None:
            text += f" [{self.confidence.name.lower()}]"
        return text


@dataclass
class Flow:
    steps: list[Step]
    stop: Stop
    why: str = ""

    def order(self) -> list[int]:
        return [s.index for s in self.steps]

    def runs(self) -> str:
        """The executed indices as runs of consecutive words, delay slots after a `|`."""
        out: list[str] = []
        start = prev = None
        for s in self.steps:
            if prev is not None and s.index == prev.index + 1 and s.slot == prev.slot:
                prev = s
                continue
            if start is not None:
                out.append(_run(start, prev))
            start = prev = s
        if start is not None:
            out.append(_run(start, prev))
        return ", ".join(out)


def _run(a: Step, b: Step) -> str:
    text = str(a.index) if a.index == b.index else f"{a.index}..{b.index}"
    return f"|{text}" if a.slot else text
