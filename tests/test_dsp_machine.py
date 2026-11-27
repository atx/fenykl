"""The datapath executor on synthetic words: lags, the lane memory, the mask chain, the reduction
register and a counted loop under the stepper's control flow; lenient stepping, also of the stock
programs."""

import random
import struct
from functools import cache

import pytest
from conftest import stock_image

from fenykl.dsp import NOP, OP, assemble, hand
from fenykl.dsp.asm import VIEWS
from fenykl.dsp.emu import HALT_OP, LOCAL_BASE, Boot, Executor, Flow, Job, NoHalt, Stop
from fenykl.dsp.fields import LANE_VECTORS, Composite, Confidence, Operands
from fenykl.dsp.machine import Machine, Sv, Unknown, Vec
from fenykl.dsp.program import Program, stock_programs
from fenykl.dsp.semantics import (
    HEAD_READS,
    Given,
    UnknownSemantics,
    head_reads,
    s_result,
)
from fenykl.dsp.state import Bytes
from fenykl.dsp.stock import commands
from fenykl.dsp.stream import Engine
from fenykl.dsp.units import UNITS
from fenykl.dsp.word import IDLE, Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED
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
    ("heads", "at", "tier"),
    [
        ((0x1D, 0x1D), (0, 1), V),
        ((0x1D,) * 16, tuple(range(16)), V),
        ((0x1D,) * 17, tuple(range(17)), I),
        ((0x1D,) * 32, tuple(range(0, 64, 2)), V),
        ((0x1D,) * 33, tuple(range(0, 66, 2)), I),
        ((0x1D, 0x1D), (0, 3), V),
        ((0x1D, 0x1D), (0, 4), V),
        ((0x1D, 0x1D, 0x1D), (0, 4, 8), I),
        ((0x1D,) * 6, (0, 1, 4, 5, 8, 9), V),
        ((0x1D,) * 4, (0, 1, 4, 6), I),
        ((0x1D, 0x1D, 0x1D), (0, 2, 3), I),
        ((0x1D, 0x1D), (0, 5), V),
        ((0x1C, 0x1D), (0, 1), I),
    ],
)
def test_lane_loads_in_flight_together(m, heads, at, tier):
    """The last of the loads lands verified while their chain begins a measured one."""
    m.a[1] = Sv(0x40)
    for k in range(len(at)):
        m.memory[0x40 + k] = Vec(A if k % 2 else B)
    words = [IDLE] * (at[-1] + 5)
    for k, (head, t) in enumerate(zip(heads, at, strict=True)):
        words[t] = w(f302_307=head, f100_104=1, f105_109=0, f2_6=k >> 1, f1=k & 1)
    words[at[-1] + 4] = w(f248_249=0, f171_174=5)
    run(m, words)
    assert m.lanes["L3"][5] == Vec(A if (len(at) - 1) % 2 else B, tier)


@pytest.mark.parametrize(("third", "at", "tier"), [(0x1D, 2, V), (0x1C, 2, I), (0x1D, 3, I)])
def test_a_load_off_the_chain_caps_the_loads_still_in_flight(m, third, at, tier):
    """Loads at words 0, 1 and `at`; the first load's vector into L3[5] four words on."""
    m.a[1] = Sv(0x40)
    m.memory[0x40] = Vec(A)
    load = {"f100_104": 1, "f105_109": 0, "f2_6": 0}
    words = [w(f302_307=0x1D, f1=0, **load), w(f302_307=0x1D, f1=1, **load)]
    words += [IDLE] * 4
    words[at] = w(f302_307=third, f1=0, **load)
    words[4] = w(f248_249=0, f171_174=5)
    run(m, words)
    assert m.lanes["L3"][5] == Vec(A, tier)


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


@pytest.mark.parametrize(
    ("file", "port"), [("L3", {"f248_249": 1, "f171_174": 9}), ("L2", {"f244_245": 1, "f163_166": 9})]
)
@pytest.mark.parametrize("at", [0, 1, 2])
def test_s0_0x52_keeps_the_port_register_beside_its_scalar_result(m, file, port, at):
    """S0 0x52 returns F0[f76_79] on M and writes no lane (measured on the camera at lags 0 and 1)."""
    m.lanes["L0"][1] = Vec(A)
    m.lanes["L0"][2] = Vec(B)
    m.lanes[file][9] = Vec((0x5555,) * 32)
    s0 = {"f278_284": 0x52, "f37_40": 1, "f41_44": 2, "f76_79": 5, "f276": 0}
    run(m, [w(**s0, **port)] if at == 0 else [w(**s0), *[IDLE] * (at - 1), w(**port)])
    assert m.lanes[file][9] == Vec((0x5555,) * 32)


def test_a_lag_1_s0_head_reaches_the_port_in_an_s0_0x52_word(m):
    """Program 15's 455 / 456 form: S0 0x20 (a + b), then S0 0x52 with S0's lane port."""
    m.lanes["L0"][1] = Vec(A)
    m.lanes["L0"][2] = Vec(B)
    add = {"f278_284": 0x20, "f37_40": 1, "f41_44": 2}
    s0 = {"f278_284": 0x52, "f37_40": 1, "f41_44": 2, "f76_79": 5, "f248_249": 1, "f171_174": 9}
    run(m, [w(**add), w(**s0)])
    assert m.lanes["L3"][9].lanes == tuple((x + y) & 0xFFFF for x, y in zip(A, B))


@pytest.mark.parametrize(
    ("head", "port"),
    [
        ({"f278_284": 0x12}, {"f248_249": 1, "f171_174": 9}),
        ({"f292_295": 0xC}, {"f250_251": 1, "f175_178": 9}),
        ({"f292_295": 0xE}, {"f250_251": 1, "f175_178": 9}),
        ({"f296_301": 0x25}, {"f250_251": 0, "f175_178": 9}),
    ],
)
@pytest.mark.parametrize("at", [0, 1])
def test_a_head_measured_to_write_no_lane_leaves_the_port_register(m, head, port, at):
    m.lanes["L3"][9] = Vec((0x5555,) * 32)
    run(m, [w(**head, **port)] if at == 0 else [w(**head), w(**port)])
    assert m.lanes["L3"][9] == Vec((0x5555,) * 32)


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


X_VALUES = (0, 1, 5, 0x7FF, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF)


