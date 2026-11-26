"""The unit function tables: the X units, the S units' lane functions and the lane memory."""

import pytest

from fenykl.dsp.semantics import (
    LaneMemory,
    lane_address,
    lane_result,
    product_high,
    x_result,
)


@pytest.mark.parametrize(
    "unit, head, a, b, k, result",
    [
        # K's top bit set: sK negative, zK large.
        ("x1", 0x00, 0xFFFFFFFF, 0, 0x1FFF, 0),  # a != sK, sK = -1
        ("x1", 0x01, -1, 0, 0x1FFF, 1),  # a == sK, either reading of a
        ("x0", 0x04, -0x400, 0, 0x400, 1),  # a <= sK signed, sK = -0x400
        ("x0", 0x05, -0x400, 0, 0x400, 0),
        ("x0", 0x06, -1, 0, 0x400, 0),  # a <= zK unsigned: 0xffffffff > 0x400
        ("x0", 0x07, 0x3FF, 0, 0x400, 1),
        ("x1", 0x06, 5, 0, 5, 1),  # a == K: <= and < apart
        ("x1", 0x07, 5, 0, 5, 0),
        ("x0", 0x08, 5, 5, 0, 1),  # a == b: <= and < apart
        ("x0", 0x09, 5, 5, 0, 0),
        ("x1", 0x0A, -5, -5, 0, 1),
        ("x1", 0x0B, -5, -5, 0, 0),
        ("x0", 0x10, 7, 5, 0, 2),  # a - b, a + b
        ("x0", 0x11, 7, 5, 0, 12),
        ("x1", 0x18, 1, 1, 0, 0),  # a ^ b, a | b
        ("x1", 0x19, 1, 1, 0, 1),
        ("x1", 0x02, 0, 0, 0x1000, 0xFFFFF000),  # a - zK
        ("x0", 0x03, 0xFFFFFFFF, 0, 0x7FF, 0x7FE),  # a + zK wraps
        ("x1", 0x08, -1, 1, 0, 0),  # a <= b unsigned
        ("x1", 0x0A, -1, 1, 0, 1),  # a <= b signed
        ("x0", 0x0B, 0x7FFFFFFF, 0x80000000, 0, 0),  # a < b signed
        ("x0", 0x09, 0x7FFFFFFF, 0x80000000, 0, 1),  # a < b unsigned
        ("x1", 0x0D, -1, 0xFFFFFFFF, 0, 1),  # a == b on 32 bits
        ("x0", 0x0E, 0, -5, 0, 5),  # abs(b)
        ("x0", 0x0F, 0, 5, 0, 0xFFFFFFFB),  # -b
        ("x1", 0x12, 0x1234, 0, 0x1FFF, 0x1FFF1234),  # a | zK << 16
        ("x0", 0x14, 0, 0, 0x7FF, 0xFFFFFFFF),  # sK, 11 bits
        ("x1", 0x14, 0, 0, 0x7FF, 0x7FF),  # sK, 13 bits: positive
        ("x1", 0x16, 0xF0, 0xFF, 0, 0x0F),  # ~a & b
        ("x1", 0x17, 0xF0, 0x0F, 0, 0xFFFFFF00),  # ~a & ~b
        ("x1", 0x1B, 0, 0, 0, 0xFFFFFFFF),  # ~b
        ("x0", 0x1A, 0, 0, 0x7FF, 0),  # sK + 1
        ("x0", 0x1E, 0, 0xFFFFFFFF, 0, 0),  # b + 1
        ("x0", 0x30, 5, 7, 0x123, 0),
        ("x0", 0x16, 5, 7, 0, None),  # no result where X1 has its logic
        ("x0", 0x22, 5, 7, 0, None),
        ("x1", 0x1C, 5, 7, 0, None),  # a register transfer
    ],
)
def test_x_function_table(unit, head, a, b, k, result):
    assert x_result(unit, head, a, b, k) == result


def test_the_x_units_have_no_rule_at_their_idle_head():
    with pytest.raises(KeyError):
        x_result("x1", 0x1F, 0, 0, 0)
    with pytest.raises(KeyError):
        x_result("x0", 0x3F, 0, 0, 0)


