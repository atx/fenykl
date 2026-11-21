import base64
import json
import struct
from functools import cache
from pathlib import Path

import pytest
from conftest import stock_image

from fenykl.dsp import commands
from fenykl.dsp.emu import Boot, Emulator, Status
from fenykl.dsp.fields import Confidence
from fenykl.dsp.program import STOCK, Program, stock_items
from fenykl.dsp.resample import Resample13Model, footprint, predict
from fenykl.dsp.resample_edits import C2_ONLY, GRID_C2, GRID_ONLY, Idle, bit
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
    "own-scale-2d": {"resampler 2d", "resampler bottom", "resampler right"},
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
    # b 1, line i at phase i % 4: unit, 4096, the lowest tap that ran (-637), one below it.
    phases = [(0, 256, 0, 0)] * 16
    phases[1:4] = [(0, 4096, 0, 0), (0, 893, -637, 0), (0, 894, -638, 0)]
    image = bytes(range(64)) * 3 * 16
    table = [(i << 16 | i % 4 << 12, 0x10000) for i in range(16)]
    conf = predict(model(), Taps.of(phases), image, table).confidence()
    line = [set(conf[64 * i : 64 * i + 64]) for i in range(4)]
    I, H = Confidence.INFERRED, Confidence.HYPOTHESIS
    assert line == [{I}, {H}, {I}, {H}]
    assert Confidence.HYPOTHESIS not in conf[1024:]
    # b != 1: plane 0's vertical pass at phase 0 reaches every pixel; a gain of 16 on values
    # below 16 keeps it exact.
    phases = [(0, 256, 0, 0)] * 16
    phases[0] = (0, 4096, 0, 0)
    image = bytes(x % 16 for x in range(64)) * 3 * 16
    table = [(i << 16, 0x11000) for i in range(16)]
    conf = predict(model(), Taps.of(phases), image, table).confidence()
    assert set(conf[:1024]) == {Confidence.HYPOTHESIS, None}
    assert Confidence.HYPOTHESIS not in conf[1024:]


# A flat source (every line constant along x) with lines 7 i + 3, at b 1: line i at phase i.
FLAT = bytes((7 * (k // 64) + 3) & 0xFF for k in range(3 * 64 * 16))
PHASES = [(-8, 264, 0, 0)] * 16
PHASES[3] = (0, 0, 0, 256)
TABLE13 = [(i << 16 | (i % 16) << 12, 0x10000) for i in range(15)] + [
    (15 << 16, 0x10000)
]


def names(p, a: int, z: int) -> set[str]:
    return {p.sources[s].name for s in p.dst.src[a:z]}


def test_a_measured_edit_runs_its_law_on_the_class_it_ran_on():
    taps = Taps.of(PHASES)
    got = predict(model(), taps, FLAT, TABLE13, edits=[(439, GRID_C2)])
    drop = predict(model(), Taps.of([(0, *p[1:]) for p in PHASES]), FLAT, TABLE13)
    stock = predict(model(), taps, FLAT, TABLE13)
    assert [c for _, c in got.result.edits] == [Confidence.VERIFIED]
    assert got.dst.known[:1024] == drop.dst.known[:1024]
    assert got.dst.data[:1024] == drop.dst.data[:1024] != stock.dst.data[:1024]
    assert not any(got.dst.known[1024:2048]) and names(got, 1024, 2048) == {
        "resampler edit"
    }
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
    # Two measured edits never ran together.
    got = predict(
        model(), Taps.of(PHASES), FLAT, TABLE13, edits=[(439, GRID_C2), (490, GRID_C2)]
    )
    assert not any(got.dst.known)
