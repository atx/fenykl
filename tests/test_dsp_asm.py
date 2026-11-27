import random
import warnings
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import stock_image

from fenykl.dsp import asm
from fenykl.dsp.asm import (
    F1,
    F3,
    L0,
    L1,
    L2,
    L3,
    LOAD,
    NOP,
    OP,
    S1,
    S2,
    S2_HI,
    S3,
    VIEWS,
    X0,
    X1,
    B,
    Bundle,
    G,
    Label,
    M,
    O,
    Raw,
    U,
    b_add,
    bundle,
    hand,
    lane_block,
    li,
    raw,
    stream_load,
    stream_store,
    to,
    wait,
)
from fenykl.dsp.disasm import listing, source
from fenykl.dsp.fields import FIELDS, OP_FIELD, TARGET, Confidence
from fenykl.dsp.program import (
    AssemblyError,
    HypothesisFieldWarning,
    Program,
    assemble,
    call_return_errors,
    labels,
    stock_programs,
)
from fenykl.dsp.semantics import HEAD_READS
from fenykl.dsp.units import UNITS
from fenykl.dsp.word import IDLE, Word


def test_bundles_rebuild_program_16():
    # The DSL rebuilds the copy from bundles read off the firmware's program: no stock word is
    # restated in the code. No field it sets is a hypothesis any more.
    stock = stock_programs(stock_image("em10ii"))[16]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        built = assemble(Bundle.of(w) for w in stock.words)
    assert len(built) == len(stock)
    for index, (a, b) in enumerate(zip(built.words, stock.words)):
        assert a == b, f"word {index}: {a.changed(b)}"


def test_every_stock_word_decodes_to_its_bundle_exactly():
    for item, program in stock_programs(stock_image("em10ii")).items():
        for index, word in enumerate(program.words):
            assert Bundle.of(word).word() == word, (item, index)


def test_any_word_decodes_exactly():
    rng = random.Random(0)
    for _ in range(500):
        word = Word(rng.getrandbits(320))
        assert Bundle.of(word).word() == word
        # Near idle: a few fields at random values, so heads, ports and M decode as parts.
        near = {f.id: rng.randrange(f.mask + 1) for f in rng.sample(FIELDS, 6)}
        word = Word.from_fields(near)
        assert Bundle.of(word).word() == word, near
        # A head whose reads are known among random fields of its unit: the bits it does not
        # read, also those inside a field it reads in part, go to a raw part.
        unit = rng.choice(list(HEAD_READS))
        values = {f.id: rng.randrange(f.mask + 1) for f in UNITS[unit].fields}
        values[VIEWS[unit].operands["op"].id] = rng.choice(list(HEAD_READS[unit]))
        word = Word.from_fields(values)
        assert Bundle.of(word).word() == word, values


def test_a_kernel_word_decodes_to_its_instructions():
    word = (
        S1.xor_imm(L1[4], imm=0x20, a=L1[4], r=F1[4])
        | to(O[1], S1)
        | X0.store(asm.A[1], k=0, o=O[1])
    ).word()
    # xor_imm reads neither a nor r: a raw part holds them, r's bits beside imm's included.
    decoded = Bundle.of(word)
    assert repr(decoded) == (
        "S1.xor_imm(L1[4], 0x20) | X0.store(A[1], 0, O[1]) | to(O[1], S1) | "
        "raw(s1a=0x4, s1r=0x4)"
    )
    assert decoded.word() == word
    # A field no part expresses, here a register held under an idle head, stays raw.
    held = (OP(0x0B, ra=G[31], imm=5) | raw(xreg=3)).word()
    parts = Bundle.of(held).parts
    assert repr(parts[0]) == "OP.li(5)" and parts[-1] == Raw({"f115_119": 3})


def test_names_and_codes_build_the_same_instruction():
    named = S1.xor_imm(L1[4], imm=0x20)
    assert named.word() == S1(0x04, b=L1[4], imm=0x20).word()
    assert set(named.fields) == {"f0", "f49_52", "f80_83", "f84_87", "f285_291"}
    reg = S3.ins_reg(L3[1], F3[2], F3[3])
    assert reg.word()["s3idx"] == 2 and Bundle.of(reg.word()).parts == (reg,)
    # The lane number spans idx and bit 2 of u: lane 21 is 5 + 16, lane 5 leaves the bit clear.
    for lane in (5, 21):
        word = S3.ins(L3[1], F3[3], lane).word()
        assert (word["s3idx"], word["s3u"] >> 2 & 1) == (lane & 15, lane >> 4)
        assert Bundle.of(word).parts == (S3.ins(L3[1], F3[3], lane),)


