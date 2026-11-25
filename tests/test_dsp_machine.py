"""The datapath executor on synthetic words: lags, the lane memory, the mask chain, the reduction
register and a counted loop under the stepper's control flow."""

import random

import pytest

from fenykl.dsp.emu import LOCAL_BASE, Boot, Executor, Job, NoHalt, Stop
from fenykl.dsp.fields import Confidence, Operands
from fenykl.dsp.machine import Machine, Sv, Unknown, Vec
from fenykl.dsp.program import Program
from fenykl.dsp.semantics import (
    LANE_RULES,
    X_RULES,
    UnknownSemantics,
    lane_result,
    s_result,
    x_result,
)
from fenykl.dsp.state import Bytes
from fenykl.dsp.word import IDLE, Word

V, I = Confidence.VERIFIED, Confidence.INFERRED
SCALAR_KEYS = (
    "s",
    "r",
    "imm",
    "t",
    "f31_34",
    "f53_56",
    "f88_91",
    "f65_68",
    "f92_95",
    "f96_99",
)
SCALAR_KEYS += ("index", "value")
A = tuple(range(100, 132))
B = tuple((7 * k + 3) % 64 for k in range(32))


def w(**fields: int) -> Word:
    return Word.from_fields(fields, base=IDLE)


def run(m: Machine, words: list[Word]) -> None:
    for t, word in enumerate(words):
        m.execute(t, word, t)


@pytest.fixture
def m() -> Machine:
    return Machine(Boot())


def l3_port(reg: int, enable: int) -> dict[str, int]:
    """L3's second port (f250_251) on register `reg`: 0 S3's result, 2 S1's."""
    return {"f250_251": enable, "f175_178": reg}


@pytest.mark.parametrize("lag", [2, 3, 4, 5])
def test_a_lane_load_lands_four_words_after_its_head(m, lag):
    m.a[1] = Sv(0x40)
    m.memory[0x42] = Vec(A)
    m.lanes["L3"][5] = Vec(B)
    load = w(f302_307=0x1D, f100_104=1, f105_109=0, f2_6=1, f1=0)  # A[1] + 2
    port = w(f248_249=0, f171_174=5)
    run(m, [load, *[IDLE] * (lag - 1), port])
    assert m.lanes["L3"][5].lanes == (A if lag == 4 else B)


@pytest.mark.parametrize(
    ("head", "lag"), [(0x18, 0), (0xA, 1)]
)  # a broadcast and a shuffle
@pytest.mark.parametrize("at", [0, 1])
def test_a_port_takes_a_head_result_at_its_lag_only(m, head, lag, at):
    m.lanes["L3"][1] = Vec(A)
    m.lanes["L3"][2] = Vec(B)
    m.lanes["L3"][9] = Vec((0x5555,) * 32)
    m.f["F3"][7] = Sv(0x1234)
    s3 = {"f296_301": head, "f61_64": 1, "f65_68": 2, "f96_99": 7}
    words = [w(**s3, **l3_port(9, 0))] if at == 0 else [w(**s3), w(**l3_port(9, 0))]
    run(m, words)
    got = m.lanes["L3"][9].lanes
    if at != lag:
        assert got == (0x5555,) * 32
    elif head == 0x18:
        assert got == (0x1234,) * 32
    else:
        assert got == tuple(x for k in range(16) for x in (B[k], A[k]))


@pytest.mark.parametrize("at", [0, 1])
def test_s3_0x6_gives_zeros_one_word_after_its_head_only(m, at):
    m.lanes["L3"][1] = Vec(A)
    m.lanes["L3"][2] = Vec(B)
    m.lanes["L3"][9] = Vec((0x5555,) * 32)
    s3 = {"f296_301": 0x6, "f61_64": 1, "f65_68": 2}
    words = [w(**s3, **l3_port(9, 0))] if at == 0 else [w(**s3), w(**l3_port(9, 0))]
    run(m, words)
    got = m.lanes["L3"][9]
    assert got.lanes == ((0x5555,) * 32 if at == 0 else (0,) * 32)
    assert got.tier == Confidence.VERIFIED


def test_the_mask_chain_holds_one_word(m):
    m.lanes["L0"][1] = Vec(A)
    m.lanes["L0"][2] = Vec(tuple(a + (k % 2) * 2 - 1 for k, a in enumerate(A)))
    cmp = w(f278_284=0x32, f37_40=1, f41_44=2)  # a < b unsigned
    into_f0 = {"f276": 0, "f274_275": 0, "f255": 0}
    run(m, [cmp, w(**into_f0, f182_185=3), w(**into_f0, f182_185=4)])
    assert m.f["F0"][3] == Sv(0xAAAAAAAA)
    assert m.f["F0"][4].value is None  # never written


def test_a_reduction_is_on_m_in_the_third_word_only(m):
    m.lanes["L3"][4] = Vec(B)
    red = w(f296_301=0x0, f65_68=4)  # unsigned max
    read = {"f276": 0, "f274_275": 3, "f272_273": 2}
    run(
        m,
        [
            red,
            w(**read, f224_228=1),
            IDLE,
            w(**read, f224_228=2),
            w(**read, f224_228=3),
            w(**read, f224_228=4),
        ],
    )
    assert m.g[1].value is None  # nothing on M at lag 1: the port wrote nothing
    assert m.g[2] == Sv(max(B))
    assert m.g[3] == m.g[4] == Sv(None)  # gone at lags 4 and 5: the port wrote nothing


def test_an_extract_reads_only_its_lane(m):
    m.lanes["L3"][4] = Vec((None,) * 5 + (0xBEEF,) + (None,) * 26)
    m.f["F3"][2] = Sv(5)
    read = {"f276": 0, "f274_275": 3, "f272_273": 2}
    extract = w(**read, f296_301=0x16, f65_68=4, f92_95=2, f224_228=1)  # lane F3[2]
    run(m, [extract, w(f296_301=0x0, f65_68=4), IDLE, IDLE, w(**read, f224_228=2)])
    assert m.g[1] == Sv(0xBEEF)
    assert m.g[2].value is None  # the maximum of a vector with unknown lanes


