"""Semantics: word classes, parts, roles and the hidden sequencer, independent of one program.

A word class is matched by field values under per-field masks (`match`, `masks`) and names the
fields it reads or ignores (`operands`). A word gets the semantics of the first class it
matches. Every field a class neither matches nor names as an operand must be idle; a word that
matches no class raises `UnknownSemantics`, naming the fields that differ from the nearest class
(strict mode). In the lenient mode the word runs as its nearest class, the differing fields are
ignored and the job is tainted "hypothesis".

"Undecoded" means "no registered semantics", not "below verified": semantics registered at
hypothesis confidence run and taint what they touch.

The classes and tables of one program are held by its model (`memcpy.Copy16Model` for program
16, `resample.Resample13Model` for program 13), which the emulator takes.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .fields import FIELDS, Confidence, Field, lookup
from .word import Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


class UnknownSemantics(Exception):
    """A word sets a field (or a field value) that no registered semantics covers."""

    def __init__(
        self, index: int, nearest: str, fields: list[tuple[Field, int]], why: str = ""
    ) -> None:
        self.index, self.nearest, self.fields = index, nearest, fields
        names = ", ".join(f"{f.id}={v:#x}" for f, v in fields)
        text = f"word {index}: no semantics for {names} (nearest class: {nearest})"
        super().__init__(f"{text}; {why}" if why else text)


class Outcome(Enum):
    """What a word, a missing word or a role at a register does to the job."""

    OK = "copies"
    NO_STORE = "dst not written"
    NO_LOAD = "no load: the store emits the held buffer"
    TIMEOUT = "the job never completes (timeout-abort)"
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
    operands: dict["OpaqueClass", tuple[int, int]] = field(default_factory=dict)
    """(index, register) of the opaque classes whose register field matters."""
    channels: dict[str, list[Transfer]] = field(default_factory=dict)
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    classes: Confidence = V
    """The weakest confidence of the classes that ran, at the positions they ran at."""
    taint: Confidence = V
    """Hypothesis when a field was ignored (lenient mode) or a word ran off its stock position."""
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, eq=False)
class WordClass:
    """A class of words; compared by identity.

    `match` holds field values and `masks` the bits of each field that `match` fixes (default:
    all of them); the other bits of a masked field are don't-care. `confidence` is that of the
    class's semantics where it was seen (`confidence_at`).
    """

    name: str
    fact: str
    match: Mapping[str, int]
    operands: frozenset[str]
    masks: Mapping[str, int] = field(default_factory=dict)
    confidence: Confidence = H
    mask: int = field(init=False, repr=False)
    bits: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        mask = bits = 0
        for key, value in self.match.items():
            f = lookup(key)
            m = self.masks.get(key, f.mask)
            mask, bits = f.put(mask, m), f.put(bits, value & m)
        object.__setattr__(self, "mask", mask)
        object.__setattr__(self, "bits", bits)

    def matches(self, word: Word) -> bool:
        return word.value & self.mask == self.bits

    def mismatches(self, word: Word) -> list[tuple[Field, int]]:
        out = []
        for key, value in self.match.items():
            f = lookup(key)
            m = self.masks.get(key, f.mask)
            if word[key] & m != value & m:
                out.append((f, word[key]))
        return out

    def confidence_at(self, index: int) -> Confidence:
        return self.confidence

    def act(self, trace: Trace, word: Word, index: int, strict: bool) -> None:
        raise NotImplementedError


class IdleClass(WordClass):
    def act(self, trace: Trace, word: Word, index: int, strict: bool) -> None:
        pass


IDLE_CLASS = IdleClass(
    "idle",
    "the no-op of the stock programs (program 16 holds it at 3, 4, 6..9) [verified]",
    {f.id: f.idle for f in FIELDS},
    frozenset(),
    confidence=V,
)


@dataclass(frozen=True)
class Part:
    """An undecoded stock word as a part of the job: what the job does without it."""

    token: str
    """What the word is for, as far as known ("store path", "completion", ...)."""
    missing: Outcome
    fact: str
    operand: Mapping[int, Outcome] | None = None
    """Outcomes by the value of its register field 115..119, where that field was swept."""
    seen: tuple[int, ...] = (KIB,)
    """The job lengths at which `missing` and `operand` were seen on the camera [verified];
    at other lengths they are inferred."""

    def at(self, length: int) -> Confidence:
        return V if length in self.seen else I


@dataclass(frozen=True, eq=False)
class OpaqueClass(WordClass):
    """Undecoded stock words matched by value: they run as their `part`."""

    part: Part | None = None
    positions: frozenset[int] = frozenset()
    """The indices of the stock words of this class."""

    def confidence_at(self, index: int) -> Confidence:
        """Its confidence at its stock positions, hypothesis anywhere else."""
        return self.confidence if index in self.positions else H

    def act(self, trace: Trace, word: Word, index: int, strict: bool) -> None:
        trace.parts[self] += 1
        if self.operands:
            trace.operands[self] = (index, word["xreg"])


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


class Mode(Enum):
    POINTER = "pointer"
    POSITION = "position"


class Sequencer:
    """Hidden state that gives the register transfers their roles [hypothesis].

    POINTER (the default): each channel has a slot pointer; a transfer with bit 308 clear takes
    the slot at the pointer and advances it, so roles follow the order of the transfers. Idling
    a transfer leaves its channel's last slot unset, and an unset slot stalls the job; this
    reproduces the camera: every idled counted transfer of 42..49 and 66..77 times out, while
    74 and 78 (bit 308 set) have no effect. The pointers start at slot 0 with every job.

    POSITION (the alternative considered): the role is the stock word index's role; a transfer
    at another index has none. It gives the same results on every edit run so far (field sweeps
    and idled words), so the camera has not told the two apart; an inserted or moved transfer
    would.
    """

    def __init__(self, mode: Mode = Mode.POINTER) -> None:
        self.mode = mode

    def assign(
        self, channels: Sequence[Channel], transfers: dict[str, list[Transfer]]
    ) -> tuple[list[tuple[Role, Transfer, Confidence]], list[str]]:
        """(role, transfer, assignment taint) per filled slot, and the unset slots.

        A channel with an unset slot stalls the job, so its filled slots are left out: what the
        shifted transfers would do there is not modelled.
        """
        by_name = {c.name: c for c in channels}
        by_channel: dict[str, list[tuple[Role, Transfer, Confidence]]] = {
            c.name: [] for c in channels
        }
        if self.mode is Mode.POINTER:
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
        else:
            stock = {r.word: (c.name, r) for c in channels for r in c.roles}
            for slots in transfers.values():
                for t in slots:
                    if t.index not in stock:
                        raise UnknownSemantics(
                            t.index, "transfer", [], "no role at this index"
                        )
                    name, role = stock[t.index]
                    by_channel[name].append((role, t, V))
        filled, unset = [], []
        for c in channels:
            have = {role.word for role, _, _ in by_channel[c.name]}
            missing = [
                f"{c.name} slot {k} ({role.name}, stock word {role.word})"
                for k, role in enumerate(c.roles)
                if role.word not in have
            ]
            if not c.needed:
                missing = []
            unset += missing
            if not missing:
                filled += by_channel[c.name]
        return filled, unset


def classify(
    classes: Sequence[WordClass],
    word: Word,
    index: int,
    trace: Trace,
    strict: bool = True,
) -> WordClass:
    """The class of `word`; raises UnknownSemantics, or in lenient mode ignores the fields that
    differ from the nearest class (recorded in `trace`, which is tainted)."""
    for c in classes:
        if c.matches(word):
            _ran(trace, c, index)
            return c
    best, best_miss = None, None
    for c in classes:
        miss = c.mismatches(word)
        if best_miss is None or len(miss) < len(best_miss):
            best, best_miss = c, miss
    assert best is not None and best_miss is not None
    if strict:
        raise UnknownSemantics(index, best.name, best_miss)
    trace.ignored.append((index, [f.id for f, _ in best_miss]))
    trace.taint = H
    _ran(trace, best, index)
    return best


def _ran(trace: Trace, c: WordClass, index: int) -> None:
    conf = c.confidence_at(index)
    trace.classes = min(trace.classes, conf)
    if conf < c.confidence:
        trace.taint = H
        trace.notes.append(f"word {index}: {c.name} off its stock position")
