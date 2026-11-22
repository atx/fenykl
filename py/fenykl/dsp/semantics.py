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

The stepping core (`emu.Core`) takes the op semantics `OP_SEMANTICS` (what a data op does,
each with its confidence) and a `Branching` hypothesis for the conditions of ops 0..2: `Given`
decisions (the default, none: no condition is known) or `RaTest`.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .fields import FIELDS, IDLE_OP, Confidence, Field, lookup
from .state import Reg, RegisterFile, Space
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
    """Outcomes by the value of its field `operand_field`, where that field was swept."""
    operand_field: str = "xreg"
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
        if self.part is not None and self.part.operand is not None:
            trace.operands[self] = (index, word[self.part.operand_field])


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
    at another index has none. Both give the same results on the field sweeps and idled words.
    Moved transfers fit neither alone [verified on the camera, one run each]: exchanging the
    count transfer 73 with the holding word 74 moves the count role to 74, with 74's register
    (POINTER; POSITION predicts a timeout); exchanging 77 with the holding word 78 times the job
    out (POSITION; POINTER predicts a copy). POINTER, which misses only the 77 / 78 exchange,
    stays the default because nothing decides against it, at hypothesis taint wherever a
    transfer is off its stock index.
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


# -- the stepping core (`emu.Core`): op semantics and the branch hypotheses


@dataclass(frozen=True)
class Effect:
    """What a data op did: register writes in the op slot's file (None = unknown after it) and
    a note for the trace."""

    writes: tuple[tuple[int, Reg | None], ...] = ()
    note: str = ""


@dataclass(frozen=True)
class OpSemantics:
    """What a data op does. `run(word, slot, local)` returns its effect, or None where the word
    lies outside what the semantics covers (then it is an op without semantics)."""

    confidence: Confidence
    fact: str
    run: Callable[[Word, RegisterFile, Space], Effect | None]


