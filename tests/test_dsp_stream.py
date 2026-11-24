"""The copy engine's stream on a synthetic transcript: load, store formats, going off script."""

from fenykl.dsp import stream
from fenykl.dsp.emu import Boot
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Machine, Sv, Vec

SRC, DST, LINES = 0xA0000000, 0xA1000000, 4


def reference(fmt: int = 0x10) -> list[tuple[int, int]]:
    ref = [(0x1C, 0x99)] * 34
    ref[stream.LOADED] = ref[stream.STORED] = (0x1D, 0x99)
    for k, v in (
        (stream.LOAD_FORMAT, 0x10),
        (stream.STORE_FORMAT, fmt),
        (stream.LOAD_LINES, LINES),
        (stream.STORE_LINES, LINES),
        (stream.LOAD_COMMAND, 0),
        (stream.SOURCE, SRC),
        (stream.PLACEMENT, 0),
        (stream.STORE_SOURCE, 0),
        (stream.DESTINATION, DST),
    ):
        ref[k] = (ref[k][0], v)
    return ref


def drive(m: Machine, s: stream.CopyStream, values: list[tuple[int, int]]) -> None:
    for k, (head, v) in enumerate(values):
        s.transfer(m, k, head, Sv(v))


def test_load_then_store_as_bytes_copies_src():
    m = Machine(Boot())
    data = bytes(range(16 * LINES))
    m.boot.put(SRC, data, "src")
    s = stream.CopyStream(reference())
    drive(m, s, reference())
    assert m.memory[1].lanes == tuple(range(32, 64))
    got = m.boot.read(DST, 16 * LINES)
    assert all(got.known) and bytes(got.data) == data


def test_a_ten_bit_pack_from_the_lane_memory():
    m = Machine(Boot())
    m.memory[0] = Vec(tuple(0x300 + k for k in range(32)))
    m.memory[1] = Vec(tuple(0x300 + k for k in range(32, 64)))
    values = reference(0x0C)
    values[stream.LOAD_COMMAND] = (0x1C, 1)  # no load: the store reads what is there
    drive(m, stream.CopyStream(reference(0x0C)), values)
    got = m.boot.read(DST, 16 * LINES)
    for line in range(LINES):
        acc = int.from_bytes(got.data[16 * line : 16 * line + 16], "little")
        assert [acc >> (10 * k) & 0x3FF for k in range(12)] == [
            0x300 + 12 * line + k for k in range(12)
        ]
        assert acc >> 120 == 0


def test_an_unknown_lane_leaves_the_bytes_it_reaches_unknown():
    m = Machine(Boot())
    m.memory[0] = Vec((None, *range(1, 32)), Confidence.INFERRED)
    values = reference(0x08)
    values[stream.LOAD_COMMAND] = (0x1C, 1)
    drive(m, stream.CopyStream(reference(0x08)), values)
    got = m.boot.read(DST, 4)
    assert list(got.known) == [0, 0, 1, 1]
    assert got.taint[2] == Confidence.INFERRED


def test_another_value_goes_off_script():
    m = Machine(Boot())
    m.memory[5] = Vec(tuple(range(32)))
    values = reference()
    values[3] = (0x1C, 0x98)
    s = stream.CopyStream(reference())
    drive(m, s, values)
    assert s.off and s.stored is None and not m.memory