def test_a_counted_loop_stores_through_r():
    """r2 := 3; loop: r3 += 5 (port 272 / 1); loop end on r2 (r2 - 1 back through 272 / 1); then
    R[0] := r3 and a word store to local 0x40; four passes."""

    def data(
        code: int, ra: int, rb: int | None = None, imm: int | None = None, **ports: int
    ) -> Word:
        return Word.from_fields(
            ports, base=Word.data(code, Operands(ra, rb, imm), base=IDLE)
        )

    li = data(0x0B, 31, imm=3, f272_273=1, f224_228=2)
    zero = data(0x0B, 31, imm=0, f272_273=1, f224_228=3)
    add = data(0x1F, 3, imm=5, f272_273=1, f224_228=3)
    end = Word.from_fields(
        {"ra": 2, "f120_122": 0, "f272_273": 1, "f224_228": 2},
        base=Word.ctl(0, 2, base=IDLE),
    )
    to_r = data(0x09, 31, 3, f270_271=1, f221_223=0)
    store = data(0x18, 31, imm=0x10, f120_122=0)
    halt = Word.from_fields({"op": 7}, base=IDLE)
    words = [li, zero, add, end, IDLE, IDLE, IDLE, IDLE, to_r, store, halt]
    boot = Boot()
    m = Machine(boot)
    m.g[31] = Sv(0)
    Executor(boot, datapath=m).step(Job(Program(tuple(words)), ()))
    got = boot.local.read(LOCAL_BASE + 0x40, 4)
    assert all(got.known)
    assert int.from_bytes(got.data, "little") == 4 * 5


# Tiers, taint and unknown operands.

PROBE_SPECIAL = (0, 1, 2, 0x100, 0x7FFF, 0x8000, 0xFFFE, 0xFFFF)


def test_declared_lane_reads_cover_what_the_rules_read():
    """Changing an operand a rule is not declared to read never changes its result, also on
    equal operands and edge values (where a random probe misses equality rules)."""
    rng = random.Random(7)

    def vec():
        return [rng.choice(PROBE_SPECIAL) for _ in range(32)]

    for unit, rules in LANE_RULES.items():
        for head, rule in rules.items():
            if rule.value is None:
                continue
            for i in range(60):
                a, b = vec(), vec()
                if i % 2:
                    b = list(a)
                c = {k: rng.randrange(16) for k in SCALAR_KEYS}
                base = lane_result(unit, head, a, b, c)
                if "a" not in rule.reads:
                    assert lane_result(unit, head, vec(), b, c) == base, (unit, head)
                if "b" not in rule.reads:
                    assert lane_result(unit, head, a, vec(), c) == base, (unit, head)


def test_declared_x_reads_cover_what_the_rules_read():
    rng = random.Random(7)
    values = (0, 1, 5, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF)
    for unit, rules in X_RULES.items():
        for head, rule in rules.items():
            if rule.value is None:
                continue
            for _ in range(60):
                a, b = rng.choice(values), rng.choice(values)
                k = rng.choice((0, 1, 5))
                base = x_result(unit, head, a, b, k)
                for other in values:
                    if "a" not in rule.reads:
                        assert x_result(unit, head, other, b, k) == base, (unit, head)
                    if "b" not in rule.reads:
                        assert x_result(unit, head, a, other, k) == base, (unit, head)


@pytest.mark.parametrize(
    ("head", "known"), [(0x0D, False), (0x0C, False), (0x13, True)]
)
def test_an_x_rule_on_unknown_registers(m, head, known):
    """a == b on two unknown registers is unknown; an immediate needs none."""
    word = w(f302_307=head, f100_104=1, f105_109=2, f2_6=3, f1=0)
    got = m._x(0, word, "x0", 0)
    assert (got.value is not None) == known


def test_a_signed_compare_against_a_negative_immediate():
    lanes = [0xFFE2] * 32
    c = {"imm": -26 & 0xFFFFFFFF, "t": 0, "r": 0}
    assert s_result("s1", 0x2B, lanes, lanes, c) == 0xFFFFFFFF  # -30 <= -26
    assert s_result("s1", 0x2A, lanes, lanes, c) == 0


def test_a_lane_shift_takes_the_tier_of_u(m):
    """Moving b down by one fills lane 0 from U, whose tier the result takes."""
    m.lanes["L3"][2] = Vec(B)
    m.u[3] = Vec((0x77,) * 32, Confidence.HYPOTHESIS)
    shift = w(f296_301=0x1B, f65_68=2, f92_95=15, f31_34=3, **l3_port(9, 0))
    run(m, [shift])
    got = m.lanes["L3"][9]
    assert got.lanes == (0x77, *B[:31])
    assert got.tier == Confidence.HYPOTHESIS


def test_an_unpredicted_shift_count_takes_nothing_from_u(m):
    m.lanes["L3"][2] = Vec(B)
    m.u[3] = Vec((0x77,) * 32)
    shift = w(f296_301=0x1C, f65_68=2, f92_95=6, f31_34=3, **l3_port(9, 0))
    run(m, [shift])
    assert m.lanes["L3"][9].lanes == (None,) * 32


def test_m_after_a_head_whose_next_word_is_open(m):
    """S1 0x13 puts a value on M one word later that is not fitted: unknown."""
    m.lanes["L1"][1] = Vec(A)
    m.f["F1"][3] = Sv(0x1234)
    head = w(f285_291=0x13, f45_48=1, f49_52=1)
    run(m, [head, w(f276=0, f274_275=1, f258=0, f190_193=3)])
    assert m.f["F1"][3] == Sv(None, I)


def test_an_own_word_result_after_an_open_head_is_unknown(m):
    """S1 0x13's value one word later is not fitted, so 0x54's own result does not stand alone."""
    m.lanes["L1"][1] = Vec(A)
    m.f["F1"][4] = Sv(0x55AA)
    head = w(f285_291=0x13, f45_48=1, f49_52=1)
    own = w(f285_291=0x54, f84_87=4, f276=0, f274_275=1, f258=0, f190_193=3)
    run(m, [head, own])
    assert m.f["F1"][3].value is None


def test_a_lone_own_word_result_under_another_unit_is_not_on_m(m):
    m.f["F1"][4] = Sv(0x55AA)
    m.f["F1"][3] = Sv(0x0A5A)
    run(m, [w(f285_291=0x54, f84_87=4, f276=0, f274_275=0, f258=0, f190_193=3)])
    assert m.f["F1"][3] == Sv(0x0A5A)


