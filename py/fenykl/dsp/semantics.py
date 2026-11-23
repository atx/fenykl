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

    POINTER (the default): each channel has a slot pointer; a transfer with X1 head 0x1c takes
    the slot at the pointer and advances it, so roles follow the order of the transfers
    (inferred). Idling a transfer leaves its channel's last slot unset, and an unset slot stalls
    the job; this reproduces the camera: every idled counted transfer of 42..49 and 66..77 times
    out, while 74 and 78 (head 0x1d) have no effect. The pointers start at slot 0 with every
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
    """Two of the scalar unit's four register files (`fields`): B, which X1 reads (xreg among its operands) and
    port 267 writes, and G, which ra and rb read and port 272 writes (verified on the camera by
    exact readout that port 267 writes no G register). R and A are not modelled."""

    XREG = "B, the file X1 reads (`Boot.regs`)"
    OPERAND = "G, the file ra and rb read (`Boot.slot`)"


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
working in the copy: the xreg transfer four words after the write, the operand read three
words after it; exact readout on the camera shows the G write read by the very next word, which
the operand latency here does not model yet."""
PR_ENABLES = {
    "f265_266": "f206_210",
    "f267_268": "f211_215",
    "f269": "f216_220",
    "f270_271": "f221_223",
    "f272_273": "f224_228",
}
"""Each PR enable and the field of its port."""
OTHER_FILES = {"f206_210": "A", "f221_223": "R"}
"""Port registers of the register files the emulator does not model: a write there leaves G
and B as they are."""


def destinations(word: Word) -> tuple[list[tuple[RegSet, int, int]], list[str], str]:
    """Where the word's data op result goes: (set, register, latency) per decoded enabled port,
    the port fields of the enabled ports whose destination is open, and why the destination is
    incomplete ("" when every enabled port is decoded and one is). An enable that takes another
    unit's result (f272_273 = 0 or 2, f265_266 = 0) or writes R or A counts as open here. Without
    an enabled port a data op writes no register (verified on the camera by exact readout);
    stepping still reports such a result as open rather than dropping it."""
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


ENUM_LIMIT = 1 << 16
"""The most input combinations a result over register intervals is enumerated for."""
SPAN = (-(1 << 31), 1 << 32)
"""Results outside [lo, hi), 32 bits read signed or unsigned, are unknown: the ops wrap at 32
bits (verified on the camera by exact readout), which the intervals do not model."""
EXACT = "verified on the camera by exact readout"
"""Own programs run on the camera, every result stored into DSP local memory and read back as an
exact 32-bit word."""

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
    outs, taint = [], min((tier, *(x.taint for x in regs)))
    for args in product(*(range(x.lo, x.hi + 1) for x in regs)):
        v = f(*args)
        if v is None:
            return Effect(True, None, f"{name}: open at {', '.join(map(hex, args))}")
        outs.append(v)
        if scope is not None:
            taint = min(taint, scope(*args))
    lo, hi = min(outs), max(outs)
    if lo < SPAN[0] or hi >= SPAN[1]:
        return Effect(True, None, f"{name}: beyond 32 bits, the wrap is not modelled")
    value = f"{lo:#x}" if lo == hi else f"{lo:#x}..{hi:#x}"
    return Effect(True, Reg(lo, hi, taint, name), f"{name} = {value}")


def _e(
    text: str, f: Value, tier: Confidence = V
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A family E op on (ra, rb); f120_122 has no effect on it."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None and args.rb is not None
        named = text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        if args.ra == args.rb:
            return _result(word, named, (slot[args.ra],), lambda a: f(a, a), tier)
        return _result(word, named, (slot[args.ra], slot[args.rb]), f, tier)

    return run


def _imm(
    text: str, f: Callable[[int, int], int | None], tier: Confidence
) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """An op on ra and c, the immediate as its format decodes it (sign- or zero-extended)."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None and args.imm is not None
        c = args.imm
        named = text.replace("ra", f"r{args.ra}").replace("c", f"{c:#x}")
        return _result(word, named, (slot[args.ra],), lambda a: f(a, c), tier)

    return run


