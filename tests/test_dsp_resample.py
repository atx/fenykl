import base64
import json
import struct
from functools import cache
from pathlib import Path

import pytest
from conftest import stock_image

from fenykl.dsp import commands, resample_edits
from fenykl.dsp.emu import Boot, Emulator, Status
from fenykl.dsp.fields import Confidence
from fenykl.dsp.program import STOCK, Program, stock_items
from fenykl.dsp.resample import Resample13Model, footprint, predict
from fenykl.dsp.resample_edits import (
    C2_ONLY,
    GRID_A_ONLY,
    GRID_B_ONLY,
    GRID_C2,
    GRID_ONLY,
    OP_IDLE,
    Fact,
    FieldValue,
    Idle,
    bit,
    set_fields,
)
from fenykl.dsp.semantics import UnknownSemantics
from fenykl.dsp.taps import Taps
from fenykl.dsp.word import IDLE

VECTORS = Path(__file__).resolve().parent / "data" / "dsp" / "resample"
SRC, DST, TABLE = 0xA0FEAC00, 0xA1112000, 0xA0FE2800
"""The bench harness's buffers; the source image has 0x400 context bytes on each side."""
CTX = 0x400
CONF = {"V": Confidence.VERIFIED, "I": Confidence.INFERRED, "H": Confidence.HYPOTHESIS}

# Per job: the weakest taint of the dst bytes the job wrote, then the result's taint. The law is
# at most inferred; u0 held into [-16, 16) at b 2 and plane 0's line 1 at H 1 are hypotheses.
TAINTS = {
    "edit-flat": "II",
    "edit-phase": "II",
    "height-1": "HI",
    "own-gains": "II II",
    "own-phases": "II",
    "own-scale-2d": "II",
    "own-scale-flat": "II",
    "own-ties": "II",
    "stock-identity": "II",
    "stock-scale": "HI II II",
    "width-leak": "II II II",
}


@cache
def model() -> Resample13Model:
    return Resample13Model.from_image(stock_image("em10ii"))


def taps_of(entry: dict) -> Taps:
    if entry["kind"] == "firmware":
        spec = STOCK[13]
        assert entry["address"] == spec.dram + spec.const_offset
        return Taps.unpack(model().program.constants)
    return Taps.of(entry["phases"])


def edit_of(entry: dict) -> Idle:
    kinds = {k.name: k for k in (GRID_C2, GRID_ONLY, C2_ONLY)}
    name = entry["kind"]
    return kinds.get(name) or bit(int(name.split()[1]))


def run_job(emu: Emulator, job: dict, k: int):
    boot = emu.boot
    ctx = bytes([job["src"]["ctx"]]) * CTX
    boot.put(
        SRC - CTX, ctx + base64.b64decode(job["src"]["data"]) + ctx, f"job {k} src"
    )
    boot.put(
        TABLE, b"".join(struct.pack("<iI", *e) for e in job["table"]), f"job {k} table"
    )
    prefill = boot.put(
        DST, bytes([job["fill"]]) * footprint(job["h"]), f"job {k} prefill"
    )
    edits = [(job["edit"]["word"], edit_of(job["edit"]))] if "edit" in job else []
    rjob = model().job(taps_of(job["taps"]), SRC, DST, TABLE, job["w"], job["h"], edits)
    result = emu.run(rjob)
    assert [c for _, c in result.edits] == [Confidence.VERIFIED] * len(edits)
    return result, prefill


