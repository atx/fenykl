"""Semantics: word classes, parts, roles and the hidden sequencer, independent of one program.

A word class is matched by field values (`match`) and names the
fields it reads or ignores (`operands`); every field a class neither matches nor names as an
operand must be idle. A program's model covers a job's word at its index: by a field semantic
(program 16's register transfers), by the row of the stock word there (`OpaqueClass`), or by its
window's coverage rule (program 13). A word nothing covers raises `UnknownSemantics`, naming the
fields that differ from what covers the stock word there (strict mode); in the lenient mode the
differing fields are ignored and the job is tainted "hypothesis".

"Undecoded" means "no registered semantics", not "below verified": semantics registered at
hypothesis confidence run and taint what they touch.

The classes and tables of one program are held by its model (`memcpy.Copy16Model` for program
16, `resample.Resample13Model` for program 13), which the executor takes.

Stepping (`emu.Executor.step`) takes the op semantics `OP_SEMANTICS` (what a data op does,
each with its confidence) and a `Branching` hypothesis for the conditions of ops 0..2: `Given`
decisions (the default, none: no condition is known) or `RaTest`.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .fields import IDLE_OP, Confidence, Field, lookup
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
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    classes: Confidence = V
    """The weakest confidence of the classes that ran, at the positions they ran at."""
    taint: Confidence = V
    """Hypothesis when a field was ignored (lenient mode) or a word ran off its stock position."""
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


class Mode(Enum):
    POINTER = "pointer"
    POSITION = "position"


class Sequencer:
    """Hidden state that gives the register transfers their roles.

    POINTER (the default): each channel has a slot pointer; a transfer with bit 308 clear takes
    the slot at the pointer and advances it, so roles follow the order of the transfers
    (inferred). Idling a transfer leaves its channel's last slot unset, and an unset slot stalls
    the job; this reproduces the camera: every idled counted transfer of 42..49 and 66..77 times
    out, while 74 and 78 (bit 308 set) have no effect. The pointers start at slot 0 with every
    job. On the camera the roles followed the counted transfers when they moved: the count one
    index later (exchanged with the holding word 74, also at another register), and 75..77 one
    index earlier as a block, with a holding word after them (one run each, verified on the
    camera). A counted transfer at a channel's `stalls` index stalls the job: program 16's word
    78, the word before the return 79, where exchanging 77 with the holding word 78 times the job
    out (verified on the camera, two boots). Why 78 fails is open: too late for the stream, a
    restriction of that index alone, or a window per role that lets a role move one index early
    and the count one late but not 77 one late (hypothesis, not separated by the runs so far). A
    transfer off its stock index is still assigned at hypothesis taint.

    POSITION (the comparison mode): the role is the stock word index's role; a transfer at
    another index has none. Both give the same results on the field sweeps and idled words; the
    moved transfers above refute it for the count and for 75..77.
    """

    def __init__(self, mode: Mode = Mode.POINTER) -> None:
        self.mode = mode

    def assign(
        self, channels: Sequence[Channel], transfers: dict[str, list[Transfer]]
    ) -> tuple[list[tuple[Role, Transfer, Confidence]], list[tuple[str, Confidence]]]:
        """(role, transfer, assignment taint) per filled slot, and why the job stalls, with
        the confidence: each unset slot (hypothesis) and each counted transfer at a stall index
        (inferred).

        A channel that stalls has its filled slots left out: what the shifted transfers would do
        there is not modelled.
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


# -- stepping (`emu.Executor.step`): op semantics and the branch hypotheses


@dataclass(frozen=True)
class Effect:
    """What a data op did: register writes in the op slot's file (None = unknown after it), a
    note for the trace and local memory writes of unknown data (address, bytes; address None =
    anywhere)."""

    writes: tuple[tuple[int, Reg | None], ...] = ()
    note: str = ""
    stores: tuple[tuple[int | None, int], ...] = ()


@dataclass(frozen=True)
class OpSemantics:
    """What a data op does. `run(word, slot, local)` returns its effect, or None where the word
    lies outside what the semantics covers (then it is an op without semantics)."""

    confidence: Confidence
    fact: str
    run: Callable[[Word, RegisterFile, Space], Effect | None]


LOCAL_UNITS = {4: "word", 2: "halfword", 1: "byte"}
FLAG_BITS = {4: 0x1800, 2: 0x1000, 1: 0}
"""The imm13 bits of a family A op above the 13-bit byte offset: a flag that does not move the
address on r0 (inferred), its meaning open; words with it set are not covered."""


def local_offset(width: int, imm13: int) -> int:
    """The byte offset of a family A access: the low 13 bits of width x imm13, signed."""
    off = width * imm13 & 0x1FFF
    return off - 0x2000 if off & 0x1000 else off


