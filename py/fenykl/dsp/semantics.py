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
from itertools import product
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
    standins: set[int] = field(default_factory=set)
    """Indices where lenient mode ran the stock transfer for a word nothing covers."""
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    classes: Confidence = V
    """The weakest confidence of the classes that ran, at the positions they ran at."""
    taint: Confidence = V
    """Hypothesis when a field was ignored (lenient mode) or a word ran off its stock position."""
    inserted: list[int] = field(default_factory=list)
    """The job indices of the words inserted into the stock program (`memcpy.align`)."""
    writes: dict[int, tuple[Reg | None, int]] = field(default_factory=dict)
    """The xreg-set registers that inserted words wrote: (value, the writer's job index)
    (`memcpy`'s engine site)."""
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
    """What a data op did: the value it computes, when it computes one (`computes`; result None
    = a value not known), which its word's PR ports route to a register (`destinations`); a
    note for the trace; local memory writes of unknown data (address, bytes; address None =
    anywhere)."""

    computes: bool = False
    result: Reg | None = None
    note: str = ""
    stores: tuple[tuple[int | None, int], ...] = ()


@dataclass(frozen=True)
class OpSemantics:
    """What a data op does. `run(word, slot, local)` returns its effect, or None where the word
    lies outside what the semantics covers (then it is an op without semantics). `slot` is the
    register set that ra and rb read."""

    confidence: Confidence
    fact: str
    run: Callable[[Word, RegisterFile, Space], Effect | None]


class RegSet(Enum):
    """The two register sets of the scalar unit (inferred, strong: a data op's result written
    through port 267 is read by the X2 xreg of a later register transfer and not as an operand,
    one written through port 272 under enable 1 is read as an operand three words later and not
    by the xreg)."""

    XREG = "the set the X2 xreg reads (`Boot.regs`)"
    OPERAND = "the set ra and rb read (`Boot.slot`)"


@dataclass(frozen=True)
class ResultPort:
    """A PR port that carries a data op's result: under `enable` = `value` the register its
    field `reg` names, in `to`, from `latency` executed words after the op on."""

    enable: str
    value: int
    reg: str
    to: RegSet
    latency: int


RESULT_PORTS = (
    ResultPort("f267_268", 1, "f211_215", RegSet.XREG, 4),
    ResultPort("f272_273", 1, "f224_228", RegSet.OPERAND, 3),
)
"""The ports a data op's result goes through (inferred: on the camera, a data op of each of the
families B, C, D and E inserted after a copy's last setup word wrote the register f211_215
names under f267_268 = 1, not ra, which word 68 or 73 then read through its xreg; f224_228
under f272_273 = 1 fed a later op's ra and rb). The latencies are the shortest distances seen
working: the xreg transfer four words after the write, the operand read three words after it;
nearer reads are not known."""
PR_ENABLES = {
    "f265_266": "f206_210",
    "f267_268": "f211_215",
    "f269": "f216_220",
    "f270_271": "f221_223",
    "f272_273": "f224_228",
}
"""Each PR enable and the field of its port."""


def destinations(word: Word) -> tuple[list[tuple[RegSet, int, int]], list[str], str]:
    """Where the word's data op result goes: (set, register, latency) per decoded enabled port,
    the port fields of the enabled ports whose destination is open, and why the destination is
    incomplete ("" when every enabled port is decoded and one is). Under f272_273 = 2 the
    result reached neither set on the camera, under f265_266 = 1 and f269 = 0 not the xreg set
    (the operand set untested; one job each, hypothesis); with no port enabled nothing was seen
    written (hypothesis)."""
    dests, open_, names = [], [], []
    for enable, reg in PR_ENABLES.items():
        v = word[enable]
        if v == lookup(enable).idle:
            continue
        port = next(
            (p for p in RESULT_PORTS if p.enable == enable and p.value == v), None
        )
        if port is None:
            open_.append(reg)
            names.append(f"{enable}={v}")
        else:
            dests.append((port.to, word[port.reg], port.latency))
    if names:
        why = f"where the result goes under {', '.join(names)} is open"
    elif not dests:
        why = "no PR port enabled: where the result goes is open"
    else:
        why = ""
    return dests, open_, why


