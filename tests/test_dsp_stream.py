"""The stream engine on synthetic values: grammar, loads, stores, timing, going off script."""

import pytest

from fenykl.dsp import stream
from fenykl.dsp.emu import LOCAL_BASE, Boot
from fenykl.dsp.fields import LANE_VECTORS, Confidence
from fenykl.dsp.machine import Machine, Sv, Vec
from fenykl.dsp.semantics import UnknownSemantics
from fenykl.dsp.state import DRAM_BYTES, Bytes
from fenykl.dsp.word import IDLE, Word

V, I, H = Confidence.VERIFIED, Confidence.INFERRED, Confidence.HYPOTHESIS
SRC, DST = 0xA0000000, 0xA1000000
SRC_V, DST_V = SRC // 64 % LANE_VECTORS, DST // 64 % LANE_VECTORS
WAIT = None
"""A 0x1d in a list of values."""


def key(channel: int, reg: int) -> int:
    return reg << 11 | channel << 6 | 0x21


def block(channel: int, *regs: int) -> list[int]:
    return [0x20 | channel << 6, *regs]


def lanes(channel: int, r3: int, r5: int = 0x10, r6: int = 0, r7: int = 1, r1=0, r4=0):
    """A channel on the lane memory, one 16-bit element per lane."""
    return block(channel, 1, r1, 0x20, r3, r4, r5, r6, r7)


def load(channel: int, frm: int, to: int) -> list[int]:
    return [channel << 6 | stream.LOAD, frm, to]


def store(channel: int, frm: int, to: int) -> list[int]:
    return [channel << 6 | stream.STORE, frm, to]


UNKNOWN = Sv(None)


def drive(m: Machine, e: stream.Engine, values: list[int | Sv | None]) -> None:
    for k, v in enumerate(values):
        value = Sv(0) if v is WAIT else v if isinstance(v, Sv) else Sv(v)
        e.transfer(m, k, 0x1D if v is WAIT else 0x1C, value, k)


def machine(data: bytes = b"") -> Machine:
    m = Machine(Boot())
    m.boot.put(SRC, data, "src")
    return m


def test_a_byte_copy_through_a_block_and_single_keys():
    """Channel 0 by a block, channel 2 by eight single keys; the store is channel 2's."""
    data = bytes(range(256)) * 4
    m, e = machine(data), stream.Engine()
    singles = []
    for reg, v in enumerate((1, 0, 0x20, 0x20, 0, 0x10, 0x40, 1)):
        singles += [key(2, reg), v]
    drive(
        m,
        e,
        [*lanes(0, 0x20, r6=0x40), *singles, *load(0, SRC, 0), WAIT]
        + [*store(2, 0, DST), WAIT],
    )
    assert m.memory[1].lanes == tuple(range(32, 64))
    got = m.boot.read(DST, len(data))
    assert all(got.known) and bytes(got.data) == data
    assert not e.off and not e.outstanding


def test_a_16_bit_load_puts_one_element_per_lane():
    data = bytes(range(256))
    m, e = machine(data), stream.Engine()
    drive(m, e, [*lanes(0, 4, r5=8, r6=0x10), *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == tuple(
        data[2 * i] | data[2 * i + 1] << 8 for i in range(32)
    )
    assert 4 not in m.memory


def test_a_12_bit_load_zero_fills_the_last_vector():
    """9 lines of 10 elements: 90 elements fill three vectors, lanes 26..31 of the last 0."""
    elements = [(0x9A5 + 37 * i) & 0xFFF for i in range(90)]
    data = b"".join(
        sum(x << 12 * k for k, x in enumerate(elements[10 * j : 10 * j + 10])).to_bytes(
            16, "little"
        )
        for j in range(9)
    )
    m, e = machine(data), stream.Engine()
    drive(m, e, [*lanes(1, 3, r5=0xA, r6=9), *load(1, SRC, 0x40), WAIT])
    assert m.memory[1].lanes + m.memory[2].lanes == tuple(elements[:64])
    assert m.memory[3].lanes == (*elements[64:], *[0] * 6)


def test_rows_step_by_their_pitches():
    data = bytes(range(256)) * 4
    m, e = machine(data), stream.Engine()
    values = lanes(0, 2, r6=4, r7=2, r1=0x600, r4=0x200)
    drive(m, e, [*values, *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == tuple(range(32))
    assert m.memory[0x18].lanes == tuple(data[0x200:0x220])
    assert set(m.memory) == {0, 1, 0x18, 0x19}


def test_a_row_address_is_floored_after_the_pitch_and_wraps_at_32_bits():
    data = bytes(range(256)) * 4
    m, e = machine(data), stream.Engine()
    drive(m, e, [*lanes(0, 2, r6=4, r7=2, r1=0x600, r4=0x208), *load(0, SRC, 0), WAIT])
    assert m.memory[0x18].lanes == tuple(data[0x200:0x220])
    m, e = machine(data), stream.Engine()
    drive(
        m,
        e,
        [
            *lanes(0, 2, r6=4, r7=2, r1=0x600, r4=0xFFFFFE00),
            *load(0, SRC + 0x200, 0),
            WAIT,
        ],
    )
    assert m.memory[0x18].lanes == tuple(data[0:0x20])


def test_the_dram_address_and_the_vector_offset_are_floored():
    data = bytes(range(64))
    m, e = machine(data), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2), *load(0, SRC + 4, 0x20), WAIT])
    assert m.memory[0].lanes == tuple(range(32))
    m.memory[0] = Vec(tuple(range(100, 132)))
    drive(m, e, [*store(0, 0, DST + 4), WAIT])
    assert bytes(m.boot.read(DST, 32).data) == bytes(range(100, 132))


def test_local_memory_both_ways():
    data = bytes(range(256))
    m, e = machine(data), stream.Engine()
    local = block(1, 2, 0, 4, 0x40, 0, 0x10, 0x10, 1)
    drive(m, e, [*local, *load(1, SRC, 0x400), WAIT, *store(1, 0x400, DST), WAIT])
    got = m.boot.local.read(LOCAL_BASE + 0x400, 256)
    assert all(got.known) and bytes(got.data) == data
    assert bytes(m.boot.read(DST, 256).data) == data
    assert not m.memory


def test_bytes_into_16_bit_local_elements_are_zero_extended():
    data = bytes(range(0x80, 0x90))
    m, e = machine(data), stream.Engine()
    drive(m, e, [*block(0, 2, 0, 2, 8, 0, 0x10, 1, 1), *load(0, SRC, 0), WAIT])
    got = m.boot.local.read(LOCAL_BASE, 32)
    assert all(got.known) and bytes(got.data) == b"".join(bytes([x, 0]) for x in data)


def test_a_local_store_of_16_bit_elements_writes_their_low_bytes():
    m, e = machine(), stream.Engine()
    m.boot.local.write(LOCAL_BASE, Bytes.of(bytes(range(64)), 0))
    drive(m, e, [*block(0, 2, 0, 2, 0x10, 0, 0x10, 2, 1), *store(0, 0, DST), WAIT])
    assert bytes(m.boot.read(DST, 32).data) == bytes(range(0, 64, 2))


def test_a_local_store_of_16_bit_elements_at_r5_0x08_writes_16_bits_each():
    m, e = machine(), stream.Engine()
    m.boot.local.write(LOCAL_BASE, Bytes.of(bytes(range(0x80, 0xA0)), 0))
    drive(m, e, [*block(0, 2, 0, 2, 8, 0, 0x08, 2, 1), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == bytes(range(0x80, 0xA0)) and set(got.taint) == {V}


def test_32_bit_elements_through_local_memory_keep_their_high_halves():
    data = bytes(range(0x40, 0x80))
    m, e = machine(data), stream.Engine()
    words = block(0, 2, 0, 1, 0x10, 0, 4, 4, 1)
    drive(m, e, [*words, *load(0, SRC, 0x400), WAIT, *store(0, 0x400, DST), WAIT])
    assert bytes(m.boot.local.read(LOCAL_BASE + 0x400, 64).data) == data
    assert bytes(m.boot.read(DST, 64).data) == data


def test_a_load_of_more_units_than_its_elements_never_ends():
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="never ends"):
        drive(m, stream.Engine(), lanes(0, 3, r6=4) + load(0, SRC, 0))


def test_a_load_of_fewer_units_fills_them_and_ends():
    m, e = machine(bytes(range(64))), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=4), *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == tuple(range(32)) and set(m.memory) == {0}


def test_a_store_of_more_elements_than_the_memory_offers_never_ends():
    m = machine()
    with pytest.raises(UnknownSemantics, match="never ends"):
        drive(m, stream.Engine(), lanes(0, 1, r6=4) + store(0, 0, DST))


def test_a_wait_with_nothing_outstanding():
    """After a command it never ends; in a job that handed none it was not measured."""
    m, e = machine(bytes(64)), stream.Engine()
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT])
    with pytest.raises(UnknownSemantics, match="never ends"):
        drive(m, e, [WAIT])
    with pytest.raises(UnknownSemantics, match="handed no command: not measured"):
        drive(machine(), stream.Engine(), [WAIT])


