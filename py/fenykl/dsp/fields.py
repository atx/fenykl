"""The field table of the 320-bit DSP word: the single source of truth for encoding.

Bit k of a word is bit (k % 8) of byte k // 8 of its 40 bytes. Every bit belongs to exactly one
field. A field has a permanent positional id that names both ends of its bits (`f115_119`, `f229`),
an optional semantic name that may be renamed at will, a kind, its value in the
idle word, a confidence and a one-line fact.

Confidence qualifies the field itself: that these bits form one field of this kind. Facts about
single values or words carry their own tags in `fact` and `values`. The emulator does not use
it: it propagates the confidence of the semantics a result depends on (see `semantics`).
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
        "always all-ones in every word of the five stock programs",
        allowed=(ones,),
    )


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


def _grid(lo: int, hi: int, unit: str) -> Field:
    fact = GRID_FACT
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
WORD504 = "program 13 word 504 has bits of this subfield clear; idling them moves its last plane's horizontal sample to x + 14/16 (verified on the camera)"
SIGNATURE = (
    "part of the control signature: a word that differs from the three identical words after it "
    "only in bits 128, 273, 318, 319 (clear) and 17, 18 (set); it ends programs 6, 0xC and 0xF "
    "and recurs mid-program (a control operation with three drain words, hypothesis)"
)
XFER_BASIS = (
    "cut from 308..312 (the segmentation's field) at the register-transfer signature"
)
XFER = (
    "0 in the register-transfer words; their role (load address, offset, count, enable, store "
    "format) is set by their position, not by an opcode (inferred, strong)"
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
        if lo in (236, 252, 296, 298):
            fact += f"; {WORD504}"
        if lo == 272:
            fact += f"; bit 273: {SIGNATURE}"
        out.append(_op(lo, hi, idle, fact))
        if hi == 276:
            out.append(_const(277, 277, "c2"))
    return out


GRID_A = [
    (23, 30),
    (31, 34),
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
    _ctl(15, 18, "ctl_lo", 0, f"bits 17, 18: {SIGNATURE}"),
    _ctl(19, 22, "ctl_lo", 0),
    # grid_a: bits 23..99, the 35..36 constant in between.
    *(_grid(lo, hi, "grid_a") for lo, hi in GRID_A[:2]),
    _const(35, 36, "grid_a"),
    *(_grid(lo, hi, "grid_a") for lo, hi in GRID_A[2:]),
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
    Field(120, 122, Kind.RAW, "regs_a", 7, H, "3 bits between the register specifiers"),
    _reg(123, "regs_a", 31),
    _reg(128, "regs_a", 31, f"bit 128: {SIGNATURE}"),
    _const(133, 140, "regs_a"),
    # grid_b: grid A + 110, the 145..146 constant in between.
    _grid(141, 144, "grid_b"),
    _const(145, 146, "grid_b"),
    *(_grid(lo, hi, "grid_b") for lo, hi in GRID_B[1:]),
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
    # c2: bit 229, the 230..232 constant, then the control block C2 233..301.
    _ctl(229, 229, "c2", 1, basis="the bit between register group B and a constant"),
    _const(230, 232, "c2"),
    _op(
        233,
        233,
        1,
        "program 16 never moves it; clear in some words of every compute program",
    ),
    _const(234, 235, "c2"),
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
    _ctl(313, 319, "ctl_hi", 0x7F, f"bits 318, 319: {SIGNATURE}"),
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


def lookup(key: str) -> Field:
    """The field with id or semantic name `key`."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(f"no DSP field {key!r}") from None