TRUE = 0x10
"""A compare's true; false is 0 (verified on the camera: a flag of 0x10 makes word 73 store 16
bytes, as a scalar unit whose values count in units of 16 would give)."""
ENUM_LIMIT = 1 << 16
"""The most input combinations a result over register intervals is enumerated for."""
SPAN = (-(1 << 31), 1 << 32)
"""Results outside [lo, hi), 32 bits read signed or unsigned, are unknown: the register width
was never measured."""

Value = Callable[..., int | None]
Scope = Callable[..., Confidence]


def _result(
    word: Word,
    text: str,
    inputs: tuple[Reg | None, ...],
    f: Value,
    tier: Confidence,
    scope: Scope | None = None,
) -> Effect:
    """The result f(*inputs) over the inputs' intervals, its hull enumerated; its taint is the
    weakest of `tier`, the inputs' and `scope` at each combination. f None at a combination
    (a case the rule leaves open) or a result outside `SPAN` gives an unknown value."""
    name = f"{text} (op {word.op:#x})"
    if any(x is None for x in inputs):
        return Effect(True, None, f"{name}: an unknown input")
    regs = [x for x in inputs if x is not None]
    n = 1
    for x in regs:
        n *= x.hi - x.lo + 1
    if n > ENUM_LIMIT:
        return Effect(True, None, f"{name}: inputs too wide to enumerate")
    outs, taint = [], min(tier, *(x.taint for x in regs))
    for args in product(*(range(x.lo, x.hi + 1) for x in regs)):
        v = f(*args)
        if v is None:
            return Effect(True, None, f"{name}: open at {', '.join(map(hex, args))}")
        outs.append(v)
        if scope is not None:
            taint = min(taint, scope(*args))
    lo, hi = min(outs), max(outs)
    if lo < SPAN[0] or hi >= SPAN[1]:
        return Effect(True, None, f"{name}: beyond 32 bits, the width is not measured")
    value = f"{lo:#x}" if lo == hi else f"{lo:#x}..{hi:#x}"
    return Effect(True, Reg(lo, hi, taint, name), f"{name} = {value}")