def test_a_load_of_no_elements_ends_with_its_vectors_unknown():
    m, e = machine(bytes(64)), stream.Engine()
    m.memory[1] = Vec((7,) * 32, V)
    drive(m, e, [*lanes(0, 2, r6=0), *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == (None,) * 32 and m.memory[1].lanes == (None,) * 32
    assert e.cap == I and not e.off


@pytest.mark.parametrize(("r6", "seen", "footprint"), [(0x40, 32, 32), (0x100, 32, 128)])
def test_a_load_of_no_units_that_ran_keeps_the_vectors_seen(r6, seen, footprint):
    m, e = machine(bytes(16 * r6)), stream.Engine()
    for v in range(footprint + 1):
        m.memory[v] = Vec((v,) * 32, V)
    drive(m, e, [*lanes(0, 0, r6=r6), *load(0, SRC, 0), WAIT])
    assert all(m.memory[v] == Vec((v,) * 32, I) for v in range(seen))
    assert all(m.memory[v].lanes == (None,) * 32 for v in range(seen, footprint))
    assert m.memory[footprint] == Vec((footprint,) * 32, V)
    assert e.cap == I and not e.off


def test_a_store_of_no_units_ends_and_writes_nothing():
    m, e = machine(), stream.Engine()
    m.boot.put(DST, bytes([0xA5]) * 64, "prefill")
    drive(m, e, [*lanes(0, 0, r6=4), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 64)
    assert bytes(got.data) == bytes([0xA5]) * 64 and min(got.taint) == V
    assert e.cap == I and not e.off


@pytest.mark.parametrize(("r3", "footprint"), [(0, 0), (0x20, 0x400), (0x80, 0x1000)])
def test_a_store_of_no_lines_that_ran_writes_nothing(r3, footprint):
    m, e = machine(), stream.Engine()
    m.boot.put(DST, bytes([0xA5]) * 0x2000, "prefill")
    drive(m, e, [*lanes(0, r3, r6=0), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 0x2000)
    assert bytes(got.data) == bytes([0xA5]) * 0x2000
    assert set(got.taint[:footprint]) <= {I} and set(got.taint[footprint:]) == {V}
    assert e.cap == I and not e.off


@pytest.mark.parametrize(
    ("values", "why"),
    [
        (
            [*block(0, 2, 0, 4, 4, 0, 0x10, 0, 1), *load(0, SRC, 0)],
            "local load at r3 4, r6 0",
        ),
        (
            [*block(0, 2, 0, 4, 0, 0, 0x10, 4, 1), *load(0, SRC, 0)],
            "local load at r3 0, r6 4",
        ),
        (
            [*block(0, 2, 0, 4, 0, 0, 0x10, 4, 1), *store(0, 0, DST)],
            "local store at r3 0, r6 4",
        ),
        (
            [*block(0, 2, 0, 4, 4, 0, 0x10, 0, 1), *store(0, 0, DST)],
            "local store at r3 4, r6 0",
        ),
        ([*lanes(0, 0, r6=4), *load(0, SRC, 0)], "lane-memory load at r3 0, r6 4"),
        ([*lanes(0, 0, r6=0), *load(0, SRC, 0)], "lane-memory load at r3 0, r6 0"),
        ([*lanes(0, 2, r6=0), *store(0, 0, DST)], "lane-memory store at r3 2, r6 0"),
        ([*lanes(0, 0, r5=0x08, r6=0), *store(0, 0, DST)], "lane-memory store at r3 0, r6 0"),
        ([*lanes(0, 0x40, r6=0), *store(0, 0, DST)], "lane-memory store at r3 64, r6 0"),
    ],
)
def test_zero_counts_not_measured_go_off_script(values, why):
    m, e = machine(bytes(64)), stream.Engine()
    drive(m, e, values)
    assert f"{why}: not measured" in e.off


@pytest.mark.parametrize(
    ("r5", "r6", "r3", "tier"),
    [(0x04, 8, 2, V), (0x04, 16, 2, I), (0x10, 2, 2, I), (0x08, 4, 2, I)],
)
def test_zeros_under_16_units_per_vector_are_verified_where_measured(r5, r6, r3, tier):
    m, e = machine(bytes(range(1, 65))), stream.Engine()
    regs = block(0, 1, 0, 0x10, r3, 0, r5, r6, 1)
    drive(m, e, [*regs, *load(0, SRC, 0), WAIT])
    assert m.memory[1].lanes == (0,) * 32 and m.memory[1].tier == tier


def test_a_command_that_moves_nothing_counts_at_inferred():
    m, e = machine(bytes(64)), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2), stream.NOTHING_MOVES, 0, 0, WAIT])
    assert e.lane_tier() == I


@pytest.mark.parametrize(("rows", "tier"), [(5, V), (6, I), (7, I), (8, V), (9, I)])
def test_lane_load_rows_are_verified_at_the_counts_run(rows, tier):
    """Measured on the camera: 5 and 8 rows, each at its DRAM pitch (also 1..4)."""
    data = bytes(range(256)) * 8
    m, e = machine(data), stream.Engine()
    values = lanes(0, 1, r6=2, r7=rows, r1=0x80, r4=0x80)
    drive(m, e, [*values, *load(0, SRC, 0), WAIT])
    last = rows - 1
    assert m.memory[2 * last].lanes == tuple(data[0x80 * last : 0x80 * last + 32])
    assert m.memory[2 * last].tier == tier and 1 not in m.memory


@pytest.mark.parametrize(("rows", "tier"), [(3, V), (4, V), (5, I)])
def test_lane_store_rows_are_verified_up_to_four(rows, tier):
    m, e = machine(), stream.Engine()
    for v in range(rows):
        m.memory[v] = Vec(tuple(range(v, v + 32)))
    drive(m, e, [*lanes(0, 1, r6=2, r7=rows, r1=0x40, r4=0x100), *store(0, 0, DST), WAIT])
    for k in range(rows):
        got = m.boot.read(DST + 0x100 * k, 32)
        assert bytes(got.data) == bytes(range(k, k + 32)) and set(got.taint) == {tier}


def test_a_store_pitch_of_0x108_steps_0x100_at_two_rows():
    """Measured on the camera: row 1 at +0x100, not +0x108 or +0x110."""
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    m.memory[1] = Vec(tuple(range(100, 132)))
    drive(m, e, [*lanes(0, 1, r6=2, r7=2, r1=0x40, r4=0x108), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST + 0x100, 32)
    assert bytes(got.data) == bytes(range(100, 132)) and set(got.taint) == {V}


@pytest.mark.parametrize(("r4", "r7"), [(0x104, 2), (0x108, 3)])
def test_other_store_pitches_off_16_bytes_go_off_script(r4, r7):
    m, e = machine(), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2, r7=r7, r1=0x40, r4=r4), *store(0, 0, DST), WAIT])
    assert "DRAM pitch" in e.off


