import warnings

import pytest
from conftest import stock_image

from fenykl.dsp import memcpy
from fenykl.dsp.disasm import listing, source
from fenykl.dsp.program import HypothesisFieldWarning, Program, assemble, stock_programs
from fenykl.dsp.units import CtlHi, RegsA
from fenykl.dsp.word import IDLE, Word


def test_memcpy_rebuilds_program_16():
    with pytest.warns(HypothesisFieldWarning):
        built = memcpy.program()
    stock = stock_programs(stock_image("em10ii"))[16]
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


def test_assemble_rejects_a_cleared_constant():
    with pytest.raises(ValueError, match="f133_140"):
        assemble([Word(regs_a=RegsA(f133_140=0))])


def test_listing_collapses_idle_runs_repeats_and_deltas():
    a = Word(regs_a=RegsA(xreg=3, f123_127=2))
    b = Word(regs_a=RegsA(xreg=7, f123_127=2))
    text = listing(Program((IDLE, IDLE, a, a, a, b, IDLE)))
    body = [line.strip() for line in text.splitlines() if not line.startswith(";")]
    assert body == [
        "0..1  idle x2",
        "2..4  x3 regs_a: xreg=r3~ f123_127=r2",
        "5  ^  regs_a: xreg=r7~",
        "6  idle",
    ]


def test_dsl_source_rebuilds_every_stock_program():
    for item, program in stock_programs(stock_image("em10ii")).items():
        namespace: dict = {}
        exec(source(program, "P"), namespace)  # noqa: S102 (our own generated source)
        assert namespace["P"] == list(program.words), item
