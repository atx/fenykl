"""Edits of program 13 measured on the camera: per edited stock word and job class, what each
plane of the output does.

An edit kind sets some bits of one stock word to the idle word's (`Idle`): the grid and C2's
free bits, the grid alone, grid A or grid B alone, C2's free bits alone, the op bits, or one
bit; or sets one field of the field table to a value (`FieldValue`, such as a register
specifier renamed, or the immediate or op of a tap read). Every edit below ran on the camera, one per job, at H 16 with stride 64,
and each plane of its output was compared byte for byte with the stock program's output of the
same job on the same boot. The outcomes hold on the job classes (`Probe`) they ran on,
whatever the source's values and the taps, except where they read the boot's tap-0
substitute (`Substitute`), a state that earlier edits on the boot move: a plane is `STOCK`
only if no byte changed in any run of that class (bytes the stock law leaves unknown, such as the right edge, aside), and
gets a law only where the law gives every changed byte exactly. Outside its classes an edit
gives no prediction.

The classes that ran: sources whose lines are constant along x ("flat") or not, at b = 1, 1 +
1/16 and 0.5, W 64, and flat sources at b = 1 with W 32 and 48."""

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .fields import GRID_A as GRID_A_FIELDS
from .fields import GRID_B as GRID_B_FIELDS
from .fields import P16_C2_BITS, Field, lookup
from .word import IDLE, Word

ONE = 0x10000


@dataclass(frozen=True)
class Idle:
    """An edit kind: the bits of `mask` set to the idle word's."""

    name: str
    mask: int

    def apply(self, stock: Word) -> Word:
        return Word.from_int(stock.value & ~self.mask | IDLE.value & self.mask)


# The 2-bit grid pairs: idle in every stock word, and the bench harness never writes them.
GRID_PAIRS = ((35, 36), (145, 146))


def _bits(ranges: list[tuple[int, int]]) -> int:
    return sum(((1 << (hi - lo + 1)) - 1) << lo for lo, hi in ranges)


GRID_A = _bits([r for r in GRID_A_FIELDS if r not in GRID_PAIRS])
GRID_B = _bits([r for r in GRID_B_FIELDS if r not in GRID_PAIRS])
GRID = GRID_A | GRID_B
C2_FREE = sum(
    1 << b for b in range(233, 302) if b not in P16_C2_BITS and not 274 <= b <= 295
)
"""C2's free bits: 233..301 without the bits program 16 moves and the subfields 274..295 that
hold one."""

OP = _bits([(265, 268), (272, 273), (302, 319)])
"""The op bits: C2's 265..268 and 272..273 and the control bits 302..319, all set in the idle
word."""

GRID_C2 = Idle("grid and C2 free bits idle", GRID | C2_FREE)
GRID_ONLY = Idle("grid idle", GRID)
# The grid A / grid B halves of GRID_ONLY; no measured facts yet.
GRID_A_ONLY = Idle("grid A idle", GRID_A)
GRID_B_ONLY = Idle("grid B idle", GRID_B)
C2_ONLY = Idle("C2 free bits idle", C2_FREE)
OP_IDLE = Idle("op bits idle", OP)


def bit(b: int) -> Idle:
    return Idle(f"bit {b} idle", 1 << b)


@dataclass(frozen=True)
class FieldValue:
    """An edit kind: the field with id or name `key` set to `value`."""

    key: str
    value: int

    @property
    def field(self) -> Field:
        return lookup(self.key)

    @property
    def name(self) -> str:
        f = self.field
        return f"{f.label} = {f.literal(self.value)}"

    def apply(self, stock: Word) -> Word:
        return Word.from_int(self.field.put(stock.value, self.value))


@dataclass(frozen=True)
class FieldValues:
    """An edit kind: several fields of one word set at once, in order."""

    values: tuple[FieldValue, ...]

    @property
    def name(self) -> str:
        return ", ".join(v.name for v in self.values)

    def apply(self, stock: Word) -> Word:
        for v in self.values:
            stock = v.apply(stock)
        return stock


def set_fields(**values: int) -> FieldValues:
    return FieldValues(tuple(FieldValue(k, v) for k, v in values.items()))


Edit = Idle | FieldValue | FieldValues


@dataclass(frozen=True)
class Probe:
    """A job class: whether every source line is constant along x, the table's one step b
    (16.16) and the width; H 16."""

    flat: bool
    b: int
    w: int

    def __str__(self) -> str:
        kind = "flat" if self.flat else "x-varying"
        return f"{kind} source, b {self.b / ONE:g}, W {self.w}"


