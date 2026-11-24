"""The stepping core on the stock copy (loaded from the firmware at test time) and on small
assembled programs."""

from functools import cache

import pytest
from conftest import stock_image

from fenykl.dsp import commands
from fenykl.dsp.emu import Boot, Executor, Flow, Job, LinkError, Stop, start_copy
from fenykl.dsp.fields import Confidence, Operands
from fenykl.dsp.memcpy import ITEM, copy_branches
from fenykl.dsp.program import Program, stock_programs
from fenykl.dsp.semantics import (
    Branching,
    Count,
    Given,
    RaTest,
    Regs,
    RegSet,
    UnknownSemantics,
    destinations,
    lane_result,
    x_result,
)
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
    with pytest.raises(UnknownSemantics, match="word 10: .*under f265_266=0 is open"):
        run_copy(0x400, copy_branches(0x400), strict=True)


def test_ra_test_on_the_transfer_registers():
    """27 names the transfer register r7, which word 14's port f216_220 names under an enable
    whose destination is open: stepping cannot tell what 27 tests."""
    flow = run_copy(0x400, RaTest(reads=Regs.TRANSFER))
    assert flow.stop is Stop.UNDECIDED and flow.order()[-1] == 27


def control(code: int, target: int, ra: int) -> Word:
    return Word.from_fields(
        {"op": code, "f229": 0, "imm": target & 0xFF, "f128_132": target >> 8, "ra": ra}
    )


HALT = Word.from_fields({"op": 7})


def test_local_read_uses_ra_as_a_byte_base():
    read = Word.data(0x1A, Operands(ra=4, imm=1))
    job = commands.copy(Program((read, HALT)), SRC, DST, 0x400)
    boot = Boot()
    boot.slot[4] = Reg(4, 4, Confidence.VERIFIED, "set by the test")
    flow = Executor(boot).step(job)
    assert flow.steps[0].note == "reads local word 0x2: 0x400"
    assert boot.slot[4] == Reg(4, 4, Confidence.VERIFIED, "set by the test")
    # f120_122 has no effect on a load.
    other = Word.from_fields({"f120_122": 3}, base=read)
    boot.slot[4] = Reg(4, 4, Confidence.VERIFIED, "set by the test")
    job = commands.copy(Program((other, HALT)), SRC, DST, 0x400)
    assert Executor(boot).step(job).steps[0].note == "reads local word 0x2: 0x400"


def test_a_program_without_a_halt_runs_past_its_end():
    job = commands.copy(Program((IDLE, IDLE)), SRC, DST, 0x400)
    with pytest.raises(UnknownSemantics, match="word 2: .*past the program end"):
        Executor(Boot(), strict=False).step(job)


def load(op: int, imm: int, ra: int = 4) -> Word:
    """A family A word (a load or a store) at the byte address ra + width x imm."""
    return Word.data(op, Operands(ra=ra, imm=imm))


def test_word_halfword_and_byte_loads_share_one_base():
    # Local word 0 holds src: 0x1a reads it whole, 0x16 its halfwords, 0x10 its bytes.
    words = (load(0x1A, 0), load(0x16, 1), load(0x10, 1), load(0x16, 2), load(0x10, -1))
    boot, flow = run_slot(words, r4=given(0))
    assert [s.note for s in flow.steps[:5]] == [
        f"reads local word 0x0: {SRC:#x}",
        f"reads local halfword 0x1: {SRC >> 16:#x}",
        f"reads local byte 0x1: {SRC >> 8 & 0xFF:#x}",
        f"reads local halfword 0x2: {DST & 0xFFFF:#x}",
        "reads local byte 0x1fff: unknown",  # the address wraps at 8 KiB
    ]
    assert boot.slot[4] == given(0)  # ra is the base, not written
    # A base of byte 4 (dst) moves both by 4 bytes.
    _, flow = run_slot((load(0x16, 1), load(0x10, 1)), r4=given(4))
    assert flow.steps[0].note == f"reads local halfword 0x3: {DST >> 16:#x}"
    assert flow.steps[1].note == f"reads local byte 0x5: {DST >> 8 & 0xFF:#x}"


def test_family_a_offset_is_width_times_the_signed_imm13():
    # From byte 8, local word 2: 4 x -1 and 2 x -2 are -4 B, the word and halfwords below;
    # 0x14 loads halfwords as 0x16 does.
    words = (load(0x1A, -1), load(0x16, -2), load(0x14, -1), load(0x14, 1))
    _, flow = run_slot(words, r4=given(8))
    assert [s.note for s in flow.steps[:4]] == [
        f"reads local word 0x1: {DST:#x}",
        f"reads local halfword 0x2: {DST & 0xFFFF:#x}",
        f"reads local halfword 0x3: {DST >> 16:#x}",
        "reads local halfword 0x5: 0x0",
    ]
    # imm13 decodes signed; modulo 8 KiB the signed and unsigned readings give one address.
    _, flow = run_slot((load(0x16, -0x800),), r4=given(0x1000))
    assert flow.steps[0].operands.imm == -0x800
    assert flow.steps[0].note == f"reads local halfword 0x0: {SRC & 0xFFFF:#x}"