def test_two_rows_at_half_a_vector_share_it_and_the_later_lands():
    """Measured on the camera: row 1 fills row 0's vector, the next vector keeps its content."""
    data = bytes(range(256)) * 2
    m, e = machine(data), stream.Engine()
    m.memory[1] = Vec((7,) * 32)
    drive(m, e, [*lanes(0, 1, r6=2, r7=2, r1=0x20, r4=0x100), *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == tuple(data[0x100:0x120]) and m.memory[0].tier == V
    assert m.memory[1].lanes == (7,) * 32


def test_other_lane_pitches_off_whole_vectors_go_off_script():
    m, e = machine(bytes(1024)), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2, r7=3, r1=0x20, r4=0x100), *load(0, SRC, 0), WAIT])
    assert "memory pitch" in e.off


def test_a_store_under_r0_0x40_writes_nothing():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(1, 33)))
    m.boot.put(DST, b"\xa5" * 32, "prefill")
    drive(m, e, [*block(0, 0x40, 0, 0x20, 1, 0, 0x10, 2, 1), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == b"\xa5" * 32 and all(got.known)
    assert not e.off and not e.outstanding


def test_a_load_under_r0_0x40_raises():
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="r0 0x40"):
        drive(m, stream.Engine(), [*block(0, 0x40, 0, 0x20, 1, 0, 0x10, 2, 1), *load(0, SRC, 0)])


def test_a_local_load_of_bytes_into_32_bit_words_never_ends():
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="never ends"):
        drive(m, stream.Engine(), [*block(0, 2, 0, 1, 8, 0, 0x10, 2, 1), *load(0, SRC, 0)])


def test_other_loads_into_32_bit_words_go_off_script():
    m, e = machine(bytes(64)), stream.Engine()
    drive(m, e, [*block(0, 2, 0, 1, 8, 0, 0x08, 2, 1), *load(0, SRC, 0), WAIT])
    assert "16-bit elements at r2 0x1" in e.off


def test_a_load_completed_by_the_jobs_end_is_inferred():
    m, e = machine(bytes(range(64))), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2), *load(0, SRC, 0)])
    e.finish(m)
    assert m.memory[0].lanes[1] == 1 and m.memory[0].tier == I


def test_a_loads_vectors_are_unknown_until_it_is_waited_for():
    m, e = machine(bytes(range(64))), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0)])
    assert m.memory[0].lanes == (None,) * 32 and e.lane_tier() == H
    e.finish(m)
    assert m.memory[1].lanes == tuple(range(32, 64)) and e.lane_tier() == V


def test_a_vector_written_while_the_load_runs_is_unknown():
    m, e = machine(bytes(range(64))), stream.Engine()
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0)])
    m.memory[1] = Vec(tuple(range(32)))
    drive(m, e, [WAIT])
    assert m.memory[0].lanes == tuple(range(32))
    assert m.memory[1].lanes == (None,) * 32


def test_a_load_reads_dram_before_a_store_handed_after_it():
    """In the order handed; on one DRAM range that order was not measured (inferred)."""
    m, e = machine(bytes(range(64))), stream.Engine()
    m.memory[5] = Vec(tuple(range(100, 132)))
    drive(m, e, [*lanes(0, 1, r6=2), *load(0, SRC, 0), *store(0, 5 * 64, SRC)])
    drive(m, e, [WAIT, WAIT])
    assert m.memory[0].lanes == tuple(range(32)) and m.memory[0].tier == I


def test_a_load_reads_what_a_store_handed_before_it_wrote():
    m, e = machine(bytes(range(64))), stream.Engine()
    m.memory[5] = Vec(tuple(range(100, 132)))
    drive(m, e, [*lanes(0, 1, r6=2), *store(0, 5 * 64, SRC), *load(0, SRC, 0)])
    drive(m, e, [WAIT, WAIT])
    assert m.memory[0].lanes == tuple(range(100, 132)) and m.memory[0].tier == I


