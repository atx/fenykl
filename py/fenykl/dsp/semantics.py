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
16, `resample.Resample13Model` for program 13), which `emu.run_model` takes.

`OP_SEMANTICS` is the op slot's table: what a data op does, each with its confidence.
Stepping (`emu.Executor.step`) can take `Given` decisions for the conditions of ops 0..2 over
the datapath's own (`Branching`). `X_RULES` are the functions of the two X units and
`LANE_RULES` the lane functions of the four S units (`lane_result`); `LaneMemory` and
`lane_address` model the lane memory X0 loads and stores. `HEAD_READS` (`head_reads`) holds the
operands each head reads, from the rules and what the machine does with a head.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .fields import (
    DIVIDE_HEAD,
    HEAD_VIEWS,
    IDLE_OP,
    LANE_FUNCTIONS,
    LANE_VECTORS,
    NO_LANE,
    S2_HIGH_HALVES,
    S_IMMEDIATES,
    S_RESULTS,
    TRANSFER_HEADS,
    U_NOTHING,
    U_TAKES,
    X0_LANE_BY_REGISTER,
    X0_LANE_LOADS,
    X0_LANE_MEMORY,
    X_FUNCTIONS,
    X_IMMEDIATES,
    Confidence,
    Field,
    lookup,
)
from .state import Reg
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


# -- the op slot's semantics and the branch hypotheses


@dataclass(frozen=True)
class OpSemantics:
    """What a data op does, at its tier: `rule` the value a computing op gives (None for the ops
    whose rule is their own: the local accesses, the and-mask, the constant, the ring steps, the
    multiplies and the idle op)."""

    confidence: Confidence
    fact: str
    rule: "OpValue | None" = None


PR_ENABLES = {
    "f265_266": "f206_210",
    "f267_268": "f211_215",
    "f269": "f216_220",
    "f270_271": "f221_223",
    "f272_273": "f224_228",
}
"""Each PR enable and the field of its port."""
EXACT = "verified on the camera by exact readout"
"""Own programs run on the camera, every result stored into DSP local memory and read back as an
exact 32-bit word."""

Value = Callable[..., int | None]


@dataclass(frozen=True)
class OpValue:
    """A computing op's rule: f(ra, rb) (`form` "rb", a family E op; f120_122 has no effect on
    it) or f(ra, c) (`form` "c", c the immediate as its format decodes it, sign- or
    zero-extended). `exact` gives the 32-bit result on exact operands (None where the rule is
    open)."""

    text: str
    f: Callable[[int, int], int | None]
    tier: Confidence
    form: str

    @property
    def reads_ra(self) -> bool:
        return re.search(r"\bra\b", self.text) is not None

    def exact(self, ra: int, rb: int | None, c: int | None) -> int | None:
        second = rb if self.form == "rb" else c
        if second is None:
            raise ValueError(f"{self.text}: no {self.form} operand")
        v = self.f(ra, second)
        return None if v is None else _u32(v)


def _e(text: str, f: Callable[[int, int], int | None], tier: Confidence = V) -> OpValue:
    """A family E op on (ra, rb)."""
    return OpValue(text, f, tier, "rb")


def _imm(text: str, f: Callable[[int, int], int | None], tier: Confidence) -> OpValue:
    """An op on ra and c."""
    return OpValue(text, f, tier, "c")


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
LOCAL_BYTES = 0x2000
"""The scalar address space: local memory, 8 KiB; a family A address is taken modulo its size
(verified on the camera by exact readout: ra up to 0xffffe008 read the word at its low 13
bits, a DRAM address read local memory)."""


def local_offset(width: int, imm13: int) -> int:
    """The byte offset of a family A access: width x imm13, imm13 signed (verified on the camera
    by exact readout)."""
    return width * imm13


def ring_step(s: int, length: int) -> int:
    """A ring pointer step from s = zext(ra) + sext(R) by the ring length L."""
    if s < 0:
        return s + length
    if s >= length:
        return s - length
    return s


MULTIPLY_LAG = 1
"""A multiply's product reaches the ALU bus this many words after the op; a port of that word
takes it, and the op's own word gets nothing (verified on the camera by exact readout)."""
LOAD_LAG = 3
"""A load's datum reaches the ALU bus this many words after the load (verified on the camera by
exact readout)."""


_EXACT_RULE = f"{EXACT}; 32-bit wrap"
LOCAL_LOADS = {
    0x1A: (4, False),
    0x16: (2, True),
    0x14: (2, False),
    0x10: (1, False),
    0x0E: (1, True),
}
"""The family A loads by op: (width in bytes, sign-extended)."""
LOCAL_STORES = {0x18: 4, 0x12: 2, 0x0C: 1}
"""The family A stores by op: the width in bytes of R[f120_122]'s low bits they write."""
LOCAL_LOADS_RB = {code + 1: form for code, form in LOCAL_LOADS.items()}
LOCAL_STORES_RB = {code + 1: width for code, width in LOCAL_STORES.items()}
"""The register forms, each its immediate form's op + 1: the byte address is ra + width x rb."""


_UNSEEN = (
    "a data op value never used in the stock programs (verified on the camera by exact readout, "
    "pre-registered, eight operand sets of both signs; no result one word later, the operands "
    "unchanged)"
)
_UNSEEN_PAIRS = (
    "a data op value never used in the stock programs (fitted on eight operand sets, then "
    "verified on the camera by exact readout, pre-registered, in two jobs of eight sets whose "
    "operands hold every bit pair in both orders, bit 31 included; the operands unchanged)"
)
_UNSEEN_IMM = (
    "a data op value never used in the stock programs (verified on the camera by exact readout, "
    "pre-registered, in two jobs of twelve operand sets each with ra below, equal to and above "
    "imm16, imm16 at and above 0x8000 against ra values that tell zero- from sign-extension, "
    "every imm16 bit set and clear; the operands unchanged)"
)