def test_an_own_word_result_beside_a_parked_head_under_another_unit_stays_inferred(m):
    m.f["F0"][5] = Sv(0x55AA)
    m.r[0] = Sv(0x0A5A)
    run(m, [w(f278_284=0x52, f76_79=5, f276=0, f274_275=1, f270_271=2, f221_223=0)])
    assert m.r[0] == Sv(0x0A5A, I)


def test_another_units_held_mask_is_not_on_m(m):
    """M under S1 one word after S0's compare: nothing written (verified)."""
    m.lanes["L0"][1] = Vec(A)
    m.f["F1"][3] = Sv(0x1234)
    run(
        m,
        [
            w(f278_284=0x32, f37_40=1, f41_44=1),
            w(f276=0, f274_275=1, f258=0, f190_193=3),
        ],
    )
    assert m.f["F1"][3] == Sv(0x1234)


def op_word(
    code: int, ra: int, rb: int | None = None, imm: int | None = None, **fields: int
) -> Word:
    return Word.from_fields(
        fields, base=Word.data(code, Operands(ra, rb, imm), base=IDLE)
    )


def test_an_op_reads_the_old_value_of_a_register_another_source_writes(m):
    m.g[12], m.g[13] = Sv(0x1000), Sv(1)
    x0_imm = {
        "f302_307": 0x13,
        "f105_109": 0xD,
        "f2_6": 2,
        "f1": 1,
    }  # X0's result: 0x345
    word = op_word(
        0x35, 12, 13, **x0_imm, f272_273=2, f224_228=12, f270_271=1, f221_223=0
    )
    run(m, [word])
    assert m.r[0] == Sv(0x1001)
    assert m.g[12] == Sv(0x345)


def test_a_head_reads_the_old_vector_of_a_register_its_port_writes(m):
    m.lanes["L1"][1] = Vec(A)
    m.lanes["L1"][2] = Vec(B)
    m.lanes["L1"][5] = Vec((0x1111,) * 32)
    add = {"f285_291": 0x34, "f49_52": 2}
    words = [
        w(**add, f45_48=1),
        w(
            **add, f45_48=5, f242_243=2, f159_162=5
        ),  # reads L1[5] while the first sum lands in it
        w(**l3_port(8, 2)),
    ]
    run(m, words)
    assert m.lanes["L1"][5] == Vec(tuple(a + b for a, b in zip(A, B, strict=True)))
    assert m.lanes["L3"][8] == Vec(tuple(0x1111 + b for b in B))


def test_a_lane_store_in_the_word_of_os_port_stores_the_old_vector(m):
    m.a[1] = Sv(0x60)
    m.lanes["L3"][1] = Vec(A)
    m.o[1] = Vec(B)
    o_port = {"f296_301": 0x19, "f65_68": 1, "f252_254": 1, "f179_181": 1}
    store = {"f302_307": 0x1A, "f100_104": 1, "f105_109": 0, "f1": 0, "f69_71": 1}
    run(m, [w(**o_port, **store, f2_6=0), w(**store, f2_6=1)])
    assert m.memory[0x60] == Vec(B)
    assert m.memory[0x62] == Vec(A)


def test_the_alu_bus_ors_a_product_with_the_ops_result(m):
    m.g[11], m.g[9] = Sv(7), Sv(0x123)
    words = [
        op_word(0x49, 11, 9),
        op_word(0x0B, 31, imm=0x1808, f272_273=1, f224_228=12),
    ]
    run(m, words)
    assert m.g[12] == Sv(0x7F5 | 0x1808)


@pytest.mark.parametrize(
    ("code", "target", "load", "want"),
    [
        (1, 9, True, Sv(0x4106)),
        (3, 9, True, Sv(0x4106)),
        (4, None, True, Sv(0x4106)),
        (3, 9, False, Sv(0x7F5, I)),  # a product in a jump word: not measured
    ],
)
def test_an_arrival_reaches_a_control_word(m, code, target, load, want):
    src = m.boot.source("test", "a known local word")
    m.boot.local.write(
        LOCAL_BASE + 0x100, Bytes.of((0x4106).to_bytes(4, "little"), src)
    )
    m.g[5], m.g[11], m.g[9] = Sv(0), Sv(7), Sv(0x123)
    ctl = Word.from_fields(
        {"ra": 11, "f120_122": 0, "f272_273": 1, "f224_228": 12},
        base=Word.ctl(code, target, base=IDLE),
    )
    before = (
        [op_word(0x1A, 5, imm=0x40), IDLE, IDLE] if load else [op_word(0x49, 11, 9)]
    )
    run(m, [*before, ctl])
    assert m.g[12] == want


@pytest.mark.parametrize(
    ("ra", "r", "rb", "want"),
    [
        (0x80001234, 5, 0x80000000, 0x1239),
        (0xFFFFFFF0, 0x20, 0x100, 0xFFFFFF10),
        (2, -5, 0x12345, 0x12342),
        (0x150, 3, 0x1FF, 0x153),
        (0x1234, 0x707, 0xFFFFFFF3, 0x193B),
    ],
)
def test_the_ring_step_takes_its_length_from_all_of_rb(m, ra, r, rb, want):
    m.g[10], m.g[11], m.r[1] = Sv(ra), Sv(rb), Sv(r & 0xFFFFFFFF)
    run(m, [op_word(0x47, 10, 11, f120_122=1, f272_273=1, f224_228=12)])
    assert m.g[12].value == want


def test_a_unit_bus_ors_a_late_result_with_the_next_words_own(m):
    m.lanes["L1"][1] = Vec(A)
    m.lanes["L1"][2] = Vec(B)
    words = [
        w(f285_291=0x34, f45_48=1, f49_52=2),  # a + b, on the bus one word later
        w(f285_291=0x03, f45_48=1, f49_52=2, **l3_port(8, 2)),  # a & b, in its own word
    ]
    run(m, words)
    want = tuple((a + b) | (a & b) for a, b in zip(A, B, strict=True))
    assert m.lanes["L3"][8] == Vec(want)


def test_a_register_form_store_and_load_address_by_rb(m):
    m.g[5], m.g[9], m.r[2] = Sv(0x40), Sv(0x31), Sv(0x8899AABB)
    store = op_word(0x13, 5, 9, f120_122=2)  # a halfword at 0x40 + 2 x 0x31
    load = op_word(0x17, 5, 9)  # the same halfword, sign-extended
    take = Word.from_fields({"f272_273": 1, "f224_228": 12}, base=IDLE)
    run(m, [store, load, IDLE, IDLE, take])
    assert m.boot.local.read(LOCAL_BASE + 0xA2, 2).data == bytes.fromhex("bbaa")
    assert m.g[12] == Sv(0xFFFFAABB)