def test_a_lane_source_rewritten_while_the_store_is_queued_is_inferred():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    drive(m, e, [*lanes(0, 1, r6=2), *store(0, 0, DST)])
    m.memory[0] = Vec((0,) * 32)
    drive(m, e, [WAIT])
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == bytes(range(32)) and set(got.taint) == {I}


def test_a_local_source_rewritten_while_the_store_is_queued_is_inferred():
    m, e = machine(), stream.Engine()
    m.boot.local.write(LOCAL_BASE + 0x400, Bytes.of(bytes(range(32)), 0))
    drive(m, e, [*block(0, 2, 0, 4, 8, 0, 0x10, 2, 1), *store(0, 0x400, DST)])
    m.boot.local.write(LOCAL_BASE + 0x404, Bytes.of(bytes(4), 0))
    drive(m, e, [WAIT])
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == bytes(range(32)) and set(got.taint) == {I}


def test_a_store_whose_source_holds_still_stays_verified():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    drive(m, e, [*lanes(0, 1, r6=2), *store(0, 0, DST), WAIT])
    assert set(m.boot.read(DST, 32).taint) == {V}


def test_a_command_with_a_register_not_handed_moves_nothing():
    m, e = machine(bytes(64)), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    m.boot.local.write(LOCAL_BASE, Bytes.of(bytes(4), 0))
    drive(m, e, [key(0, 3), 2, key(0, 6), 4, *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == tuple(range(32))
    assert not e.off and e.lane_tier() == I
    assert set(m.boot.local.read(LOCAL_BASE, 4).taint) == {I}


def test_a_store_with_a_register_not_handed_leaves_dram_inferred():
    m, e = machine(), stream.Engine()
    m.boot.put(DST, bytes(range(32)), "prefill")
    drive(m, e, [key(0, 3), 1, key(0, 6), 2, *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == bytes(range(32)) and set(got.taint) == {I}


def test_kind_2_loads_as_kind_0_at_inferred():
    m, e = machine(bytes(range(64))), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2), 0x02, SRC, DST + 0x40, WAIT])
    assert m.memory[DST_V + 1].lanes == tuple(range(32)) and e.lane_tier() == I


def test_kind_4_stores_as_kind_6_at_inferred():
    m, e = machine(bytes(range(64))), stream.Engine()
    m.boot.put(DST, bytes(32), "prefill")
    drive(
        m,
        e,
        [*lanes(0, 1, r6=2), *load(0, SRC, 64 * SRC_V), WAIT, 0x04, SRC, DST, WAIT],
    )
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == bytes(range(32)) and set(got.taint) == {I}


@pytest.mark.parametrize("kind", [2, 3, 4, 5, 7, 8])
def test_kinds_outside_their_shapes_raise(kind):
    m = machine(bytes(256))
    with pytest.raises(UnknownSemantics, match="outside the shapes it ran in"):
        drive(m, stream.Engine(), [*lanes(0, 5, r6=10), kind, SRC, DST, WAIT])


def test_kind_2_loads_16_bit_elements_verified():
    m, e = (
        machine(b"".join(k.to_bytes(2, "little") for k in range(32))),
        stream.Engine(),
    )
    drive(m, e, [*lanes(0, 1, r5=8, r6=4), 0x02, SRC, DST + 0x40, WAIT])
    assert m.memory[DST_V + 1].lanes == tuple(range(32)) and e.lane_tier() == V


def test_kind_2_loads_three_rows_at_their_pitches():
    m, e = machine(bytes(k & 0xFF for k in range(0x300))), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2, r7=3, r1=0x80, r4=0x100), 0x02, SRC, DST, WAIT])
    assert [m.memory[DST_V + v].lanes[0] for v in (0, 2, 4)] == [0, 0, 0]
    assert DST_V + 1 not in m.memory and m.memory[DST_V + 2].lanes == tuple(range(32))
    assert e.lane_tier() == V


@pytest.mark.parametrize("kind", [3, 5, 7])
def test_kinds_3_5_and_7_move_nothing_at_inferred(kind):
    m, e = machine(bytes(range(64))), stream.Engine()
    m.memory[0] = Vec((7,) * 32, V)
    drive(m, e, [*lanes(0, 1, r6=2), kind, SRC, DST, WAIT])
    assert m.memory[0].lanes == (7,) * 32 and e.cap == I


@pytest.mark.parametrize(("r3", "r5", "r6"), [(1, 0x10, 2), (1, 8, 4), (4, 0x10, 8)])
def test_kind_8_fills_r3_vectors_with_the_low_16_bits_of_from(r3, r5, r6):
    m, e = machine(), stream.Engine()
    m.boot.put(DST, bytes(32), "prefill")
    drive(m, e, [*lanes(0, r3, r5=r5, r6=r6), 0x08, 0x12FCB800, DST + 0x40 * 5, WAIT])
    for v in range(DST_V + 5, DST_V + 5 + r3):
        assert m.memory[v].lanes == (0xB800,) * 32 and m.memory[v].tier == V
    assert DST_V + 5 + r3 not in m.memory
    assert bytes(m.boot.read(DST, 32).data) == bytes(32)


def local_fill(r2: int, r3: int, r6: int, channel: int = 0) -> list[int]:
    """A channel on the local memory as kind 8 ran there."""
    return block(channel, 2, 0, r2, r3, 0, 0x10, r6, 1)


@pytest.mark.parametrize(
    ("regs", "to", "word", "tier"),
    [
        ((4, 5, 2), 0x500, 0x21212121, I),  # each shape ran in one job
        ((2, 4, 4), 0x700, 0x2A212A21, I),
        ((1, 3, 5), 0x700, 0x00002A21, I),
        ((2, 8, 2), 0x600, 0x2A212A21, I),
        ((4, 5, 2), 0x540, 0x21212121, I),  # another "to"
    ],
)
def test_kind_8_on_local_memory_fills_r3_words_with_copies_of_the_low_element(
    regs, to, word, tier
):
    m, e = machine(), stream.Engine()
    m.boot.local.write(LOCAL_BASE + to, Bytes.of(bytes(range(64)), 0))
    r3 = regs[1]
    drive(m, e, [*local_fill(*regs), 0x08, 0x2A21, to])
    assert not any(m.boot.local.read(LOCAL_BASE + to, 4 * r3).known)  # in flight
    drive(m, e, [WAIT])
    got = m.boot.local.read(LOCAL_BASE + to, 64)
    assert bytes(got.data[: 4 * r3]) == word.to_bytes(4, "little") * r3
    assert all(got.known) and got[: 4 * r3].weakest() == tier
    assert bytes(got.data[4 * r3 :]) == bytes(range(4 * r3, 64))
    assert not m.memory and not e.off


