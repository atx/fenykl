"""A field-level executor for windows of a stock program: the machine state of the window, its
step loop with a latency queue, the coverage rule per (word, field) and symbolic values.

The executor does not run a program from word 0: it runs a window of words inside a stock region
whose contract stays the region's law (the frame: geometry, edges, trip counts). Per (word,
field) a coverage rule decides what runs: a field semantic, a don't-care (a held field whose
owner strobe or enable is idle in the word), the stock anchor (the stock value at its stock
index, part of the region's law) or a strict unknown. Window values are symbolic (`Row`,
`TapBytes`, `PhaseWeight`, `Unknown`); the accumulators hold term lists that the frame
evaluates per pixel at the window's output stage.

Within one word every unit reads the state as of the end of the previous executed word and
schedules its writes with a latency in executed words; no unit sees another's same-word result
[inferred: a word that writes an operand register and reads it in the same word gets the old
content]. Two writes to one target that fall due in one step raise `UnknownSemantics`.

Confidence is scoped: a field at its stock value and stock index has the law's tier (inferred)
on every job class; an edited value has the tier of its measured cell (verified for a
pre-registered confirmed cell, inferred for a post hoc fit) on the job classes it ran on, the
mechanism's tier off its cells, and no semantic (unknown) outside the job classes it ran on.
A job class can extend another under a cap: its own cells keep their tiers, the other class's
cells and mechanisms hold there at most at the cap.
"""

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from .fields import GRID_A, GRID_B, PORTS, Confidence, Field, lookup
from .resample_edits import Probe
from .semantics import UnknownSemantics, local_offset
from .units import UNITS
from .word import Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED
LAW = I
"""The tier of a stock value at its stock index: the region's law, measured from outside."""


# -- symbolic values


@dataclass(frozen=True)
class Row:
    """The source pixel of `plane` on line int(a) + dy, in the lane's column."""

    plane: int
    dy: int


@dataclass(frozen=True)
class TapBytes:
    """The struct `fmt` value at byte `offset` of the line's phase's 8 tap bytes (c0..c3, s16
    little-endian)."""

    offset: int
    fmt: str


def coef(k: int) -> TapBytes:
    """c_k of the line's phase."""
    return TapBytes(2 * k, "<h")


@dataclass(frozen=True)
class PhaseWeight:
    """p, the line's phase in 16ths, or 16 - p (`complement`): the chroma weights."""

    complement: bool


@dataclass(frozen=True)
class Unknown:
    """A value that is not known; `key` tells apart unknowns of one reason that may differ (a
    load's op, base register and offset)."""

    why: str
    key: tuple[int, ...] = ()


Value = Row | TapBytes | PhaseWeight | Unknown


@dataclass(frozen=True)
class Held:
    """A state item's content and its confidence."""

    value: Value
    confidence: Confidence


@dataclass(frozen=True)
class Term:
    """One product reaching an accumulator: weight x operand."""

    weight: Value
    operand: Value
    confidence: Confidence


# -- field semantics and the coverage rule


LOADS: dict[int, tuple[int, str]] = {
    0x1A: (4, "<h"),
    0x16: (2, "<h"),
    0x14: (2, "<h"),
    0x10: (1, "B"),
    0x0E: (1, "b"),
}
"""The family A loads: width and the format of the datum as a coefficient register takes it (a
word's low 16 bits: 0x1a in place of a halfword tap read gave the low half of the word)."""
S2_HEAD = "f292_295"
S2_TERM = 5
"""The S2 head value whose word adds C[f88_91] x R[f57_60] to its accumulator (the window
model's reading, hypothesis). On the camera with own programs head 5 is the signed b x t, b =
L2[f57_60], t = F2[f88_91]: its low half is the unit's lane result, its high half goes to S1's
file through the first port under enable 1, and S1's double-width shift narrows the pair
(`fields.S2_HIGH_HALVES`)."""


