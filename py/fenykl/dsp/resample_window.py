"""Program 13's loop-head window on the field-level executor (`exec`), beside the model.

The window is words 430..447 at b = 1: the tap loads, the coefficient and operand register
writes and the eight S2 term words. What it takes from the stock region (the anchors):

- the element each operand register write port delivers per (word, port) [the contents
  verified at the reading words: luma and chroma impulse responses; that the nibble names the
  destination inferred at word 439, a hypothesis elsewhere]: plane 0 lines a - 1 .. a + 2,
  planes 1 and 2 lines a and a + 1;
- the accumulator of each S2 term word (439..442 plane 0, 443 / 444 plane 1, 445 / 447
  plane 2) and its output stage: plane 0 clamp(R64(R4(S))), the chroma planes the low byte of
  R16(S) [the wrap verified on the chroma planes];
- the tap base register (the base register of the stock tap reads) and the coefficient
  registers C0 = p and C1 = 16 - p at the window's entry (their writer is not located).

`Resample13Model` takes its b = 1 bytes from the window's term lists.
"""

from collections.abc import Sequence

from .exec import (
    COEFFICIENT_WRITE,
    HELD,
    PORT_WRITES,
    Anchors,
    FieldSemantic,
    PhaseWeight,
    Row,
    chroma_out,
    cover,
    field_is,
    is_load,
    luma_out,
    owner,
)
from .fields import Confidence, lookup
from .program import Program
from .resample_edits import (
    COEFFICIENT_SELECT,
    FACTS,
    FLAT,
    ONE,
    Fact,
    Idle,
    Stock,
)
from .word import Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

WINDOW = range(430, 448)
TAP_READ = 433
"""The tap-1 read, the word whose op-slot edits ran."""
TAP_WORDS = range(439, 443)
"""The S2 term words of taps 0..3."""
CHROMA_WORDS = (443, 444, 445, 447)
ELEMENTS = {
    (434, "f163_166"): Row(0, 0),
    (435, "f163_166"): Row(0, 1),
    (437, "f167_170"): Row(0, -1),
    (438, "f163_166"): Row(1, 0),
    (438, "f167_170"): Row(0, 2),
    (439, "f163_166"): Row(1, 1),
    (440, "f163_166"): Row(2, 0),
    (441, "f163_166"): Row(2, 1),
}
"""What each write port delivers; the ports of 436 and 437 that write R2 before 438 does are
not located."""
ROUTING = {**{i: 0 for i in TAP_WORDS}, 443: 1, 444: 1, 445: 2, 447: 2}


def _coefficient_off(index: int, values: tuple[int, ...]) -> Confidence | None:
    """f88_91 off its cells: the map's values at the chroma words inferred, 0 and 1 at the
    tap words no semantic (they read as the chroma weights, post hoc), 15 none."""
    (v,) = values
    if v == 15 or index in TAP_WORDS and v in (0, 1):
        return None
    return I if index in CHROMA_WORDS and v in COEFFICIENT_SELECT else H


def _register_off(index: int, values: tuple[int, ...]) -> Confidence | None:
    return None if values == (15,) else H


S2 = field_is("f292_295", 5)
SEMANTICS = (
    FieldSemantic(
        "load",
        ("op", "imm", "f128_132", "ra"),
        is_load,
        lambda index, values: H,
        "a family A load: the datum at 4 ra + width x imm reaches the load latch 3 words on "
        "(hypothesis); at the tap base register it is the phase's tap bytes",
    ),
    FieldSemantic(
        "f57_60",
        ("f57_60",),
        S2,
        _register_off,
        "the S2 term's operand: R[f57_60]",
    ),
    FieldSemantic(
        "f88_91",
        ("f88_91",),
        S2,
        _coefficient_off,
        "the S2 term's weight: C[f88_91]",
    ),
    FieldSemantic(
        "f53_56",
        ("f53_56",),
        S2,
        lambda index, values: None,
        "a don't-care of the S2 term at 1 and 2",
    ),
    *(
        FieldSemantic(
            fid,
            (fid,),
            field_is(enable, active),
            _register_off,
            "R[nibble] := the port's element, from the next word (destination a hypothesis)",
        )
        for fid, enable, active in PORT_WRITES
    ),
    FieldSemantic(
        COEFFICIENT_WRITE[0],
        (COEFFICIENT_WRITE[0],),
        field_is(*COEFFICIENT_WRITE[1:]),
        _register_off,
        "C[nibble] := the load latch, from the next word (hypothesis)",
    ),
)


