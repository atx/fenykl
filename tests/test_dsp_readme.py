"""The fenykl.dsp README stays true: its snippets run, the constants it cites exist with the values
it gives, and what it says raises does."""

import importlib
import importlib.util
import pkgutil
import re
import warnings
from pathlib import Path

import pytest
from conftest import stock_image

import fenykl.dsp
import fenykl.dsp.stock
from fenykl.dsp import (
    L0,
    L1,
    LOAD,
    NOP,
    OP,
    S1,
    X0,
    A,
    Boot,
    G,
    Label,
    NoHalt,
    O,
    R,
    UnknownSemantics,
    Word,
    assemble,
    raw,
    to,
)
from fenykl.dsp.program import AssemblyError, HypothesisFieldWarning

README = Path(__file__).resolve().parents[1] / "py" / "fenykl" / "dsp" / "README.md"
TEXT = README.read_text()
BLOCKS = re.findall(r"```python\n(.*?)```", TEXT, re.DOTALL)
STOCK = [b for b in BLOCKS if re.search(r"\bimage\b", b)]
CORE = [b for b in BLOCKS if b not in STOCK]


def _run(block: str, **names) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", HypothesisFieldWarning)
        exec(compile(block, str(README), "exec"), dict(names))  # noqa: S102 (the README)


@pytest.mark.parametrize("block", CORE, ids=range(len(CORE)))
def test_the_core_snippets_run(block):
    _run(block)


@pytest.mark.parametrize("block", STOCK, ids=range(len(STOCK)))
def test_the_stock_snippets_run(block):
    _run(block, image=stock_image("em10ii"))


def test_there_are_snippets_of_both_kinds():
    assert len(CORE) >= 5 and STOCK


# `module.NAME` with an optional "(value)" after it; modules under fenykl.dsp or its stock.
CITATION = re.compile(r"`([a-z_]+(?:\.[A-Za-z_]\w*)+)`(?: \(([^)]*)\))?")


MODULES = {
    m.name
    for package in (fenykl.dsp, fenykl.dsp.stock)
    for m in pkgutil.iter_modules(package.__path__)
}


def _resolve(path: str):
    """The object a cited path names: fenykl.dsp, its stock, or one of their modules, then
    attributes."""
    head, *rest = path.split(".")
    if head == "fenykl":
        return importlib.import_module(path)
    package = (
        "fenykl.dsp"
        if importlib.util.find_spec(f"fenykl.dsp.{head}")
        else "fenykl.dsp.stock"
    )
    obj = importlib.import_module(f"{package}.{head}")
    for name in rest:
        obj = getattr(obj, name)
    return obj


def _number(text: str) -> float:
    token = text.split()[0]
    return int(token, 0) if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", token) else float(token)


def test_cited_constants_exist_with_their_values():
    cited = [
        (path, value)
        for path, value in CITATION.findall(TEXT)
        if path.split(".")[0] in MODULES | {"fenykl"}
        and not path.endswith((".py", ".md"))
    ]
    assert len([v for _, v in cited if v]) >= 10
    for path, value in cited:
        obj = _resolve(path)
        if not value:
            continue
        actual = obj.us if hasattr(obj, "us") else obj
        assert actual == _number(value), (
            f"`{path}` is {actual}, the README says {value}"
        )


def _halted(*bundles):
    return assemble([*bundles, OP.halt(), *[NOP] * 4])


def test_what_the_readme_says_raises():
    xor = S1.xor_imm(L1[4], imm=0x20)
    with pytest.raises(ValueError, match="set by"):
        xor | xor
    with pytest.raises(ValueError, match="two ports"):
        to(L1[4], LOAD) | to(L1[4], S1)
    with pytest.raises(ValueError, match="no port"):
        to(G[1], S1)
    with pytest.raises(ValueError, match="does not fit"):
        S1.xor_imm(L1[4], imm=0x2000)
    with pytest.raises(IndexError):
        L1[16]
    with pytest.raises(TypeError):
        S1.xor_imm(L0[4], imm=0x20)
    with pytest.raises(TypeError):
        NOP | Word.from_int(0)
    with pytest.raises(AssemblyError, match="not defined"):
        assemble([OP.loop(G[8], "top", rr=R[0])])
    with pytest.raises(AssemblyError, match="twice"):
        assemble([Label("a"), Label("a")])
    with pytest.warns(HypothesisFieldWarning):
        assemble([raw(f35_36=0)])


def test_what_the_readme_says_boot_run_raises():
    boot = Boot()
    spin = assemble([Label("spin") | OP.jump("spin", rr=R[0]), *[NOP] * 4])
    with pytest.raises(NoHalt) as e:
        boot.run(spin, limit=100)
    assert e.value.flow.steps
    assert boot.carry is None
    for strict in (True, False):
        with pytest.raises(UnknownSemantics, match="past the program end"):
            Boot().run(assemble([NOP] * 3), strict=strict)
    with pytest.raises(UnknownSemantics, match="control op"):
        Boot().run(_halted(OP.jump(5)))


def test_a_store_in_the_port_word_stores_the_old_content():
    """The README's lag rule for O: the store head in the port's word reads O as it was."""
    boot = Boot()
    regs = {"L1": {4: [1] * 32}, "O": {1: [7] * 32}, "A": {1: 0}}
    store = X0.store(A[1], k=0, o=O[1])
    run = boot.run(
        _halted(S1.xor_imm(L1[4], imm=0x20) | to(O[1], S1) | store), regs=regs
    )
    assert run.machine.memory[0].lanes == (7,) * 32
