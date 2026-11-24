"""The datapath executor: the register files, the lane fabric, the lane memory and the buses of the
engine, run word by word under `emu.Executor.step`'s control flow.

One clock rule: every unit reads the state from before the word, and every port write lands at the
word's end, so a register a port writes in word t is read from word t + 1 on (verified on the camera
for G, R, A, the lane files L0..L3 and the scalar files). A head that reads a register a port writes
in its own word reads the old value, which was not measured: such a read is a hypothesis. Buses
carry values in the word they arrive in, counted in executed words:

- the ALU bus: the word's own op result; a multiply's product one word after the op; a local load's
  datum three words after the load (`semantics.MULTIPLY_LAG`, `LOAD_LAG`); a data op in the word
  wins over a delivery, whose datum is lost;
- X0's and X1's results in their head's word; under the M strobe (f276 = 0) X0's result bus carries
  M's value instead (`fields.S_RESULTS`);
- a lane head's result on its unit's bus for exactly one word, `fields.LANE_LAG` words after the head
  (a port at another word writes nothing); a head whose lag was not measured puts its result on the
  bus at both lags 0 and 1 as a hypothesis;
- a lane memory load's vector four words after its X0 head (`fields.LOAD_LAG`), at the first lane
  ports and O's port under enable 0; a port with no load arriving writes nothing;
- M: an S head's own-word scalar result; S0's and S1's held masks one word after the head, for one
  word; S3's reductions from the third word after the head on, held.

Strict throughout: a case the facts do not cover raises `UnknownSemantics`, a value the facts do not
give is unknown (None), and a value from a rule below verified carries that tier. Values are 32-bit
scalars (`Sv`) and 32-lane vectors of 16 bits (`Vec`), each with the weakest tier of what made it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .emu import LOCAL_BASE, Boot
from .fields import (
    ALU_BUS,
    HEADS,
    LANE_FILES,
    LANE_IMMEDIATES,
    LANE_LAG,
    LANE_LOAD,
    LANE_SOURCES,
    OP_FIELD,
    PB_FILES,
    PB_SOURCES,
    PORTS,
    PR_FILES,
    PR_SOURCES,
    S0_RESULT,
    S1_RESULT,
    S2_HIGH,
    S2_HIGH_HALVES,
    S2_LOW,
    S2_RESULT,
    S3_RESULT,
    S_NEXT_NONE,
    S_NEXT_VALUES,
    S_OWN_OPEN,
    STORE_SOURCES,
    TRANSFER_HEADS,
    X0_LANE_LOADS,
    X0_LANE_MEMORY,
    X_IMMEDIATES,
    Confidence,
    lookup,
    op,
)
from .fields import (
    LOAD_LAG as LANE_LOAD_LAG,
)
from .semantics import (
    IDLE_OP,
    LANE_COUNT,
    LANE_RULES,
    LOAD_LAG,
    LOCAL_BYTES,
    LOCAL_LOADS,
    LOCAL_STORES,
    MULTIPLY_LAG,
    OP_SEMANTICS,
    PR_ENABLES,
    S_RULES,
    X_RULES,
    Branch,
    OpValue,
    UnknownInput,
    UnknownSemantics,
    lane_address,
    lane_result,
    lane_tier,
    product_high,
    s_result,
    x_result,
)
from .state import Bytes, RegisterFile
from .word import Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED
M32 = 0xFFFFFFFF


@dataclass(frozen=True)
class Sv:
    """A 32-bit scalar, None when not known, with the weakest tier of what made it."""

    value: int | None
    tier: Confidence = V


@dataclass(frozen=True)
class Vec:
    """32 lanes of 16 bits, a None lane where not known."""

    lanes: tuple[int | None, ...]
    tier: Confidence = V


UNKNOWN = Sv(None)
UNKNOWN_VEC = Vec((None,) * LANE_COUNT)
NOTHING = object()
"""A bus with nothing on it: a port taking it writes nothing."""


@dataclass(frozen=True)
class Kept:
    """A bus with nothing on it by a rule below verified: a port taking it keeps its register,
    whose value drops to `tier`."""

    tier: Confidence


def _cap(x, tier: Confidence):
    if x is NOTHING or x.tier <= tier:
        return x
    if isinstance(x, Kept):
        return Kept(tier)
    return Sv(x.value, tier) if isinstance(x, Sv) else Vec(x.lanes, tier)


def _s32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def _sext(v: int, bits: int) -> int:
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


M_UNITS = ("s0", "s1", "s2", "s3")
"""f274_275's values: the unit whose result M puts out."""
U_TAKES = {
    0x1F: range(4),
    0x21: range(4),
    0x1C: range(4),
    0x1B: range(28, 32),
    0x1E: range(28, 32),
    0x20: range(28, 32),
}
"""What U's port takes beside an S3 head under f232_233 = 1: b's lanes into U's lanes 0..3
(verified on the camera, pre-registered); U's lanes 4..31 are not known after a write."""
U_NOTHING = (0x18, 0x19, 0x22, 0x14)
"""S3 heads beside which U's port wrote nothing (measured on the camera)."""
LANE_SHIFTS = (0x1B, 0x1C, 0x22)
STORE_SETTLE = 3
"""Words from O's port write to a store head that stores the new content (verified); a store in
the write's own word stores the old content (inferred); 1 and 2 words were not measured."""
DELIVERING_OPS = (IDLE_OP, 1)
"""Ops of a word that a delivery on the ALU bus reaches: the idle op and brf, which has no result
of its own (the stock copy's brf took a load's datum into G through 272 / 1, measured on the camera
by prefix dumps: inferred, so a datum delivered into a brf word is at most inferred)."""
HALF = 1 << 16


