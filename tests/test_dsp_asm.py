import random
import warnings
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
    OP,
    S1,
    S2,
    S2_HI,
    S3,
    X0,
    X1,
    B,
    Bundle,
    G,
    O,
    Raw,
    U,
    raw,
    to,
)
from fenykl.dsp.disasm import listing, source
from fenykl.dsp.fields import FIELDS, Confidence, Operands
from fenykl.dsp.program import (
    AssemblyError,
    Ctl,
    HypothesisFieldWarning,
    Label,
    Program,
    assemble,
    call_return_errors,
    labels,
    stock_programs,
)
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
    rng = random.Random(616)
    for _ in range(500):
        word = Word(rng.getrandbits(320))
        assert Bundle.of(word).word() == word
        # Near idle: a few fields at random values, so heads, ports and M decode as parts.
        near = {f.id: rng.randrange(f.mask + 1) for f in rng.sample(FIELDS, 6)}
        word = Word.from_fields(near)
        assert Bundle.of(word).word() == word, near


def test_a_kernel_word_decodes_to_its_instructions():
    word = (
        S1.xor_imm(L1[4], imm=0x20, a=L1[4], r=F1[4])
        | to(O[1], S1)
        | X0.store(asm.A[1], k=0, o=O[1])
    ).word()
    assert repr(Bundle.of(word)) == (
        "S1.xor_imm(L1[4], 0x20, a=L1[4], r=F1[4]) | X0.store(A[1], 0, O[1]) | to(O[1], S1)"
    )
    # A field no part expresses, here a register held under an idle head, stays raw.
    held = Word.data(0x0B, Operands(ra=31, imm=5), base=Word.from_fields({"xreg": 3}))
    parts = Bundle.of(held).parts
    assert repr(parts[0]) == "OP.li(5)" and parts[-1] == Raw({"f115_119": 3})


def test_names_and_codes_build_the_same_instruction():
    named = S1.xor_imm(L1[4], imm=0x20)
    assert named.word() == S1(0x04, b=L1[4], imm=0x20).word()
    assert set(named.fields) == {"f0", "f49_52", "f80_83", "f84_87", "f285_291"}
    assert S3.ins_reg(L3[1], F3[2], F3[3]).word()["s3idx"] == 2
    assert S3.ins(L3[1], F3[3], 5).word()["s3idx"] == 5


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
        (lambda: OP.call(), "target None"),
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
        (OP.call("routine"), raw(imm=0)),
    ],
)
def test_a_field_set_by_two_parts_raises_even_with_equal_values(parts):
    with pytest.raises(ValueError, match="set by"):
        Bundle(parts)


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


def test_a_bundle_names_its_target_by_label():
    program = assemble(
        [OP.call("r") | X1.hand(B[3]), *[IDLE] * 4, Label("r"), OP.halt()]
    )
    assert program.words[0].target == 5 and program.words[0]["xreg"] == 3
    with pytest.raises(AssemblyError, match="'nowhere' is not defined"):
        assemble([OP.br(G[1], "nowhere")])


def test_the_mnemonics_file_is_current():
    path = Path(asm.__file__).with_name("MNEMONICS.md")
    assert path.read_text() == asm.mnemonics_markdown()


def test_assemble_warns_only_about_hypothesis_fields():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assemble([X1.hand(B[3]), IDLE])
    with pytest.warns(
        HypothesisFieldWarning, match=r"2 hypothesis fields: f35_36, f234_235$"
    ):
        assemble(
            [
                Word.from_fields({"xreg": 3, "f234_235": 1}),
                Word.from_fields({"f35_36": 0}),
            ]
        )


@pytest.mark.parametrize("const", ["f133_140", "f230_231", "f277"])
def test_assemble_rejects_a_cleared_constant(const):
    with pytest.raises(ValueError, match=const):
        assemble([Word.from_fields({const: 0})])


@pytest.mark.parametrize("field", ["f35_36", "f145_146", "f232_233", "f234_235"])
def test_fields_constant_only_in_the_stock_programs_take_other_values(field):
    Word.from_fields({field: 0}).check()


