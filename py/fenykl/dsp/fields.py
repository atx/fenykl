"""The field table of the 320-bit DSP word: the single source of truth for encoding.

Bit k of a word is bit (k % 8) of byte k // 8 of its 40 bytes. Every bit belongs to exactly one
field. A field has a permanent positional id that names both ends of its bits (`f115_119`, `f229`),
an optional semantic name that may be renamed at will, a kind, its value in the
idle word, a confidence and a one-line fact.

Confidence qualifies the field itself: that these bits form one field of this kind. Facts about
single values or words carry their own tags in `fact` and `values`. The emulator does not use
it: it propagates the confidence of the semantics a result depends on (see `semantics`).

Bits 313..319 are one op field in every word (`OPS` names its values). In a control word, op
0..4, bits 15..22 and 128..132 hold the target T, which `TARGET` composes from the positional
fields f15_22 and f128_132; those fields stay as they are in every word, so the table is one
partition of the bits for all words. The sequencer rule that spans words, four delay slots after
every control op, is `DELAY_SLOTS`.

A data op 8..73 holds its operands in the op slot: the register ra (f123_127), then f128_132 as a
register rb or as bits 8..12 of the immediate, f120_122 as an operand or as bits 13..15. Which,
and how wide the immediate is, is the op's family (`FAMILIES`, `FORMATS`); as with T, the
operands compose from the positional fields.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

WORD_BITS = 320
WORD_BYTES = WORD_BITS // 8


class Kind(StrEnum):
    REG = "reg"  # a register specifier r0..r31
    OPCODE = "opcode"  # an operation or enable code
    ROUTE = "route"  # a source or path select
    FLAG = "flag"  # a single control bit
    CONST = "const"  # always the same value in every stock word
    IMM = "imm"  # an immediate operand
    RAW = "raw"  # bits not decoded yet


class Confidence(IntEnum):
    """Ordered so that min() of several is the weakest."""

    HYPOTHESIS = 0
    INFERRED = 1
    VERIFIED = 2

    @property
    def mark(self) -> str:
        """Listing mark; hypothesis, the common case, stays unmarked."""
        return {0: "", 1: "~", 2: "!"}[self.value]


H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


@dataclass(frozen=True)
class Field:
    lo: int
    hi: int
    kind: Kind
    unit: str
    idle: int
    confidence: Confidence
    fact: str
    name: str = ""
    values: Mapping[int, str] = field(default_factory=dict)
    """Known values and their meaning (documentation, not a restriction)."""
    allowed: tuple[int, ...] | None = None
    """The only values an assembled word may hold, when known."""

    @property
    def id(self) -> str:
        if self.lo == self.hi:
            return f"f{self.lo}"
        return f"f{self.lo}_{self.hi}"

    @property
    def label(self) -> str:
        """The semantic name, or the id while there is none."""
        return self.name or self.id

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    def literal(self, value: int) -> str:
        """`value` as a Python literal: decimal for registers, hex otherwise."""
        return str(value) if self.kind is Kind.REG else f"{value:#x}"

    def get(self, word: int) -> int:
        return (word >> self.lo) & self.mask

    def put(self, word: int, value: int) -> int:
        if not 0 <= value <= self.mask:
            raise ValueError(f"{self.label}: {value:#x} does not fit {self.width} bits")
        return (word & ~(self.mask << self.lo)) | (value << self.lo)

    def check(self, value: int) -> None:
        """Raise ValueError unless `value` is one an assembled word may hold."""
        if not 0 <= value <= self.mask:
            raise ValueError(f"{self.label}: {value:#x} does not fit {self.width} bits")
        if self.allowed is not None and value not in self.allowed:
            raise ValueError(f"{self.label}: {value:#x} not in {self.allowed}")


def _const(lo: int, hi: int, unit: str) -> Field:
    ones = (1 << (hi - lo + 1)) - 1
    return Field(
        lo,
        hi,
        Kind.CONST,
        unit,
        ones,
        V,
        "always all-ones in every word of the five stock programs, and across later firmware "
        "versions of the same family but for one word that may be data",
        allowed=(ones,),
    )


IDLE_OP = 0x7F
SEGMENTED = "boundary from a value-entropy segmentation of the stock programs"


def _ctl(
    lo: int, hi: int, unit: str, idle: int, fact: str = "", basis: str = SEGMENTED
) -> Field:
    kind = Kind.FLAG if lo == hi else Kind.RAW
    base = f"control bits; {basis}"
    return Field(lo, hi, kind, unit, idle, H, f"{base}. {fact}" if fact else base)


GRID_FACT = (
    "grid field, a source or path select (all-ones = none); 0 is its most common value in "
    "the compute sections; boundaries from segmentation and the lag-110 repeat of grid A "
    "in grid B"
)
# Program 16 sets these grid nibbles to 0 in a staircase and holds them to its end.
STAIRCASE = {194: 19, 84: 20, 198: 29, 88: 30, 182: 59, 76: 60}


GRID_PAIR = (
    "2-bit grid field, a source or path select, at lag 110 like grids A and B; all-ones in every "
    "word of the five stock programs, cleared in hundreds of words of some programs across later "
    "firmware versions of the same family"
)


def _grid(lo: int, hi: int, unit: str) -> Field:
    fact = GRID_PAIR if hi - lo == 1 else GRID_FACT
    if lo in STAIRCASE:
        fact += (
            f"; program 16 sets it to 0 from word {STAIRCASE[lo]} on, held to the end "
            "(the plain pass-through a copy needs, hypothesis)"
        )
    return Field(
        lo,
        hi,
        Kind.ROUTE,
        unit,
        (1 << (hi - lo + 1)) - 1,
        H,
        fact,
        values={0xF: "none"} if hi - lo == 3 else {},
    )


# The register groups that differ between program 0x10's words 308..458 and 802..952.
KERNEL_REMAPPED = (110, 115, 123, 128, 211, 216, 224)


def _reg(
    lo: int,
    unit: str,
    idle: int,
    fact: str = "",
    name: str = "",
    confidence: Confidence = H,
) -> Field:
    base = (
        "5-bit register specifier: its bit probabilities fall from the low to the high bit, "
        "the signature of small integers"
    )
    if lo in KERNEL_REMAPPED:
        base += (
            "; remapped between the two copies of program 0x10's kernel, which are "
            "otherwise identical"
        )
    fact = f"{base}. {fact}" if fact else base
    return Field(lo, lo + 4, Kind.REG, unit, idle, confidence, fact, name=name)


def _op(lo: int, hi: int, idle: int, fact: str = "") -> Field:
    base = (
        "per-word op or enable code candidate; 2-8 values per subfield, mostly one or two bits "
        "cleared out of the idle value; boundary from segmentation"
    )
    return Field(
        lo, hi, Kind.OPCODE, "c2", idle, H, f"{base}. {fact}" if fact else base
    )


# Program 16 never moves the C2 bits outside this set.
P16_C2_BITS = (
    255,
    260,
    261,
    262,
    265,
    266,
    267,
    268,
    269,
    272,
    273,
    274,
    275,
    276,
    278,
    280,
    281,
    283,
    285,
    286,
    288,
    290,
    292,
)
WORD504 = (
    "program 13 word 504 has bits of this subfield clear; idling any one of 296, 297, 299, 300 "
    "moves its last plane's horizontal sample to x + 14/16 at b = 1, as idling all of C2's "
    "free bits does (verified on the camera)"
)
WORD504_NONE = (
    "program 13 word 504 has bits of this subfield clear; idling 237, 239 or 254 alone changed "
    "no byte (verified on the camera, an x-varying source at b = 1)"
)
RARE = (
    "rarely used: all-ones in every word of the five stock programs, clear in 1..12 words of "
    "some programs across later firmware versions of the same family"
)
CONTROL_FACT = (
    "clear exactly in the words whose op (f313_319) is 0..4, the control ops, set in all others "
    "(verified in the bytes of the five stock programs and across later firmware versions of the "
    "same family); a control op 0..3 holds its target T in bits 15..22 and 128..132"
)
IMM_FACT = (
    "the 8-bit immediate of the op in f313_319: 0 in every word whose op is idle (verified in the "
    "bytes of the five stock programs and across later firmware versions of the same family); in "
    "a control op 0..3 it is the low byte of the target T (verified in the bytes); in a data op the "
    "low bits of an immediate 5, 13, 15 or 16 bits wide by op, and 0 in every register-form op "
    "(verified in the bytes, `FAMILIES`)"
)
T_HIGH = (
    "in a control op 0..3 bits 8..12 of the target T, not a register (verified in the bytes); in a "
    "return mostly 1 (in every return of the five stock programs but three, which hold 2, 3 and "
    "7), meaning open. In a data op it is the register rb of the register forms (families A', C, "
    "E) or bits 8..12 of the immediate (families A, B, D, F) (verified in the bytes across later "
    "firmware versions of the same family: in 31878 of 32911 idle-op words it holds the last "
    "register-form word's value, which survives the immediate forms in between)"
)
RA_FACT = (
    "the op slot's register ra: it changes in 1 of 33175 idle-op words and in 19884 of 33290 "
    "data-op words across later firmware versions of the same family, so only a word that "
    "carries an op writes it, a data op or a control op 0..2, while a call holds it (verified "
    "in the bytes); whether it is a destination, a source or a base is open; in ops 0..2 the "
    "candidate for the branch or loop condition (hypothesis)"
)
FLAGS_FACT = (
    "3 bits between the register specifiers; bits 120..121 are 0 in every op 0..3 word and bit 122 "
    "is a flag in them, set in some words of each control op and mostly constant within a routine "
    "(verified in the bytes across later firmware versions of the same family), its meaning open. "
    "In a data op bits 13..15 of the immediate in ops 0x0a, 0x0b, 0x38 and 13..14 in 0x37, 0 in 99 % "
    "of family D, an operand of open meaning in families A, A', C and E, where it varies apart "
    "from the immediate (verified in the bytes)"
)
SELECTOR = (
    "the loop-end selector pair f270_271 / f272_273: in every op 0 word of the five stock programs exactly "
    "one of f270_271 and f272_273 is 1, and in 386 of 395 across later firmware versions of the "
    "same family; ops 1..3 mostly hold 3 in both; a loop counter select (hypothesis)"
)
OP_FACT = (
    "the op, one 7-bit field in every word (verified in the bytes of the five stock programs and "
    "across later firmware versions of the same family): 0..4 the control ops with f229 clear, 7 "
    "halt, 8..73 data ops, 127 idle; 5, 6, 74..126 and the data op values 0x22, 0x26, 0x27, "
    "0x2d, 0x39, 0x3b and 0x45 never occur. Bits 17, 18 set with 128, "
    "273, 318 and 319 clear, once read as a control signature, is a coincidence of fields that "
    "several ops share; the word it marks at the end of programs 6, 0xC and 0xF is data op 0x1f "
    "in a return's delay slot, its 13-bit immediate (f15_22 | f128_132 << 8) a multiple of 4 in all "
    "300 such words across later firmware versions of the same family (inferred)"
)
XFER_BASIS = (
    "cut from 308..312 (the segmentation's field) at the register-transfer signature"
)
XFER = (
    "0 in the register-transfer words; their role (load address, offset, count, enable, store "
    "format) is set by their position, not by an opcode (inferred, strong)"
)


CONDITION = (
    "the condition is no field of its own; the candidate is the register ra (f123_127), which "
    "ops 0..2 write and calls hold (hypothesis)"
)


@dataclass(frozen=True)
class Op:
    """A meaning of a value of the op field f313_319."""

    name: str
    confidence: Confidence
    fact: str
    format: "Format | None" = None
    """The operand layout of a data op in the op slot; None for the other ops."""


OPS: dict[int, Op] = {
    0: Op(
        "loop",
        I,
        "loop end: back to T, at or before the word in the five stock programs (some short "
        "bodies across later firmware versions of the same family have T up to 3 words after "
        "it); loop ends nest and never cross; a counted loop's back-edge (inferred, strong)",
    ),
    1: Op(
        "brf",
        I,
        "forward conditional branch, T always after it; the guard of a loop, at the loop's T - 5 "
        "with its own T at the loop's exit (inferred); " + CONDITION,
    ),
    2: Op(
        "br",
        I,
        "conditional branch either way, the back-edge of most guarded loops (inferred); "
        + CONDITION,
    ),
    3: Op(
        "call",
        I,
        "call: when a return lies between the word and T, the word at i + 5 is almost never a "
        "branch target (11 of 950 across later firmware versions of the same family), so a "
        "return reaches it (inferred, strong); program 16's word 0 calls its job routine at 10; "
        "the op 3 words with no return before T are open, a jump or a call",
    ),
    4: Op(
        "ret",
        I,
        "return to the word after the call's delay slots (inferred, strong); it has no target",
    ),
    7: Op(
        "halt",
        I,
        "ends the job: word 5 of every program and no other word, where the call at word 0 "
        "returns (inferred)",
    ),
    IDLE_OP: Op(
        "idle", I, "no op: the idle word's value, its immediate always 0 (inferred)"
    ),
}
CONTROL_OPS = range(5)
"""The control ops, the words with f229 clear."""
DATA_OPS = range(8, 74)
DATA_OP_CONFIDENCE = I
DATA_OP_FACT = (
    "a data op: it leaves f229 set and holds its operands in the op slot (`FAMILIES`); what it "
    "does is open (inferred)"
)
UNSEEN_OP_FACT = (
    "never occurs in the stock programs nor across later firmware versions of the "
    "same family"
)


def op(value: int) -> Op:
    """The meaning of op value `value`; data ops and unseen values are named op<hex>."""
    if value in OPS:
        return OPS[value]
    name = f"op{value:02x}"
    if value in FORMATS:
        fmt = FORMATS[value]
        fact = f"{DATA_OP_FACT}; family {fmt.family.name} ({FAMILY_BASIS}): {fmt.family.fact}"
        if value in MEANINGS:
            fact += f"; {MEANINGS[value]}"
        return Op(name, DATA_OP_CONFIDENCE, fact, fmt)
    return Op(name, H, UNSEEN_OP_FACT)


DELAY_SLOTS = 4
DELAY_FACT = (
    "the four words after a control op are its delay slots: in the five stock programs none of "
    "them is a target or a control word (verified in the bytes), and across later firmware "
    "versions of the same family only short loop ends break it; idling program 16's slots "
    "46..49 of the branch 45 (taken in a copy) times the job out, while idling 50..57, "
    "which it skips, has no effect (verified on the camera). Reading: the slots always run and "
    "the transfer takes effect after them, so a call returns to i + 5 (inferred, strong)"
)


def _c2_fact(lo: int, hi: int) -> str:
    moved = [b for b in P16_C2_BITS if lo <= b <= hi]
    return (
        f"program 16 moves bits {', '.join(map(str, moved))}"
        if moved
        else "program 16 never moves it"
    )


_C2 = [
    (236, 239, 0xF),
    (240, 243, 0xF),
    (244, 247, 0xF),
    (248, 251, 0xF),
    (252, 255, 0xF),
    (256, 258, 0x7),
    (259, 260, 0x3),
    (261, 262, 0x3),
    (263, 264, 0x3),
    (265, 268, 0xF),
    (269, 269, 0x1),
    (270, 271, 0x3),
    (272, 273, 0x3),
    (274, 276, 0x4),
    (278, 280, 0x7),
    (281, 284, 0xF),
    (285, 287, 0x7),
    (288, 291, 0xF),
    (292, 295, 0xF),
    (296, 297, 0x3),
    (298, 301, 0xF),
]


def _c2() -> list[Field]:
    out = []
    for lo, hi, idle in _C2:
        fact = _c2_fact(lo, hi)
        if lo in (296, 298):
            fact += f"; {WORD504}"
        if lo in (236, 252):
            fact += f"; {WORD504_NONE}"
        if lo in (270, 272):
            fact += f"; {SELECTOR}"
        out.append(_op(lo, hi, idle, fact))
        if hi == 276:
            out.append(_const(277, 277, "c2"))
    return out


GRID_A = [
    (23, 30),
    (31, 34),
    (35, 36),
    *((lo, lo + 3) for lo in range(37, 68, 4)),
    (69, 71),
    *((lo, lo + 3) for lo in range(72, 99, 4)),
]
# Grid B repeats grid A's 31..95 at +110.
GRID_B = [(lo + 110, hi + 110) for lo, hi in GRID_A if 31 <= lo and hi <= 95]

FIELDS: tuple[Field, ...] = (
    # ctl_lo: control bits 0..22, all clear in the idle word.
    _ctl(0, 0, "ctl_lo", 0, "program 16 never sets it"),
    _ctl(1, 1, "ctl_lo", 0),
    _ctl(2, 6, "ctl_lo", 0),
    _ctl(7, 8, "ctl_lo", 0),
    _ctl(9, 14, "ctl_lo", 0, "program 16 never sets bits 11 and 14"),
    Field(15, 22, Kind.IMM, "ctl_lo", 0, V, IMM_FACT, name="imm"),
    # grid_a: bits 23..99.
    *(_grid(lo, hi, "grid_a") for lo, hi in GRID_A),
    # regs_a: bits 100..132 and the 133..140 constant.
    _reg(100, "regs_a", 31),
    _reg(105, "regs_a", 31),
    _reg(110, "regs_a", 31),
    _reg(
        115,
        "regs_a",
        31,
        "In program 16 the register-transfer words name the register they use here: as a load "
        "address (word 67), a buffer offset (68 load side, 76 store side), a store count (73), "
        "an enable (42, 66, 69, 70) or the store element format (47) (verified on the camera "
        "per word); r3 holds the job source address",
        name="xreg",
        confidence=I,
    ),
    Field(120, 122, Kind.RAW, "regs_a", 7, H, FLAGS_FACT),
    _reg(123, "regs_a", 31, RA_FACT, name="ra", confidence=V),
    _reg(128, "regs_a", 31, T_HIGH),
    _const(133, 140, "regs_a"),
    # grid_b: grid A + 110.
    *(_grid(lo, hi, "grid_b") for lo, hi in GRID_B),
    # regs_b: bits 206..228.
    _reg(
        206,
        "regs_b",
        0,
        "Flipping its bits in program 16 words 67 and 68 leaves the copy unchanged (verified on the camera)",
    ),
    _reg(
        211,
        "regs_b",
        0,
        "Flipping its bits in program 16 words 67 and 68 leaves the copy unchanged (verified on the camera); the only difference between words 66..68",
    ),
    _reg(216, "regs_b", 31),
    Field(221, 223, Kind.RAW, "regs_b", 7, H, "3 bits between the register specifiers"),
    _reg(224, "regs_b", 0),
    # c2: the control-word flag 229, the 230..231 constant, bit 232, then C2 233..301.
    Field(
        229,
        229,
        Kind.FLAG,
        "c2",
        1,
        V,
        CONTROL_FACT,
        name="ctl_n",
        values={0: "control word", 1: "other word"},
    ),
    _const(230, 231, "c2"),
    Field(232, 232, Kind.FLAG, "c2", 1, H, RARE),
    _op(
        233,
        233,
        1,
        "program 16 never moves it; clear in some words of every compute program",
    ),
    Field(234, 235, Kind.RAW, "c2", 3, H, RARE),
    *_c2(),
    # ctl_hi: control bits 302..319, all set in the idle word.
    _ctl(302, 307, "ctl_hi", 0x3F),
    _ctl(
        308,
        309,
        "ctl_hi",
        0x3,
        f"{XFER}; in program 16 words 58..79, bit 308 clear marks the words that stall the job when idled",
        basis=XFER_BASIS,
    ),
    _ctl(310, 312, "ctl_hi", 0x7, basis=XFER_BASIS),
    Field(
        313,
        319,
        Kind.OPCODE,
        "ctl_hi",
        IDLE_OP,
        V,
        OP_FACT,
        name="op",
        values={value: meaning.name for value, meaning in OPS.items()},
    ),
)


def _index(fields: tuple[Field, ...]) -> dict[str, Field]:
    """Fields by id and by semantic name; raises on overlaps, gaps and name clashes."""
    owner = [""] * WORD_BITS
    by_key: dict[str, Field] = {}
    for f in fields:
        if not 0 <= f.lo <= f.hi < WORD_BITS:
            raise ValueError(f"{f.id}: bits {f.lo}..{f.hi} outside the word")
        if not 0 <= f.idle <= f.mask:
            raise ValueError(f"{f.id}: idle {f.idle:#x} does not fit")
        for bit in range(f.lo, f.hi + 1):
            if owner[bit]:
                raise ValueError(f"bit {bit} in both {owner[bit]} and {f.id}")
            owner[bit] = f.id
        for key in filter(None, (f.id, f.name)):
            if key in by_key:
                raise ValueError(f"field key {key!r} used twice")
            by_key[key] = f
    gaps = [bit for bit, o in enumerate(owner) if not o]
    if gaps:
        raise ValueError(f"bits without a field: {gaps}")
    return by_key


BY_KEY = _index(FIELDS)
IDLE_VALUE = sum(f.idle << f.lo for f in FIELDS)
"""The idle word (a no-op) composed from the fields' idle values."""


