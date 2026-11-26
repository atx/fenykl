"""The op slot over register intervals: what a data op of `semantics.OP_SEMANTICS` does when its
operands are `state.Reg` intervals (`op_effect`), and where the word's PR ports send its result
(`destinations`). The copy model covers inserted words by it (`memcpy.Copy16Model`); the plain
stepper (`emu.Executor.step` without a datapath) runs its data ops by it."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from itertools import product

from .fields import IDLE_OP, PR_SOURCES, Confidence, lookup
from .semantics import (
    LOCAL_BYTES,
    LOCAL_LOADS,
    LOCAL_LOADS_RB,
    LOCAL_STORES,
    LOCAL_STORES_RB,
    LOCAL_UNITS,
    MULTIPLY_LAG,
    PR_ENABLES,
    OpSemantics,
    OpValue,
    Value,
    local_offset,
    ring_step,
)
from .state import Reg, RegisterFile, Space
from .word import Word

V = Confidence.VERIFIED


@dataclass(frozen=True)
class Effect:
    """What a data op did: the value it computes, when it computes one (`computes`; result None
    = a value not known), which its word's PR ports route to a register (`destinations`); a
    note for the trace; local memory writes of unknown data (address, bytes; address None =
    anywhere)."""

    computes: bool = False
    result: Reg | None = None
    note: str = ""
    stores: tuple[tuple[int | None, int], ...] = ()


class RegSet(Enum):
    """Two of the scalar unit's four register files (`fields`): B, which X1 reads (xreg among its operands) and
    port 267 writes, and G, which ra and rb read and port 272 writes (verified on the camera by
    exact readout that port 267 writes no G register). R and A are not modelled."""

    XREG = "B, the file X1 reads (`Boot.regs`)"
    OPERAND = "G, the file ra and rb read (`Boot.slot`)"


@dataclass(frozen=True)
class ResultPort:
    """A PR port that carries a data op's result: under `enable` = `value` the register its
    field `reg` names, in `to`, from `latency` executed words after the op on."""

    enable: str
    value: int
    reg: str
    to: RegSet
    latency: int


RESULT_PORTS = (
    ResultPort("f267_268", 1, "f211_215", RegSet.XREG, 4),
    ResultPort("f272_273", 1, "f224_228", RegSet.OPERAND, 3),
)
"""The ports a data op's result goes through (verified on the camera by exact readout: each
enable value's source, `fields.PR_SOURCES`; and in the copy, a data op of each of the families
B, C, D and E inserted after a copy's last setup word wrote the register f211_215 names under
f267_268 = 1, not ra, which word 68 or 73 then read through its xreg; f224_228
under f272_273 = 1 fed a later op's ra and rb). The latencies are the shortest distances seen
working in the copy: the xreg transfer four words after the write, the operand read three
words after it; exact readout on the camera shows the G write read by the very next word, which
the operand latency here does not model yet."""


OTHER_FILES = {"f206_210": "A", "f221_223": "R"}
"""Port registers of the register files the emulator does not model: a write there leaves G
and B as they are."""


def destinations(word: Word) -> tuple[list[tuple[RegSet, int, int]], list[str], str]:
    """Where the word's data op result goes: (set, register, latency) per decoded enabled port,
    the port fields of the enabled ports whose destination is open, and why the destination is
    incomplete ("" when every enabled port is decoded and one is). An enable that takes X0's or
    X1's result (`fields.PR_SOURCES`), which stepping does not run, or writes R or A counts as
    open here; one that writes nothing (f267_268 = 2) is skipped as idle. Without an enabled
    port a data op writes no register (verified on the camera by exact readout); stepping still
    reports such a result as open rather than dropping it."""
    dests, open_, names = [], [], []
    for enable, reg in PR_ENABLES.items():
        v = word[enable]
        if v == lookup(enable).idle or (
            v in PR_SOURCES[enable] and PR_SOURCES[enable][v] is None
        ):
            continue
        port = next(
            (p for p in RESULT_PORTS if p.enable == enable and p.value == v), None
        )
        if port is None:
            open_.append(reg)
            names.append(f"{enable}={v}")
        else:
            dests.append((port.to, word[port.reg], port.latency))
    if names:
        why = f"where the result goes under {', '.join(names)} is open"
    elif not dests:
        why = "no PR port enabled: where the result goes is open"
    else:
        why = ""
    return dests, open_, why


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


def _interval(rule: OpValue, word: Word, slot: RegisterFile) -> Effect:
    """A computing op's rule (`OpValue`) over the register intervals ra, rb read."""
    args = word.operands
    assert args is not None
    if rule.form == "rb":
        assert args.rb is not None
        named = rule.text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        if args.ra == args.rb:
            return _result(
                word, named, (slot[args.ra],), lambda a: rule.f(a, a), rule.tier
            )
        return _result(word, named, (slot[args.ra], slot[args.rb]), rule.f, rule.tier)
    assert args.imm is not None
    c = args.imm
    named = rule.text.replace("ra", f"r{args.ra}").replace("c", f"{c:#x}")
    return _result(word, named, (slot[args.ra],), lambda a: rule.f(a, c), rule.tier)