@pytest.mark.parametrize("base", [0x2004, 0x10004, -0x1FFC, 0x9D000004])
def test_local_addresses_wrap_at_8_kib(base):
    _, flow = run_slot((load(0x1A, 0),), r4=given(base))
    assert flow.steps[0].note == f"reads local word 0x1: {DST:#x}"


@pytest.mark.parametrize("op, base", [(0x1A, 2), (0x16, 1), (0x18, 6)])
def test_an_unaligned_family_a_access_is_not_covered(op: int, base: int):
    with pytest.raises(UnknownSemantics, match="outside what its semantics covers"):
        run_slot((load(op, 0),), r4=given(base))


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
    assert (
        flow.steps[0].note == "writes local halfword r4 + 0x0 B, r4 unknown: anywhere"
    )
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


def ported(word: Word, port: str, reg: int) -> Word:
    """`word` with one result port enabled: "x" (f267_268 = 1) or "operand" (f272_273 = 1)."""
    enable, field = {
        "x": ("f267_268", "f211_215"),
        "operand": ("f272_273", "f224_228"),
    }[port]
    return Word.from_fields({enable: 1, field: reg}, base=word)


def test_a_result_goes_to_its_port_register_after_the_latency():
    """Not to ra: f211_215 in the xreg set from 4 words on, f224_228 in the operand set from
    3 words on, unknown before."""
    shift = Word.data(0x3F, Operands(ra=2, rb=2, imm=1))
    words = (ported(shift, "operand", 9), ported(shift, "x", 12), IDLE)
    boot, _ = run_slot(words, r2=given(0x200, 0x201))
    assert boot.regs[12] is None and boot.slot[9] is not None
    assert (boot.slot[9].lo, boot.slot[9].hi) == (0x100, 0x100)
    assert boot.slot[9].taint is Confidence.INFERRED
    assert boot.slot[2] == given(0x200, 0x201)
    boot, _ = run_slot((*words, IDLE, IDLE))
    assert boot.slot[9] is None  # the input was unknown
    boot, _ = run_slot((*words, IDLE, IDLE), r2=given(0x200))
    assert boot.regs[12].exact == 0x100 and boot.slot[9].exact == 0x100


def test_a_result_without_a_decoded_port_is_open():
    shift = Word.data(0x3F, Operands(ra=2, rb=2, imm=1))
    with pytest.raises(UnknownSemantics, match="no PR port enabled"):
        run_slot((shift,), r2=given(0x200))
    other = Word.from_fields({"f272_273": 2, "f224_228": 4}, base=shift)
    with pytest.raises(UnknownSemantics, match="under f272_273=2 is open"):
        run_slot((other,), r2=given(0x200))
    # Lenient: the open port's register is unknown in both sets.
    boot = Boot()
    boot.slot[4] = boot.regs[4] = given(1)
    boot.slot[2] = given(0x200)
    job = commands.copy(Program((other, IDLE, IDLE, IDLE, HALT)), SRC, DST, 0x400)
    Executor(boot, strict=False).step(job)
    assert boot.slot[4] is None and boot.regs[4] is None


def test_and_mask_sign_extends_imm16():
    def masked(imm: int, r4: Reg) -> Reg | None:
        line = ported(Word.data(0x38, Operands(ra=4, imm=imm)), "operand", 5)
        boot, _ = run_slot((line, IDLE, IDLE, IDLE), r4=r4)
        return boot.slot[5]

    assert masked(-0x20, given(0xA1113610)) == Reg(
        0xA1113600, 0xA1113600, Confidence.VERIFIED, "r4 & 0xffffffe0 (op 0x38)"
    )
    # Clearing low bits keeps the order of the bounds; other masks are enumerated.
    wide = masked(-0x20, given(0x30, 0x5F))
    assert (wide.lo, wide.hi) == (0x20, 0x40)
    low = masked(0xF, given(0x13, 0x40))
    assert (low.lo, low.hi) == (0, 0xF)
    assert masked(0xF, given(0x13)).exact == 3
    mixed = masked(-0x7FFF, given(0x10, 0x11))
    assert (mixed.lo, mixed.hi) == (0, 1)


