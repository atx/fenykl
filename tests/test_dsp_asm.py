import warnings

import pytest
from conftest import stock_image

from fenykl.dsp.disasm import listing, source
from fenykl.dsp.fields import Operands
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
from fenykl.dsp.units import X0, X1, OpSlot
from fenykl.dsp.word import IDLE, Word


def test_slots_rebuild_program_16():
    # The DSL rebuilds the copy from slots read off the firmware's program: no stock word is
    # restated in the code. No field it sets is a hypothesis any more.
    stock = stock_programs(stock_image("em10ii"))[16]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        built = assemble(Word(**w.slots()) for w in stock.words)
    assert len(built) == len(stock)
    for index, (a, b) in enumerate(zip(built.words, stock.words)):
        assert a == b, f"word {index}: {a.changed(b)}"


def test_assemble_warns_only_about_hypothesis_fields():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assemble([Word(x1=X1(xreg=3)), IDLE])
    with pytest.warns(
        HypothesisFieldWarning, match=r"2 hypothesis fields: f0, f80_83$"
    ):
        assemble(
            [Word.from_fields({"xreg": 3, "f0": 1}), Word.from_fields({"f80_83": 0})]
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
    call = Word(x0=X0(f1=1), op_slot=OpSlot(imm=0xA3, f128_132=0x5, ctl_n=0, op=3))
    ret = Word(op_slot=OpSlot(f128_132=1, ctl_n=0, op=4))
    data = Word.data(0x1F, Operands(ra=2, imm=0xC))
    assert (call.target, call.control, ret.target, ret.control, data.control) == (
        0x5A3,
        True,
        None,
        True,
        False,
    )
    # A control op with f229 set shows the flag, which equals idle.
    flagged = Word(op_slot=OpSlot(imm=10, f128_132=0, op=3))
    text = listing(Program((call, *[IDLE] * 6, ret, data, flagged)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0   call~ -> 1443!  x0: f1=1!",
        "1..4 | idle x4",
        "5..6   idle x2",
        "7   ret~  op_slot: f128_132=r1!",
        "8 | op1f~ r2, #0xc",
        "9 | call~ -> 10! ctl_n=1!",
    ]


def test_listing_collapses_idle_runs_repeats_and_deltas():
    a = Word(x1=X1(xreg=3), op_slot=OpSlot(f123_127=2))
    b = Word(x1=X1(xreg=7), op_slot=OpSlot(f123_127=2))
    text = listing(Program((IDLE, IDLE, a, a, a, b, IDLE, b)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0..1   idle x2",
        "2..4   x3 idle~  op_slot: ra=r2!  x1: xreg=r3!",
        "5   idle~  ^  x1: xreg=r7!",
        "6   idle",
        "7   idle~  op_slot: ra=r2!  x1: xreg=r7!",
    ]


def test_dsl_source_rebuilds_every_stock_program():
    for item, program in stock_programs(stock_image("em10ii")).items():
        namespace: dict = {}
        exec(source(program, "P"), namespace)  # noqa: S102 (our own generated source)
        assert namespace["P"] == list(program.words), item


def test_a_slot_rejects_a_field_given_by_id_and_name():
    with pytest.raises(ValueError, match="xreg given twice"):
        X1(xreg=3, f115_119=4)


def test_listing_shows_data_ops_in_their_format():
    # Operands replace their fields; a slot field outside the format (imm in a register form,
    # f120_122 as an R register) stays a field.
    shift = Word.data(0x3F, Operands(ra=2, rb=2, imm=5))
    signed = Word.data(
        0x0C, Operands(ra=3, imm=-7), base=Word(op_slot=OpSlot(f120_122=1))
    )
    wide = Word.data(0x0B, Operands(ra=1, imm=0x2468))
    reg = Word.data(0x49, Operands(ra=1, rb=2), base=Word(op_slot=OpSlot(imm=4)))
    text = listing(Program((shift, IDLE, signed, IDLE, wide, IDLE, reg)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body[::2] == [
        "0   op3f~ r2, r2, #5",
        "2   op0c~ r3, #-7  op_slot: f120_122=r1!",
        "4   op0b~ r1, #0x2468",
        "6   op49~ r1, r2  op_slot: imm=4!",
    ]


HALT = Word(op_slot=OpSlot(op=7))


def labelled() -> list:
    """Call 0 -> routine, halt at 5; the routine loops back to its top and returns."""
    return [
        Ctl(3, "routine"),
        *[IDLE] * 4,
        HALT,
        Label("routine"),
        Word(x1=X1(xreg=3)),
        Ctl(0, "routine", Word(x0=X0(f1=1))),
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
    base = Word(x0=X0(f1=1))
    assert Word.ctl(3, 0x5A3, base) == Word(
        x0=X0(f1=1), op_slot=OpSlot(imm=0xA3, f128_132=0x5, ctl_n=0, op=3)
    )
    assert Word.ctl(4) == Word(op_slot=OpSlot(ctl_n=0, op=4))
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
    assert "    Ctl(3, 'routine'),  # 32 call -> 38\n    IDLE,  # 33\n" in text
    assert "    Label('routine'),\n    Word(x1=X1(xreg=3)),  # 38\n" in text
    assert "    Ctl(0, 'routine', Word(x0=X0(f1=0x1))),  # 39 loop -> 38\n" in text
    assert text.endswith("  # 48\n    Label('end'),\n]\n")
    namespace: dict = {}
    exec(text, namespace)  # noqa: S102 (our own generated source)
    assert namespace["P"] == labelled()
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