class _Transfers:
    """A stream engine that keeps what X1's transfers hand it; 0x1d hands nothing
    (`stream.Engine.transfer`)."""

    cap = V
    off = ""

    def __init__(self) -> None:
        self.got: list[tuple[int, Sv | None]] = []

    def transfer(self, m, index, head, value, t) -> None:
        self.got.append((head, None if head == 0x1D else value))

    def lane_tier(self, vector=None):
        return V


def _registers(rng: random.Random, unit: str, words: tuple[Word, ...]) -> dict:
    """Known registers of every file from edge values and the words' immediates zero- and
    sign-extended, so compares against an immediate meet equal values; on every third draw one
    value or vector fills each file, so equality rules meet equal operands."""
    imms = []
    for o in VIEWS[unit].operands.values():
        if isinstance(o, Composite):
            for w in words:
                v = o.get(w.value) & ((1 << o.width) - 1)
                imms += [v, v - (v >> (o.width - 1) << o.width)]
    words = X_VALUES + tuple(v & 0xFFFFFFFF for v in imms)
    lanes = PROBE_SPECIAL + tuple(v & 0xFFFF for v in imms)
    pool = [Vec(tuple(rng.choice(lanes) for _ in range(32))) for _ in range(8)]
    equal = rng.randrange(3) == 0

    def scalars(n: int) -> list[Sv]:
        if equal:
            return [Sv(rng.choice(words))] * n
        return [Sv(rng.choice(words + (rng.getrandbits(32),))) for _ in range(n)]

    def vectors(n: int) -> list[Vec]:
        return [pool[0]] * n if equal else [rng.choice(pool) for _ in range(n)]

    regs = {"g": scalars(32), "a": scalars(32), "b": scalars(32), "r": scalars(8)}
    regs["f"] = {name: scalars(16) for name in ("F0", "F1", "F2", "F3")}
    regs["lanes"] = {name: vectors(16) for name in ("L0", "L1", "L2", "L3")}
    regs["u"], regs["o"] = vectors(16), vectors(8)
    regs["latched"] = {"s0": rng.choice(pool), "s1": rng.choice(pool)}
    if unit == "x0":
        regs["memory"] = {k: rng.choice(pool) for k in range(LANE_VECTORS)}
    return regs


def _head_result(unit: str, regs: dict, word: Word) -> tuple:
    """What the machine's code for `unit`'s head does in `word` on `regs`: its result and what it
    leaves (results due, the lane memory, the latch, U's port, the stream's transfers) and
    its lapses on a lenient machine."""
    stream = _Transfers()
    m = Machine(Boot(), stream=stream)
    m.strict = False
    for name, v in regs.items():
        nested = name in ("f", "lanes")
        setattr(m, name, {k: list(x) for k, x in v.items()} if nested else v.copy())
    got = m._x(0, word, unit, 0) if unit in ("x0", "x1") else m._s_units(0, word, 0)
    return (
        got,
        m.lane_due,
        m.held_due,
        m.reductions,
        m.x1_due,
        m.load_due,
        m.memory,
        m.latched,
        m._u_write,
        stream.got,
        m.lapses,
        m.cap,
    )


def _changes(rng: random.Random, o, value: int) -> list[int]:
    """Other values of operand `o`: edge values and a random one."""
    width = o.width
    out = []
    for v in (0, 1, -1, o.get(value) ^ 1, rng.getrandbits(width)):
        v &= (1 << width) - 1
        if isinstance(o, Composite) and o.signed and v >> (width - 1):
            v -= 1 << width
        out.append(v)
    return out


@pytest.mark.parametrize("unit", list(HEAD_READS))
def test_head_reads_are_what_the_machine_reads(unit):
    """For every head with known reads: bits of its unit outside the operands it reads never
    change what the machine does for it (32 draws), and each operand it reads changes it on some
    draw (up to 400)."""
    rng = random.Random(unit)
    view = VIEWS[unit]
    op_field = view.operands["op"]
    fields = [f for f in UNITS[unit].fields if f.id != op_field.id]
    unit_span = sum(f.span for f in fields)
    for head, declared in HEAD_READS[unit].items():
        wanted: set[str] = set()
        changed: set[str] = set()
        for draw in range(400):
            if draw >= 32 and changed == wanted:
                break
            values = {
                f.id: rng.choice((0, f.mask, rng.randrange(f.mask + 1))) for f in fields
            }
            if unit == "s3" and rng.randrange(2):
                values |= {"f232_233": 1, "f141_144": rng.randrange(16)}
            word = Word.from_fields({**values, op_field.id: head}, base=IDLE)
            reads = head_reads(unit, word.value)
            if reads is None:
                continue  # an S3 head whose U port take is open
            assert declared <= reads
            wanted |= reads
            span = op_field.span
            for k in reads:
                span |= view.operands[k].span
            outside = unit_span & ~span
            other = Word(word.value & ~outside | rng.getrandbits(320) & outside)
            regs = _registers(rng, unit, (word, other))
            base = _head_result(unit, regs, word)
            if draw < 32:
                assert _head_result(unit, regs, other) == base, (unit, hex(head))
            for k in reads - changed:
                o = view.operands[k]
                for v in _changes(rng, o, word.value):
                    if _head_result(unit, regs, Word(o.put(word.value, v))) != base:
                        changed.add(k)
                        break
        assert changed == wanted, (unit, hex(head), wanted - changed)


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


def test_two_s0_0x4f_under_m_into_port_272_run(m):
    s0 = w(f278_284=0x4F, f276=0, f272_273=2, f224_228=12)
    run(m, [s0, w(), s0])


def test_a_third_s0_0x4f_under_m_into_port_272_wedges(m):
    s0 = w(f278_284=0x4F, f276=0, f272_273=2, f224_228=12)
    with pytest.raises(UnknownSemantics, match="wedged"):
        run(m, [s0, w(), s0, w(), s0])


def test_s0_0x4f_under_m_into_port_270_runs(m):
    run(m, [w(f278_284=0x4F, f276=0, f270_271=2, f221_223=0)])


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


@pytest.mark.parametrize(
    "rr, r, want, tier",
    [
        (3, 0, 0x1111, Confidence.VERIFIED),
        (3, 0x80000000, 0x2222, Confidence.VERIFIED),
        (5, 0x10000, 0x2222, Confidence.VERIFIED),
        (1, 0, 0x1111, Confidence.INFERRED),
        (3, None, None, Confidence.VERIFIED),
    ],
)
def test_op_8_selects_ra_on_a_zero_r_register(m, rr, r, want, tier):
    m.g[10], m.g[11], m.r[rr] = Sv(0x1111), Sv(0x2222), Sv(r)
    run(m, [op_word(0x08, 10, 11, f120_122=rr, f272_273=1, f224_228=12)])
    assert m.g[12] == Sv(want, tier)


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


