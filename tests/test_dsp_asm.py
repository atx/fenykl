import warnings

import pytest
from conftest import stock_image

from fenykl.dsp.disasm import listing, source
from fenykl.dsp.program import HypothesisFieldWarning, Program, assemble, stock_programs
from fenykl.dsp.units import C2, CtlHi, CtlLo, RegsA
from fenykl.dsp.word import IDLE, Word


def test_slots_rebuild_program_16():
    # The DSL rebuilds the copy from slots read off the firmware's program: no stock word is
    # restated in the code.
    stock = stock_programs(stock_image("em10ii"))[16]
    with pytest.warns(HypothesisFieldWarning):
        built = assemble(Word(**w.slots()) for w in stock.words)
    assert len(built) == len(stock)
    for index, (a, b) in enumerate(zip(built.words, stock.words)):
        assert a == b, f"word {index}: {a.changed(b)}"


def test_assemble_warns_only_about_hypothesis_fields():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assemble([Word(regs_a=RegsA(xreg=3)), IDLE])
    with pytest.warns(
        HypothesisFieldWarning, match=r"2 hypothesis fields: f123_127, f308_309$"
    ):
        assemble(
            [Word(regs_a=RegsA(xreg=3, f123_127=2)), Word(ctl_hi=CtlHi(f308_309=0))]
        )


@pytest.mark.parametrize("const", ["f133_140", "f230_231", "f277"])
def test_assemble_rejects_a_cleared_constant(const):
    with pytest.raises(ValueError, match=const):
        assemble([Word.from_fields({const: 0})])


@pytest.mark.parametrize("field", ["f35_36", "f145_146", "f232", "f234_235"])
def test_fields_constant_only_in_the_stock_programs_take_other_values(field):
    Word.from_fields({field: 0}).check()


def test_listing_names_ops_targets_and_delay_slots():
    # T = 0x5A3: its low byte in imm, bits 8..12 in f128_132. The idle run after the call splits
    # where its delay slots end; a return shows f128_132, which is no target there.
    call = Word(
        ctl_lo=CtlLo(imm=0xA3, f1=1),
        regs_a=RegsA(f128_132=0x5),
        c2=C2(ctl_n=0),
        ctl_hi=CtlHi(op=3),
    )
    ret = Word(regs_a=RegsA(f128_132=1), c2=C2(ctl_n=0), ctl_hi=CtlHi(op=4))
    data = Word(ctl_lo=CtlLo(imm=0xC), ctl_hi=CtlHi(op=0x1F))
    assert (call.target, call.control, ret.target, ret.control, data.control) == (
        0x5A3,
        True,
        None,
        True,
        False,
    )
    text = listing(Program((call, *[IDLE] * 6, ret, data)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0   call~ -> 1443!  ctl_lo: f1=1",
        "1..4 | idle x4",
        "5..6   idle x2",
        "7   ret~  regs_a: f128_132=r1",
        "8 | op1f~  ctl_lo: imm=c!",
    ]


def test_listing_collapses_idle_runs_repeats_and_deltas():
    a = Word(regs_a=RegsA(xreg=3, f123_127=2))
    b = Word(regs_a=RegsA(xreg=7, f123_127=2))
    text = listing(Program((IDLE, IDLE, a, a, a, b, IDLE, b)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0..1   idle x2",
        "2..4   x3 idle~  regs_a: xreg=r3~ f123_127=r2",
        "5   idle~  ^  regs_a: xreg=r7~",
        "6   idle",
        "7   idle~  regs_a: xreg=r7~ f123_127=r2",
    ]


def test_dsl_source_rebuilds_every_stock_program():
    for item, program in stock_programs(stock_image("em10ii")).items():
        namespace: dict = {}
        exec(source(program, "P"), namespace)  # noqa: S102 (our own generated source)
        assert namespace["P"] == list(program.words), item


def test_a_slot_rejects_a_field_given_by_id_and_name():
    with pytest.raises(ValueError, match="xreg given twice"):
        RegsA(xreg=3, f115_119=4)
