"""The word-level emulator: a `Job` (a program and its request in local memory) run on a `Boot`.

The words run in order; the model covers each at its index (the stock word's row, a field
semantic such as X1's register transfer, or the coverage rule of a window), which records what it
contributes to the job's `Trace`. At the end the model of the program
(`memcpy.Copy16Model`, `resample.Resample13Model`) applies the trace: it reads the command's
request from local memory, resolves the job and writes its result to the boot's state.

Whatever a model cannot tell becomes UNKNOWN, with a source that says why; a later job that
reads it gets UNKNOWN bytes, never guessed ones.

`Executor.step` steps a job instead: a program counter that follows the control ops (calls,
returns, branches and loop ends with their delay slots, the halt) and runs the data ops by the
op slot's semantics (`semantics.OP_SEMANTICS`); its `Flow` lists every executed word in order.

The emulator does not model time. On the camera a word costs about 62 ns the first time it runs
(straight-line words; a branch over 1200 idle words cost nothing, running them about 62 ns
each, and the call times of 58 straight-line jobs fit the same per word) and about 5 ns when it
repeats in a loop (0.185 us per 36-word pass, idle and compute words alike; per word 2..7 ns
over three loop sizes, a per-pass cost not excluded) (measured on the camera; inferred).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..firmware import unpack_firmware
from .fields import DELAY_SLOTS, OP_FIELD, Confidence, Kind, Operands, lookup, op
from .program import Program
from .semantics import (
    LOCAL_BYTES,
    OP_SEMANTICS,
    OTHER_FILES,
    Branching,
    Given,
    OpSemantics,
    RegSet,
    Sequencer,
    Trace,
    UnknownSemantics,
    destinations,
)
from .state import (
    BootParams,
    Bytes,
    JobView,
    Reg,
    RegisterFile,
    Source,
    Space,
    SparseSpace,
)
from .word import Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

LOCAL_BASE = 0x9D000000
LOCAL_SIZE = LOCAL_BYTES
"""Local memory, 8 KiB, which the scalar addresses wrap in (`semantics.LOCAL_BYTES`)."""
BUFFER_LO, BUFFER_HI = -0x1000, 0x10000
"""The modelled buffer indices, reaching below index 0. The buffer is the lane memory
(`fields.LANE_MEMORY`), one source byte per element, so it holds 0x8000 bytes and its indices
count modulo that (inferred); the modelled range exceeds 0x8000 because the model does not wrap
them."""
LINE_PAIRS = 3 * 2 * 64
"""The resampler's held line pairs: plane c, line parity r, pixel x at (2 c + r) x 64 + x."""


def dram_alias(addr: int) -> int:
    """0x6xxxxxxx (cached) and 0xAxxxxxxx (uncached) are the same DRAM."""
    return addr & 0x3FFFFFFF


class Status(Enum):
    OK = "ok"
    TIMEOUT = "timeout-abort (0xA111)"
    HAZARD = "wedged or link lost"


@dataclass(frozen=True)
class Job:
    """A program and its request: (offset, bytes) writes to DSP local memory."""

    program: Program
    local: tuple[tuple[int, bytes], ...]


@dataclass
class JobResult:
    status: Status
    why: list[str] = field(default_factory=list)
    """The outcomes that decided the status and the store."""
    stored: tuple[int, int] | None = None
    """The dst range the store wrote, None when it wrote nothing."""
    taint: Confidence = V
    """The confidence of what this job did: its status, whether it stored and the store itself;
    stored bytes can be weaker still through the buffer they come from."""
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    edits: list[tuple[str, Confidence]] = field(default_factory=list)
    """The measured edits of the stock program the job ran, each with the confidence of its
    outcome on this job."""


