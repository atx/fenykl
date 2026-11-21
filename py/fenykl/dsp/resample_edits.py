"""Edits of program 13 measured on the camera: per edited stock word and job class, what each
plane of the output does.

An edit kind sets some bits of one stock word to the idle word's (`Idle`): the grid and C2's
free bits, the grid alone, grid A or grid B alone, C2's free bits alone, the op bits, or one
bit; or sets one field of the field table to a value (`FieldValue`, such as a register
specifier renamed). Every edit below ran on the camera, one per job, at H 16 with stride 64,
and each plane of its output was compared byte for byte with the stock program's output of the
same job on the same boot. The outcomes hold on the job classes (`Probe`) they ran on,
whatever the source's values and the taps: a plane is `STOCK` only if no byte changed in any
run of that class (bytes the stock law leaves unknown, such as the right edge, aside), and
gets a law only where the law gives every changed byte exactly. Outside its classes an edit
gives no prediction.

The classes that ran: sources whose lines are constant along x ("flat") or not, at b = 1, 1 +
1/16 and 0.5, W 64, and flat sources at b = 1 with W 32 and 48."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from .fields import FIELDS, P16_C2_BITS, Field, Kind, lookup
from .word import IDLE, Word

ONE = 0x10000


@dataclass(frozen=True)
class Idle:
    """An edit kind: the bits of `mask` set to the idle word's."""

    name: str
    mask: int

    def apply(self, stock: Word) -> Word:
        return Word.from_int(stock.value & ~self.mask | IDLE.value & self.mask)


def _grid(unit: str) -> int:
    return sum(
        f.mask << f.lo for f in FIELDS if f.unit == unit and f.kind is Kind.ROUTE
    )


GRID_A = _grid("grid_a")
GRID_B = _grid("grid_b")
GRID = GRID_A | GRID_B
C2_FREE = sum(
    1 << b for b in range(233, 302) if b not in P16_C2_BITS and not 274 <= b <= 295
)
"""C2's free bits: 233..301 without the bits program 16 moves and the subfields 274..295 that
hold one."""

OP = sum(
    f.mask << f.lo
    for f in FIELDS
    if f.id in ("f265_268", "f272_273") or f.unit == "ctl_hi"
)
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


Edit = Idle | FieldValue


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


Effect = Stock | Changed | Operands | Phase
Planes = tuple[Effect, Effect, Effect]

STOCK = S = Stock()
BLOCK = Changed("bytes changed, per 32-pixel column block; no law gives them")
LANE = Changed("bytes changed, W-dependent (a per-lane offset); no law gives them")


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
            FLAT_W32: (LANE, Changed(), S),
            FLAT_W48: (LANE, Changed(), S),
            FLAT: _planes("lo.", DROP0),
            VARY: _planes("oo."),
            VARY_UP: _planes("o.."),
        },
        "tap 0's product is dropped and a per-lane offset added, 0 at W 64 on flat sources",
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
        {FLAT_W32: _planes("oo."), FLAT_W48: _planes("oo."), FLAT: _planes("oo.")},
        "",
    ),
    Fact(GRID_ONLY, (440, 441, 446), {FLAT: _planes("o.o")}, ""),
    Fact(GRID_ONLY, (443, 444), {FLAT: _planes("oo.")}, ""),
    Fact(GRID_ONLY, (445, 447, 449, 450), {FLAT: _planes("ooo")}, ""),
    Fact(GRID_ONLY, (448,), {FLAT: _planes(".oo")}, ""),
    Fact(GRID_ONLY, (490,), {VARY_UP: _planes("o..")}, ""),
)