@pytest.mark.parametrize(
    "build, error",
    [
        (lambda: S1.xor(L0[1], L1[2]), "a register of L1"),
        (lambda: S1.xor_imm(4, imm=1), "a register of L1"),
        (lambda: X0.load(asm.A[1], k=G[1]), "an integer"),
        (lambda: L1[16], "0..15"),
        (lambda: S1.nope(L1[1]), "no instruction"),
        (lambda: S1.xor(L1[1], L1[2], L1[3]), "2 positional"),
        (lambda: S1(0x04, s=F1[1]), "no operand 's'"),
        (lambda: OP.li(5, rr=asm.R[1]), "part of its format"),
        (lambda: OP.jump(), "target None"),
        (lambda: OP.li(), "takes an immediate"),
        (lambda: S3.ins_reg(L3[1], 2, F3[3]), "a register of F3"),
        (lambda: S3.ins(L3[1], F3[3], idx=F3[2]), "an integer"),
        (lambda: S1.xor(L1[1], a=L1[2]), "a given twice"),
        (lambda: to(S1, O[1]), r"to\(register, source\)"),
        (lambda: M(X0), "M puts out S0..S3"),
    ],
)
def test_operands_are_typed_and_checked(build, error):
    with pytest.raises(
        (TypeError, ValueError, IndexError, AttributeError), match=error
    ):
        build()


@pytest.mark.parametrize(
    "parts",
    [
        (S1.mov(L1[1]), S1.mov(L1[1])),
        (raw(xreg=3), X1.hand(B[3])),
        (to(L1[1], LOAD), to(L1[2], LOAD)),
        (OP.jump("routine"), raw(imm=0)),
    ],
)
def test_a_field_set_by_two_parts_raises_even_with_equal_values(parts):
    with pytest.raises(ValueError, match="set by"):
        Bundle(parts)


def test_a_raw_part_completes_a_field_an_instruction_sets_in_part():
    """xor_imm's imm holds bits 2..3 of r's field: a raw r may set the other two, agreeing."""
    instr = S1.xor_imm(L1[4], imm=0x20)
    assert (instr | raw(s1r=0x4)).word()["s1r"] == 0x4
    assert (raw(s1r=0x4) | instr).word()["s1r"] == 0x4
    with pytest.raises(ValueError, match="set by"):
        instr | raw(s1r=0x0)


@pytest.mark.parametrize(
    "order",
    [(0, 1, 2), (1, 0, 2), (1, 2, 0)],
    ids=["instr-first", "between", "instr-last"],
)
@pytest.mark.parametrize("second", [0x4, 0x5])
def test_a_second_raw_on_a_completed_field_raises_in_any_order(order, second):
    parts = (S1.xor_imm(L1[4], imm=0x20), raw(s1r=0x4), raw(s1r=second))
    with pytest.raises(ValueError, match="set by"):
        Bundle(tuple(parts[i] for i in order))


def test_a_port_is_the_one_of_its_file_that_takes_the_source():
    assert to(L1[4], LOAD).fields == {"f240_241": 0, "f155_158": 4}
    assert to(L1[4], S1).fields == {"f242_243": 2, "f159_162": 4}
    assert to(L2[1], S2).fields == {"f246_247": 1, "f167_170": 1}
    assert to(L0[1], S2_HI).fields == {"f236_237": 1, "f147_150": 1}
    assert to(O[2], S2).fields == {"f252_254": 2, "f179_181": 2}
    assert to(U[3], S3).fields == {"f232_233": 1, "f141_144": 3}
    assert to(G[9], OP).fields == {"f272_273": 1, "f224_228": 9}
    with pytest.raises(ValueError, match="no port of L1 writes X0"):
        to(L1[4], X0)


def test_an_instruction_carries_its_weakest_tier():
    assert S2.mul(L2[1], L2[2]).tier is Confidence.VERIFIED
    assert S1.movx(L1[1]).tier is Confidence.HYPOTHESIS
    # S1's head field is inferred: not every value has a verified row.
    assert S1.xor(L1[1], L1[2]).tier is Confidence.INFERRED
    assert (S2.mul(L2[1], L2[2]) | raw(f35_36=0)).tier is Confidence.HYPOTHESIS


