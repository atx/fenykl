"""The datapath executor on synthetic words: lags, the lane memory, the mask chain, the reduction
register and a counted loop under the stepper's control flow."""

import random

import pytest

from fenykl.dsp.emu import LOCAL_BASE, Boot, Executor, Job
from fenykl.dsp.fields import Confidence, Operands
from fenykl.dsp.machine import Machine, Sv, Vec
from fenykl.dsp.program import Program
from fenykl.dsp.semantics import (
    LANE_RULES,
    X_RULES,
    lane_result,
    lane_tier,
    s_result,
    x_result,
)
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


def test_the_mask_chain_holds_one_word(m):
    m.lanes["L0"][1] = Vec(A)
    m.lanes["L0"][2] = Vec(tuple(a + (k % 2) * 2 - 1 for k, a in enumerate(A)))
    cmp = w(f278_284=0x32, f37_40=1, f41_44=2)  # a < b unsigned
    into_f0 = {"f276": 0, "f274_275": 0, "f255": 0}
    run(m, [cmp, w(**into_f0, f182_185=3), w(**into_f0, f182_185=4)])
    assert m.f["F0"][3] == Sv(0xAAAAAAAA)
    assert m.f["F0"][4].value is None  # never written


def test_the_reduction_register_from_the_third_word(m):
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
        ],
    )
    assert m.g[1].value is None  # nothing on M at lag 1: the port wrote nothing
    assert m.g[2] == Sv(max(B)) == m.g[3]


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
        {"ra": 2, "f272_273": 1, "f224_228": 2}, base=Word.ctl(0, 2, base=IDLE)
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


@pytest.mark.parametrize(("count", "tier"), [(15, V), (12, V), (1, I), (6, I)])
def test_a_lane_shift_takes_the_tier_of_its_count(count, tier):
    assert lane_tier("s3", 0x1B, {"f92_95": count}) == tier


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
    shift = w(f296_301=0x1C, f65_68=2, f92_95=5, f31_34=3, **l3_port(9, 0))
    run(m, [shift])
    assert m.lanes["L3"][9].lanes == (None,) * 32


def test_m_after_a_head_whose_next_word_is_open(m):
    """S1 0x13 puts a value on M one word later that is not fitted: unknown."""
    m.lanes["L1"][1] = Vec(A)
    m.f["F1"][3] = Sv(0x1234)
    head = w(f285_291=0x13, f45_48=1, f49_52=1)
    run(m, [head, w(f276=0, f274_275=1, f258=0, f190_193=3)])
    assert m.f["F1"][3] == Sv(None, Confidence.HYPOTHESIS)


def test_another_units_held_mask_is_not_on_m(m):
    """M under S1 one word after S0's compare: nothing written, the register at most inferred."""
    m.lanes["L0"][1] = Vec(A)
    m.f["F1"][3] = Sv(0x1234)
    run(
        m,
        [
            w(f278_284=0x32, f37_40=1, f41_44=1),
            w(f276=0, f274_275=1, f258=0, f190_193=3),
        ],
    )
    assert m.f["F1"][3] == Sv(0x1234, I)
