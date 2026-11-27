"""The op slot over register intervals: what a data op of `semantics.OP_SEMANTICS` computes when its
operands are `registers.Reg` intervals (`op_effect`), and where the word's PR ports send its result
(`destinations`). The copy model covers inserted words by it (`copy.Copy16Model`). The ops' rules and
tiers are `semantics`', as `machine.Machine` steps by them; over intervals a select gives the hull
of its inputs and a result past 32 bits is unknown."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from itertools import product

from ..fields import IDLE_OP, PR_SOURCES, Confidence, lookup
from ..semantics import (
    LOCAL_BYTES,
    LOCAL_LOADS,
    LOCAL_LOADS_RB,
    LOCAL_STORES,
    LOCAL_STORES_RB,
    LOCAL_UNITS,
    PR_ENABLES,
    SELECT_MEASURED,
    OpSemantics,
    OpValue,
    Value,
    local_offset,
    ring_length,
    ring_step,
)
from ..word import Word
from .registers import Reg, RegisterFile

I, V = Confidence.INFERRED, Confidence.VERIFIED

STORES = frozenset({*LOCAL_STORES, *LOCAL_STORES_RB})
"""The local stores: what they write is not modelled over intervals."""


@dataclass(frozen=True)
class Effect:
    """What a data op did: the value it computes, when it computes one (`computes`; result None
    = a value not known), which its word's PR ports route to a register (`destinations`); a
    note for the trace."""

    computes: bool = False
    result: Reg | None = None
    note: str = ""


class RegSet(Enum):
    """Two of the scalar unit's four register files (`fields`): B, which X1 reads (xreg among its operands) and
    port 267 writes, and G, which ra and rb read and port 272 writes (verified on the camera by
    exact readout that port 267 writes no G register). R and A are not modelled."""

    XREG = "B, the file X1 reads"
    OPERAND = "G, the file ra and rb read"


@dataclass(frozen=True)
class ResultPort:
    """A PR port that carries a data op's result: under `enable` = `value` the register its
    field `reg` names, in `to`."""

    enable: str
    value: int
    reg: str
    to: RegSet


RESULT_PORTS = (
    ResultPort("f267_268", 1, "f211_215", RegSet.XREG),
    ResultPort("f272_273", 1, "f224_228", RegSet.OPERAND),
)
"""The ports a data op's result goes through (verified on the camera by exact readout: each
enable value's source, `fields.PR_SOURCES`; and in the copy, a data op of each of the families
B, C, D and E inserted after a copy's last setup word wrote the register f211_215 names under
f267_268 = 1, not ra, which word 68 or 73 then read through its xreg; f224_228 under
f272_273 = 1 fed a later op's ra and rb). A port write lands at its word's end and is read from
the next word on (`machine`'s clock rule)."""


def destinations(word: Word) -> tuple[list[tuple[RegSet, int]], str]:
    """Where the word's data op result goes: (set, register) per decoded enabled port, and why
    the destination is incomplete ("" when every enabled port is decoded and one is). An enable
    that takes X0's or X1's result (`fields.PR_SOURCES`) or writes R or A counts as open here;
    one that writes nothing (f267_268 = 2) is skipped as idle. Without an enabled port a data op
    writes no register (verified on the camera by exact readout); this still reports such a
    result as open rather than dropping it."""
    dests, names = [], []
    for enable in PR_ENABLES:
        v = word[enable]
        if v == lookup(enable).idle or (
            v in PR_SOURCES[enable] and PR_SOURCES[enable][v] is None
        ):
            continue
        port = next(
            (p for p in RESULT_PORTS if p.enable == enable and p.value == v), None
        )
        if port is None:
            names.append(f"{enable}={v}")
        else:
            dests.append((port.to, word[port.reg]))
    if names:
        why = f"where the result goes under {', '.join(names)} is open"
    elif not dests:
        why = "no PR port enabled: where the result goes is open"
    else:
        why = ""
    return dests, why


ENUM_LIMIT = 1 << 16
"""The most input combinations a result over register intervals is enumerated for."""


SPAN = (-(1 << 31), 1 << 32)
"""Results outside [lo, hi), 32 bits read signed or unsigned, are unknown: the ops wrap at 32
bits (verified on the camera by exact readout), which the intervals do not model."""


def _result(
    word: Word,
    text: str,
    inputs: tuple[Reg | None, ...],
    f: Value,
    tier: Confidence,
) -> Effect:
    """The result f(*inputs) over the inputs' intervals, its hull enumerated; its taint is the
    weakest of `tier` and the inputs'. f None at a combination
    (a case the rule leaves open) or a result outside `SPAN` gives an unknown value."""
    name = f"{text} (op {word.op:#x})"
    if any(x is None for x in inputs):
        return Effect(True, None, f"{name}: an unknown input")
    regs = [x for x in inputs if x is not None]
    n = 1
    for x in regs:
        n *= x.hi - x.lo + 1
    if n > ENUM_LIMIT:
        return Effect(True, None, f"{name}: inputs too wide to enumerate")
    outs, taint = [], min((tier, *(x.taint for x in regs)))
    for args in product(*(range(x.lo, x.hi + 1) for x in regs)):
        v = f(*args)
        if v is None:
            return Effect(True, None, f"{name}: open at {', '.join(map(hex, args))}")
        outs.append(v)
    lo, hi = min(outs), max(outs)
    if lo < SPAN[0] or hi >= SPAN[1]:
        return Effect(True, None, f"{name}: beyond 32 bits, the wrap is not modelled")
    value = f"{lo:#x}" if lo == hi else f"{lo:#x}..{hi:#x}"
    return Effect(True, Reg(lo, hi, taint, name), f"{name} = {value}")


def _interval(rule: OpValue, tier: Confidence, word: Word, slot: RegisterFile) -> Effect:
    """A computing op's rule (`OpValue`) over the register intervals ra, rb read."""
    args = word.operands
    assert args is not None
    if rule.form == "rb":
        assert args.rb is not None
        named = rule.text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        if args.ra == args.rb:
            return _result(word, named, (slot[args.ra],), lambda a: rule.f(a, a), tier)
        return _result(word, named, (slot[args.ra], slot[args.rb]), rule.f, tier)
    assert args.imm is not None
    c = args.imm
    named = rule.text.replace("ra", f"r{args.ra}").replace("c", f"{c:#x}")
    return _result(word, named, (slot[args.ra],), lambda a: rule.f(a, c), tier)


def _const(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """0x0b: the sign-extended imm16; ra is not read."""
    args = word.operands
    assert args is not None and args.imm is not None
    c = args.imm
    return _result(word, f"{c:#x}", (), lambda: c, sem.confidence)


def _local_load(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """A read of local memory at ra + width x imm13; ra is a base it does not write. The read
    is recorded only: the datum's delivery to the ALU bus three words on is not modelled. None
    at an address not aligned to the width (not measured)."""
    width = LOCAL_LOADS[word.op][0]
    args = word.operands
    assert args is not None and args.imm is not None
    offset = local_offset(width, args.imm)
    where = f"local {LOCAL_UNITS[width]} r{args.ra} + {offset:#x} B"
    base = slot[args.ra]
    if base is None or base.exact is None:
        return Effect(note=f"reads {where}, r{args.ra} unknown")
    if (base.exact + offset) % LOCAL_BYTES % width:
        return None
    return Effect(note=f"reads {where}")


def _and_mask(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """ra & imm16 sign-extended. A mask that clears only low bits (~(2^k - 1)) is monotonic, so
    a wide ra interval keeps exact bounds; other masks of a wide interval give a bound."""
    args = word.operands
    assert args is not None and args.imm is not None
    mask = args.imm
    src = slot[args.ra]
    text = f"r{args.ra} & {mask & 0xFFFFFFFF:#x}"
    clears_low = mask < 0 and (-mask) & (-mask - 1) == 0
    if src is None or src.exact is not None or not clears_low:
        return _result(word, text, (src,), lambda a: a & mask, sem.confidence)
    if src.lo < 0:
        return Effect(True, None, f"{text} (op {word.op:#x}): a negative input")
    name = f"{text} (op {word.op:#x})"
    taint = min(src.taint, sem.confidence)
    reg = Reg(src.lo & mask, src.hi & mask, taint, name)
    return Effect(True, reg, f"{name} = {reg.lo:#x}..{reg.hi:#x}")


def _ring_one(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """0x44: the ring pointer step with R = 1 and L from imm13 (`semantics.ring_length`)."""
    args = word.operands
    assert args is not None and args.imm is not None
    length, tier = ring_length(args.imm)
    if length is None:
        text = f"ring step r{args.ra} + 1, imm13 {args.imm:#x} (op {word.op:#x})"
        return Effect(True, None, f"{text}: L not measured")
    text = f"ring step r{args.ra} + 1, L {length:#x}"
    return _result(
        word,
        text,
        (slot[args.ra],),
        lambda a: ring_step((a & 0xFFFFFFFF) + 1, length),
        min(tier, sem.confidence),
    )


def _ring_r(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """0x46 / 0x47: the ring pointer step with R[f120_122], which the model does not hold, so
    the result is unknown."""
    return Effect(
        True,
        None,
        f"ring step, R{word['f120_122']} (op {word.op:#x}): the R file is not modelled",
    )


def _select(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """0x08: ra where R[f120_122] is 0, else rb; the R file is not modelled, so the result is
    the hull of ra and rb."""
    args = word.operands
    assert args is not None and args.rb is not None
    a, b = slot[args.ra], slot[args.rb]
    r = word["f120_122"]
    name = f"R{r} == 0 ? r{args.ra} : r{args.rb} (op 0x8)"
    if a is None or b is None:
        return Effect(True, None, f"{name}: an unknown input")
    measured = V if r in SELECT_MEASURED else I
    taint = min(a.taint, b.taint, sem.confidence, measured)
    lo, hi = min(a.lo, b.lo), max(a.hi, b.hi)
    value = f"{lo:#x}" if lo == hi else f"{lo:#x}..{hi:#x}"
    return Effect(True, Reg(lo, hi, taint, name), f"{name} = {value}")


def _recorded(note: str) -> Callable[[OpSemantics, Word, RegisterFile], Effect | None]:
    """An op whose result is not modelled here: recorded only."""
    return lambda sem, word, slot: Effect(note=note)


Evaluate = Callable[[OpSemantics, Word, RegisterFile], Effect | None]

_OWN_RULES: dict[int, Evaluate] = {
    IDLE_OP: _recorded(""),
    **{code: _local_load for code in LOCAL_LOADS},
    **{code: _recorded("reads local memory at ra + w x rb") for code in LOCAL_LOADS_RB},
    0x38: _and_mask,
    0x08: _select,
    0x0B: _const,
    0x44: _ring_one,
    0x46: _ring_r,
    0x47: _ring_r,
    0x48: _recorded("a product on the ALU bus, not delivered"),
    0x49: _recorded("a product on the ALU bus, not delivered"),
}
"""The ops of `OP_SEMANTICS` without an `OpValue` rule, by op, but the local stores."""


def op_effect(sem: OpSemantics, word: Word, slot: RegisterFile) -> Effect | None:
    """What data op `word` with semantics `sem` does over the register intervals of `slot`, the
    set ra and rb read; None where the word lies outside what the semantics covers. A local
    store (`STORES`) has no effect here: its caller refuses it."""
    if sem.rule is not None:
        return _interval(sem.rule, sem.confidence, word, slot)
    return _OWN_RULES[word.op](sem, word, slot)