def test_a_bundle_takes_the_weakest_tier_of_its_ports_and_m():
    port, strobe = to(O[1], S2), M(S2)
    word = S2.mul(L2[1], L2[2]) | port | strobe
    assert port.tier is strobe.tier is word.tier is Confidence.VERIFIED
    # A port whose source row were inferred: every port's row is verified today.
    text, _ = port.spec.sources[port.value]
    spec = replace(port.spec, sources={port.value: (text, Confidence.INFERRED)})
    weak = replace(port, spec=spec)
    assert weak.tier is Confidence.INFERRED
    assert (S2.mul(L2[1], L2[2]) | weak | strobe).tier is Confidence.INFERRED


def test_a_bundle_names_its_target_by_label():
    program = assemble(
        [OP.jump("r") | X1.hand(B[3]), *[NOP] * 4, Label("r") | OP.halt()]
    )
    assert program.words[0].target == 5 and program.words[0]["xreg"] == 3
    assert program.words[5] == OP.halt().word()
    with pytest.raises(AssemblyError, match="'nowhere' is not defined"):
        assemble([OP.bnz(G[1], "nowhere")])


def test_a_label_alone_is_an_idle_word():
    program = assemble([NOP, Label("top"), OP.jump("top")])
    assert program.words[:2] == (IDLE, IDLE) and program.words[2].target == 1
    with pytest.raises(ValueError, match="two labels on one word"):
        Label("a") | OP.halt() | Label("b")


def test_two_ports_on_one_register_raise():
    # L1 has two ports, one taking the load, one S1's result.
    with pytest.raises(ValueError, match=r"two ports write L1\[4\]"):
        to(L1[4], LOAD) | to(L1[4], S1)
    to(L1[4], LOAD) | to(L1[5], S1)
    # Such a word still decodes exactly, its second port raw.
    word = (to(L1[4], LOAD) | raw(f242_243=2, f159_162=4)).word()
    parts = Bundle.of(word).parts
    assert parts[0] == to(L1[4], LOAD) and isinstance(parts[1], Raw)
    assert Bundle.of(word).word() == word


@pytest.mark.parametrize(
    "build",
    [
        lambda: IDLE | X0.load(asm.A[1], k=0),
        lambda: X0.load(asm.A[1], k=0) | IDLE,
        lambda: NOP | IDLE,
        lambda: assemble([IDLE]),
    ],
)
def test_a_raw_word_is_no_part(build):
    with pytest.raises(TypeError, match=r"raw\(word\).*NOP"):
        build()


def test_raw_takes_a_word():
    word = Word.from_fields({"xreg": 3, "ra": 2})
    assert raw(word) == raw(xreg=3, ra=2)
    assert (raw(IDLE) | X1.hand(B[3])).word() == X1.hand(B[3]).word()


def test_hand_takes_registers_as_they_are_and_integers_through_alternating_scratch():
    b14, b15 = B[14], B[15]
    assert hand(1, B[3], 2, 3) == [
        li(b14, 1),
        X1.hand(b14),
        X1.hand(B[3]),
        li(b15, 2),
        X1.hand(b15),
        li(b14, 3),
        X1.hand(b14),
    ]


@pytest.mark.parametrize("nbytes", [0, 48])
def test_a_lane_block_is_whole_vectors(nbytes):
    with pytest.raises(ValueError, match="whole vectors"):
        lane_block(0, nbytes)


@pytest.mark.parametrize(
    ("build", "error", "match"),
    [
        (lambda: lane_block(4, 32), ValueError, "channel is 0..3, not 4"),
        (lambda: stream_load(-1, B[3], 0), ValueError, "channel is 0..3, not -1"),
        (lambda: stream_load(0, B[3], -1), ValueError, "vector is 0..511"),
        (lambda: stream_store(0, 0x200, B[3]), ValueError, "vector is 0..511"),
        (lambda: stream_load(0, 0xA0000000, 0), TypeError, "dram is a register of B"),
        (lambda: stream_store(0, 0, G[3]), TypeError, "dram is a register of B"),
        (lambda: b_add(G[3], B[1], 0), TypeError, "writes a register of B"),
    ],
)
def test_the_stream_helpers_check_their_arguments(build, error, match):
    with pytest.raises(error, match=match):
        build()