def _const(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """0x0b: the sign-extended imm16; ra is not read."""
    args = word.operands
    assert args is not None and args.imm is not None
    c = args.imm
    return _result(word, f"{c:#x}", (), lambda: c, V)


def _local_access(
    width: int, word: Word, slot: RegisterFile
) -> tuple[str, int | None] | None:
    """(the access as text, its index in units of `width` from the local base, None for an
    unknown base) at the byte address ra + width x imm13 modulo `LOCAL_BYTES`; None at an
    address not aligned to the width (not measured)."""
    args = word.operands
    assert args is not None and args.imm is not None
    offset = local_offset(width, args.imm)
    where = f"local {LOCAL_UNITS[width]} r{args.ra} + {offset:#x} B"
    base = slot[args.ra]
    if base is None or base.exact is None:
        return f"{where}, r{args.ra} unknown", None
    addr = (base.exact + offset) % LOCAL_BYTES
    if addr % width:
        return None
    return where, addr // width


def _local_load(width: int) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A read of `width` bytes of local memory; ra is a base it does not write. The read is
    recorded only: the datum's delivery to a port three words on is not modelled."""
    unit = LOCAL_UNITS[width]

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        access = _local_access(width, word, slot)
        if access is None:
            return None
        where, k = access
        if k is None:
            return Effect(note=f"reads {where}")
        b = local.read(local.base + width * k, width)
        value = f"{int.from_bytes(b.data, 'little'):#x}" if all(b.known) else "unknown"
        return Effect(note=f"reads local {unit} {k:#x}: {value}")

    return run


def _local_store(width: int) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A write of `width` bytes of local memory; ra is a base it does not write. The datum,
    R[f120_122], is not modelled, so the bytes become unknown; with an unknown base, all of
    local memory."""
    unit = LOCAL_UNITS[width]

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        access = _local_access(width, word, slot)
        if access is None:
            return None
        where, k = access
        if k is None:
            return Effect(stores=((None, width),), note=f"writes {where}: anywhere")
        addr = local.base + width * k
        return Effect(
            stores=((addr, width),), note=f"writes local {unit} {k:#x}: unknown datum"
        )

    return run


def _local_load_rb(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """A register-form load: stepping holds no rb, so it records the read only."""
    return Effect(note="reads local memory at ra + w x rb")


def _local_store_rb(width: int) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """A register-form store: stepping holds no rb, so all of local memory becomes unknown."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        return Effect(stores=((None, width),), note="writes ra + w x rb: anywhere")

    return run


def _and_mask(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """ra & imm16 sign-extended. A mask that clears only low bits (~(2^k - 1)) is monotonic, so
    a wide ra interval keeps exact bounds; other masks of a wide interval give a bound."""
    args = word.operands
    assert args is not None and args.imm is not None
    mask = args.imm
    src = slot[args.ra]
    text = f"r{args.ra} & {mask & 0xFFFFFFFF:#x}"
    clears_low = mask < 0 and (-mask) & (-mask - 1) == 0
    if src is None or src.exact is not None or not clears_low:
        return _result(word, text, (src,), lambda a: a & mask, V)
    if src.lo < 0:
        return Effect(True, None, f"{text} (op {word.op:#x}): a negative input")
    name = f"{text} (op {word.op:#x})"
    reg = Reg(src.lo & mask, src.hi & mask, src.taint, name)
    return Effect(True, reg, f"{name} = {reg.lo:#x}..{reg.hi:#x}")


def _ring_one(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """0x44: the ring pointer step with R = 1 and L the low 8 bits of imm13."""
    args = word.operands
    assert args is not None and args.imm is not None
    length = args.imm & 0xFF
    text = f"ring step r{args.ra} + 1, L {length:#x}"
    return _result(
        word,
        text,
        (slot[args.ra],),
        lambda a: ring_step((a & 0xFFFFFFFF) + 1, length),
        V,
    )


def _ring_r(text: str) -> Callable[[Word, RegisterFile, Space], Effect | None]:
    """0x46 / 0x47: the ring pointer step with R[f120_122], which the model does not hold, so
    the result is unknown."""

    def run(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
        args = word.operands
        assert args is not None
        named = text.replace("ra", f"r{args.ra}").replace("rb", f"r{args.rb}")
        r = word["f120_122"]
        return Effect(
            True, None, f"{named}, R{r} (op {word.op:#x}): the R file is not modelled"
        )

    return run


def _multiply(word: Word, slot: RegisterFile, local: Space) -> Effect | None:
    """0x48 / 0x49: the low 32 bits of ra x rb, recorded only: stepping does not deliver a
    product to the next word's ports."""
    args = word.operands
    assert args is not None and args.rb is not None
    text = f"r{args.ra} x r{args.rb}"
    if args.ra == args.rb:
        product_ = _result(
            word, text, (slot[args.ra],), lambda a: a * a & 0xFFFFFFFF, V
        )
    else:
        product_ = _result(
            word,
            text,
            (slot[args.ra], slot[args.rb]),
            lambda a, b: a * b & 0xFFFFFFFF,
            V,
        )
    return Effect(
        note=f"{product_.note}; on the ALU bus {MULTIPLY_LAG} word on, not delivered"
    )


Evaluate = Callable[[Word, RegisterFile, Space], Effect | None]

_OWN_RULES: dict[int, Evaluate] = {
    IDLE_OP: lambda word, slot, local: Effect(),
    **{code: _local_load(width) for code, (width, _) in LOCAL_LOADS.items()},
    **{code: _local_store(width) for code, width in LOCAL_STORES.items()},
    **{code: _local_load_rb for code in LOCAL_LOADS_RB},
    **{code: _local_store_rb(width) for code, width in LOCAL_STORES_RB.items()},
    0x38: _and_mask,
    0x0B: _const,
    0x44: _ring_one,
    0x46: _ring_r("ring step ra + R, L imm"),
    0x47: _ring_r("ring step ra + R, L rb"),
    0x48: _multiply,
    0x49: _multiply,
}
"""The ops of `OP_SEMANTICS` without an `OpValue` rule, by op."""


def op_effect(
    sem: OpSemantics, word: Word, slot: RegisterFile, local: Space
) -> Effect | None:
    """What data op `word` with semantics `sem` does over the register intervals of `slot`, the
    set ra and rb read; None where the word lies outside what the semantics covers."""
    if sem.rule is not None:
        return _interval(sem.rule, word, slot)
    return _OWN_RULES[word.op](word, slot, local)
