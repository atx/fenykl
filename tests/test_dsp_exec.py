from collections import Counter
from functools import cache

import pytest
from conftest import stock_image

from fenykl.dsp.exec import (
    Coverage,
    Row,
    TapBytes,
    Term,
    Unknown,
    UnknownTerm,
    Window,
    accumulate,
    chroma_out,
    cover,
    readers,
    run_window,
    weight,
)
from fenykl.dsp.fields import FIELDS, Confidence
from fenykl.dsp.resample import Resample13Model, predict
from fenykl.dsp.resample_edits import (
    FLAT,
    VARY,
    FieldValue,
    set_fields,
)
from fenykl.dsp.resample_window import WINDOW
from fenykl.dsp.semantics import UnknownSemantics
from fenykl.dsp.taps import Taps

I, V = Confidence.INFERRED, Confidence.VERIFIED


@cache
def model() -> Resample13Model:
    return Resample13Model.from_image(stock_image("em10ii"))


def rne(n: int, d: int) -> int:
    """n / d to nearest, ties to even (the law's R_d)."""
    q, r = divmod(n, d)
    return q + (2 * r > d or (2 * r == d and q % 2 == 1))


def law(c: int, taps: tuple[int, ...], p: int) -> dict[int, int]:
    """The law's kernel of plane c at phase p by line offset: plane 0 the phase's taps on lines
    a - 1 .. a + 2, the chroma planes (0, 256 - 16p, 16p, 0)."""
    kernel = taps if c == 0 else (0, 256 - 16 * p, 16 * p, 0)
    return {k - 1: t for k, t in enumerate(kernel) if t}


def kernel(terms, c: int, taps: tuple[int, ...], p: int) -> dict[int, int]:
    """The window's kernel of plane c by line offset, at the law's scale (the chroma stage
    divides by 16, the law's by 256)."""
    out: Counter[int] = Counter()
    for t in terms:
        assert t.operand.plane == c
        out[t.operand.dy] += weight(t.weight, taps, p) * (1 if c == 0 else 16)
    return {dy: w for dy, w in out.items() if w}


PHASES = [
    (-8, 264, 0, 0),
    (0, 0, 0, 256),
    (40, 100, 70, 46),
    (-300, 900, -200, -144),
    (0x7FFF, -0x8000, 1, -1),
    (17, -3, 255, 0),
    *[(p, 2 * p, -3 * p, 256 - p) for p in range(10)],
]


def edited(at: int, kind) -> list:
    words = list(model().program.words)
    words[at] = kind.apply(words[at])
    return words


def test_the_stock_window_gives_the_laws_kernels_for_every_phase_and_plane():
    m = model()
    acc = m.stock
    assert sorted(acc) == [0, 1, 2]
    for p, taps in enumerate(PHASES):
        for c in range(3):
            assert kernel(acc[c], c, taps, p) == law(c, taps, p), (p, c)
    assert {t.confidence for terms in acc.values() for t in terms} == {I}


def s8(b: int) -> int:
    return b - 256 if b > 127 else b


def tap(t: int, f):
    """Tap t of a phase's taps c replaced by f(c)."""
    return lambda c: tuple(f(c) if k == t else c[k] for k in range(4))


SELECT = (3, 2, 4, 5)
"""The f88_91 values selecting c0..c3 (word 439 + k's stock value selects c_k)."""
FIELD_EDITS = [
    # Word 433 reads tap 1: the load's width, sign and immediate pick the bytes.
    (433, FieldValue("imm", 0), tap(1, lambda c: c[0])),
    (433, FieldValue("op", 0x10), tap(1, lambda c: c[0] >> 8 & 0xFF)),
    (433, set_fields(op=0x10, imm=0), tap(1, lambda c: c[0] & 0xFF)),
    (433, FieldValue("op", 0x0E), tap(1, lambda c: s8(c[0] >> 8 & 0xFF))),
    (433, set_fields(op=0x0E, imm=0), tap(1, lambda c: s8(c[0] & 0xFF))),
    (433, FieldValue("op", 0x14), lambda c: c),
    (433, set_fields(op=0x14, imm=0), tap(1, lambda c: c[0])),
    (433, FieldValue("op", 0x1A), tap(1, lambda c: c[2])),
    # Word 439's operand 4 is tap 1's line, its f53_56 a don't-care.
    (439, FieldValue("f57_60", 4), lambda c: (0, c[0] + c[1], c[2], c[3])),
    (439, FieldValue("f53_56", 1), lambda c: c),
    *[
        (439 + t, FieldValue("f88_91", v), tap(t, lambda c, k=k: c[k]))
        for t in range(4)
        for k, v in enumerate(SELECT)
        if k != t
    ],
]
"""The measured single field edits of the window, with the plane-0 taps each gives."""


