"""The timing estimate on own programs run through the datapath executor: transfers hidden by the
words before their 0x1d, one engine for all channels, the tiers of the parts and the cases
without a measured time."""

from fenykl.dsp import stream
from fenykl.dsp.emu import Boot, Executor, Job
from fenykl.dsp.fields import Confidence, Operands
from fenykl.dsp.machine import Machine, Sv
from fenykl.dsp.program import Program
from fenykl.dsp.timing import Estimate, estimate
from fenykl.dsp.word import IDLE, Word

V, I = Confidence.VERIFIED, Confidence.INFERRED
SRC, DST = 0xA0000000, 0xA1000000
WAIT = None
HALT = Word.from_fields({"op": 7}, base=IDLE)


def lanes(channel: int, r6: int, r5: int = 0x10, r7: int = 1) -> list[int]:
    """A block for a channel on the lane memory moving r6 16-byte lines per row."""
    r3 = -(-r5 * r6 // 0x20)
    return [0x20 | channel << 6, 1, 0x400, 0x20, r3, 0x400, r5, r6, r7]


def local(channel: int, r6: int) -> list[int]:
    """A block for a channel on local memory, bytes at 4 per word."""
    return [0x20 | channel << 6, 2, 0, 4, 4 * r6, 0, 0x10, r6, 1]


def load(channel: int, to: int = 0) -> list[int]:
    return [channel << 6 | stream.LOAD, SRC, to]


def store(channel: int) -> list[int]:
    return [channel << 6 | stream.STORE, 0, DST]


def run(values: list, between: dict[int, int] | None = None, body=()) -> Estimate:
    """Hands `values` (None a 0x1d) from B registers, with `between[k]` idle words before the
    k-th value, then `body` and a halt."""
    regs = sorted({v for v in values if v is not WAIT})
    m = Machine(Boot(), stream=stream.Engine())
    for k, v in enumerate(regs):
        m.b[k] = Sv(v)
    words = []
    for k, v in enumerate(values):
        words += [IDLE] * (between or {}).get(k, 0)
        head = 0x1D if v is WAIT else 0x1C
        f115 = 0 if v is WAIT else regs.index(v)
        words.append(Word.from_fields({"f308_312": head, "f115_119": f115}, base=IDLE))
    job = Job(Program((*words, *body, HALT)), ())
    flow = Executor(m.boot, datapath=m).step(job)
    m.between_jobs()
    return estimate(job, flow, m.stream)


def part(e: Estimate, name: str):
    return next(p for p in e.parts if p.name == name)


def test_words_before_the_wait_hide_the_transfer():
    """A 16 KiB load: words between the command and its 0x1d cost nothing while the transfer
    runs, and once they outlast it the wait is gone but stays a fitted part."""
    values = [*lanes(0, 0x400), *load(0), WAIT]
    alone = run(values)
    short = run(values, {len(values) - 1: 20})
    long = run(values, {len(values) - 1: 300})
    assert part(alone, "stream waits").us > 0
    assert abs(short.us - alone.us) < 1e-9
    assert long.us > alone.us
    assert part(long, "stream waits").us == 0 and part(long, "stream waits").tier == I


def test_commands_on_two_channels_run_one_after_the_other():
    """Two 8 KiB loads on channels 0 and 2 and words enough to hide one: the second 0x1d waits."""
    one = run([*lanes(0, 0x200), *load(0), WAIT], {12: 100})
    two = [*lanes(0, 0x200), *lanes(2, 0x200), *load(0), *load(2, 0x8000), WAIT, WAIT]
    both = run(two, {24: 100})
    assert part(one, "stream waits").us == 0
    assert part(both, "stream waits").us > 0


def test_the_parts_carry_their_tiers():
    """A loop's repeated words are verified, a repeated S head's inferred, first runs inferred;
    the total is the weakest."""

    def data(code: int, ra: int, imm: int, **ports: int) -> Word:
        return Word.from_fields(
            ports, base=Word.data(code, Operands(ra, None, imm), base=IDLE)
        )

    li = data(0x0B, 31, imm=3, f272_273=1, f224_228=2)
    head = Word.from_fields({"f296_301": 0, "f61_64": 1, "f65_68": 2}, base=IDLE)
    end = Word.from_fields(
        {"ra": 2, "f120_122": 0, "f272_273": 1, "f224_228": 2},
        base=Word.ctl(0, 1, base=IDLE),
    )
    e = run([], body=[li, head, end, IDLE, IDLE, IDLE, IDLE])
    tiers = {p.name: p.tier for p in e.parts}
    assert tiers == {
        "job": V,
        "first runs": I,
        "repeats": V,
        "S repeats": I,
    }
    assert e.tier == I


def test_a_lone_wait_is_an_inferred_part():
    e = run([WAIT])
    assert part(e, "stream waits").us == 0 and e.tier == I


def test_a_command_outstanding_at_the_halt_has_no_time():
    e = run([*lanes(0, 0x40), *load(0)])
    assert e.us is None and "outstanding" in e.unknown


def test_shapes_not_timed_have_no_time():
    cases = {
        "two rows": [*lanes(0, 0x40, r7=2), *load(0), WAIT],
        "16-bit load": [*lanes(0, 0x40, r5=0x08), *load(0), WAIT],
        "32-bit store": [*lanes(0, 0x40, r5=0x04), *store(0), WAIT],
        "under 1 KiB": [*lanes(0, 0x20), *load(0), WAIT],
        "2 KiB into local memory": [*local(0, 0x80), *load(0, 0x800), WAIT],
        "nothing moves": [*lanes(0, 0x40), 1, SRC, 0, WAIT],
    }
    for name, values in cases.items():
        assert run(values).us is None, name


def test_4_kib_into_local_memory_is_timed():
    e = run([*local(0, 0x100), *load(0, 0x800), WAIT])
    assert part(e, "stream waits").us > 0


def test_an_engine_off_script_has_no_time():
    e = run([0x7FFF, WAIT])
    assert e.us is None and "off script" in e.unknown