@dataclass(frozen=True)
class FieldSemantic:
    """A semantic of `fields` (read as one value) in words where `context` holds. `off(index,
    values)` is its tier off the measured cells, None where it has no semantic for that value."""

    name: str
    fields: tuple[str, ...]
    context: Callable[[Word], bool]
    off: Callable[[int, tuple[int, ...]], Confidence | None]
    fact: str

    def key(self, word: Word) -> tuple[int, ...]:
        return tuple(word[f] for f in self.fields)


def field_is(fid: str, value: int) -> Callable[[Word], bool]:
    return lambda word: word[fid] == value


def is_load(word: Word) -> bool:
    """A family A load the load semantic covers (f120_122 has no effect on a load)."""
    return word["op"] in LOADS


class Coverage(Enum):
    SEMANTIC = "semantic"
    DONT_CARE = "don't-care"
    ANCHORED = "anchored"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Cover:
    field: Field
    coverage: Coverage
    confidence: Confidence | None
    why: str = ""
    semantic: str = ""
    """The semantic that covers the field (its fields read as one value)."""


HELD = frozenset(
    f"f{lo}_{hi}" if lo != hi else f"f{lo}" for lo, hi in (*GRID_A, *GRID_B)
)
"""The held fields: the grid nibbles, each owned by a strobe or a port enable."""
_ENABLE = {owned: enable for enable, owned in PORTS.items()}


def owner(f: Field) -> Field | None:
    """The strobe or enable a held field changes under, None for other fields."""
    if f.id not in HELD:
        return None
    if f.id in _ENABLE:
        return lookup(_ENABLE[f.id])
    unit = UNITS[f.unit]
    return unit.strobes[0] if len(unit.strobes) == 1 else None


@dataclass(frozen=True)
class Anchors:
    """What a window takes from its stock region: its words, the element each write port
    delivers per (word, port field), the accumulator of each S2 term word, the op-slot register
    that holds the phase's tap base, the coefficient registers at the window's entry, the
    semantics with their measured cells per job class and the job classes (each None, or the
    class it extends and the cap on that class's tiers there), per word the sets
    of held fields measured don't-care together, and the stock words shown inert in the job's
    class (their reads do not count for the def-use rule); the output stage per
    accumulator."""

    words: range
    elements: Mapping[tuple[int, str], Value]
    routing: Mapping[int, int]
    stages: Mapping[int, Callable[[int], int]]
    phase_base: int
    coefficients: Mapping[int, Value]
    semantics: tuple[FieldSemantic, ...]
    cells: Mapping[tuple[Probe, str, int, tuple[int, ...]], Confidence]
    classes: Mapping[Probe, tuple[Probe, Confidence] | None]
    dont_cares: Mapping[int, tuple[frozenset[str], ...]]
    inert: frozenset[int]


def scoped(
    anchors: Anchors,
    sem: FieldSemantic,
    index: int,
    values: tuple[int, ...],
    stock: tuple[int, ...],
    probe: Probe | None,
) -> tuple[Confidence | None, str]:
    """The tier of `sem` at (index, values) on job class `probe`: the law's at the stock
    value, its measured cell's, else its tier off the cells; None (and why) where it has
    none."""
    if values == stock:
        return LAW, ""
    cell = measured(anchors, probe, (sem.name, index, values))
    off = sem.off(index, values)
    if probe not in anchors.classes:
        ran = any(
            (p, sem.name, index, values) in anchors.cells for p in anchors.classes
        )
        if not ran and off is None:
            return None, f"{sem.name} has no semantic for {values} at word {index}"
        return None, f"{sem.name} at word {index}: outside the measured job classes"
    if cell is None and off is None:
        return None, f"{sem.name} has no semantic for {values} at word {index}"
    return (cell if cell is not None else min(off, cap(anchors, probe))), ""


def cap(anchors: Anchors, probe: Probe | None) -> Confidence:
    """The cap on what holds on `probe` from the class it extends (none on its own)."""
    via = anchors.classes.get(probe)
    return V if via is None else via[1]