FLAT = Probe(True, ONE, 64)
FLAT_W32 = Probe(True, ONE, 32)
FLAT_W48 = Probe(True, ONE, 48)
VARY = Probe(False, ONE, 64)
VARY_UP = Probe(False, ONE + ONE // 16, 64)
VARY_HALF = Probe(False, ONE // 2, 64)


# -- what a plane does


@dataclass(frozen=True)
class Stock:
    """The stock law holds: no byte the law gives changed."""


@dataclass(frozen=True)
class Changed:
    """Bytes changed and no law gives them: the plane is unknown."""

    why: str = "bytes changed, no law gives them"


class Operand(Enum):
    LOW = "a value at or below 0.5, not a pixel: a one-tap line with its tap in 1..256 gives 0"
    UNKNOWN = "not known: the bytes it reaches are unknown"


@dataclass(frozen=True)
class Operands:
    """Plane 0 at b = 1 with tap k multiplying ops[k]: the source line of tap j (an int j; k
    itself is the stock line), nothing (None: the product is dropped) or an `Operand`."""

    ops: tuple[int | Operand | None, ...]


@dataclass(frozen=True)
class Phase:
    """A chroma plane at b = 1 sampled along the line at horizontal phase q / 16 instead of 0:
    the vertical pass I = R4(sum of tap x pixel), unclamped, then the Q14 sum of the fixed
    chroma taps (0, 256 - 16q, 16q, 0) x I on pixels x - 1 .. x + 2, one rounding to 8 bits
    (ties to even) and the clamp; pixel 63 reads past the line and is unknown."""

    q: int


@dataclass(frozen=True)
class TapLoad:
    """Plane 0 at b = 1 with tap `tap`'s coefficient loaded from the phase's taps as the tap
    words hold them (c0..c3, s16 little-endian): the value of struct format `fmt` at byte
    `offset`; the tap still multiplies its own source line."""

    tap: int
    offset: int
    fmt: str

    def kernel(self, kernel: tuple[int, ...]) -> tuple[int, ...]:
        (c,) = struct.unpack_from(self.fmt, struct.pack("<4h", *kernel), self.offset)
        return (*kernel[: self.tap], c, *kernel[self.tap + 1 :])


@dataclass(frozen=True)
class Substitute:
    """Plane 0 at b = 1 with tap 0's product replaced by the boot's tap-0 substitute
    (`Boot.substitute`), a per-lane value added without the multiply: the drop of tap 0's
    product (`Operands((None, 1, 2, 3))`) where the substitute is known to be 0, unknown where it
    is not known."""


Effect = Stock | Changed | Operands | Phase | TapLoad | Substitute
Planes = tuple[Effect, Effect, Effect]

STOCK = S = Stock()
BLOCK = Changed("bytes changed, per 32-pixel column block; no law gives them")
SUBSTITUTED = Changed(
    "tap 0's product replaced by the boot's tap-0 substitute; at this width it never ran where "
    "the substitute is known"
)


def _planes(spec: str, *laws: Effect) -> Planes:
    """'.' stock, 'o' changed, 'l' the next of `laws`."""
    it = iter(laws)
    out = tuple(S if c == "." else Changed() if c == "o" else next(it) for c in spec)
    assert len(out) == 3 and next(it, None) is None, spec
    return out  # type: ignore[return-value]


@dataclass(frozen=True)
class Fact:
    kind: Edit
    words: tuple[int, ...]
    planes: Mapping[Probe, Planes]
    fact: str


def _r(lo: int, hi: int) -> tuple[int, ...]:
    return tuple(range(lo, hi + 1))


B1 = {FLAT: _planes("..."), VARY: _planes("...")}
"""No byte changed at b = 1, W 64, on flat and x-varying sources."""

DROP0 = Operands((None, 1, 2, 3))
TAP1_LOW = Operands((Operand.UNKNOWN, Operand.LOW, Operand.UNKNOWN, Operand.UNKNOWN))
PHASE14 = Phase(14)
TAP1_FROM_C0 = TapLoad(1, 0, "<h")
TAP1_FROM_C0_HIGH = TapLoad(1, 1, "B")
"""Tap 1 := c0's bits 8..15, zero-extended: the byte at offset 1 of the phase's taps."""
SUBSTITUTE = Substitute()
TAP0_FROM = {v: TapLoad(0, 2 * k, "<h") for k, v in enumerate((3, 2, 4, 5))}
"""Word 439's f88_91 value -> tap 0 := c_k (3, 2, 4, 5: c0..c3)."""
HELD_433 = set_fields(
    f147_150=15,
    f151_154=15,
    f155_158=15,
    f159_162=15,
    f163_166=15,
    f167_170=15,
    f171_174=15,
    f175_178=15,
    f179_181=7,
    f182_185=15,
    f186_189=15,
    f190_193=15,
)
"""Word 433's held grid B nibbles of idle enables, at their idle values."""
HELD_B_439 = set_fields(
    f147_150=15,
    f151_154=15,
    f155_158=15,
    f159_162=15,
    f167_170=15,
    f171_174=15,
    f175_178=15,
    f179_181=7,
    f182_185=15,
    f186_189=15,
    f190_193=15,
    f198_201=15,
    f202_205=15,
)
"""Word 439's held grid B nibbles of idle enables, at their idle values."""
HELD_A_439 = set_fields(
    f37_40=15, f41_44=15, f45_48=15, f49_52=15, f65_68=15, f76_79=15, f80_83=15
)
"""Word 439's held grid A nibbles of idle heads, at their idle values."""
HELD_A_439_X0 = set_fields(
    f37_40=15,
    f41_44=15,
    f45_48=15,
    f49_52=15,
    f65_68=15,
    f69_71=7,
    f76_79=15,
    f80_83=15,
)
S2_439 = set_fields(f53_56=15, f57_60=15, f88_91=15)
"""Word 439's S2 operand triple, at the idle values."""


def _copies(up: str, half: str) -> dict[Probe, Planes]:
    return {**B1, VARY_UP: _planes(up), VARY_HALF: _planes(half)}


FACTS: tuple[Fact, ...] = (
    # Grid and C2 free bits idle.
    Fact(
        GRID_C2,
        (
            *_r(391, 395),
            *_r(402, 407),
            *_r(414, 416),
            *_r(418, 426),
            *_r(589, 595),
            597,
        ),
        B1,
        "no effect at b = 1",
    ),
    Fact(
        GRID_C2,
        (*_r(583, 585), 587, *_r(652, 666)),
        _copies("...", "..."),
        "no effect at any step that ran",
    ),
    Fact(
        GRID_C2, (490, 494, 501), _copies("o..", "o.."), "a horizontal pass at b != 1"
    ),
    Fact(GRID_C2, (491,), _copies("o.o", "..o"), "a horizontal pass at b != 1"),
    Fact(
        GRID_C2,
        (492, 493, 498, 500),
        _copies("o.o", "o.o"),
        "a horizontal pass at b != 1",
    ),
    Fact(GRID_C2, (499,), _copies("o.o", "o.."), "a horizontal pass at b != 1"),
    Fact(GRID_C2, (503,), _copies("oo.", ".o."), "a horizontal pass at b != 1"),
    Fact(
        GRID_C2,
        (*_r(505, 507), *_r(512, 514), 518, 519),
        _copies("o.o", "..."),
        "a horizontal pass at b 1 + 1/16 only",
    ),
    Fact(
        GRID_C2, (508,), _copies("o..", "..."), "a horizontal pass at b 1 + 1/16 only"
    ),
    Fact(
        GRID_C2,
        (509, 510, 517),
        _copies("oo.", "..."),
        "a horizontal pass at b 1 + 1/16 only",
    ),
    Fact(
        GRID_C2, (511,), _copies("ooo", "..."), "a horizontal pass at b 1 + 1/16 only"
    ),
    Fact(GRID_C2, (516,), _copies("oo.", "o.."), "a horizontal pass at b != 1"),
    Fact(
        GRID_C2,
        (586, 639, 640, 645, 647),
        _copies("...", "o.o"),
        "a horizontal pass at b 0.5 only",
    ),
    Fact(
        GRID_C2,
        (637, 646, 648, 649),
        _copies("...", "o.."),
        "a horizontal pass at b 0.5 only",
    ),
    Fact(GRID_C2, (638, 651), _copies("...", "..o"), "a horizontal pass at b 0.5 only"),
    Fact(GRID_C2, (650,), _copies("...", ".o."), "a horizontal pass at b 0.5 only"),
    Fact(
        GRID_C2,
        (504,),
        {
            FLAT: _planes("..l", PHASE14),
            VARY: _planes("..l", PHASE14),
            VARY_UP: _planes("ooo"),
            VARY_HALF: _planes("..o"),
        },
        "the last chroma plane's horizontal phase: 14 / 16 at b = 1",
    ),
    Fact(
        GRID_C2,
        (433, 435),
        {
            FLAT_W32: (BLOCK, S, S),
            FLAT_W48: (BLOCK, S, S),
            FLAT: (BLOCK, S, S),
            VARY: (BLOCK, S, S),
            VARY_UP: (BLOCK, S, S),
        },
        "plane 0 changes per 32-pixel column block (x = 32 at W 32, 48 and 64)",
    ),
    Fact(
        GRID_C2,
        (436,),
        {
            FLAT_W32: (TAP1_LOW, S, S),
            FLAT_W48: (TAP1_LOW, S, S),
            FLAT: (TAP1_LOW, S, S),
            VARY: (BLOCK, S, S),
            VARY_UP: (BLOCK, S, S),
        },
        "tap 1's operand drops to 0; the other lines change per 32-pixel column block",
    ),
    Fact(
        GRID_C2,
        (437,),
        {
            FLAT: _planes("l..", Operands((Operand.UNKNOWN, 1, Operand.LOW, 0))),
            VARY: _planes("o.."),
            VARY_UP: _planes("o.."),
        },
        "tap sequencing: tap 3 reads tap 0's line, tap 2 a value at or below 0.5, tap 1 is intact, "
        "tap 0's operand is not known",
    ),
    Fact(
        GRID_C2,
        (442,),
        {FLAT: _planes("o.."), VARY: _planes("o.."), VARY_UP: _planes("o..")},
        "",
    ),
    Fact(
        GRID_C2,
        (439,),
        {
            FLAT_W32: (SUBSTITUTED, Changed(), S),
            FLAT_W48: (SUBSTITUTED, Changed(), S),
            FLAT: _planes("lo.", SUBSTITUTE),
            VARY: _planes("oo."),
            VARY_UP: _planes("o.."),
        },
        "tap 0's product is replaced by the boot's tap-0 substitute",
    ),
    Fact(
        GRID_C2,
        (440, 446),
        {FLAT: _planes("o.o"), VARY: _planes("o.o"), VARY_UP: _planes("o.o")},
        "",
    ),
    Fact(
        GRID_C2,
        (441,),
        {FLAT: _planes("o.o"), VARY: _planes("o.o"), VARY_UP: _planes("o..")},
        "",
    ),
    Fact(
        GRID_C2,
        (443, 444),
        {FLAT: _planes("oo."), VARY: _planes("oo."), VARY_UP: _planes("oo.")},
        "",
    ),
    Fact(
        GRID_C2,
        (445, *_r(447, 450)),
        {FLAT: _planes("ooo"), VARY: _planes("ooo"), VARY_UP: _planes("ooo")},
        "every plane changes: pass set-up (accumulator, scale or output format)",
    ),
    # Word 433 reads tap 1 of the line's phase (a halfword load, imm 1).
    Fact(
        FieldValue("imm", 0),
        (433,),
        {FLAT: _planes("l..", TAP1_FROM_C0)},
        "tap 1 takes tap 0's coefficient: the immediate is the tap index (one run)",
    ),
    Fact(
        FieldValue("op", 0x10),
        (433,),
        {FLAT: _planes("l..", TAP1_FROM_C0_HIGH)},
        "tap 1 takes c0's bits 8..15, zero-extended: a byte load at the same address (one run)",
    ),
    Fact(
        set_fields(op=0x10, imm=0),
        (433,),
        {FLAT: _planes("l..", TapLoad(1, 0, "B"))},
        "tap 1 takes c0's bits 0..7, zero-extended: op 0x10's immediate counts bytes (one run)",
    ),
    Fact(
        FieldValue("op", 0x0E),
        (433,),
        {FLAT: _planes("l..", TapLoad(1, 1, "b"))},
        "tap 1 takes c0's bits 8..15, sign-extended (one run each on two tap sets); a byte "
        "load at the same address and byte 1 of the datum the word before delivers are not "
        "separated",
    ),
    Fact(
        set_fields(op=0x0E, imm=0),
        (433,),
        {FLAT: _planes("l..", TapLoad(1, 0, "b"))},
        "tap 1 takes c0's bits 0..7, sign-extended (one run); load or datum select as at imm 1",
    ),
    Fact(
        FieldValue("op", 0x14),
        (433,),
        {FLAT: _planes("...")},
        "op 0x14 at imm 1 reads the stock coefficient c1: a halfword at imm halfwords (one run)",
    ),
    Fact(
        set_fields(op=0x14, imm=0),
        (433,),
        {FLAT: _planes("l..", TAP1_FROM_C0)},
        "tap 1 takes c0, as 0x16 at imm 0 (one run)",
    ),
    Fact(
        FieldValue("op", 0x1A),
        (433,),
        {FLAT: _planes("l..", TapLoad(1, 4, "<h"))},
        "tap 1 takes c2: the word at imm 1 words, its low 16 bits reaching the coefficient "
        "(one run)",
    ),
    # Word 439's S2 operands: f57_60 tap 0's source line, f88_91 tap 0's coefficient.
    Fact(
        FieldValue("f57_60", 4),
        (439,),
        {FLAT: _planes("l..", Operands((1, 1, 2, 3)))},
        "tap 0 multiplies tap 1's line, with its own coefficient (5, the stock value, is its "
        "own line)",
    ),
    *(
        Fact(
            FieldValue("f88_91", v),
            (439,),
            {FLAT: _planes("l..", TAP0_FROM[v])},
            f"tap 0 takes c{TAP0_FROM[v].offset // 2} (pre-registered, one run each); "
            f'"tap 0 takes c_k", "tap 0\'s product under tap k\'s coefficient" and "tap k '
            "multiplies tap 0's line too\" give the same bytes",
        )
        for v in (2, 4, 5)
    ),
    Fact(
        FieldValue("f88_91", 0),
        (439,),
        {FLAT: _planes("o..")},
        "no coefficient select fits; what 0 selects is open",
    ),
    Fact(
        FieldValue("f88_91", 1),
        (439,),
        {FLAT: _planes("o..")},
        "no coefficient select fits; what 1 selects is open",
    ),
    Fact(FieldValue("f53_56", 1), (439,), {FLAT: _planes("...")}, "a don't-care here"),
    Fact(S2_439, (439,), {FLAT: _planes("l..", SUBSTITUTE)}, "as grid A idle"),
    # Word 437's S2 operands act at the edge lines only.
    Fact(
        FieldValue("f57_60", 1),
        (437,),
        {FLAT: _planes("o..")},
        "the top line changes only (tap 0's line at the top edge)",
    ),
    Fact(
        FieldValue("f88_91", 5),
        (437,),
        {FLAT: _planes("o..")},
        "the top and bottom lines change only (tap 0's line at the edges)",
    ),
    # Single bits of word 504's C2.
    Fact(bit(237), (504,), {VARY: _planes("...")}, ""),
    Fact(bit(239), (504,), {VARY: _planes("...")}, ""),
    Fact(bit(254), (504,), {VARY: _planes("...")}, ""),
    *(
        Fact(bit(b), (504,), {VARY: _planes("..l", PHASE14)}, "as the whole C2 idled")
        for b in (296, 297, 299, 300)
    ),
    # C2 free bits idle.
    Fact(C2_ONLY, (433,), {FLAT: _planes("...")}, "433 acts through its grid only"),
    Fact(C2_ONLY, (504,), {FLAT: _planes("..l", PHASE14)}, ""),
    Fact(C2_ONLY, (651,), {VARY: _planes("..."), VARY_HALF: _planes("...")}, ""),
    Fact(C2_ONLY, (435, 436, 442, 443, 447), {FLAT: _planes("o..")}, ""),
    Fact(
        C2_ONLY,
        (437,),
        {FLAT: _planes("l..", Operands((Operand.UNKNOWN, 1, 2, 0)))},
        "as the whole edit without tap 2's change",
    ),
    Fact(C2_ONLY, (439, 444), {FLAT: _planes(".o.")}, ""),
    Fact(C2_ONLY, (440, 441), {FLAT: _planes("o.o")}, ""),
    Fact(C2_ONLY, (445, 449), {FLAT: _planes("oo.")}, ""),
    Fact(C2_ONLY, (446,), {FLAT: _planes("..o")}, ""),
    Fact(C2_ONLY, (448, 450), {FLAT: _planes(".oo")}, ""),
    # Grid idle.
    Fact(GRID_ONLY, (504,), {FLAT: _planes("...")}, ""),
    Fact(GRID_ONLY, (433, 435, 442), {FLAT: _planes("o..")}, ""),
    Fact(GRID_ONLY, (433,), {FLAT_W32: _planes("o..")}, ""),
    Fact(GRID_ONLY, (436,), {FLAT: (TAP1_LOW, S, S)}, "as the whole edit"),
    Fact(
        GRID_ONLY,
        (437,),
        {FLAT: _planes("l..", Operands((Operand.UNKNOWN, 1, Operand.LOW, 0)))},
        "as the whole edit",
    ),
    Fact(
        GRID_ONLY,
        (439,),
        {
            FLAT_W32: _planes("oo."),
            FLAT_W48: _planes("oo."),
            FLAT: _planes("lo.", SUBSTITUTE),
        },
        "tap 0's product is replaced by the boot's tap-0 substitute",
    ),
    Fact(GRID_ONLY, (440, 441, 446), {FLAT: _planes("o.o")}, ""),
    Fact(GRID_ONLY, (443, 444), {FLAT: _planes("oo.")}, ""),
    Fact(GRID_ONLY, (445, 447, 449, 450), {FLAT: _planes("ooo")}, ""),
    Fact(GRID_ONLY, (448,), {FLAT: _planes(".oo")}, ""),
    Fact(GRID_ONLY, (490,), {VARY_UP: _planes("o..")}, ""),
    # Grid A or grid B idle.
    Fact(GRID_A_ONLY, (433,), {FLAT: _planes("..."), FLAT_W32: _planes("...")}, ""),
    Fact(GRID_B_ONLY, (433,), {FLAT: _planes("o.."), FLAT_W32: _planes("o..")}, ""),
    Fact(GRID_A_ONLY, (437,), {FLAT: _planes("o..")}, "tap 0 reads a per-boot value"),
    Fact(GRID_B_ONLY, (437,), {FLAT: _planes("o..")}, ""),
    Fact(
        GRID_A_ONLY,
        (439,),
        {FLAT: _planes("l..", SUBSTITUTE)},
        "tap 0's product is replaced by the boot's tap-0 substitute",
    ),
    Fact(GRID_B_ONLY, (439,), {FLAT: _planes(".o.")}, "the second plane's path"),
    # Single grid nibbles, at their idle values.
    Fact(FieldValue("f198_201", 15), (433,), {FLAT: _planes("o..")}, "as grid B idle"),
    Fact(FieldValue("f202_205", 15), (433,), {FLAT: _planes("...")}, ""),
    Fact(
        set_fields(f198_201=15, f202_205=15),
        (433,),
        {FLAT: _planes("o..")},
        "as grid B idle",
    ),
    Fact(HELD_433, (433,), {FLAT: _planes("...")}, ""),
    Fact(FieldValue("f163_166", 15), (439,), {FLAT: _planes(".o.")}, "as grid B idle"),
    Fact(HELD_B_439, (439,), {FLAT: _planes("...")}, ""),
    Fact(HELD_A_439, (439,), {FLAT: _planes("...")}, ""),
    Fact(HELD_A_439_X0, (439,), {FLAT: _planes("...")}, ""),
    Fact(FieldValue("f69_71", 7), (439,), {FLAT: _planes("...")}, ""),
)

SUBSTITUTE_MOVER = (433, GRID_B_ONLY)
"""The edit that sets the tap-0 substitute to 0 on a flat source at W 64, whatever it held
before (inferred: on every boot where it ran, word 439's grid A idle gave the drop of tap 0's
product after it, and a per-lane pattern before it where that ran)."""
SUBSTITUTE_KEEPERS: tuple[tuple[int, Edit], ...] = (
    (433, GRID_A_ONLY),
    *((437, k) for k in (GRID_ONLY, GRID_A_ONLY, GRID_B_ONLY)),
    *((439, k) for k in (GRID_ONLY, GRID_A_ONLY, GRID_B_ONLY)),
    *(
        (439, k)
        for k in (
            FieldValue("f163_166", 15),
            HELD_B_439,
            HELD_A_439,
            HELD_A_439_X0,
            FieldValue("f69_71", 7),
            S2_439,
            FieldValue("f57_60", 4),
            FieldValue("f53_56", 1),
        )
    ),
    *((439, FieldValue("f88_91", v)) for v in (0, 1, 2, 4, 5)),
)
"""The edits seen to leave a substitute of 0 in place, on a flat source at W 64 (verified: a
grid A idle of word 439 after them still gave the drop of tap 0's product). Every other edited
job makes it unknown; a stock job keeps it."""