def lanes(**at: int) -> list[int]:
    """32 lanes of 0 but the given ones, named l0..l31."""
    v = [0] * 32
    for key, value in at.items():
        v[int(key[1:])] = value
    return v


S3_NIBBLES = {
    "f31_34": 0,
    "f65_68": 0,
    "f92_95": 0,
    "f96_99": 0,
    "index": 0,
    "value": 0,
}


@pytest.mark.parametrize(
    "unit, head, a, b, scalars, expect",
    [
        # Saturating and wrapping adds apart at the top of the range.
        ("s0", 0x07, lanes(l0=0x7FFF), lanes(l0=1), {}, {0: 0x7FFF}),
        ("s0", 0x20, lanes(l0=0x7FFF), lanes(l0=1), {}, {0: 0x8000}),
        # The neighbour forms read a's next lane; lane 31's is in the next block.
        ("s0", 0x02, lanes(l1=5), lanes(l0=7), {}, {0: 2, 30: 0, 31: None}),
        # Halving rounds ties to even: 5 / 2 -> 2, 7 / 2 -> 4, -3 / 2 -> -2.
        (
            "s0",
            0x14,
            lanes(),
            lanes(l0=5, l1=7, l2=0xFFFD),
            {"s": 0},
            {0: 2, 1: 4, 2: 0xFFFE},
        ),
        # Unsigned max against the immediate: 0x8000 is above it.
        (
            "s0",
            0x34,
            lanes(),
            lanes(l0=0x8000, l1=3),
            {"imm": 0x515A},
            {0: 0x8000, 1: 0x515A},
        ),
        ("s0", 0x26, lanes(l0=0x8000, l1=0xFFFF), lanes(), {}, {0: 0x7FFF, 1: 1}),
        ("s0", 0x27, lanes(l0=0x8000), lanes(), {}, {0: 0x8000}),
        ("s0", 0x3A, lanes(), lanes(l0=1, l1=0x9000), {"imm": 2}, {0: 0, 1: 0x8FFE}),
        # S2 0x7 takes b where bit 1 of its nibble f88_91 is set, else a.
        ("s2", 0x07, lanes(l0=1), lanes(l0=2), {"f88_91": 0xA}, {0: 2, 31: 0}),
        ("s2", 0x07, lanes(l0=1), lanes(l0=2), {"f88_91": 0xD}, {0: 1}),
        # Rows on a state not identified: open.
        ("s1", 0x55, lanes(), lanes(l0=2), {}, {0: None}),
        # The clamps: lower bound 0 or ~bound; a negative bound is not predicted.
        (
            "s0",
            0x08,
            lanes(),
            lanes(l0=0xFFF0, l1=0x50, l2=0x200),
            {"imm": 0x100},
            {0: 0, 1: 0x50, 2: 0x100},
        ),
        ("s0", 0x08, lanes(), lanes(l0=5), {"imm": 0x8000}, {0: None, 31: None}),
        (
            "s0",
            0x09,
            lanes(),
            lanes(l0=0x8000, l1=0x7FFF),
            {"s": 0x40},
            {0: 0, 1: 0x40},
        ),
        (
            "s0",
            0x0B,
            lanes(),
            lanes(l0=0x8000, l1=0x7FFF, l2=0xFFF0),
            {"imm": 0x100},
            {0: 0xFEFF, 1: 0x100, 2: 0xFFF0},
        ),
        (
            "s0",
            0x0C,
            lanes(),
            lanes(l0=0x8000, l1=0x7FFF),
            {"s": 0x40},
            {0: 0xFFBF, 1: 0x40},
        ),
        # Bound a, per lane, of either sign: the sign bit of a wrapped 16-bit sum decides.
        (
            "s0",
            0x0A,
            lanes(l0=0x40, l1=0x40, l2=0xFFFF, l3=0xFFF0),
            lanes(l0=0x80, l1=0xFFFE, l2=3, l3=0x8000),
            {},
            {0: 0x40, 1: 0, 2: 0xFFFF, 3: 0},
        ),
        (
            "s0",
            0x0D,
            lanes(l0=0x40, l1=0x40, l2=0x8000, l3=0xFFFB, l4=0x7FFF, l5=0x8000),
            lanes(l0=0x80, l1=0xFF00, l2=3, l3=0xFFF6, l4=0x8000, l5=0x8000),
            {},
            {0: 0x40, 1: 0xFFBF, 2: 3, 3: 4, 4: 0x8000, 5: 0x8000},
        ),
        # The carry-in heads add bit k of the 32-bit mask r in lane k.
        (
            "s0",
            0x1F,
            lanes(l0=0xFFFF, l1=1, l2=5),
            lanes(l0=1, l1=2, l2=6),
            {"r": 0x80000003},
            {0: 1, 1: 4, 2: 11, 31: 1},
        ),
        (
            "s0",
            0x1E,
            lanes(l0=0x7FFF, l1=0x8000, l2=0x7FFE),
            lanes(l1=0xFFFF),
            {"r": 0b111},
            {0: 0x7FFF, 1: 0x8000, 2: 0x7FFF, 3: 0},
        ),
        (
            "s0",
            0x2C,
            lanes(l0=0xFFFF, l1=0xFFFE, l2=0x8000),
            lanes(l2=0x8000),
            {"r": 0b011},
            {0: 0xFFFF, 1: 0xFFFF, 2: 0xFFFF, 3: 0},
        ),
        (
            "s1",
            0x3E,
            lanes(l0=0xFFFF, l1=0x7FFF),
            lanes(l1=0x7FFF),
            {"r": 0x80000001},
            {0: 0, 1: 0xFFFE, 31: 1},
        ),
        (
            "s1",
            0x32,
            lanes(l0=0x7FFF, l1=0x8000),
            lanes(l1=0x8000),
            {"r": 0b01},
            {0: 0x7FFF, 1: 0x8000},
        ),
        # Logical and arithmetic shifts by imm & 15 (10 here).
        ("s1", 0x0C, lanes(), lanes(l0=0x8000), {"imm": 0x2A}, {0: 0x20}),
        ("s1", 0x0D, lanes(), lanes(l0=0x8000), {"imm": 0x2A}, {0: 0xFFE0}),
        ("s1", 0x0E, lanes(), lanes(l0=0x700, l1=0x100), {"imm": 0x2A}, {0: 2, 1: 0}),
        (
            "s1",
            0x0A,
            lanes(),
            lanes(l0=0x4000, l1=0xC000),
            {"imm": 1},
            {0: 0x7FFF, 1: 0x8000},
        ),
        ("s1", 0x11, lanes(), lanes(l0=0x8000, l1=3), {"imm": 1}, {0: 0xFFFF, 1: 6}),
        # The double-width shift of a : b, a the high half.
        (
            "s1",
            0x14,
            lanes(l0=1, l1=0),
            lanes(l0=0, l1=0x500),
            {"imm": 0x2A},
            {0: 0x40, 1: 1},
        ),
        ("s1", 0x0F, lanes(l0=0x7FFF), lanes(l0=0xFFFF), {"imm": 0x10}, {0: 0x7FFF}),
        # The floor shifts of a : b add the carry-in bit k of r; the result wraps.
        (
            "s1",
            0x16,
            lanes(l0=0x1234, l1=0xFFFF),
            lanes(l0=0x5678, l1=0xFFFF),
            {"imm": 0x24, "r": 0b10},
            {0: 0x4567, 1: 0},
        ),
        ("s1", 0x21, lanes(), lanes(l0=0x10), {"t": 0x13, "r": 1}, {0: 3, 1: 0}),
        ("s1", 0x28, lanes(l0=0x8000), lanes(), {"t": 4}, {0: 0x8000}),
        ("s1", 0x4A, lanes(), lanes(l0=0xFFFF, l1=1), {"t": 3}, {0: 3, 1: 1}),
        (
            "s2",
            0x03,
            lanes(),
            lanes(l0=0x100, l1=7),
            {"t": 0x101},
            {0: 0x100, 1: 0x707},
        ),
        ("s2", 0x00, lanes(), lanes(l0=3), {"imm": 10}, {0: 30}),
        ("s2", 0x0B, lanes(), lanes(l0=0x8000, l1=0x7FFF), {}, {0: 0, 1: 0xFFFF}),
        (
            "s3",
            0x0A,
            lanes(l0=1, l1=2),
            lanes(l0=3, l1=4),
            {},
            {0: 3, 1: 1, 2: 4, 3: 2},
        ),
        (
            "s3",
            0x0C,
            lanes(l1=1, l3=2),
            lanes(l1=3, l3=4),
            {},
            {0: 3, 1: 4, 16: 1, 17: 2},
        ),
        # The insert at lane f92_95 + 16 x (f31_34 bit 2), the other lanes b's.
        (
            "s3",
            0x10,
            lanes(),
            lanes(l4=9),
            S3_NIBBLES | {"f92_95": 4, "f31_34": 7, "value": 0x12345},
            {4: 9, 20: 0x2345},
        ),
        (
            "s3",
            0x15,
            lanes(),
            lanes(),
            S3_NIBBLES | {"index": 33, "value": 5},
            {1: 5, 0: 0},
        ),
        # The 14-bit immediate: f96_99 : f65_68 : f31_34 bits 3..2 : f92_95, sign-extended.
        (
            "s3",
            0x11,
            lanes(),
            lanes(),
            S3_NIBBLES | {"f96_99": 15, "f31_34": 12, "f92_95": 1},
            {0: 0xFC31, 31: 0xFC31},
        ),
        # Lane shifts by f92_95 as a signed nibble; the lanes moved in are not predicted.
        (
            "s3",
            0x1B,
            lanes(),
            lanes(l2=6, l3=7),
            S3_NIBBLES | {"f92_95": 2},
            {0: 6, 1: 7, 30: None},
        ),
        (
            "s3",
            0x1C,
            lanes(),
            lanes(l0=6),
            S3_NIBBLES | {"f92_95": 0xD},
            {2: None, 3: 6},
        ),
        # 0x1c and 0x22 only at the counts they were registered at.
        (
            "s3",
            0x22,
            lanes(),
            lanes(l0=6),
            S3_NIBBLES | {"f92_95": 9},
            {0: None, 2: None},
        ),
        (
            "s3",
            0x22,
            lanes(),
            lanes(l3=6),
            S3_NIBBLES | {"f92_95": 3},
            {0: 6, 29: None},
        ),
        # The select heads read all 32 bits of s as a lane mask, bit k for lane k.
        *(
            (
                "s0",
                head,
                lanes(l0=1, l1=2, l16=3, l31=4),
                [0x100 + k for k in range(32)],
                {"s": 0x80010001, "r": 0x1234, "imm": 0xABCD},
                {0: 0x100, 1: other[0], 16: 0x110, 31: 0x11F, 30: other[1]},
            )
            for head, other in (
                (0x46, (2, 0)),
                (0x45, (0x1234,) * 2),
                (0x44, (0xABCD,) * 2),
            )
        ),
        ("s0", 0x4B, lanes(), lanes(), {"s": 0x80000002}, {0: 0, 1: 1, 30: 0, 31: 1}),
    ],
)
def test_lane_function_table(unit, head, a, b, scalars, expect):
    got = lane_result(unit, head, a, b, scalars)
    assert {k: got[k] for k in expect} == expect