def run_vector(vector: dict) -> list[str]:
    bad = []
    emu = Emulator(Boot(), model())
    taints = TAINTS[vector["name"]].split()
    if len(taints) != len(vector["jobs"]):
        bad.append(f"{len(taints)} taints for {len(vector['jobs'])} jobs")
    for k, (job, taint) in enumerate(zip(vector["jobs"], taints)):
        result, prefill = run_job(emu, job, k)
        if result.status.name.lower() != job["expect"]["status"]:
            bad.append(f"job {k}: status {result.status.name}")
        if CONF[taint[1]] is not result.taint:
            bad.append(
                f"job {k}: result taint {result.taint.name}, expected {taint[1]}"
            )
        got = emu.boot.read(DST, footprint(job["h"]))
        written = [
            t for t, s, kn in zip(got.taint, got.src, got.known) if kn and s != prefill
        ]
        if not written or CONF[taint[0]] is not Confidence(min(written)):
            bad.append(
                f"job {k}: dst taint {min(written, default=None)}, expected {taint[0]}"
            )
        for seg in job["expect"]["dst"]:
            a, z = seg["at"], seg["end"]
            part = got[a:z]
            where = f"job {k} [{a:#x}, {z:#x}) {seg['kind']}"
            if seg["kind"] == "unknown":
                if any(part.known):
                    bad.append(f"{where}: {sum(part.known)} bytes known")
                continue
            if not all(part.known):
                bad.append(f"{where}: {len(part) - sum(part.known)} bytes unknown")
                continue
            want = base64.b64decode(seg["data"])
            if bytes(part.data) != want:
                n = sum(x != y for x, y in zip(part.data, want))
                bad.append(f"{where}: {n} bytes differ")
    return bad


@pytest.mark.parametrize("path", sorted(VECTORS.glob("*.json")), ids=lambda p: p.stem)
def test_camera_vector(path: Path):
    vector = json.loads(path.read_text())
    assert vector["name"] == path.stem
    assert run_vector(vector) == [], vector["fact"]


def test_every_vector_has_its_taints():
    assert sorted(TAINTS) == sorted(p.stem for p in VECTORS.glob("*.json"))


def test_the_vectors_hold_no_firmware_bytes():
    # No 16-byte window of program 13's item (its words and its taps) with 4 or more distinct
    # byte values is in a vector (fewer: zero runs with a stray byte, which images hold too).
    item = stock_items(stock_image("em10ii"))[13]
    windows = {
        item[o : o + 16]
        for o in range(len(item) - 15)
        if len(set(item[o : o + 16])) >= 4
    }
    for path in VECTORS.glob("*.json"):
        for job in json.loads(path.read_text())["jobs"]:
            blobs = [base64.b64decode(job["src"]["data"])]
            blobs += [
                base64.b64decode(s["data"]) for s in job["expect"]["dst"] if "data" in s
            ]
            if job["taps"]["kind"] == "phases":
                blobs.append(taps_of(job["taps"]).pack())
            for blob in blobs:
                assert not any(
                    blob[o : o + 16] in windows for o in range(len(blob) - 15)
                ), path.stem


def identity_job(w: int = 64, h: int = 16) -> dict:
    image = bytes((7 * i + 3) & 0xFF for i in range(3 * 64 * h))
    return {
        "w": w,
        "h": h,
        "fill": 0xE7,
        "taps": {"kind": "firmware", "address": 0xA324BC00},
        "table": [[i << 16, 0x10000] for i in range(h)],
        "src": {"ctx": 0x30, "data": base64.b64encode(image).decode()},
    }


def test_an_edited_program_has_no_semantics():
    words = list(model().program.words)
    words[439] = IDLE
    program = Program(tuple(words), model().program.constants)
    boot = Boot()
    job = commands.resample(program, SRC, DST, TABLE, 64, 64, 16)
    with pytest.raises(UnknownSemantics, match="word 439"):
        Emulator(boot, model()).run(job)
    result = Emulator(boot, model(), strict=False).run(job)
    assert result.taint is Confidence.HYPOTHESIS


def test_a_partial_block_on_a_fresh_boot_is_unknown():
    emu = Emulator(Boot(), model())
    run_job(emu, identity_job(w=48), 0)
    line = emu.boot.read(DST, 64)
    assert all(line.known[:32]) and not any(line.known[32:48])
    assert {emu.boot.sources[s].name for s in line.src[32:48]} == {"unwritten"}
    assert bytes(line.data[48:]) == b"\xe7" * 16


def test_a_partial_block_of_a_job_that_is_no_identity_is_unknown():
    # The held lines are known after the first job; the second reads source lines 2i.
    emu = Emulator(Boot(), model())
    run_job(emu, identity_job(), 0)
    job = identity_job(w=48)
    job["table"] = [[min(2 * i, 15) << 16, 0x10000] for i in range(16)]
    run_job(emu, job, 1)
    line = emu.boot.read(DST, 64)
    assert all(line.known[:32]) and not any(line.known[32:48])
    assert {emu.boot.sources[s].name for s in line.src[32:48]} == {"resampler partial"}