def _cells(program: Program) -> dict[tuple[str, int, tuple[int, ...]], Confidence]:
    """The measured cells, on flat sources at b = 1, W 64: one run each, verified where the
    outcome was pre-registered, inferred for a post hoc fit."""
    read = program.words[TAP_READ]
    high, ra = read["f128_132"], read["ra"]
    loads = (
        (0x16, 0),
        (0x10, 1),
        (0x10, 0),
        (0x0E, 1),
        (0x0E, 0),
        (0x14, 1),
        (0x14, 0),
    )
    cells = {("load", TAP_READ, (op, imm, high, ra)): V for op, imm in loads}
    cells["load", TAP_READ, (0x1A, read["imm"], high, ra)] = V
    cells |= {("f88_91", i, (v,)): V for i in TAP_WORDS for v in COEFFICIENT_SELECT}
    cells |= {("f88_91", i, (v,)): V for i in CHROMA_WORDS for v in (0, 1)}
    cells |= {
        ("f88_91", i, (v,)): V for i, v in zip(CHROMA_WORDS, (3, 2, 5, 4), strict=True)
    }
    cells |= {("f57_60", 439, (4,)): V, ("f57_60", 440, (5,)): I}
    cells |= {("f57_60", i, (5,)): V for i in (441, 442)}
    cells["f53_56", 439, (1,)] = V
    return cells


def inert(facts: Sequence[Fact]) -> frozenset[int]:
    """The words whose S2 reads are inert at b = 1: idling bits that include f57_60 and f88_91
    changed no byte on any b = 1 class it ran on, flat sources among them."""
    reads = sum(lookup(k).mask << lookup(k).lo for k in ("f57_60", "f88_91"))
    out: set[int] = set()
    for fact in facts:
        if not isinstance(fact.kind, Idle) or fact.kind.mask & reads != reads:
            continue
        b1 = [planes for probe, planes in fact.planes.items() if probe.b == ONE]
        if FLAT in fact.planes and all(isinstance(e, Stock) for p in b1 for e in p):
            out.update(fact.words)
    return frozenset(out)


def anchors(program: Program, facts: Sequence[Fact] = FACTS) -> Anchors:
    """Program 13's window anchors; the tap base register is the stock tap read's."""
    return Anchors(
        words=WINDOW,
        elements=ELEMENTS,
        routing=ROUTING,
        stages={0: luma_out, 1: chroma_out, 2: chroma_out},
        phase_base=program.words[TAP_READ]["ra"],
        coefficients={0: PhaseWeight(False), 1: PhaseWeight(True)},
        semantics=SEMANTICS,
        cells=_cells(program),
        classes=frozenset({FLAT}),
        dont_care_words=frozenset({TAP_READ, 439}),
        inert=inert(facts),
    )


def free(anchors: Anchors, stock: Word) -> frozenset[str]:
    """The fields of a window word that the coverage rule may find changed: those of the
    semantics whose context holds in the stock word and the held fields whose owner is idle."""
    out = {
        lookup(k).id
        for sem in anchors.semantics
        if sem.context(stock)
        for k in sem.fields
    }
    for fid in HELD:
        own = owner(lookup(fid))
        if own is not None and own.get(stock.value) == own.idle:
            out.add(fid)
    return frozenset(out)


def uncovered(anchors: Anchors, stock: Word, word: Word, index: int) -> str:
    """Why a changed field of `word` at `index` is covered on no measured job class; "" if
    every one is."""
    why = ""
    for probe in anchors.classes:
        whys = [
            c.why
            for c in cover(anchors, stock, word, index, probe)
            if c.confidence is None
        ]
        if not whys:
            return ""
        why = whys[0]
    return why