def _local_access(
    width: int, word: Word, slot: RegisterFile
) -> tuple[str, int | None] | None:
    """(the access as text, its index in units of `width` from the local base, None for an
    unknown base) at byte 4 ra + offset, ra a word index; None outside what is covered."""
    args = word.operands
    assert args is not None and args.imm is not None
    imm13 = args.imm & 0x1FFF
    if word["f120_122"] != 7 or imm13 & FLAG_BITS[width]:
        return None
    units = local_offset(width, imm13) // width
    where = f"local {LOCAL_UNITS[width]} r{args.ra} + {units:#x}"
    base = slot[args.ra]
    if base is None or base.exact is None:
        return f"{where}, r{args.ra} unknown", None
    return where, 4 * base.exact // width + units


def _local_load(width: int) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A read of `width` bytes of local memory; ra is a base it does not write. The read is
    recorded only: where the datum goes is open."""
    unit = LOCAL_UNITS[width]

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        access = _local_access(width, word, slot)
        if access is None:
            return None
        where, k = access
        if k is None:
            return Effect(note=f"reads {where}")
        addr = local.base + width * k
        if not local.contains(addr, width):
            return Effect(
                note=f"reads {where} = {k:#x}, outside the modelled local memory"
            )
        b = local.read(addr, width)
        value = f"{int.from_bytes(b.data, 'little'):#x}" if all(b.known) else "unknown"
        return Effect(note=f"reads local {unit} {k:#x}: {value}")

    return run


def _local_store(width: int) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A write of `width` bytes of local memory; ra is a base it does not write. The datum's
    source is open, so the bytes become unknown; with an unknown base, all of local memory."""
    unit = LOCAL_UNITS[width]

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        access = _local_access(width, word, slot)
        if access is None:
            return None
        where, k = access
        if k is None:
            return Effect(stores=((None, width),), note=f"writes {where}: anywhere")
        addr = local.base + width * k
        if not local.contains(addr, width):
            return Effect(
                note=f"writes {where} = {k:#x}, outside the modelled local memory"
            )
        return Effect(
            stores=((addr, width),), note=f"writes local {unit} {k:#x}: unknown datum"
        )

    return run


def _computed(
    word: Word,
    src: Reg | None,
    name: str,
    bounds: tuple[int, int] | None,
    taint: Confidence = I,
) -> Effect:
    """ra := `name`, in `bounds` computed from register `src`, at most at `taint` (that of the
    destination); None bounds: the input lies outside what the operation covers. Either way an
    unknown input leaves ra unknown."""
    args = word.operands
    assert args is not None
    if src is None or bounds is None:
        why = "an unknown input" if src is None else "a negative input"
        return Effect(((args.ra, None),), f"r{args.ra} := {name}: {why}, unknown")
    lo, hi = bounds
    reg = Reg(lo, hi, min(src.taint, taint), f"{name} (op {word.op:#x})")
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
    bounds = (src.lo >> args.imm, src.hi >> args.imm)
    return _computed(word, src, name, bounds, H)  # the destination ra is a hypothesis


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
    clears_low = mask < 0 and (-mask) & (-mask - 1) == 0  # ~(2^k - 1): monotonic
    if src.exact is not None or clears_low:
        return _computed(word, src, name, (src.lo & mask, src.hi & mask))
    return _computed(word, src, name, (0, min(src.hi, mask) if mask >= 0 else src.hi))


_ADDRESS = (
    "the byte address is 4 ra plus the low 13 bits of width x imm13 as a signed offset; imm13's "
    "bits above them (11..12 in the word ops, 12 in the halfword ops) do not move the address "
    "on r0 (inferred: across later firmware versions of the same family, every read of an "
    "r0-based slot follows a store to the same low-13-bit bytes whatever those bits hold), so "
    "they are a flag of open meaning and words with it set are not covered"
)
_R0_READS = (
    "across later firmware versions of the same family, every read of an r0-based slot follows "
    "a store to the same bytes"
)