def control(code: int, target: int | None = None, **fields: int) -> Word:
    return Word.from_fields(
        {"f120_122": 0, **fields}, base=Word.ctl(code, target, base=IDLE)
    )


def inc(reg: int) -> Word:
    return op_word(0x35, reg, 30, f272_273=1, f224_228=reg)  # reg += r30


def stepped(words: list[Word], **regs: int) -> Machine:
    boot = Boot()
    m = Machine(boot)
    m.g[30] = Sv(1)
    for name, value in regs.items():
        m.g[int(name[1:])] = Sv(value)
    Executor(boot, datapath=m).step(
        Job(Program((*words, Word.from_fields({"f313_319": 7}, base=IDLE))), ())
    )
    return m


def test_op_4_jumps_to_the_word_its_register_names():
    words = [control(4, f128_132=21), *[inc(16)] * 4, inc(17), inc(17), inc(18)]
    m = stepped(words, r16=0, r17=0, r18=0, r21=7, r1=5)
    assert (m.g[16].value, m.g[17].value, m.g[18].value) == (4, 0, 1)


def test_a_branch_in_a_delay_slot_jumps_after_its_own_slots():
    words = [
        control(2, 7, ra=11),
        control(2, 13, ra=11),
        *[inc(16)] * 3,
        inc(17),
        inc(17),
        inc(18),  # 7: the first target, the second branch's fourth slot
        inc(19),
        *[IDLE] * 4,
        inc(20),  # 13: the second target
    ]
    m = stepped(words, r11=7, r16=0, r17=0, r18=0, r19=0, r20=0)
    assert [m.g[r].value for r in (16, 17, 18, 19, 20)] == [3, 0, 1, 0, 1]


def test_a_loop_end_into_its_own_slots_runs_them_again():
    loop = control(0, 3, ra=8, f272_273=1, f224_228=8)
    words = [loop, inc(16), inc(17), inc(18), inc(19), inc(20)]
    m = stepped(words, r8=3, r16=0, r17=0, r18=0, r19=0, r20=0)
    assert [m.g[r].value for r in (8, 16, 17, 18, 19, 20)] == [2, 1, 1, 2, 2, 1]


def test_a_run_reads_what_the_run_before_on_its_boot_left():
    boot = Boot()
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    program = Program((inc(16), halt))
    boot.run(program, regs={"G": {30: 1, 16: 5}})
    assert boot.run(program).machine.g[16] == Sv(7)


def test_a_run_that_does_not_halt_raises_and_leaves_no_carry():
    boot = Boot()
    boot.run(Program((Word.from_fields({"f313_319": 7}, base=IDLE),)))
    assert boot.carry is not None
    with pytest.raises(NoHalt, match="the step limit") as e:
        boot.run(Program((control(3, 0), *[IDLE] * 4)), limit=50)
    assert e.value.flow.stop is Stop.STEP_LIMIT
    assert boot.carry is None


def test_a_job_starts_with_the_registers_the_job_before_left(m):
    m.g[16], m.r[3], m.f["F2"][5] = Sv(0x7110), Sv(0x7203), Sv(0x7525)
    m.lanes["L1"][6], m.u[6], m.o[3] = Vec(A), Vec(B), Vec(A)
    m.memory[0x70] = Vec(B)
    later = Machine(Boot(), m.between_jobs())
    assert (later.g[16], later.r[3], later.f["F2"][5]) == (
        Sv(0x7110),
        Sv(0x7203),
        Sv(0x7525),
    )
    assert (later.lanes["L1"][6], later.u[6], later.o[3]) == (Vec(A), Vec(B), Vec(A))
    assert later.memory[0x70] == Vec(B)


@pytest.mark.parametrize("lag", [4, 8, 30])
@pytest.mark.parametrize("between", [None, "extract", "copy"])
def test_a_reduction_is_not_on_m_past_its_first_word(m, lag, between):
    m.lanes["L3"][1] = Vec(A)
    m.r[0] = Sv(0xDEAD)
    reduce = w(f296_301=0x2, f65_68=1)
    other = {
        None: IDLE,
        "extract": w(f296_301=0xF, f65_68=1, f92_95=5, f31_34=1, f276=0, f274_275=3),
        "copy": w(f296_301=0x19, f65_68=1),
    }[between]
    read = w(f276=0, f274_275=3, f270_271=2, f221_223=0)
    run(m, [reduce, IDLE, other, *[IDLE] * (lag - 3), read])
    assert m.r[0] == Sv(0xDEAD)


def test_an_extract_meets_the_reduction_register_in_its_first_word_only(m):
    m.lanes["L3"][1] = Vec(A)
    reduce = w(f296_301=0x2, f65_68=1)
    extract = w(
        f296_301=0xF,
        f65_68=1,
        f92_95=5,
        f31_34=1,
        f276=0,
        f274_275=3,
        f270_271=2,
        f221_223=0,
    )
    run(m, [reduce, IDLE, IDLE, extract])
    assert m.r[0] == Sv(A[5] | sum(A))
    run(m, [reduce, IDLE, IDLE, IDLE, extract])
    assert m.r[0] == Sv(A[5])


def test_m_keeps_the_port_register_two_words_after_a_head(m):
    m.lanes["L1"][1], m.lanes["L1"][2] = Vec(A), Vec(B)
    m.r[0] = Sv(0x0A5A)
    read = w(f276=0, f274_275=1, f270_271=2, f221_223=0)
    run(m, [w(f285_291=0x34, f45_48=1, f49_52=2), IDLE, read])
    assert m.r[0] == Sv(0x0A5A)


def test_m_replaces_an_active_x0_heads_result(m):
    m.f["F0"][5] = Sv(0x1C3)
    word = w(
        f278_284=0x52,
        f76_79=5,
        f276=0,
        f274_275=0,
        f302_307=0x13,
        f105_109=8,
        f2_6=0x1E,
        f1=0,
    )
    run(m, [Word.from_fields({"f272_273": 2, "f224_228": 12}, base=word)])
    assert m.g[12] == Sv(0x1C3)