def test_the_transfers_take_register_a_0_unless_given_another():
    """The idle word's a (31) never ran in a transfer; program 13's a = 2 wait stays expressible."""
    assert X1.hand(B[3]).word()["f110_114"] == 0
    assert wait().word()["f110_114"] == 0
    other = X1.wait(B[2], b=B[5])
    assert other.word()["f110_114"] == 2
    assert Bundle.of(other.word()).parts[0] == X1.wait(B[2])


def test_the_mnemonics_file_is_current():
    path = Path(asm.__file__).with_name("MNEMONICS.md")
    assert path.read_text() == asm.mnemonics_markdown()


def test_assemble_warns_only_about_hypothesis_fields():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assemble([X1.hand(B[3]), NOP])
    with pytest.warns(
        HypothesisFieldWarning, match=r"2 hypothesis fields: f35_36, f234_235$"
    ):
        assemble([raw(xreg=3, f234_235=1), raw(f35_36=0)])


@pytest.mark.parametrize("const", ["f133_140", "f230_231", "f277"])
def test_assemble_rejects_a_cleared_constant(const):
    with pytest.raises(ValueError, match=const):
        assemble([raw(**{const: 0})])


@pytest.mark.parametrize("field", ["f35_36", "f145_146", "f232_233", "f234_235"])
def test_fields_constant_only_in_the_stock_programs_take_other_values(field):
    Word.from_fields({field: 0}).check()


