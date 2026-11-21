"""The stepping core on the stock copy (loaded from the firmware at test time) and on small
assembled programs."""

from functools import cache

import pytest
from conftest import stock_image

from fenykl.dsp import commands
from fenykl.dsp.emu import Boot, Core, Flow, Stop, start_copy
from fenykl.dsp.fields import Confidence, Operands
from fenykl.dsp.memcpy import ITEM, copy_branches
from fenykl.dsp.program import Program, stock_programs
from fenykl.dsp.semantics import Branching, Count, Given, RaTest, Regs, UnknownSemantics
from fenykl.dsp.state import Reg
from fenykl.dsp.word import IDLE, Word

SRC, DST = 0xA0FCA800, 0xA1113600


@cache
def copy_program() -> Program:
    return stock_programs(stock_image("em10ii"))[ITEM]


def run_copy(length: int, branching: Branching, strict: bool = False) -> Flow:
    job = commands.copy(copy_program(), SRC, DST, length)
    return Core(Boot(), branching, strict=strict).run(job, start_copy)


def span(*runs: tuple[int, int]) -> list[int]:
    return [i for a, b in runs for i in range(a, b + 1)]


# Call 0 -> 10 with slots 1..4; 27 falls through (slots 28..31); 45 jumps over 50..57 to 58
# after its slots 46..49; the return 79 goes back to 5 after its slots 80..83; 5 halts.
COPY_ORDER = span((0, 4), (10, 49), (58, 83), (5, 5))


def test_copy_flow_through_call_slots_and_halt():
    flow = run_copy(0x400, copy_branches(0x400))
    assert flow.stop is Stop.HALT
    assert flow.order() == COPY_ORDER
    by_index = {s.index: s for s in flow.steps}
    assert [by_index[i].taken for i in (0, 27, 45, 79)] == [True, False, True, True]
    slots = [s.index for s in flow.steps if s.slot]
    assert slots == span((1, 4), (28, 31), (46, 49), (80, 83))
    assert by_index[45].confidence is Confidence.INFERRED
    assert (
        copy_branches(0x408).confidence is Confidence.HYPOTHESIS
    )  # no run had this length


def test_guard_45_switches_the_path_into_the_loop():
    once = Given({27: False, 45: False, 53: [True, False]})
    flow = run_copy(0x400, once)
    assert flow.stop is Stop.HALT
    loop = span((50, 57))
    assert flow.order() == span((0, 4), (10, 49)) + loop + loop + COPY_ORDER[-27:]
    unguarded = run_copy(0x400, Given({27: False, 45: False}))
    assert unguarded.stop is Stop.UNDECIDED
    assert unguarded.order()[-1] == 53


def test_undecided_branch_stops_or_raises():
    flow = run_copy(0x400, Given())
    assert flow.stop is Stop.UNDECIDED and flow.order()[-1] == 27
    flow = run_copy(0x400, RaTest())  # r7 of the op slot: unknown
    assert flow.stop is Stop.UNDECIDED and flow.order()[-1] == 27
    with pytest.raises(UnknownSemantics, match="word 1: no semantics"):
        run_copy(0x400, copy_branches(0x400), strict=True)


def test_ra_test_on_the_transfer_registers():
    """The job-start transfer registers decide both branches: 27 names r7 = len / 2."""
    hypothesis = RaTest(reads=Regs.TRANSFER)
    flow = run_copy(0x400, hypothesis)
    assert flow.order() == COPY_ORDER
    assert {s.confidence for s in flow.steps if s.index in (27, 45)} == {
        Confidence.HYPOTHESIS
    }
    empty = run_copy(0, hypothesis)
    assert empty.order() == span((0, 4), (10, 31), (79, 83), (5, 5))


def control(code: int, target: int, ra: int) -> Word:
    return Word.from_fields(
        {"op": code, "f229": 0, "imm": target & 0xFF, "f128_132": target >> 8, "ra": ra}
    )


HALT = Word.from_fields({"op": 7})


def test_local_read_uses_ra_as_a_base():
    read = Word.from_fields(
        {"f120_122": 7}, base=Word.data(0x1A, Operands(ra=4, imm=1))
    )
    job = commands.copy(Program((read, HALT)), SRC, DST, 0x400)
    boot = Boot()
    boot.slot[4] = Reg(1, 1, Confidence.VERIFIED, "set by the test")
    flow = Core(boot).run(job)
    assert flow.steps[0].note == "reads local word 0x2: 0x400"
    assert boot.slot[4] == Reg(1, 1, Confidence.VERIFIED, "set by the test")
    other = Word.from_fields({"f120_122": 3}, base=read)
    with pytest.raises(UnknownSemantics, match="outside what its semantics covers"):
        Core(Boot()).run(commands.copy(Program((other, HALT)), SRC, DST, 0x400))


def test_loop_end_count_hypotheses():
    # 0: loop body 1..7 (back-edge at 3, slots 4..7), halt at 8.
    program = Program(
        (IDLE, IDLE, IDLE, control(0, 1, 1), IDLE, IDLE, IDLE, IDLE, HALT)
    )
    job = commands.copy(program, SRC, DST, 0x400)

    def run(count: Count, limit: int = 1000) -> Flow:
        boot = Boot()
        boot.slot[1] = Reg(3, 3, Confidence.VERIFIED, "set by the test")
        return Core(boot, RaTest(count=count), limit=limit).run(job)

    flow = run(Count.DECREMENT)
    assert flow.stop is Stop.HALT
    assert flow.order() == [0] + span((1, 7)) * 3 + [8]
    assert run(Count.TEST, limit=100).stop is Stop.STEP_LIMIT
