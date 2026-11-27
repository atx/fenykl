"""What the stock models run on: word classes, parts, roles and the hidden sequencer; the boot
state the models keep (`StockBoot`) and `run_model`.

A word class is matched by field values (`match`) and names the
fields it reads or ignores (`operands`); every field a class neither matches nor names as an
operand must be idle. A program's model covers a job's word at its index: by a field semantic
(program 16's register transfers), by the row of the stock word there (`OpaqueClass`), or by its
window's coverage rule (program 13). A word nothing covers raises `UnknownSemantics`, naming the
fields that differ from what covers the stock word there (strict mode); in the lenient mode the
differing fields are ignored and the job is tainted "hypothesis".

"Undecoded" means "no registered semantics", not "below verified": semantics registered at
hypothesis confidence run and taint what they touch.

`run_model` runs a job of a stock program by its model: the words run in order; the model
covers each at its index, which records what it contributes to the job's `Trace`. At the end the
model of the program (`copy.Copy16Model`, `resample.Resample13Model`) applies the trace: it reads
the command's request from local memory, resolves the job and writes its result to the boot's
state.

Whatever a model cannot tell becomes UNKNOWN, with a source that says why; a later job that
reads it gets UNKNOWN bytes, never guessed ones.

The confidence rule. Every semantic carries a confidence: each word class
(`WordClass.confidence`), each part's outcomes (`Part.seen`), each role's table and use
(`Role.confidence`, `seen`, `beyond`), each register fact. What was seen on the camera exactly
is verified; what a model extends from it is weaker:

- positions: a semantic at its stock position is at most inferred, at any other position a
  hypothesis (an undecoded stock word is a row at its stock index only); a transfer whose role
  the sequencer assigns away from the role's stock word is a hypothesis; the idle word is
  verified anywhere; stock words that inserted words move keep their rows at hypothesis, and
  an inserted data op has its op semantics' confidence;
- outcome tables: what a role's table, a part's swept register or an idled part gives is
  verified at the job lengths it ran at (`seen`) and inferred at others;
- uses: a role's use of its register's value (address, offset, count, format) is inferred, at
  its stock register too, and never stronger than the register's fact;
- combinations: a job that differs from the stock program in two or more words is at most
  inferred, but for the field values of program 13's loop-head window that ran together
  (`resample_window.COMPOSED`);
- lenient mode: an ignored field, or a route nibble off the stock transfers' values, makes the
  job a hypothesis, its loaded, stored and zero-filled bytes alike.

Per output byte a model takes the minimum: the loaded bytes get the weakest of the semantics
that ran, the parts' outcomes, the caps above and the load's roles; the stored bytes the weakest
of the buffer bytes they come from and the same with the store's roles. No word of program 16 is
decoded, so every byte it copies is at most inferred.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..emu import Boot
from ..fields import Confidence, Field, lookup
from ..job import Job
from ..program import Program
from ..semantics import UnknownSemantics
from ..state import Space
from ..word import Word
from .registers import BootParams, Reg, RegisterFile

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


class Outcome(Enum):
    """What a word, a missing word or a role at a register does to the job."""

    OK = "copies"
    NO_STORE = "dst not written"
    NO_LOAD = "no load: the store emits the held buffer"
    TIMEOUT = "the job never completes (timeout-abort)"
    STORE_TIMEOUT = "dst is written, then the job never completes (timeout-abort)"
    HAZARD = "the job wedges or the camera loses its link"
    ZERO_FILL = "the store writes 0x4000 zero bytes from dst"
    DATA_UNKNOWN = "dst is written with data the model does not know"


KIB = 0x400


@dataclass(frozen=True)
class Transfer:
    """A register transfer as the sequencer sees it."""

    index: int
    reg: int
    taint: Confidence
    """Weaker than verified when the word moves fields the transfer is assumed to ignore."""


@dataclass
class Trace:
    """What the words of one job leave for the resolution at its end."""

    parts: Counter["OpaqueClass"] = field(default_factory=Counter)
    """How often each opaque class ran."""
    operands: dict["OpaqueClass", tuple[int, dict[str, int]]] = field(
        default_factory=dict
    )
    """(index, {field id: value}) of the opaque classes with operand fields."""
    channels: dict[str, list[Transfer]] = field(default_factory=dict)
    standins: set[int] = field(default_factory=set)
    """Indices where lenient mode ran the stock transfer for a word nothing covers."""
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    classes: Confidence = V
    """The weakest confidence of the classes that ran, at the positions they ran at."""
    taint: Confidence = V
    """Hypothesis when a field was ignored (lenient mode) or a word ran off its stock position."""
    inserted: list[int] = field(default_factory=list)
    """The job indices of the words inserted into the stock program (`copy.align`)."""
    writes: dict[int, tuple[Reg | None, int]] = field(default_factory=dict)
    """The xreg-set registers that inserted words wrote: (value, the writer's job index)
    (`copy`'s engine site)."""
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, eq=False)
class WordClass:
    """A class of words; compared by identity.

    `match` holds field values the word must have. `confidence` is that of the class's
    semantics where it was seen.
    """

    name: str
    fact: str
    match: Mapping[str, int]
    operands: frozenset[str]
    confidence: Confidence = H
    mask: int = field(init=False, repr=False)
    bits: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        mask = bits = 0
        for key, value in self.match.items():
            f = lookup(key)
            mask, bits = f.put(mask, f.mask), f.put(bits, value & f.mask)
        object.__setattr__(self, "mask", mask)
        object.__setattr__(self, "bits", bits)

    def matches(self, word: Word) -> bool:
        return word.value & self.mask == self.bits

    def mismatches(self, word: Word) -> list[tuple[Field, int]]:
        return [
            (lookup(key), word[key])
            for key, value in self.match.items()
            if word[key] != value & lookup(key).mask
        ]

    def act(self, trace: Trace, word: Word, index: int, strict: bool) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class Part:
    """An undecoded stock word as a part of the job: what the job does without it."""

    token: str
    """What the word is for, as far as known ("store path", "completion", ...)."""
    missing: Outcome
    fact: str
    operand: Mapping[int, Outcome] | None = None
    """Outcomes by the value of its field `operand_field`, where that field was swept."""
    operand_field: str = "xreg"
    flips: Mapping[str, Mapping[int, Outcome]] = field(default_factory=dict)
    """Outcomes of edits of one field, by field and by the bits flipped against the stock
    value (so that no stock value is part of the code)."""
    seen: tuple[int, ...] = (KIB,)
    """The job lengths at which `missing` and `operand` were seen on the camera [verified];
    at other lengths they are inferred."""

    def at(self, length: int) -> Confidence:
        return V if length in self.seen else I


@dataclass(frozen=True, eq=False)
class OpaqueClass(WordClass):
    """Undecoded stock words, the row of each of their stock indices: a job's word there that
    matches it runs as its `part`."""

    part: Part | None = None
    positions: frozenset[int] = frozenset()
    """The indices of the stock words of this class."""

    def act(self, trace: Trace, word: Word, index: int, strict: bool) -> None:
        trace.parts[self] += 1
        if self.operands:
            trace.operands[self] = (index, {k: word[k] for k in self.operands})


# -- roles of the register transfers


class Use(Enum):
    """How a role uses the value of its register."""

    NONE = "only the outcome table is known"
    ADDRESS = "the load address"
    LOAD_OFFSET = "the load's buffer index, value / 32 truncated"
    COUNT = "the store's count in bytes"
    STORE_OFFSET = "the store's buffer index, value / 32 rounded down"
    FORMAT = "the store's element format"


@dataclass(frozen=True)
class Role:
    name: str
    word: int
    """The stock word that holds the role."""
    fact: str
    table: Mapping[int, Outcome] = field(default_factory=dict)
    """Outcome by register, seen on the camera at the `seen` lengths [verified], inferred at
    the others; checked before `use`."""
    use: Use = Use.NONE
    load: bool = False
    """A role of the load (its taint goes to the loaded bytes), else of the store."""
    confidence: Confidence = I
    """That of `use`: at the stock register, and at the others at the `seen` lengths."""
    seen: tuple[int, ...] = (KIB,)
    """The job lengths at which the table and the other registers ran on the camera."""
    beyond: Confidence = I
    """That of `use` at another register and length."""

    def table_at(self, length: int) -> Confidence:
        return V if length in self.seen else I

    def use_at(self, stock: bool, length: int) -> Confidence:
        if stock or length in self.seen:
            return self.confidence
        return min(self.confidence, self.beyond)


@dataclass(frozen=True)
class Channel:
    """A group of transfers sharing a register context, and their roles in slot order."""

    name: str
    anchor: int
    """A stock word whose register context names the channel."""
    roles: tuple[Role, ...]
    needed: bool = True
    """False when idling its transfers has no effect: its unset slots do not stall the job."""
    stalls: tuple[int, ...] = ()
    """Word indices at which a counted transfer of the channel stalls the job (inferred)."""


def assign_roles(
    channels: Sequence[Channel], transfers: dict[str, list[Transfer]]
) -> tuple[list[tuple[Role, Transfer, Confidence]], list[tuple[str, Confidence]]]:
    """The hidden sequencer's assignment of the register transfers to their roles: (role,
    transfer, assignment taint) per filled slot, and why the job stalls, with the confidence:
    each unset slot (hypothesis) and each counted transfer at a stall index (inferred).

    Each channel has a slot pointer; a transfer with X1 head 0x1c takes the slot at the pointer
    and advances it, so roles follow the order of the transfers (inferred). Idling a transfer
    leaves its channel's last slot unset, and an unset slot stalls the job; this reproduces the
    camera: every idled counted transfer of 42..49 and 66..77 times out, while 74 and 78 (head
    0x1d) have no effect. The pointers start at slot 0 with every job. On the camera the roles
    followed the counted transfers when they moved: the count one index later (exchanged with
    the holding word 74, also at another register), and 75..77 one index earlier as a block,
    with a holding word after them (one run each, verified on the camera); a role fixed to its
    stock word index does not explain these runs. A counted transfer at a channel's `stalls`
    index stalls the job: program 16's word 78, the word before the return 79, where exchanging
    77 with the holding word 78 times the job out (verified on the camera, two boots). Why 78
    fails is open: too late for the stream, a restriction of that index alone, or a window per
    role that lets a role move one index early and the count one late but not 77 one late
    (hypothesis, not separated by the runs so far). A transfer off its stock index is still
    assigned at hypothesis taint.

    A channel that stalls has its filled slots left out: what the shifted transfers would do
    there is not modelled.
    """
    by_name = {c.name: c for c in channels}
    by_channel: dict[str, list[tuple[Role, Transfer, Confidence]]] = {
        c.name: [] for c in channels
    }
    for name, slots in transfers.items():
        roles = by_name[name].roles
        if len(slots) > len(roles):
            extra = ", ".join(str(t.index) for t in slots[len(roles) :])
            raise UnknownSemantics(
                slots[len(roles)].index,
                "transfer",
                [],
                f"{name}: no slot for words {extra}",
            )
        for role, t in zip(roles, slots):
            by_channel[name].append((role, t, V if t.index == role.word else H))
    filled, stalls = [], []
    for c in channels:
        have = {role.word for role, _, _ in by_channel[c.name]}
        why = [
            (f"unset {c.name} slot {k} ({role.name}, stock word {role.word})", H)
            for k, role in enumerate(c.roles)
            if role.word not in have
        ]
        if not c.needed:
            why = []
        why += [
            (f"{c.name}: a counted transfer at word {t.index}", I)
            for t in transfers.get(c.name, [])
            if t.index in c.stalls
        ]
        stalls += why
        if not why:
            filled += by_channel[c.name]
    return filled, stalls


# -- the boot state of the models and running a job by its model

BUFFER_LO, BUFFER_HI = -0x1000, 0x10000
"""The modelled buffer indices, reaching below index 0. The buffer is the lane memory
(`fields.LANE_MEMORY`), one source byte per element, so it holds 0x8000 bytes and its indices
count modulo that (inferred); the modelled range exceeds 0x8000 because the model does not wrap
them."""
LINE_PAIRS = 3 * 2 * 64
"""The resampler's held line pairs: plane c, line parity r, pixel x at (2 c + r) x 64 + x."""


class Status(Enum):
    OK = "ok"
    TIMEOUT = "timeout-abort (0xA111)"
    HAZARD = "wedged or link lost"


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


class StockBoot(Boot):
    """A boot with the state the stock models keep across its jobs."""

    def __init__(self, params: BootParams | None = None) -> None:
        super().__init__()
        self.params = params or BootParams()
        self.regs = RegisterFile()
        """The transfer registers as the copy model's register facts give them
        (`copy.start_copy`)."""
        never = self.source("unwritten", "no job on this boot wrote it")
        self.buffer = Space("buffer", BUFFER_LO, BUFFER_HI - BUFFER_LO, never)
        self.lines = Space("line pairs", 0, LINE_PAIRS, never)
        self.substitute: int | None = None
        """The resampler's tap-0 substitute, a per-lane value that program 13 adds in place of tap
        0's product when word 439's grid A is idled; None while it is not known: its value at boot
        varies per boot and edits move it (`resample_edits.SUBSTITUTE_MOVER`)."""
        self.substitute_at_boot = True
        """Whether the substitute still holds its boot value: no edit has moved it."""

    def forget_held(self, why: int) -> None:
        """What jobs hold across the boot, the held line pairs and the tap-0 substitute, becomes
        unknown (source `why`): after a job that raised `UnknownSemantics`, whose effect on them
        is not known."""
        self.lines.forget(why)
        self.substitute, self.substitute_at_boot = None, False


@dataclass(frozen=True)
class ModelRun:
    """What a model runs a job with (`run_model`)."""

    boot: StockBoot
    strict: bool
    """False: fields without semantics are ignored, and taint."""


class Model(Protocol):
    """The model of one program: what covers each word of a job (`cover_job`, which records
    into the job's trace, or raises `UnknownSemantics` in strict mode, and returns the job as
    `apply` takes it), and what the trace does."""

    program: Program

    def cover_job(self, run: ModelRun, job: Job, trace: Trace) -> Job: ...

    def apply(self, run: ModelRun, job: Job, trace: Trace) -> JobResult: ...


def run_model(
    boot: StockBoot,
    model: Model,
    job: Job,
    *,
    strict: bool = True,
) -> JobResult:
    """Run `job` on `boot` by `model`: the job's setup, the model's cover of its words, then the
    model applies the trace to the boot."""
    boot.setup(job)
    run = ModelRun(boot, strict)
    trace = Trace()
    return model.apply(run, model.cover_job(run, job, trace), trace)