def test_listing_names_ops_targets_and_delay_slots():
    # T = 0x5A3: its low byte in imm, bits 8..12 in f128_132. The idle run after the call splits
    # where its delay slots end; a return shows f128_132, which is no target there.
    call = Word.from_fields({"f1": 1, "imm": 0xA3, "f128_132": 5, "ctl_n": 0, "op": 3})
    ret = Word.from_fields({"f128_132": 1, "ctl_n": 0, "op": 4})
    data = OP(0x1F, ra=G[2], imm=0xC).word()
    assert (call.target, call.control, ret.target, ret.control, data.control) == (
        0x5A3,
        True,
        None,
        True,
        False,
    )
    # A control op with f229 set shows the flag, which equals idle.
    flagged = Word.from_fields({"imm": 10, "f128_132": 0, "op": 3})
    text = listing(Program((call, *[IDLE] * 6, ret, data, flagged)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0   call! -> 1443!  x0: x0k_lo=1!",
        "1..4 | idle x4",
        "5..6   idle x2",
        "7   ret!  op_slot: rb=r1!",
        "8 | op1f! r2, #0xc",
        "9 | call! -> 10! ctl_n=1!",
    ]


def test_listing_collapses_idle_runs_repeats_and_deltas():
    a = Word.from_fields({"xreg": 3, "ra": 2})
    b = Word.from_fields({"xreg": 7, "ra": 2})
    text = listing(Program((IDLE, IDLE, a, a, a, b, IDLE, b)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0..1   idle x2",
        "2..4   x3 idle~  op_slot: ra=r2!  x1: xreg=r3!",
        "5   idle~  ^  x1: xreg=r7!",
        "6   idle",
        "7   idle~  op_slot: ra=r2!  x1: xreg=r7!",
    ]


@pytest.mark.parametrize("labelled", [False, True])
def test_dsl_source_rebuilds_every_stock_program(labelled):
    for item, program in stock_programs(stock_image("em10ii")).items():
        targets = {w.target for w in program.words if w.target is not None}
        names = {f"w{t}": t for t in targets if t < len(program)} if labelled else None
        text = source(program, "P", names)
        assert ("Label('w" in text) is labelled
        namespace: dict = {}
        exec(text, namespace)  # noqa: S102 (our own generated source)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", HypothesisFieldWarning)
            assert assemble(namespace["P"]).words == program.words, item


def test_dsl_source_rebuilds_every_code_and_random_words():
    rng = random.Random(1)
    words = [Word(rng.getrandbits(320)) for _ in range(200)]
    for key, unit in asm.UNITS_BY_KEY.items():
        head = OP_FIELD if key == "op" else VIEWS[key].operands["op"]
        own = UNITS["op_slot" if key == "op" else key].fields
        for code in unit.names:
            words.append(Word.from_fields({head.id: code}))
            others = {f.id: rng.randrange(f.mask + 1) for f in own}
            words.append(Word.from_fields(others | {head.id: code}))
    namespace: dict = {}
    exec(source(Program(tuple(words)), "P"), namespace)  # noqa: S102 (our own generated source)
    assert [bundle(b).word() for b in namespace["P"]] == words


def test_listing_shows_data_ops_in_their_format():
    # Operands replace their fields; a slot field outside the format (imm in a register form,
    # f120_122 as an R register) stays a field.
    shift = OP(0x3F, ra=G[2], rb=G[2], imm=5).word()
    signed = (OP(0x0C, ra=G[3], imm=-7) | raw(f120_122=1)).word()
    wide = OP(0x0B, ra=G[1], imm=0x2468).word()
    reg = (OP(0x49, ra=G[1], rb=G[2]) | raw(imm=4)).word()
    text = listing(Program((shift, IDLE, signed, IDLE, wide, IDLE, reg)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body[::2] == [
        "0   op3f! r2, r2, #5",
        "2   op0c! r3, #-7  op_slot: rr=r1!",
        "4   op0b! r1, #0x2468",
        "6   op49! r1, r2  op_slot: imm=4!",
    ]


def labelled() -> list:
    """Call 0 -> routine, halt at 5; the routine loops back to its top and returns."""
    return [
        OP.jump("routine"),
        *[NOP] * 4,
        OP.halt(),
        Label("routine") | raw(xreg=3),
        OP.loop(G[31], "routine") | raw(f1=1),
        *[NOP] * 4,
        OP.ret(),
        *[NOP] * 4,
        Label("end"),
    ]


def build(stmts: list, origin: int = 0) -> Program:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HypothesisFieldWarning)
        return assemble(stmts, origin=origin)


def test_a_control_op_takes_a_target_exactly_when_it_jumps():
    with pytest.raises(ValueError, match="op 0x4: target 10"):
        OP(4, to=10)
    with pytest.raises(ValueError, match="op 0x3: target None"):
        OP(3)


def test_relocation_moves_every_label_target_and_nothing_else():
    at0, at40 = build(labelled()), build(labelled(), 40)
    assert [(i, w.target) for i, w in enumerate(at0.words) if w.target is not None] == [
        (0, 6),
        (7, 6),
    ]
    assert labels(labelled(), 40) == {"routine": 46, "end": 57}
    for a, b in zip(at0.words, at40.words, strict=True):
        if a.target is None:
            assert a == b
            continue
        assert b.target == a.target + 40
        assert Word(TARGET.put(b.value, a.target)) == a


def test_undefined_and_duplicate_labels_raise():
    with pytest.raises(AssemblyError, match="'nowhere' is not defined"):
        build([OP.jump("nowhere"), NOP])
    with pytest.raises(AssemblyError, match="'end' defined twice"):
        build([*labelled(), Label("end")])


def test_a_target_over_13_bits_raises():
    build(labelled(), 8191 - 6)
    with pytest.raises(ValueError, match="does not fit 13 bits"):
        build(labelled(), 8191 - 5)


def test_labelled_source_reassembles_the_program():
    program = build(labelled(), 32)
    text = source(program, "P", labels(labelled(), 32), 32)
    assert "    OP.jump('routine'),  # 32 jump -> 38\n    NOP,  # 33\n" in text
    assert "    Label('routine') | raw(xreg=3),  # 38\n" in text
    assert "    OP.loop(G[31], 'routine') | raw(x0k_lo=0x1),  # 39 loop -> 38\n" in text
    assert text.endswith("  # 48\n    Label('end'),  # 49\n]\n")
    with pytest.raises(ValueError, match="outside the program"):
        source(program, "P", {"after": 50}, 32)
    with pytest.raises(ValueError, match="on one word"):
        source(program, "P", {"a": 33, "b": 33}, 32)
    namespace: dict = {}
    exec(text, namespace)  # noqa: S102 (our own generated source)
    assert build(namespace["P"], 32) == program


def test_call_return_check_reports_a_return_past_the_end():
    program = build(labelled())
    assert call_return_errors(program) == []
    # 3 returns to 8, the last word; 5 to 10, past the end.
    call = OP.jump(0).word()
    short = Program((*[IDLE] * 3, call, IDLE, call, *[IDLE] * 3))
    assert call_return_errors(short) == [
        "op 3 at word 5 returns to 10, past the end (9 words)"
    ]