def test_an_odd_height_above_1_is_unknown():
    emu = Emulator(Boot(), model())
    result, _ = run_job(emu, identity_job(h=3), 0)
    assert result.status is Status.OK and result.stored == (DST, DST + footprint(3))
    assert not any(emu.boot.read(DST, footprint(3)).known)


# Per own-tap vector, the reasons the prediction gives for its unknown bytes.
REASONS = {
    "own-gains": set(),
    "own-phases": {"resampler bottom"},
    "own-scale-2d": {"resampler bottom", "resampler right"},
    "own-scale-flat": {"resampler bottom", "resampler right"},
    "own-ties": set(),
}


@pytest.mark.parametrize("name", sorted(REASONS))
def test_the_prediction_meets_the_own_tap_vectors(name: str):
    vector = json.loads((VECTORS / f"{name}.json").read_text())
    for k, job in enumerate(vector["jobs"]):
        got = predict(
            model(),
            taps_of(job["taps"]),
            base64.b64decode(job["src"]["data"]),
            [tuple(e) for e in job["table"]],
            job["w"],
        )
        masks = got.unknown()
        assert set(masks) == REASONS[name], k
        confidence = got.confidence()
        for i in range(len(got.dst)):
            assert (confidence[i] is None) == any(m[i] for m in masks.values())
        for seg in job["expect"]["dst"]:
            a, z = seg["at"], seg["end"]
            if seg["kind"] == "unknown":
                assert confidence[a:z] == [None] * (z - a), (k, a)
                continue
            assert None not in confidence[a:z], (k, a)
            assert bytes(got.dst.data[a:z]) == base64.b64decode(seg["data"]), (k, a)


def test_taps_outside_those_that_ran_are_a_hypothesis():
    # b 1, line i at phase i % 4. The stock program ran the whole s16 range: unit, the two s16
    # limits, one above the highest tap that ran with an edited program (2048).
    phases = [(0, 256, 0, 0)] * 16
    phases[1:4] = [(0, 0x7FFF, 0, 0), (0, 893, -0x8000, 0), (0, 2049, 0, 0)]
    image = bytes(range(64)) * 3 * 16
    table = [(i << 16 | i % 4 << 12, 0x10000) for i in range(16)]
    conf = predict(model(), Taps.of(phases), image, table).confidence()
    I, H = Confidence.INFERRED, Confidence.HYPOTHESIS
    assert H not in conf
    # An edited program ran taps in -1840..2048 only (a measured edit on its class).
    phases = list(PHASES)
    phases[1] = (0, 2049, 0, 0)
    got = predict(model(), Taps.of(phases), FLAT, TABLE13, edits=[(437, GRID_C2)])
    conf = got.confidence()
    assert set(conf[64:128]) == {H} and set(conf[192:256]) == {I}
    # b != 1 ran taps in -637..2048 only: plane 0's vertical pass at phase 0 reaches every pixel.
    phases = [(0, 256, 0, 0)] * 16
    phases[0] = (0, 4096, 0, 0)
    image = bytes(x % 16 for x in range(64)) * 3 * 16
    table = [(i << 16, 0x11000) for i in range(16)]
    conf = predict(model(), Taps.of(phases), image, table).confidence()
    assert set(conf[:1024]) == {H, None}
    assert H not in conf[1024:]


# b = 1 + 1/16 at H 2: output pixel 16 sits at horizontal phase 0 on source pixel 15, pixel 20
# at phase 4 on pixels 19 and 20, pixel 24 at phase 8 on pixels 23 and 24. Phase 0 (-2, 258) also
# runs down the lines.
LAW_TAPS = [(0, 256, 0, 0)] * 16
LAW_TAPS[0] = (-2, 258, 0, 0)
LAW_TAPS[4] = (0, 130, 126, 0)
LAW_TAPS[8] = (0, 128, 128, 0)
LAW_TABLE = [(0, 0x11000), (1 << 16, 0x11000)]