@pytest.mark.parametrize(
    ("fill", "frm", "to", "why"),
    [
        (local_fill(2, 7, 2), 0x11, 0x400, "outside the shapes"),
        (local_fill(4, 5, 1), 0x11, 0x400, "outside the shapes"),
        (local_fill(4, 5, 2, channel=1) + [0x48], 0x11, 0x500, "channel 1"),
        (local_fill(4, 5, 2), 0x11, 0x200, "fill to 0x200"),
        (local_fill(4, 5, 2), 0x11, 0x502, "fill to 0x502"),
        (local_fill(4, 5, 2), 0x1180, 0x500, "8-bit local fill"),
        (local_fill(2, 4, 4), 0x8001, 0x700, "16-bit local fill"),
        (local_fill(2, 4, 4), 0x12340011, 0x700, "16-bit local fill"),
        (local_fill(1, 3, 5), 0x8001, 0x700, "32-bit local fill"),
    ],
)
def test_an_unmeasured_local_fill_raises(fill, frm, to, why):
    """A list ending in its command key runs that key; others kind 8 on channel 0."""
    command = [] if len(fill) > 9 else [0x08]
    m = machine()
    with pytest.raises(UnknownSemantics, match=why):
        drive(m, stream.Engine(), [*fill, *command, frm, to])


def test_a_local_fill_raced_by_a_write_leaves_those_bytes_unknown():
    m, e = machine(), stream.Engine()
    drive(m, e, [*local_fill(2, 4, 4), 0x08, 0x1111, 0x600])
    m.boot.local.write(LOCAL_BASE + 0x604, Bytes.of(bytes(4), 0))
    drive(m, e, [WAIT])
    got = m.boot.local.read(LOCAL_BASE + 0x600, 16)
    assert list(got.known) == [1] * 4 + [0] * 4 + [1] * 8


def test_a_store_of_vectors_nothing_wrote_is_listed():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec((7,) * 32, V)
    drive(m, e, [*lanes(0, 2, r6=4), *store(0, 0, DST), WAIT])
    assert e.unwritten == [("transfer 11 (word 11)", 1)]


def test_more_commands_outstanding_than_the_depth_raise():
    m = machine(bytes(64))
    loads = [v for k in range(stream.DEPTH) for v in load(0, SRC, 64 * k)]
    drive(m, stream.Engine(), [*lanes(0, 1, r6=2), *loads, *[WAIT] * stream.DEPTH])
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="more than 44 commands outstanding"):
        drive(m, stream.Engine(), [*lanes(0, 1, r6=2), *loads, *load(0, SRC, 0)])


def test_an_unmeasured_kind_raises():
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="kind 0x9"):
        drive(m, stream.Engine(), [*lanes(0, 1, r6=2), 0x09, SRC, DST])


def test_an_off_script_store_leaves_its_destination_unknown():
    m, e = machine(bytes(64)), stream.Engine()
    m.boot.put(DST, bytes(32), "prefill")
    drive(m, e, [*lanes(0, 1, r5=0x0E, r6=2), *store(0, 0, DST), WAIT])
    assert "format 0xe" in e.off and not any(m.boot.read(DST, 32).known)


def test_a_local_load_after_going_off_script_leaves_its_destination_unknown():
    m, e = machine(bytes(64)), stream.Engine()
    m.boot.local.write(LOCAL_BASE + 0x400, Bytes.of(bytes(64), 0))
    local = block(1, 2, 0, 4, 0x10, 0, 0x10, 4, 1)
    drive(m, e, [*lanes(0, 1, r5=0x0E, r6=2), *store(0, 0, DST), *local])
    drive(m, e, [*load(1, SRC, 0x400), WAIT, WAIT])
    assert e.off and not any(m.boot.local.read(LOCAL_BASE + 0x400, 64).known)


def test_a_store_to_an_unknown_address_raises():
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="DRAM address unknown"):
        drive(m, stream.Engine(), [*lanes(0, 1, r6=2), *store(0, 0, UNKNOWN)])


def test_store_rows_at_one_address_write_it_once_the_last_row():
    """At DRAM pitch 0 every row rewrites the first: of two rows, each from the next vector, the
    last one's vector lands."""
    data = bytes(range(64))
    m, e = machine(data), stream.Engine()
    store_rows = lanes(0, 1, r6=2, r7=2, r1=0x40)
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT, *store_rows])
    drive(m, e, [*store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == data[32:]
    assert set(got.taint) == {I}  # which row lands was not measured
    m, e = machine(), stream.Engine()
    # Rows of no elements reach no DRAM: off script for the count, not raising for the reach.
    drive(m, e, [*lanes(0, 1, r6=0, r7=0xFFFFFFFF), *store(0, 0, DST), WAIT])
    assert e.off.endswith("r3 1, r6 0: not measured")


def test_dram_behind_an_outstanding_store_is_inferred():
    """A load of DRAM a store still outstanding writes, and a second store over it, run in order
    at inferred; a load of other DRAM beside them stays verified."""
    data = bytes(range(64))
    m, e = machine(data), stream.Engine()
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT, *lanes(1, 1, r6=2)])
    drive(m, e, [*store(1, 0, DST), *load(1, DST + 16, 0x400), *load(1, SRC + 32, 0x480)])
    drive(m, e, [*store(1, 0x40, DST), WAIT, WAIT, WAIT, WAIT])
    assert m.memory[0x10].lanes[:16] == tuple(data[16:32])
    assert m.memory[0x10].tier == I
    assert m.memory[0x12].tier == V
    got = m.boot.read(DST, 32)
    assert bytes(got.data) == data[32:] and set(got.taint) == {I}


def test_a_store_row_wraps_at_the_top_of_dram():
    m, e = machine(bytes(range(32))), stream.Engine()
    top = 0x80000000 + DRAM_BYTES - 16
    drive(m, e, [*lanes(0, 1, r6=2), *load(0, SRC, 0), WAIT, *store(0, 0, top), WAIT])
    assert bytes(m.boot.read(top, 16).data) == bytes(range(16))
    assert bytes(m.boot.read(0x80000000, 16).data) == bytes(range(16, 32))