def test_a_given_decision_comes_first_and_caps_the_path_the_machine_decides_on():
    # 0 brf -> 7 (r11 = 5: not taken by the machine, taken as given), 7 br -> 12 (r12 = 1).
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    words = [control(1, 7, ra=11), *[inc(16)] * 4, inc(17), inc(17)]
    words += [control(2, 12, ra=12), *[inc(18)] * 4, halt]
    boot = Boot()
    m = Machine(boot)
    m.preset({"G": {30: 1, 11: 5, 12: 1, 16: 0, 17: 0, 18: 0}})
    flow = Executor(boot, datapath=m, branching=Given({0: True})).step(
        Job(Program(tuple(words)), ())
    )
    assert flow.order() == [0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12]
    given, decided = flow.steps[0], flow.steps[5]
    assert given.note.endswith("given by the caller") and given.confidence is H
    assert decided.note.endswith("r12 = 0x1") and decided.confidence is H
    assert flow.steps[-1].confidence is H  # the halt
    assert (m.g[16], m.g[17], m.g[18]) == (Sv(4, H), Sv(0), Sv(4, H))


def test_a_run_reads_what_the_run_before_on_its_boot_left():
    boot = Boot()
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    program = Program((inc(16), halt))
    boot.run(program, regs={"G": {30: 1, 16: 5}})
    assert boot.run(program).machine.g[16] == Sv(7)


HALT = Word.from_fields({"f313_319": 7}, base=IDLE)


def test_a_lenient_machine_forgets_what_a_store_at_an_unknown_address_may_write():
    store = op_word(0x13, 5, 9, f120_122=2)  # r5 unknown
    program = Program((store, inc(16), control(2, 7, ra=16), *[IDLE] * 4, HALT))
    regs = {"G": {30: 1, 16: 0, 9: 1}}
    with pytest.raises(UnknownSemantics, match="a local store at an unknown address"):
        Boot().run(program, regs=regs)
    boot = Boot()
    boot.local.write(LOCAL_BASE + 0x40, Bytes.of(b"\x11", boot.source("test")))
    run = boot.run(program, regs=regs, strict=False)
    lapsed, _, branch = run.flow.steps[:3]
    assert lapsed.unknown and lapsed.confidence is H
    assert lapsed.note == "op13: a local store at an unknown address"
    assert run.machine.lapses == [lapsed.note] and run.lapses == (lapsed.note,)
    assert run.estimate.us is None  # a lapsed run's time is not known
    assert boot.local.read(LOCAL_BASE + 0x40, 1).known == bytearray([0])
    # All the job leaves and decides after a lapse is a hypothesis.
    assert run.machine.g[16] == Sv(1, H) and branch.confidence is H
    assert boot.carry is None


@pytest.mark.parametrize("a", [1, 2, 8])
def test_a_wait_with_register_a_1_2_or_8_hands_nothing_inferred(a):
    stream = _Transfers()
    m = Machine(Boot(), stream=stream)
    m.execute(0, w(f308_312=0x1D, f110_114=a, f115_119=3), 0)
    assert stream.got == [] and not m.lapses and m.cap is I


@pytest.mark.parametrize(("head", "a"), [(0x1D, 31), (0x1D, 3), (0x1C, 1), (0x1C, 31)])
def test_a_transfer_with_a_register_a_not_measured_raises_or_forgets_every_memory(
    head, a
):
    word = w(f308_312=head, f110_114=a, f115_119=3)
    with pytest.raises(UnknownSemantics, match=f"f110_114={a:#x}"):
        Machine(Boot(), stream=_Transfers()).execute(0, word, 0)
    boot = Boot()
    boot.put(0x10000000, b"\x22", "dram")
    stream = _Transfers()
    m = Machine(boot, stream=stream)
    m.strict = False
    m.execute(0, word, 0)
    assert stream.got == [] and len(m.lapses) == 1
    assert m.lanes_lost and m.cap is H
    assert not any(boot.read(0x10000000, 1).known)


def test_a_lenient_transfer_without_a_stream_forgets_every_memory():
    boot = Boot()
    boot.put(0x10000000, b"\x22", "dram")
    boot.local.write(LOCAL_BASE, Bytes.of(b"\x11", boot.source("test")))
    m = Machine(boot)
    m.strict = False
    m.execute(0, w(f308_312=0x1C, f110_114=0, f115_119=3), 0)
    assert m.lapses == ["x1 0x1c: a transfer to the stream engine: no stream"]
    assert m.lanes_lost and m.cap is H
    assert not any(boot.local.read(LOCAL_BASE, 1).known)
    assert not any(boot.read(0x10000000, 1).known)


def test_a_run_that_does_not_halt_raises_and_leaves_no_carry():
    boot = Boot()
    boot.run(Program((Word.from_fields({"f313_319": 7}, base=IDLE),)))
    assert boot.carry is not None
    with pytest.raises(NoHalt, match="the step limit") as e:
        boot.run(Program((control(3, 0), *[IDLE] * 4)), limit=50)
    assert e.value.flow.stop is Stop.STEP_LIMIT
    assert boot.carry is None


def test_a_halt_after_the_stream_goes_off_script_leaves_the_end_open():
    """A key with bits outside its fields: whether the job ends is not known."""
    boot = Boot()
    boot.run(assemble([OP.halt(), *[NOP] * 4]))
    run = boot.run(assemble([*hand(0x234), OP.halt(), *[NOP] * 4]))
    assert run.flow.stop is Stop.OPEN_END and "bits outside its fields" in run.flow.why
    stop = next(s for s in run.flow.steps if s.op == HALT_OP)
    assert stop.confidence is H and "off script" in stop.note
    assert run.estimate.us is None and boot.carry is None


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
    job = Job(Program((halt, control(2, 0, ra=1))), ())
    with pytest.raises(UnknownSemantics, match="a control op behind a halt"):
        boot = Boot()
        Executor(boot, datapath=Machine(boot)).step(job)
    boot = Boot()
    boot.local.write(LOCAL_BASE, Bytes.of(b"\x11", boot.source("test")))
    m = Machine(boot)
    m.g[16] = Sv(0)
    flow = Executor(boot, strict=False, datapath=m).step(job)
    (step,) = flow.steps  # lenient: the job ends at the halt
    assert flow.stop is Stop.HALT and step.unknown and step.confidence is H
    assert step.note == "br: a control op behind a halt"
    # What the rest of the tail may write is forgotten.
    assert not any(boot.local.read(LOCAL_BASE, 1).known) and m.g[16] == Sv(0, H)


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