def _const(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """0x0b: the sign-extended imm16; ra is not read."""
    args = word.operands
    assert args is not None and args.imm is not None
    c = args.imm
    return _result(word, f"{c:#x}", (), lambda: c, V)


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def _s32(v: int) -> int:
    """v read as a signed 32-bit value: a register may hold either reading."""
    return ((v + (1 << 31)) & 0xFFFFFFFF) - (1 << 31)


def _min(a: int, b: int) -> int:
    return a if _s32(a) <= _s32(b) else b


def _max(a: int, b: int) -> int:
    return a if _s32(a) >= _s32(b) else b


def _flag(test: bool) -> int:
    return 1 if test else 0


LOCAL_UNITS = {4: "word", 2: "halfword", 1: "byte"}


def local_offset(width: int, imm13: int) -> int:
    """The byte offset of a family A access: width x imm13, imm13 signed (verified on the camera
    by exact readout)."""
    return width * imm13


def _local_access(
    width: int, word: Word, slot: RegisterFile
) -> tuple[str, int | None] | None:
    """(the access as text, its index in units of `width` from the local base, None for an
    unknown base) at the byte address ra + width x imm13; None at an address not aligned to
    the width (not measured)."""
    args = word.operands
    assert args is not None and args.imm is not None
    offset = local_offset(width, args.imm)
    where = f"local {LOCAL_UNITS[width]} r{args.ra} + {offset:#x} B"
    base = slot[args.ra]
    if base is None or base.exact is None:
        return f"{where}, r{args.ra} unknown", None
    addr = base.exact + offset
    if addr % width:
        return None
    return where, addr // width


def _local_load(width: int) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A read of `width` bytes of local memory; ra is a base it does not write. The read is
    recorded only: the datum's delivery to a port three words on is not modelled."""
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
    """A write of `width` bytes of local memory; ra is a base it does not write. The datum,
    R[f120_122], is not modelled, so the bytes become unknown; with an unknown base, all of
    local memory."""
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


def _and_mask(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """ra & imm16 sign-extended. A mask that clears only low bits (~(2^k - 1)) is monotonic, so
    a wide ra interval keeps exact bounds; other masks of a wide interval give a bound."""
    args = word.operands
    assert args is not None and args.imm is not None
    mask = args.imm
    src = slot[args.ra]
    text = f"r{args.ra} & {mask & 0xFFFFFFFF:#x}"
    clears_low = mask < 0 and (-mask) & (-mask - 1) == 0
    if src is None or src.exact is not None or not clears_low:
        return _result(word, text, (src,), lambda a: a & mask, V)
    if src.lo < 0:
        return Effect(True, None, f"{text} (op {word.op:#x}): a negative input")
    name = f"{text} (op {word.op:#x})"
    reg = Reg(src.lo & mask, src.hi & mask, src.taint, name)
    return Effect(True, reg, f"{name} = {reg.lo:#x}..{reg.hi:#x}")


def _ring_step(s: int, length: int) -> int:
    """A ring pointer step from s = zext(ra) + sext(R) by the ring length L."""
    if s < 0:
        return s + length
    if s >= length:
        return s - length
    return s


def _ring_one(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """0x44: the ring pointer step with R = 1 and L the low 8 bits of imm13."""
    args = word.operands
    assert args is not None and args.imm is not None
    length = args.imm & 0xFF
    text = f"ring step r{args.ra} + 1, L {length:#x}"
    return _result(
        word, text, (slot[args.ra],), lambda a: _ring_step(_u32(a) + 1, length), V
    )


def _ring_r(text: str) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """0x46 / 0x47: the ring pointer step with R[f120_122], which the model does not hold, so
    the result is unknown."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None
        named = text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        r = word["f120_122"]
        return Effect(
            True, None, f"{named}, R{r} (op {word.op:#x}): the R file is not modelled"
        )

    return run


_EXACT_RULE = f"{EXACT}; 32-bit wrap"


OP_SEMANTICS: dict[int, OpSemantics] = {
    IDLE_OP: OpSemantics(
        I,
        "no op in the slot; the word's other units (the register transfers) are outside the "
        "op slot",
        lambda word, slot, local: Effect(),
    ),
    **{
        code: OpSemantics(
            V,
            f"{fmt} local {LOCAL_UNITS[width]} load at the byte address ra + {width} x imm13, ra "
            f"a base it does not write ({EXACT}); the datum reaches the ALU result bus three "
            "words on, which stepping does not model: it records the read only",
            _local_load(width),
        )
        for code, width, fmt in (
            (0x1A, 4, "a"),
            (0x16, 2, "a signed"),
            (0x14, 2, "an unsigned"),
            (0x10, 1, "an unsigned"),
            (0x0E, 1, "a signed"),
        )
    },
    **{
        code: OpSemantics(
            V,
            f"a local {LOCAL_UNITS[width]} store at the byte address ra + {width} x imm13 of "
            f"the low {8 * width} bits of R[f120_122], ra a base it does not write ({EXACT}); "
            "the R file is not modelled, so the bytes become unknown",
            _local_store(width),
        )
        for code, width in ((0x18, 4), (0x12, 2), (0x0C, 1))
    },
    0x38: OpSemantics(V, f"ra & imm16 sign-extended ({EXACT})", _and_mask),
    0x0B: OpSemantics(V, f"imm16 sign-extended; ra not read ({EXACT})", _const),
    0x37: OpSemantics(
        V, f"ra | imm15 ({EXACT})", _imm("ra | c", lambda a, c: a | c, V)
    ),
    0x0A: OpSemantics(
        I,
        "ra | (imm16 << 16) (inferred, by exact readout)",
        _imm("ra | (c << 16)", lambda a, c: a | c << 16, I),
    ),
    0x3E: OpSemantics(
        I,
        "ra >> imm5, arithmetic; rb and f120_122 have no effect (inferred, by exact readout)",
        _imm("ra >> c", lambda a, c: _s32(a) >> c, I),
    ),
    0x3F: OpSemantics(
        I,
        "ra >> imm5, logical; rb and f120_122 have no effect (inferred, by exact readout)",
        _imm("ra >>> c", lambda a, c: _u32(a) >> c, I),
    ),
    0x40: OpSemantics(
        I,
        "ra << imm5; rb and f120_122 have no effect (inferred, by exact readout)",
        _imm("ra << c", lambda a, c: a << c, I),
    ),
    0x44: OpSemantics(
        V,
        "the ring pointer step: s = zext(ra) + 1, L the low 8 bits of imm13, the result s + L "
        f"if s < 0, s - L if s >= L, else s ({EXACT} at 216 points with 0x46 and 0x47)",
        _ring_one,
    ),
    0x46: OpSemantics(
        V,
        "the ring pointer step as 0x44's with s = zext(ra) + sext(R[f120_122]) "
        f"({EXACT}); the R file is not modelled, so the result is unknown",
        _ring_r("ring step ra + R, L imm"),
    ),
    0x47: OpSemantics(
        V,
        "the ring pointer step as 0x46's with L = rb "
        f"({EXACT}); the R file is not modelled, so the result is unknown",
        _ring_r("ring step ra + R, L rb"),
    ),
    **{
        code: OpSemantics(V, f"rb ({_EXACT_RULE})", _e("rb", lambda a, b: b))
        for code in (0x08, 0x09)
    },
    0x32: OpSemantics(
        I,
        "abs(rb) (inferred, by exact readout)",
        _e("abs(rb)", lambda a, b: abs(_s32(b)), I),
    ),
    0x3C: OpSemantics(V, f"ra & rb ({_EXACT_RULE})", _e("ra & rb", lambda a, b: a & b)),
    0x3A: OpSemantics(V, f"ra | rb ({_EXACT_RULE})", _e("ra | rb", lambda a, b: a | b)),
    0x34: OpSemantics(V, f"ra - rb ({_EXACT_RULE})", _e("ra - rb", lambda a, b: a - b)),
    0x35: OpSemantics(V, f"ra + rb ({_EXACT_RULE})", _e("ra + rb", lambda a, b: a + b)),
    0x33: OpSemantics(V, f"-rb ({_EXACT_RULE})", _e("-rb", lambda a, b: -b)),
    0x3D: OpSemantics(V, f"~ra ({_EXACT_RULE})", _e("~ra", lambda a, b: ~a)),
    0x2F: OpSemantics(
        V, f"min(ra, rb) signed ({_EXACT_RULE})", _e("min(ra, rb)", _min)
    ),
    0x2E: OpSemantics(
        V, f"max(ra, rb) signed ({_EXACT_RULE})", _e("max(ra, rb)", _max)
    ),
    0x2C: OpSemantics(
        I,
        "max(ra, rb) unsigned (inferred, by exact readout)",
        _e("maxu(ra, rb)", lambda a, b: a if _u32(a) >= _u32(b) else b, I),
    ),
    0x28: OpSemantics(
        V,
        f"1 if ra <= rb unsigned, else 0 ({EXACT})",
        _e("ra <=u rb", lambda a, b: _flag(_u32(a) <= _u32(b))),
    ),
    0x29: OpSemantics(
        V,
        f"1 if ra < rb unsigned, else 0 ({EXACT})",
        _e("ra <u rb", lambda a, b: _flag(_u32(a) < _u32(b))),
    ),
    0x2A: OpSemantics(
        V,
        f"1 if ra <= rb signed, else 0 ({EXACT})",
        _e("ra <= rb", lambda a, b: _flag(_s32(a) <= _s32(b))),
    ),
    0x2B: OpSemantics(
        V,
        f"1 if ra < rb signed, else 0 ({EXACT})",
        _e("ra < rb", lambda a, b: _flag(_s32(a) < _s32(b))),
    ),
    0x30: OpSemantics(
        V,
        f"1 if ra != rb, else 0 ({EXACT})",
        _e("ra != rb", lambda a, b: _flag(_u32(a) != _u32(b))),
    ),
    0x31: OpSemantics(
        V,
        f"1 if ra == rb, else 0 ({EXACT})",
        _e("ra == rb", lambda a, b: _flag(_u32(a) == _u32(b))),
    ),
    0x41: OpSemantics(
        V,
        f"ra >> (rb & 31), arithmetic ({EXACT})",
        _e("ra >> rb", lambda a, b: _s32(a) >> (b & 31)),
    ),
    0x42: OpSemantics(
        I,
        "ra >> (rb & 31), logical (inferred, by exact readout)",
        _e("ra >>> rb", lambda a, b: _u32(a) >> (b & 31), I),
    ),
    0x43: OpSemantics(
        V,
        f"ra << (rb & 31) ({_EXACT_RULE})",
        _e("ra << rb", lambda a, b: a << (b & 31)),
    ),
    0x1E: OpSemantics(
        I,
        "ra - imm16, imm16 zero-extended (inferred, by exact readout)",
        _imm("ra - c", lambda a, c: a - c, I),
    ),
    0x1F: OpSemantics(
        I,
        "ra + imm16, imm16 zero-extended (inferred, by exact readout)",
        _imm("ra + c", lambda a, c: a + c, I),
    ),
    0x36: OpSemantics(
        V,
        f"ra ^ c, c = imm16 sign-extended ({EXACT})",
        _imm("ra ^ c", lambda a, c: a ^ c, V),
    ),
    0x24: OpSemantics(
        V,
        f"max(ra, c) signed, c = imm16 sign-extended ({EXACT})",
        _imm("max(ra, c)", _max, V),
    ),
    0x25: OpSemantics(
        V,
        f"min(ra, c) signed, c = imm16 sign-extended ({EXACT})",
        _imm("min(ra, c)", _min, V),
    ),
    0x1C: OpSemantics(
        V,
        f"1 if ra != c, else 0; c = imm16 sign-extended ({EXACT})",
        _imm("ra != c", lambda a, c: _flag(_u32(a) != _u32(c)), V),
    ),
    0x1D: OpSemantics(
        V,
        f"1 if ra == c, else 0; c = imm16 sign-extended ({EXACT})",
        _imm("ra == c", lambda a, c: _flag(_u32(a) == _u32(c)), V),
    ),
    0x20: OpSemantics(
        V,
        f"1 if ra <= c signed, else 0; c = imm16 sign-extended ({EXACT})",
        _imm("ra <= c", lambda a, c: _flag(_s32(a) <= c), V),
    ),
    0x21: OpSemantics(
        V,
        f"1 if ra < c signed, else 0; c = imm16 sign-extended ({EXACT})",
        _imm("ra < c", lambda a, c: _flag(_s32(a) < c), V),
    ),
    0x23: OpSemantics(
        I,
        "1 if ra < imm16 unsigned, else 0; imm16 zero-extended (inferred, by exact readout)",
        _imm("ra <u c", lambda a, c: _flag(_u32(a) < c), I),
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
    """A condition on the register ra of the control word: brf taken when `brf` holds, br when
    `br` holds, a loop end goes back to T when `loop` holds (after `count`).

    The defaults of brf and br are the measured rules: op 1 branches iff ra == 0, op 2 iff ra
    != 0 (verified on the camera by exact readout). The measured loop end branches iff ra > 0
    signed and computes ra - 1, which an enabled PR port writes back; `loop` and `count` do not
    express that write-back, so a loop end here stays a hypothesis. In program 16 stepping
    leaves r7 unknown by 27: word 14 names it through a PR port whose destination is open."""

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