def test_the_words_behind_a_halt_run_and_a_control_word_there_raises():
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    boot = Boot()
    m = Machine(boot)
    m.g[30], m.g[16] = Sv(1), Sv(0)
    Executor(boot, datapath=m).step(Job(Program((halt, *[inc(16)] * 4)), ()))
    assert m.g[16] == Sv(3)
    with pytest.raises(UnknownSemantics, match="a control op behind a halt"):
        boot = Boot()
        Executor(boot, datapath=Machine(boot)).step(
            Job(Program((halt, control(2, 0, ra=1))), ())
        )


def test_the_machine_refuses_a_control_word_with_f120_122_low_bits():
    boot = Boot()
    job = Job(
        Program((Word.ctl(3, 1, base=IDLE), IDLE)), ()
    )  # the idle word's f120_122 is 7
    with pytest.raises(UnknownSemantics, match="f120_122 bits 0..1"):
        Executor(boot, datapath=Machine(boot)).step(job)


@pytest.mark.parametrize("source", [5, 6])
def test_os_port_under_5_and_6_keeps_its_content(m, source):
    m.lanes["L3"][1] = Vec(A)
    m.o[2] = Vec(B)
    run(m, [w(f296_301=0x19, f65_68=1, f252_254=source, f179_181=2)])
    assert m.o[2] == Vec(B)


def test_m_is_quiet_in_the_own_word_of_a_head_without_a_result_there(m):
    m.lanes["L1"][1], m.lanes["L1"][2] = Vec(A), Vec(B)
    m.r[0] = Sv(0x0A5A)
    read = {"f276": 0, "f274_275": 1, "f270_271": 2, "f221_223": 0}
    run(m, [w(f285_291=0x34, f45_48=1, f49_52=2, **read)])
    assert m.r[0] == Sv(0x0A5A)


def test_u_port_without_an_s3_head_writes_nothing(m):
    m.u[5] = Vec(A)
    run(m, [w(f232_233=1, f141_144=5)])
    assert m.u[5] == Vec(A)


def test_a_jump_second_behind_a_halt_is_not_taken():
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    words = (
        halt,
        inc(16),
        control(3, 7),
        inc(16),
        inc(16),
        inc(16),
        IDLE,
        inc(17),
        halt,
    )
    m = stepped(list(words), g16=0, g17=0)
    assert (m.g[16], m.g[17]) == (Sv(2), Sv(0))


def test_a_halt_in_a_taken_jumps_first_slot_wins():
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    words = (
        control(3, 9),
        halt,
        inc(16),
        inc(16),
        inc(16),
        inc(16),
        halt,
        IDLE,
        IDLE,
        inc(17),
        halt,
    )
    m = stepped(list(words), g16=0, g17=0)
    assert (m.g[16], m.g[17]) == (Sv(3), Sv(0))


@pytest.mark.parametrize("sel", [0, 2, 3])
def test_m_under_another_unit_is_quiet_in_a_heads_own_word(m, sel):
    m.lanes["L1"][1], m.lanes["L1"][2] = Vec(A), Vec(B)
    m.r[0] = Sv(0x0A5A)
    read = {"f276": 0, "f274_275": sel, "f270_271": 2, "f221_223": 0}
    run(m, [w(f285_291=0x34, f45_48=1, f49_52=2, **read)])
    assert m.r[0] == Sv(0x0A5A)


def test_m_without_any_head_is_quiet_at_inferred(m):
    m.r[0] = Sv(0x0A5A)
    run(m, [w(f276=0, f274_275=3, f270_271=2, f221_223=0)])
    assert m.r[0] == Sv(0x0A5A, I)


@pytest.mark.parametrize(
    ("jump", "k", "slots_tier", "target_tier"),
    [
        (control(3, 9), 2, V, V),
        (control(3, 9), 3, V, V),
        (control(3, 9), 4, V, V),
        (control(4, f128_132=20), 2, V, V),
        (control(4, f128_132=20), 3, V, V),
        (control(4, f128_132=20), 4, V, V),
    ],
)
def test_a_jump_due_among_the_words_behind_a_halt_redirects_them(
    jump, k, slots_tier, target_tier
):
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    slots = [inc(16)] * 4
    slots[k - 1] = halt
    # A jump to 9; slots 1..4; three words after the slots; the target counts in g17.
    words = (
        jump,
        *slots,
        inc(16),
        inc(16),
        inc(16),
        IDLE,
        inc(17),
        inc(17),
        inc(17),
    )
    m = stepped(list(words), g16=0, g17=0, g20=9)
    among_slots = 4 - k
    assert (m.g[16], m.g[17]) == (
        Sv(k - 1 + among_slots, slots_tier),
        Sv(3 - among_slots, target_tier),
    )


def test_a_halt_and_an_op_4_behind_a_halt_change_nothing():
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    m = stepped([halt, halt, inc(16), inc(16), inc(16)], g16=0)
    assert m.g[16] == Sv(2)
    m = stepped(
        [halt, inc(16), control(4, f128_132=20), inc(16), inc(16)], g16=0, g20=0
    )
    assert m.g[16] == Sv(2)


def test_m_under_s0_in_an_s3_heads_own_word_is_quiet(m):
    m.lanes["L3"][1] = Vec(A)
    m.r[0] = Sv(0x0A5A)
    read = {"f276": 0, "f274_275": 0, "f270_271": 2, "f221_223": 0}
    run(m, [w(f296_301=0x19, f65_68=1, **read)])
    assert m.r[0] == Sv(0x0A5A)


@pytest.mark.parametrize("head", [0x1C, 0x22])
def test_a_lane_shift_by_two_takes_in_two_of_us_lanes(m, head):
    m.lanes["L3"][2] = Vec(B)
    m.u[3] = Vec((0x77,) * 32)
    shift = w(f296_301=head, f65_68=2, f92_95=2, f31_34=3, **l3_port(9, 0))
    run(m, [shift])
    assert m.lanes["L3"][9] == Vec((*B[2:], 0x77, 0x77))


R0_UNDER = {"f276": 0, "f270_271": 2, "f221_223": 0}
"""M's strobe with port 270 / 2 on R[0]; f274_275 picks the unit."""
S1_AND = {"f285_291": 0x03, "f45_48": 1, "f49_52": 2}
"""S1's a & b: a lane result in its own word, nothing on M."""