def test_a_store_whose_rows_exceed_dram_raises():
    """2^22 rows 0x100 apart, each 0x200 bytes: they write twice the DRAM alias space."""
    values = lanes(0, 1, r6=0x20, r7=DRAM_BYTES // 0x100, r4=0x100)
    with pytest.raises(UnknownSemantics, match="more than all of DRAM"):
        drive(machine(), stream.Engine(), [*values, *store(0, 0, DST)])


def test_a_load_into_an_unknown_memory_raises():
    m = machine(bytes(64))
    regs = block(0, UNKNOWN, 0, 4, 0x10, 0, 0x10, 4, 1)
    with pytest.raises(UnknownSemantics, match="r0 unknown"):
        drive(m, stream.Engine(), [*regs, *load(0, SRC, 0x400)])


def test_a_key_with_bits_outside_its_fields_loses_the_grammar():
    m, e = machine(bytes(64)), stream.Engine()
    with pytest.raises(UnknownSemantics, match="grammar was lost"):
        drive(m, e, [0x234, *block(0, 2, 0, 4, 0x10, 0, 0x10, 4, 1)])
    assert "bits outside" in e.off and e.lost


def test_a_wait_inside_a_commands_values_raises():
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="inside a command"):
        drive(m, stream.Engine(), [*lanes(0, 1, r6=2), stream.LOAD, SRC, WAIT])


@pytest.mark.parametrize(
    ("fmt", "lines", "check"),
    [
        (0x0C, 2, lambda acc, line: [acc >> 10 * k & 0x3FF for k in range(12)]),
        (0x08, 2, lambda acc, line: [acc >> 16 * k & 0xFFFF for k in range(8)]),
        (0x04, 2, lambda acc, line: [acc >> 32 * k & 0xFFFFFFFF for k in range(4)]),
    ],
)
def test_store_formats_pack_the_lanes(fmt, lines, check):
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(0x300 + k for k in range(32)))
    drive(m, e, [*lanes(1, 1, r5=fmt, r6=lines), *store(1, 0, DST), WAIT])
    got = m.boot.read(DST, 16 * lines)
    per = 128 // stream.STORE_FORMATS[fmt][0]
    for line in range(lines):
        acc = int.from_bytes(got.data[16 * line : 16 * line + 16], "little")
        assert check(acc, line) == [0x300 + per * line + k for k in range(per)]
    assert all(got.known)


def test_store_format_0x0d_writes_zeros():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(1, 33)))
    drive(m, e, [*lanes(0, 1, r5=0x0D, r6=2), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 32)
    assert all(got.known) and bytes(got.data) == bytes(32)


@pytest.mark.parametrize(
    ("fmt", "tier"),
    [(0x00, V), (0x40, V), *((f, I) for f in (0x01, 0x46, 0x80, 0x200, 0x400))],
)
def test_the_zero_formats_write_zeros(fmt, tier):
    """0x00 and 0x40 pre-registered on the camera; the rest measured after the run."""
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(1, 33)))
    m.boot.put(DST, b"\xa5" * 48, "prefill")
    drive(m, e, [*lanes(0, 1, r5=fmt, r6=2), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 48)
    assert bytes(got.data[:32]) == bytes(32) and all(got.known[:32])
    assert set(got.taint[:32]) == {tier} and bytes(got.data[32:]) == b"\xa5" * 16


def test_an_unknown_lane_leaves_the_bytes_it_reaches_unknown():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec((None, *range(1, 32)), I)
    drive(m, e, [*lanes(0, 1, r5=8, r6=1), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 4)
    assert list(got.known) == [0, 0, 1, 1] and got.taint[2] == I


def test_a_wide_element_under_0x44_is_not_predicted():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec((0x12, 0x1234, *range(30)))
    drive(m, e, [*lanes(0, 1, r5=0x44, r6=1), *store(0, 0, DST), WAIT])
    got = m.boot.read(DST, 8)
    assert list(got.known) == [1, 1, 1, 1, 0, 0, 0, 0] and got.data[0] == 0x12


def w(**fields: int) -> Word:
    return Word.from_fields(fields, base=IDLE)


def test_transfers_through_the_machine_carry_their_tier():
    """The same copy handed from B registers, one of them inferred: the store is inferred."""
    values = [*lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT, *store(0, 0, DST), WAIT]
    regs = {v: k for k, v in enumerate(sorted({v for v in values if v is not WAIT}))}
    m = Machine(Boot(), stream=stream.Engine())
    m.boot.put(SRC, bytes(range(64)), "src")
    for v, k in regs.items():
        m.b[k] = Sv(v, I if v == 0x20 else V)
    for t, v in enumerate(values):
        head = 0x1D if v is WAIT else 0x1C
        b = 0 if v is WAIT else regs[v]
        m.execute(t, w(f308_312=head, f110_114=0, f115_119=b), t)
    got = m.boot.read(DST, 64)
    assert all(got.known) and set(got.taint) == {I}


def test_kind_4_stores_three_vectors_as_kind_6_at_inferred():
    m, e = machine(bytes(k & 0xFF for k in range(96))), stream.Engine()
    m.boot.put(DST, bytes(128), "prefill")
    drive(m, e, [*lanes(0, 3, r6=6), *load(0, SRC, 0), WAIT, 0x04, 0, DST, WAIT])
    got = m.boot.read(DST, 128)
    assert bytes(got.data) == bytes(range(96)) + bytes(32) and set(got.taint[:96]) == {
        I
    }


def test_kind_2_loads_bytes_into_local_memory_verified():
    m, e = machine(bytes(range(32))), stream.Engine()
    drive(m, e, [*block(0, 2, 0, 4, 8, 0, 0x10, 2, 1), 0x02, SRC, 0x400, WAIT])
    got = m.boot.local.read(LOCAL_BASE + 0x400, 32)
    assert bytes(got.data) == bytes(range(32)) and set(got.taint) == {V}


@pytest.mark.parametrize(
    ("load_regs", "store_regs", "n", "tier"),
    [
        ((1, 0, 0x20, 1, 0, 0x08, 4, 1), (1, 0, 0x20, 1, 0, 0x08, 4, 1), 64, V),
        ((2, 0, 4, 8, 0, 0x10, 2, 1), (2, 0, 4, 8, 0, 0x10, 2, 1), 32, V),
        ((1, 0, 0x20, 2, 0, 0x10, 4, 1), (1, 0, 0x20, 2, 0, 0x10, 4, 1), 64, I),
    ],
)
def test_kind_4_stores_its_direct_shapes_as_kind_6(load_regs, store_regs, n, tier):
    """16-bit elements, local memory and two vectors, each from a direct offset 0x400."""
    m, e = machine(bytes(k & 0xFF for k in range(128))), stream.Engine()
    m.boot.put(DST, bytes(128), "prefill")
    drive(
        m,
        e,
        [
            *block(0, *load_regs),
            *load(0, SRC, 0x400),
            WAIT,
            *block(1, *store_regs),
            1 << 6 | 0x04,
            0x400,
            DST,
            WAIT,
        ],
    )
    got = m.boot.read(DST, 128)
    assert bytes(got.data) == bytes(k & 0xFF for k in range(n)) + bytes(128 - n)
    assert set(got.taint[:n]) == {tier}


@pytest.mark.parametrize(
    ("kind", "regs", "frm", "to"),
    [
        (2, (1, 0, 0x20, 1, 0, 0x10, 2, 1), SRC, 0x40),  # base: ran wrapped
        (2, (1, 0, 0x20, 1, 0, 0x08, 4, 1), SRC, 0x40),
        (2, (1, 0x80, 0x20, 1, 0x100, 0x10, 2, 3), SRC, 0x40),
        (2, (2, 0, 4, 8, 0, 0x10, 2, 1), SRC, DST),  # local: ran direct
        (4, (1, 0, 0x20, 1, 0, 0x10, 2, 1), 0x40, DST),
        (4, (1, 0, 0x20, 1, 0, 0x08, 4, 1), SRC, DST),
        (4, (2, 0, 4, 8, 0, 0x10, 2, 1), SRC, DST),
        (4, (1, 0, 0x20, 2, 0, 0x10, 4, 1), SRC, DST),
        (4, (1, 0, 0x20, 3, 0, 0x10, 6, 1), SRC, DST),
        (8, (1, 0, 0x20, 1, 0, 0x10, 2, 1), 0x1234, 0x40),
        (8, (1, 0, 0x20, 1, 0, 0x08, 4, 1), 0x1234, 0x40),
        (8, (1, 0, 0x20, 4, 0, 0x10, 8, 1), 0x1234, 0x40),
    ],
)
def test_kinds_with_the_offset_form_they_did_not_run_in_raise(kind, regs, frm, to):
    m = machine(bytes(256))
    with pytest.raises(UnknownSemantics, match="memory offset not"):
        drive(m, stream.Engine(), [*block(0, *regs), kind, frm, to, WAIT])


@pytest.mark.parametrize("kind", [3, 5, 7])
@pytest.mark.parametrize(("frm", "to"), [(SRC, 0x40), (0x40, DST)])
def test_kinds_3_5_and_7_with_a_direct_offset_raise(kind, frm, to):
    m = machine(bytes(64))
    with pytest.raises(UnknownSemantics, match="memory offset not"):
        drive(m, stream.Engine(), [*lanes(0, 1, r6=2), kind, frm, to, WAIT])


def test_a_fill_not_vector_aligned_raises():
    m = machine()
    with pytest.raises(UnknownSemantics, match="not vector-aligned"):
        drive(m, stream.Engine(), [*lanes(0, 1, r6=2), 0x08, 0x1234, DST + 0x20, WAIT])


def test_lane_vectors_outside_every_outstanding_command_keep_their_tier():
    """A load of vectors 0..1 outstanding: vectors 0 and 1 are a hypothesis, vector 2 is not;
    a command on local memory leaves every vector a hypothesis."""
    m, e = machine(bytes(range(64))), stream.Engine()
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0)])
    assert e.lane_tier(0) == H and e.lane_tier(1) == H and e.lane_tier(2) == V
    e.finish(m)
    assert e.lane_tier(0) == V


