"""The resampler's taps: 16 phases of 4 signed 16-bit taps, 256 = 1.0, as cmd 0xC's 32 tap words.

Word 2p holds phase p's tap 0 in its low half and tap 1 in its high half, word 2p + 1 taps 2
and 3 [verified on the camera]. Every tap of the s16 range acts as its value with the stock
program [verified on the camera]; with an edited program only taps from -637 to 2048 (gains
-2.49 .. 8.0) ran.
"""

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from math import floor, isfinite

PHASES = 16
TAPS = 4
ONE = 256
"""The tap value of a gain of 1.0."""
TAP_RANGE = (-0x8000, 0x7FFF)
_FORMAT = struct.Struct(f"<{PHASES * TAPS}h")
SIZE = _FORMAT.size
"""The bytes of the 32 tap words."""


class Rounding(Enum):
    """How a float tap becomes an integer: each tap on its own, the phase sums not renormalised."""

    HALF_EVEN = "half-even"
    HALF_AWAY = "half-away"
    """Ties away from zero."""
    FLOOR = "floor"


def _round(v: Fraction, rounding: Rounding) -> int:
    match rounding:
        case Rounding.HALF_EVEN:
            return round(v)
        case Rounding.HALF_AWAY:
            return floor(abs(v) + Fraction(1, 2)) * (1 if v >= 0 else -1)
        case Rounding.FLOOR:
            return floor(v)


@dataclass(frozen=True)
class Taps:
    """16 phases of 4 taps, phase p = (tap 0, 1, 2, 3); checked on construction."""

    phases: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if len(self.phases) != PHASES:
            raise ValueError(f"{len(self.phases)} phases, the resampler has 16")
        lo, hi = TAP_RANGE
        for p, phase in enumerate(self.phases):
            if len(phase) != TAPS:
                raise ValueError(f"phase {p}: {len(phase)} taps, a phase has 4")
            for k, t in enumerate(phase):
                if type(t) is not int:
                    raise TypeError(f"phase {p} tap {k}: {t!r} is not an int")
                if not lo <= t <= hi:
                    raise ValueError(f"phase {p} tap {k}: {t} is not an s16")

    @classmethod
    def of(cls, phases: Sequence[Sequence[int]]) -> "Taps":
        return cls(tuple(tuple(phase) for phase in phases))

    @classmethod
    def from_floats(
        cls, phases: Sequence[Sequence[float]], rounding: Rounding
    ) -> "Taps":
        """Taps from gains (1.0 = 256), each tap rounded by `rounding`; a tap outside the s16
        range after rounding raises."""
        out = []
        for p, phase in enumerate(phases):
            row = []
            for k, g in enumerate(phase):
                if not isfinite(g):
                    raise ValueError(f"phase {p} tap {k}: {g} is not finite")
                row.append(_round(Fraction(g) * ONE, rounding))
            out.append(row)
        return cls.of(out)

    @classmethod
    def unpack(cls, data: bytes) -> "Taps":
        """The taps in the 32 tap words."""
        if len(data) != SIZE:
            raise ValueError(f"{len(data)} tap bytes, the tap words are {SIZE}")
        v = _FORMAT.unpack(data)
        return cls.of([v[TAPS * p : TAPS * p + TAPS] for p in range(PHASES)])

    def pack(self) -> bytes:
        """The 32 tap words."""
        return _FORMAT.pack(*(t for phase in self.phases for t in phase))