def _id(lo: int, hi: int) -> str:
    return f"f{lo}" if lo == hi else f"f{lo}_{hi}"


HEAD_IDS = {u: _id(*HEADS[u]) for u in HEADS}
LANE_PORT_FILE = {
    enable: next(
        f
        for f, (_, *nibs) in LANE_FILES.items()
        if _id(int(PORTS[enable][1:4]) - 110, int(PORTS[enable][1:4]) - 107) in nibs
    )
    for enable in LANE_SOURCES
}
"""The lane file each lane port writes: its register nibble lies 110 bits above the file's nibble."""
LANE_UNIT = {
    S0_RESULT: "s0",
    S1_RESULT: "s1",
    S2_LOW: "s2",
    S2_RESULT: "s2",
    S3_RESULT: "s3",
}


@dataclass
class Carry:
    """What one job leaves to the next of the same boot: the lane memory above the copy's buffer."""

    lane_memory: dict[int, Vec] = field(default_factory=dict)


class _Scalars(Mapping):
    """The scalars a lane rule reads; reading an unknown one raises `UnknownInput`."""

    def __init__(self, values: dict[str, Sv]) -> None:
        self.values = values
        self.tier = V

    def __getitem__(self, key: str) -> int:
        v = self.values[key]
        if v.value is None:
            raise UnknownInput(key)
        self.tier = min(self.tier, v.tier)
        return v.value

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)


def _own_word(unit: str, head: int) -> bool:
    rule = S_RULES[unit].get(head)
    return rule is not None and rule.lag == 0 and not rule.held


class Stream(Protocol):
    """The copy engine's side of X1's transfer heads (`stream.CopyStream`)."""

    def transfer(self, m: "Machine", index: int, head: int, value: Sv) -> None:
        """Transfer `head` of word `index` hands `value` (B[f115_119]) to the engine."""

    def lane_tier(self) -> Confidence:
        """The tier of the lane memory's content for own words at the stream's position."""


