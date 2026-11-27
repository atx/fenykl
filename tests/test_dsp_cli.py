import re

from conftest import stock_image

from fenykl.cli import main
from fenykl.dsp.emu import Boot
from fenykl.dsp.fields import FIELDS
from fenykl.dsp.program import stock_programs
from fenykl.dsp.stock import commands
from fenykl.dsp.stock.copy import ITEM

SRC, DST, LEN = 0xA0FCA800, 0xA1113600, 0x400
"""The flow command's defaults."""


def _out(capsys, *argv: str) -> list[str]:
    main(["dsp", *argv])
    return capsys.readouterr().out.splitlines()


def test_fields_prints_every_field_and_the_summary(capsys):
    lines = _out(capsys, "fields")
    ids = {line.split()[0] for line in lines}
    assert {f.id for f in FIELDS} <= ids
    assert lines[-1].startswith(f"{len(FIELDS)} fields; ")


def test_disasm_and_source_print_the_program(capsys):
    stock_image("em10ii")
    listing = _out(capsys, "disasm", str(ITEM))
    assert listing[0].startswith(f"; item {ITEM}, command 0xf: ")
    text = "\n".join(_out(capsys, "source", str(ITEM)))
    compile(text, "source", "exec")
    assert f"ITEM{ITEM}" in text


def _first_branch_not_taken() -> int:
    program = stock_programs(stock_image("em10ii"))[ITEM]
    job = commands.copy(program, SRC, DST, LEN)
    flow = Boot().run(job.program, request=job.local, strict=False).flow
    return next(s.index for s in flow.steps if s.op in (1, 2) and s.taken is False)


def test_flow_prints_the_runs_the_steps_and_the_stop(capsys):
    stock_image("em10ii")
    runs, *steps, end = _out(capsys, "flow")
    # Runs of consecutive words, delay slots after a `|`, and at least one jump.
    assert re.fullmatch(r"\|?\d+(\.\.\d+)?(, \|?\d+(\.\.\d+)?)+", runs)
    assert "|" in runs
    assert re.fullmatch(r"\d+ steps, 0 not covered; stop: halt", end)
    assert all(re.match(r" *\d+[| ] \w+", s) for s in steps)
    assert any(s.endswith("[verified]") for s in steps)


def test_flow_branch_overrides_the_datapath_at_a_hypothesis(capsys):
    index = _first_branch_not_taken()
    steps = _out(capsys, "flow")[1:-1]
    given = _out(capsys, "flow", "--branch", f"{index}=1")[1:-1]
    mine = next(s for s in steps if s.split()[0] == str(index))
    theirs = next(s for s in given if s.split()[0] == str(index))
    assert ": not taken, " in mine and not mine.endswith("[hypothesis]")
    assert ": taken, " in theirs and "given on the command line" in theirs
    assert theirs.endswith("[hypothesis]")
    assert given != steps
