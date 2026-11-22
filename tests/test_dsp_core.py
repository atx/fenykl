"""The stepping core on the stock copy (loaded from the firmware at test time) and on small
assembled programs."""

from functools import cache

import pytest
from conftest import stock_image

from fenykl.dsp import commands
from fenykl.dsp.emu import Boot, Executor, Flow, Stop, start_copy
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
    return Executor(Boot(), branching=branching, strict=strict).step(job, start_copy)


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


def test_a_halt_inside_a_called_routine_ends_the_job():
    # Word 5 in place of 11: the routine called at 0 halts with its return pending.
    words = list(copy_program().words)
    words[11] = words[5]
    job = commands.copy(Program(tuple(words)), SRC, DST, 0x400)
    flow = Executor(Boot(), branching=copy_branches(0x400), strict=False).step(
        job, start_copy
    )
    assert flow.stop is Stop.HALT and flow.order() == span((0, 4), (10, 11))


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
    flow = Executor(boot).step(job)
    assert flow.steps[0].note == "reads local word 0x2: 0x400"
    assert boot.slot[4] == Reg(1, 1, Confidence.VERIFIED, "set by the test")
    other = Word.from_fields({"f120_122": 3}, base=read)
    with pytest.raises(UnknownSemantics, match="outside what its semantics covers"):
        Executor(Boot()).step(commands.copy(Program((other, HALT)), SRC, DST, 0x400))


def test_a_program_without_a_halt_runs_past_its_end():
    job = commands.copy(Program((IDLE, IDLE)), SRC, DST, 0x400)
    with pytest.raises(UnknownSemantics, match="word 2: .*past the program end"):
        Executor(Boot(), strict=False).step(job)


def load(op: int, imm: int, ra: int = 4) -> Word:
    """A family A word (a load or a store) at ra + imm, covered (f120_122 = 7)."""
    return Word.from_fields(
        {"f120_122": 7}, base=Word.data(op, Operands(ra=ra, imm=imm))
    )


def test_word_halfword_and_byte_loads_share_one_base():
    # Local word 0 holds src: 0x1a reads it whole, 0x16 its halfwords, 0x10 its bytes.
    words = (load(0x1A, 0), load(0x16, 1), load(0x10, 1), load(0x16, 2), load(0x10, -1))
    boot, flow = run_slot(words, r4=given(0))
    assert [s.note for s in flow.steps[:5]] == [
        f"reads local word 0x0: {SRC:#x}",
        f"reads local halfword 0x1: {SRC >> 16:#x}",
        f"reads local byte 0x1: {SRC >> 8 & 0xFF:#x}",
        f"reads local halfword 0x2: {DST & 0xFFFF:#x}",
        "reads local byte r4 + -0x1 = -0x1, outside the modelled local memory",
    ]
    assert boot.slot[4] == given(0)  # ra is the base, not written
    # A base of word 1 (dst) moves both by 4 bytes.
    _, flow = run_slot((load(0x16, 1), load(0x10, 1)), r4=given(1))
    assert flow.steps[0].note == f"reads local halfword 0x3: {DST >> 16:#x}"
    assert flow.steps[1].note == f"reads local byte 0x5: {DST >> 8 & 0xFF:#x}"


def test_family_a_offset_is_the_signed_low_13_bits_of_width_times_imm13():
    # From local word 2: 4 x 0x7ff and 2 x 0xffe are -4 B in 13 bits, the word and halfwords
    # below; 0x14 loads halfwords as 0x16 does.
    words = (load(0x1A, 0x7FF), load(0x16, 0xFFE), load(0x14, 0xFFF), load(0x14, 1))
    _, flow = run_slot(words, r4=given(2))
    assert [s.note for s in flow.steps[:4]] == [
        f"reads local word 0x1: {DST:#x}",
        f"reads local halfword 0x2: {DST & 0xFFFF:#x}",
        f"reads local halfword 0x3: {DST >> 16:#x}",
        "reads local halfword 0x5: 0x0",
    ]
    # Bit 11 of a halfword op's imm13 is the offset's sign, not the flag.
    _, flow = run_slot((load(0x16, 0x800),), r4=given(0))
    assert flow.steps[0].note.endswith("= -0x800, outside the modelled local memory")


@pytest.mark.parametrize("op, imm", [(0x1A, 0x800), (0x1A, 0x1000), (0x16, 0x1000)])
def test_family_a_flag_bits_are_not_covered(op: int, imm: int):
    with pytest.raises(UnknownSemantics, match="outside what its semantics covers"):
        run_slot((load(op, imm),), r4=given(0))


def test_family_a_stores_make_their_bytes_unknown():
    # 0x18 writes local word 1, 0x12 halfword 5 (bytes 10, 11): the reads after see unknown
    # data there and the request elsewhere.
    words = (load(0x18, 1), load(0x12, 5), load(0x1A, 0), load(0x1A, 1), load(0x16, 4))
    boot, flow = run_slot((*words, load(0x16, 5)), r4=given(0))
    assert [s.note for s in flow.steps[:6]] == [
        "writes local word 0x1: unknown datum",
        "writes local halfword 0x5: unknown datum",
        f"reads local word 0x0: {SRC:#x}",
        "reads local word 0x1: unknown",
        "reads local halfword 0x4: 0x400",
        "reads local halfword 0x5: unknown",
    ]
    assert boot.slot[4] == given(0)
    # At an unknown base the store may hit any byte.
    boot, flow = run_slot((load(0x12, 0), load(0x1A, 0, ra=5)), r5=given(0))
    assert flow.steps[0].note == "writes local halfword r4 + 0x0, r4 unknown: anywhere"
    assert flow.steps[1].note == "reads local word 0x0: unknown"


