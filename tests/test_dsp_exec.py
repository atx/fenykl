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
    GRID_A_ONLY,
    HELD_433,
    HELD_A_439,
    HELD_B_439,
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


def test_an_edited_load_off_the_tap_base_reaches_the_head_8_words():
    # The loads of 430 / 431 read off the tap base; 433 / 434 write their data to C4 / C5,
    # which 437 / 438 read without a semantic. An edit that changes what they load is unknown.
    for at, kind, reaches in (
        (430, FieldValue("imm", 5), "C4 reaches word 437"),
        (431, FieldValue("imm", 6), "C5 reaches word 438"),
        (430, FieldValue("op", 0x16), "C4 reaches word 437"),
        (431, FieldValue("ra", 6), "C5 reaches word 438"),
    ):
        got = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=[(at, kind)])
        assert not any(got.dst.known), (at, kind)
        assert got.result.edits == []
        assert reasons(got) == {("resampler operand", f"an edited value of {reaches}")}


def test_an_unknown_terms_bytes_name_its_reason():
    edits = [(439, FieldValue("f57_60", 7))]
    got = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits)
    assert reasons(got) == {("resampler operand", "R7 holds no value in the window")}


def reasons(got) -> set[tuple[str, str]]:
    """(name, fact) of the sources of the unknown bytes."""
    return {
        (got.sources[s].name, got.sources[s].fact)
        for s, k in zip(got.dst.src, got.dst.known)
        if not k
    }


def test_two_writes_to_one_register_in_one_step_raise():
    m = model()
    words = edited(438, FieldValue("f167_170", 2))
    with pytest.raises(UnknownSemantics, match="f163_166=0x2, f167_170=0x2 .*R2"):
        run_window(m.anchors, m.program.words, words, FLAT)
    # Lenient mode takes the stock law, as a hypothesis.
    edits = [(438, FieldValue("f167_170", 2))]
    with pytest.raises(UnknownSemantics, match="write conflict"):
        predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits)
    got = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits, strict=False)
    want = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE)
    assert any(got.dst.known) and got.dst.known == want.dst.known
    assert got.dst.data == want.dst.data
    assert set(got.confidence()) - {None} == {Confidence.HYPOTHESIS}


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
    # 57 = 4 with 88 = 5 at 439: tap 0 takes c3 and multiplies tap 1's line.
    merged = Taps.of([(0, c1 + c3, c2, c3) for _, c1, c2, c3 in COMPOSE])
    # 88 = 5 at 441 and 88 = 4 at 442: taps 2 and 3 swap their coefficients.
    swapped23 = Taps.of([(c0, c1, c3, c2) for c0, c1, c2, c3 in COMPOSE])
    for edits, want_taps in (
        ([(439, FieldValue("f88_91", 2)), (440, FieldValue("f88_91", 3))], swapped),
        ([(441, FieldValue("f88_91", 5)), (442, FieldValue("f88_91", 4))], swapped23),
        ([(439, set_fields(f57_60=4, f88_91=5))], merged),
    ):
        got = predict(model(), taps, FLAT_SRC, TABLE, edits=edits)
        want = predict(model(), want_taps, FLAT_SRC, TABLE)
        assert [c for _, c in got.result.edits] == [V, V], edits
        assert got.dst.known == want.dst.known
        assert got.dst.data == want.dst.data
    # A plane-0 tap and a chroma term: 88 = 3 at 440 with 88 = 2 at 444.
    edits = [(440, FieldValue("f88_91", 3)), (444, FieldValue("f88_91", 2))]
    got = predict(model(), taps, FLAT_SRC, TABLE, edits=edits)
    assert [c for _, c in got.result.edits] == [V, V]


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
    # 57 = 4 with 88 = 4 at 439 never ran: tap 0 takes c2 and multiplies tap 1's line.
    got = predict(
        model(), taps, FLAT_SRC, TABLE, edits=[(439, set_fields(f57_60=4, f88_91=4))]
    )
    want = predict(
        model(),
        Taps.of([(0, c1 + c2, c2, c3) for _, c1, c2, c3 in COMPOSE]),
        FLAT_SRC,
        TABLE,
    )
    assert [c for _, c in got.result.edits] == [I, I]
    assert got.dst.known == want.dst.known and got.dst.data == want.dst.data
    for edits in (
        [(439, FieldValue("f88_91", 4)), (440, FieldValue("f88_91", 3))],
        [(440, FieldValue("f88_91", 3)), (443, FieldValue("f88_91", 2))],
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


def test_a_row_word_with_a_window_edit_is_unknown():
    # Word 439's grid A idle has a row and no window cover; with 88 = 3 at 440 the job is unknown.
    edits = [(439, GRID_A_ONLY), (440, FieldValue("f88_91", 3))]
    got = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits)
    assert not any(got.dst.known) and got.result.edits == []
    assert set(got.unknown()) == {"resampler edit"}