# Each control op taken to word 9: op 3, op 4 through g20, the loop end and br on g30 = 1, brf
# on g12 = 0.
TAKEN = [
    control(3, 9),
    control(4, f128_132=20),
    control(0, 9, ra=30),
    control(1, 9, ra=12),
    control(2, 9, ra=30),
]


@pytest.mark.parametrize("jump", TAKEN)
def test_a_halt_in_a_taken_jumps_first_slot_wins(jump):
    halt = Word.from_fields({"f313_319": 7}, base=IDLE)
    words = (
        jump,
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
    m = stepped(list(words), g16=0, g17=0, g20=9, g12=0)
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


@pytest.mark.parametrize("k", [2, 3, 4])
@pytest.mark.parametrize("jump", TAKEN)
def test_a_jump_due_among_the_words_behind_a_halt_redirects_them(jump, k):
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
    m = stepped(list(words), g16=0, g17=0, g20=9, g12=0)
    among_slots = 4 - k
    assert (m.g[16], m.g[17]) == (Sv(k - 1 + among_slots), Sv(3 - among_slots))


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


@pytest.mark.parametrize(
    ("x0_head", "tier"),
    [
        ({}, V),
        ({"f302_307": 0x13, "f105_109": 8, "f2_6": 0x1E, "f1": 0}, I),
        ({"f302_307": 0x16}, I),  # a head without a result
    ],
)
def test_nothing_on_m_beside_an_active_x0_head_keeps_x0s_port_at_inferred(m, x0_head, tier):
    m.lanes["L1"][1], m.lanes["L1"][2] = Vec(A), Vec(B)
    m.g[12] = Sv(0x0A5A)
    read = w(f276=0, f274_275=1, f272_273=2, f224_228=12, **x0_head)
    run(m, [w(**S1_AND), IDLE, read])
    assert m.g[12] == Sv(0x0A5A, tier)


@pytest.mark.parametrize(("s0", "tier"), [(0x0, V), (0x3, I)])
def test_nothing_on_m_beside_x0_0x13_and_s0_0x0_keeps_x0s_port(m, s0, tier):
    m.lanes["L0"][1], m.lanes["L0"][2] = Vec(A), Vec(B)
    m.g[12] = Sv(0x0A5A)
    x0_head = {"f302_307": 0x13, "f105_109": 8, "f2_6": 0x1E, "f1": 0}
    s0_head = {"f278_284": s0, "f37_40": 1, "f41_44": 2}
    run(m, [w(f276=0, f274_275=2, f272_273=2, f224_228=12, **x0_head, **s0_head)])
    assert m.g[12] == Sv(0x0A5A, tier)


SENTINEL = Sv(0x0A5A)
UNIT_HEADS = {"s0": "f278_284", "s1": "f285_291", "s2": "f292_295", "s3": "f296_301"}
OPERANDS = {
    "f37_40": 1, "f41_44": 2, "f45_48": 1, "f49_52": 2,
    "f53_56": 1, "f57_60": 2, "f61_64": 1, "f65_68": 2,
    "f84_87": 3, "f88_91": 4, "f96_99": 13,
}
"""Each unit's a and b at register 1 and 2 of its lane file; S1 0x54's, S2 0xe's and S3 0x24's
own-word results F1[3], F2[4], F3[13]."""


def heads(*pairs: tuple[str, int]) -> dict[str, int]:
    return {**OPERANDS, **{UNIT_HEADS[u]: h for u, h in pairs}}


def m_read(words: list[dict[str, int]], at: int, sel: int) -> Sv:
    """R[0] after `words` with M's strobe under `sel` in word `at` (a fresh machine)."""
    m = Machine(Boot())
    for file in ("L0", "L1", "L2", "L3"):
        m.lanes[file][1], m.lanes[file][2] = Vec(A), Vec(B)
    m.f["F1"][3], m.f["F2"][4], m.f["F3"][13] = Sv(0x20020), Sv(0x40040), Sv(0x80080)
    m.r[0] = SENTINEL
    words = [dict(x) for x in words] + [{} for _ in range(at + 1 - len(words))]
    words[at] |= {**R0_UNDER, "f274_275": sel}
    run(m, [w(**x) for x in words])
    return m.r[0]


@pytest.mark.parametrize(
    ("pairs", "at", "sel", "tier"),
    [
        ((("s0", 0x0), ("s1", 0x0)), 1, 3, V),  # lane-only heads of two units
        ((("s0", 0x0), ("s1", 0x0), ("s2", 0x8), ("s3", 0x19)), 0, 1, V),
        ((("s0", 0x0), ("s1", 0x0), ("s2", 0x8)), 0, 3, I),  # three: not measured
        ((("s2", 0x8), ("s3", 0x19)), 1, 2, I),  # S2 one word after its own head
        ((("s2", 0x8), ("s3", 0x19)), 1, 0, V),
        ((("s2", 0xE), ("s3", 0x19)), 0, 3, V),  # an own-word result beside a lane head
        ((("s2", 0xE), ("s3", 0x19)), 0, 0, I),  # under a third unit
        ((("s2", 0xE), ("s3", 0x19)), 1, 2, I),  # one word later
        ((("s2", 0xE), ("s3", 0x24)), 0, 1, V),  # two own-word results
        ((("s2", 0xE), ("s3", 0x24)), 1, 1, I),
        ((("s0", 0x32), ("s1", 0x45)), 1, 2, V),  # two held results
        ((("s0", 0x32), ("s1", 0x45)), 0, 2, I),
    ],
)
def test_nothing_on_m_beside_several_units_heads(pairs, at, sel, tier):
    assert m_read([heads(*pairs)], at, sel) == Sv(SENTINEL.value, tier)


@pytest.mark.parametrize(
    ("held", "beside", "tier"),
    [
        (("s0", 0x32), ("s2", 0xE), V),
        (("s1", 0x45), ("s3", 0x24), V),
        (("s0", 0x32), ("s1", 0x54), I),  # beside another own-word head
        (("s0", 0x22), ("s2", 0xE), I),  # another held head
    ],
)
def test_a_held_result_beside_another_units_own_word_result_is_on_m_alone(held, beside, tier):
    sel = int(held[0][1])
    alone = m_read([heads(held)], 1, sel)
    assert alone.value is not None and alone.tier == V
    assert m_read([heads(held), heads(beside)], 1, sel) == Sv(alone.value, tier)


@pytest.mark.parametrize(("s0", "tier"), [(0x32, V), (0x22, I)])
def test_two_held_results_in_one_word_each_under_its_unit(s0, tier):
    swapped = {"f45_48": 2, "f49_52": 1}  # S1's a < b on every lane: its mask is not zero
    alone, s1 = m_read([heads(("s0", s0))], 1, 0), m_read([heads(("s1", 0x45)) | swapped], 1, 1)
    assert alone.tier == s1.tier == V and alone.value | s1.value != alone.value
    assert m_read([heads(("s0", s0), ("s1", 0x45)) | swapped], 1, 0) == Sv(alone.value, tier)


EXTRACT_A = {"f292_295": 0xC, "f53_56": 1, "f57_60": 2, "f88_91": 3}
EXTRACT_B = {"f292_295": 0xC, "f53_56": 2, "f57_60": 2, "f88_91": 9}
PRODUCT = {"f292_295": 0x1, "f53_56": 1, "f57_60": 2}


def test_m_under_s2_after_consecutive_extracts_is_the_second_alone():
    alone = m_read([EXTRACT_B], 0, 2)
    assert alone.value is not None and alone.tier == V
    assert m_read([EXTRACT_A, EXTRACT_B], 1, 2) == alone
    # After a multiply the next extract on M is not measured.
    after = m_read([PRODUCT, EXTRACT_B], 1, 2)
    assert after.value is None and after.tier < V


@pytest.mark.parametrize("first", [EXTRACT_A, PRODUCT])
def test_nothing_on_m_under_s2_one_word_after_an_extract_or_a_multiply(first):
    assert m_read([first], 1, 2) == SENTINEL
    # Another unit's head in the next word: not measured.
    beside = m_read([first, heads(("s3", 0x19))], 1, 2)
    assert beside.value is None and beside.tier < V


PENDING = "a jump pending behind a halt"


@pytest.mark.parametrize(
    ("before", "behind", "match"),
    [
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


# Measured on the camera: a at t 0; b at 1, 0xfe85, 0x10000 and 0xffffffff (a zero low half is not enough).
@pytest.mark.parametrize(
    ("t", "want"), [(0, A), (1, B), (0xFE85, B), (0x10000, B), (0xFFFFFFFF, B), (None, None)]
)
def test_s2_0x8_moves_a_where_t_is_0_else_b(t, want):
    m = Machine(Boot())
    m.lanes["L2"][2], m.lanes["L2"][4] = Vec(A), Vec(B)
    m.f["F2"][4] = Sv(t)
    run(m, [w(f292_295=0x8, f53_56=2, f57_60=4, f88_91=4, f238_239=1, f151_154=9)])
    got = m.lanes["L0"][9]
    if want is None:
        assert set(got.lanes) == {None}
        return
    assert got == Vec(tuple(want), V)


# Measured on the camera: a at r 0; b at 9 and 0x10000.
@pytest.mark.parametrize(("r", "want"), [(0, A), (9, B), (0x10000, B), (None, None)])
def test_s0_0x41_moves_a_where_r_is_0_else_b(r, want):
    m = Machine(Boot())
    m.lanes["L0"][0], m.lanes["L0"][1] = Vec(A), Vec(B)
    m.f["F0"][5] = Sv(r)
    run(m, [w(f278_284=0x41, f37_40=0, f41_44=1, f76_79=5, f236_237=2, f147_150=9)])
    got = m.lanes["L0"][9]
    if want is None:
        assert set(got.lanes) == {None}
        return
    assert got == Vec(tuple(want), V)


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


@pytest.mark.parametrize("gap", [1, 2, 4, 5])
def test_two_divides_in_flight_land_as_alone(m, gap):
    """Two X1 0x1e heads `gap` words apart; G[5] and G[6] take the two quotients."""
    m.b[1], m.b[2], m.b[3] = Sv(1000), Sv(7), Sv(10)
    first = {"f308_312": 0x1E, "f110_114": 1, "f115_119": 2}
    second = {"f308_312": 0x1E, "f110_114": 1, "f115_119": 3}
    words = [w(**first)] + [IDLE] * (gap + 4)
    words[gap] = w(**second)
    words[4] = w(f272_273=0, f224_228=5, **(second if gap == 4 else {}))
    words[gap + 4] = w(f272_273=0, f224_228=6)
    run(m, words)
    assert m.g[5] == Sv(142, V) and m.g[6] == Sv(100, V)


@pytest.mark.parametrize(
    ("gaps", "tiers"),
    [
        ((1, 1, 1), (V, V, V, V)),
        ((3,), (V, V)),
        ((1, 1, 1, 1), (I, I, I, I, I)),
        ((2, 1), (I, I, I)),
        ((1, 2), (I, I, I)),
        ((3, 3), (V, I, I)),
        ((4, 4), (V, I, I)),
    ],
)
def test_divides_in_flight_land_as_alone_in_the_measured_chains_only(m, gaps, tiers):
    """Each quotient into its own G register: a chain off the measured ones caps every quotient
    still due."""
    m.b[1], m.b[2] = Sv(1000), Sv(7)
    head = {"f308_312": 0x1E, "f110_114": 1, "f115_119": 2}
    at = [0]
    for g in gaps:
        at.append(at[-1] + g)
    words = [{} for _ in range(at[-1] + 5)]
    for k, t in enumerate(at):
        words[t] |= head
        words[t + 4] |= {"f272_273": 0, "f224_228": 5 + k}
    run(m, [w(**f) for f in words])
    assert [m.g[5 + k] for k in range(len(at))] == [Sv(142, t) for t in tiers]


K = 0x1C3
X1_K = {"f115_119": K >> 8, "f9_14": K >> 2 & 63, "f7_8": K & 3}


@pytest.mark.parametrize(
    ("beside", "want"),
    [
        ({}, Sv(142, V)),
        ({"f308_312": 0x1E, "f110_114": 1, "f115_119": 2}, Sv(142, V)),  # another divide
        ({"f308_312": 0x13, **X1_K}, Sv(142 | K, V)),  # zK
        ({"f308_312": 0x03, "f110_114": 4, **X1_K}, Sv(142 | 0x100 + K, I)),  # B[4] + zK
        ({"f308_312": 0x03, "f110_114": 5, **X1_K}, Sv(None, V)),  # B[5] unknown
        ({"f308_312": 0x1C, "f110_114": 0, "f115_119": 4}, Sv(142, I)),  # a transfer
    ],
)
def test_a_quotient_beside_an_x1_head(beside, want):
    """G[5] := X1's bus four words after X1 0x1e, beside `beside`'s X1 head."""
    m = Machine(Boot(), stream=_Transfers())
    m.b[1], m.b[2], m.b[4] = Sv(1000), Sv(7), Sv(0x100)
    words = [w(f308_312=0x1E, f110_114=1, f115_119=2)] + [IDLE] * 3
    words.append(w(f272_273=0, f224_228=5, **beside))
    run(m, words)
    assert m.g[5] == want


def l0_after(words: list[dict[str, int]]) -> Vec:
    """L0[8] after `words`, ending in the one whose ports name it."""
    m = Machine(Boot())
    for file in ("L0", "L1", "L3"):
        m.lanes[file][1], m.lanes[file][2] = Vec(A), Vec(B)
    m.lanes["L0"][8] = Vec((0x0A5A,) * 32)
    m.f["F0"][5] = Sv(9)
    run(m, [w(**f) for f in words])
    return m.lanes["L0"][8]


S0_PORT = {"f236_237": 2, "f147_150": 8}
"""L0's first port on register 8: S0's result."""
S3_PORT = {"f238_239": 0, "f151_154": 8}
"""L0's second port on register 8: S3's result."""
S0_MOVE = {"f278_284": 0x41, "f37_40": 1, "f41_44": 2, "f76_79": 5}  # b, at lag 0
S3_COPY = {"f296_301": 0x19, "f61_64": 2}  # lag 0


@pytest.mark.parametrize(
    ("before", "heads", "second", "tier"),
    [
        ({}, S0_MOVE | S3_COPY, S3_PORT, V),  # the measured form
        ({}, S0_MOVE | {"f285_291": 0x03, "f45_48": 1, "f49_52": 2}, {"f238_239": 2, "f151_154": 8}, I),
        ({"f278_284": 0x0, "f37_40": 1, "f41_44": 2}, S3_COPY, S3_PORT, I),  # S0 0x0 at lag 1
        ({"f278_284": 0x0, "f37_40": 1, "f41_44": 2}, S0_MOVE | S3_COPY, S3_PORT, I),  # two S0 results
    ],
)
def test_two_ports_on_one_lane_register_the_first_wins(before, heads, second, tier):
    first = l0_after([before, heads | S0_PORT])
    other = l0_after([before, heads | second])
    assert first.tier == V and other.lanes != first.lanes
    assert l0_after([before, heads | S0_PORT | second]) == Vec(first.lanes, tier)


def test_a_second_port_beside_a_first_that_takes_nothing_is_inferred():
    alone = l0_after([{}, S3_COPY | S3_PORT])
    assert alone.tier == V
    assert l0_after([{}, S3_COPY | S0_PORT | S3_PORT]) == Vec(alone.lanes, I)


def l1_high(beside: dict[str, int]) -> Vec:
    """L1[8] from S2's high port one word after S2 0x2, beside `beside`'s S2 head."""
    m = Machine(Boot())
    m.lanes["L2"][1], m.lanes["L2"][2] = Vec(A), Vec(tuple(0x8000 + x for x in B))
    m.lanes["L1"][8] = Vec((0x0A5A,) * 32)
    product = w(f292_295=0x2, f53_56=1, f57_60=2)
    run(m, [product, w(**beside, f53_56=1, f57_60=2, f240_241=1, f155_158=8)])
    return m.lanes["L1"][8]


@pytest.mark.parametrize("head", [0x8, 0xD, 0x9])
def test_a_products_high_half_beside_a_lag_0_s2_head_is_inferred(head):
    alone = l1_high({})
    assert None not in alone.lanes and alone.tier == V
    assert l1_high({"f292_295": head}) == Vec(alone.lanes, I)


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


@cache
def stock(item: int) -> Program:
    return stock_programs(stock_image("em10ii"))[item]


def lenient(boot: Boot, job: Job, decisions: dict) -> tuple[Flow, Machine]:
    m = Machine(boot, None, Engine())
    flow = Executor(boot, strict=False, datapath=m, branching=Given(decisions)).step(
        job
    )
    return flow, m


def test_program_15_without_a_request_reaches_its_halt_on_a_lenient_machine():
    job = Job(stock(15), ())
    with pytest.raises(UnknownSemantics, match="word 68: .*a lane store at an unknown"):
        boot = Boot()
        Executor(boot, datapath=Machine(boot, None, Engine())).step(job)
    given = dict.fromkeys((109, 212, 280, 315, 379, 456, 562, 625), False)
    flow, m = lenient(Boot(), job, given)
    assert flow.stop is Stop.HALT and flow.order()[-4:] == [5, 6, 7, 8]
    assert flow.steps[[s.index for s in flow.steps].index(68)].unknown
    assert sum(s.unknown for s in flow.steps) == len(m.lapses) > 0


def test_program_13_halts_strict_its_waits_with_register_a_nonzero_handing_nothing():
    src, table, lines = 0x10000000, 0x10200000, 16
    job = commands.resample(stock(13), src, 0x10100000, table, 64, 64, lines)

    def boot() -> Boot:
        b = Boot()
        b.put(src, bytes(3 * 64 * lines), "src")
        identity = b"".join(struct.pack("<iI", i << 16, 0x10000) for i in range(lines))
        b.put(table, identity, "table")
        return b

    # Its 0x1d in words 210 (a = 2) and 312 (a = 1) hand nothing: the others balance its commands.
    b = boot()
    m = Machine(b, None, Engine())
    flow = Executor(b, datapath=m).step(job)
    assert flow.stop is Stop.HALT and not m.lapses
    assert m.cap is I


SRC, DST, LENGTH = 0xA0FCA800, 0xA1113600, 0x400
M32 = 0xFFFFFFFF
TAKE = Word.from_fields({"f272_273": 1, "f224_228": 12}, base=IDLE)
"""G[12] takes the ALU bus."""


def span(*runs: tuple[int, int]) -> list[int]:
    return [i for a, b in runs for i in range(a, b + 1)]


def copy_job(program: Program | None = None) -> Job:
    return commands.copy(program or stock(16), SRC, DST, LENGTH)


# Call 0 -> 10 with slots 1..4; 27 falls through (slots 28..31); 45 jumps over 50..57 to 58
# after its slots 46..49; the return 79 goes back to 5 after its slots 80..83; 5 halts, and the
# three words behind it run.
COPY_ORDER = span((0, 4), (10, 49), (58, 83), (5, 8))


def test_the_copy_flows_through_its_call_slots_to_the_halt_and_its_tail():
    boot = Boot()
    flow = Executor(boot, datapath=Machine(boot, None, Engine())).step(copy_job())
    assert flow.stop is Stop.HALT and flow.order() == COPY_ORDER
    by_index = {s.index: s for s in flow.steps}
    assert [by_index[i].taken for i in (0, 27, 45, 79)] == [True, False, True, True]
    assert {by_index[i].confidence for i in (0, 27, 45, 79)} == {V}
    slots = [s.index for s in flow.steps if s.slot]
    assert slots == span((1, 4), (28, 31), (46, 49), (80, 83))


def test_a_halt_inside_a_called_routine_ends_the_job_after_its_tail():
    words = list(stock(16).words)
    words[11] = words[5]
    boot = Boot()
    flow = Executor(boot, datapath=Machine(boot, None, Engine())).step(
        copy_job(Program(tuple(words)))
    )
    assert flow.stop is Stop.HALT and flow.order() == span((0, 4), (10, 14))


def test_guard_45_not_taken_switches_the_copy_into_its_loop():
    # No stream engine: the loop's lane loads on one take minutes (lenient: the transfers lapse).
    once = Given({45: False, 53: [True, False]})
    boot = Boot()
    flow = Executor(boot, strict=False, datapath=Machine(boot), branching=once).step(
        copy_job()
    )
    assert flow.stop is Stop.HALT
    assert flow.order() == span((0, 4), (10, 57), (50, 83), (5, 8))


def test_an_undecided_branch_stops_or_raises():
    job = Job(stock(16), ())  # no request: r7 is not known at 27
    boot = Boot()
    flow = Executor(boot, strict=False, datapath=Machine(boot, None, Engine())).step(
        job
    )
    assert flow.stop is Stop.UNDECIDED and flow.order()[-1] == 27
    with pytest.raises(UnknownSemantics, match="word 27: .*r7 undecided: r7 unknown"):
        boot = Boot()
        Executor(boot, datapath=Machine(boot, None, Engine())).step(job)


def test_a_program_without_a_halt_runs_past_its_end():
    boot = Boot()
    executor = Executor(boot, strict=False, datapath=Machine(boot))
    with pytest.raises(UnknownSemantics, match="word 2: .*past the program end"):
        executor.step(Job(Program((IDLE, IDLE)), ()))


def requested() -> Machine:
    """A machine whose local memory holds a copy's request: src, dst and len at bytes 0, 4, 8."""
    boot = Boot()
    boot.setup(copy_job(Program((HALT,))))
    return Machine(boot)


@pytest.mark.parametrize(
    "code, imm, base, value",
    [
        # Local word 0 holds src: 0x1a reads it whole, 0x16 its halfwords (signed), 0x10 its bytes.
        (0x1A, 0, 0, SRC),
        (0x16, 1, 0, 0xFFFF0000 | SRC >> 16),
        (0x10, 1, 0, SRC >> 8 & 0xFF),
        (0x16, 2, 0, DST & 0xFFFF),
        (0x10, -1, 0, None),  # byte 0x1fff: the address wraps at 8 KiB
        # A base of byte 4 (dst) moves both by 4 bytes.
        (0x16, 1, 4, 0xFFFF0000 | DST >> 16),
        (0x10, 1, 4, DST >> 8 & 0xFF),
        # The offset is width x the signed imm13; 0x14 loads halfwords unsigned.
        (0x1A, -1, 8, DST),
        (0x16, -2, 8, DST & 0xFFFF),
        (0x14, -1, 8, DST >> 16),
        (0x14, 1, 8, 0),
        (0x16, -0x800, 0x1000, 0xFFFF0000 | SRC & 0xFFFF),
        *((0x1A, 0, base, DST) for base in (0x2004, 0x10004, -0x1FFC, 0x9D000004)),
    ],
)
def test_a_local_load_reads_at_ra_plus_width_times_imm13(code, imm, base, value):
    m = requested()
    m.g[4] = Sv(base & M32)
    run(m, [op_word(code, 4, imm=imm), IDLE, IDLE, TAKE])
    assert m.g[12].value == value
    assert m.g[4] == Sv(base & M32)  # ra is the base, not written


@pytest.mark.parametrize("code, base", [(0x1A, 2), (0x16, 1), (0x18, 6)])
def test_an_unaligned_local_access_is_not_covered(code, base):
    m = requested()
    m.g[4] = Sv(base)
    with pytest.raises(UnknownSemantics, match="an unaligned local access"):
        run(m, [op_word(code, 4, imm=0)])
    m.strict = False
    run(m, [op_word(code, 4, imm=0), IDLE, IDLE, TAKE])
    assert m.lapses == [f"op{code:02x}: an unaligned local access at {base:#x}"]
    if code == 0x18:  # a store: local memory is forgotten
        assert not any(m.boot.local.read(LOCAL_BASE, 12).known)
    else:  # a load: its datum is unknown
        assert m.g[12].value is None


@pytest.mark.parametrize(
    "code, operands, a, b, value",
    [
        # B, C, D, F: ra and the immediate as the op's format extends it; rb unused.
        (0x0B, Operands(ra=2, imm=-13), 0x1010, None, -13),
        (0x0A, Operands(ra=2, imm=0x1234), 0x10, None, 0x12340010),
        (0x37, Operands(ra=2, imm=0x7001), 0x10, None, 0x7011),
        (0x38, Operands(ra=2, imm=-0x20), 0xA1113610, None, 0xA1113600),  # imm16 signed
        (0x38, Operands(ra=2, imm=0xF), 0x13, None, 3),
        (0x3E, Operands(ra=2, rb=3, imm=4), -0x100, 0x1234, -0x10),
        (0x3F, Operands(ra=2, rb=3, imm=4), -0x100, 0x1234, 0x0FFFFFF0),
        (0x40, Operands(ra=2, rb=3, imm=4), 0x10, 0x1234, 0x100),
        (0x1F, Operands(ra=2, imm=0xE010), 0x1010, None, 0xF020),  # zero-extended
        (0x25, Operands(ra=2, imm=0x7F0), 0x1010, None, 0x7F0),
        (0x25, Operands(ra=2, imm=-1), 0x10, None, -1),  # sign-extended
        (0x23, Operands(ra=2, imm=0xFFFF), 0x10, None, 1),
        (0x21, Operands(ra=2, imm=-1), 0x10, None, 0),
        (0x44, Operands(ra=2, imm=0x408), 7, None, 0),  # 7 + 1 wraps at L = 8
        (0x44, Operands(ra=2, imm=0x408), 2, None, 3),
        # E, on (r2, r3).
        (0x28, Operands(ra=2, rb=3), -0x10, 0x1010, 0),  # unsigned <=
        (0x2A, Operands(ra=2, rb=3), -0x10, 0x1010, 1),  # signed <=
        (0x31, Operands(ra=2, rb=3), -1, 0xFFFFFFFF, 1),
        (0x2E, Operands(ra=2, rb=3), 0xFFFFFFF0, 0x10, 0x10),
        (0x3D, Operands(ra=2, rb=3), 0x400, 0, -0x401),
        (0x32, Operands(ra=2, rb=3), 0, -5, 5),
        (0x2F, Operands(ra=2, rb=3), -0x1000, 0x1010, -0x1000),
        (0x2E, Operands(ra=2, rb=3), -0x1000, 0x1010, 0x1010),
        (0x2C, Operands(ra=2, rb=3), -0x1000, 0x1010, -0x1000),  # unsigned
        (0x41, Operands(ra=2, rb=3), 0x1010, 0x22, 0x1010 >> 2),  # rb & 31
        (0x41, Operands(ra=2, rb=3), -0x1400, 1, -0xA00),  # arithmetic
        (0x42, Operands(ra=2, rb=3), -0x10, 4, 0x0FFFFFFF),  # logical
        (0x43, Operands(ra=2, rb=3), 0x10, 0x21, 0x20),
        # The seven values unused in stock code; imm16 zero-extended on both sides of ra.
        (0x22, Operands(ra=2, imm=0x0B00), 0x24681234, None, 0),
        (0x22, Operands(ra=2, imm=0x0B00), 0xB00, None, 1),  # equal
        (0x22, Operands(ra=2, imm=0x8000), 0xFFFF8000, None, 0),  # not sign-extended
        (0x26, Operands(ra=2, imm=0x8E12), 0xDB97EDCB, None, 0xDB97EDCB),
        (0x26, Operands(ra=2, imm=0x8E12), 0x10, None, 0x8E12),
        (0x27, Operands(ra=2, imm=0xEFFF), 0x24681234, None, 0xEFFF),
        (0x27, Operands(ra=2, imm=0xFFFF), 0x8000, None, 0x8000),
        (0x2D, Operands(ra=2, rb=3), 0xFFFFFFF3, 0x1234, 0x1234),  # unsigned
        (0x39, Operands(ra=2, rb=3), 0x24681234, 0x1234, 0x24680000),
        (0x3B, Operands(ra=2, rb=3), 0xFFFFFFF3, 0x1234, 0xFFFFEDC3),
        (0x45, Operands(ra=2, rb=3), 0x24681234, 0x210, 0x24681235 - 0x210),
        (0x45, Operands(ra=2, rb=3), 5, 0x210, 6),
    ],
)
def test_measured_op_rules(m, code, operands, a, b, value):
    m.g[2] = Sv(a & M32)
    if b is not None:
        m.g[3] = Sv(b & M32)
    word = op_word(code, operands.ra, operands.rb, operands.imm, f272_273=1, f224_228=9)
    run(m, [word])
    assert m.g[9] == Sv(value & M32)


@pytest.mark.parametrize(
    ("imm", "value", "tier"),
    [(0xC80, 0x382, V), (0x100, 0x302, I), (0x1F0, 0x212, I)],
)
def test_the_ring_length_takes_bit_8(m, imm, value, tier):
    m.g[2] = Sv(0x401)
    run(m, [op_word(0x44, 2, None, imm, f272_273=1, f224_228=9)])
    assert m.g[9] == Sv(value, tier)


@pytest.mark.parametrize("imm", [0x2F0, 0x10F0])
def test_a_ring_length_with_bit_9_or_12_lapses(imm):
    word = op_word(0x44, 2, None, imm, f272_273=1, f224_228=9)
    strict = Machine(Boot())
    strict.g[2] = Sv(0x401)
    with pytest.raises(UnknownSemantics, match="ring length"):
        run(strict, [word])
    lenient = Machine(Boot())
    lenient.strict = False
    lenient.g[2] = Sv(0x401)
    run(lenient, [word])
    assert lenient.g[9].value is None and lenient.lapses


def test_ops_chain_through_g_until_an_op_without_semantics():
    """A mask into r2, a shift of it in the next word; an op without semantics into r2 lapses
    (lenient), and the shift of r2 after it is unknown."""
    words = (
        op_word(0x38, 2, imm=-0x10, f272_273=1, f224_228=2),
        op_word(0x3F, 2, 2, 2, f272_273=1, f224_228=7),
        Word.from_fields({"f313_319": 0x4A, "f272_273": 1, "f224_228": 2}, base=IDLE),
        op_word(0x3F, 2, 2, 1, f272_273=1, f224_228=8),
        HALT,
    )
    job = Job(Program(words), ())
    boot = Boot()
    boot.local.write(LOCAL_BASE, Bytes.of(b"\x11", boot.source("test")))
    m = Machine(boot)
    m.g[2] = Sv(0x40F)
    flow = Executor(boot, strict=False, datapath=m).step(job)
    assert m.g[7] == Sv(0x100)
    assert not any(boot.local.read(LOCAL_BASE, 1).known)  # the op slot may store there
    assert flow.steps[2].unknown and flow.steps[2].note == "op4a: no semantics"
    assert m.g[2] == Sv(None, H) and m.g[8] == Sv(None, H)
    with pytest.raises(
        UnknownSemantics, match="word 2: no semantics for f313_319=0x4a"
    ):
        boot = Boot()
        Executor(boot, datapath=Machine(boot)).step(job)


def test_a_multiply_writes_nothing_in_its_own_word(m):
    m.g[2], m.g[3], m.g[4] = Sv(-13 & M32), Sv(7), Sv(0x5555)
    run(m, [op_word(0x49, 2, 3, f272_273=1, f224_228=4), TAKE])
    assert m.g[4] == Sv(0x5555) and m.g[12] == Sv(-91 & M32)