@pytest.mark.parametrize(
    "head, a, b, scalars, high",
    [
        # Unsigned a x signed b: 0xffff x 2 is 0x1fffe, not -2.
        (
            0x02,
            lanes(l0=0xFFFF, l1=0xFFFF),
            lanes(l0=2, l1=0xFFFF),
            {},
            {0: 1, 1: 0xFFFF},
        ),
        (0x05, lanes(), lanes(l0=0x8000, l1=0x7FFF), {"t": 3}, {0: 0xFFFE, 1: 1}),
        (0x00, lanes(), lanes(l0=0xFFFF), {"imm": 3}, {0: 2}),
        # A negative immediate multiplies as its 16 bits unsigned: 0x7fff x 0xffe2.
        (0x00, lanes(), lanes(l0=0x7FFF, l1=1), {"imm": -30}, {0: 0x7FF0, 1: 0}),
        (0x01, lanes(), lanes(l0=0xFFFF, l1=0x7FFF), {"imm": -30}, {0: 0, 1: 0xFFF1}),
        (0x03, lanes(), lanes(l0=0xFFFF), {"t": 3}, {0: 2}),
        (0x04, lanes(l0=0xFFFF), lanes(l0=0xFFFF), {}, {0: 0xFFFE}),
        (
            0x06,
            lanes(l0=0xFFFF, l1=0x8000),
            lanes(l0=0xFFFF, l1=0x7FFF),
            {},
            {0: 0, 1: 0xC000},
        ),
    ],
)
def test_product_high_halves(head, a, b, scalars, high):
    got = product_high(head, a, b, scalars)
    assert {k: got[k] for k in high} == high