def test_the_window_gives_the_measured_field_edits_laws():
    assert len(FIELD_EDITS) == 22
    m = model()
    for at, kind, taps_of in FIELD_EDITS:
        w = run_window(m.anchors, m.program.words, edited(at, kind), FLAT)
        assert isinstance(w, Window) and not isinstance(w.acc, Unknown), (at, kind)
        for p, taps in enumerate(PHASES):
            assert kernel(w.acc[0], 0, taps, p) == law(0, taps_of(taps), 0), (
                at,
                kind,
                p,
            )
            for c in (1, 2):
                assert kernel(w.acc[c], c, taps, p) == law(c, taps, p)
        # Off the job classes the edits ran on, the window gives no terms.
        assert not isinstance(
            run_window(m.anchors, m.program.words, edited(at, kind), VARY), Window
        )


def test_coverage_census_of_the_stock_window():
    """Per stock window word: the fields with a semantic, don't-cares and anchored ones."""
    m = model()
    words = m.program.words
    census = {}
    for i in WINDOW:
        c = Counter(
            x.coverage for x in cover(m.anchors, words[i], words[i], i, None, FIELDS)
        )
        assert Coverage.UNKNOWN not in c
        census[i] = c[Coverage.SEMANTIC], c[Coverage.DONT_CARE], c[Coverage.ANCHORED]
    assert census == {
        430: (4, 33, 53),
        431: (4, 33, 53),
        432: (4, 33, 53),
        433: (5, 31, 54),
        434: (6, 31, 53),
        435: (6, 31, 53),
        436: (2, 31, 57),
        437: (3, 27, 60),
        438: (3, 26, 61),
        439: (4, 29, 57),
        440: (4, 28, 58),
        441: (4, 28, 58),
        442: (3, 25, 62),
        443: (3, 27, 60),
        444: (3, 21, 66),
        445: (3, 27, 60),
        446: (0, 24, 66),
        447: (3, 24, 63),
    }


def test_def_use_census_of_the_stock_program():
    m = model()
    reads = readers(m.anchors, m.program.words)
    inside = {item: [i for i in at if i in WINDOW] for item, at in reads.items()}
    # The window's words that read state without a semantic: the two head-8 words.
    assert {k: v for k, v in inside.items() if v} == {
        ("R", 4): [437],
        ("C", 4): [437],
        ("R", 1): [438],
        ("C", 5): [438],
    }
    # The unlocated elements (R2's writes at 436 and 437) and the loads off the tap base (C4
    # and C5's writes at 433 and 434) reach no term: every term is a coefficient x a pixel.
    terms = [t for terms in m.stock.values() for t in terms]
    assert all(
        isinstance(t.operand, Row) and not isinstance(t.weight, Unknown) for t in terms
    )


def test_an_edited_value_that_reaches_a_word_without_semantics_makes_the_window_unknown():
    m = model()
    words = m.program.words
    # Word 434's port writes R7 instead of R4: R4 still holds no value at 437's head-8 read.
    w = run_window(m.anchors, words, edited(434, FieldValue("f163_166", 7)), FLAT)
    assert isinstance(w, Window) and isinstance(w.acc, Unknown)
    assert "R4 reaches word 437" in w.acc.why
    # An off-cell load at 433 leaves C2 edited after the window, where other words read it.
    w = run_window(m.anchors, words, edited(433, FieldValue("imm", 2)), FLAT)
    assert isinstance(w, Window) and isinstance(w.acc, Unknown)
    assert "C2 reaches word" in w.acc.why
    # The measured load at 433 changes C2 too, but its cell's measurement covers the job.
    w = run_window(m.anchors, words, edited(433, set_fields(op=0x1A)), FLAT)
    assert isinstance(w, Window) and not isinstance(w.acc, Unknown)
    assert w.acc[0][1].weight == TapBytes(4, "<h")


def test_two_writes_to_one_register_in_one_step_raise():
    m = model()
    words = edited(438, FieldValue("f167_170", 2))
    with pytest.raises(UnknownSemantics, match="write conflict on R2"):
        run_window(m.anchors, m.program.words, words, FLAT)


def test_the_chroma_stage_equals_the_law_on_every_weight_and_pixel_pair():
    for p in range(16):
        for a in range(256):
            for b in range(256):
                t = (16 - p) * a + p * b
                s = (256 - 16 * p) * a + 16 * p * b
                assert chroma_out(t) == max(0, min(255, rne(rne(s, 4), 64))), (p, a, b)