def test_loop_end_count_hypotheses():
    # 0: loop body 1..7 (back-edge at 3, slots 4..7), halt at 8.
    program = Program(
        (IDLE, IDLE, IDLE, control(0, 1, 1), IDLE, IDLE, IDLE, IDLE, HALT)
    )
    job = commands.copy(program, SRC, DST, 0x400)

    def run(count: Count, limit: int = 1000) -> Flow:
        boot = Boot()
        boot.slot[1] = Reg(3, 3, Confidence.VERIFIED, "set by the test")
        return Executor(boot, branching=RaTest(count=count), limit=limit).step(job)

    flow = run(Count.DECREMENT)
    assert flow.stop is Stop.HALT
    assert flow.order() == [0] + span((1, 7)) * 3 + [8]
    assert run(Count.TEST, limit=100).stop is Stop.STEP_LIMIT


def run_slot(words: tuple[Word, ...], **regs: Reg) -> tuple[Boot, Flow]:
    """Run `words` and a halt on a boot whose op-slot registers r<n> are `regs`."""
    boot = Boot()
    for name, reg in regs.items():
        boot.slot[int(name[1:])] = reg
    job = commands.copy(Program((*words, HALT)), SRC, DST, 0x400)
    return boot, Executor(boot).step(job)


def given(lo: int, hi: int | None = None) -> Reg:
    return Reg(lo, lo if hi is None else hi, Confidence.VERIFIED, "set by the test")


def test_shift_right_reads_rb_and_writes_ra():
    shift = Word.data(0x3F, Operands(ra=7, rb=2, imm=1))
    boot, flow = run_slot((shift,), r2=given(0x200, 0x201), r7=given(5))
    assert boot.slot[7] is not None and boot.slot[7].exact == 0x100
    assert boot.slot[7].taint is Confidence.HYPOTHESIS  # the destination ra
    assert boot.slot[2] == given(0x200, 0x201)
    assert flow.steps[0].note == "r7 := r2 >> 1 = 0x100"
    # A negative rb (logical and arithmetic shifts differ) leaves ra unknown, not stale.
    boot, _ = run_slot((shift,), r2=given(-4), r7=given(5))
    assert boot.slot[7] is None
    other = Word.from_fields({"f120_122": 3}, base=shift)
    with pytest.raises(UnknownSemantics, match="outside what its semantics covers"):
        run_slot((other,))


def test_and_mask_sign_extends_imm16():
    line = Word.data(0x38, Operands(ra=4, imm=0xFFE0))
    boot, _ = run_slot((line,), r4=given(0xA1113610))
    assert boot.slot[4] == Reg(
        0xA1113600, 0xA1113600, Confidence.INFERRED, "r4 & 0xffffffe0 (op 0x38)"
    )
    # Clearing low bits keeps the order of the bounds; a low mask bounds the result by itself.
    boot, _ = run_slot((line,), r4=given(0x30, 0x5F))
    assert (boot.slot[4].lo, boot.slot[4].hi) == (0x20, 0x40)
    low = Word.data(0x38, Operands(ra=4, imm=0xF))
    boot, _ = run_slot((low,), r4=given(0x13, 0x40))
    assert (boot.slot[4].lo, boot.slot[4].hi) == (0, 0xF)
    boot, _ = run_slot((low,), r4=given(0x13))
    assert boot.slot[4].exact == 3
    # A negative mask with low bits set is not monotonic: bounded by the input only.
    mixed = Word.data(0x38, Operands(ra=4, imm=0x8001))
    boot, _ = run_slot((mixed,), r4=given(0x10, 0x11))
    assert (boot.slot[4].lo, boot.slot[4].hi) == (0, 0x11)


def test_measured_ops_chain_through_the_slot():
    """A mask, then a shift of its result; an op without semantics breaks the chain (lenient)."""
    words = (
        Word.data(0x38, Operands(ra=2, imm=0xFFF0)),
        Word.data(0x3F, Operands(ra=7, rb=2, imm=2)),
        Word.data(0x40, Operands(ra=2, rb=2, imm=1)),
        Word.data(0x3F, Operands(ra=8, rb=2, imm=1)),
    )
    boot = Boot()
    boot.slot[2] = given(0x40F)
    job = commands.copy(Program((*words, HALT)), SRC, DST, 0x400)
    flow = Executor(boot, strict=False).step(job)
    assert boot.slot[7].exact == 0x100
    assert flow.steps[2].unknown and boot.slot[2] is None
    assert boot.slot[8] is None
    assert flow.steps[3].note == "r8 := r2 >> 1: an unknown input, unknown"