def measured(
    anchors: Anchors, probe: Probe | None, key: tuple[str, int, tuple[int, ...]]
) -> Confidence | None:
    """The tier of the cell `key` (semantic, index, values) on `probe`: its own, else the
    extended class's under the cap; None where neither ran."""
    own = anchors.cells.get((probe, *key))
    via = anchors.classes.get(probe)
    if own is not None or via is None:
        return own
    base = anchors.cells.get((via[0], *key))
    return None if base is None else min(base, via[1])


def cover(
    anchors: Anchors,
    stock: Word,
    word: Word,
    index: int,
    probe: Probe | None,
    fields: Sequence[Field] | None = None,
) -> list[Cover]:
    """The coverage at `index` of each of `fields` of `word` (default: those that differ from
    `stock`), by the rule's order: semantic, don't-care, anchored, unknown."""
    changed = {f.id for f, _ in word.changed(stock)}
    claimed: dict[str, FieldSemantic] = {}
    if index in anchors.words:
        for sem in anchors.semantics:
            if sem.context(word) and sem.context(stock):
                claimed.update({lookup(k).id: sem for k in sem.fields})
    held = [
        f
        for f, _ in word.changed(stock)
        if f.id not in claimed and _owner_idle(f, word)
    ]
    together = frozenset(f.id for f in held if f.get(word.value) == f.idle)
    if len(together) < len(held):
        together = frozenset()
    if fields is None:
        fields = [f for f, _ in word.changed(stock)]
    return [
        _cover_field(anchors, f, stock, word, index, probe, claimed, changed, together)
        for f in fields
    ]


def _owner_idle(f: Field, word: Word) -> bool:
    own = owner(f)
    return own is not None and own.get(word.value) == own.idle


def _cover_field(
    anchors: Anchors,
    f: Field,
    stock: Word,
    word: Word,
    index: int,
    probe: Probe | None,
    claimed: Mapping[str, FieldSemantic],
    changed: set[str],
    together: frozenset[str],
) -> Cover:
    """`together`: the word's changed held fields under idle owners if all are at their idle
    values, else empty. They are verified don't-care only as a set measured at the word."""
    sem = claimed.get(f.id)
    if sem is not None:
        tier, why = scoped(anchors, sem, index, sem.key(word), sem.key(stock), probe)
        kind = Coverage.UNKNOWN if tier is None else Coverage.SEMANTIC
        return Cover(f, kind, tier, why, sem.name)
    if _owner_idle(f, word):
        if f.id not in changed:
            return Cover(f, Coverage.DONT_CARE, LAW)
        if probe not in anchors.classes:
            return Cover(f, Coverage.UNKNOWN, None, "outside the measured job classes")
        sets = anchors.dont_cares.get(index, ())
        if together in sets:
            return Cover(f, Coverage.DONT_CARE, cap(anchors, probe))
        tier = I if any(f.id in s for s in sets) else H
        return Cover(f, Coverage.DONT_CARE, min(tier, cap(anchors, probe)))
    if f.id not in changed:
        return Cover(f, Coverage.ANCHORED, LAW)
    value = f.get(word.value)
    return Cover(
        f, Coverage.UNKNOWN, None, f"word {index}: {f.id} = {value:#x} has no semantic"
    )


# -- the machine