@dataclass(frozen=True)
class Composite:
    """A value whose bits lie in several positional fields, lowest part first."""

    name: str
    parts: tuple[Field, ...]
    confidence: Confidence
    fact: str

    def get(self, word: int) -> int:
        value = 0
        for f in reversed(self.parts):
            value = value << f.width | f.get(word)
        return value

    @property
    def width(self) -> int:
        return sum(f.width for f in self.parts)

    def put(self, word: int, value: int) -> int:
        if not 0 <= value < 1 << self.width:
            raise ValueError(f"{self.name}: {value:#x} does not fit {self.width} bits")
        for f in self.parts:
            word = f.put(word, value & f.mask)
            value >>= f.width
        return word


CONTROL_FLAG = BY_KEY["f229"]
"""Clear exactly in the words whose op is a control op."""
OP_FIELD = BY_KEY["op"]
TARGET_OPS = range(4)
"""The control ops that name a target T; a return has none."""

TARGET = Composite(
    "target",
    (BY_KEY["imm"], BY_KEY["f128_132"]),
    V,
    "the target of a control op 0..3: an absolute word index into its program, bits 15..22 | "
    "bits 128..132 << 8 (verified in the bytes: it follows its target word wherever code moved "
    "across later firmware versions of the same family, and lies inside its program in every "
    "op 0..3 word); what goes there is the op's meaning",
)