def _e(
    text: str, f: Value, tier: Confidence = V, scope: Scope | None = None
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A family E op on (ra, rb), covered at f120_122 = 7, where it was measured."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None and args.rb is not None
        if word["f120_122"] != 7:
            return None
        named = text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        if args.ra == args.rb:
            one = None if scope is None else (lambda a: scope(a, a))
            return _result(word, named, (slot[args.ra],), lambda a: f(a, a), tier, one)
        return _result(word, named, (slot[args.ra], slot[args.rb]), f, tier, scope)

    return run


def _imm(
    text: str, f: Callable[[int, int], int | None], tier: Confidence
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """An op on ra and c = imm << 4, imm as its format decodes it (family D's signed imm16)."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None and args.imm is not None
        c = args.imm << 4
        named = text.replace("ra", f"r{args.ra}").replace("c", f"{c:#x}")
        return _result(word, named, (slot[args.ra],), lambda a: f(a, c), tier)

    return run


def _imm_shift(
    text: str,
    f: Callable[[int, int], int | None],
    tier: Confidence,
    measured: Callable[[int], bool] | None = None,
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A family C op by its immediate, on a register that ra and rb both name (which of the two
    it reads is open), covered at f120_122 = 7 and imm 1..31 (imm 0 ended in a timeout, its
    reading open); `measured(imm)` false lowers the taint one tier."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None and args.imm is not None
        if word["f120_122"] != 7 or args.ra != args.rb or args.imm == 0:
            return None
        k = args.imm
        t = tier if measured is None or measured(k) else Confidence(max(tier - 1, 0))
        named = text.replace("ra", f"r{args.ra}").replace("k", str(k))
        return _result(word, named, (slot[args.ra],), lambda a: f(a, k), t)

    return run


def _signed_ops(scope_tier: Confidence) -> Scope:
    """`scope_tier` where the operands' signs differ, else verified."""
    return lambda a, b: scope_tier if (a < 0) != (b < 0) else V


def _same_sign(f: Callable[[int, int], int]) -> Value:
    """f where the operands' signs agree; None (open) where they differ."""
    return lambda a, b: f(a, b) if (a < 0) == (b < 0) else None


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def _flag(test: bool) -> int:
    return TRUE if test else 0


def _shift_count(rb: int) -> int:
    return (rb >> 4) & 31


def _rb_shift_scope(a: int, b: int) -> Confidence:
    """Verified on a non-negative ra and a count rb >> 4 below 32; the arithmetic shift of a
    negative ra and the & 31 are inferred."""
    return I if a < 0 or b < 0 or b >> 4 > 31 else V


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


def sign_extend16(imm: int) -> int:
    return imm - 0x10000 if imm & 0x8000 else imm


def _and_mask(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """ra & imm16 sign-extended. A mask that clears only low bits (~(2^k - 1)) is monotonic, so
    a wide ra interval keeps exact bounds; other masks of a wide interval give a bound."""
    args = word.operands
    assert args is not None and args.imm is not None
    if word["f267_268"] == 1:
        return None
    mask = sign_extend16(args.imm)
    src = slot[args.ra]
    text = f"r{args.ra} & {mask & 0xFFFFFFFF:#x}"
    clears_low = mask < 0 and (-mask) & (-mask - 1) == 0
    if src is None or src.exact is not None or not clears_low:
        return _result(word, text, (src,), lambda a: a & mask, I)
    if src.lo < 0:
        return Effect(True, None, f"{text} (op {word.op:#x}): a negative input")
    name = f"{text} (op {word.op:#x})"
    reg = Reg(src.lo & mask, src.hi & mask, min(src.taint, I), name)
    return Effect(True, reg, f"{name} = {reg.lo:#x}..{reg.hi:#x}")


def _f_imm(
    text: str, f: Callable[[int, int], int | None], tier: Confidence
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A family F op on ra and imm13 << 4, covered at f120_122 = 0 and imm13 below 0x1000 (bit
    12's sign is not measured)."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None and args.imm is not None
        if word["f120_122"] != 0 or args.imm >= 0x1000:
            return None
        c = args.imm << 4
        named = text.replace("ra", f"r{args.ra}").replace("c", f"{c:#x}")
        return _result(word, named, (slot[args.ra],), lambda a: f(a, c), tier)

    return run


def _hidden(
    text: str, same_regs: bool
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """An op that adds R, a per-boot value no job sets: its result is unknown until R is.
    Covered at f120_122 = 0 (other values select other hidden values), and with ra and rb one
    register where `same_regs` (the only form run)."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None
        if word["f120_122"] != 0 or same_regs and args.ra != args.rb:
            return None
        named = text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        return Effect(
            True, None, f"{named} (op {word.op:#x}): R, a per-boot value, unknown"
        )

    return run


def _move(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """ra where ra >= imm (max(ra, imm) is not excluded); exact equality is inferred, the
    readout resolving 1 KiB."""
    args = word.operands
    assert args is not None and args.imm is not None
    if not 0 <= args.imm <= 0x1234:
        return None
    imm = args.imm
    return _result(
        word, f"r{args.ra}", (slot[args.ra],), lambda a: a if a >= imm else None, I
    )


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
        "ra & imm16 sign-extended (inferred: on the camera, clearing bit 4 of the "
        "immediate 0xfff0 in a copy's store-side 0x38 word moved the whole store 16 B down at a "
        "dst 16 mod 32 and left it in place at a 32-aligned dst, an AND of an address-derived "
        "value; the sign extension rests on the encoding (f120_122 = 7 exactly when f128_132 = "
        "31), since the masked value there is small); unlike the other immediates measured, "
        "not scaled by 16 there. Not covered through port 267 (f267_268 = 1): inserted so, "
        "r2 & 0x1080 at len 0x1100 stored nothing where 0x1000 bytes were expected, and what "
        "that port carries for 0x38 is open",
        _and_mask,
    ),
    0x3F: OpSemantics(
        I,
        "rb >> imm (inferred: on the camera, at imm 1..5 (others hypothesis) and two lengths "
        "the count a later "
        "transfer reads followed len >> imm, a positive value, so logical and arithmetic "
        "shifts are not separated and a negative input is open; ra and rb named one register "
        "in every word measured, so which one it reads is open); covered with ra = rb at "
        "f120_122 = 7",
        _imm_shift(
            "ra >> k", lambda a, k: a >> k if a >= 0 else None, I, lambda k: k <= 5
        ),
    ),
    0x3E: OpSemantics(
        I,
        "rb >> imm, as 0x3f (inferred: on the camera, imm 1 halved its input at three "
        "lengths; logical and arithmetic not separated, the operand role open; other "
        "immediates hypothesis); covered with ra = rb at f120_122 = 7",
        _imm_shift(
            "ra >> k", lambda a, k: a >> k if a >= 0 else None, I, lambda k: k == 1
        ),
    ),
    0x40: OpSemantics(
        V,
        "ra << imm (verified on the camera at imm 1, 9, 10 and 11 with ra and rb naming one "
        "register, so which one it shifts is open; other immediates inferred); covered with "
        "ra = rb at f120_122 = 7",
        _imm_shift("ra << k", lambda a, k: a << k, V, lambda k: k in (1, 9, 10, 11)),
    ),
    0x37: OpSemantics(
        I,
        "ra | (imm << 4) (inferred: on the camera at three lengths with one immediate; add "
        "and 3 x ra refuted)",
        _imm("ra | c", lambda a, c: a | c, I),
    ),
    0x0A: OpSemantics(
        V,
        "ra (verified on the camera at imm 0..0x1234 through word 68's 1 KiB resolution, so "
        "exact equality is inferred; max(ra, imm) is not excluded, so covered where ra >= imm, "
        "at imm16 0..0x1234)",
        _move,
    ),
    0x44: OpSemantics(
        V,
        "ra - (imm13 << 4) (verified on the camera at four immediates, f120_122 = 0)",
        _f_imm("ra - c", lambda a, c: a - c, V),
    ),
    0x46: OpSemantics(
        I,
        "ra + (imm13 << 4) + R, R a value no job input sets that differs between boots "
        "(inferred: slope 1 in ra and in the immediate on the camera, the rest one value per "
        "boot); R is an unknown input, so the result is unknown; covered at f120_122 = 0",
        _hidden("ra + imm + R", False),
    ),
    0x47: OpSemantics(
        I,
        "ra + rb + R_f, R_f a value no job input sets that differs between boots, selected by "
        "f120_122 (inferred: measured with ra and rb one register only, at f120_122 = 0 the "
        "same R as 0x46's); the result is unknown while R is; covered at f120_122 = 0 with ra "
        "= rb",
        _hidden("ra + rb + R", True),
    ),
    **{
        code: OpSemantics(
            V,
            "rb (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
            _e("rb", lambda a, b: b),
        )
        for code in (0x08, 0x09, 0x32)
    },
    0x3C: OpSemantics(
        V,
        "ra & rb (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra & rb", lambda a, b: a & b),
    ),
    0x3A: OpSemantics(
        V,
        "ra | rb (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra | rb", lambda a, b: a | b),
    ),
    0x34: OpSemantics(
        V,
        "ra - rb (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra - rb", lambda a, b: a - b),
    ),
    0x35: OpSemantics(
        V,
        "ra + rb (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra + rb", lambda a, b: a + b),
    ),
    0x33: OpSemantics(
        V,
        "-rb (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("-rb", lambda a, b: -b),
    ),
    0x3D: OpSemantics(
        V,
        "~ra, not -ra (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run; a negate prediction failed by one)",
        _e("~ra", lambda a, b: ~a),
    ),
    0x2F: OpSemantics(
        V,
        "min(ra, rb) (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run; signed inferred: one point with a negative operand, its "
        "offset not pre-registered)",
        _e("min(ra, rb)", min, V, _signed_ops(I)),
    ),
    **{
        code: OpSemantics(
            V,
            "max(ra, rb) (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run); signed or unsigned is open, so operands of "
            "different signs are not covered",
            _e("max(ra, rb)", _same_sign(max)),
        )
        for code in (0x2C, 0x2E)
    },
    0x28: OpSemantics(
        V,
        "0x10 if ra <= rb unsigned, else 0 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra <=u rb", lambda a, b: _flag(_u32(a) <= _u32(b))),
    ),
    0x29: OpSemantics(
        V,
        "0x10 if ra < rb unsigned, else 0 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra <u rb", lambda a, b: _flag(_u32(a) < _u32(b))),
    ),
    0x2A: OpSemantics(
        V,
        "0x10 if ra <= rb signed, else 0 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra <= rb", lambda a, b: _flag(a <= b)),
    ),
    0x2B: OpSemantics(
        V,
        "0x10 if ra < rb signed, else 0 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra < rb", lambda a, b: _flag(a < b)),
    ),
    0x30: OpSemantics(
        V,
        "0x10 if ra != rb, else 0 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra != rb", lambda a, b: _flag(a != b)),
    ),
    0x31: OpSemantics(
        V,
        "0x10 if ra == rb, else 0 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run)",
        _e("ra == rb", lambda a, b: _flag(a == b)),
    ),
    **{
        code: OpSemantics(
            V,
            "ra >> ((rb >> 4) & 31), the count in units of 16 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run; arithmetic "
            "inferred from negative inputs whose offsets were not pre-registered, the & 31 "
            "from one point; 0x41 and 0x42 not told apart)",
            _e(
                "ra >> (rb >> 4)", lambda a, b: a >> _shift_count(b), V, _rb_shift_scope
            ),
        )
        for code in (0x41, 0x42)
    },
    0x43: OpSemantics(
        V,
        "ra << ((rb >> 4) & 31), the count in units of 16 (verified on the camera: chain-fed constants and len-derived values in ra and rb, the result read through word 68's load offset or word 73's count, each rule passing pre-registered dst sha256 predictions; at f120_122 = 7, the value of every word run; the & 31 inferred "
        "from 0x41's one point)",
        _e("ra << (rb >> 4)", lambda a, b: a << _shift_count(b), V, _rb_shift_scope),
    ),
    0x1E: OpSemantics(
        I,
        "ra - c, c = imm16 << 4 (inferred, strong: pre-registered dst sha256 predictions "
        "passed on the camera at several c of both signs)",
        _imm("ra - c", lambda a, c: a - c, I),
    ),
    0x1F: OpSemantics(
        I,
        "ra + c, c = imm16 << 4 (inferred, strong: as 0x1e)",
        _imm("ra + c", lambda a, c: a + c, I),
    ),
    0x36: OpSemantics(
        I,
        "ra ^ c, c = imm16 << 4 (inferred, strong: as 0x1e)",
        _imm("ra ^ c", lambda a, c: a ^ c, I),
    ),
    0x24: OpSemantics(
        I,
        "max(ra, c), c = imm16 << 4 (inferred: pre-registered predictions passed on the "
        "camera)",
        _imm("max(ra, c)", max, I),
    ),
    0x25: OpSemantics(
        I,
        "min(ra, c), c = imm16 << 4, signed (inferred: as 0x24; c < 0 gave c)",
        _imm("min(ra, c)", min, I),
    ),
    0x1C: OpSemantics(
        V,
        "0x10 if ra != c, else 0; c = imm16 << 4 (verified on the camera through word 73's "
        "count, pre-registered)",
        _imm("ra != c", lambda a, c: _flag(a != c), V),
    ),
    0x1D: OpSemantics(
        V,
        "0x10 if ra == c, else 0; c = imm16 << 4 (verified as 0x1c)",
        _imm("ra == c", lambda a, c: _flag(a == c), V),
    ),
    0x20: OpSemantics(
        V,
        "0x10 if ra <= c signed, else 0; c = imm16 << 4 (verified as 0x1c)",
        _imm("ra <= c", lambda a, c: _flag(a <= c), V),
    ),
    0x21: OpSemantics(
        V,
        "0x10 if ra < c signed, else 0; c = imm16 << 4 (verified as 0x1c)",
        _imm("ra < c", lambda a, c: _flag(a < c), V),
    ),
    0x23: OpSemantics(
        V,
        "0x10 if ra < c unsigned, else 0; c = imm16 << 4 (verified as 0x1c)",
        _imm("ra <u c", lambda a, c: _flag(_u32(a) < _u32(c)), V),
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
    but ra and xreg read two register sets (inferred), and stepping leaves r7 unknown by 27:
    word 14 names it through a PR port whose destination is open. 45 is taken in every tested job, also with each of src, dst, len,
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