LATENCY = {"port": 1, "load": 3, "coefficient": 1, "term": 1}
"""Executed words from a write to the first read that sees it: a port write to an S2 read
(inferred; on the camera with own programs a load's write through f163_166 is read by S2 0x2 in
the next word, verified, pre-registered, f167_170's not measured), a load to the load latch
that f198_201 reads (hypothesis), f198_201's write to an f88_91 read (hypothesis; on the camera S2 head 0xe reads such a write from the next word on, and
the ALU bus carries a load's datum three words on), an S2 term to its accumulator (inferred post hoc on the chroma
planes)."""
PORT_WRITES = (("f163_166", "f244_245", 0), ("f167_170", "f246_247", 1))
"""The operand register write ports: (nibble, enable, the enable's active value). On the camera
with own programs these are the ports of S2's lane file L2, f167_170 under 1 writing S2's lane
result (a product's low half) and f163_166 under 0 the vector an X0 load head asked for four
words earlier (`fields.LANE_SOURCES`, `fields.LANE_MEMORY`); that program 13's loads there are
its tap lines is a hypothesis, and this executor does not model the lane memory."""
COEFFICIENT_WRITE = ("f198_201", "f261_262", 2)
"""The coefficient register write port: C[nibble] := the load latch (hypothesis). On the camera
with own programs this port under 2 writes the ALU bus, which carries a load's datum three words
on, to register f198_201 of the scalar file F2 that S2 head 0xe reads (inferred,
`fields.PB_SOURCES`); that C is F2 is a hypothesis."""

Item = tuple[str, int]
"""A state item: ("R", n) an operand register, ("C", n) a coefficient register, ("latch", 0)
the load latch."""
LATCH: Item = ("latch", 0)


def _name(item: Item) -> str:
    return "the load latch" if item == LATCH else f"{item[0]}{item[1]}"


@dataclass(frozen=True)
class Write:
    """A scheduled write: due at `step`, issued by field `by` of word `index`."""

    step: int
    index: int
    by: tuple[Field, int]
    target: Item | int
    """A state item, or an accumulator."""
    value: Held | Term


@dataclass
class Machine:
    """The window's state: the operand registers R0..R15 (per lane), the coefficient
    registers C0..C15, the load latch, the accumulators and the queue of scheduled writes.
    `tiers` gives the confidence per (word index, semantic name) of the edited fields; every
    other field runs at the law's tier."""

    anchors: Anchors
    tiers: Mapping[tuple[int, str], Confidence] = field(default_factory=dict)
    items: dict[Item, Held] = field(default_factory=dict)
    acc: dict[int, list[Term]] = field(default_factory=dict)
    queue: list[Write] = field(default_factory=list)
    step: int = 0
    seen: dict[int, dict[Item, Held]] = field(default_factory=dict)
    """Per word index, the state its units read."""

    def __post_init__(self) -> None:
        for n, value in self.anchors.coefficients.items():
            self.items["C", n] = Held(value, LAW)

    def read(self, item: Item) -> Held:
        held = self.items.get(item)
        if held is None:
            return Held(Unknown(f"{_name(item)} holds no value in the window"), LAW)
        return held

    def tier(self, index: int, name: str) -> Confidence:
        return self.tiers.get((index, name), LAW)

    def run(self, words: Sequence[Word]) -> dict[int, list[Term]]:
        for index in self.anchors.words:
            self.execute(words[index], index)
        while self.queue:
            self._advance()
        return self.acc

    def execute(self, word: Word, index: int) -> None:
        """One step: every unit reads the state as it is now, then the step's writes retire."""
        self.seen[index] = dict(self.items)
        writes: list[tuple[int, str, Item | int, Held | Term]] = []
        if is_load(word):
            datum = self._datum(word)
            held = Held(datum, self.tier(index, "load"))
            writes.append((LATENCY["load"], "op", LATCH, held))
        if word[S2_HEAD] == S2_TERM:
            if index not in self.anchors.routing:
                raise UnknownSemantics(
                    index,
                    "window",
                    [(lookup(S2_HEAD), S2_TERM)],
                    "an S2 term word with no accumulator",
                )
            r, c = self.read(("R", word["f57_60"])), self.read(("C", word["f88_91"]))
            tier = min(self.tier(index, n) for n in ("f53_56", "f57_60", "f88_91"))
            term = Term(c.value, r.value, min(r.confidence, c.confidence, tier))
            writes.append((LATENCY["term"], S2_HEAD, self.anchors.routing[index], term))
        for fid, enable, active in PORT_WRITES:
            if word[enable] != active or word[fid] == 15:
                continue
            element = self.anchors.elements.get(
                (index, fid),
                Unknown(f"the element of word {index}'s port {fid} is not located"),
            )
            held = Held(element, self.tier(index, fid))
            writes.append((LATENCY["port"], fid, ("R", word[fid]), held))
        fid, enable, active = COEFFICIENT_WRITE
        if word[enable] == active and word[fid] != 15:
            latch = self.read(LATCH)
            held = Held(latch.value, min(latch.confidence, self.tier(index, fid)))
            writes.append((LATENCY["coefficient"], fid, ("C", word[fid]), held))
        self.queue += [
            Write(self.step + lat, index, (lookup(f), word[f]), target, v)
            for lat, f, target, v in writes
        ]
        self._advance()

    def _datum(self, word: Word) -> Value:
        """What a family A load returns: the phase's tap bytes at the tap base register."""
        width, fmt = LOADS[word["op"]]
        args = word.operands
        assert args is not None and args.imm is not None
        offset = local_offset(width, args.imm)
        key = (word["op"], word["ra"], offset)
        if word["ra"] != self.anchors.phase_base:
            return Unknown(f"r{word['ra']} holds no known base in the window", key)
        if not 0 <= offset <= 8 - width:
            return Unknown("a load beyond the phase's 8 tap bytes", key)
        return TapBytes(offset, fmt)

    def _advance(self) -> None:
        self.step += 1
        due = [q for q in self.queue if q.step == self.step]
        self.queue = [q for q in self.queue if q.step != self.step]
        written: dict[Item, Write] = {}
        for q in due:
            if isinstance(q.target, int):
                assert isinstance(q.value, Term)
                self.acc.setdefault(q.target, []).append(q.value)
                continue
            first = written.get(q.target)
            if first is not None:
                raise UnknownSemantics(
                    q.index,
                    "window",
                    [first.by, q.by],
                    f"write conflict on {_name(q.target)} (words {first.index}, {q.index})",
                )
            assert isinstance(q.value, Held)
            written[q.target] = q
            self.items[q.target] = q.value