RA = BY_KEY["ra"]
RB = BY_KEY["f128_132"]
IMM = BY_KEY["imm"]
IMM13 = Composite(
    "imm13",
    (IMM, RB),
    V,
    "a data op's 13-bit immediate, imm | f128_132 << 8 (verified in the bytes across later firmware "
    "versions of the same family: in the immediate forms f128_132 moves with imm and the register "
    "value it held comes back after them)",
)
IMM16 = Composite(
    "imm16",
    (IMM, RB, BY_KEY["f120_122"]),
    V,
    "a data op's 16-bit immediate, imm | f128_132 << 8 | f120_122 << 13 (verified in the bytes "
    "across later firmware versions of the same family: in 0x38 f120_122 = 7 exactly when "
    "f128_132 = 31, a sign extension)",
)
IMM_WIDTHS = {
    5: Composite("imm5", (IMM,), V, "imm, its values 0..31"),
    13: IMM13,
    15: IMM16,
    16: IMM16,
}
"""The fields of an immediate by its width; a 15-bit one leaves bit 122 clear."""


@dataclass(frozen=True)
class Family:
    """Data ops that share one layout of the op slot: ra (f123_127) in every family, then rb
    (f128_132) when `rb`, then an immediate `imm_bits` wide (0: none)."""

    name: str
    ops: tuple[int, ...]
    rb: bool
    imm_bits: int
    fact: str


