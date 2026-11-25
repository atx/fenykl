"""The timing estimate on own programs run through the datapath executor: transfers hidden by the
words before their 0x1d, one engine for all channels, the tiers of the parts and the cases
without a measured time."""

import pytest

from fenykl.dsp import stream
from fenykl.dsp.emu import Boot, Executor, Job
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Machine, Sv
from fenykl.dsp.program import Program
from fenykl.dsp.timing import (
    FIRST,
    LANE_LOAD,
    LANE_LOAD16,
    LANE_STORE16,
    LOCAL_LOAD32,
    REPEAT,
    REPEAT_I,
    Estimate,
    estimate,
)
from fenykl.dsp.word import IDLE, Word

V, I = Confidence.VERIFIED, Confidence.INFERRED
SRC, DST = 0xA0000000, 0xA1000000
WAIT = None
HALT = Word.from_fields({"op": 7}, base=IDLE)
THREE = Sv(3)


def lanes(channel: int, r6: int, r5: int = 0x10, r7: int = 1) -> list[int]:
    """A block for a channel on the lane memory moving r6 16-byte lines per row."""
    r3 = -(-r5 * r6 // 0x20)
    return [0x20 | channel << 6, 1, 0x400, 0x20, r3, 0x400, r5, r6, r7]


def local(channel: int, r6: int) -> list[int]:
    """A block for a channel on local memory, bytes at 4 per word."""
    return [0x20 | channel << 6, 2, 0, 4, 4 * r6, 0, 0x10, r6, 1]


def local32(channel: int, r6: int) -> list[int]:
    """A block for a channel on local memory, 32-bit elements at one per word."""
    return [0x20 | channel << 6, 2, 0, 1, 4 * r6, 0, 0x04, r6, 1]


def load(channel: int, to: int = 0) -> list[int]:
    return [channel << 6 | stream.LOAD, SRC, to]


def store(channel: int) -> list[int]:
    return [channel << 6 | stream.STORE, 0, DST]


def run(
    values: list, between: dict[int, int] | None = None, body=(), g=None
) -> Estimate:
    """Hands `values` (None a 0x1d) from B registers, with `between[k]` idle words before the
    k-th value, then `body` and a halt; `g` presets G registers."""
    regs = sorted({v for v in values if v is not WAIT})
    m = Machine(Boot(), stream=stream.Engine())
    for r, v in (g or {}).items():
        m.g[r] = v
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


def loop(slots: int, head: bool = False, counter: Sv = THREE) -> Estimate:
    """Passes of [an S3 head or an idle word, `slots` idles, a loop end on r2 and its four
    slots], r2 counting down from `counter`."""
    first = (
        Word.from_fields({"f296_301": 0, "f61_64": 1, "f65_68": 2}, base=IDLE)
        if head
        else IDLE
    )
    end = Word.from_fields(
        {"ra": 2, "f120_122": 0, "f272_273": 1, "f224_228": 2},
        base=Word.ctl(0, 0, base=IDLE),
    )
    body = [first, *[IDLE] * slots, end, IDLE, IDLE, IDLE, IDLE]
    return run([], body=body, g={2: counter})


def test_a_loops_words_are_counted_and_tiered():
    """Four passes of ten words: ten first runs and the halt, thirty repeats, of which the S
    head's three are inferred."""
    e = loop(4, head=True)
    parts = {p.name: (p.us, p.tier) for p in e.parts}
    assert parts["first runs"] == (11 * FIRST.us, I)
    assert parts["repeats"] == (27 * REPEAT.us, V)
    assert parts["inferred repeats"] == (3 * REPEAT_I.us, I)
    assert e.tier == I


@pytest.mark.parametrize("n", [900, 1994])
def test_idle_passes_of_up_to_1024_words_and_of_2000_repeat_verified(n):
    """A pass of n + 6 words: its idle words repeat verified, its loop word inferred."""
    parts = {p.name: p.us for p in loop(n).parts}
    idle, other = parts["repeats"] / REPEAT.us, parts["inferred repeats"] / REPEAT_I.us
    assert round(idle) == (n + 5) * round(other) and round(other) > 0


@pytest.mark.parametrize("n", [1100, 1530, 1799, 2010])
def test_idle_passes_between_1024_and_2000_words_and_beyond_repeat_inferred(n):
    parts = {p.name for p in loop(n).parts}
    assert "repeats" not in parts and "inferred repeats" in parts


def test_passes_shorter_than_the_measured_loops_are_inferred():
    e = loop(0)
    assert {p.name for p in e.parts} == {"job", "first runs", "inferred repeats"}


def test_a_branch_on_an_inferred_register_caps_the_words():
    e = loop(4, counter=Sv(3, I))
    assert part(e, "repeats").tier == I


def test_a_wait_joins_the_oldest_command():
    """A 16 KiB load, then a 1 KiB one on another channel; the first 0x1d joins the 16 KiB load
    and five idles later the second the 1 KiB one, which runs after it."""
    values = [*lanes(0, 0x400), *lanes(2, 0x40), *load(0), *load(2, 0x8000), WAIT, WAIT]
    e = run(values, {len(values) - 1: 5})
    big = LANE_LOAD.per_command + 16 * LANE_LOAD.per_kib
    small = LANE_LOAD.per_command + LANE_LOAD.per_kib
    # 21 words hand the 16 KiB load; the 1 KiB load ends after both; then the halt.
    assert (
        abs(e.us - (part(e, "job").us + 21 * FIRST.us + big + small + FIRST.us)) < 1e-9
    )
    assert part(e, "first runs").us == 32 * FIRST.us


def test_a_lone_wait_is_an_inferred_part():
    e = run([WAIT])
    assert part(e, "stream waits").us == 0 and e.tier == I


def test_a_command_outstanding_at_the_halt_has_no_time():
    e = run([*lanes(0, 0x40), *load(0)])
    assert e.us is None and "outstanding" in e.unknown


def test_shapes_not_timed_have_no_time():
    cases = {
        "two rows": [*lanes(0, 0x40, r7=2), *load(0), WAIT],
        "16-bit load of 32 KiB": [*lanes(0, 0x800, r5=0x08), *load(0), WAIT],
        "16-bit load under 1 KiB": [*lanes(0, 0x20, r5=0x08), *load(0), WAIT],
        "16-bit store of 32 B": [*lanes(0, 2, r5=0x08), *store(0), WAIT],
        "16-bit store of 32 KiB": [*lanes(0, 0x800, r5=0x08), *store(0), WAIT],
        "12-bit store of 1 KiB": [*lanes(0, 0x40, r5=0x0A), *store(0), WAIT],
        "32-bit local load of 128 B": [*local32(0, 8), *load(0, 0x800), WAIT],
        "32-bit local load of 4.25 KiB": [*local32(0, 0x110), *load(0, 0x400), WAIT],
        "32-bit local load, r3 short": [0x20, 2, 0, 1, 0x10, 0, 0x04, 0x10, 1]
        + [*load(0, 0x800), WAIT],
        "32-bit local store": [*local32(0, 0x40), *store(0), WAIT],
        "32-bit store": [*lanes(0, 0x40, r5=0x04), *store(0), WAIT],
        "r3 short of r6": [0x20, 1, 0x400, 0x20, 0x10, 0x400, 0x10, 0x40, 1]
        + [*load(0), WAIT],
        "under 1 KiB": [*lanes(0, 0x20), *load(0), WAIT],
        "2 KiB into local memory": [*local(0, 0x80), *load(0, 0x800), WAIT],
        "nothing moves": [*lanes(0, 0x40), 1, SRC, 0, WAIT],
        "r1 and r4 not handed": [
            *[
                x
                for r, v in enumerate(lanes(0, 0x40)[1:])
                if r not in (1, 4)
                for x in (r << 11 | 0x21, v)
            ],
            *load(0),
            WAIT,
        ],
    }
    for name, values in cases.items():
        e = run(values)
        assert e.us is None and e.tier is None, name


def test_more_commands_than_the_queue_held_have_no_time():
    held = run([*lanes(0, 0x40), *load(0) * 16, *[WAIT] * 16])
    over = run([*lanes(0, 0x40), *load(0) * 17, *[WAIT] * 17])
    assert held.us is not None
    assert over.us is None and "more than 16" in over.unknown


def test_4_kib_into_local_memory_is_timed():
    e = run([*local(0, 0x100), *load(0, 0x800), WAIT])
    assert part(e, "stream waits").us > 0


@pytest.mark.parametrize(
    ("block", "command", "rate", "kib"),
    [
        (lanes(0, 0x40, r5=0x08), load(0), LANE_LOAD16, 1),
        (lanes(0, 0x400, r5=0x08), load(0), LANE_LOAD16, 16),
        (lanes(0, 4, r5=0x08), store(0), LANE_STORE16, 0.0625),
        (lanes(0, 0x400, r5=0x08), store(0), LANE_STORE16, 16),
        (local32(0, 0x10), load(0, 0x800), LOCAL_LOAD32, 0.25),
        (local32(0, 0x100), load(0, 0x800), LOCAL_LOAD32, 4),
    ],
)
def test_16_bit_and_32_bit_local_transfers_are_timed_at_their_measured_sizes(
    block, command, rate, kib
):
    """A command joined at once waits for its whole transfer: the rate at the command's size."""
    e = run([*block, *command, WAIT])
    # The 0x1d word runs after the command's word: one first-run word of the transfer is hidden.
    want = rate.per_command + rate.per_kib * kib - FIRST.us
    assert part(e, "stream waits").us == pytest.approx(want)


def test_an_engine_off_script_has_no_time():
    e = run([0x7FFF, WAIT])
    assert e.us is None and "off script" in e.unknown


def test_words_branched_over_cost_only_inside_a_fetch_line_that_runs():
    """A taken op 2 at word 0 (four slots) to word 8 or to word 16: words 5..7 share line 0
    with words that run and cost as unrun; line 1 (8..15), skipped whole, costs nothing."""
    from fenykl.dsp.timing import LINE, UNRUN

    def over(target: int) -> Estimate:
        br = Word.from_fields(
            {"ra": 2, "f120_122": 0}, base=Word.ctl(2, target, base=IDLE)
        )
        body = [br, *[IDLE] * (target - 1), *[IDLE] * 8]
        return run([], body=body, g={2: Sv(1)})

    assert LINE == 8
    near, far = over(8), over(16)
    assert (
        part(near, "unrun line words").us
        == part(far, "unrun line words").us
        == 3 * UNRUN.us
    )
    assert part(near, "first runs").us == part(far, "first runs").us
