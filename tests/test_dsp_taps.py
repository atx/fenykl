import struct

import pytest

from fenykl.dsp.stock.taps import Rounding, Taps

UNIT = [(0, 256, 0, 0)] * 16


def test_pack_puts_tap_0_and_2_in_the_low_halves():
    phases = [*UNIT[:5], (-1, 2, -32768, 32767), *UNIT[6:]]
    words = struct.unpack("<32I", Taps.of(phases).pack())
    assert words[10] == 0x0002FFFF
    assert words[11] == 0x7FFF8000
    assert words[0] == 0x01000000 and words[1] == 0
    assert Taps.unpack(Taps.of(phases).pack()) == Taps.of(phases)


@pytest.mark.parametrize(
    "phases, error",
    [
        (UNIT[:15], "15 phases"),
        ([*UNIT[:3], (0, 256, 0), *UNIT[4:]], "phase 3: 3 taps"),
        ([*UNIT[:15], (0, 256, 0, 0x8000)], "phase 15 tap 3: 32768"),
        ([(-0x8001, 0, 0, 0), *UNIT[1:]], "phase 0 tap 0: -32769"),
    ],
)
def test_taps_check_their_shape_and_range(phases, error):
    with pytest.raises(ValueError, match=error):
        Taps.of(phases)


def test_taps_are_ints():
    with pytest.raises(TypeError, match="phase 2 tap 1"):
        Taps.of([*UNIT[:2], (0, 256.0, 0, 0), *UNIT[3:]])


def test_unpack_needs_the_32_words():
    with pytest.raises(ValueError, match="127 tap bytes"):
        Taps.unpack(bytes(127))


@pytest.mark.parametrize(
    "rounding, want",
    [
        (Rounding.HALF_EVEN, (0, 2, -2, -1)),
        (Rounding.HALF_AWAY, (1, 3, -3, -1)),
        (Rounding.FLOOR, (0, 2, -3, -2)),
    ],
)
def test_float_taps_round_each_tap_by_the_given_rule(rounding, want):
    # 0.5, 2.5, -2.5 and -1.25 in tap units: ties at both signs, then a non-tie below zero.
    gains = (0.5 / 256, 2.5 / 256, -2.5 / 256, -1.25 / 256)
    assert Taps.from_floats([gains] * 16, rounding).phases[0] == want


def test_float_taps_are_not_renormalised_and_stay_in_range():
    # Three thirds round to 85 each: the phase sums to 255, not 256.
    taps = Taps.from_floats([(1 / 3, 1 / 3, 1 / 3, 0)] * 16, Rounding.HALF_EVEN)
    assert sum(taps.phases[0]) == 255
    with pytest.raises(ValueError, match="phase 0 tap 1: 32768"):
        Taps.from_floats([(0, 128.0, 0, 0)] * 16, Rounding.HALF_EVEN)
    with pytest.raises(ValueError, match="not finite"):
        Taps.from_floats([(0, float("nan"), 0, 0)] * 16, Rounding.FLOOR)