def law_image(line0: dict[int, int], line1: dict[int, int]) -> bytes:
    """Plane 0's two lines from {pixel: value}, the rest 0."""
    image = bytearray(3 * 64 * 2)
    for y, line in enumerate((line0, line1)):
        for x, v in line.items():
            image[64 * y + x] = v
    return bytes(image)


def test_the_two_pass_law_at_b_not_1():
    # A source constant down the lines, so the vertical pass gives 64 x pixel exactly (measured
    # on the camera at b = 1 + 1/16 with such sources and a non-unit phase 0).
    pixels = {14: 200, 15: 10, 19: 101, 20: 102, 23: 100, 24: 101}
    got = predict(model(), Taps.of(LAW_TAPS), law_image(pixels, pixels), LAW_TABLE)
    line = got.dst.data[64 + 16], got.dst.data[64 + 20], got.dst.data[64 + 24]
    # Horizontal phase 0 is the unit: pixel 15, not -2 x 200 + 258 x 10.
    # Phase 4: 130 x 101 + 126 x 102 = 25982 = 101.49 x 256 rounds once to 101 (R4 then R64:
    # 6495.5 -> 6496, 101.5 -> 102).
    # Phase 8: 128 x 100 + 128 x 101 = 100.5 x 256, a tie to even: 100.
    assert line == (10, 101, 100)
    assert got.confidence()[64 + 16] is Confidence.INFERRED


def test_plane_0_at_b_not_1_on_a_source_varying_down_the_lines():
    # Line 1, pixel 15: -2 x 38 + 258 x 101 = 25982; R4 gives 6496 (6495.5, ties to even), the
    # unit phase 0 then 6496 / 64 = 101.5 -> 102. This rounding of the vertical pass is a fit to
    # camera runs on such sources, not a designed measurement.
    got = predict(model(), Taps.of(LAW_TAPS), law_image({15: 38}, {15: 101}), LAW_TABLE)
    assert (got.dst.data[16], got.dst.data[64 + 16]) == (38, 102)
    assert got.confidence()[64 + 16] is Confidence.INFERRED