def test_a_half_unit_load_holds_r3_vectors_per_row():
    """r2 0x10 (half units), r3 2: the load holds and zeroes two vectors, so both are a
    hypothesis beside it; outside them words keep at most inferred (not a byte row)."""
    m, e = machine(bytes(range(256))), stream.Engine()
    drive(m, e, [*block(0, 1, 0, 0x10, 2, 0, 4, 8, 1), *load(0, SRC, 0x10 * 64)])
    assert e.lane_tier(0x10) == H and e.lane_tier(0x11) == H
    assert e.lane_tier(0x12) == I


def test_words_beside_other_shapes_keep_at_most_inferred():
    """Two rows of a load and a 16-bit load: outside their vectors inferred, not verified."""
    for values in (
        [*lanes(0, 1, r6=2, r7=2, r1=64, r4=64), *load(0, SRC, 0)],
        [*lanes(0, 1, r5=8, r6=4), *load(0, SRC, 0)],
    ):
        m, e = machine(bytes(range(256))), stream.Engine()
        drive(m, e, values)
        assert e.lane_tier(0x40) == I


def test_kind_4_rows_on_channel_1_store_as_run():
    """Kind 4 stores two rows at DRAM pitch 0x100 and three at 0x200 as kind 6, each shape run in
    one job: inferred."""
    for r7, r4 in ((2, 0x100), (3, 0x200)):
        m, e = machine(), stream.Engine()
        for v in range(r7):
            m.memory[v] = Vec(tuple(range(32 * v, 32 * v + 32)))
        drive(
            m, e, [*lanes(1, 1, r6=2, r7=r7, r1=0x40, r4=r4), 1 << 6 | 4, 0, DST, WAIT]
        )
        for v in range(r7):
            got = m.boot.read(DST + r4 * v, 32)
            assert bytes(got.data) == bytes(range(32 * v, 32 * v + 32))
            assert got.weakest() == I


def test_kind_4_rows_in_another_pitch_raise():
    m, e = machine(), stream.Engine()
    m.memory[0] = m.memory[1] = Vec(tuple(range(32)))
    with pytest.raises(UnknownSemantics):
        drive(m, e, [*lanes(1, 1, r6=2, r7=2, r1=0x40, r4=0x200), 1 << 6 | 4, 0, DST])


def test_five_store_rows_are_inferred_whatever_their_pitch():
    """Kind 6 rows past four, also at the pitches kind 4 ran (r1 0x40, r4 0x200) and with
    overlapping 64-byte rows (r3 2, r6 4): inferred."""
    for r3, r6 in ((1, 2), (2, 4)):
        m, e = machine(), stream.Engine()
        for v in range(6):
            m.memory[v] = Vec(tuple(range(32)))
        drive(
            m,
            e,
            [*lanes(1, r3, r6=r6, r7=5, r1=0x40, r4=0x200), *store(1, 0, DST), WAIT],
        )
        assert m.boot.read(DST, 32).weakest() == I