@pytest.mark.parametrize(
    ("before", "unit", "tier"),
    [
        ([w(**S1_AND)], 1, V),  # the head's own unit
        ([w(**S1_AND)], 0, V),
        ([w(f296_301=0x19, f65_68=1)], 0, V),
        ([w(**S1_AND, f296_301=0x19, f65_68=1)], 1, I),  # two units' heads
        ([w(**S1_AND), IDLE], 0, V),
        ([], 1, I),  # no head before in the job
    ],
)
def test_nothing_on_m_behind_a_head_keeps_the_register(m, before, unit, tier):
    m.lanes["L1"][1], m.lanes["L1"][2] = Vec(A), Vec(B)
    m.lanes["L3"][1] = Vec(B)
    m.r[0] = Sv(0x0A5A)
    run(m, [*before, w(**R0_UNDER, f274_275=unit)])
    assert m.r[0] == Sv(0x0A5A, tier)


@pytest.mark.parametrize(("x0", "tier"), [(False, V), (True, I)])
def test_nothing_on_m_beside_an_active_x0_head_keeps_x0s_port_at_inferred(m, x0, tier):
    m.lanes["L1"][1], m.lanes["L1"][2] = Vec(A), Vec(B)
    m.g[12] = Sv(0x0A5A)
    x0_head = {"f302_307": 0x13, "f105_109": 8, "f2_6": 0x1E, "f1": 0} if x0 else {}
    read = w(f276=0, f274_275=1, f272_273=2, f224_228=12, **x0_head)
    run(m, [w(**S1_AND), IDLE, read])
    assert m.g[12] == Sv(0x0A5A, tier)


PENDING = "a jump pending behind a halt"


@pytest.mark.parametrize(
    ("before", "behind", "match"),
    [
        ([control(2, 7, ra=30)], [], PENDING),  # a taken br, not an op 3 or op 4
        # Op 4 with the halt in its first slot.
        ([control(4, f128_132=20)], [], PENDING),
        # Two jumps pending: an op 3 in an op 3's first slot, the halt in its first slot.
        ([control(3, 12), control(3, 12)], [], PENDING),
        # The two measured shapes together: a halt in an op 3's first slot, an op 3 behind it.
        ([control(3, 9)], [IDLE, control(3, 9)], "a control op behind a halt"),
    ],
)
def test_a_halt_in_an_unmeasured_jump_shape_raises(before, behind, match):
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    with pytest.raises(UnknownSemantics, match=match):
        stepped([*before, halt, *behind, *[IDLE] * 12], g20=12)


def control_tiers(words: list[Word]) -> dict[int, Confidence | None]:
    """The tier of each control word's step, by index."""
    boot = Boot()
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    program = Program((*words, halt, *[IDLE] * 3))
    flow = Executor(boot, datapath=Machine(boot)).step(Job(program, ()))
    return {s.index: s.confidence for s in flow.steps if s.taken is not None}


def test_a_jump_in_a_jumps_delay_slots_is_inferred():
    words = [control(3, 8), control(3, 13), *[IDLE] * 12]
    assert control_tiers(words) == {0: V, 1: I}


@pytest.mark.parametrize(("target", "tier"), [(6, V), (2, I)])
def test_a_jump_into_its_own_delay_slots_is_inferred(target, tier):
    assert control_tiers([control(3, target), *[IDLE] * 5]) == {0: tier}


def s2_high_half(head: int, t: int, tier: Confidence = V) -> Vec:
    m = Machine(Boot())
    m.lanes["L2"][1], m.lanes["L2"][2] = Vec(A), Vec(B)
    m.f["F2"][4] = Sv(t, tier)
    run(m, [w(f292_295=head, f53_56=1, f57_60=2, f88_91=4), w(f236_237=1, f147_150=9)])
    return m.lanes["L0"][9]


@pytest.mark.parametrize("head", [0x3, 0x5])
def test_s2s_high_half_by_a_negative_t_is_verified(head):
    # Measured with t's bit 15 set, its bits 16..31 set and clear: those bits are not read.
    wide, narrow = s2_high_half(head, 0xFFFF8003), s2_high_half(head, 0x8003)
    assert wide == narrow and wide.tier == V and None not in wide.lanes
    assert s2_high_half(head, 0x8003, I).tier == I


def test_a_halt_third_behind_a_halt_on_a_jumps_target_ends_the_job():
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    # op 3 to 6, the halt in its fourth slot: the tail is the target's three words.
    words = [
        control(3, 6),
        IDLE,
        IDLE,
        IDLE,
        halt,
        IDLE,
        inc(17),
        inc(17),
        halt,
        inc(17),
    ]
    m = stepped(words, g17=0)
    assert m.g[17] == Sv(2, V)


@pytest.mark.parametrize(
    ("head", "later", "tier"),
    [(0x60, 0, V), (0x60, 1, V), (0x50, 0, I), (0x50, 1, I), (0x50, 2, V)],
)
def test_quiet_m_beside_an_unread_head_stays_inferred(m, head, later, tier):
    # M under S1 beside an S0 head: read beside every head but the parked ones.
    m.r[0] = Sv(0x0A5A)
    read = {"f276": 0, "f274_275": 1, "f270_271": 2, "f221_223": 0}
    if later == 0:
        words = [w(f278_284=head, **read)]
    else:
        words = [w(f278_284=head), *[IDLE] * (later - 1), w(**read)]
    run(m, words)
    assert m.r[0] == Sv(0x0A5A, tier)


@pytest.mark.parametrize(
    ("a", "b", "quotient", "tier"),
    [
        (1000, 7, 142, V),
        (7, 1000, 0, V),
        (-1000 & 0xFFFFFFFF, 7, -142 & 0xFFFFFFFF, I),
        (1000, 0, None, V),
    ],
)
@pytest.mark.parametrize("lag", [0, 3, 4, 5])
def test_x1_divides_onto_its_bus_four_words_after_the_head(
    m, a, b, quotient, tier, lag
):
    """G[5] := X1's result (f272_273 = 0) at `lag` words after X1 0x1e on B[1] / B[2]."""
    m.b[1], m.b[2], m.g[5] = Sv(a), Sv(b), Sv(0x5A)
    head = {"f308_312": 0x1E, "f110_114": 1, "f115_119": 2}
    port = {"f272_273": 0, "f224_228": 5}
    words = [w(**head)] + [IDLE] * 5
    words[lag] = w(**port, **(head if lag == 0 else {}))
    run(m, words)
    if lag == 4:
        assert m.g[5] == Sv(quotient, tier)
    elif lag == 0:
        assert m.g[5] == Sv(0x5A)  # nothing on the bus in the head's own word
    else:
        assert m.g[5].value is None  # an idle X1's bus is not known