class Boot:
    """The state of one camera boot; it persists across the jobs run on it."""

    def __init__(self, params: BootParams | None = None) -> None:
        self.params = params or BootParams()
        self.sources: list[Source] = []
        self._source_ids: dict[Source, int] = {}
        self.regs = RegisterFile()
        self.slot = RegisterFile()
        """The op slot's registers, apart from the transfer registers `regs`."""
        never = self.source("unwritten", "no job on this boot wrote it")
        self.local = Space("local", LOCAL_BASE, LOCAL_SIZE, never)
        self.buffer = Space("buffer", BUFFER_LO, BUFFER_HI - BUFFER_LO, never)
        self.lines = Space("line pairs", 0, LINE_PAIRS, never)
        self.substitute: int | None = None
        """The resampler's tap-0 substitute, a per-lane value that program 13 adds in place of tap
        0's product when word 439's grid A is idled; None while it is not known: its value at boot
        varies per boot and edits move it (`resample_edits.SUBSTITUTE_MOVER`)."""
        self.substitute_at_boot = True
        """Whether the substitute still holds its boot value: no edit has moved it."""
        self.dram = SparseSpace("dram", self.source("dram", "not in the DRAM view"))

    def source(self, name: str, fact: str = "") -> int:
        """The id of source (name, fact). Equal sources share one id, so the list stays bounded
        by the distinct sources; give puts distinct names to tell them apart."""
        s = Source(name, fact)
        if s not in self._source_ids:
            self._source_ids[s] = len(self.sources)
            self.sources.append(s)
        return self._source_ids[s]

    def put(self, addr: int, data: bytes, name: str, taint: Confidence = V) -> int:
        """Known DRAM bytes (a src, a prefill); returns their source."""
        src = self.source(name)
        self.dram.write(dram_alias(addr), Bytes.of(data, src, taint))
        return src

    def back(self, addr: int, data: bytes, name: str) -> int:
        """A read-only DRAM image under `addr` (e.g. firmware text), read only where touched."""
        src = self.source(name)
        self.dram.back(dram_alias(addr), data, src)
        return src

    def read(self, addr: int, n: int) -> Bytes:
        return self.dram.read(dram_alias(addr), n)

    def u32(self, offset: int) -> int | None:
        b = self.local.read(LOCAL_BASE + offset, 4)
        return int.from_bytes(b.data, "little") if all(b.known) else None

    def forget_held(self, why: int) -> None:
        """What jobs hold across the boot, the held line pairs and the tap-0 substitute, becomes
        unknown (source `why`): after a job that raised `UnknownSemantics`, whose effect on them
        is not known."""
        self.lines.forget(why)
        self.substitute, self.substitute_at_boot = None, False

    def setup(self, job: Job) -> None:
        """Write the job's request to local memory, as its command's CPU setup does."""
        request = self.source("request", "written by the command's CPU setup")
        for offset, data in job.local:
            self.local.write(LOCAL_BASE + offset, Bytes.of(data, request))


def start_copy(boot: Boot) -> JobView:
    """Set the transfer registers that the copy's register facts give for the request in local
    memory (src, dst and len at words 0..2); returns that view of the job."""
    view = JobView(boot.u32(0), boot.u32(4), boot.u32(8), boot.params)
    if view.dst is None or view.length is None:
        raise ValueError("local memory holds no dst and len (no command setup ran)")
    boot.regs.start_job(view)
    return view


class Model(Protocol):
    """The model of one program: what covers each word of a job (`cover_job`, which records
    into the job's trace, or raises `UnknownSemantics` in strict mode, and returns the job as
    `apply` takes it), and what the trace does."""

    program: Program

    def cover_job(self, emu: "Executor", job: Job, trace: Trace) -> Job: ...

    def apply(self, emu: "Executor", job: Job, trace: Trace) -> JobResult: ...


class Stop(Enum):
    HALT = "halt"
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
    """A data op without semantics, recorded (lenient mode)."""
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


HALT_OP = 7


class LinkError(UnknownSemantics):
    """A return that pops no link of its own call: none at all, or one a local op 3 pushed."""


@dataclass(frozen=True)
class Link:
    back: int
    local: bool = False
    """Pushed by a local op 3 (`Program.local_op3`), which has no return of its own."""


