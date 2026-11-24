"""The copy engine's stream on a synthetic transcript: load, store formats, going off script."""

from fenykl.dsp import stream
from fenykl.dsp.emu import Boot
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Machine, Sv, Vec
from fenykl.dsp.word import IDLE, Word

SRC, DST, LINES = 0xA0000000, 0xA1000000, 4


def w(**fields: int) -> Word:
    return Word.from_fields(fields, base=IDLE)


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


def test_a_wide_element_under_0x44_is_not_predicted():
    m = Machine(Boot())
    m.memory[0] = Vec((0x12, 0x1234, *range(30)))
    values = reference(0x44)
    values[stream.LOAD_COMMAND] = (0x1C, 1)
    drive(m, stream.CopyStream(reference(0x44)), values)
    got = m.boot.read(DST, 8)
    assert list(got.known) == [1, 1, 1, 1, 0, 0, 0, 0]
    assert got.data[0] == 0x12


def test_off_script_loses_the_lane_memory_for_the_rest_of_the_job():
    m = Machine(Boot())
    values = reference()
    values[3] = (0x1C, 0x98)
    drive(m, stream.CopyStream(reference()), values)
    m.a[1] = Sv(0)
    m.o[0] = Vec(tuple(range(32)))
    store = w(f302_307=0x1B, f100_104=1, f105_109=0, f69_71=0)
    load = w(f302_307=0x1D, f100_104=1, f105_109=0)
    for t, word in enumerate([store, IDLE, IDLE, load]):
        m.execute(t, word, t)
    assert m.load_due[7][0].lanes == (None,) * 32
    assert m.between_jobs().lane_memory == {}


def test_transfers_through_the_machine_carry_their_tier():
    """The same values from a register at inferred: the store is at most inferred."""
    ref = reference()
    regs = {v: k for k, v in enumerate(sorted({v for _, v in ref}))}
    m = Machine(Boot(), stream=stream.CopyStream(ref))
    m.boot.put(SRC, bytes(range(16 * LINES)), "src")
    for v, k in regs.items():
        m.b[k] = Sv(v, Confidence.INFERRED if v == 0x99 else Confidence.VERIFIED)
    for t, (head, v) in enumerate(ref):
        m.execute(t, w(f308_312=head, f115_119=regs[v]), t)
    got = m.boot.read(DST, 16 * LINES)
    assert all(got.known) and set(got.taint) == {Confidence.INFERRED}