def readers(anchors: Anchors, words: Sequence[Word]) -> dict[Item, list[int]]:
    """The def-use census: per state item the words of `words` that read it without a
    semantic (an S2 read outside the window's term words, a coefficient write outside the
    window)."""
    out: dict[Item, list[int]] = {}
    idle = lookup(S2_HEAD).idle
    fid, enable, active = COEFFICIENT_WRITE
    for index, word in enumerate(words):
        inside = index in anchors.words
        if word[S2_HEAD] != idle and (word[S2_HEAD] != S2_TERM or not inside):
            out.setdefault(("R", word["f57_60"]), []).append(index)
            out.setdefault(("C", word["f88_91"]), []).append(index)
        if not inside and word[enable] == active and word[fid] != 15:
            out.setdefault(LATCH, []).append(index)
    return out


@dataclass(frozen=True)
class Uncovered:
    """A job the executor gives no terms for: a changed field has no semantic or lies outside
    the window."""

    why: str


@dataclass(frozen=True)
class Window:
    """The window's term lists per accumulator, or why every accumulator is unknown, and the
    covers of the job's changed fields by word index."""

    acc: Mapping[int, Sequence[Term]] | Unknown
    edits: tuple[tuple[int, Cover], ...] = ()


def run_window(
    anchors: Anchors, stock: Sequence[Word], words: Sequence[Word], probe: Probe | None
) -> Window | Uncovered:
    """The window of `words` (the stock program `stock`, edited) on job class `probe`.

    Every changed field must be covered (a semantic or a don't-care), in any number of words.
    Unless the job edits one semantic at a measured cell, an edited value that reaches a word
    reading it without a semantic (`readers`), before it is overwritten in the window or after
    it, makes every accumulator unknown; words in `anchors.inert` do not count."""
    tiers: dict[tuple[int, str], Confidence] = {}
    edits: list[tuple[int, Cover]] = []
    one_cell = True
    for index, (s, w) in enumerate(zip(stock, words, strict=True)):
        if s == w:
            continue
        if index not in anchors.words:
            return Uncovered(f"word {index} lies outside the window")
        for c in cover(anchors, s, w, index, probe):
            if c.confidence is None:
                return Uncovered(c.why)
            edits.append((index, c))
        for sem in anchors.semantics:
            if not (sem.context(w) and sem.context(s)) or sem.key(w) == sem.key(s):
                continue
            tier, _ = scoped(anchors, sem, index, sem.key(w), sem.key(s), probe)
            assert tier is not None
            tiers[index, sem.name] = tier
            cell = (sem.name, index, sem.key(w))
            one_cell &= measured(anchors, probe, cell) is not None
    edited = Machine(anchors, tiers)
    acc = edited.run(words)
    if one_cell and len(tiers) <= 1:
        return Window(acc, tuple(edits))
    base = Machine(anchors)
    base.run(stock)
    reads = readers(anchors, stock)
    # One pass suffices across the loop's back edge: 433 / 434 rewrite C4 / C5 and 434 / 435
    # rewrite R4 / R1 before 437 / 438 read them, so no earlier iteration's value reaches them.
    for item, at in reads.items():
        for index in at:
            if index in anchors.inert:
                continue
            if index in anchors.words:
                old, new = base.seen[index].get(item), edited.seen[index].get(item)
            else:
                old, new = base.items.get(item), edited.items.get(item)
            if (old and old.value) != (new and new.value):
                return Window(
                    Unknown(f"an edited value of {_name(item)} reaches word {index}"),
                    tuple(edits),
                )
    return Window(acc, tuple(edits))