# A flat source (every line constant along x) with lines 7 i + 3, at b 1: line i at phase i.
FLAT = bytes((7 * (k // 64) + 3) & 0xFF for k in range(3 * 64 * 16))
PHASES = [(-8, 264, 0, 0)] * 16
PHASES[3] = (0, 0, 0, 256)
TABLE13 = [(i << 16 | (i % 16) << 12, 0x10000) for i in range(15)] + [
    (15 << 16, 0x10000)
]


def names(p, a: int, z: int) -> set[str]:
    return {p.sources[s].name for s in p.dst.src[a:z]}


def run_on(boot: Boot, taps: Taps, edits=()) -> bytes:
    """One flat job of TABLE13 on `boot`; its dst footprint with 0x100 for unknown bytes."""
    boot.put(SRC, FLAT, "src")
    boot.put(TABLE, b"".join(struct.pack("<iI", a, b) for a, b in TABLE13), "table")
    job = model().job(taps, SRC, DST, TABLE, 64, 16, edits)
    try:
        Emulator(boot, model()).run(job)
    except UnknownSemantics:
        boot.forget_held(boot.source("unmodelled job"))
    got = boot.read(DST, footprint(16))
    return bytes(v if k else 0 for v, k in zip(got.data, got.known)) + bytes(
        not k for k in got.known
    )


def plane0(dst: bytes) -> bytes | None:
    """Plane 0 of a `run_on` footprint, None if a byte of it is unknown."""
    n = footprint(16)
    return None if any(dst[n : n + 1024]) else dst[:1024]


def test_word_439s_grid_a_idle_reads_the_boots_tap0_substitute():
    taps = Taps.of(PHASES)
    drop = run_on(Boot(), Taps.of([(0, *p[1:]) for p in PHASES]))
    stock = run_on(Boot(), taps)
    boot = Boot()
    # Not known on a fresh boot; 433's grid B idle sets it to 0, which drops tap 0's product.
    assert plane0(run_on(boot, taps, [(439, GRID_A_ONLY)])) is None
    run_on(boot, taps, [(433, GRID_B_ONLY)])
    for kind in (GRID_A_ONLY, GRID_ONLY):
        got = run_on(boot, taps, [(439, kind)])
        assert plane0(got) == plane0(drop) != plane0(stock)
    # A keeper and a stock job leave it at 0.
    for edits in ([(439, FieldValue("f88_91", 2))], []):
        run_on(boot, taps, edits)
        assert plane0(run_on(boot, taps, [(439, GRID_A_ONLY)])) == plane0(drop)
    assert plane0(run_on(boot, taps, [(439, GRID_C2)])) == plane0(drop)
    # Another edit, measured or not, makes it unknown, and the mover does not bring it back.
    for edits in ([(433, GRID_C2)], [(433, OP_IDLE)]):
        boot = Boot()
        run_on(boot, taps, [(433, GRID_B_ONLY)])
        run_on(boot, taps, edits)
        assert plane0(run_on(boot, taps, [(439, GRID_A_ONLY)])) is None
        run_on(boot, taps, [(433, GRID_B_ONLY)])
        assert plane0(run_on(boot, taps, [(439, GRID_A_ONLY)])) is None
    # The mover acts on its class only: on a source varying along x it makes it unknown.
    boot = Boot()
    boot.put(SRC, bytes(range(64)) * 48, "src")
    Emulator(boot, model()).run(
        model().job(taps, SRC, DST, TABLE, 64, 16, [(433, GRID_B_ONLY)])
    )
    assert plane0(run_on(boot, taps, [(439, GRID_A_ONLY)])) is None


def test_words_440_and_441s_grid_a_idle_drop_their_tap_where_the_substitute_is_0():
    # Phase 0 (line 15) has no tap 2: it would read below the source.
    phases = [(0, 256, 0, 0)] + [(-8, 200, 64, 0)] * 15
    taps = Taps.of(phases)
    stock = run_on(Boot(), taps)
    for tap in (1, 2):
        drop = run_on(Boot(), Taps.of([(*p[:tap], 0, *p[tap + 1 :]) for p in phases]))
        boot = Boot()
        assert plane0(run_on(boot, taps, [(439 + tap, GRID_A_ONLY)])) is None
        run_on(boot, taps, [(433, GRID_B_ONLY)])
        got = run_on(boot, taps, [(439 + tap, GRID_A_ONLY)])
        assert plane0(got) == plane0(drop) != plane0(stock)
    # 442's is no drop; it keeps the substitute, as do the coefficient selects at 440..442.
    drop0 = run_on(Boot(), Taps.of([(0, *p[1:]) for p in phases]))
    boot = Boot()
    run_on(boot, taps, [(433, GRID_B_ONLY)])
    assert plane0(run_on(boot, taps, [(442, GRID_A_ONLY)])) is None
    for at, v in ((440, 3), (441, 2), (442, 4)):
        run_on(boot, taps, [(at, FieldValue("f88_91", v))])
    assert plane0(run_on(boot, taps, [(439, GRID_A_ONLY)])) == plane0(drop0)


def test_a_measured_edit_runs_its_law_on_the_class_it_ran_on():
    taps = Taps.of(PHASES)
    got = predict(model(), taps, FLAT, TABLE13, edits=[(439, GRID_C2)])
    stock = predict(model(), taps, FLAT, TABLE13)
    assert [c for _, c in got.result.edits] == [Confidence.VERIFIED]
    assert not any(got.dst.known[:2048]) and names(got, 0, 2048) == {"resampler edit"}
    assert got.dst.data[2048:] == stock.dst.data[2048:]
    # Word 437: line 3 has tap 3 only, which reads tap 0's line, a - 1.
    got = predict(model(), taps, FLAT, TABLE13, edits=[(437, GRID_C2)])
    assert set(got.dst.data[3 * 64 : 4 * 64]) == {FLAT[2 * 64]}
    assert got.result.taint is Confidence.INFERRED


def test_a_measured_edit_outside_its_classes_gives_no_prediction():
    # Word 439's edit never ran at b 0.5 on a flat source.
    table = [(i << 16, 0x8000) for i in range(16)]
    got = predict(model(), Taps.of(PHASES), FLAT, table, edits=[(439, GRID_C2)])
    assert not any(got.dst.known) and names(got, 0, len(got.dst)) == {"resampler edit"}
    assert "ran only on" in got.result.notes[-1] and got.result.edits == []
    got = predict(
        model(), Taps.of(PHASES), FLAT, table, edits=[(439, GRID_C2)], strict=False
    )
    assert set(got.confidence()) - {None} == {Confidence.HYPOTHESIS}
    # Two measured edits that the window does not cover never ran together.
    got = predict(
        model(), Taps.of(PHASES), FLAT, TABLE13, edits=[(439, GRID_C2), (490, GRID_C2)]
    )
    assert not any(got.dst.known)


def bits(*ranges: tuple[int, int]) -> int:
    return sum(((1 << (hi - lo + 1)) - 1) << lo for lo, hi in ranges)


def test_the_edit_kinds_are_the_bench_harness_encoding():
    # The harness sets the op bits to 1; the idle word holds 1 in every one of them.
    op = bits((265, 268), (272, 273), (302, 319))
    assert OP_IDLE.mask == op
    for w in model().program.words:
        assert OP_IDLE.apply(w).value == w.value | op
    # Grid A is 23..34 and 37..99; grid B repeats A's 31..95 at +110; together the grid.
    assert GRID_A_ONLY.mask == bits((23, 34), (37, 99))
    assert GRID_B_ONLY.mask == bits((141, 144), (147, 205))
    assert GRID_A_ONLY.mask | GRID_B_ONLY.mask == GRID_ONLY.mask
    # It sets register groups 115..119 and 211..215 whole, each one field of the table.
    w = model().program.words[437]
    for lo, v in ((115, 6), (211, 4)):
        got = FieldValue(f"f{lo}_{lo + 4}", v).apply(w).value
        assert got == w.value & ~bits((lo, lo + 4)) | v << lo
    with pytest.raises(KeyError):
        FieldValue("f115_118", 6).apply(w)
    with pytest.raises(ValueError):
        FieldValue("f211_215", 32).apply(w)


def test_a_field_value_edit_is_scored_on_its_classes_only():
    rename = FieldValue("f211_215", 4)
    stock_planes = (resample_edits.STOCK,) * 3
    fact = Fact(rename, (437,), {resample_edits.FLAT: stock_planes}, "")
    m = Resample13Model(model().program, (fact,))
    taps = Taps.of(PHASES)
    got = predict(m, taps, FLAT, TABLE13, edits=[(437, rename)])
    stock = predict(m, taps, FLAT, TABLE13)
    assert [c for _, c in got.result.edits] == [Confidence.VERIFIED]
    assert got.dst.known[:3072] == stock.dst.known[:3072]
    assert got.dst.data[:3072] == stock.dst.data[:3072]
    # At b 0.5 it never ran: unknown in strict mode, a hypothesis in lenient mode.
    table = [(i << 16, 0x8000) for i in range(16)]
    got = predict(m, taps, FLAT, table, edits=[(437, rename)])
    assert not any(got.dst.known) and got.result.edits == []
    got = predict(m, taps, FLAT, table, edits=[(437, rename)], strict=False)
    assert set(got.confidence()) - {None} == {Confidence.HYPOTHESIS}
    # Another value is no measured edit.
    with pytest.raises(UnknownSemantics, match="word 437"):
        predict(m, taps, FLAT, TABLE13, edits=[(437, FieldValue("f211_215", 9))])


def test_an_edited_tap_read_loads_another_coefficient():
    # Word 433 reads tap 1. Its imm 0 loads c0 instead (c1 := c0), op 0x10 the byte at offset 1
    # of the phase's taps, zero-extended (c1 := c0's bits 8..15): -8 gives 255, 384 gives 1.
    phases = [(-8, 264, 0, 0)] * 16
    phases[3] = (0, 0, 0, 256)
    phases[5] = (384, -128, 0, 0)
    imm0 = [(-8, -8, 0, 0)] * 16
    imm0[3], imm0[5] = (0, 0, 0, 256), (384, 384, 0, 0)
    byte1 = [(-8, 255, 0, 0)] * 16
    byte1[3], byte1[5] = (0, 0, 0, 256), (384, 1, 0, 0)
    stock = predict(model(), Taps.of(phases), FLAT, TABLE13)
    # Op 0x0e sign-extends that byte (-8 gives -1); with imm 0 0x10 and 0x0e take byte 0 (-8
    # gives 248 and -8, 384 gives 128 and -128), 0x14 the halfword c0; 0x1a takes c2's word.
    sext1 = [(-8, -1, 0, 0)] * 16
    sext1[3], sext1[5] = (0, 0, 0, 256), (384, 1, 0, 0)
    byte0 = [(-8, 248, 0, 0)] * 16
    byte0[3], byte0[5] = (0, 0, 0, 256), (384, 128, 0, 0)
    sext0 = [(-8, -8, 0, 0)] * 16
    sext0[3], sext0[5] = (0, 0, 0, 256), (384, -128, 0, 0)
    word1 = [(-8, 0, 0, 0)] * 16
    word1[3], word1[5] = (0, 0, 0, 256), (384, 0, 0, 0)
    loads = (
        (FieldValue("imm", 0), imm0),
        (FieldValue("op", 0x10), byte1),
        (FieldValue("op", 0x0E), sext1),
        (set_fields(op=0x10, imm=0), byte0),
        (set_fields(op=0x0E, imm=0), sext0),
        (set_fields(op=0x14, imm=0), imm0),
        (FieldValue("op", 0x1A), word1),
    )
    for edit, loaded in loads:
        got = predict(model(), Taps.of(phases), FLAT, TABLE13, edits=[(433, edit)])
        want = predict(model(), Taps.of(loaded), FLAT, TABLE13)
        assert [c for _, c in got.result.edits] == [Confidence.VERIFIED]
        assert got.dst.known[:1024] == want.dst.known[:1024]
        assert got.dst.data[:1024] == want.dst.data[:1024] != stock.dst.data[:1024]
        assert set(got.confidence()[:1024]) - {None} == {Confidence.INFERRED}
        assert got.dst.data[1024:] == stock.dst.data[1024:]


def test_words_439_to_442s_s2_operands_select_their_taps_line_and_coefficient():
    phases = [(-8, 264, 0, 0)] * 16
    phases[3] = (0, 0, 0, 256)
    phases[5] = (40, 100, 70, 46)
    taps = Taps.of(phases)
    stock = predict(model(), taps, FLAT, TABLE13)
    # f57_60 4 at 439: tap 0 multiplies tap 1's line with its own coefficient.
    merged = Taps.of([(0, c0 + c1, c2, c3) for c0, c1, c2, c3 in phases])
    # f88_91 3, 2, 4, 5 at word 439 + t: tap t takes c0, c1, c2, c3.
    loads = [(439, FieldValue("f57_60", 4), merged)] + [
        (
            439 + t,
            FieldValue("f88_91", v),
            Taps.of([(*p[:t], p[k], *p[t + 1 :]) for p in phases]),
        )
        for t in range(4)
        for k, v in enumerate((3, 2, 4, 5))
        if k != t
    ]
    for at, edit, want_taps in loads:
        got = predict(model(), taps, FLAT, TABLE13, edits=[(at, edit)])
        want = predict(model(), want_taps, FLAT, TABLE13)
        assert [c for _, c in got.result.edits] == [Confidence.VERIFIED], (at, edit)
        assert got.dst.known[:1024] == want.dst.known[:1024]
        assert got.dst.data[:1024] == want.dst.data[:1024] != stock.dst.data[:1024]
        assert got.dst.data[1024:] == stock.dst.data[1024:]
    # 0 and 1 at 439 select no coefficient, and 57 = 5 at 440 no line: plane 0 is unknown.
    for at, edit in (
        (439, FieldValue("f88_91", 0)),
        (439, FieldValue("f88_91", 1)),
        (440, FieldValue("f57_60", 5)),
    ):
        got = predict(model(), taps, FLAT, TABLE13, edits=[(at, edit)])
        assert [c for _, c in got.result.edits] == [Confidence.VERIFIED], (at, edit)
        assert not any(got.dst.known[:1024])
        assert got.dst.data[1024:] == stock.dst.data[1024:]