def _local_read(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    args = word.operands
    assert args is not None and args.imm is not None
    if word["f120_122"] != 7:
        return None
    base = slot[args.ra]
    where = f"local word r{args.ra} + {args.imm:#x}"
    if base is None or base.exact is None:
        return Effect(note=f"reads {where}, r{args.ra} unknown")
    k = base.exact + args.imm
    addr = local.base + 4 * k
    if not local.contains(addr, 4):
        return Effect(note=f"reads {where} = {k:#x}, outside the modelled local memory")
    b = local.read(addr, 4)
    value = f"{int.from_bytes(b.data, 'little'):#x}" if all(b.known) else "unknown"
    return Effect(note=f"reads local word {k:#x}: {value}")


def _computed(
    word: Word, src: Reg | None, name: str, bounds: tuple[int, int] | None
) -> Effect:
    """ra := `name`, in `bounds` computed from register `src`; None bounds: the input lies
    outside what the operation covers. Either way an unknown input leaves ra unknown."""
    args = word.operands
    assert args is not None
    if src is None or bounds is None:
        why = "an unknown input" if src is None else "a negative input"
        return Effect(((args.ra, None),), f"r{args.ra} := {name}: {why}, unknown")
    lo, hi = bounds
    reg = Reg(lo, hi, min(src.taint, I), f"{name} (op {word.op:#x})")
    value = f"{lo:#x}" if lo == hi else f"{lo:#x}..{hi:#x}"
    return Effect(((args.ra, reg),), f"r{args.ra} := {name} = {value}")


def _shift_right(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    args = word.operands
    assert args is not None and args.rb is not None and args.imm is not None
    if word["f120_122"] != 7:
        return None
    src = slot[args.rb]
    name = f"r{args.rb} >> {args.imm}"
    if src is None or src.lo < 0:
        return _computed(word, src, name, None)
    return _computed(word, src, name, (src.lo >> args.imm, src.hi >> args.imm))


def sign_extend16(imm: int) -> int:
    return imm - 0x10000 if imm & 0x8000 else imm


def _and_mask(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    args = word.operands
    assert args is not None and args.imm is not None
    mask = sign_extend16(args.imm)
    src = slot[args.ra]
    name = f"r{args.ra} & {mask & 0xFFFFFFFF:#x}"
    if src is None or src.lo < 0:
        return _computed(word, src, name, None)
    if (
        src.exact is not None or mask < 0
    ):  # a mask that clears low bits only is monotonic
        return _computed(word, src, name, (src.lo & mask, src.hi & mask))
    return _computed(word, src, name, (0, min(src.hi, mask)))


OP_SEMANTICS: dict[int, OpSemantics] = {
    IDLE_OP: OpSemantics(
        I,
        "no op in the slot; the word's other units (the register transfers) are outside the "
        "core",
        lambda word, slot, local: Effect(),
    ),
    0x1A: OpSemantics(
        I,
        "reads local word ra + imm, ra a base register it does not write (inferred: program "
        "16's three 0x1a words name local words 0, 1 and 2 (src, dst, len) with one ra, and "
        "across later firmware versions of the same family, in 59 % of its words the next "
        "naming of that register is this op again, with another immediate in 95 % of those "
        "pairs, which as writes would be dead); where the datum goes is open, so the "
        "core records the read only; covered at f120_122 = 7, the value of those three words",
        _local_read,
    ),
    0x38: OpSemantics(
        I,
        "ra := ra & imm16 sign-extended (inferred: on the camera, clearing bit 4 of the "
        "immediate 0xfff0 in a copy's store-side 0x38 word moved the whole store 16 B down at a "
        "dst 16 mod 32 and left it in place at a 32-aligned dst, an AND of an address-derived "
        "value; the sign extension rests on the encoding (f120_122 = 7 exactly when f128_132 = "
        "31), since the masked value there is small; that ra is both source and destination is "
        "inferred from the format, the measured word naming one register)",
        _and_mask,
    ),
    0x3F: OpSemantics(
        I,
        "ra := rb >> imm (inferred: on the camera, the immediate of a copy's 0x3f word set to 1 "
        "and 2 halved and quartered the count a later transfer reads, on a positive value, so "
        "logical and arithmetic shifts are not separated and a negative rb gives an unknown "
        "result; the measured word names one register in ra and rb, so their roles come from "
        "the format; the count was read through the register its xreg field names, not ra, so "
        "where the result is written is open); covered at f120_122 = 7, the value of that word",
        _shift_right,
    ),
}
"""The data ops the core runs; every other op has no semantics (`Core`'s strict switch)."""


@dataclass(frozen=True)
class Branch:
    """A control op's decision: taken (None = undecided), why, and its confidence."""

    taken: bool | None
    why: str
    confidence: Confidence


class Branching(Protocol):
    """Decides the conditional control ops 0..2 (loop end, brf, br) at their `visit`-th run
    (from 0), from the op slot's registers `slot` or the transfer registers `transfer`."""

    def decide(
        self,
        index: int,
        visit: int,
        word: Word,
        slot: RegisterFile,
        transfer: RegisterFile,
    ) -> Branch: ...


@dataclass(frozen=True)
class Given:
    """Decisions supplied by the caller, by word index: one for every visit, or one per visit;
    every other decision is undecided. The default of the core: no condition of ops 0..2 is
    known."""

    decisions: Mapping[int, bool | Sequence[bool]] = field(default_factory=dict)
    confidence: Confidence = H
    fact: str = "given by the caller"

    def decide(
        self,
        index: int,
        visit: int,
        word: Word,
        slot: RegisterFile,
        transfer: RegisterFile,
    ) -> Branch:
        d = self.decisions.get(index)
        if isinstance(d, Sequence):
            d = d[visit] if visit < len(d) else None
        if d is None:
            return Branch(None, f"no decision given for visit {visit}", self.confidence)
        return Branch(d, self.fact, self.confidence)


class Test(Enum):
    ZERO = "ra == 0"
    NONZERO = "ra != 0"

    def holds(self, reg: Reg | None) -> bool | None:
        """Whether the test holds for `reg`; None when its interval does not decide it."""
        if reg is None:
            return None
        if reg.lo == reg.hi == 0:
            zero = True
        elif reg.lo > 0 or reg.hi < 0:
            zero = False
        else:
            return None
        return zero if self is Test.ZERO else not zero


class Regs(Enum):
    SLOT = "the op slot's registers"
    TRANSFER = "the transfer registers (xreg)"


class Count(Enum):
    TEST = "the loop end tests ra as it is"
    DECREMENT = "the loop end decrements ra, then tests it"


@dataclass(frozen=True)
class RaTest:
    """A condition on the register ra of the control word [hypothesis]: brf taken when `brf`
    holds, br when `br` holds, a loop end goes back to T when `loop` holds (after `count`).

    No reading is supported: in program 16 the transfer registers' job-start values fit both
    branches seen on the camera (27 names r7 = len / 2, not taken; 45 names r5, about 0, taken),
    but nothing shows the op slot's ra and xreg to name one file, and 27 tests r7 before any
    word of the slot names it. A guard and its loop end name the same register in about half
    the guarded loops, so the register is a shared loop operand of open meaning."""

    brf: Test = Test.ZERO
    br: Test = Test.NONZERO
    loop: Test = Test.NONZERO
    count: Count = Count.TEST
    reads: Regs = Regs.SLOT

    def decide(
        self,
        index: int,
        visit: int,
        word: Word,
        slot: RegisterFile,
        transfer: RegisterFile,
    ) -> Branch:
        regs = slot if self.reads is Regs.SLOT else transfer
        ra = word["ra"]
        reg = regs[ra]
        test = (self.loop, self.brf, self.br)[word.op]
        if word.op == 0 and self.count is Count.DECREMENT and reg is not None:
            reg = Reg(reg.lo - 1, reg.hi - 1, reg.taint, f"{reg.fact}, decremented")
            regs[ra] = reg
        taken = test.holds(reg)
        value = "unknown" if reg is None else _interval(reg)
        taint = H if reg is None else min(H, reg.taint)
        return Branch(
            taken, f"{test.value} in {self.reads.value}: r{ra} = {value}", taint
        )


def _interval(reg: Reg) -> str:
    return f"{reg.lo:#x}" if reg.exact is not None else f"{reg.lo:#x}..{reg.hi:#x}"