def test_s1_0x60_gives_the_b_its_unit_last_read_latched(m):
    m.lanes["L1"][2], m.lanes["L1"][5] = Vec(A), Vec(B)
    words = [
        w(f285_291=0x61, f45_48=1, f49_52=5, **l3_port(8, 2)),  # no S1 head before: 0
        w(f285_291=0x34, f45_48=1, f49_52=2),
        w(f285_291=0x39, f45_48=1, f49_52=5),  # reads a only: the latch stays
        IDLE,
        w(f285_291=0x61, f45_48=1, f49_52=5, **l3_port(9, 2)),
        IDLE,
    ]
    run(m, words)
    assert m.lanes["L3"][8] == Vec((0,) * 32)
    assert m.lanes["L3"][9] == Vec(tuple(A))


@pytest.mark.parametrize("gap", [1, 4, 5])
def test_two_divides_in_flight_are_inferred(m, gap):
    """Two X1 0x1e heads `gap` words apart; G[5] and G[6] take the two quotients."""
    m.b[1], m.b[2], m.b[3] = Sv(1000), Sv(7), Sv(10)
    first = {"f308_312": 0x1E, "f110_114": 1, "f115_119": 2}
    second = {"f308_312": 0x1E, "f110_114": 1, "f115_119": 3}
    words = [w(**first)] + [IDLE] * (gap + 4)
    words[gap] = w(**second)
    words[4] = w(f272_273=0, f224_228=5, **(second if gap == 4 else {}))
    words[gap + 4] = w(f272_273=0, f224_228=6)
    run(m, words)
    tier = V if gap > 4 else I
    assert m.g[5] == Sv(142, tier) and m.g[6] == Sv(100, tier)


@pytest.mark.parametrize(
    ("count", "lane", "tier"),
    [(2, 0b10, V), (10, 0x200, I), (10, 0x201, V), (0, 0x8000, V)],
)
def test_s1s_rounding_of_a_tie_is_verified_only_at_the_counts_met(count, lane, tier):
    # S1 0xe by imm & 15 = f84_87 << 1; the tie in lane 7 only.
    m = Machine(Boot())
    m.lanes["L1"][2] = Vec(tuple(lane if k == 7 else 0 for k in range(32)))
    head = w(f285_291=0x0E, f49_52=2, f84_87=count >> 1)
    run(m, [head, w(f242_243=2, f159_162=9)])
    assert m.lanes["L1"][9].tier == tier and None not in m.lanes["L1"][9].lanes


def test_the_trace_is_off_by_default(m):
    run(m, [w(f285_291=0x34, f45_48=1, f49_52=2), w(f242_243=2, f159_162=9)])
    assert m.trace is None and None in m.lanes["L1"][9].lanes


def test_the_trace_names_a_heads_unknown_operand():
    m = Machine(Boot(), trace=True)
    m.lanes["L1"][1] = Vec(A)
    run(m, [w(f285_291=0x34, f45_48=1, f49_52=2), w(f242_243=2, f159_162=9)])
    assert m.trace == {
        "L1[9]": Unknown(1, "S1's result", "s1 0x34: operand b = L1[2] not known")
    }


def test_the_trace_follows_a_load_of_an_unwritten_vector():
    m = Machine(Boot(), trace=True)
    m.a[1] = Sv(0x40)
    load = w(f302_307=0x1D, f100_104=1, f105_109=0, f2_6=1, f1=0)  # A[1] + 2
    run(m, [load, *[IDLE] * 3, w(f248_249=0, f171_174=5), w(f248_249=0, f171_174=6)])
    (entry,) = m.trace.values()
    assert m.trace == {"L3[5]": entry} and entry.index == 4
    assert entry.reason == "memory[0x42] not written by the datapath"


ALONE_HEAD = {"s0": "f278_284", "s1": "f285_291"}
"""The head fields of the units with `fields.ALONE_ZEROS`."""
SENT = Vec((0x0A5A,) * 32)
ZEROS_0_15 = Vec((0,) * 16 + (None,) * 16)


def alone_port(unit: str, reg: int) -> dict[str, int]:
    """The unit's port into L3[reg]: f248_249 / 1 for S0's result, f250_251 / 2 for S1's."""
    return {"f248_249": 1, "f171_174": reg} if unit == "s0" else l3_port(reg, 2)


CAMERA_SELECTS = {
    "s0": {"f37_40": 1, "f41_44": 2, "f72_75": 4, "f76_79": 5, "f23_30": 0x5A},
    "s1": {"f45_48": 1, "f49_52": 2, "f80_83": 4, "f84_87": 5},
}
"""The selects the parked heads ran with on the camera: a, b, s / t and S0's immediate byte."""


def alone_head(unit: str, head: int) -> dict[str, int]:
    return {ALONE_HEAD[unit]: head, **CAMERA_SELECTS[unit]}


def alone_operands(m: Machine) -> None:
    for f in ("L0", "L1"):
        m.lanes[f][1], m.lanes[f][2] = Vec(A), Vec(B)
    m.lanes["L3"][8] = m.lanes["L3"][9] = SENT


@pytest.mark.parametrize(
    ("unit", "head", "lag"),
    [("s0", 0x54, 1), ("s0", 0x5F, 1), ("s1", 0x57, 0), ("s1", 0x5F, 0)],
)
def test_a_parked_head_alone_in_its_job_gives_zeros_at_its_lag_only(m, unit, head, lag):
    """As run on the camera: the head twice, its port in its own word, then one word later."""
    alone_operands(m)
    h = alone_head(unit, head)
    run(
        m, [w(**h, **alone_port(unit, 8)), IDLE, IDLE, w(**h), w(**alone_port(unit, 9))]
    )
    got = (m.lanes["L3"][8], m.lanes["L3"][9])
    assert got == ((ZEROS_0_15, SENT) if lag == 0 else (SENT, ZEROS_0_15))