def test_multiply_then_narrow():
    # S2 0x5 b x t, its halves into S1's a and b, then S1's double-width shift by 2: rounded
    # ties to even (0x1d; 18 / 4 -> 4) and saturated (0x29).
    b = lanes(l0=0x7FFF, l1=0x8000, l2=6, l3=1)

    def narrow(head, t):
        high = product_high(0x05, lanes(), b, {"t": t})
        low = lane_result("s2", 0x05, lanes(), b, {"t": t})
        return lane_result("s1", head, high, low, {"t": 2})[:4]

    assert narrow(0x1D, 3) == [0x5FFF, 0xA000, 4, 1]
    assert narrow(0x29, 0x7FFF) == [0x7FFF, 0x8000, 0x7FFF, 0x2000]


def test_heads_without_a_lane_result():
    # A reduction's result is a scalar, as is S1's scalar-read head's.
    assert lane_result("s3", 0x00, lanes(), lanes(), S3_NIBBLES) is None
    assert lane_result("s1", 0x54, lanes(), lanes(), {}) is None
    with pytest.raises(KeyError):
        lane_result("s0", 0x4F, lanes(), lanes(), {})  # open


@pytest.mark.parametrize(
    "head, a, b, k, vector",
    [
        (0x1C, 0x30, 0, 0x7FE, 0x2E),  # K is signed, 11 bits
        (0x1D, 0x105, 0, 0x403, 0x108),  # -0x3fd, then the wrap
        (0x1B, 0x3FF, 0, 1, 0),
        (0x21, 0x150, 0x7FFF, 0x7FF, 0x14F),  # A[b] = 0x7fff acts as -1; K not added
        (0x1E, 0x150, 0x2B0, 0, 0),
        (0x20, 0xFFFFFFFF, 1, 0, 0),  # 32-bit operands wrap the same way
    ],
)
def test_lane_memory_address(head, a, b, k, vector):
    assert lane_address(head, a, b, k) == vector