@pytest.mark.parametrize(
    "op, operands, a, b, value, taint",
    [
        # B, C, D, F: ra and the immediate as the op's format extends it; rb unused.
        (0x0B, Operands(ra=2, imm=-13), 0x1010, None, -13, "V"),
        (0x0A, Operands(ra=2, imm=0x1234), 0x10, None, 0x12340010, "I"),
        (0x37, Operands(ra=2, imm=0x7001), 0x10, None, 0x7011, "V"),
        (0x3E, Operands(ra=2, rb=3, imm=4), -0x100, 0x1234, -0x10, "I"),
        (0x3F, Operands(ra=2, rb=3, imm=4), -0x100, 0x1234, 0x0FFFFFF0, "I"),
        (0x40, Operands(ra=2, rb=3, imm=4), 0x10, 0x1234, 0x100, "I"),
        (0x1F, Operands(ra=2, imm=0xE010), 0x1010, None, 0xF020, "I"),  # zero-extended
        (0x25, Operands(ra=2, imm=0x7F0), 0x1010, None, 0x7F0, "V"),
        (0x25, Operands(ra=2, imm=-1), 0x10, None, -1, "V"),  # sign-extended
        (0x23, Operands(ra=2, imm=0xFFFF), 0x10, None, 1, "I"),
        (0x21, Operands(ra=2, imm=-1), 0x10, None, 0, "V"),
        (0x44, Operands(ra=2, imm=0x408), 7, None, 0, "V"),  # 7 + 1 wraps at L = 8
        (0x44, Operands(ra=2, imm=0x408), 2, None, 3, "V"),
        # E, on (r2, r3).
        (0x28, Operands(ra=2, rb=3), -0x10, 0x1010, 0, "V"),  # unsigned <=
        (0x2A, Operands(ra=2, rb=3), -0x10, 0x1010, 1, "V"),  # signed <=
        (0x2A, Operands(ra=2, rb=3), 0xFFFFFFF0, 0x10, 1, "V"),  # a value held unsigned
        (0x31, Operands(ra=2, rb=3), -1, 0xFFFFFFFF, 1, "V"),
        (0x2E, Operands(ra=2, rb=3), 0xFFFFFFF0, 0x10, 0x10, "V"),
        (0x3D, Operands(ra=2, rb=3), 0x400, 0, -0x401, "V"),
        (0x32, Operands(ra=2, rb=3), 0, -5, 5, "I"),
        (0x2F, Operands(ra=2, rb=3), -0x1000, 0x1010, -0x1000, "V"),
        (0x2E, Operands(ra=2, rb=3), -0x1000, 0x1010, 0x1010, "V"),
        (0x2C, Operands(ra=2, rb=3), -0x1000, 0x1010, -0x1000, "I"),  # unsigned
        (0x41, Operands(ra=2, rb=3), 0x1010, 0x22, 0x1010 >> 2, "V"),  # rb & 31
        (0x41, Operands(ra=2, rb=3), -0x1400, 1, -0xA00, "V"),  # arithmetic
        (0x42, Operands(ra=2, rb=3), -0x10, 4, 0x0FFFFFFF, "I"),  # logical
        (0x43, Operands(ra=2, rb=3), 0x10, 0x21, 0x20, "V"),
    ],
)
def test_measured_op_rules(op, operands, a, b, value, taint):
    word = Word.data(op, operands)
    regs = {"r2": given(a)} | ({} if b is None else {"r3": given(b)})
    boot, _ = run_slot((ported(word, "operand", 9), IDLE, IDLE, IDLE), **regs)
    assert boot.slot[9].exact == value
    assert (
        boot.slot[9].taint
        is {"I": Confidence.INFERRED, "V": Confidence.VERIFIED}[taint]
    )


def test_ops_chain_through_the_operand_set():
    """A mask into r2, then a shift of it three words on; an op without semantics breaks the
    chain (lenient)."""
    words = (
        ported(Word.data(0x38, Operands(ra=2, imm=-0x10)), "operand", 2),
        IDLE,
        IDLE,
        ported(Word.data(0x3F, Operands(ra=2, rb=2, imm=2)), "operand", 7),
        ported(Word.data(0x1B, Operands(ra=2, rb=2)), "operand", 2),
        IDLE,
        IDLE,
        ported(Word.data(0x3F, Operands(ra=2, rb=2, imm=1)), "operand", 8),
        IDLE,
        IDLE,
        IDLE,
    )
    boot = Boot()
    boot.slot[2] = given(0x40F)
    job = commands.copy(Program((*words, HALT)), SRC, DST, 0x400)
    flow = Executor(boot, strict=False).step(job)
    assert boot.slot[7].exact == 0x100
    assert flow.steps[4].unknown and boot.slot[2] is None
    assert boot.slot[8] is None
    assert flow.steps[7].note == "r2 >>> 0x1 (op 0x3f): an unknown input"