FAMILY_BASIS = (
    "the format verified in the bytes across later firmware versions of the same family, the "
    "meanings hypotheses"
)
FAMILIES = (
    Family(
        "A",
        tuple(range(0x0C, 0x1B, 2)),
        False,
        13,
        "ra and a 13-bit immediate; f120_122 an operand that varies apart from it (0x0c holds "
        "every f120_122 with f128_132 = 31); the immediates lie within 0x200 of a multiple of 0x800, "
        "so bits 11..12 may select one of four regions and the rest be a small signed offset "
        "(hypothesis); each op's register form is the op + 1",
    ),
    Family(
        "A'",
        tuple(range(0x0D, 0x1C, 2)),
        True,
        0,
        "ra and rb, imm 0 in every word; f120_122 an operand; the operation of the op - 1 with a "
        "register in place of its immediate (inferred from the pairing)",
    ),
    Family(
        "B",
        (0x0A, 0x0B, 0x37, 0x38),
        False,
        16,
        "ra and a wide immediate, f120_122 its bits 13..15 (13..14 in 0x37, whose f120_122 is "
        "0..3 in every word)",
    ),
    Family(
        "C",
        (0x3E, 0x3F, 0x40),
        True,
        5,
        "ra, rb and an immediate 0..31 in every word; f120_122 an operand; shifts or bit-field ops "
        "by a 5-bit count (hypothesis), 16 the most common count of all three",
    ),
    Family(
        "D",
        (*range(0x1C, 0x22), *range(0x23, 0x26), 0x36),
        False,
        13,
        "ra and a small 13-bit immediate; f120_122 is 0 in 99 % of the words",
    ),
    Family(
        "E",
        (
            0x08,
            0x09,
            *range(0x28, 0x2D),
            *range(0x2E, 0x36),
            0x3A,
            0x3C,
            0x3D,
            0x41,
            0x42,
            0x43,
            0x47,
            0x48,
            0x49,
        ),
        True,
        0,
        "ra, rb and f120_122, imm 0 in every word; register-register ALU or multiply-accumulate "
        "ops (hypothesis)",
    ),
    Family(
        "F",
        (0x44, 0x46),
        False,
        13,
        "ra and a 13-bit immediate whose f128_132 is 4 or 8 (0x404, 0x408, 0x40c, 0x460): packed "
        "constants (hypothesis)",
    ),
)
IMM_BITS = {0x37: 15}
"""Ops whose immediate is narrower than their family's."""
SIGNED_OPS = frozenset({0x0C, 0x10})
"""Ops whose 13-bit immediates 0x1fxx read as small negative numbers, two's complement (inferred)."""
MEANINGS = {
    0x09: "the op at 294 of 381 call targets, often with ra = 31: a routine prologue (inferred "
    "from its position), what it does open",
    0x0A: "periodic immediates such as 0x5555 and 0x3333: a lane or phase mask set-up (hypothesis)",
    0x0B: "the most varied immediates, often in a call's delay slots: sets ra from the immediate "
    "(hypothesis, strong)",
    0x0C: "its immediates 0x1fxx read as small negative numbers (inferred)",
    0x10: "its immediates 0x1fxx read as small negative numbers (inferred); in program 13 every "
    "non-zero immediate lies in its tap block 0x00..0x1f: reads a coefficient or line by index "
    "(hypothesis)",
    0x12: "in program 13 every non-zero immediate lies in its tap block 0x00..0x1f: reads a "
    "coefficient or line by index (hypothesis)",
    0x16: "in program 13 every non-zero immediate lies in its tap block 0x00..0x1f, in runs that "
    "step 0, 1, 2, 3 with one ra per run: reads a coefficient or line by index (hypothesis)",
    0x18: "its immediates spread over the four regions as 0x1a's do: a local-memory access at its "
    "immediate (hypothesis)",
    0x1A: "reads the local word at its immediate (inferred, strong): program 16's three 0x1a words "
    "name local words 0, 1 and 2 (src, dst, len), all with ra = 5; idling the one at 2 on the "
    "camera stores 16 KiB of zeros whatever len is, idling the other two leaves dst unwritten",
    0x1E: "at a routine's entry + 1 in 67 routines: adds or subtracts a small immediate, a frame or "
    "pointer adjust (hypothesis)",
    0x1F: "in 300 return delay slots, its immediate a multiple of 4 in all: adds or subtracts a "
    "small immediate, a frame or pointer adjust (hypothesis)",
    0x36: "imm 1, f128_132 and f120_122 0 in all 53 words: an increment (hypothesis)",
    0x37: "periodic immediates such as 0x5555 and 0x2222: a lane or phase mask set-up (hypothesis)",
    0x38: "1022 of 1321 immediates are 2^k - 1 or its complement: an AND with a mask (hypothesis)",
}


