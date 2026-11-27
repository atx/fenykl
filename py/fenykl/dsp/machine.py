"""The datapath executor: the register files, the lane fabric, the lane memory and the buses of the
engine, run word by word under `emu.Executor.step`'s control flow.

One clock rule: every unit reads the state from before the word, and every port write lands at the
word's end, so a register a port writes in word t is read from word t + 1 on, and a unit that reads
a register a port writes in its own word reads the old value (verified on the camera, each file
with the port's value from another source than the reader: G by the op slot and by op 0's
decision, A by X0, B by X1, R by a store and by a ring step, F0..F3 by their read heads, L0..L3 by
a head beside another unit's result, the unit's own previous result and a lane load, U by the lane
shift, O by a lane store). The lane memory and the local memory are read in the load's word and
written in the store's word (verified on the camera: a store one, two or three words after a load
of its address does not change the load's datum, a load one or two words after a store reads the
new content). Buses carry values in the word they arrive in, counted in executed words:

- the ALU bus: the word's own op result; a multiply's product one word after the op; a local load's
  datum three words after the load (`semantics.MULTIPLY_LAG`, `LOCAL_LOAD_LAG`); an arrival in a word
  whose op has no result (a branch, a store, a load, a multiply) is on the bus alone, and
  arrivals beside each other or beside an op's result are OR-ed (verified on the camera, values
  whose bits are not nested); in an op 3 or op 4 word the same (verified for a load's datum,
  inferred for a product);
- X0's and X1's results in their head's word, X1 0x1e's quotient four words after it
  (`fields.DIVIDE_LAG`); under the M strobe (f276 = 0) X0's result bus carries
  M's value instead, also beside an active X0 head (`fields.S_RESULTS`; verified on the camera
  through three ports, values whose bits are not nested);
- a lane head's result on its unit's bus for exactly one word, `fields.LANE_LAG` words after the head
  (a port at another word writes nothing); a head whose lag was not measured puts its result on the
  bus at both lags 0 and 1 as a hypothesis; a lag-1 result and the next word's lag-0 result of
  one unit are OR-ed on its bus (verified on the camera in S0, S1, S2 and S3);
- a lane memory load's vector four words after its X0 head (`fields.LANE_LOAD_LAG`), at the first lane
  ports and O's port under enable 0; a port with no load arriving writes nothing; loads in flight
  together (issued at most four words apart) land at inferred, except X0 0x1d heads in the measured
  chains of `rules.LOAD_CHAINS` (`Machine._chained`);
- M: an S head's own-word scalar result; S0's and S1's held masks one word after the head, for one
  word; S3's reductions in the third word after the head only.

Strict by default: a case the facts do not cover raises `UnknownSemantics`, a value the facts do not
give is unknown (None), and a value from a rule below verified carries that tier. Values are 32-bit
scalars (`Sv`) and 32-lane vectors of 16 bits (`Vec`), each with the weakest tier of what made it.
Lenient (`Machine.strict` False), a case the facts do not cover lapses instead (`Machine.lapse`):
what it may write is forgotten, its word's step is unknown and all the job leaves and decides from
there on is a hypothesis. A control word whose transfer cannot be followed (it leaves its program)
raises in either mode.

Programs composed from these rules ran exact on the camera, every output byte pre-registered:
- a loop of 16 passes, each incrementing an index into G and A, loading two vectors into L2
  (X0 0x1d), multiplying them (S2 0x2), taking the product's halves into L1 through the ports
  of f155_158 and f159_162 under enable 1 and its low half into L3 through f175_178's under 1,
  narrowing with S1 0x29 into O under 3,
  storing with X0 0x1b and reducing the products in L3 with S3 0x2, read through M into G and
  accumulated by op 0x35: the 32-bit sum of 512 products and every stored vector;
- a 3 x 3 convolution of a byte image with signed weights in F2 and 32-bit accumulators per
  lane (S2 0x5's product halves into L0 and L1, the 32-bit add per lane, S1 0x28's narrowing
  shift, S0 0x8's clamp, an X0 store), DRAM to DRAM in one job; also as a loop of 27 words per
  32-pixel output row, the multiplies one word apart, at about 5.4 ns per word (timed over
  several copies);
- a dense layer whose weights stay in the lane memory, so that later jobs carry only their
  input;
- layers chained in one job: two convolutions and a 2 x 2 max pool (max_u of row pairs, then
  S3's even / odd lane splits 0xd / 0xc joined by max_u), the rows between the layers staying in
  the lane memory.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .fields import (
    ALONE_PORTS,
    ALONE_SELECTS,
    ALONE_ZERO_LANES,
    ALONE_ZEROS,
    ALU_BUS,
    DIVIDE_HEAD,
    DIVIDE_LAG,
    HEADS,
    LANE_FILES,
    LANE_LAG,
    LANE_LOAD,
    LANE_LOAD_LAG,
    LANE_SOURCES,
    M_HELD_BESIDE_OWN,
    M_LANE_ONLY,
    M_OTHER_OPEN,
    M_TOGETHER,
    M_UNITS,
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
    S2_NEXT,
    S2_RESULT,
    S3_RESULT,
    S_IMMEDIATES,
    S_NEXT_NONE,
    S_NEXT_VALUES,
    S_OWN_OPEN,
    STORE_NOTHING,
    STORE_SOURCES,
    STREAM_SELECT,
    TRANSFER_HEADS,
    U_NOTHING,
    U_TAKES,
    WAIT_HEAD,
    WAIT_NOTHING,
    X0_BESIDE_KEPT,
    X0_LANE_BY_REGISTER,
    X0_LANE_LOADS,
    X0_LANE_MEMORY,
    X_IMMEDIATES,
    Confidence,
    lane_file,
    lookup,
    op,
)
from .rules import DIVIDE_CHAINS, FIRST_PORT_WINS, LOAD_CHAIN_HEAD, LOAD_CHAINS
from .semantics import (
    IDLE_OP,
    LANE_COUNT,
    LANE_RULES,
    LANE_SHIFTS,
    LATCH_OPERAND,
    LATCHED,
    LOCAL_BYTES,
    LOCAL_LOAD_LAG,
    LOCAL_LOADS,
    LOCAL_LOADS_RB,
    LOCAL_STORES,
    LOCAL_STORES_RB,
    MULTIPLY_LAG,
    OP_SEMANTICS,
    PR_ENABLES,
    S_RULES,
    SELECT_MEASURED,
    X_RULES,
    Branch,
    UnknownInput,
    UnknownSemantics,
    lane_address,
    lane_result,
    lane_tier,
    product_high,
    ring_length,
    ring_step,
    s32,
    s_result,
    x_result,
)
from .state import LOCAL_BASE, Bytes, Space, SparseSpace
from .word import Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED
LOADS = LOCAL_LOADS | LOCAL_LOADS_RB
STORES = LOCAL_STORES | LOCAL_STORES_RB
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


@dataclass(frozen=True)
class Unknown:
    """Where a register or lane memory vector first went unknown (`Machine.trace`): the
    word's program index, the source it took and, where known, why that was unknown."""

    index: int
    source: str
    reason: str