def test_listing_names_ops_targets_and_delay_slots():
    # T = 0x5A3: its low byte in imm, bits 8..12 in f128_132. The idle run after the call splits
    # where its delay slots end; a return shows f128_132, which is no target there.
    call = Word.from_fields({"f1": 1, "imm": 0xA3, "f128_132": 5, "ctl_n": 0, "op": 3})
    ret = Word.from_fields({"f128_132": 1, "ctl_n": 0, "op": 4})
    data = Word.data(0x1F, Operands(ra=2, imm=0xC))
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
        "0   call! -> 1443!  x0: k=0x7c1!",
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
        "2..4   x3 idle~  op_slot: ra=r2!  x1: b=r3! k=0x300!",
        "5   idle~  ^  x1: b=r7! k=0x700!",
        "6   idle",
        "7   idle~  op_slot: ra=r2!  x1: b=r7! k=0x700!",
    ]


def test_dsl_source_rebuilds_every_stock_program():
    for item, program in stock_programs(stock_image("em10ii")).items():
        namespace: dict = {}
        exec(source(program, "P"), namespace)  # noqa: S102 (our own generated source)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", HypothesisFieldWarning)
            assert assemble(namespace["P"]).words == program.words, item


def test_listing_shows_data_ops_in_their_format():
    # Operands replace their fields; a slot field outside the format (imm in a register form,
    # f120_122 as an R register) stays a field.
    shift = Word.data(0x3F, Operands(ra=2, rb=2, imm=5))
    signed = Word.data(
        0x0C, Operands(ra=3, imm=-7), base=Word.from_fields({"f120_122": 1})
    )
    wide = Word.data(0x0B, Operands(ra=1, imm=0x2468))
    reg = Word.data(0x49, Operands(ra=1, rb=2), base=Word.from_fields({"imm": 4}))
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
        Ctl(3, "routine"),
        *[IDLE] * 4,
        OP.halt(),
        Label("routine"),
        raw(xreg=3),
        OP.loop(G[31], "routine") | raw(f1=1),
        *[IDLE] * 4,
        Word.ctl(4),
        *[IDLE] * 4,
        Label("end"),
    ]


def build(stmts: list, origin: int = 0) -> Program:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HypothesisFieldWarning)
        return assemble(stmts, origin=origin)


def test_word_ctl_holds_its_target_in_imm_and_f128_132():
    base = Word.from_fields({"f1": 1})
    assert Word.ctl(3, 0x5A3, base) == (OP.call(0x5A3) | raw(f1=1)).word()
    assert Word.ctl(4) == OP.ret().word()
    for bad in ((4, 10), (3, None), (7, None)):
        with pytest.raises(ValueError):
            Word.ctl(*bad)


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
        assert Word.ctl(b.op, a.target, b) == a


def test_undefined_and_duplicate_labels_raise():
    with pytest.raises(AssemblyError, match="'nowhere' is not defined"):
        build([Ctl(1, "nowhere"), IDLE])
    with pytest.raises(AssemblyError, match="'end' defined twice"):
        build([*labelled(), Label("end")])


def test_a_target_over_13_bits_raises():
    build(labelled(), 8191 - 6)
    with pytest.raises(ValueError, match="does not fit 13 bits"):
        build(labelled(), 8191 - 5)


def test_labelled_source_reassembles_the_program():
    program = build(labelled(), 32)
    text = source(program, "P", labels(labelled(), 32), 32)
    assert "    OP.call('routine'),  # 32 call -> 38\n    IDLE,  # 33\n" in text
    assert "    Label('routine'),\n    raw(xreg=3),  # 38\n" in text
    assert "    OP.loop(G[31], 'routine') | raw(x0k_lo=0x1),  # 39 loop -> 38\n" in text
    assert text.endswith("  # 48\n    Label('end'),\n]\n")
    namespace: dict = {}
    exec(text, namespace)  # noqa: S102 (our own generated source)
    assert build(namespace["P"], 32) == program


def test_call_return_check_reports_a_return_past_the_end():
    program = build(labelled())
    assert call_return_errors(program) == []
    # 3 returns to 8, the last word; 5 to 10, past the end.
    call = Word.ctl(3, 0)
    short = Program((*[IDLE] * 3, call, IDLE, call, *[IDLE] * 3))
    assert call_return_errors(short) == [
        "op 3 at word 5 returns to 10, past the end (9 words)"
    ]