def test_a_load_that_wraps_onto_its_own_vectors_leaves_them_unknown():
    """LANE_VECTORS + 2 vectors from vector 0: the second writes of 0 and 1 race the first."""
    data = bytes(range(256)) * (LANE_VECTORS // 8 + 1)
    m, e = machine(data), stream.Engine()
    r3 = LANE_VECTORS + 2
    drive(m, e, [*lanes(0, r3, r6=2 * r3), *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == m.memory[1].lanes == (None,) * 32
    assert m.memory[2].lanes == tuple(data[64:96])
    assert m.memory[LANE_VECTORS - 1].lanes == tuple(
        data[32 * LANE_VECTORS - 32 : 32 * LANE_VECTORS]
    )


def test_a_load_of_2_to_the_27_vectors_goes_off_script():
    """As the stock copy's word 68 hands with the loop 50..57 run (its count gone negative): 4 GiB
    cannot move before the firmware aborts the job."""
    m, e = machine(), stream.Engine()
    drive(m, e, [*lanes(0, 1 << 27, r6=1 << 28), *load(0, SRC, 0), WAIT])
    assert "more than a job's time moves" in e.off
    assert m.lanes_lost


def test_store_rows_past_a_jobs_time_go_off_script():
    """2^31 rows of one line at DRAM pitch 0 write one address, but 32 GiB take longer than the
    firmware lets a job run: whether the job ends is not known."""
    m, e = machine(bytes(32)), stream.Engine()
    regs = lanes(0, 1, r6=1, r7=1 << 31)
    drive(m, e, [*lanes(0, 1, r6=2), *load(0, SRC, 0), WAIT, *regs, *store(0, 0, DST)])
    assert "more than a job's time moves" in e.off
    rows = stream.MOST_BYTES // 16
    m, e = machine(bytes(32)), stream.Engine()
    drive(m, e, [*lanes(0, 1, r6=2), *load(0, SRC, 0), WAIT, *lanes(0, 1, r6=1, r7=rows)])
    drive(m, e, [*store(0, 0, DST), WAIT])
    assert not e.off


def test_a_plain_load_that_wraps_keeps_at_most_inferred_beside_it():
    """A byte load of two vectors from vector 0x3ff wraps to vector 0: outside it inferred."""
    m, e = machine(bytes(range(64))), stream.Engine()
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, (LANE_VECTORS - 1) * 64)])
    assert e.lane_tier(0) == H and e.lane_tier(0x10) == I


def lenient_machine() -> tuple[Machine, stream.Engine]:
    """A lenient machine (`Machine.lapse`) driving its engine, with known src, dst and local
    bytes."""
    e = stream.Engine()
    m = Machine(Boot(), stream=e)
    m.strict = False
    m.boot.put(SRC, bytes(64), "src")
    m.boot.put(DST, bytes(32), "prefill")
    m.boot.local.write(LOCAL_BASE, Bytes.of(bytes(4), 0))
    return m, e


def forgotten(m: Machine) -> bool:
    """Every memory forgotten: the lane memory, local memory and DRAM."""
    local, dram = m.boot.local.read(LOCAL_BASE, 4), m.boot.read(DST, 32)
    return m.lanes_lost and not any(local.known) and not any(dram.known)


@pytest.mark.parametrize(
    "values, why",
    [
        ([0x234, *block(0, 2, 0, 4, 0x10, 0, 0x10, 4, 1)], "grammar was lost"),
        ([*lanes(0, 1, r6=2), 0x09, SRC, DST, WAIT], "kind 0x9"),
        ([*lanes(0, 5, r6=10), 4, SRC, DST, WAIT], "outside the shapes it ran in"),
        ([*lanes(0, 1, r6=2), *store(0, 0, UNKNOWN), WAIT], "DRAM address unknown"),
        (
            [*lanes(0, 0x7FFFFE20, r6=0xFFFFFC40), *store(0, 0, DST), WAIT],
            "more than all of DRAM",
        ),
    ],
)
def test_a_lenient_engine_forgets_every_memory_where_a_reach_is_not_known(values, why):
    m, e = lenient_machine()
    drive(m, e, values)
    assert why in m.lapses[0] and m.cap is H and e.off
    assert forgotten(m)


def test_an_off_script_local_load_past_the_end_forgets_every_memory():
    """Its third row at 0x2000 leaves local memory: a reach not measured, though a pitch of half
    of it would bring a wrapped row back."""
    m, e = lenient_machine()
    loads = block(1, 2, 0x1000, 4, 0x40, 0, 0x10, 0x10, 3)
    drive(
        m, e, [*lanes(0, 1, r6=2), 0x09, SRC, SRC, WAIT, *loads, *load(1, SRC, 0), WAIT]
    )
    assert any("0x2000..0x2100: not measured" in x for x in m.lapses)
    assert forgotten(m)


def test_a_lenient_command_that_never_ends_goes_off_script():
    m, e = lenient_machine()
    drive(m, e, [*lanes(0, 1, r6=4), *store(0, 0, DST), WAIT])
    assert m.lapses == [
        "stream: transfer 11 (word 11): a store of 64 elements from 32 never ends"
    ]
    assert "never ends" in e.off and m.lanes_lost
    assert not any(m.boot.read(DST, 32).known)  # its destination
    assert all(m.boot.local.read(LOCAL_BASE, 4).known)  # not reached
    m, e = lenient_machine()
    drive(m, e, [*lanes(0, 3, r6=4), *load(0, SRC, 0), WAIT])
    assert "never ends" in m.lapses[0] and m.lanes_lost


def test_a_lenient_wait_that_never_returns_goes_on_at_hypothesis():
    m, e = lenient_machine()
    data = [*lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT]
    drive(m, e, [*data, WAIT, *load(0, SRC, 0), WAIT])
    assert m.lapses == ["x1 0x1d: a 0x1d with no command outstanding never ends"]
    assert m.memory[0] == Vec((0,) * 32, H)  # the load after it lands, a hypothesis


def test_a_lenient_wait_in_a_job_that_handed_no_command_goes_on_at_hypothesis():
    m, e = lenient_machine()
    drive(m, e, [WAIT, *lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT])
    assert m.lapses == ["x1 0x1d: a 0x1d in a job that handed no command: not measured"]
    assert m.memory[0] == Vec((0,) * 32, H) and not e.off


def test_a_lenient_queue_past_the_depth_goes_on_at_hypothesis():
    m, e = lenient_machine()
    loads = [v for k in range(stream.DEPTH + 1) for v in load(0, SRC, 64 * k)]
    drive(m, e, [*lanes(0, 1, r6=2), *loads, *[WAIT] * (stream.DEPTH + 1)])
    assert len(m.lapses) == 1 and "more than 44 commands" in m.lapses[0]
    assert not e.outstanding and m.memory[stream.DEPTH] == Vec((0,) * 32, H)