@dataclass(frozen=True)
class Operands:
    """A data op's operands; rb and imm are None where its format has none."""

    ra: int
    rb: int | None = None
    imm: int | None = None


@dataclass(frozen=True)
class Format:
    """The operand layout of one data op; decoding never fails, encoding checks the widths."""

    family: Family
    imm_bits: int
    signed: bool

    @property
    def imm(self) -> Composite | None:
        return IMM_WIDTHS.get(self.imm_bits)

    @property
    def fields(self) -> tuple[Field, ...]:
        """The fields the operands stand for."""
        regs = (RA, RB) if self.family.rb else (RA,)
        return regs + (self.imm.parts if self.imm else ())

    def decode(self, word: int) -> Operands:
        rb = RB.get(word) if self.family.rb else None
        if self.imm is None:
            return Operands(RA.get(word), rb)
        imm = self.imm.get(word)
        if self.signed and imm >> (self.imm_bits - 1):
            imm -= 1 << self.imm_bits
        return Operands(RA.get(word), rb, imm)

    def encode(self, word: int, operands: Operands) -> int:
        if (operands.rb is not None) != self.family.rb:
            raise ValueError(f"family {self.family.name}: rb {operands.rb!r}")
        if (operands.imm is not None) != (self.imm is not None):
            raise ValueError(f"family {self.family.name}: imm {operands.imm!r}")
        word = RA.put(word, operands.ra)
        if operands.rb is not None:
            word = RB.put(word, operands.rb)
        if self.imm is None:
            return word
        lo = -(1 << (self.imm_bits - 1)) if self.signed else 0
        if not lo <= operands.imm < lo + (1 << self.imm_bits):
            raise ValueError(f"imm {operands.imm} does not fit {self.imm_bits} bits")
        return self.imm.put(word, operands.imm & ((1 << self.imm_bits) - 1))


def _formats(families: tuple[Family, ...]) -> dict[int, Format]:
    """Each data op's format; raises on an op in two families or outside `DATA_OPS`."""
    formats: dict[int, Format] = {}
    for family in families:
        for value in family.ops:
            if value in formats or value not in DATA_OPS:
                raise ValueError(f"op {value:#x} in family {family.name}")
            bits = IMM_BITS.get(value, family.imm_bits)
            formats[value] = Format(family, bits, value in SIGNED_OPS)
    return formats


FORMATS = _formats(FAMILIES)


def is_control(word: int) -> bool:
    return OP_FIELD.get(word) in CONTROL_OPS


def operands(word: int) -> Operands | None:
    """The operands of a data op; None for the other words."""
    fmt = op(OP_FIELD.get(word)).format
    return None if fmt is None else fmt.decode(word)


def target(word: int) -> int | None:
    """The target of a control op 0..3; None for other words."""
    if OP_FIELD.get(word) not in TARGET_OPS:
        return None
    return TARGET.get(word)


def lookup(key: str) -> Field:
    """The field with id or semantic name `key`."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(f"no DSP field {key!r}") from None