def _known(x) -> bool:
    return x.value is not None if isinstance(x, Sv) else None not in x.lanes


def _cap(x, tier: Confidence):
    if x is NOTHING or x.tier <= tier:
        return x
    if isinstance(x, Kept):
        return Kept(tier)
    return Sv(x.value, tier) if isinstance(x, Sv) else Vec(x.lanes, tier)


def _or_bus(quotient: Sv, x1: Sv, head: int) -> Sv:
    """A quotient and an X1 head's own result on X1's bus in one word: OR-ed, verified beside
    X1 0x13 only."""
    if quotient.value is None or x1.value is None:
        return Sv(None, min(quotient.tier, x1.tier))
    return Sv(quotient.value | x1.value, min(quotient.tier, x1.tier, V if head == 0x13 else I))


def _unknown(file: str) -> Sv | Vec:
    return UNKNOWN_VEC if file in VECTOR_FILES else UNKNOWN


def _divide(a: Sv, b: Sv) -> Sv:
    """X1 0x1e: a / b signed, truncated toward zero (`fields.DIVIDE_HEAD`); b <= 0 was not
    measured."""
    tier = min(a.tier, b.tier)
    if a.value is None or b.value is None or s32(b.value) <= 0:
        return Sv(None, tier)
    n, d = s32(a.value), s32(b.value)
    q = abs(n) // d
    return Sv((-q if n < 0 else q) & M32, min(tier, I if n < 0 else V))


def _sext(v: int, bits: int) -> int:
    return v - (1 << bits) if v >> (bits - 1) & 1 else v


def _id(lo: int, hi: int) -> str:
    return f"f{lo}" if lo == hi else f"f{lo}_{hi}"


HEAD_IDS = {u: _id(*HEADS[u]) for u in HEADS}
WEDGING_S0 = 0x4F
WEDGING_S0_AT = 3
"""The count of `WEDGING_S0` words under M's strobe and port 272 / 2 at which a job lapses."""
WEDGING_S0_WHY = (
    "a third S0 0x4f word under M's strobe with port 272 / 2 in the job: three or four such "
    "words (nibbles 1..3, G12 written) wedged the camera's job, each as a fresh boot's first "
    "job after a stock copy, so the boot's earlier jobs are not needed; one alone (four "
    "settings, either port form) and two (four pairs, adjacent or not) ran; each of three "
    "triples wedged (verified in one frame; other frames and settings inferred)"
)
"""The S0 head whose words, under M's strobe and with port 272 / 2, never let a job end on the
camera from the third such word in a job; one or two ran, each writing nothing through the port."""
LATCH_MEASURED = {
    "s0": frozenset({0x05, 0x20, 0x41}),
    "s1": frozenset({0x00, 0x01, 0x34, 0x53}),
}
"""Heads whose read was latched on the camera; other heads latch at inferred. S1 0x39, which
reads a only, kept S1's latch (measured)."""
LANE_PORT_FILE = {enable: lane_file(enable) for enable in LANE_SOURCES}
"""The lane file each lane port writes: its register nibble lies 110 bits above the file's nibble."""
S_OPERANDS = {
    "s0": ("L0", "f37_40", "f41_44"),
    "s1": ("L1", "f45_48", "f49_52"),
    "s2": ("L2", "f53_56", "f57_60"),
    "s3": ("L3", "f61_64", "f65_68"),
}
"""Each S unit's lane file and the fields naming its operands a and b."""
LANE_UNIT = {
    S0_RESULT: "s0",
    S1_RESULT: "s1",
    S2_LOW: "s2",
    S2_RESULT: "s2",
    S2_HIGH: "s2",
    S3_RESULT: "s3",
}
"""The unit whose bus each lane port source takes."""
OWN_RULES = frozenset(
    {IDLE_OP, 0x08, 0x0B, 0x38, 0x44, 0x46, 0x47, 0x48, 0x49, *LOADS, *STORES}
)
"""The data ops without an `OpValue` rule that `Machine._op_slot` runs by its own rule."""


def _check_rules() -> None:
    """Every value of the X heads, the scalar ports' enables and O's source but the idle one,
    every lane port source and every data op with semantics has a rule here."""

    def values(name: str) -> set[int]:
        f = lookup(name)
        return set(range(1 << f.width)) - {f.idle}

    gaps = [
        *(f"{u} {h:#x}" for u in ("x0", "x1") for h in values(HEAD_IDS[u]) - set(X_RULES[u])),
        *(f"{e} {v}" for e in PR_ENABLES for v in values(e) - set(PR_SOURCES[e])),
        *(f"{e} {v}" for e in PB_FILES for v in values(e) - set(PB_SOURCES[e])),
        *(f"f252_254 {v}" for v in values("f252_254") - {*STORE_SOURCES, *STORE_NOTHING}),
        *(
            text
            for rows in (*LANE_SOURCES.values(), STORE_SOURCES)
            for text, _ in rows.values()
            if text != LANE_LOAD and text not in LANE_UNIT
        ),
        *(f"op {c:#x}" for c, s in OP_SEMANTICS.items() if s.rule is None and c not in OWN_RULES),
    ]
    if gaps:
        raise ValueError(f"no machine rule for {', '.join(gaps)}")


_check_rules()


@dataclass
class Carry:
    """What one job leaves to the next of the same boot: the lane memory and every register file
    (verified on the camera: a job read what the job before had written into G, R, A, B, F0..F3,
    the four lane files, U, O and a lane-memory vector), each file by its name. A program
    that runs between the two jobs changes what it writes: replaying it is the caller's part.

    Nothing clears the DSP's state between jobs: every register file, the lane memory and the
    local memory (`emu.Boot`) keep their content, except what the next job's own words write; a
    stock copy of n bytes overwrites lane-memory vectors 0..ceil(n / 32) - 1 and its own
    registers (verified on the camera, pre-registered). After power-up they hold SRAM content,
    stable within a boot and a few bits apart between boots (verified on the camera)."""

    lane_memory: dict[int, Vec] = field(default_factory=dict)
    registers: dict[str, list] = field(default_factory=dict)


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
    """The stream engine's side of X1's transfer heads (`stream.Engine`)."""

    cap: Confidence
    """The tier cap of what the engine writes from here on (`Machine.limit` lowers it)."""
    off: str
    """Why the engine went off script; empty while it follows the measured model."""

    def transfer(self, m: "Machine", index: int, head: int, value: Sv, t: int) -> None:
        """Transfer `head` of word `index`, executed word `t` of the job, hands `value`
        (B[f115_119]) to the engine."""

    def lane_tier(self, vector: int | None = None) -> Confidence:
        """The tier of the lane memory's content (at `vector`, or anywhere) for own words at the
        stream's position."""

    def finish(self, m: "Machine") -> None:
        """The job ends: what the engine still has to do completes."""