@pytest.mark.parametrize("at", [10, 100, 428])
def test_a_held_field_outside_the_window_has_no_semantics(at: int):
    # The don't-care rule holds in the window only: elsewhere an edited held field under an
    # idle owner raises (strict) or is ignored at hypothesis (lenient).
    edits = [(at, FieldValue("f23_30", 0))]
    assert model().program.words[at]["f23_30"] != 0
    with pytest.raises(UnknownSemantics, match=f"word {at}"):
        predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits)
    got = predict(model(), Taps.of(COMPOSE), FLAT_SRC, TABLE, edits=edits, strict=False)
    assert got.result.ignored == [(at, ["f23_30"])]
    assert set(got.confidence()) - {None} == {Confidence.HYPOTHESIS}


def test_a_chroma_weight_beyond_the_ones_that_ran_is_a_hypothesis():
    # 88 = 3 at 443 weights plane 1's line a by c0: 2000 lies inside the plane-0 taps that ran
    # with an edited program, beyond the chroma weights that ran.
    src = bytes([255]) * (3 * 64 * 16)
    taps = Taps.of([(2000, 0, 0, 0)] * 16)
    got = predict(model(), taps, src, TABLE, edits=[(443, FieldValue("f88_91", 3))])
    conf = got.confidence()
    plane0, plane1 = conf[: 16 * 64], conf[16 * 64 : 32 * 64]
    assert set(plane0) == {I}
    assert set(plane1) == {Confidence.HYPOTHESIS}


def test_held_fields_are_verified_dont_cares_only_as_the_sets_that_ran():
    m = model()
    words = m.program.words

    def tiers(at: int, kind) -> set:
        word = kind.apply(words[at])
        covers = cover(m.anchors, words[at], word, at, FLAT)
        assert {c.coverage for c in covers} == {Coverage.DONT_CARE}, (at, kind)
        return {c.confidence for c in covers}

    for at, kind in (
        (433, HELD_433),
        (439, HELD_B_439),
        (439, HELD_A_439),
    ):
        assert tiers(at, kind) == {V}, (at, kind)
    # Part of a set that ran is inferred; a held field in no set that ran at the word, a
    # hypothesis.
    assert tiers(433, set_fields(f147_150=15, f151_154=15)) == {I}
    assert tiers(439, set_fields(f37_40=15)) == {I}
    for fid in ("f37_40", "f53_56", "f57_60", "f65_68", "f76_79", "f88_91"):
        assert tiers(433, FieldValue(fid, 15)) == {Confidence.HYPOTHESIS}, fid


# Chroma taps inside the weights that ran in a chroma sum; c0 and c2 make the sums leave 0..255.
CHROMA_TAPS = [(256 - 3 * p, -43 + p, 269 - 2 * p, 40) for p in range(16)]
SLOTS = {443: (1, 0), 444: (1, 1), 445: (2, 0), 447: (2, 1)}
"""The chroma term words: (plane, slot), slot 0 weighting line a, slot 1 line a + 1."""
MEASURED = {443: 3, 444: 2, 445: 5, 447: 4}
"""The map value measured at each chroma word, beside the 0 / 1 swap."""


def chroma_weight(v: int, taps: tuple[int, ...], p: int) -> int:
    """f88_91's weight: 0 = p, 1 = 16 - p, 3 / 2 / 4 / 5 = c0 / c1 / c2 / c3."""
    return {0: p, 1: 16 - p, 3: taps[0], 2: taps[1], 4: taps[2], 5: taps[3]}[v]


def test_chroma_word_weight_selects_give_the_wrapping_chroma_law():
    taps = Taps.of(CHROMA_TAPS)
    stock = predict(model(), taps, FLAT_SRC, TABLE)
    wrapped = False
    for at, (c, slot) in SLOTS.items():
        swap = 0 if slot == 0 else 1
        for v in (0, 1, 2, 3, 4, 5):
            if v == 1 - swap:
                continue  # the word's stock value
            got = predict(
                model(), taps, FLAT_SRC, TABLE, edits=[(at, FieldValue("f88_91", v))]
            )
            tier = V if v in (swap, MEASURED[at]) else I
            assert [t for _, t in got.result.edits] == [tier], (at, v)
            for plane in range(3):
                span = slice(64 * 16 * plane, 64 * 16 * (plane + 1))
                if plane == c:
                    continue
                assert got.dst.data[span] == stock.dst.data[span], (at, v, plane)
                assert got.dst.known[span] == stock.dst.known[span], (at, v, plane)
            for i, (a, _) in enumerate(TABLE):
                y, p = a >> 16, a >> 12 & 15
                w = [16 - p, p]
                w[slot] = chroma_weight(v, CHROMA_TAPS[p], p)
                line = 64 * (16 * c + i)
                known = got.dst.known[line : line + 64]
                if y == 15 and w[1]:
                    assert not any(known), (at, v, i)  # line a + 1 past the bottom
                    continue
                pixels = [(7 * (16 * c + min(y + k, 15)) + 3) & 0xFF for k in (0, 1)]
                s = w[0] * pixels[0] + w[1] * pixels[1]
                want = rne(s, 16) & 0xFF
                wrapped |= want != max(0, min(255, rne(s, 16)))
                assert all(known)
                assert got.dst.data[line : line + 64] == bytes([want]) * 64, (at, v, i)
                assert set(got.confidence()[line : line + 64]) == {I}
    # The sums left 0..255: a clamping stage would give other bytes.
    assert wrapped