@pytest.mark.parametrize(
    ("unit", "head", "lag", "before"),
    [
        ("s0", 0x54, 1, alone_head("s0", 0x55)),  # another value of the unit
        ("s0", 0x54, 1, {"f285_291": 0x03, "f45_48": 1, "f49_52": 2}),  # an S1 head
        ("s1", 0x57, 0, {"f278_284": 0x07, "f37_40": 1, "f41_44": 2}),  # an S0 head
        ("s1", 0x57, 0, {"f292_295": 0x02, "f53_56": 1, "f57_60": 2}),  # an S2 head
    ],
)
def test_a_parked_head_after_another_head_is_not_known(m, unit, head, lag, before):
    alone_operands(m)
    h = alone_head(unit, head)
    port = alone_port(unit, 8)
    run(
        m,
        [
            w(**before),
            IDLE,
            IDLE,
            *([w(**h, **port)] if lag == 0 else [w(**h), w(**port)]),
        ],
    )
    assert m.lanes["L3"][8].lanes == (None,) * 32


@pytest.mark.parametrize(
    "beside",
    [{"f296_301": 0x19, "f65_68": 1}, {"f292_295": 0x02, "f53_56": 1, "f57_60": 2}],
)
@pytest.mark.parametrize(("unit", "head", "lag"), [("s0", 0x54, 1), ("s1", 0x57, 0)])
def test_a_parked_head_beside_another_s_head_is_not_known(m, unit, head, lag, beside):
    alone_operands(m)
    h = {**alone_head(unit, head), **beside}
    port = alone_port(unit, 8)
    run(m, [w(**h, **port)] if lag == 0 else [w(**h), w(**port)])
    assert m.lanes["L3"][8].lanes == (None,) * 32


@pytest.mark.parametrize(
    ("unit", "head", "lag", "runs", "tier"),
    [
        ("s0", 0x54, 1, 1, I),  # the zeros were read on S0's second run
        ("s0", 0x54, 1, 3, None),
        ("s1", 0x57, 0, 2, I),  # and on S1's first
        ("s1", 0x57, 0, 3, None),
    ],
)
def test_a_parked_heads_zeros_on_a_run_not_read_are_inferred_then_unknown(
    m, unit, head, lag, runs, tier
):
    alone_operands(m)
    h = alone_head(unit, head)
    port = alone_port(unit, 8)
    last = [w(**h, **port)] if lag == 0 else [w(**h), w(**port)]
    run(m, [*[w(**h), IDLE, IDLE] * (runs - 1), *last])
    got = m.lanes["L3"][8]
    if tier is None:
        assert got.lanes == (None,) * 32
    else:
        assert got == Vec(ZEROS_0_15.lanes, tier)


def test_a_parked_head_after_its_own_run_beside_another_head_is_not_known(m):
    alone_operands(m)
    h = alone_head("s0", 0x54)
    beside = w(**h, f296_301=0x19, f65_68=1)
    run(m, [beside, IDLE, IDLE, w(**h), w(**alone_port("s0", 8))])
    assert m.lanes["L3"][8].lanes == (None,) * 32


@pytest.mark.parametrize("port_at", [1, 2])
def test_a_parked_heads_adjacent_runs_are_not_known(m, port_at):
    """S0 0x54 in two consecutive words, its port in the second word or the third."""
    alone_operands(m)
    h = w(**alone_head("s0", 0x54))
    words = [h, h, IDLE]
    words[port_at] = w(**alone_head("s0", 0x54), **alone_port("s0", 8))
    if port_at == 2:
        words[port_at] = w(**alone_port("s0", 8))
    run(m, words)
    assert m.lanes["L3"][8].lanes == (None,) * 32


@pytest.mark.parametrize(
    ("unit", "head", "other"),
    [
        ("s0", 0x54, {"f37_40": 3}),
        ("s0", 0x54, {"f76_79": 6}),
        ("s1", 0x57, {"f49_52": 5}),
        ("s1", 0x57, {"f84_87": 3}),
    ],
)
def test_a_parked_head_on_other_selects_is_not_known(m, unit, head, other):
    alone_operands(m)
    h = {**alone_head(unit, head), **other}
    port = alone_port(unit, 8)
    run(m, [w(**h, **port)] if unit == "s1" else [w(**h), w(**port)])
    assert m.lanes["L3"][8].lanes == (None,) * 32


@pytest.mark.parametrize(("unit", "head"), [("s0", 0x54), ("s1", 0x57)])
def test_a_parked_heads_port_in_both_words_is_not_known(m, unit, head):
    alone_operands(m)
    port = alone_port(unit, 8)
    run(m, [w(**alone_head(unit, head), **port), w(**port)])
    assert m.lanes["L3"][8].lanes == (None,) * 32


@pytest.mark.parametrize(
    ("unit", "head", "lag", "port"),
    [
        ("s0", 0x54, 1, {"f236_237": 2, "f147_150": 8}),  # into L0
        ("s1", 0x57, 0, {"f238_239": 2, "f151_154": 8}),  # into L0
    ],
)
def test_a_parked_heads_result_through_another_port_is_not_known(
    m, unit, head, lag, port
):
    alone_operands(m)
    m.lanes["L0"][8] = SENT
    h = alone_head(unit, head)
    run(m, [w(**h, **port)] if lag == 0 else [w(**h), w(**port)])
    assert m.lanes["L0"][8].lanes == (None,) * 32


def test_a_parked_head_after_an_s3_setup_gives_zeros_and_traces_the_lanes_not_read():
    m = Machine(Boot(), trace=True)
    alone_operands(m)
    m.f["F3"][15] = Sv(0x0A5A)
    sentinel = w(f296_301=0x18, f96_99=15, **l3_port(8, 0))  # S3 broadcasts F3[15]
    h = alone_head("s1", 0x57)
    run(m, [sentinel, IDLE, w(**h, **alone_port("s1", 8)), IDLE])
    assert m.lanes["L3"][8] == ZEROS_0_15
    assert m.trace["L3[8]"].reason == "s1 0x57: lanes 16..31 not read"


@pytest.mark.parametrize("at", [0, 1])
def test_an_alone_s0_zero_head_leaves_m_and_the_latch_unknown(m, at):
    alone_operands(m)
    m.r[0] = Sv(0x0A5A)
    h = alone_head("s0", 0x54)
    m_read = {**R0_UNDER, "f274_275": 0}
    words = [w(**h, **m_read)] if at == 0 else [w(**h), w(**m_read)]
    # S0 0x61 gives the a S0's last head latched, one word later.
    latch = [w(**alone_head("s0", 0x61)), w(**alone_port("s0", 9))]
    run(m, [*words, IDLE, *latch])
    assert m.r[0].value is None
    assert m.lanes["L3"][9].lanes == (None,) * 32
