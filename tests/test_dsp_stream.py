"""The stream engine on synthetic values: grammar, loads, stores, timing, going off script."""

import pytest

from fenykl.dsp import stream
from fenykl.dsp.emu import LOCAL_BASE, Boot
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Machine, Sv, Vec
from fenykl.dsp.semantics import UnknownSemantics
from fenykl.dsp.state import Bytes
from fenykl.dsp.word import IDLE, Word

V, I, H = Confidence.VERIFIED, Confidence.INFERRED, Confidence.HYPOTHESIS
SRC, DST = 0xA0000000, 0xA1000000
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


def drive(m: Machine, e: stream.Engine, values: list[int | None]) -> None:
    for k, v in enumerate(values):
        e.transfer(m, k, 0x1D if v is WAIT else 0x1C, Sv(0 if v is WAIT else v))


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
    """After a command it never ends; in a job that handed none it returns."""
    m, e = machine(bytes(64)), stream.Engine()
    drive(m, e, [WAIT])
    drive(m, e, [*lanes(0, 2, r6=4), *load(0, SRC, 0), WAIT])
    with pytest.raises(UnknownSemantics, match="never ends"):
        drive(m, e, [WAIT])


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


def test_a_store_takes_its_source_when_handed():
    m, e = machine(), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    drive(m, e, [*lanes(0, 1, r6=2), *store(0, 0, DST)])
    m.memory[0] = Vec((0,) * 32)
    drive(m, e, [WAIT])
    assert bytes(m.boot.read(DST, 32).data) == bytes(range(32))


def test_a_command_with_a_register_not_handed_moves_nothing():
    m, e = machine(bytes(64)), stream.Engine()
    m.memory[0] = Vec(tuple(range(32)))
    drive(m, e, [key(0, 3), 2, key(0, 6), 4, *load(0, SRC, 0), WAIT])
    assert m.memory[0].lanes == tuple(range(32))
    assert not e.off and e.lane_tier() == I


def test_an_unmeasured_kind_goes_off_script():
    m, e = machine(bytes(64)), stream.Engine()
    m.memory[5] = Vec(tuple(range(32)))
    drive(m, e, [*lanes(0, 1, r6=2), 0x03, SRC, 0, *store(0, 0, DST), WAIT, WAIT])
    assert "kind 0x3" in e.off and not m.memory
    assert not any(m.boot.read(DST, 32).known)


def test_a_key_with_bits_outside_its_fields_loses_the_grammar():
    m, e = machine(bytes(64)), stream.Engine()
    drive(m, e, [0x234, *lanes(0, 1, r6=2), *load(0, SRC, 0), WAIT])
    assert "bits outside" in e.off and e.lost and not e.outstanding


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
        m.execute(t, w(f308_312=head, f115_119=0 if v is WAIT else regs[v]), t)
    got = m.boot.read(DST, 64)
    assert all(got.known) and set(got.taint) == {I}