OP_SEMANTICS: dict[int, OpSemantics] = {
    IDLE_OP: OpSemantics(
        I,
        "no op in the slot; the word's other units (the register transfers) are outside the "
        "op slot",
    ),
    **{
        code: OpSemantics(
            V,
            f"{fmt} local {LOCAL_UNITS[width]} load at the byte address ra + {width} x imm13 "
            f"modulo 0x2000, ra a base it does not write ({EXACT}); the datum reaches the ALU "
            f"result bus {LOAD_LAG} words on, which stepping does not model: it records the "
            "read only",
        )
        for code, (width, signed) in LOCAL_LOADS.items()
        for fmt in ("a" if width == 4 else "a signed" if signed else "an unsigned",)
    },
    **{
        code: OpSemantics(
            V,
            f"a local {LOCAL_UNITS[width]} store at the byte address ra + {width} x imm13 "
            f"modulo 0x2000 of the low {8 * width} bits of R[f120_122], ra a base it does not write ({EXACT}); "
            "the R file is not modelled, so the bytes become unknown",
        )
        for code, width in LOCAL_STORES.items()
    },
    **{
        code: OpSemantics(
            V,
            f"the register form of {code - 1:#x}: a local {LOCAL_UNITS[width]} load at the byte "
            f"address ra + {width} x rb modulo 0x2000 ({EXACT}, one address each)",
        )
        for code, (width, _) in LOCAL_LOADS_RB.items()
    },
    **{
        code: OpSemantics(
            V,
            f"the register form of {code - 1:#x}: a local {LOCAL_UNITS[width]} store at the byte "
            f"address ra + {width} x rb modulo 0x2000 of the low {8 * width} bits of R[f120_122] "
            f"({EXACT}, one address each)",
        )
        for code, width in LOCAL_STORES_RB.items()
    },
    0x38: OpSemantics(V, f"ra & imm16 sign-extended ({EXACT})"),
    0x0B: OpSemantics(V, f"imm16 sign-extended; ra not read ({EXACT})"),
    0x37: OpSemantics(
        V, f"ra | imm15 ({EXACT})", _imm("ra | c", lambda a, c: a | c, V)
    ),
    0x0A: OpSemantics(
        V,
        "ra | (imm16 << 16) (verified on the camera by exact readout, pre-registered)",
        _imm("ra | (c << 16)", lambda a, c: a | c << 16, V),
    ),
    0x3E: OpSemantics(
        V,
        "ra >> imm5, arithmetic; rb and f120_122 have no effect (verified on the camera by exact readout, pre-registered)",
        _imm("ra >> c", lambda a, c: _s32(a) >> c, V),
    ),
    0x3F: OpSemantics(
        V,
        "ra >> imm5, logical; rb and f120_122 have no effect (verified on the camera by exact readout, pre-registered)",
        _imm("ra >>> c", lambda a, c: _u32(a) >> c, V),
    ),
    0x40: OpSemantics(
        V,
        "ra << imm5; rb and f120_122 have no effect (verified on the camera by exact readout, pre-registered)",
        _imm("ra << c", lambda a, c: a << c, V),
    ),
    0x44: OpSemantics(
        V,
        "the ring pointer step: s = zext(ra) + 1, L the low 8 bits of imm13, the result s + L "
        f"if s < 0, s - L if s >= L, else s ({EXACT} at 216 points with 0x46 and 0x47)",
    ),
    0x46: OpSemantics(
        V,
        "the ring pointer step as 0x44's with s = zext(ra) + sext(R[f120_122]) "
        f"({EXACT}); the R file is not modelled, so the result is unknown",
    ),
    0x47: OpSemantics(
        V,
        "the ring pointer step as 0x46's with L = rb "
        f"({EXACT}); the R file is not modelled, so the result is unknown",
    ),
    **{
        code: OpSemantics(V, f"rb ({_EXACT_RULE})", _e("rb", lambda a, b: b))
        for code in (0x08, 0x09)
    },
    0x32: OpSemantics(
        V,
        "abs(rb) (verified on the camera by exact readout, pre-registered)",
        _e("abs(rb)", lambda a, b: abs(_s32(b)), V),
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
        V,
        "max(ra, rb) unsigned (verified on the camera by exact readout, pre-registered)",
        _e("maxu(ra, rb)", lambda a, b: a if _u32(a) >= _u32(b) else b, V),
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
        V,
        "ra >> (rb & 31), logical (verified on the camera by exact readout, pre-registered)",
        _e("ra >>> rb", lambda a, b: _u32(a) >> (b & 31), V),
    ),
    0x43: OpSemantics(
        V,
        f"ra << (rb & 31) ({_EXACT_RULE})",
        _e("ra << rb", lambda a, b: a << (b & 31)),
    ),
    0x1E: OpSemantics(
        V,
        "ra - imm16, imm16 zero-extended (verified on the camera by exact readout, pre-registered)",
        _imm("ra - c", lambda a, c: a - c, V),
    ),
    0x1F: OpSemantics(
        V,
        "ra + imm16, imm16 zero-extended (verified on the camera by exact readout, pre-registered)",
        _imm("ra + c", lambda a, c: a + c, V),
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
    0x22: OpSemantics(
        V,
        f"1 if ra <= imm16 unsigned, else 0; imm16 zero-extended ({_UNSEEN_IMM})",
        OpValue("ra <=u c", lambda a, c: _flag(_u32(a) <= c), V, "c"),
    ),
    0x26: OpSemantics(
        V,
        f"max(ra, imm16) unsigned, imm16 zero-extended ({_UNSEEN_IMM})",
        OpValue("maxu(ra, c)", lambda a, c: max(_u32(a), c), V, "c"),
    ),
    0x27: OpSemantics(
        V,
        f"min(ra, imm16) unsigned, imm16 zero-extended ({_UNSEEN_IMM})",
        OpValue("minu(ra, c)", lambda a, c: min(_u32(a), c), V, "c"),
    ),
    0x2D: OpSemantics(
        V,
        f"min(ra, rb) unsigned ({_UNSEEN})",
        _e("minu(ra, rb)", lambda a, b: min(_u32(a), _u32(b)), V),
    ),
    0x39: OpSemantics(V, f"ra ^ rb ({_UNSEEN})", _e("ra ^ rb", lambda a, b: a ^ b, V)),
    0x3B: OpSemantics(
        V, f"ra & ~rb ({_UNSEEN_PAIRS})", _e("ra & ~rb", lambda a, b: a & ~b, V)
    ),
    0x45: OpSemantics(
        V,
        "the ring pointer step as 0x44's with s = zext(ra) + 1 and L = rb, all 32 bits of rb "
        f"({_UNSEEN})",
        _e("ring step ra + 1, L rb", lambda a, b: ring_step(_u32(a) + 1, _u32(b)), V),
    ),
    0x23: OpSemantics(
        V,
        "1 if ra < imm16 unsigned, else 0; imm16 zero-extended (verified on the camera by exact readout, pre-registered)",
        _imm("ra <u c", lambda a, c: _flag(_u32(a) < c), V),
    ),
    0x49: OpSemantics(
        V,
        f"(ra x rb) & 0xffffffff on the ALU bus {MULTIPLY_LAG} word after the op, nothing in "
        "the op's own word nor two or three words on; back to back, each word's port takes the "
        f"previous multiply's product; f120_122 has no effect ({EXACT}, 48 of 48 values on "
        "eight operand pairs of both signs). Stepping records the product only: the next "
        "word's ports do not get it",
    ),
    0x48: OpSemantics(
        V,
        f"as 0x49, the same 32 bits on every operand pair ({EXACT}); what sets the two apart "
        "is open (the high half was not seen). Stepping records the product only",
    ),
}
"""The op slot's semantics: the data ops stepping runs; every other op has none (strict
stepping raises)."""


XValue = Callable[[int, int, int, int], int]
"""An X function of (a, b, zK, sK)."""
_X_SHARED: dict[int, XValue] = {
    0x00: lambda a, b, zk, sk: _flag(_u32(a) != _u32(sk)),
    0x01: lambda a, b, zk, sk: _flag(_u32(a) == _u32(sk)),
    0x02: lambda a, b, zk, sk: a - zk,
    0x03: lambda a, b, zk, sk: a + zk,
    0x04: lambda a, b, zk, sk: _flag(_s32(a) <= sk),
    0x05: lambda a, b, zk, sk: _flag(_s32(a) < sk),
    0x06: lambda a, b, zk, sk: _flag(_u32(a) <= zk),
    0x07: lambda a, b, zk, sk: _flag(_u32(a) < zk),
    0x08: lambda a, b, zk, sk: _flag(_u32(a) <= _u32(b)),
    0x09: lambda a, b, zk, sk: _flag(_u32(a) < _u32(b)),
    0x0A: lambda a, b, zk, sk: _flag(_s32(a) <= _s32(b)),
    0x0B: lambda a, b, zk, sk: _flag(_s32(a) < _s32(b)),
    0x0C: lambda a, b, zk, sk: _flag(_u32(a) != _u32(b)),
    0x0D: lambda a, b, zk, sk: _flag(_u32(a) == _u32(b)),
    0x0E: lambda a, b, zk, sk: abs(_s32(b)),
    0x0F: lambda a, b, zk, sk: -b,
    0x10: lambda a, b, zk, sk: a - b,
    0x11: lambda a, b, zk, sk: a + b,
    0x12: lambda a, b, zk, sk: a | zk << 16,
    0x13: lambda a, b, zk, sk: zk,
    0x14: lambda a, b, zk, sk: sk,
    0x15: lambda a, b, zk, sk: b,
}
_X_ONLY: dict[str, dict[int, XValue | None]] = {
    "x0": {
        **{h: None for h in (0x16, 0x17, 0x18, 0x19, 0x22)},
        **{h: lambda a, b, zk, sk: sk + 1 for h in range(0x1A, 0x1E)},
        **{h: lambda a, b, zk, sk: b + 1 for h in range(0x1E, 0x22)},
        **{h: lambda a, b, zk, sk: 0 for h in range(0x23, 0x3F)},
    },
    "x1": {
        0x16: lambda a, b, zk, sk: ~a & b,
        0x17: lambda a, b, zk, sk: ~a & ~b,
        0x18: lambda a, b, zk, sk: a ^ b,
        0x19: lambda a, b, zk, sk: a | b,
        0x1A: lambda a, b, zk, sk: a & b,
        0x1B: lambda a, b, zk, sk: ~b,
        **{h: None for h in (0x1C, 0x1D, 0x1E)},
    },
}


@dataclass(frozen=True)
class XRule:
    """What an X unit's head value does: `value` of (a, b, zK, sK), None for no result on the
    ALU bus; `reads` the register operands it reads ("a", "b")."""

    confidence: Confidence
    fact: str
    value: XValue | None
    reads: str = "ab"


def _reads(**heads: tuple[int, ...] | range) -> dict[int, str]:
    """Heads by the operands they read: a=..., b=..., none=...; any other head reads both."""
    return {h: "" if key == "none" else key for key, hs in heads.items() for h in hs}


_X_READS = {
    "x0": _reads(
        a=(*range(8), 0x12),
        b=(0x0E, 0x0F, 0x15, *range(0x1E, 0x22)),
        none=(0x13, 0x14, *range(0x1A, 0x1E), *range(0x23, 0x3F)),
    ),
    "x1": _reads(a=(*range(8), 0x12), b=(0x0E, 0x0F, 0x15, 0x1B), none=(0x13, 0x14)),
}
"""The register operands each X head reads, from its rule: a compare against the immediate a,
abs / negate / move b, the immediates none."""


def _x_rules(unit: str) -> dict[int, XRule]:
    values = _X_SHARED | _X_ONLY[unit]
    table = X_FUNCTIONS[unit]
    if set(values) != set(table):
        raise ValueError(
            f"{unit}: rules and fields.X_FUNCTIONS differ in their head values"
        )
    return {
        h: XRule(
            conf,
            f"{text}; {note}" if note else text,
            values[h],
            _X_READS[unit].get(h, "ab"),
        )
        for h, (text, conf, note) in table.items()
    }


X_RULES = {unit: _x_rules(unit) for unit in X_FUNCTIONS}
"""X0's and X1's functions by head value, each with its tier (`fields.X_FUNCTIONS`): a and b
the unit's register operands, K its immediate (`fields.X_IMMEDIATES`)."""


def _sk(unit: str, k: int) -> int:
    """X unit `unit`'s raw immediate k sign-extended from its width; raises ValueError for a k
    wider than that."""
    width = X_IMMEDIATES[unit].width
    if not 0 <= k < 1 << width:
        raise ValueError(f"{unit}: immediate {k:#x} does not fit {width} bits")
    return k - (1 << width) if k >> (width - 1) else k


def x_result(unit: str, head: int, a: int, b: int, k: int) -> int | None:
    """The 32-bit result X unit `unit` puts out at head value `head` on operands a, b and the
    raw immediate k; None where the head gives no result. Raises KeyError at a head value
    without a rule (the idle value), ValueError for a k wider than the unit's immediate."""
    rule = X_RULES[unit][head]
    if rule.value is None:
        return None
    return _u32(rule.value(a, b, k, _sk(unit, k)))


LANE_COUNT = 32
LANE_MASK = 0xFFFF
Lanes = list[int | None]
"""The 32 lanes of a lane register, 16 bits each; None where a rule does not predict a lane."""
Scalars = Mapping[str, int]
LaneValue = Callable[[Sequence[int], Sequence[int], Scalars], Lanes]
"""A lane function of (a, b, scalars)."""


def _s16(v: int) -> int:
    v &= LANE_MASK
    return v - 0x10000 if v & 0x8000 else v


def _sat_s(v: int) -> int:
    return max(-0x8000, min(0x7FFF, v))


def _sat_u(v: int) -> int:
    return max(0, min(LANE_MASK, v))


def _round_even(v: int, n: int) -> int:
    """v divided by 2^n, rounded to the nearest integer and a tie to the even one."""
    q, r = divmod(v, 1 << n)
    if 2 * r > 1 << n or (2 * r == 1 << n and q % 2):
        q += 1
    return q


def _signed(f: Callable[[int, int, Scalars], int]) -> LaneValue:
    return lambda a, b, c: [f(_s16(x), _s16(y), c) for x, y in zip(a, b, strict=True)]


def _unsigned(f: Callable[[int, int, Scalars], int]) -> LaneValue:
    return lambda a, b, c: [f(x, y, c) for x, y in zip(a, b, strict=True)]


def _wide(f: Callable[[int, Scalars], int]) -> LaneValue:
    """f on the 32-bit value a : b, a the signed high half."""
    return lambda a, b, c: [f(_s16(x) << 16 | y, c) for x, y in zip(a, b, strict=True)]


def _next(f: Callable[[int, int], int], of_a: bool) -> LaneValue:
    """f(a, b) on signed lanes with the next lane of a (or of b); lane 31's is not predicted."""

    def value(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
        pairs = zip(a[1:], b) if of_a else zip(a, b[1:])
        return [f(_s16(x), _s16(y)) for x, y in pairs] + [None]

    return value


def _open(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
    return [None] * LANE_COUNT


def _lookup(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
    """Lane k := b[(k & 16) + a[k]] where a[k] < 16, else 0: a table lookup in each half."""
    return [b[(k & 16) + a[k]] if a[k] < 16 else 0 for k in range(LANE_COUNT)]


def _block_select(size: int) -> LaneValue:
    """Block j of `size` lanes := b's block a[j] where a[j] is a block number, else 0: the
    block numbers sit in a's first lanes, one per output block."""
    blocks = LANE_COUNT // size

    def select(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
        out: Lanes = []
        for j in range(blocks):
            src = a[j]
            out += b[src * size : (src + 1) * size] if src < blocks else [0] * size
        return out

    return select


def _lane_numbers(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
    return list(range(LANE_COUNT))


def _insert(b: Sequence[int], lane: int, value: int) -> Lanes:
    out: Lanes = list(b)
    out[lane & (LANE_COUNT - 1)] = value
    return out


def _imm14(c: Scalars) -> int:
    return S_IMMEDIATES["s3"].compose(c)


def _lane_shift(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
    """b moved by f92_95 as a signed nibble; the lanes moved in, and a move by 0, are not
    predicted."""
    n = _s16(c["f92_95"] << 12) >> 12
    if n == 0:
        return [None] * LANE_COUNT
    if abs(n) > 4:
        return list(b)
    if n > 0:
        return [*b[n:], *[None] * n]
    return [*[None] * -n, *b[:n]]


SHIFT_COUNTS = (1, 2, 3, 4, 5, 8, 11, 12, 13, 14, 15)
"""The counts f92_95 at which S3 0x1c and 0x22 were pre-registered as 0x1b."""


def _counted_shift(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
    """`_lane_shift` at `SHIFT_COUNTS` only; other counts are not predicted."""
    if c["f92_95"] not in SHIFT_COUNTS:
        return [None] * LANE_COUNT
    return _lane_shift(a, b, c)


def _n(c: Scalars) -> int:
    return c["imm"] & 15


def _t(c: Scalars) -> int:
    return c["t"] & 15


def _u(c: Scalars, key: str) -> int:
    return c[key] & LANE_MASK


def _sv(c: Scalars, key: str) -> int:
    return _s16(c[key])


def _select(other: Callable[[Sequence[int], Scalars], Sequence[int]]) -> LaneValue:
    """b where bit k of S0's 32-bit scalar s is set, else lane k of `other`(a, scalars)."""

    def value(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
        alt = other(a, c)
        return [b[k] if c["s"] >> k & 1 else alt[k] for k in range(LANE_COUNT)]

    return value


def _carry_in(f: Callable[[int, int, int], int], signed: bool) -> LaneValue:
    """f(a, b, c) with c bit k of the 32-bit carry-in mask "r" for lane k."""

    def value(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
        conv = _s16 if signed else int
        return [
            f(conv(x), conv(y), c["r"] >> k & 1)
            for k, (x, y) in enumerate(zip(a, b, strict=True))
        ]

    return value


def _clamp(lo: Callable[[int], int], hi: Callable[[int, Scalars], int]) -> LaneValue:
    """clamp(b, lo(h), h) with h = hi(a, scalars) signed; a lane whose bound h is negative is not
    predicted."""

    def value(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
        out: Lanes = []
        for x, y in zip(a, b, strict=True):
            h = hi(_s16(x), c)
            out.append(None if h < 0 else min(max(_s16(y), lo(h)), h))
        return out

    return value


def _wrapped_clamp(neg: Callable[[int, int], int]) -> LaneValue:
    """S0 0xa / 0xd on signed lanes: for b >= 0, b where b - a has its 16-bit sign bit set,
    else a; for b < 0, neg(a, b)."""

    def lane(x: int, y: int, c: Scalars) -> int:
        if y < 0:
            return neg(x, y)
        return y if (y - x) & 0x8000 else x

    return _signed(lane)


def _floor_carry(n: Callable[[Scalars], int]) -> LaneValue:
    """((a : b) >> n) + c with c bit k of the carry-in mask "r" for lane k."""

    def value(a: Sequence[int], b: Sequence[int], c: Scalars) -> Lanes:
        return [
            ((_s16(x) << 16 | y) >> n(c)) + (c["r"] >> k & 1)
            for k, (x, y) in enumerate(zip(a, b, strict=True))
        ]

    return value


def _zero(h: int) -> int:
    return 0


def _not(h: int) -> int:
    return ~h


_CARRY_SAT_S = _carry_in(lambda x, y, k: _sat_s(x + y + k), signed=True)
_CARRY_WRAP = _carry_in(lambda x, y, k: x + y + k, signed=False)
_CARRY_SAT_U = _carry_in(lambda x, y, k: _sat_u(x + y + k), signed=False)
_B = _signed(lambda x, y, c: y)
_ABS_DIFF = _signed(lambda x, y, c: _sat_s(abs(x - y)))
_SUB = _signed(lambda x, y, c: _sat_s(y - x))
_ADD = _signed(lambda x, y, c: _sat_s(x + y))
_NEGATIVE = _signed(lambda x, y, c: -(y < 0))
_NOT_NEGATIVE = _signed(lambda x, y, c: -(y >= 0))

_LANE_VALUES: dict[str, dict[int, LaneValue | None]] = {
    "s0": {
        0x00: _next(lambda x, y: _sat_s(abs(x - y)), of_a=True),
        0x01: _next(lambda x, y: _sat_s(abs(x - y)), of_a=False),
        0x02: _next(lambda x, y: _sat_s(y - x), of_a=True),
        0x03: _next(lambda x, y: _sat_s(y - x), of_a=False),
        0x04: _next(lambda x, y: _sat_s(x + y), of_a=True),
        0x05: _ABS_DIFF,
        0x21: _ABS_DIFF,
        0x06: _SUB,
        0x28: _SUB,
        0x07: _ADD,
        0x29: _ADD,
        0x08: _clamp(_zero, lambda x, c: _sv(c, "imm")),
        0x09: _clamp(_zero, lambda x, c: _sv(c, "s")),
        0x0A: _wrapped_clamp(lambda x, y: 0),
        0x0B: _clamp(_not, lambda x, c: _sv(c, "imm")),
        0x0C: _clamp(_not, lambda x, c: _sv(c, "s")),
        0x0D: _wrapped_clamp(lambda x, y: ~x if (y + x + 1) & 0x8000 else y),
        0x0E: _signed(lambda x, y, c: max(y, _sv(c, "imm"))),
        0x0F: _signed(lambda x, y, c: min(y, _sv(c, "imm"))),
        0x10: _signed(lambda x, y, c: _sat_s(y - _sv(c, "imm"))),
        0x11: _signed(lambda x, y, c: _sat_s(y + _sv(c, "imm"))),
        **dict.fromkeys((0x12, 0x13, 0x18, 0x19, 0x24, 0x25, *range(0x2E, 0x34))),
        0x14: _signed(lambda x, y, c: _round_even(y - _sv(c, "s"), 1)),
        0x15: _signed(lambda x, y, c: _round_even(y + _sv(c, "s"), 1)),
        0x16: _signed(lambda x, y, c: max(y, _sv(c, "s"))),
        0x17: _signed(lambda x, y, c: min(y, _sv(c, "s"))),
        0x1A: _signed(lambda x, y, c: _sat_s(y - _sv(c, "s"))),
        0x1B: _signed(lambda x, y, c: _sat_s(y + _sv(c, "s"))),
        0x1D: _signed(lambda x, y, c: _round_even(x + y, 1)),
        0x1C: _signed(lambda x, y, c: _round_even(y - x, 1)),
        **dict.fromkeys((0x1E, 0x2A), _CARRY_SAT_S),
        **dict.fromkeys((0x1F, 0x2B), _CARRY_WRAP),
        **dict.fromkeys((0x2C, 0x2D), _CARRY_SAT_U),
        0x20: _signed(lambda x, y, c: x + y),
        0x22: _signed(lambda x, y, c: max(x, y)),
        0x23: _signed(lambda x, y, c: min(x, y)),
        0x26: _signed(lambda x, y, c: _sat_s(abs(x))),
        0x27: _signed(lambda x, y, c: -x),
        0x34: _unsigned(lambda x, y, c: max(y, _u(c, "imm"))),
        0x35: _unsigned(lambda x, y, c: min(y, _u(c, "imm"))),
        0x36: _unsigned(lambda x, y, c: max(y, _u(c, "s"))),
        0x37: _unsigned(lambda x, y, c: min(y, _u(c, "s"))),
        0x38: _unsigned(lambda x, y, c: max(x, y)),
        0x39: _unsigned(lambda x, y, c: min(x, y)),
        0x3A: _unsigned(lambda x, y, c: _sat_u(y - _u(c, "imm"))),
        0x3B: _unsigned(lambda x, y, c: _sat_u(y + _u(c, "imm"))),
        0x3C: _unsigned(lambda x, y, c: _sat_u(y - _u(c, "s"))),
        0x3D: _unsigned(lambda x, y, c: _sat_u(y + _u(c, "s"))),
        0x3E: _unsigned(lambda x, y, c: _sat_u(y - x)),
        0x3F: _unsigned(lambda x, y, c: _sat_u(x + y)),
        0x41: _unsigned(lambda x, y, c: y),
        0x4D: _unsigned(lambda x, y, c: y),
        0x42: _unsigned(lambda x, y, c: c["imm"]),
        0x43: _unsigned(lambda x, y, c: c["r"]),
        0x44: _select(lambda a, c: [c["imm"]] * LANE_COUNT),
        0x45: _select(lambda a, c: [c["r"]] * LANE_COUNT),
        0x46: _select(lambda a, c: a),
        0x4B: lambda a, b, c: [c["s"] >> k & 1 for k in range(LANE_COUNT)],
        **dict.fromkeys(range(0x60, 0x7F), lambda a, b, c: list(a)),
    },
    "s1": {
        0x00: _signed(lambda x, y, c: ~y),
        0x01: _signed(lambda x, y, c: x ^ y),
        0x02: _signed(lambda x, y, c: x | y),
        0x03: _signed(lambda x, y, c: x & y),
        0x04: _unsigned(lambda x, y, c: y ^ _u(c, "imm")),
        0x05: _unsigned(lambda x, y, c: y | _u(c, "imm")),
        0x06: _unsigned(lambda x, y, c: y & _u(c, "imm")),
        0x07: _unsigned(lambda x, y, c: y ^ _u(c, "t")),
        0x08: _unsigned(lambda x, y, c: y | _u(c, "t")),
        0x09: _unsigned(lambda x, y, c: y & _u(c, "t")),
        **dict.fromkeys((0x0A, 0x13), _signed(lambda x, y, c: _sat_s(y << _n(c)))),
        0x0B: _signed(lambda x, y, c: y << _n(c)),
        0x0C: _unsigned(lambda x, y, c: y >> _n(c)),
        0x0D: _signed(lambda x, y, c: y >> _n(c)),
        0x0E: _signed(lambda x, y, c: _round_even(y, _n(c))),
        0x0F: _wide(lambda v, c: _sat_s(v >> _n(c))),
        0x10: _wide(lambda v, c: _sat_s(_round_even(v, _n(c)))),
        **dict.fromkeys((0x11, 0x12), _unsigned(lambda x, y, c: _sat_u(y << _n(c)))),
        **dict.fromkeys((0x14, 0x15), _wide(lambda v, c: _round_even(v, _n(c)))),
        **dict.fromkeys(range(0x16, 0x1A), _floor_carry(_n)),
        **dict.fromkeys((0x1A, 0x1C), _unsigned(lambda x, y, c: _sat_u(y << _t(c)))),
        **dict.fromkeys((0x1B, 0x23), _signed(lambda x, y, c: _sat_s(y << _t(c)))),
        **dict.fromkeys((0x1D, 0x1E), _wide(lambda v, c: _round_even(v, _t(c)))),
        **dict.fromkeys(range(0x1F, 0x23), _floor_carry(_t)),
        0x24: _signed(lambda x, y, c: y << _t(c)),
        0x25: _unsigned(lambda x, y, c: y >> _t(c)),
        0x26: _signed(lambda x, y, c: y >> _t(c)),
        0x27: _signed(lambda x, y, c: _round_even(y, _t(c))),
        0x28: _wide(lambda v, c: _sat_s(v >> _t(c))),
        0x29: _wide(lambda v, c: _sat_s(_round_even(v, _t(c)))),
        0x2A: _signed(lambda x, y, c: max(y, _sv(c, "imm"))),
        0x2B: _signed(lambda x, y, c: min(y, _sv(c, "imm"))),
        0x2C: _signed(lambda x, y, c: max(y, _sv(c, "t"))),
        0x2D: _signed(lambda x, y, c: min(y, _sv(c, "t"))),
        0x2E: _signed(lambda x, y, c: max(x, y)),
        0x2F: _signed(lambda x, y, c: min(x, y)),
        0x30: _signed(lambda x, y, c: _sat_s(y - _sv(c, "imm"))),
        0x31: _signed(lambda x, y, c: _sat_s(y + _sv(c, "imm"))),
        **dict.fromkeys((0x32, 0x3D), _CARRY_SAT_S),
        **dict.fromkeys((0x33, 0x3E), _CARRY_WRAP),
        **dict.fromkeys((0x3F, 0x40), _CARRY_SAT_U),
        0x34: _signed(lambda x, y, c: x + y),
        0x35: _signed(lambda x, y, c: _round_even(y + _sv(c, "t"), 1)),
        0x36: _signed(lambda x, y, c: _sat_s(y - _sv(c, "t"))),
        0x37: _signed(lambda x, y, c: _sat_s(y + _sv(c, "t"))),
        0x38: _signed(lambda x, y, c: _round_even(x + y, 1)),
        0x39: _signed(lambda x, y, c: _sat_s(abs(x))),
        0x3A: _signed(lambda x, y, c: -x),
        0x3B: _SUB,
        0x3C: _ADD,
        **dict.fromkeys((*range(0x41, 0x47), 0x54)),
        0x47: _unsigned(lambda x, y, c: max(y, _u(c, "imm"))),
        0x48: _unsigned(lambda x, y, c: min(y, _u(c, "imm"))),
        0x49: _unsigned(lambda x, y, c: max(y, _u(c, "t"))),
        0x4A: _unsigned(lambda x, y, c: min(y, _u(c, "t"))),
        0x4B: _unsigned(lambda x, y, c: max(x, y)),
        0x4C: _unsigned(lambda x, y, c: min(x, y)),
        0x4D: _unsigned(lambda x, y, c: _sat_u(y - _u(c, "imm"))),
        0x4E: _unsigned(lambda x, y, c: _sat_u(y + _u(c, "imm"))),
        0x4F: _unsigned(lambda x, y, c: _sat_u(y - _u(c, "t"))),
        0x50: _unsigned(lambda x, y, c: _sat_u(y + _u(c, "t"))),
        0x51: _unsigned(lambda x, y, c: _sat_u(y - x)),
        0x52: _unsigned(lambda x, y, c: _sat_u(x + y)),
        0x53: _B,
        **dict.fromkeys(range(0x55, 0x60), _open),
        **dict.fromkeys(range(0x60, 0x7F), lambda a, b, c: list(b)),
    },
    "s2": {
        **dict.fromkeys((0x00, 0x01), _signed(lambda x, y, c: y * c["imm"])),
        **dict.fromkeys((0x02, 0x04, 0x06), _signed(lambda x, y, c: x * y)),
        **dict.fromkeys((0x03, 0x05), _signed(lambda x, y, c: y * c["t"])),
        0x07: lambda a, b, c: list(b if c["f88_91"] & 2 else a),
        0x08: _B,
        0x0D: _B,
        0x09: _NEGATIVE,
        0x0A: _lane_numbers,
        0x0B: _NOT_NEGATIVE,
        0x0C: None,
        0x0E: None,
    },
    "s3": {
        **dict.fromkeys((*range(0x06), 0x0E, 0x0F, 0x16, 0x17, 0x1A, 0x1D, 0x1E, 0x1F)),
        **dict.fromkeys(range(0x24, 0x3F)),
        0x06: lambda a, b, c: [0] * LANE_COUNT,
        0x07: _block_select(8),
        0x08: _block_select(16),
        0x09: _lookup,
        0x0A: lambda a, b, c: [v for k in range(16) for v in (b[k], a[k])],
        0x0B: lambda a, b, c: [v for k in range(16, 32) for v in (b[k], a[k])],
        0x0C: lambda a, b, c: [*b[1::2], *a[1::2]],
        0x0D: lambda a, b, c: [*b[0::2], *a[0::2]],
        0x10: lambda a, b, c: _insert(b, _imm14(c) & 31, c["value"]),
        0x11: lambda a, b, c: [_imm14(c)] * LANE_COUNT,
        0x12: _NEGATIVE,
        0x13: _NOT_NEGATIVE,
        0x14: _lane_numbers,
        0x15: lambda a, b, c: _insert(b, c["index"], c["value"]),
        0x18: lambda a, b, c: [c["value"]] * LANE_COUNT,
        **dict.fromkeys((0x19, 0x20, 0x21, 0x23), _B),
        0x1B: _lane_shift,
        **dict.fromkeys((0x1C, 0x22), _counted_shift),
    },
}


@dataclass(frozen=True)
class LaneRule:
    """What an S unit's head value does to the lanes: `value` of (a, b, scalars), None for a
    head that writes no lane result; `reads` the lane operands it reads ("a", "b")."""

    confidence: Confidence
    fact: str
    value: LaneValue | None
    reads: str = "ab"


_LANE_READS = {
    "s0": _reads(
        none=(0x42, 0x43, 0x4B),
        a=(0x26, 0x27, *range(0x60, 0x7F)),
        b=(
            *(0x08, 0x09, 0x0B, 0x0C, 0x0E, 0x0F, 0x10, 0x11, 0x14, 0x15, 0x16, 0x17),
            *(0x1A, 0x1B, 0x34, 0x35, 0x36, 0x37, 0x3A, 0x3B, 0x3C, 0x3D, 0x41, 0x4D),
            *(0x44, 0x45),
        ),
    ),
    "s1": _reads(
        none=range(0x55, 0x60),
        a=(0x39, 0x3A),
        b=(
            0x00,
            *range(0x04, 0x0F),
            0x11,
            0x12,
            0x13,
            0x1A,
            0x1B,
            0x1C,
            *range(0x23, 0x28),
            0x2A,
            0x2B,
            0x2C,
            0x2D,
            0x30,
            0x31,
            0x35,
            0x36,
            0x37,
            *range(0x47, 0x4B),
            0x4D,
            0x4E,
            0x4F,
            0x50,
            0x53,
            *range(0x60, 0x7F),
        ),
    ),
    "s2": _reads(none=(0x0A,), b=(0x00, 0x01, 0x03, 0x05, 0x08, 0x09, 0x0B, 0x0D)),
    "s3": _reads(
        none=(0x06, 0x11, 0x14, 0x18),
        b=(0x10, 0x12, 0x13, 0x15, 0x19, 0x1B, 0x1C, 0x20, 0x21, 0x22, 0x23),
    ),
}
"""The lane operands each S head's lane rule reads, from its rule; a head not listed reads both."""


def _lane_rules(unit: str) -> dict[int, LaneRule]:
    values = _LANE_VALUES[unit]
    table = LANE_FUNCTIONS[unit]
    if set(values) != set(table):
        raise ValueError(
            f"{unit}: rules and fields.LANE_FUNCTIONS differ in their head values"
        )
    if any(
        (values[h] is None) != (text == NO_LANE) for h, (text, _, _) in table.items()
    ):
        raise ValueError(
            f"{unit}: rules and fields.LANE_FUNCTIONS differ in which heads write"
        )
    return {
        h: LaneRule(
            conf,
            f"{text}; {note}" if note else text,
            values[h],
            _LANE_READS[unit].get(h, "ab"),
        )
        for h, (text, conf, note) in table.items()
    }


LANE_RULES = {unit: _lane_rules(unit) for unit in LANE_FUNCTIONS}
"""The S units' lane functions by head value, each with its tier (`fields.LANE_FUNCTIONS`)."""


TIES_MET = range(1, 4)
"""The counts at which S1's rounding shifts by imm & 15 met ties to even; at the others a tie
lane is inferred (`fields.TIES_AT_10`)."""


def _ties_by_imm(a: Sequence[int], b: Sequence[int], c: Scalars) -> Confidence:
    # The count is under 16, so the bits rounded off are b's in the 32-bit a : b too.
    n = _n(c)
    if n == 0 or n in TIES_MET:
        return V
    half = 1 << (n - 1)
    return I if any(y & (2 * half - 1) == half for y in b) else V


LANE_CAPS: dict[
    tuple[str, int], Callable[[Sequence[int], Sequence[int], Scalars], Confidence]
] = {("s1", h): _ties_by_imm for h in (0x0E, 0x10, 0x14, 0x15)}
"""Rows whose tier depends on their operands' values: the cap at given lanes and scalars."""


def lane_tier(
    unit: str, head: int, a: Sequence[int], b: Sequence[int], scalars: Scalars
) -> Confidence:
    """The tier of S unit `unit`'s lane result at head value `head` on lanes a and b and
    `scalars`: the row's, capped where it depends on the values (`LANE_CAPS`)."""
    tier = LANE_RULES[unit][head].confidence
    cap = LANE_CAPS.get((unit, head))
    return tier if cap is None else min(tier, cap(a, b, scalars))


def lane_result(
    unit: str, head: int, a: Sequence[int], b: Sequence[int], scalars: Scalars
) -> Lanes | None:
    """The 32 lanes S unit `unit` writes at head value `head` from its lane operands a and b
    (32 lanes of 0..0xffff each); None where the head writes no lane result, a None lane where
    the rule does not predict it (the open heads give 32). Raises KeyError at a head value
    without a rule.

    `scalars` holds what the rules read, by unit: S0 "s" (F0[f72_75], whose 32 bits the select
    heads read as a lane mask), "r" (F0[f76_79], whose 32 bits the carry-in heads read as their
    carry-in mask) and "imm"; S1 "t" (F1[f84_87]), "r" (F1[f80_83], the carry-in mask of the carry-in heads and the
    floor shifts) and "imm"; S2 "t" (F2[f88_91]), "imm" (0x0 / 0x1's multiplier) and the
    nibble "f88_91" (0x7's choice); S3 the
    nibbles "f31_34", "f65_68", "f92_95", "f96_99" and "index" = F3[f92_95], "value" =
    F3[f96_99]. A scalar enters with its low 16 bits (S2's t verified so, its bits 16..31 not
    read; how the others' wider values enter is open), but the select heads read all 32 bits of "s" and the carry-in heads and S1's floor shifts all
    32 of "r" as a lane mask; a shift count enters as imm & 15
    or t & 15."""
    if len(a) != LANE_COUNT or len(b) != LANE_COUNT:
        raise ValueError(f"{len(a)} and {len(b)} lanes: expected {LANE_COUNT} each")
    rule = LANE_RULES[unit][head]
    if rule.value is None:
        return None
    return [None if v is None else v & LANE_MASK for v in rule.value(a, b, scalars)]


_HIGH_VALUES: dict[int, LaneValue] = {
    0x02: _unsigned(lambda x, y, c: x * _s16(y) >> 16),
    0x05: _signed(lambda x, y, c: y * _sv(c, "t") >> 16),
    0x00: _unsigned(lambda x, y, c: y * (c["imm"] & LANE_MASK) >> 16),
    0x01: _signed(lambda x, y, c: y * _sv(c, "imm") >> 16),
    0x03: _unsigned(lambda x, y, c: y * (c["t"] & LANE_MASK) >> 16),
    0x04: _unsigned(lambda x, y, c: x * y >> 16),
    0x06: _signed(lambda x, y, c: x * y >> 16),
}
if set(_HIGH_VALUES) != set(S2_HIGH_HALVES):
    raise ValueError(
        "high halves and fields.S2_HIGH_HALVES differ in their head values"
    )


def product_high(
    head: int, a: Sequence[int], b: Sequence[int], scalars: Scalars
) -> Lanes:
    """The high halves of S2's 32-bit products at head value `head` (`fields.S2_HIGH_HALVES`),
    which the first ports of L0 and L1 take under enable 1; `lane_result` gives the low halves.
    Raises KeyError at a head whose high half is not known."""
    if len(a) != LANE_COUNT or len(b) != LANE_COUNT:
        raise ValueError(f"{len(a)} and {len(b)} lanes: expected {LANE_COUNT} each")
    return [v & LANE_MASK for v in _HIGH_VALUES[head](a, b, scalars)]


class UnknownInput(LookupError):
    """A rule read an input (a lane, a scalar) that is not known."""


@dataclass(frozen=True)
class ScalarValue:
    """An S head's scalar result `run`(a, b, scalars), reading the lane operands `reads` ("a",
    "b"); a lane it reads that is None raises `UnknownInput`."""

    reads: str
    run: Callable[[Sequence[int | None], Sequence[int | None], Scalars], int]


def _known(v: Sequence[int | None], name: str) -> list[int]:
    if any(x is None for x in v):
        raise UnknownInput(name)
    return list(v)


def _bits(tests: Iterable[bool]) -> int:
    return sum(1 << k for k, t in enumerate(tests) if t)


def _mask_ab(f: Callable[[int, int], bool]) -> ScalarValue:
    """Bit k from f(lane k of a, lane k of b)."""
    return ScalarValue(
        "ab", lambda a, b, c: _bits(map(f, _known(a, "a"), _known(b, "b")))
    )


def _mask_b(f: Callable[[int, Scalars], bool]) -> ScalarValue:
    """Bit k from f(lane k of b, scalars)."""
    return ScalarValue("b", lambda a, b, c: _bits(f(y, c) for y in _known(b, "b")))


def _carry_mask(carry_in: bool) -> ScalarValue:
    """The carries out of a + b per lane, with carry-in bit k of "r" for lane k."""

    def value(a: Sequence[int | None], b: Sequence[int | None], c: Scalars) -> int:
        r = c["r"] if carry_in else 0
        pairs = enumerate(zip(_known(a, "a"), _known(b, "b")))
        return _bits((x + y + (r >> k & 1)) >> 16 for k, (x, y) in pairs)

    return ScalarValue("ab", value)


def _overflows(a: Sequence[int | None], b: Sequence[int | None], c: Scalars) -> int:
    """The signed overflows of a + b per lane, with carry-in bit k of "r" for lane k."""
    pairs = enumerate(zip(_known(a, "a"), _known(b, "b")))
    return _bits(
        not -0x8000 <= _s16(x) + _s16(y) + (c["r"] >> k & 1) <= 0x7FFF
        for k, (x, y) in pairs
    )


_OVERFLOW_MASK = ScalarValue("ab", _overflows)


def _lane_of(index: Callable[[Scalars], int], signed: bool) -> ScalarValue:
    """Lane index(scalars) & 31 of b, zero- or sign-extended."""

    def value(a: Sequence[int | None], b: Sequence[int | None], c: Scalars) -> int:
        k = index(c) & 31
        x = b[k]
        if x is None:
            raise UnknownInput(f"lane {k}")
        return _s16(x) if signed else x

    return ScalarValue("b", value)


def _reduce(f: Callable[[list[int]], int], signed: bool) -> ScalarValue:
    def value(a: Sequence[int | None], b: Sequence[int | None], c: Scalars) -> int:
        lanes = _known(b, "b")
        return f([_s16(x) for x in lanes] if signed else lanes)

    return ScalarValue("b", value)


def _scalar(key: str) -> ScalarValue:
    return ScalarValue("", lambda a, b, c: c[key])


_S_VALUES: dict[str, dict[int, ScalarValue | None]] = {
    "s0": {
        0x52: _scalar("r"),
        0x24: _mask_ab(lambda x, y: x != y),
        0x25: _mask_ab(lambda x, y: x == y),
        0x32: _mask_ab(lambda x, y: x < y),
        0x33: _mask_ab(lambda x, y: x >= y),
        0x2E: _mask_b(lambda y, c: y > c["imm"]),
        0x2F: _mask_b(lambda y, c: y <= c["imm"]),
        0x30: _mask_b(lambda y, c: y > _u(c, "s")),
        0x31: _mask_b(lambda y, c: y <= _u(c, "s")),
        0x12: _mask_b(lambda y, c: y != c["imm"]),
        0x13: _mask_b(lambda y, c: y == c["imm"]),
        0x18: _mask_b(lambda y, c: y != _u(c, "s")),
        0x19: _mask_b(lambda y, c: y == _u(c, "s")),
        0x20: _carry_mask(carry_in=False),
        0x1F: _carry_mask(carry_in=True),
        0x2C: _carry_mask(carry_in=True),
        0x2A: _OVERFLOW_MASK,
        0x22: _mask_ab(lambda x, y: _s16(x) < _s16(y)),
        0x23: _mask_ab(lambda x, y: _s16(x) >= _s16(y)),
        0x16: _mask_b(lambda y, c: _s16(y) > _sv(c, "s")),
        0x17: _mask_b(lambda y, c: _s16(y) <= _sv(c, "s")),
        0x0E: _mask_b(lambda y, c: _s16(y) > _sv(c, "imm")),
        0x0F: _mask_b(lambda y, c: _s16(y) <= _sv(c, "imm")),
    },
    "s1": {
        0x54: _scalar("t"),
        0x34: _carry_mask(carry_in=False),
        0x33: _carry_mask(carry_in=True),
        0x3F: _carry_mask(carry_in=True),
        0x3D: _OVERFLOW_MASK,
        0x2E: _mask_ab(lambda x, y: _s16(x) < _s16(y)),
        0x2F: _mask_ab(lambda x, y: _s16(x) >= _s16(y)),
        0x2C: _mask_b(lambda y, c: _s16(y) > _sv(c, "t")),
        0x2D: _mask_b(lambda y, c: _s16(y) <= _sv(c, "t")),
        0x2A: _mask_b(lambda y, c: _s16(y) > _sv(c, "imm")),
        0x2B: _mask_b(lambda y, c: _s16(y) <= _sv(c, "imm")),
        0x12: None,
        0x41: _mask_b(lambda y, c: y > _u(c, "imm")),
        0x42: _mask_b(lambda y, c: y <= _u(c, "imm")),
        0x43: _mask_b(lambda y, c: y > _u(c, "t")),
        0x44: _mask_b(lambda y, c: y <= _u(c, "t")),
        0x45: _mask_ab(lambda x, y: x < y),
        0x46: _mask_ab(lambda x, y: x >= y),
    },
    "s2": {
        0xC: _lane_of(lambda c: S_IMMEDIATES["s2"].compose(c) & 31, signed=True),
        0xE: _scalar("t"),
    },
    "s3": {
        0x0: _reduce(max, signed=False),
        0x1: _reduce(min, signed=False),
        0x2: _reduce(sum, signed=False),
        0x3: _reduce(max, signed=True),
        0x4: _reduce(min, signed=True),
        0x5: _reduce(sum, signed=True),
        0xE: _lane_of(lambda c: _imm14(c) & 31, signed=False),
        0xF: _lane_of(lambda c: _imm14(c) & 31, signed=True),
        0x16: _lane_of(lambda c: c["index"], signed=False),
        0x17: _lane_of(lambda c: c["index"], signed=True),
        0x24: _scalar("value"),
    },
}


@dataclass(frozen=True)
class SRule:
    """An S head's scalar result on M (`fields.S_RESULTS`): `value` of (a, b, scalars), None
    where the rule is not exact; on M from `lag` words after the head, for that word only or
    `held` from then on."""

    confidence: Confidence
    fact: str
    lag: int
    held: bool
    value: ScalarValue | None


def _s_rules(unit: str) -> dict[int, SRule]:
    values = _S_VALUES[unit]
    table = S_RESULTS[unit]
    if set(values) != set(table):
        raise ValueError(
            f"{unit}: scalar rules and fields.S_RESULTS differ in their heads"
        )
    return {
        h: SRule(row.tier, row.text, row.lag, row.held, values[h])
        for h, row in table.items()
    }


S_RULES = {unit: _s_rules(unit) for unit in S_RESULTS}
"""The S units' scalar results on M by head value, each with its tier and timing."""


def s_result(
    unit: str,
    head: int,
    a: Sequence[int | None],
    b: Sequence[int | None],
    scalars: Scalars,
) -> int | None:
    """The 32-bit scalar S unit `unit` puts on M at head value `head` from its lane operands a
    and b (`fields.S_RESULTS`); None where the rule is not exact. The operands are as in
    `lane_result`, plus S2's nibble "f53_56" for its lane extract. Raises `UnknownInput` where it reads a lane that is None, KeyError at a head
    value without a scalar result."""
    if len(a) != LANE_COUNT or len(b) != LANE_COUNT:
        raise ValueError(f"{len(a)} and {len(b)} lanes: expected {LANE_COUNT} each")
    rule = S_RULES[unit][head]
    if rule.value is None:
        return None
    return rule.value.run(a, b, scalars) & 0xFFFFFFFF


def lane_address(head: int, a: int, b: int, k: int) -> int:
    """The lane memory vector X0's load or store head `head` addresses (`fields.LANE_MEMORY`):
    a + b at 0x1e..0x21, a + sK (k sign-extended from X0's 11 bits) at 0x1a..0x1d, modulo
    `LANE_VECTORS`. Raises KeyError at any other head, ValueError for a k over 11 bits."""
    if head not in X0_LANE_MEMORY:
        raise KeyError(head)
    if head in X0_LANE_BY_REGISTER:
        return (a + b) % LANE_VECTORS
    return (a + _sk("x0", k)) % LANE_VECTORS


LATCHED = range(0x60, 0x7F)
"""S0's and S1's heads that give the operand their unit's last head read in the job, latched
(S0 its a, S1 its b; 0 with none), not their own nibbles' (verified on the camera)."""
LATCH_OPERAND = {"s0": "a", "s1": "b"}
LANE_SHIFTS = (0x1B, 0x1C, 0x22)
"""S3's lane shifts, which fill the lanes moved in from U[f31_34]."""
U_PORT = lookup("f232_233")
"""U's port, which takes what the S3 head beside it saves under 1 (`fields.U_TAKES`)."""

_X_IMMEDIATE_READS = {
    "x0": (*range(8), *range(0x12, 0x15), *range(0x1A, 0x1E)),
    "x1": (*range(8), *range(0x12, 0x15)),
}
"""The X heads whose rule reads K (`X_RULES`)."""


def _x_head_reads(unit: str, head: int) -> frozenset[str] | None:
    rule = X_RULES[unit][head]
    if unit == "x1" and head in TRANSFER_HEADS:
        # 0x1c hands B[b] to the stream engine, 0x1d hands nothing.
        return frozenset("b" if head == 0x1C else "")
    if unit == "x1" and head == DIVIDE_HEAD:
        return frozenset("ab")
    if unit == "x0" and head in X0_LANE_MEMORY:
        address = "ab" if head in X0_LANE_BY_REGISTER else "ak"
        store = "" if head in X0_LANE_LOADS else "o"
    elif rule.value is None:
        return None
    else:
        address = store = ""
    k = "k" if head in _X_IMMEDIATE_READS[unit] else ""
    return frozenset(rule.reads + address + store + k)


class _Recorder(Mapping):
    """Scalars that are all 1, recording the keys a rule reads."""

    def __init__(self) -> None:
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> int:
        self.read.add(key)
        return 1

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


_SCALAR_OPERANDS = {"index": "idx", "value": "val"}
"""A scalar's operand where their names differ: S3's F3[idx] and F3[val]."""
_NIBBLE_READS = {
    ("s2", 0x07): "sel",
    ("s2", 0x0C): "imm",
    **{("s3", h): "lane" for h in (0x0E, 0x0F, 0x10)},
    ("s3", 0x11): "imm",
    **{("s3", h): "idx" for h in LANE_SHIFTS},
}
"""The operand of each head whose rules read raw nibbles of the word (the scalar keys "f..."):
S2 0x7's choice and 0xc's lane index, S3's lane of 0xe..0x10, its immediate and the shift
count."""


def _s_head_reads(unit: str, head: int) -> frozenset[str] | None:
    lane, scalar = LANE_RULES[unit].get(head), S_RULES[unit].get(head)
    if (
        (lane is None and scalar is None)
        or (lane is not None and lane.value is _open)
        or (scalar is not None and scalar.value is None)
    ):
        return None
    reads, rec, ones = set(), _Recorder(), [1] * LANE_COUNT
    if lane is not None and lane.value is not None:
        reads |= set(lane.reads)
        lane.value(ones, ones, rec)
        extras = [LANE_CAPS.get((unit, head))]
        if unit == "s2":
            extras.append(_HIGH_VALUES.get(head))
        for f in filter(None, extras):
            f(ones, ones, rec)
    if scalar is not None:
        reads |= set(scalar.value.reads)
        scalar.value.run(ones, ones, rec)
    nibbles = {k for k in rec.read if k.startswith("f")}
    if nibbles:
        reads.add(_NIBBLE_READS[unit, head])
    reads |= {_SCALAR_OPERANDS.get(k, k) for k in rec.read - nibbles}
    if unit == "s3" and head in LANE_SHIFTS:
        reads.add("u")
    if unit == "s3" and head in U_TAKES:
        reads.add("b")
    if head in LATCHED and unit in LATCH_OPERAND:
        reads -= {"a", "b"}  # the latched operand stands in for both
    return frozenset(reads)


def _head_reads(unit: str) -> dict[int, frozenset[str]]:
    if unit in X_RULES:
        heads, of = X_RULES[unit], _x_head_reads
    else:
        heads, of = set(LANE_RULES[unit]) | set(S_RULES[unit]), _s_head_reads
    return {h: r for h in sorted(heads) if (r := of(unit, h)) is not None}


HEAD_READS = {unit: _head_reads(unit) for unit in HEAD_VIEWS}
"""The operands of its unit's view (`fields.HEAD_VIEWS`, with S2's sel and S3's lane) each head
reads, where they are known: its rules' (`X_RULES`, `LANE_RULES`, `S_RULES`, the scalars as
`lane_result` names them), X0's lane-memory address and store register O[o], X1's transfer and
divide, a lane shift's U[u] and the b of an S3 head whose U port take is known. Not known, and
left out: a head without a rule, with an open lane rule (S1 0x55..0x5f), a scalar rule that is
not exact or an X rule without a result and nothing else the machine does with it. The latch is
`head_reads`'."""


def head_reads(unit: str, word: int) -> frozenset[str] | None:
    """The operands the head of `unit` in `word` reads: none for the idle head, `HEAD_READS`
    with the operand S0 / S1 latches (`LATCHED`) where it latches it; None where not known, also
    for an S3 head whose U port take is open under its port's 1."""
    view = HEAD_VIEWS[unit].operands["op"]
    head = view.get(word)
    if head == view.idle:
        return frozenset()
    reads = HEAD_READS[unit].get(head)
    if reads is None:
        return None
    if unit == "s3" and U_PORT.get(word) == 1 and head not in (*U_TAKES, *U_NOTHING):
        return None
    if unit in LATCH_OPERAND and head not in LATCHED:
        lane = LANE_RULES[unit].get(head)
        if unit == "s0" or lane is None or "b" in lane.reads:
            reads |= {LATCH_OPERAND[unit]}
    return reads


@dataclass
class LaneMemory:
    """The lane memory's content by vector (`fields.LANE_MEMORY`), a None lane where it is not
    known (power-up content). The addresses count modulo `LANE_VECTORS`."""

    vectors: dict[int, Lanes] = field(default_factory=dict)

    def load(self, address: int) -> Lanes:
        return list(self.vectors.get(address % LANE_VECTORS, [None] * LANE_COUNT))

    def store(self, address: int, lanes: Sequence[int | None]) -> None:
        if len(lanes) != LANE_COUNT:
            raise ValueError(f"{len(lanes)} lanes: expected {LANE_COUNT}")
        self.vectors[address % LANE_VECTORS] = [
            None if v is None else v & LANE_MASK for v in lanes
        ]

    def copy_in(self, vector: int, data: bytes) -> None:
        """The stock copy's load: byte i, zero-extended, to lane i % 32 of vector + i // 32;
        a last partial vector's other lanes are unknown (not measured)."""
        for start in range(0, len(data), LANE_COUNT):
            address = vector + start // LANE_COUNT
            chunk = list(data[start : start + LANE_COUNT])
            self.store(address, chunk + [None] * (LANE_COUNT - len(chunk)))

    def copy_out(self, vector: int, length: int) -> list[int | None]:
        """The stock copy's store of `length` bytes from `vector` on: each element's low 8 bits,
        None for an unknown lane."""
        lanes = [
            v for n in range(-(-length // LANE_COUNT)) for v in self.load(vector + n)
        ]
        return [None if v is None else v & 0xFF for v in lanes[:length]]


@dataclass(frozen=True)
class Branch:
    """A control op's decision: taken (None = undecided), why, and its confidence."""

    taken: bool | None
    why: str
    confidence: Confidence


class Branching(Protocol):
    """Decides the conditional control ops 0..2 (loop end, brf, br) at their `visit`-th run
    (from 0)."""

    def decide(self, index: int, visit: int, word: Word) -> Branch: ...


@dataclass(frozen=True)
class Given:
    """Decisions supplied by the caller, by word index: one for every visit, or one per visit;
    every other decision is undecided (and stepping leaves it to the datapath)."""

    decisions: Mapping[int, bool | Sequence[bool]] = field(default_factory=dict)
    confidence: Confidence = H
    fact: str = "given by the caller"

    def decide(self, index: int, visit: int, word: Word) -> Branch:
        d = self.decisions.get(index)
        if isinstance(d, Sequence):
            d = d[visit] if visit < len(d) else None
        if d is None:
            return Branch(None, f"no decision given for visit {visit}", self.confidence)
        return Branch(d, self.fact, self.confidence)
