import pytest

from fenykl.dsp import NOP, OP, Boot, Confidence, G, Label, R, assemble
from fenykl.dsp.emu import NoHalt
from fenykl.dsp.job import Stop
from fenykl.dsp.semantics import Given

H, V = Confidence.HYPOTHESIS, Confidence.VERIFIED


def _branch_on_an_unknown_register():
    """A bz on r5, unknown on a fresh boot, over one word to the halt."""
    return assemble(
        [OP.bz(G[5], "end", rr=R[0]), NOP, NOP, NOP, NOP, NOP, Label("end") | OP.halt()]
        + [NOP] * 3
    )


def test_a_run_needs_its_undecided_branch_given():
    with pytest.raises(NoHalt) as e:
        Boot().run(_branch_on_an_unknown_register(), strict=False)
    assert e.value.flow.stop is Stop.UNDECIDED


@pytest.mark.parametrize("tier, carried", [(H, False), (V, True)])
def test_a_run_on_a_given_path_below_v_leaves_no_carry(tier, carried):
    boot = Boot()
    run = boot.run(_branch_on_an_unknown_register(), branching=Given({0: True}, tier))
    assert run.flow.stop is Stop.HALT
    assert [s.index for s in run.flow.steps][:6] == [0, 1, 2, 3, 4, 6]
    assert run.flow.steps[0].confidence == tier
    assert (boot.carry is not None) == carried