class Executor:
    """Runs jobs on `boot`.

    `run` runs a job of `model`'s program and has the model apply its trace. `step` steps a
    job's program through its control flow from word 0, with no model: a program counter that
    follows the control ops (`fields.OPS`, inferred): a call at i goes to T and returns to
    i + 5, a return goes back there, brf / br / a loop end go to T when `branching` takes them;
    every control op has four delay slots that run before its transfer takes effect, and op 7
    halts at once at any word and call depth, the return stack unchecked (whether a halt has
    delay slots of its own is open). `branching` decides ops 0..2; the default `Given()`
    decides none of them. A local op 3 (`Program.local_op3`) runs as a call, or with
    `local_op3_jump` as a jump to T after its delay slots, pushing no link. A loop end whose T
    lies in its own delay slots, a control op in another's delay slots and a step past the
    program end raise `UnknownSemantics`: what they do is open. A return without a link, or
    one that pops a local op 3's link, raises `LinkError`: no return lies between that op 3 and
    its T, so the return is not its own (stepped as calls, a stock loop re-ran its tail).
    A data op runs as the op slot's semantics `ops`, its operands read from the operand set
    (`Boot.slot`); a result goes where the word's PR ports send it (`semantics.destinations`),
    landing `latency` executed words later and unknown until then. A result whose destination
    is open raises `UnknownSemantics` (strict) or makes the open ports' registers unknown in both
    sets. An op without semantics raises (strict) or is recorded in its step, and then its ra
    and its ports' registers are unknown, since the op may write them. Strict mode also raises
    at an undecided control op; lenient mode stops there. Stepping does not model the register
    transfers or any datapath unit.

    strict=False ignores fields without semantics (and taints).
    """

    def __init__(
        self,
        boot: Boot,
        model: "Model | None" = None,
        strict: bool = True,
        sequencer: Sequencer | None = None,
        *,
        branching: Branching | None = None,
        ops: dict[int, OpSemantics] | None = None,
        limit: int = 100_000,
        local_op3_jump: bool = False,
    ) -> None:
        self.boot, self.model, self.strict, self.limit = boot, model, strict, limit
        self.local_op3_jump = local_op3_jump
        self.sequencer = sequencer or Sequencer()
        self.branching = branching or Given()
        self.ops = OP_SEMANTICS if ops is None else ops
        self._landing: list[tuple[int, RegisterFile, int, Reg | None]] = []
        """Results in flight: (step count they land at, set, register, value)."""

    def run(self, job: Job) -> JobResult:
        if self.model is None:
            raise ValueError("run needs a model; step runs a job without one")
        self.boot.setup(job)
        trace = Trace()
        job = self.model.cover_job(self, job, trace)
        return self.model.apply(self, job, trace)

    def step(self, job: Job, start: Callable[[Boot], object] | None = None) -> Flow:
        """Step `job` from word 0 after its setup and `start`, which sets the registers the
        job starts with (`start_copy`)."""
        self.boot.setup(job)
        if start is not None:
            start(self.boot)
        words = job.program.words
        local = job.program.local_op3()
        steps: list[Step] = []
        stack: list[Link] = []
        visits: dict[int, int] = {}
        pc, slots, pending = 0, 0, None
        self._landing = []
        for _ in range(self.limit):
            self._land(len(steps))
            if pc >= len(words):
                # On the camera the stock copy with its return word idled ran on and the job
                # timed out; the stepper does not model what runs past the end, so it raises.
                raise UnknownSemantics(
                    pc, "end", [], f"past the program end ({len(words)} words)"
                )
            word = words[pc]
            if word.op == HALT_OP:
                steps.append(
                    Step(pc, word.op, slots > 0, confidence=op(HALT_OP).confidence)
                )
                return Flow(steps, Stop.HALT)
            if not word.control:
                steps.append(self._data(pc, word, slots > 0, len(steps)))
                if slots:
                    slots -= 1
                    if slots == 0 and pending is not None:
                        pc, pending = pending, None
                        continue
                pc += 1
                continue
            if slots:
                raise UnknownSemantics(
                    pc, op(word.op).name, [], "a control op in a delay slot"
                )
            visit = visits.get(pc, 0)
            visits[pc] = visit + 1
            if pc in local:
                step, pending = self._local_op3(pc, word, stack)
            else:
                step, pending = self._control(pc, visit, word, stack)
            steps.append(step)
            if step.taken is None:
                if self.strict:
                    raise UnknownSemantics(pc, op(word.op).name, [], step.note)
                return Flow(steps, Stop.UNDECIDED, f"word {pc}: {step.note}")
            slots = DELAY_SLOTS
            pc += 1
        return Flow(steps, Stop.STEP_LIMIT, f"{self.limit} steps")

    def _local_op3(self, i: int, word: Word, stack: list[Link]) -> tuple[Step, int]:
        t, conf = word.target, op(word.op).confidence
        if self.local_op3_jump:
            note = "local op 3 as a jump, no link"
        else:
            back = i + DELAY_SLOTS + 1
            stack.append(Link(back, local=True))
            note = f"local op 3 as a call, returns to {back}"
        return Step(i, word.op, target=t, taken=True, note=note, confidence=conf), t

    def _control(
        self, i: int, visit: int, word: Word, stack: list[Link]
    ) -> tuple[Step, int | None]:
        """The step of control word i and where it transfers after its delay slots (None: it
        falls through or is undecided)."""
        name, conf = op(word.op).name, op(word.op).confidence
        t = word.target
        if word.op == 3:
            back = i + DELAY_SLOTS + 1
            stack.append(Link(back))
            return Step(
                i,
                word.op,
                target=t,
                taken=True,
                note=f"returns to {back}",
                confidence=conf,
            ), t
        if word.op == 4:
            if not stack:
                raise LinkError(i, name, [], "a return without a call")
            link = stack.pop()
            if link.local:
                raise LinkError(
                    i, name, [], f"a return to {link.back}, a local op 3's link"
                )
            back = link.back
            return Step(
                i, word.op, taken=True, note=f"to {back}", confidence=conf
            ), back
        if word.op == 0 and t is not None and i < t <= i + DELAY_SLOTS:
            raise UnknownSemantics(
                i, name, [], f"loop end to {t}, inside its own delay slots"
            )
        b = self.branching.decide(i, visit, word, self.boot.slot, self.boot.regs)
        ra = f"r{word['ra']}"
        if b.taken is None:
            return Step(i, word.op, target=t, note=f"{ra} undecided: {b.why}"), None
        what = "taken" if b.taken else "not taken"
        step = Step(
            i,
            word.op,
            target=t,
            taken=b.taken,
            note=f"{what}, {ra}: {b.why}",
            confidence=min(conf, b.confidence),
        )
        return step, t if b.taken else None

    def _land(self, now: int) -> None:
        for due, regs, r, value in [x for x in self._landing if x[0] <= now]:
            regs[r] = value
        self._landing = [x for x in self._landing if x[0] > now]

    def _sets(self) -> dict[RegSet, RegisterFile]:
        return {RegSet.XREG: self.boot.regs, RegSet.OPERAND: self.boot.slot}

    def _route(self, i: int, word: Word, result: Reg | None, now: int) -> str:
        """Send a data op's result through its word's PR ports; returns why a destination is
        open ("" when none is)."""
        dests, open_, why = destinations(word)
        if why and self.strict:
            raise UnknownSemantics(i, op(word.op).name, [], why)
        sets = self._sets()
        for to, r, latency in dests:
            sets[to][r] = None
            self._landing.append((now + latency, sets[to], r, result))
        for fid in open_:
            if lookup(fid).kind is Kind.REG and fid not in OTHER_FILES:
                for regs in sets.values():
                    regs[word[fid]] = None
        return why

    def _data(self, i: int, word: Word, slot: bool, now: int = 0) -> Step:
        args = word.operands
        sem = self.ops.get(word.op)
        effect = None if sem is None else sem.run(word, self.boot.slot, self.boot.local)
        if effect is not None:
            note = effect.note
            if effect.computes:
                why = self._route(i, word, effect.result, now)
                note = f"{note}; {why}" if why else note
            for addr, n in effect.stores:
                src = self.boot.source(
                    "local store", f"op {word.op:#x}: its datum is open"
                )
                if addr is None:
                    self.boot.local.forget(src)
                else:
                    self.boot.local.write(addr, Bytes.unknown(n, src))
            return Step(i, word.op, slot, args, note=note, confidence=sem.confidence)
        why = "no semantics" if sem is None else "outside what its semantics covers"
        if self.strict:
            raise UnknownSemantics(i, op(word.op).name, [(OP_FIELD, word.op)], why)
        if args is not None:
            self.boot.slot[args.ra] = None
            self._route(i, word, None, now)
            why += f", r{args.ra} and its ports' registers unknown after it"
        return Step(i, word.op, slot, args, note=why, unknown=True)


MAIN_CODE = 0x60021020
"""Where the main code runs: word 67 at r21 reads its bytes at 0x606CC000 (offset 0x6AAFE0)."""


def back_firmware(boot: Boot, image: bytes) -> int:
    """Back the DRAM view with the main code of the stock `.BIN` `image`; returns its source."""
    firmware = unpack_firmware(image)
    body = firmware.blocks[firmware.find_block("main-code-01")].body
    return boot.back(MAIN_CODE, body, "firmware main code")
