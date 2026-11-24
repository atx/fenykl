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
from .fields import (
    DELAY_SLOTS,
    OP_FIELD,
    Confidence,
    Kind,
    Operands,
    lookup,
    op,
)
from .program import Program
from .semantics import (
    LOCAL_BYTES,
    OP_SEMANTICS,
    OTHER_FILES,
    Branch,
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


class Datapath(Protocol):
    """What runs a word's units under `Executor.step` (`machine.Machine`): `execute` runs word
    `word` at program index `index` as executed word number `t` and returns a note; as a
    `Branching` it decides the conditional control ops, before the control word's own units
    run."""

    def execute(self, index: int, word: Word, t: int) -> str: ...

    def link(self, word: Word) -> tuple[int | None, Confidence]:
        """The word an op 4 jumps to, G[f128_132], with its tier (None: not known)."""
        ...

    def decide(
        self,
        index: int,
        visit: int,
        word: Word,
        slot: RegisterFile,
        transfer: RegisterFile,
    ) -> Branch: ...


HALT_TAIL = 3
"""The words behind a halt that still run: of five stores there the first three wrote (verified
on the camera in two runs, the second pre-registered)."""


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
    follows the control ops (`fields.OPS`): a call at i goes to T and returns to
    i + 5, a return goes back there (the plain stepper's reading of the stock convention,
    inferred), brf / br / a loop end go to T when `branching` takes them;
    every control op has four delay slots that run before its transfer takes effect, and op 7
    halts at any word and call depth, the return stack unchecked; on a `datapath` the three
    words behind a halt still run (`HALT_TAIL`). `branching` decides ops 0..2; the default `Given()`
    decides none of them. A control op in another's delay slots jumps after its own four, and
    so does a loop end whose T lies in its own slots (verified on the camera). On the camera
    op 3 is a jump that writes no link and op 4 jumps to the word G[f128_132] names, r1 by
    the stock convention: a call's delay slots hold 0x0b #(i + 5) on r1 (verified on the
    camera: op 4 through r1 and through r21, op 3 with and without a link word). With a
    `datapath` the stepper takes op 4's target from it; without one, or while that register
    is unknown, it keeps a return stack of its own: a local op 3 (`Program.local_op3`) runs
    as a call, or with `local_op3_jump` as a jump to T after its delay slots, pushing no
    link. A step past the
    program end raises `UnknownSemantics`: what it does is open. A return without a link, or
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
        datapath: "Datapath | None" = None,
    ) -> None:
        self.boot, self.model, self.strict, self.limit = boot, model, strict, limit
        self.local_op3_jump = local_op3_jump
        self.sequencer = sequencer or Sequencer()
        self.datapath = datapath
        self.branching = branching or datapath or Given()
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
        pc, slots = 0, 0
        # [delay slots left, target] of each control op taken
        jumps: list[list[int]] = []
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
            slot = slots > 0
            slots = max(slots - 1, 0)
            if word.op == HALT_OP:
                steps.append(Step(pc, word.op, slot, confidence=op(HALT_OP).confidence))
                self._behind_halt(words, pc + 1, steps, jumps)
                return Flow(steps, Stop.HALT)
            target = None
            if not word.control:
                if self.datapath is None:
                    steps.append(self._data(pc, word, slot, len(steps)))
                else:
                    note = self.datapath.execute(pc, word, len(steps))
                    steps.append(Step(pc, word.op, slot, word.operands, note=note))
            else:
                visit = visits.get(pc, 0)
                visits[pc] = visit + 1
                if pc in local and self.datapath is None:
                    step, target = self._local_op3(pc, word, stack)
                else:
                    step, target = self._control(pc, visit, word, stack)
                if self.datapath is not None:
                    self.datapath.execute(pc, word, len(steps))
                steps.append(step)
                if step.taken is None:
                    if self.strict:
                        raise UnknownSemantics(pc, op(word.op).name, [], step.note)
                    return Flow(steps, Stop.UNDECIDED, f"word {pc}: {step.note}")
                slots = DELAY_SLOTS
            # Every taken control op jumps after its own four delay slots, also when it stands
            # in another's (verified on the camera: a br in a br's first slot, a loop end whose
            # target is one of its own slots).
            pc += 1
            for jump in jumps:
                jump[0] -= 1
                if jump[0] == 0:
                    pc = jump[1]
            jumps = [j for j in jumps if j[0]]
            if target is not None:
                jumps.append([DELAY_SLOTS, target])
        return Flow(steps, Stop.STEP_LIMIT, f"{self.limit} steps")

    def _behind_halt(
        self,
        words: tuple[Word, ...],
        at: int,
        steps: list[Step],
        jumps: list[list[int]],
    ) -> None:
        """The three words the sequencer fetches behind a halt still run (`HALT_TAIL`), on a
        `datapath` only: plain stepping ends at the halt. The job ends after them. A taken jump
        due among them redirects the fetch: with the halt in slot 2 or 3 of a taken op 4 the
        tail is the remaining slots, then the target's words (verified on the camera; slots 2
        to 4 of an op 3 fitted, slot 4 putting all three tail words at the target); with the
        halt in slot 1 the jump is due after the tail and not taken (verified). An op 3 or op 4
        as the second tail word is not taken and a halt as the first changes nothing
        (verified); a halt as the third, the target's own (fitted), changes nothing. Other
        control words or halts in the tail raise; words past the program's end are not run."""
        if self.datapath is None:
            return
        pending = [list(jump) for jump in jumps]

        def fetched(nxt: int) -> int:
            """The next word's index after one word, a pending jump's target when it is due."""
            for jump in pending:
                jump[0] -= 1
                if jump[0] == 0:
                    nxt = jump[1]
            return nxt

        i = fetched(at)  # the halt's own word counts toward a pending jump
        for n in range(HALT_TAIL):
            if i >= len(words):
                return
            word = words[i]
            passive = (word.control and word.op in (3, 4) and n == 1) or (
                word.op == HALT_OP and n != 1
            )
            if (word.control or word.op == HALT_OP) and not passive:
                raise UnknownSemantics(
                    i, op(word.op).name, [], "a control op behind a halt"
                )
            if passive:
                if word.op != HALT_OP:
                    self.datapath.execute(i, word, len(steps))
                note = "behind a halt: not taken"
            else:
                note = self.datapath.execute(i, word, len(steps))
            steps.append(Step(i, word.op, False, word.operands, note=note))
            i = fetched(i + 1)

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
        if word.op == 3 and self.datapath is not None:
            return Step(
                i, word.op, target=t, taken=True, note="jump", confidence=conf
            ), t
        if word.op == 4 and self.datapath is not None:
            to, tier = self.datapath.link(word)
            reg = f"r{word['f128_132']}"
            if to is None:
                return Step(
                    i, word.op, note=f"{reg} undecided: its value is unknown"
                ), None
            return Step(
                i, word.op, taken=True, note=f"to {to}, {reg}", confidence=tier
            ), to
        # Without a datapath the stepper infers the link from the stock convention.
        conf = min(conf, Confidence.INFERRED)
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
        self._route(i, word, None, now)
        if args is None:
            why += ", its ports' registers unknown after it"
        else:
            self.boot.slot[args.ra] = None
            why += f", r{args.ra} and its ports' registers unknown after it"
        return Step(i, word.op, slot, args, note=why, unknown=True)


MAIN_CODE = 0x60021020
"""Where the main code runs: word 67 at r21 reads its bytes at 0x606CC000 (offset 0x6AAFE0)."""


def back_firmware(boot: Boot, image: bytes) -> int:
    """Back the DRAM view with the main code of the stock `.BIN` `image`; returns its source."""
    firmware = unpack_firmware(image)
    body = firmware.blocks[firmware.find_block("main-code-01")].body
    return boot.back(MAIN_CODE, body, "firmware main code")