def test_a_multiply_writes_nothing_in_its_own_word():
    # -13 x 7: the product is recorded, its own word's port keeps r4.
    mul = ported(Word.data(0x49, Operands(ra=2, rb=3)), "operand", 4)
    boot, flow = run_slot((mul, IDLE), r2=given(-13), r3=given(7), r4=given(0x5555))
    assert flow.steps[0].note == (
        "r2 x r3 (op 0x49) = 0xffffffa5; on the ALU bus 1 word on, not delivered"
    )
    assert boot.slot[4] == given(0x5555)


def test_a_port_that_writes_nothing_leaves_no_open_destination():
    word = Word.from_fields(
        {"f267_268": 2, "f211_215": 5, "f272_273": 1, "f224_228": 6},
        base=Word.data(0x0B, Operands(ra=0, imm=1)),
    )
    assert destinations(word) == ([(RegSet.OPERAND, 6, 3)], [], "")


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
        ("s0", 0x0B, lanes(), lanes(), {}, {0: None, 31: None}),  # a carry form: open
        # Logical and arithmetic shifts by imm & 15 (10 here).
        ("s1", 0x0C, lanes(), lanes(l0=0x8000), {"imm": 0x2A}, {0: 0x20}),
        ("s1", 0x0D, lanes(), lanes(l0=0x8000), {"imm": 0x2A}, {0: 0xFFE0}),
        ("s1", 0x0E, lanes(), lanes(l0=0x600, l1=0x200), {"imm": 0x2A}, {0: 2, 1: 0}),
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
            lanes(l0=0, l1=0x600),
            {"imm": 0x2A},
            {0: 0x40, 1: 2},
        ),
        ("s1", 0x0F, lanes(l0=0x7FFF), lanes(l0=0xFFFF), {"imm": 0x10}, {0: 0x7FFF}),
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
            S3_NIBBLES | {"f92_95": 0xE},
            {1: None, 2: 6},
        ),
        ("s3", 0x22, lanes(), lanes(l0=6), S3_NIBBLES | {"f92_95": 7}, {0: 6, 31: 0}),
    ],
)
def test_lane_function_table(unit, head, a, b, scalars, expect):
    got = lane_result(unit, head, a, b, scalars)
    assert {k: got[k] for k in expect} == expect


def test_heads_without_a_lane_result():
    # A reduction's result is a scalar, a store-only S1 head writes nothing.
    assert lane_result("s3", 0x00, lanes(), lanes(), S3_NIBBLES) is None
    assert lane_result("s1", 0x54, lanes(), lanes(), {}) is None
    with pytest.raises(KeyError):
        lane_result("s0", 0x1C, lanes(), lanes(), {})  # open


def framed(*routine: Word) -> Job:
    """Call 0 -> 10, halt at 5, `routine` from word 10."""
    words = (Word.ctl(3, 10), *[IDLE] * 4, HALT, *[IDLE] * 4, *routine)
    return Job(Program(words), ())


# 10 is a local op 3: no return between it and its T 16.
LOCAL = framed(Word.ctl(3, 16), *[IDLE] * 5, Word.ctl(4), *[IDLE] * 4)


def test_a_local_op3_runs_as_a_jump_with_the_option():
    assert LOCAL.program.local_op3() == {10}
    flow = Executor(Boot(), local_op3_jump=True).step(LOCAL)
    assert flow.stop is Stop.HALT
    assert flow.order() == span((0, 4), (10, 14), (16, 20), (5, 5))


def test_a_return_popping_a_local_op3s_link_raises():
    with pytest.raises(
        LinkError, match="word 16: .*a return to 15, a local op 3's link"
    ):
        Executor(Boot()).step(LOCAL)


def test_a_call_with_a_return_before_its_target_stays_a_call_with_the_option():
    # 10 calls 22 over the return at 16; 22 returns to 15, which falls into 16 and back to 5.
    job = framed(
        Word.ctl(3, 22), *[IDLE] * 5, Word.ctl(4), *[IDLE] * 5, Word.ctl(4), *[IDLE] * 4
    )
    assert job.program.local_op3() == set()
    orders = {
        jump: Executor(Boot(), local_op3_jump=jump).step(job).order()
        for jump in (False, True)
    }
    assert (
        orders[False]
        == orders[True]
        == span((0, 4), (10, 14), (22, 26), (15, 20), (5, 5))
    )


def test_a_return_without_a_call_raises():
    job = Job(Program((Word.ctl(4), *[IDLE] * 4, HALT)), ())
    with pytest.raises(LinkError, match="word 0: .*a return without a call"):
        Executor(Boot()).step(job)