VECTOR_FILES = frozenset(("U", "O", *LANE_FILES))
Preset = int | Sv | Vec | Sequence[int | None]


def _preset(file: str, v: Preset) -> Sv | Vec:
    if file in VECTOR_FILES:
        vec = v if isinstance(v, Vec) else Vec(tuple(v))
        if len(vec.lanes) != LANE_COUNT:
            raise ValueError(f"{file} takes {LANE_COUNT} lanes, not {len(vec.lanes)}")
        return vec
    if isinstance(v, Sv):
        return v
    if not isinstance(v, int):
        raise TypeError(f"{file} takes an int or an Sv, not {v!r}")
    return Sv(v)


class BootState(Protocol):
    """What the machine and the stream engine use of the boot they run on (`emu.Boot`)."""

    local: Space
    dram: SparseSpace

    def source(self, name: str, fact: str = "") -> int: ...

    def read(self, addr: int, n: int) -> Bytes: ...


class Machine:
    """The datapath of one job on `boot` (its local memory is `boot.local`), with what an earlier
    job of the boot left (`carry`). It is the `datapath` of `emu.Executor` and decides the
    conditional control ops on its own register values (a `semantics.Branching`)."""

    def __init__(
        self,
        boot: BootState,
        carry: Carry | None = None,
        stream: "Stream | None" = None,
        trace: bool = False,
    ) -> None:
        self.boot = boot
        self.trace: dict[str, Unknown] | None = {} if trace else None
        """With `trace`: the first unknown write to each register ("L1[9]") and lane memory
        vector ("memory[0x12]") of the job by the datapath; the stream engine's writes to the
        lane memory are not traced. An unknown register without an entry was not written."""
        self.why_due: dict[int, dict[str, str]] = {}
        """With `trace`: why a bus's value is unknown, by word and source (a unit, a load)."""
        self.stream = stream
        self.lanes_lost = False
        self.g = [UNKNOWN] * 32
        self.a = [UNKNOWN] * 32
        self.b = [UNKNOWN] * 32
        self.r = [UNKNOWN] * 8
        self.f = {name: [UNKNOWN] * 16 for name in ("F0", "F1", "F2", "F3")}
        self.lanes = {name: [UNKNOWN_VEC] * 16 for name in LANE_FILES}
        self.u = [UNKNOWN_VEC] * 16
        self.o = [UNKNOWN_VEC] * 8
        self.memory: dict[int, Vec] = dict(carry.lane_memory) if carry else {}
        for name, regs in (carry.registers if carry else {}).items():
            self._file(name)[:] = regs
        self.bus_due: dict[int, list[tuple[Sv, str]]] = {}
        self.x1_due: dict[int, Sv] = {}
        """X1 0x1e's quotients on X1's result bus, by word."""
        self.load_due: dict[int, list[Vec]] = {}
        self.chains: dict[str, tuple[int, tuple[int, ...], bool]] = {}
        """The last ops in flight together by kind ("load", "divide"): the last one's word, the
        words between their heads, whether they begin a measured chain (`Machine._chained`)."""
        self.lane_due: dict[int, dict[str, list[tuple[Vec, Vec | None]]]] = {}
        self.held_due: dict[int, dict[str, Sv]] = {}
        self.reductions: list[tuple[int, Sv]] = []
        self.latched = {unit: Vec((0,) * LANE_COUNT, V) for unit in LATCH_OPERAND}
        """S0's / S1's latched operand (`LATCHED`): 0 at a job's start."""
        self.last_head: int | None = None
        """The word of the last S head run."""
        self.head_values: set[tuple[str, int]] = set()
        """The (unit, head) pairs that ran in that word."""
        self.prev_heads: tuple[int | None, frozenset[tuple[str, int]]] = (None, frozenset())
        """The word with S heads before that one and its (unit, head) pairs."""
        self.heads_run: dict[tuple[str, int], int] = {}
        """The runs so far in the job of each S0, S1 and S2 (unit, head) pair (`ALONE_ZEROS`)."""
        self.wedging_runs = 0
        """The words of the job so far with `WEDGING_S0` under M's strobe and port 272 / 2."""
        self.alone_runs: dict[tuple[str, int], int] = {}
        """Likewise the runs of `ALONE_ZEROS` heads in the form run on the camera."""
        self.alone: dict[str, tuple[int, int, int, bool, bool]] = {}
        """Each unit's last such run: its word, head, lag, whether it is the run whose zeros
        were read and whether its word had the port of `ALONE_PORTS`."""
        self._u_write: tuple[int, Vec] | None = None
        self.cap = V
        """The tier cap of all the job leaves and decides from here on (`limit`)."""
        self.ceiling = V
        """The tier cap of what the word being run leaves: `cap`, or a hypothesis from its
        lapse on."""
        self.open_m: dict[int, set[str]] = {}
        self.open_next: dict[int, dict[str, int]] = {}
        """M results not known one word after a head: by word, unit and head."""
        self.store_src = boot.source("local store", "an own program's family A store")
        self.strict = True
        """Whether a case the facts do not cover raises (set by `emu.Executor`), or lapses."""
        self.lapses: list[str] = []
        """Lenient: why each lapse of the job (`lapse`), in order."""

    def between_jobs(self) -> Carry:
        """The job's end: the stream's outstanding commands complete, and what it leaves."""
        if self.stream is not None:
            self.stream.finish(self)
        names = ("G", "A", "B", "R", *self.f, *self.lanes, "U", "O")
        registers = {name: list(self._file(name)) for name in names}
        return Carry({} if self.lanes_lost else dict(self.memory), registers)

    def open_end(self) -> str:
        """Why whether the job ends is not known, empty when its halt ends it: off script, the
        stream engine may hold a command that never completes."""
        if self.stream is None or not self.stream.off:
            return ""
        return f"the stream engine went off script: {self.stream.off}"

    def lose_lanes(self) -> None:
        """The lane memory is not known from now on (the stream engine's activity is not)."""
        self.lanes_lost = True
        self.memory.clear()

    def lapse(self, index: int, what: str, why: str, fields: Sequence = ()) -> None:
        """A case of word `index` the facts do not cover: raises `UnknownSemantics` when strict;
        lenient, it is recorded and all the job leaves and decides from here on is a hypothesis.
        The caller forgets what the case may write."""
        if self.strict:
            raise UnknownSemantics(index, what, list(fields), why)
        self.lapses.append(f"{what}: {why}")
        self.limit(H)
        self.ceiling = H

    def lose_memories(self, why: str) -> None:
        """The lane, local and DRAM memories are not known from here on (lenient): something
        whose reach is not bounded may have written them."""
        self.lose_lanes()
        src = self.boot.source("lapse", why)
        self.boot.local.forget(src)
        self.boot.dram.backing.clear()
        for page in self.boot.dram.pages.values():
            page.forget(src)

    def forget_job(self, why: str) -> None:
        """Words that ran but are not stepped (lenient): every memory is forgotten and every
        register is a hypothesis."""
        self.lose_memories(why)
        for name in ("G", "A", "B", "R", *self.f, *self.lanes, "U", "O"):
            regs = self._file(name)
            regs[:] = [_cap(v, H) for v in regs]

    def limit(self, tier: Confidence) -> None:
        """Cap all the job leaves and decides from here on at `tier`: the stepping follows a
        path the caller gave (`emu.Executor`'s branching)."""
        self.cap = min(self.cap, tier)
        if self.stream is not None:
            self.stream.cap = min(self.stream.cap, tier)

    def preset(self, regs: Mapping[str, Mapping[int, Preset]]) -> None:
        """Set registers by file and index, e.g. {"B": {3: 0xA0000000}}: an int or `Sv` in G, A,
        B, R and F0..F3, a `Vec` or its 32 lanes in L0..L3, U and O."""
        for file, values in regs.items():
            for reg, v in values.items():
                self._file(file)[reg] = _preset(file, v)

    # ------------------------------------------------------------------ branching

    def link(self, word: Word) -> tuple[int | None, Confidence]:
        """Op 4's target: the word G[f128_132] names (verified on the camera at r1 and r21)."""
        v = self.g[word["f128_132"]]
        return v.value, min(v.tier, self.cap)

    def decide(self, index: int, visit: int, word: Word) -> Branch:
        """ops 0..2 on G[ra] (verified on the camera by exact readout): brf iff ra == 0, br iff
        ra != 0, the loop end iff ra > 0 signed."""
        ra = self.g[word["ra"]]
        if ra.value is None:
            return Branch(None, f"r{word['ra']} unknown", V)
        v = ra.value
        taken = {1: v == 0, 2: v != 0, 0: s32(v) > 0}[word.op]
        return Branch(taken, f"r{word['ra']} = {v:#x}", min(ra.tier, self.cap))

    # ------------------------------------------------------------------ reads

    def _rd(self, file: str, reg: int) -> Sv:
        return self._file(file)[reg]

    # ------------------------------------------------------------------ the word

    def execute(self, index: int, word: Word, t: int) -> str:
        """Run word `word` (program index `index`) as executed word number `t`; returns a note."""
        self.ceiling = self.cap
        if word.control and word.op < 4 and word["f120_122"] & 3:
            # 0 or 4 in every stock control word; with 7 a word left its program on the camera.
            raise UnknownSemantics(
                index, op(word.op).name, [], "f120_122 bits 0..1 set in a control op"
            )
        notes: list[str] = []
        alu = self._op_slot(index, word, t, notes)
        bus = self._bus(word, alu, t)
        x0 = self._x(index, word, "x0", t)
        x1 = self._x(index, word, "x1", t)
        own = self._s_units(index, word, t)
        if word["f276"] == 0:
            if word[HEAD_IDS["s0"]] == WEDGING_S0 and word["f272_273"] == 2:
                self.wedging_runs += 1
                if self.wedging_runs >= WEDGING_S0_AT:
                    self.lapse(index, "s0 0x4f", WEDGING_S0_WHY)
            m = self._m(index, word, own, t)
            # An active X0 head's result is not on its bus under M (verified with a value on
            # M; with nothing on M the kept register measured beside `X0_BESIDE_KEPT` only).
            x0_head = word[HEAD_IDS["x0"]]
            x0bus = m
            if isinstance(m, Kept) and x0_head != lookup(HEAD_IDS["x0"]).idle:
                beside = (x0_head, self._heads_at(t)) == X0_BESIDE_KEPT
                x0bus = Kept(min(m.tier, V if beside else I))
        else:
            x0bus = UNKNOWN if x0 is None else x0
        x1bus = self._x1_bus(word[HEAD_IDS["x1"]], x1, t)
        writes = self._ports(index, word, t, bus, x0bus, x1bus)
        for key, value in writes.items():
            file, reg = key
            self._file(file)[reg] = _cap(value, self.ceiling)
        for due in (
            self.bus_due,
            self.x1_due,
            self.load_due,
            self.lane_due,
            self.held_due,
            self.open_m,
            self.open_next,
            self.why_due,
        ):
            due.pop(t, None)
        return "; ".join(notes)

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
            self.lapse(index, op(code).name, "no semantics", [(OP_FIELD, code)])
            self.boot.local.forget(self.store_src)  # the op slot stores there
            return UNKNOWN
        args = word.operands
        assert args is not None
        ra = self._rd("G", args.ra)
        rb = self._rd("G", args.rb) if args.rb is not None else None
        tier = min(sem.confidence, ra.tier, rb.tier if rb else V)
        if code in LOADS or code in STORES:
            self._local(index, word, code, ra, rb, t, notes)
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
        if code == 0x08:
            assert rb is not None
            return self._select(word, ra, rb, sem.confidence)
        if sem.rule is not None:
            rule = sem.rule
            ins = [v for name, v in (("a", ra), ("b", rb)) if name in rule.reads]
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
        return self._ring(index, word, code, ra, rb, c, tier)

    def _select(self, word: Word, ra: Sv, rb: Sv, tier: Confidence) -> Sv:
        """Op 0x08: ra where R[f120_122] is 0, else rb (measured on the camera by exact readout:
        R 0 gave ra at f120_122 0, 3, 5, 6 and 7, every non-zero R tried gave rb, also in a
        stock program's select). An unknown R gives an unknown result unless ra and rb agree."""
        r = word["f120_122"]
        x = self._rd("R", r)
        if r not in SELECT_MEASURED:
            tier = min(tier, I)
        if x.value is None:
            same = ra.value is not None and ra.value == rb.value
            return Sv(ra.value if same else None, min(tier, ra.tier, rb.tier, x.tier))
        pick = ra if x.value == 0 else rb
        return Sv(pick.value, min(tier, x.tier, pick.tier))

    def _ring(
        self, index: int, word: Word, code: int, ra: Sv, rb: Sv | None, c: int, tier: Confidence
    ) -> Sv:
        """The ring pointer step (`semantics.OP_SEMANTICS` 0x44 / 0x46 / 0x47)."""
        if code == 0x44:
            step = Sv(1)
        else:
            step = self._rd("R", word["f120_122"])
            step = Sv(None if step.value is None else s32(step.value), step.tier)
        length, ltier = (rb.value, V) if code == 0x47 else ring_length(c)
        if length is None and code != 0x47:
            self.lapse(index, op(code).name, f"ring length of imm13 {c:#x}: not measured")
        if step.value is None or length is None:
            return Sv(None, tier)
        s = ring_step(ra.value + step.value, length)
        return Sv(s & M32, min(tier, step.tier, ltier))

    def _local(
        self,
        index: int,
        word: Word,
        code: int,
        ra: Sv,
        rb: Sv | None,
        t: int,
        notes: list[str],
    ) -> None:
        """A family A access at ra + width x imm13, or at ra + width x rb in the register forms."""
        width = LOADS[code][0] if code in LOADS else STORES[code]
        by_register = code in LOCAL_LOADS_RB or code in LOCAL_STORES_RB
        offset = rb if by_register else Sv(word.operands.imm)
        if ra.value is None or offset.value is None:
            if code in STORES:
                self.lapse(index, op(code).name, "a local store at an unknown address")
            self._unknown_access(code, t)
            return
        ra = Sv(ra.value, min(ra.tier, offset.tier))
        addr = (ra.value + width * offset.value) % LOCAL_BYTES
        if addr % width:
            self.lapse(index, op(code).name, f"an unaligned local access at {addr:#x}")
            self._unknown_access(code, t)
            return
        if code in STORES:
            datum = self._rd("R", word["f120_122"])
            tier = min(datum.tier, ra.tier, self.ceiling)
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
        signed = LOADS[code][1]
        if all(b.known):
            v = int.from_bytes(b.data, "little")
            v = _sext(v, 8 * width) & M32 if signed else v
            datum = Sv(v, min(ra.tier, b.weakest()))
        else:
            datum = Sv(None, ra.tier)
        self.bus_due.setdefault(t + LOCAL_LOAD_LAG, []).append((datum, "load"))

    def _unknown_access(self, code: int, t: int) -> None:
        """A family A access at an address not known or not covered: a store forgets local
        memory, a load's datum is unknown."""
        if code in STORES:
            self.boot.local.forget(self.store_src)
        else:
            self.bus_due.setdefault(t + LOCAL_LOAD_LAG, []).append((Sv(None), "load"))

    def _bus(self, word: Word, alu: Sv | None, t: int) -> Sv | object:
        # Of the arrivals in an op 3 or op 4 word only a load's datum was measured.
        capped = word.control and word.op in (3, 4)
        parts = [
            _cap(v, I) if capped and kind != "load" else v
            for v, kind in self.bus_due.get(t, [])
        ]
        if alu is not None:
            parts.append(alu)
        if not parts:
            return NOTHING
        if len(parts) == 1:
            return parts[0]
        tier = min(v.tier for v in parts)
        if any(v.value is None for v in parts):
            return Sv(None, tier)
        value = 0
        for v in parts:
            value |= v.value
        return Sv(value, tier)

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
            select = word["f110_114"]
            if head == WAIT_HEAD and select in WAIT_NOTHING:
                self.limit(I)  # hands nothing (inferred): what follows depends on it
                return None
            if select != STREAM_SELECT:
                why = f"register a {select}: not measured (`STREAM_SELECT`)"
                self.lapse(index, f"x1 {head:#x}", why, [(lookup("f110_114"), select)])
                self.lose_memories(why)
                return None
            if self.stream is None:
                why = "a transfer to the stream engine: no stream"
                self.lapse(index, f"x1 {head:#x}", why)
                self.lose_memories(why)
                return None
            self.stream.transfer(self, index, head, b, t)
            return None
        if unit == "x1" and head == DIVIDE_HEAD:
            quotient = _divide(a, b)
            if not self._chained("divide", t, self.x1_due, DIVIDE_CHAINS, True):
                self.x1_due = {k: _cap(q, I) for k, q in self.x1_due.items()}
                quotient = _cap(quotient, I)
            self.x1_due[t + DIVIDE_LAG] = quotient
        if unit == "x0" and head in X0_LANE_MEMORY:
            self._lane_memory(index, word, head, a, b, k, t)
        rule = X_RULES[unit][head]
        if rule.value is None:
            return None
        ins = [v for name, v in (("a", a), ("b", b)) if name in rule.reads]
        tier = min([rule.confidence, *(v.tier for v in ins)])
        if any(v.value is None for v in ins):
            return Sv(None, tier)
        return Sv(x_result(unit, head, a.value or 0, b.value or 0, k), tier)

    def _x1_bus(self, head: int, x1: Sv | None, t: int) -> Sv | object:
        """X1's result bus in word t: the head's result, a quotient due (`fields.DIVIDE_LAG`) or
        their OR (measured on the camera beside X1 0x13 with an immediate, two words; other heads
        with a result inferred). A quotient is as alone beside an idle X1 or a divide (measured),
        inferred beside the other heads without a result."""
        quotient = self.x1_due.get(t)
        if quotient is None:
            if x1 is None and head == DIVIDE_HEAD:
                return NOTHING  # a divide drives nothing in its own word (verified)
            return UNKNOWN if x1 is None else x1
        if x1 is not None:
            return _or_bus(quotient, x1, head)
        if head in (lookup(HEAD_IDS["x1"]).idle, DIVIDE_HEAD):
            return quotient
        return _cap(quotient, I)

    def _lane_memory(
        self, index: int, word: Word, head: int, a: Sv, b: Sv, k: int, t: int
    ) -> None:
        by_reg = head in X0_LANE_BY_REGISTER
        flight = V
        if head in X0_LANE_LOADS and not self._chained(
            "load", t, self.load_due, LOAD_CHAINS, head == LOAD_CHAIN_HEAD
        ):
            for when, due in self.load_due.items():
                self.load_due[when] = [_cap(v, I) for v in due]
            flight = I
        if a.value is None or (by_reg and b.value is None):
            if head not in X0_LANE_LOADS:
                why = "a lane store at an unknown address"
                self.lapse(index, f"x0 {head:#x}", why)
                self.lose_lanes()
                return
            self.load_due.setdefault(t + LANE_LOAD_LAG, []).append(UNKNOWN_VEC)
            self._why(t + LANE_LOAD_LAG, LANE_LOAD, f"x0 {head:#x}: address not known")
            return
        addr = lane_address(head, a.value, b.value if b.value is not None else 0, k)
        tier = min(
            a.tier, b.tier if by_reg else V, X0_LANE_MEMORY[head][1], self.ceiling
        )
        tier = min(tier, flight)
        if self.stream is not None:
            tier = min(tier, self.stream.lane_tier(addr))
        if self.lanes_lost:
            if head in X0_LANE_LOADS:
                self.load_due.setdefault(t + LANE_LOAD_LAG, []).append(
                    Vec(UNKNOWN_VEC.lanes, H)
                )
                self._why(t + LANE_LOAD_LAG, LANE_LOAD, "the lane memory is lost")
            return
        if head in X0_LANE_LOADS:
            v = self.memory.get(addr, UNKNOWN_VEC)
            self.load_due.setdefault(t + LANE_LOAD_LAG, []).append(_cap(v, tier))
            if self.trace is not None and not _known(v):
                state = (
                    "not known"
                    if addr in self.memory
                    else "not written by the datapath"
                )
                self._why(t + LANE_LOAD_LAG, LANE_LOAD, f"memory[{addr:#x}] {state}")
            return
        reg = word["f69_71"]
        self.memory[addr] = _cap(self._rd("O", reg), tier)
        if self.trace is not None:
            source = f"x0 {head:#x}'s store of O[{reg}]"
            self._note(index, f"memory[{addr:#x}]", source, self.memory[addr], "")

    def _chained(
        self, kind: str, t: int, due: Mapping, chains: tuple, measured: bool
    ) -> bool:
        """Whether an op of `kind` in word t lands as it would alone beside the ops in flight
        (`due`: results due in word t or later, one landing in this word included): alone, or
        while every op of the chain is `measured` and the words between their heads, from the
        chain's first op on, begin one of `chains`. Otherwise each op in flight and this one land
        at inferred."""
        if not due:
            self.chains[kind] = (t, (), measured)
            return True
        last, gaps, ok = self.chains[kind]
        gaps += (t - last,)
        ok = ok and measured and any(c[: len(gaps)] == gaps for c in chains)
        self.chains[kind] = (t, gaps, ok)
        return ok

    # ------------------------------------------------------------------ S units

    def _s_units(self, index: int, word: Word, t: int) -> dict[str, Sv]:
        """Run the active S heads: lane results scheduled at their lag, held results scheduled;
        returns the own-word scalar results by unit."""
        own: dict[str, Sv] = {}
        for unit, (file, a_id, b_id) in S_OPERANDS.items():
            head = word[HEAD_IDS[unit]]
            if head == lookup(HEAD_IDS[unit]).idle:
                continue
            a, b = self._rd(file, word[a_id]), self._rd(file, word[b_id])
            scalars = self._scalars(unit, word)
            self._lane_head(index, unit, head, a, b, scalars, word, t)
            if unit != "s3":
                self.heads_run[unit, head] = self.heads_run.get((unit, head), 0) + 1
            if self.last_head != t:
                self.prev_heads = (self.last_head, frozenset(self.head_values))
                self.head_values = set()
            self.head_values.add((unit, head))
            self.last_head = t
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
                value = Sv(None, tier)
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
                "f31_34",
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
        if unit != "s3":
            layout = S_IMMEDIATES[unit]
            imm = Sv(layout.get(word.value), layout.confidence)
        if unit == "s0":
            return {
                "s": self._rd("F0", word["f72_75"]),
                "r": self._rd("F0", n["f76_79"]),
                "imm": imm,
            }
        if unit == "s1":
            return {
                "t": self._rd("F1", n["f84_87"]),
                "r": self._rd("F1", n["f80_83"]),
                "imm": imm,
            }
        if unit == "s2":
            return {
                "t": self._rd("F2", n["f88_91"]),
                "imm": imm,
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
        if unit in LATCH_OPERAND:
            if head in LATCHED:
                a = b = self.latched[unit]
            else:
                self._latch(unit, head, a if LATCH_OPERAND[unit] == "a" else b)
        rule = LANE_RULES[unit].get(head)
        zeros = self._alone_zeros(unit, head, word, t)
        if rule is None:
            # A head without a lane rule: its lane result is unknown at either lag (but for
            # `ALONE_ZEROS`), and so is M's result of its unit in those words unless the head
            # has a scalar rule (`S_RULES`), which then gives M.
            for when in (t, t + 1):
                if head not in S_RULES[unit]:
                    self.open_m.setdefault(when, set()).add(unit)
                if zeros:
                    continue
                unknown = Vec(UNKNOWN_VEC.lanes, H)
                self.lane_due.setdefault(when, {}).setdefault(unit, []).append(
                    (unknown, unknown)
                )
                self._why(when, unit, f"{unit} {head:#x} has no lane rule")
            if unit == "s3" and word["f232_233"] == 1:
                self._u_write = (word["f141_144"], Vec(UNKNOWN_VEC.lanes, H))
            return
        if rule.value is None:
            if unit == "s3" and word["f232_233"] == 1:
                self._u_port(index, head, b, word)
            return
        if zeros:
            return
        sc = _Scalars(scalars)
        ins = [v for name, v in (("a", a), ("b", b)) if name in rule.reads]
        high, why = None, ""
        if any(x is None for v in ins for x in v.lanes):
            low = UNKNOWN_VEC
            if self.trace is not None:
                why = self._unknown_operands(unit, head, word, a, b, rule.reads)
        else:
            al = [x or 0 for x in a.lanes]
            bl = [x or 0 for x in b.lanes]
            try:
                lanes = lane_result(unit, head, al, bl, sc)
                tier = min([lane_tier(unit, head, al, bl, sc), *(v.tier for v in ins)])
                if unit == "s3" and head in LANE_SHIFTS:
                    lanes, tier = self._shift_in(lanes, scalars, tier)
                low = Vec(tuple(lanes), min(tier, sc.tier))
                if unit == "s2" and head in S2_HIGH_HALVES:
                    hi = product_high(head, al, bl, sc)
                    htier = min(tier, S2_HIGH_HALVES[head][1], sc.tier)
                    high = Vec(tuple(hi), htier)
                if self.trace is not None and None in lanes:
                    why = f"{unit} {head:#x}: lanes not predicted"
            except UnknownInput as e:
                low = UNKNOWN_VEC
                why = f"{unit} {head:#x}: scalar {e.args[0]} not known"
        lag = LANE_LAG[unit].get(head)
        if lag is None:
            landings = [(t, H), (t + 1, H)]
        else:
            landings = [(t + lag[0], lag[1])]
        for when, ltier in landings:
            entry = (_cap(low, ltier), None if high is None else _cap(high, ltier))
            self.lane_due.setdefault(when, {}).setdefault(unit, []).append(entry)
            if why:
                self._why(when, unit, why)
        if unit == "s3" and word["f232_233"] == 1:
            self._u_port(index, head, b, word)

    def _alone_zeros(self, unit: str, head: int, word: Word, t: int) -> bool:
        """Records a run of an `ALONE_ZEROS` head in the form run on the camera, whose ports
        `_alone_read` serves; False for any other head, run or job."""
        heads, lag, read = ALONE_ZEROS.get(unit, ((), 0, 0))
        key = (unit, head)
        runs = self.heads_run.get(key, 0)
        if (
            head not in heads
            or set(self.heads_run) - {key}
            or runs > 1
            or self.alone_runs.get(key, 0) != runs
            # The previous run can still deliver: adjacent runs were not run.
            or (unit in self.alone and t - self.alone[unit][0] <= 1)
            or any(word[f] != v for f, v in ALONE_SELECTS[unit].items())
            or any(
                word[HEAD_IDS[u]] != lookup(HEAD_IDS[u]).idle
                for u in S_OPERANDS
                if u != unit
            )
        ):
            return False
        self.alone_runs[key] = runs + 1
        enable, v = ALONE_PORTS[unit]
        self.alone[unit] = (t, head, lag, runs == read, word[enable] == v)
        return True

    def _alone_read(self, unit: str, t: int, port: tuple[str, int] | None):
        """What `port` (None for another reader) takes of `unit`'s result in word t when it
        follows a run of `_alone_zeros`: the zeros at its lag, nothing at the other, each
        verified on the run it was read and inferred on the other, unknown in a form not run;
        None in any other word."""
        if unit not in self.alone or not 0 <= t - self.alone[unit][0] <= 1:
            return None
        start, head, lag, zeros_read, port_first = self.alone[unit]
        if (
            self.lane_due.get(t, {}).get(unit)
            or port != ALONE_PORTS[unit]
            or (t > start and port_first)
        ):
            self._why(t, unit, f"{unit} {head:#x}: read in a form not run")
            return Vec(UNKNOWN_VEC.lanes, H)
        if t - start != lag:
            return Kept(I if zeros_read else V)
        self._why(t, unit, f"{unit} {head:#x}: lanes 16..31 not read")
        lanes = tuple(0 if k in ALONE_ZERO_LANES else None for k in range(LANE_COUNT))
        return Vec(lanes, V if zeros_read else I)

    def _unknown_operands(
        self, unit: str, head: int, word: Word, a: Vec, b: Vec, reads: str
    ) -> str:
        file, a_id, b_id = S_OPERANDS[unit]
        if unit in LATCH_OPERAND and head in LATCHED:
            return f"{unit} {head:#x}: the latched operand not known"
        names = {"a": f"{file}[{word[a_id]}]", "b": f"{file}[{word[b_id]}]"}
        bad = [
            f"{k} = {names[k]}"
            for k, v in (("a", a), ("b", b))
            if k in reads and None in v.lanes
        ]
        return f"{unit} {head:#x}: operand {', '.join(bad)} not known"

    def _why(self, t: int, source: str, reason: str) -> None:
        """With `trace`: why `source`'s value in word t is unknown, the first reason kept."""
        if self.trace is not None:
            self.why_due.setdefault(t, {}).setdefault(source, reason)

    def _note(self, index: int, dest: str, source: str, value, reason: str) -> None:
        """With `trace`: `dest`'s first unknown write."""
        if (
            self.trace is None
            or dest in self.trace
            or value is NOTHING
            or isinstance(value, Kept)
            or _known(value)
        ):
            return
        self.trace[dest] = Unknown(index, source, reason)

    def _shift_in(
        self, lanes: list[int | None], scalars: dict[str, Sv], tier: Confidence
    ) -> tuple[list[int | None], Confidence]:
        """S3's lane shift: the lanes moved in come from U[f31_34]'s lanes 0..3, where the rule
        predicts the shift."""
        n = _sext(scalars["f92_95"].value, 4)
        if not 1 <= abs(n) <= 4 or all(x is None for x in lanes):
            return lanes, tier
        u = self._rd("U", scalars["f31_34"].value)
        out = list(lanes)
        if n > 0:
            out[LANE_COUNT - n :] = u.lanes[:n]
        else:
            out[:-n] = u.lanes[4 + n : 4]
        return out, min(tier, u.tier)

    def _latch(self, unit: str, head: int, operand: Vec) -> None:
        """What a head of S0 / S1 leaves for the unit's 0x60.. heads (`LATCHED`)."""
        rule = LANE_RULES[unit].get(head)
        if rule is None and head not in S_RULES[unit]:
            self.latched[unit] = Vec(UNKNOWN_VEC.lanes, H)
            return
        if unit == "s1" and rule is not None and "b" not in rule.reads:
            return
        tier = V if head in LATCH_MEASURED[unit] else I
        self.latched[unit] = Vec(operand.lanes, min(operand.tier, tier))

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
        several), else the unit's held result; S3's held one is a reduction's result, on M in its
        first word only. A held result of another unit is not on M (inferred), so beside one M's
        value is at most inferred."""
        sel = M_UNITS[word["f274_275"]]
        held = dict(self.held_due.get(t, {}))
        fresh = next((v for w, v in reversed(self.reductions) if w == t), None)
        if fresh is not None:
            held["s3"] = fresh
        # A reduction is on M under 3 beside another unit's held result (verified); beside the
        # heads of `M_TOGETHER` each held result is on M under its own unit alone (measured).
        others = any(u != sel for u in held if u != "s3") and not (
            self.last_head == t - 1 and self._together(sel, t)
        )
        tier = I if others and not (sel == "s3" and fresh is not None) else V
        if sel in self.open_m.get(t, ()):
            return Sv(None, H)
        before = self.open_next.get(t, {}).get(sel)
        # S2 0xc's extract and S2 0x1's product are not on M one word later: `S2_NEXT`.
        s2_next = sel == "s2" and (self._heads_at(t - 1), self._heads_at(t)) in S2_NEXT
        # Lane-only heads of several units: nothing on M one word later (inferred where not
        # measured, `M_TOGETHER`).
        lane_only = (
            self.last_head == t - 1
            and frozenset(self.head_values) in M_TOGETHER
            and self.head_values <= M_LANE_ONLY
        )
        if before is not None and not s2_next and not lane_only and not (
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
            # Another unit's held result is not on M here (verified one word after its head).
            return Kept(V if self._quiet(sel, t) else I)
        if len(own) > 1 and sel not in own and not self._quiet(sel, t):
            self.lapse(index, "m", f"own-word results of {sorted(own)}, M under {sel}")
            return UNKNOWN
        if sel not in own:
            # Another unit's own-word result is not on M (verified): nothing, or the sel unit's
            # held result alone (measured in the forms of `M_HELD_BESIDE_OWN`, else inferred).
            if h is None:
                return Kept(V if self._quiet(sel, t) else I)
            beside = (self._heads_at(t - 1), self._heads_at(t)) in M_HELD_BESIDE_OWN
            return _cap(h, tier if beside else I)
        v = own[sel]
        if sel == "s3":
            if fresh is None:
                # Beside a register older than its first word the extract comes alone
                # (verified: four, five, eight and twenty words after the reduction head).
                return _cap(v, tier)
            # A reduction's register on its first word, OR-ed with an extract's lane (verified).
            tier = min(v.tier, fresh.tier)
            if v.value is None or fresh.value is None:
                return Sv(None, tier)
            return Sv(v.value | fresh.value, tier)
        if h is not None:
            return Sv(
                None, H
            )  # an own-word result beside its unit's held one: not measured
        return _cap(v, tier)

    def _quiet(self, sel: str, t: int) -> bool:
        """Whether "nothing on M" was measured for this case: in every word from two after a
        head under every unit, also after a reduction (but its first word) with other heads
        between; with one unit's heads in the word, in the head's own word and the next under
        every unit (verified). Beside a head of `M_OTHER_OPEN` under another unit in its word and
        the next, with two units' heads in one word and with no head in the job, it is
        inferred."""
        if self.last_head is None:
            return False
        if t - self.last_head >= 2:
            return True
        if len({u for u, _ in self.head_values}) > 1:
            return self._together(sel, t)
        return not any(
            u != sel and h in M_OTHER_OPEN.get(u, ()) for u, h in self.head_values
        )

    def _together(self, sel: str, t: int) -> bool:
        """Whether M under `sel` in word t was measured beside the last word's heads of several
        units (`M_TOGETHER`)."""
        if self.last_head is None:
            return False
        cells = M_TOGETHER.get(frozenset(self.head_values), frozenset())
        return (t - self.last_head, sel) in cells

    def _heads_at(self, t: int) -> frozenset[tuple[str, int]]:
        """The (unit, head) pairs that ran in word t, for the last two words with S heads."""
        if self.last_head == t:
            return frozenset(self.head_values)
        return self.prev_heads[1] if self.prev_heads[0] == t else frozenset()

    # ------------------------------------------------------------------ ports

    def _ports(
        self, index: int, word: Word, t: int, bus, x0bus, x1
    ) -> dict[tuple[str, int], object]:
        writes: dict[tuple[str, int], object] = {}
        kept: dict[tuple[str, int], Confidence] = {}
        sources: dict[tuple[str, int], str] = {}

        def put(file: str, reg: int, value, source: str) -> None:
            key = (file, reg)
            if self.trace is not None:
                unit = LANE_UNIT.get(source, source)
                why = self.why_due.get(t, {}).get(unit, "")
                self._note(index, f"{file}[{reg}]", source, value, why)
            if key in sources:
                second(key, value, source)
                return
            sources[key] = source
            if isinstance(value, Kept):
                kept[key] = value.tier
            elif value is not NOTHING:
                writes[key] = value

        def second(key: tuple[str, int], value, source: str) -> None:
            """A second port on a register: the first port's value (`FIRST_PORT_WINS`)."""
            if key[0] not in LANE_FILES:
                self.lapse(index, "ports", f"two ports write {key[0]}[{key[1]}]")
                writes[key] = _unknown(key[0])
                return
            heads, due = self._heads_at(t), self.lane_due.get(t, {})
            carries = value is not NOTHING and not isinstance(value, Kept)
            if (
                key in writes
                and carries
                and (sources[key], source) == (S0_RESULT, S3_RESULT)
                and all(len(due.get(u, ())) == 1 and (u, h) in heads for u, h in FIRST_PORT_WINS)
            ):
                return
            if key not in writes and carries:
                writes[key] = value
            if key in writes:
                writes[key] = _cap(writes[key], I)
            if isinstance(value, Kept):
                kept[key] = min(kept.get(key, V), value.tier)
            if key in kept:
                kept[key] = min(kept[key], I)

        scalar = {"X0's result": x0bus, "X1's result": x1, ALU_BUS: bus}
        for enable, reg_id in PR_ENABLES.items():
            v = word[enable]
            if v == lookup(enable).idle:
                continue
            src = PR_SOURCES[enable][v]
            if src is not None:
                put(PR_FILES[enable], word[reg_id], scalar[src], src)
        for enable, file in PB_FILES.items():
            v = word[enable]
            if v == lookup(enable).idle:
                continue
            src = PB_SOURCES[enable][v]
            if src is not None:
                put(file, word[PORTS[enable]], scalar[src], src)
        for enable, file in LANE_PORT_FILE.items():
            v = word[enable]
            if v == lookup(enable).idle:
                continue
            src = LANE_SOURCES[enable].get(v)
            if src is None:
                continue  # wrote nothing from any S head (measured on the camera)
            value = _cap(self._lane_source(src[0], t, (enable, v)), src[1])
            put(file, word[PORTS[enable]], value, src[0])
        v = word["f252_254"]
        if v in STORE_NOTHING:
            kept[("O", word["f179_181"])] = V
        elif v != lookup("f252_254").idle:
            text, tier = STORE_SOURCES[v]
            put("O", word["f179_181"], _cap(self._lane_source(text, t), tier), text)
        # U's port writes under 1 only; with no S3 head (or one of U_NOTHING) nothing (verified).
        if word["f232_233"] == 1 and self._u_write is not None:
            put("U", *self._u_write, "S3's U port")
        self._u_write = None
        for (file, reg), tier in kept.items():
            if (file, reg) not in writes:
                writes[(file, reg)] = _cap(self._file(file)[reg], tier)
        return writes

    def _lane_source(self, text: str, t: int, port: tuple[str, int] | None = None):
        if text == LANE_LOAD:
            due = self.load_due.get(t, [])
            if len(due) > 1:
                self._why(t, LANE_LOAD, "two lane loads arrive in one word")
            return NOTHING if not due else due[0] if len(due) == 1 else UNKNOWN_VEC
        unit = LANE_UNIT[text]
        alone = self._alone_read(unit, t, port)
        if alone is not None:
            return alone
        due = self.lane_due.get(t, {}).get(unit, [])
        if not due:
            return NOTHING
        if text == S2_HIGH:
            now = [h for u, h in self._heads_at(t) if u == "s2"]
            timed = bool(now) and LANE_LAG["s2"].get(now[0]) == (0, V)
            if len(due) == 2 and due[0][1] is not None and due[1][1] is None and timed:
                # A product's high half beside a timed lag-0 S2 head without one: the high half
                # (measured on the camera beside S2 0x8 / 0xd moves of lanes whose high half, if
                # the move drove one, was zero: inferred).
                return _cap(due[0][1], I)
            if len(due) > 1:
                self._why(t, "s2", "two S2 high halves in one word")
                return Vec(
                    UNKNOWN_VEC.lanes, H
                )  # two high halves in one word: not measured
            return UNKNOWN_VEC if due[0][1] is None else due[0][1]
        low = due[0][0]
        for other, _ in due[1:]:
            # A lag-1 result and the next word's lag-0 result on one bus: OR-ed (verified).
            lanes = tuple(
                None if x is None or y is None else x | y
                for x, y in zip(low.lanes, other.lanes, strict=True)
            )
            low = Vec(lanes, min(low.tier, other.tier))
        return low

    def _file(self, file: str) -> list:
        scalar = {"G": self.g, "A": self.a, "B": self.b, "R": self.r, **self.f}
        vector = {"U": self.u, "O": self.o, **self.lanes}
        return scalar.get(file) or vector[file]