class Machine:
    """The datapath of one job on `boot` (its local memory is `boot.local`), with what an earlier
    job of the boot left (`carry`). It is the `datapath` of `emu.Executor` and decides the
    conditional control ops on its own register values (a `semantics.Branching`)."""

    def __init__(
        self, boot: Boot, carry: Carry | None = None, stream: "Stream | None" = None
    ) -> None:
        self.boot = boot
        self.stream = stream
        self.lanes_lost = False
        self.heads_seen: dict[str, list[tuple[int, int]]] = {}
        """The S heads run so far by unit: (word, head)."""
        self.g = [UNKNOWN] * 32
        self.a = [UNKNOWN] * 32
        self.b = [UNKNOWN] * 32
        self.r = [UNKNOWN] * 8
        self.f = {name: [UNKNOWN] * 16 for name in ("F0", "F1", "F2", "F3")}
        self.lanes = {name: [UNKNOWN_VEC] * 16 for name in LANE_FILES}
        self.u = [UNKNOWN_VEC] * 16
        self.o = [UNKNOWN_VEC] * 8
        self.memory: dict[int, Vec] = dict(carry.lane_memory) if carry else {}
        self.bus_due: dict[int, list[tuple[Sv, str]]] = {}
        self.load_due: dict[int, list[Vec]] = {}
        self.lane_due: dict[int, dict[str, list[tuple[Vec, Vec | None]]]] = {}
        self.held_due: dict[int, dict[str, Sv]] = {}
        self.reductions: list[tuple[int, Sv]] = []
        self.o_written: dict[int, int] = {}
        self._writes: set[tuple[str, int]] = set()
        self._u_write: tuple[int, Vec] | None = None
        self.open_m: dict[int, set[str]] = {}
        self.open_next: dict[int, dict[str, int]] = {}
        """M results not known one word after a head: by word, unit and head."""
        self.store_src = boot.source("local store", "an own program's family A store")

    def between_jobs(self) -> Carry:
        return Carry({} if self.lanes_lost else dict(self.memory))

    def lose_lanes(self) -> None:
        """The lane memory is not known from now on (the copy engine's activity is not)."""
        self.lanes_lost = True
        self.memory.clear()

    # ------------------------------------------------------------------ branching

    def decide(
        self,
        index: int,
        visit: int,
        word: Word,
        slot: RegisterFile,
        transfer: RegisterFile,
    ) -> Branch:
        """ops 0..2 on G[ra] (verified on the camera by exact readout): brf iff ra == 0, br iff
        ra != 0, the loop end iff ra > 0 signed."""
        ra = self.g[word["ra"]]
        if ra.value is None:
            return Branch(None, f"r{word['ra']} unknown", V)
        v = ra.value
        taken = {1: v == 0, 2: v != 0, 0: _s32(v) > 0}[word.op]
        return Branch(taken, f"r{word['ra']} = {v:#x}", ra.tier)

    # ------------------------------------------------------------------ reads

    def _rd(self, file: str, reg: int) -> Sv:
        v = self._file(file)[reg]
        return _cap(v, H) if (file, reg) in self._writes else v

    _rv = _rd

    # ------------------------------------------------------------------ the word

    def execute(self, index: int, word: Word, t: int) -> str:
        """Run word `word` (program index `index`) as executed word number `t`; returns a note."""
        self._writes = self._port_targets(word)
        notes: list[str] = []
        alu = self._op_slot(index, word, t, notes)
        bus = self._bus(word, alu, t)
        x0 = self._x(index, word, "x0", t)
        x1 = self._x(index, word, "x1", t)
        own = self._s_units(index, word, t)
        if word["f276"] == 0:
            m = self._m(index, word, own, t)
            x0bus = m if x0 is None else _cap(m, H)
        else:
            x0bus = UNKNOWN if x0 is None else x0
        writes = self._ports(index, word, t, bus, x0bus, UNKNOWN if x1 is None else x1)
        for key, value in writes.items():
            self._commit(key, value, t)
        for due in (
            self.bus_due,
            self.load_due,
            self.lane_due,
            self.held_due,
            self.open_m,
            self.open_next,
        ):
            due.pop(t, None)
        return "; ".join(notes)

    def _port_targets(self, word: Word) -> set[tuple[str, int]]:
        out = set()
        for enable, reg in PR_ENABLES.items():
            if word[enable] != lookup(enable).idle:
                out.add((PR_FILES[enable], word[reg]))
        for enable, file in PB_FILES.items():
            if word[enable] != lookup(enable).idle:
                out.add((file, word[PORTS[enable]]))
        for enable, file in LANE_PORT_FILE.items():
            if word[enable] != lookup(enable).idle:
                out.add((file, word[PORTS[enable]]))
        if word["f252_254"] != lookup("f252_254").idle:
            out.add(("O", word["f179_181"]))
        if word["f232_233"] == 1:
            out.add(("U", word["f141_144"]))
        return out

    def _op_slot(self, index: int, word: Word, t: int, notes: list[str]) -> Sv | None:
        """The op slot: its result on the ALU bus this word (None: no result), its deliveries
        scheduled, its local stores done."""
        code = word.op
        if code == IDLE_OP:
            return None
        if word.control:
            if code == 0:
                ra = self._rd("G", word["ra"])
                return Sv(None if ra.value is None else (ra.value - 1) & M32, ra.tier)
            return None
        sem = OP_SEMANTICS.get(code)
        if sem is None:
            raise UnknownSemantics(
                index, op(code).name, [(OP_FIELD, code)], "no semantics"
            )
        args = word.operands
        assert args is not None
        ra = self._rd("G", args.ra)
        rb = self._rd("G", args.rb) if args.rb is not None else None
        tier = min(sem.confidence, ra.tier, rb.tier if rb else V)
        if code in LOCAL_LOADS or code in LOCAL_STORES:
            self._local(index, word, code, ra, t, notes)
            return None
        if code in (0x48, 0x49):
            value = (
                None
                if ra.value is None or rb.value is None
                else ra.value * rb.value & M32
            )
            self.bus_due.setdefault(t + MULTIPLY_LAG, []).append(
                (Sv(value, tier), "product")
            )
            return None
        if isinstance(sem.run, OpValue):
            rule = sem.run
            ins = [ra] if rule.reads_ra else []
            ins += [rb] if rule.form == "rb" else []
            tier = min([sem.confidence, *(v.tier for v in ins)])
            if any(v.value is None for v in ins):
                return Sv(None, tier)
            second = None if rb is None else (rb.value or 0)
            return Sv(rule.exact(ra.value or 0, second, args.imm), tier)
        c = args.imm
        if code == 0x0B:
            return Sv(c & M32, sem.confidence)
        if ra.value is None:
            return Sv(None, tier)
        if code == 0x38:
            return Sv(ra.value & c & M32, tier)
        if code in (0x44, 0x46, 0x47):
            return self._ring(word, code, ra, rb, c, tier)
        raise UnknownSemantics(
            index, op(code).name, [(OP_FIELD, code)], "no exact rule"
        )

    def _ring(
        self, word: Word, code: int, ra: Sv, rb: Sv | None, c: int, tier: Confidence
    ) -> Sv:
        """The ring pointer step (`semantics.OP_SEMANTICS` 0x44 / 0x46 / 0x47)."""
        if code == 0x44:
            step = Sv(1)
        else:
            step = self._rd("R", word["f120_122"])
            step = Sv(None if step.value is None else _s32(step.value), step.tier)
        length = (
            (c if code != 0x47 else rb.value)
            if code != 0x47 or rb.value is not None
            else None
        )
        if step.value is None or length is None:
            return Sv(None, tier)
        length &= 0xFF
        s = ra.value + step.value
        s = s + length if s < 0 else s - length if s >= length else s
        return Sv(s & M32, min(tier, step.tier))

    def _local(
        self, index: int, word: Word, code: int, ra: Sv, t: int, notes: list[str]
    ) -> None:
        width = LOCAL_LOADS[code][0] if code in LOCAL_LOADS else LOCAL_STORES[code]
        imm = word.operands.imm
        if ra.value is None:
            if code in LOCAL_STORES:
                raise UnknownSemantics(
                    index, op(code).name, [], "a local store at an unknown address"
                )
            self.bus_due.setdefault(t + LOAD_LAG, []).append((Sv(None), "load"))
            return
        addr = (ra.value + width * imm) % LOCAL_BYTES
        if addr % width:
            raise UnknownSemantics(
                index, op(code).name, [], f"an unaligned local access at {addr:#x}"
            )
        if code in LOCAL_STORES:
            datum = self._rd("R", word["f120_122"])
            tier = min(datum.tier, ra.tier)
            if datum.value is None:
                self.boot.local.write(
                    LOCAL_BASE + addr, Bytes.unknown(width, self.store_src)
                )
            else:
                data = (datum.value & ((1 << 8 * width) - 1)).to_bytes(width, "little")
                self.boot.local.write(
                    LOCAL_BASE + addr, Bytes.of(data, self.store_src, tier)
                )
            notes.append(f"local {addr:#x} := R{word['f120_122']}")
            return
        b = self.boot.local.read(LOCAL_BASE + addr, width)
        signed = LOCAL_LOADS[code][1]
        if all(b.known):
            v = int.from_bytes(b.data, "little")
            v = _sext(v, 8 * width) & M32 if signed else v
            datum = Sv(v, min(ra.tier, b.weakest()))
        else:
            datum = Sv(None)
        self.bus_due.setdefault(t + LOAD_LAG, []).append((datum, "load"))

    def _bus(self, word: Word, alu: Sv | None, t: int) -> Sv | object:
        due = self.bus_due.get(t, [])
        if alu is not None:
            return alu
        if word.op not in DELIVERING_OPS and due:
            return UNKNOWN  # a delivery into a word with another op: not measured
        if len(due) == 1:
            return due[0][0] if word.op == IDLE_OP else _cap(due[0][0], I)
        if due:
            return UNKNOWN
        return NOTHING

    # ------------------------------------------------------------------ X0, X1

    def _x(self, index: int, word: Word, unit: str, t: int) -> Sv | None:
        head = word[HEAD_IDS[unit]]
        f = lookup(HEAD_IDS[unit])
        if head == f.idle:
            return None
        a_id, b_id = (
            ("f100_104", "f105_109") if unit == "x0" else ("f110_114", "f115_119")
        )
        file = "A" if unit == "x0" else "B"
        a, b = self._rd(file, word[a_id]), self._rd(file, word[b_id])
        k = X_IMMEDIATES[unit].get(word.value)
        if unit == "x1" and head in TRANSFER_HEADS:
            if self.stream is None:
                raise UnknownSemantics(
                    index,
                    f"x1 {head:#x}",
                    [],
                    "a transfer to the copy engine: no stream",
                )
            self.stream.transfer(self, index, head, b)
            return None
        if unit == "x0" and head in X0_LANE_MEMORY:
            self._lane_memory(index, word, head, a, b, k, t)
        if head not in X_RULES[unit]:
            raise UnknownSemantics(index, f"{unit} {head:#x}", [], "no rule")
        rule = X_RULES[unit][head]
        if rule.value is None:
            return None
        ins = [v for name, v in (("a", a), ("b", b)) if name in rule.reads]
        if any(v.value is None for v in ins):
            return Sv(None)
        value = x_result(unit, head, a.value or 0, b.value or 0, k)
        return Sv(value, min([rule.confidence, *(v.tier for v in ins)]))

    def _lane_memory(
        self, index: int, word: Word, head: int, a: Sv, b: Sv, k: int, t: int
    ) -> None:
        by_reg = head in range(0x1E, 0x22)
        if a.value is None or (by_reg and b.value is None):
            if head not in X0_LANE_LOADS:
                raise UnknownSemantics(
                    index, f"x0 {head:#x}", [], "a lane store at an unknown address"
                )
            self.load_due.setdefault(t + LANE_LOAD_LAG, []).append(UNKNOWN_VEC)
            return
        addr = lane_address(head, a.value, b.value if b.value is not None else 0, k)
        tier = min(a.tier, b.tier if by_reg else V, X0_LANE_MEMORY[head][1])
        if self.stream is not None:
            tier = min(tier, self.stream.lane_tier())
        if self.lanes_lost:
            if head in X0_LANE_LOADS:
                self.load_due.setdefault(t + LANE_LOAD_LAG, []).append(
                    Vec(UNKNOWN_VEC.lanes, H)
                )
            return
        if head in X0_LANE_LOADS:
            v = self.memory.get(addr, UNKNOWN_VEC)
            self.load_due.setdefault(t + LANE_LOAD_LAG, []).append(_cap(v, tier))
            return
        reg = word["f69_71"]
        v = self._rv("O", reg)
        since = t - self.o_written.get(reg, -STORE_SETTLE)
        if 0 < since < STORE_SETTLE:
            v = _cap(v, H)
        self.memory[addr] = _cap(v, tier)

    # ------------------------------------------------------------------ S units

    def _s_units(self, index: int, word: Word, t: int) -> dict[str, Sv]:
        """Run the active S heads: lane results scheduled at their lag, held results scheduled;
        returns the own-word scalar results by unit."""
        own: dict[str, Sv] = {}
        for unit, (file, a_id, b_id) in zip(
            ("s0", "s1", "s2", "s3"),
            (
                ("L0", "f37_40", "f41_44"),
                ("L1", "f45_48", "f49_52"),
                ("L2", "f53_56", "f57_60"),
                ("L3", "f61_64", "f65_68"),
            ),
            strict=True,
        ):
            head = word[HEAD_IDS[unit]]
            if head == lookup(HEAD_IDS[unit]).idle:
                continue
            a, b = self._rv(file, word[a_id]), self._rv(file, word[b_id])
            scalars = self._scalars(unit, word)
            self._lane_head(index, unit, head, a, b, scalars, word, t)
            self.heads_seen.setdefault(unit, []).append((t, head))
            rule = S_RULES[unit].get(head)
            if head in S_OWN_OPEN.get(unit, ()):
                self.open_m.setdefault(t, set()).add(unit)
            if (rule is None or rule.lag != 1) and head not in S_NEXT_NONE[unit]:
                self.open_next.setdefault(t + 1, {})[unit] = head
            if rule is None:
                continue
            sc = _Scalars(scalars)
            reads = rule.value.reads if rule.value else ""
            ins = [v for k, v in (("a", a), ("b", b)) if k in reads]
            tier = min([rule.confidence, *(v.tier for v in ins)])
            try:
                value = Sv(
                    s_result(unit, head, a.lanes, b.lanes, sc), min(tier, sc.tier)
                )
            except UnknownInput:
                value = Sv(None)
            if rule.held:
                self.reductions.append((t + rule.lag, value))
            elif rule.lag == 0:
                own[unit] = value
            else:
                self.held_due.setdefault(t + rule.lag, {})[unit] = value
        return own

    def _scalars(self, unit: str, word: Word) -> dict[str, Sv]:
        n = {
            k: word[k]
            for k in (
                "f0",
                "f23_30",
                "f31_34",
                "f37_40",
                "f53_56",
                "f65_68",
                "f76_79",
                "f80_83",
                "f84_87",
                "f88_91",
                "f92_95",
                "f96_99",
            )
        }
        if unit == "s0":
            imm = n["f76_79"] << 12 | n["f37_40"] << 8 | n["f23_30"]
            return {
                "s": self._rd("F0", word["f72_75"]),
                "r": self._rd("F0", n["f76_79"]),
                "imm": Sv(imm, LANE_IMMEDIATES["s0"][1]),
            }
        if unit == "s1":
            imm = _sext((n["f80_83"] >> 2) << 5 | n["f84_87"] << 1 | n["f0"], 7)
            return {
                "t": self._rd("F1", n["f84_87"]),
                "r": self._rd("F1", n["f80_83"]),
                "imm": Sv(imm, LANE_IMMEDIATES["s1"][1]),
            }
        if unit == "s2":
            imm = _sext(n["f53_56"] << 3 | n["f88_91"] >> 1, 7)
            return {
                "t": self._rd("F2", n["f88_91"]),
                "imm": Sv(imm, LANE_IMMEDIATES["s2"][1]),
                "f53_56": Sv(n["f53_56"]),
                "f88_91": Sv(n["f88_91"]),
            }
        return {
            **{k: Sv(n[k]) for k in ("f31_34", "f65_68", "f92_95", "f96_99")},
            "index": self._rd("F3", n["f92_95"]),
            "value": self._rd("F3", n["f96_99"]),
        }

    def _lane_head(
        self,
        index: int,
        unit: str,
        head: int,
        a: Vec,
        b: Vec,
        scalars: dict[str, Sv],
        word: Word,
        t: int,
    ) -> None:
        rule = LANE_RULES[unit].get(head)
        if rule is None and head in S_RULES[unit]:
            return
        if rule is None:
            # A head without a rule: its lane result is unknown at either lag, and so is M's
            # result of its unit in those words.
            for when in (t, t + 1):
                unknown = Vec(UNKNOWN_VEC.lanes, H)
                self.lane_due.setdefault(when, {}).setdefault(unit, []).append(
                    (unknown, unknown)
                )
                self.open_m.setdefault(when, set()).add(unit)
            if unit == "s3" and word["f232_233"] == 1:
                self._u_write = (word["f141_144"], Vec(UNKNOWN_VEC.lanes, H))
            return
        if rule.value is None:
            if unit == "s3" and word["f232_233"] == 1:
                self._u_port(index, head, b, word)
            return
        sc = _Scalars(scalars)
        ins = [v for name, v in (("a", a), ("b", b)) if name in rule.reads]
        tier = min([lane_tier(unit, head, sc), *(v.tier for v in ins)])
        high = None
        if any(x is None for v in ins for x in v.lanes):
            low = UNKNOWN_VEC
        else:
            al = [x or 0 for x in a.lanes]
            bl = [x or 0 for x in b.lanes]
            try:
                lanes = lane_result(unit, head, al, bl, sc)
                if unit == "s3" and head in LANE_SHIFTS:
                    lanes, tier = self._shift_in(lanes, scalars, tier)
                low = Vec(tuple(lanes), min(tier, sc.tier))
                if unit == "s2" and head in S2_HIGH_HALVES:
                    hi = product_high(head, al, bl, sc)
                    high = Vec(tuple(hi), min(tier, S2_HIGH_HALVES[head][1], sc.tier))
            except UnknownInput:
                low = UNKNOWN_VEC
        lag = LANE_LAG[unit].get(head)
        if lag is None:
            landings = [(t, H), (t + 1, H)]
        else:
            landings = [(t + lag[0], lag[1])]
        for when, ltier in landings:
            entry = (_cap(low, ltier), None if high is None else _cap(high, ltier))
            self.lane_due.setdefault(when, {}).setdefault(unit, []).append(entry)
        if unit == "s3" and word["f232_233"] == 1:
            self._u_port(index, head, b, word)

    def _shift_in(
        self, lanes: list[int | None], scalars: dict[str, Sv], tier: Confidence
    ) -> tuple[list[int | None], Confidence]:
        """S3's lane shift: the lanes moved in come from U[f31_34]'s lanes 0..3, where the rule
        predicts the shift."""
        n = _sext(scalars["f92_95"].value, 4)
        if not 1 <= abs(n) <= 4 or all(x is None for x in lanes):
            return lanes, tier
        u = self._rv("U", scalars["f31_34"].value)
        out = list(lanes)
        if n > 0:
            out[LANE_COUNT - n :] = u.lanes[:n]
        else:
            out[:-n] = u.lanes[4 + n : 4]
        return out, min(tier, u.tier)

    def _u_port(self, index: int, head: int, b: Vec, word: Word) -> None:
        reg = word["f141_144"]
        if head in U_NOTHING:
            self._u_write = None
            return
        take = U_TAKES.get(head)
        if take is None:
            self._u_write = (
                reg,
                Vec(UNKNOWN_VEC.lanes, H),
            )  # what U takes here is open
            return
        lanes = (*(b.lanes[k] for k in take), *(None,) * (LANE_COUNT - 4))
        self._u_write = (reg, Vec(lanes, b.tier))

    # ------------------------------------------------------------------ M

    def _m(self, index: int, word: Word, own: dict[str, Sv], t: int) -> Sv | object:
        """M's value under f274_275's unit `sel`: an own-word result (f274_275 chooses among
        several), else the unit's held result; S3's held one is its reduction register, unknown
        before the job's first reduction. A held result of another unit is not on M (inferred),
        so beside one M's value is at most inferred."""
        sel = M_UNITS[word["f274_275"]]
        held = dict(self.held_due.get(t, {}))
        ready = [(w, v) for w, v in self.reductions if w <= t]
        fresh = None
        if ready:
            w, v = max(ready, key=lambda x: x[0])
            held["s3"] = v
            fresh = v if w == t else None
        elif sel == "s3":
            held["s3"] = Sv(None)
        tier = I if any(u != sel for u in held if u != "s3") else V
        if sel in self.open_m.get(t, ()):
            return Sv(None, H)
        before = self.open_next.get(t, {}).get(sel)
        if before is not None and not (
            sel == "s3" and sel in own and _own_word("s3", before)
        ):
            # One word after a head whose result there is open (S3's extracts in consecutive
            # words each read their own lane: measured), M is not known: a value not fitted is
            # there (inferred) or the word was not read (hypothesis).
            return Sv(None, I if before in S_NEXT_VALUES.get(sel, ()) else H)
        h = held.get(sel)
        if not own:
            if h is not None:
                return _cap(h, tier)
            return Kept(min(tier, self._quiet_tier(t)))
        if len(own) > 1 and sel not in own:
            raise UnknownSemantics(
                index, "m", [], f"own-word results of {sorted(own)}, M under {sel}"
            )
        if sel not in own:
            # A lone own-word result under another unit's f274_275 (not measured).
            v = next(iter(own.values()))
            return _cap(v, H) if h is None else Sv(None, H)
        v = own[sel]
        if sel == "s3":
            if fresh is None:
                # Beside a register older than its first word the extract came alone in one
                # program (inferred).
                return _cap(v, min(tier, I)) if ready else _cap(v, tier)
            # A reduction's register on its first word, OR-ed with an extract's lane (measured).
            if v.value is None or fresh.value is None:
                return Sv(None)
            return Sv(v.value | fresh.value, min(v.tier, fresh.tier, I))
        if h is not None:
            return Sv(
                None, H
            )  # an own-word result beside its unit's held one: not measured
        return _cap(v, tier)

    def _quiet_tier(self, t: int) -> Confidence:
        """The tier of "nothing on M" two or more words after S heads: verified two words after
        S0 0x20 only, inferred after the others."""
        earlier = [
            (w, u, h) for u, hs in self.heads_seen.items() for w, h in hs if w <= t - 2
        ]
        if all(u == "s0" and h == 0x20 and w == t - 2 for w, u, h in earlier):
            return V
        return I

    # ------------------------------------------------------------------ ports

    def _ports(
        self, index: int, word: Word, t: int, bus, x0bus, x1: Sv
    ) -> dict[tuple[str, int], object]:
        writes: dict[tuple[str, int], object] = {}
        kept: dict[tuple[str, int], Confidence] = {}

        def put(file: str, reg: int, value) -> None:
            if value is NOTHING:
                return
            if isinstance(value, Kept):
                kept[(file, reg)] = value.tier
                return
            if (file, reg) in writes:
                raise UnknownSemantics(
                    index, "ports", [], f"two ports write {file}[{reg}]"
                )
            writes[(file, reg)] = value

        scalar = {"X0's result": x0bus, "X1's result": x1, ALU_BUS: bus}
        for enable, reg_id in PR_ENABLES.items():
            v = word[enable]
            if v == lookup(enable).idle:
                continue
            if v not in PR_SOURCES[enable]:
                raise UnknownSemantics(index, enable, [], f"enable {v}")
            src = PR_SOURCES[enable][v]
            if src is not None:
                put(PR_FILES[enable], word[reg_id], scalar[src])
        for enable, file in PB_FILES.items():
            v = word[enable]
            if v == lookup(enable).idle:
                continue
            if v not in PB_SOURCES[enable]:
                raise UnknownSemantics(index, enable, [], f"enable {v}")
            src = PB_SOURCES[enable][v]
            if src is not None:
                put(file, word[PORTS[enable]], scalar[src])
        for enable, file in LANE_PORT_FILE.items():
            v = word[enable]
            if v == lookup(enable).idle:
                continue
            src = LANE_SOURCES[enable].get(v)
            if src is None:
                continue  # wrote nothing from any S head (measured on the camera)
            put(file, word[PORTS[enable]], _cap(self._lane_source(src[0], t), src[1]))
        v = word["f252_254"]
        if v != lookup("f252_254").idle:
            if v not in STORE_SOURCES:
                raise UnknownSemantics(index, "f252_254", [], f"O's source {v}")
            text, tier = STORE_SOURCES[v]
            put("O", word["f179_181"], _cap(self._lane_source(text, t), tier))
        u = word["f232_233"]
        if u == 1:
            if word[HEAD_IDS["s3"]] == lookup(HEAD_IDS["s3"]).idle:
                self._u_write = (
                    word["f141_144"],
                    Vec(UNKNOWN_VEC.lanes, H),
                )  # no S3 head: open
            if self._u_write is not None:
                put("U", *self._u_write)
        elif u != lookup("f232_233").idle and u not in (0, 2):
            raise UnknownSemantics(index, "f232_233", [], f"enable {u}")
        self._u_write = None
        for (file, reg), tier in kept.items():
            if (file, reg) not in writes:
                writes[(file, reg)] = _cap(self._file(file)[reg], tier)
        return writes

    def _lane_source(self, text: str, t: int):
        if text == LANE_LOAD:
            due = self.load_due.get(t, [])
            return NOTHING if not due else due[0] if len(due) == 1 else UNKNOWN_VEC
        unit = LANE_UNIT.get(text, "s2" if text == S2_HIGH else None)
        if unit is None:
            raise ValueError(text)
        due = self.lane_due.get(t, {}).get(unit, [])
        if not due:
            return NOTHING
        if len(due) > 1:
            return Vec(UNKNOWN_VEC.lanes, H)
        low, high = due[0]
        if text == S2_HIGH:
            return UNKNOWN_VEC if high is None else high
        return low

    def _file(self, file: str) -> list:
        scalar = {"G": self.g, "A": self.a, "B": self.b, "R": self.r, **self.f}
        vector = {"U": self.u, "O": self.o, **self.lanes}
        return scalar.get(file) or vector[file]

    def _commit(self, key: tuple[str, int], value, t: int) -> None:
        file, reg = key
        self._file(file)[reg] = value
        if file == "O":
            self.o_written[reg] = t