OP_SEMANTICS: dict[int, OpSemantics] = {
    IDLE_OP: OpSemantics(
        I,
        "no op in the slot; the word's other units (the register transfers) are outside the "
        "op slot",
        lambda word, slot, local: Effect(),
    ),
    0x1A: OpSemantics(
        I,
        "reads local word ra + imm, ra a base register it does not write (inferred: program "
        "16's three 0x1a words name local words 0, 1 and 2 (src, dst, len) with one ra, and "
        "across later firmware versions of the same family, in 59 % of its words the next "
        "naming of that register is this op again, with another immediate in 95 % of those "
        "pairs, which as writes would be dead; on the camera, 0x1a in place of the resampler's "
        "tap-1 read, imm 1 and ra kept, gave tap 1 the coefficient c2, the low 16 bits of the "
        f"word at 4 bytes: the immediate counts words); {_ADDRESS}; where the datum goes is "
        "open, so stepping records the read only; covered at f120_122 = 7, the value of those "
        "three words",
        _local_load(4),
    ),
    0x16: OpSemantics(
        I,
        "reads the local halfword at ra + imm halfwords, ra a base register it does not write, "
        "as 0x1a's (verified on the camera at one word that the immediate selects the tap: in "
        "the resampler, the word that reads tap 1 of the line's phase holds imm 1, and imm 0 "
        "gave tap 1 tap 0's coefficient, byte for byte; the halfword unit is inferred from "
        "that and from the request's halfwords, and ra a word index as 0x1a's is a hypothesis); "
        f"{_ADDRESS}; the sign extension is open; where the datum goes is open, so stepping "
        "records the read only; covered at f120_122 = 7, the value of that word",
        _local_load(2),
    ),
    0x14: OpSemantics(
        I,
        "reads the local halfword at ra + imm halfwords, ra a base register it does not write "
        f"(inferred: {_R0_READS}, 31 of 31 for 0x14; on the camera, 0x14 in place of the "
        "resampler's tap-1 read gave tap 1 c0 at imm 0 and the stock c1 at imm 1, so the "
        "immediate counts halfwords and the word does not pass on the datum read before it; "
        f"its sign extension against 0x16's is open); {_ADDRESS}; stepping records the read "
        "only; covered at f120_122 = 7",
        _local_load(2),
    ),
    0x10: OpSemantics(
        I,
        "reads the local byte at ra + imm bytes, zero-extended, ra a base register it does not "
        "write (inferred: on the camera, 0x10 in place of the resampler's tap-1 read, ra kept, "
        "gave tap 1 c0's bits 8..15 zero-extended at imm 1 and its bits 0..7 at imm 0, the "
        "bytes at offsets 1 and 0 of the phase's taps, on every line; a sign-extended byte is "
        f"refuted); {_ADDRESS}; where the datum goes is open, so stepping records the read "
        "only; covered at f120_122 = 7",
        _local_load(1),
    ),
    0x0E: OpSemantics(
        I,
        "reads the local byte at ra + imm bytes, sign-extended, ra a base register it does not "
        "write (inferred: on the camera, 0x0e in place of the resampler's tap-1 read, ra kept, "
        "gave tap 1 c0's bits 0..7 at imm 0 and its bits 8..15 at imm 1, sign-extended, on "
        "every line (imm 1 on two tap sets); nothing delivered and a zero-extended byte are refuted; "
        "a load from local memory and a select of byte imm of the datum the word before it "
        "delivered give the same bytes there, so the direction is open); "
        f"{_ADDRESS}; stepping records the read only; covered at f120_122 = 7",
        _local_load(1),
    ),
    0x12: OpSemantics(
        I,
        "writes the local halfword at ra + imm halfwords, ra a base register it does not write "
        "(inferred: across later firmware versions of the same family, 108 of its 114 accesses "
        "of r0-based slots are their first touch, 105 of its 124 accesses at -64..-1 B lie in "
        "the first fifth of their routine, an entry spill, and its displacements fit a halfword "
        f"unit); {_ADDRESS}; the datum's source is open, so the bytes become unknown; covered at "
        "f120_122 = 7",
        _local_store(2),
    ),
    0x18: OpSemantics(
        I,
        "writes the local word at ra + imm words, ra a base register it does not write "
        "(inferred: across later firmware versions of the same family, it opens 88 of the 95 "
        "r0-based word slots of the E-M10 II programs, its accesses at -64..-1 B lean to the "
        "first fifth of their routine (101 against 61 in the last), and a later access to the "
        "same address repeats the op in 0.19 of the cases against 0.66 for 0x1a); "
        f"{_ADDRESS}; the datum's source is open, so the bytes become unknown; covered at "
        "f120_122 = 7",
        _local_store(4),
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
        "the format); the destination ra is a hypothesis that the camera does not support, and "
        "the written register carries hypothesis taint: the count was read through the register "
        "the word's xreg field names, and in that copy no later data op names it in ra; covered "
        "at f120_122 = 7, the value of that word",
        _shift_right,
    ),
}
"""The op slot's semantics: the data ops stepping runs; every other op has none (strict
stepping raises)."""


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
    every other decision is undecided. The default of stepping: no condition of ops 0..2 is
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
    word of the slot names it. 45 is taken in every tested job, also with each of src, dst, len,
    src + len, dst + len and src - dst odd, so a test of ra = a job value & 0xf (the AND at 28)
    is refuted under either polarity (inferred); what 45 tests is open. A guard and its loop end name the same register in about half
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