def test_lane_memory_aliases_modulo_its_size():
    memory = LaneMemory()
    memory.store(0x400, list(range(0xFFF0, 0x10010)))
    assert memory.load(0) == [v & 0xFFFF for v in range(0xFFF0, 0x10010)]
    assert memory.load(0x4001) == [None] * 32


def test_the_stock_copy_fills_one_element_per_byte_and_stores_its_low_byte():
    memory = LaneMemory()
    memory.copy_in(0x3FF, bytes(range(40)))
    assert memory.load(0x3FF) == list(range(32))
    assert memory.load(0)[:9] == [*range(32, 40), None]  # the wrap; the rest unknown
    memory.store(0x3FF, [0x1234] * 32)
    assert memory.copy_out(0x3FF, 34) == [0x34] * 32 + [32, 33]


def test_s3_0x9_looks_b_up_in_each_half():
    a = [3, 16, 0x8000, 0] * 4 + [0, 15, 0x100, 2] * 4
    b = list(range(100, 132))
    out = lane_result("s3", 0x09, a, b, S3_NIBBLES)
    assert out[:4] == [103, 0, 0, 100]
    assert out[16:20] == [116, 131, 0, 118]


def test_s3_0x7_and_0x8_select_blocks_of_b_by_a_s_first_lanes():
    b = list(range(100, 132))
    a = [3, 0, 15, 0xFFFF] + [1] * 28
    assert lane_result("s3", 0x07, a, b, S3_NIBBLES) == [*b[24:32], *b[0:8], *[0] * 16]
    assert lane_result("s3", 0x08, a, b, S3_NIBBLES) == [*[0] * 16, *b[0:16]]
    a = [1, 1, 2, 0] + [9] * 28
    assert lane_result("s3", 0x07, a, b, S3_NIBBLES) == [
        *b[8:16],
        *b[8:16],
        *b[16:24],
        *b[0:8],
    ]
    assert lane_result("s3", 0x08, a, b, S3_NIBBLES) == [*b[16:32], *b[16:32]]