# -- the output stage


class UnknownTerm(Exception):
    """A term with a nonzero or unknown weight whose weight or operand is not known."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def weight(value: Value, taps: Sequence[int] | None, p: int) -> int | None:
    """A term's weight at phase p with the phase's taps c0..c3; None if it is not known."""
    if isinstance(value, TapBytes) and taps is not None:
        return struct.unpack_from(value.fmt, struct.pack("<4h", *taps), value.offset)[0]
    if isinstance(value, PhaseWeight):
        return 16 - p if value.complement else p
    return None


def accumulate(
    terms: Sequence[Term],
    taps: Sequence[int] | None,
    p: int,
    read: Callable[[Row], int],
) -> tuple[int, Confidence]:
    """The sum of weight x operand over `terms` at phase p and the weakest confidence of the
    terms it used. A term of weight 0 is dropped before its operand is read; `read` reads a
    source pixel (and raises where the frame gives none)."""
    s, conf = 0, V
    for t in terms:
        w = weight(t.weight, taps, p)
        if w == 0:
            continue
        if w is None:
            raise UnknownTerm(getattr(t.weight, "why", "a weight that is not known"))
        if not isinstance(t.operand, Row):
            raise UnknownTerm(
                getattr(t.operand, "why", "an operand that is not a pixel")
            )
        s += w * read(t.operand)
        conf = min(conf, t.confidence)
    return s, conf


def rhe(n: int, d: int) -> int:
    """n / d rounded to nearest, ties to even."""
    q, r = divmod(n, d)
    return q + (1 if 2 * r > d or (2 * r == d and q & 1) else 0)


def out_of(s: int) -> int:
    """clamp(R64(R4(S)), 0, 255), ties to even: the output byte of a sum of tap x pixel, the
    plane-0 output stage."""
    return max(0, min(255, rhe(rhe(s, 4), 64)))


def chroma_out(s: int) -> int:
    """The low byte of R16(S), ties to even (no clamp): the chroma output stage."""
    return rhe(s, 16) & 0xFF