def test_a_zero_weight_term_reads_no_operand():
    def read(row: Row) -> int:
        raise AssertionError(f"read {row}")

    zero = Term(TapBytes(0, "<h"), Row(0, 2), I)
    unknown = Term(TapBytes(0, "<h"), Unknown("not a pixel"), I)
    assert accumulate([zero, unknown], (0, 5, 0, 0), 0, read) == (
        0,
        Confidence.VERIFIED,
    )
    with pytest.raises(UnknownTerm, match="not a pixel"):
        accumulate([unknown], (3, 5, 0, 0), 0, read)
    lines = {0: 10, 1: 20}
    terms = [Term(TapBytes(2 * k, "<h"), Row(0, k), I) for k in range(2)]
    assert accumulate(terms, (3, -1, 0, 0), 0, lambda r: lines[r.dy]) == (10, I)


# A flat source: line k of the three planes' 48 lines holds 7 k + 3; line i < 15 at phase i.
FLAT_SRC = bytes((7 * (k // 64) + 3) & 0xFF for k in range(3 * 64 * 16))
TABLE = [(i << 16 | i << 12, 0x10000) for i in range(15)] + [(15 << 16, 0x10000)]
COMPOSE = [(-8, 264, 0, 0)] * 16
COMPOSE[3] = (0, 0, 0, 256)
COMPOSE[5] = (40, 100, 70, 46)


def test_edits_of_two_window_fields_compose_as_the_camera_showed():
    taps = Taps.of(COMPOSE)
    # 88 = 2 at 439 and 88 = 3 at 440: taps 0 and 1 swap their coefficients.
    swapped = Taps.of([(c1, c0, c2, c3) for c0, c1, c2, c3 in COMPOSE])
    # 57 = 4 with 88 = 4 at 439: tap 0 takes c2 and multiplies tap 1's line.
    merged = Taps.of([(0, c1 + c2, c2, c3) for _, c1, c2, c3 in COMPOSE])
    for edits, want_taps in (
        ([(439, FieldValue("f88_91", 2)), (440, FieldValue("f88_91", 3))], swapped),
        ([(439, set_fields(f57_60=4, f88_91=4))], merged),
    ):
        got = predict(model(), taps, FLAT_SRC, TABLE, edits=edits)
        want = predict(model(), want_taps, FLAT_SRC, TABLE)
        assert [c for _, c in got.result.edits] == [V, V], edits
        assert got.dst.known == want.dst.known
        assert got.dst.data == want.dst.data


def test_a_tap_term_reads_another_planes_line():
    def line(c: int, k: int) -> int:
        return (7 * (16 * c + max(k, 0)) + 3) & 0xFF

    # 57 = 5 with 88 = 5 at 441: tap 2 takes c3 and multiplies plane 1's line a + 1; 57 = 5
    # at 440 (a post hoc fit): tap 1 multiplies it, past the bottom on line 15.
    cases = (
        (
            [(441, set_fields(f57_60=5, f88_91=5))],
            lambda c, y: (
                c[0] * line(0, y - 1)
                + c[1] * line(0, y)
                + c[3] * (line(1, y + 1) + line(0, y + 2))
            ),
            [V, V],
            set(),
        ),
        (
            [(440, FieldValue("f57_60", 5))],
            lambda c, y: (
                c[0] * line(0, y - 1)
                + c[1] * line(1, y + 1)
                + c[2] * line(0, y + 1)
                + c[3] * line(0, y + 2)
            ),
            [I],
            {15},
        ),
    )
    for edits, law_of, tiers, bottom in cases:
        got = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits)
        assert [c for _, c in got.result.edits] == tiers
        for i, (a, _) in enumerate(TABLE):
            known = got.dst.known[64 * i : 64 * i + 64]
            assert all(known) if i not in bottom else not any(known)
            if i in bottom:
                continue
            s = law_of(COMPOSE[(a >> 12) & 15], a >> 16)
            want = max(0, min(255, rne(rne(s, 4), 64)))
            assert got.dst.data[64 * i : 64 * i + 64] == bytes([want]) * 64, (edits, i)
    # 57 = 15 has no semantic: the job is unmodelled.
    with pytest.raises(UnknownSemantics, match="word 441"):
        predict(
            model(),
            Taps.of(COMPOSE),
            FLAT_SRC,
            TABLE,
            edits=[(441, FieldValue("f57_60", 15))],
        )


def test_other_compositions_are_at_most_inferred():
    taps = Taps.of(COMPOSE)
    for edits in (
        [
            (439, FieldValue("f88_91", 2)),
            (440, FieldValue("f88_91", 3)),
            (441, FieldValue("f88_91", 2)),
        ],
        [(443, FieldValue("f88_91", 2)), (445, FieldValue("f88_91", 4))],
        [(439, FieldValue("f57_60", 4)), (440, FieldValue("f88_91", 3))],
    ):
        got = predict(model(), taps, FLAT_SRC, TABLE, edits=edits)
        assert {c for _, c in got.result.edits} == {I}, edits
