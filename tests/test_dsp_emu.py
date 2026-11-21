import base64
import json
import struct
import warnings
from functools import cache
from pathlib import Path

import pytest
from conftest import stock_image

from fenykl.dsp import commands, memcpy
from fenykl.dsp.emu import Boot, Emulator, Status, back_firmware
from fenykl.dsp.fields import Confidence
from fenykl.dsp.program import HypothesisFieldWarning, Program
from fenykl.dsp.semantics import STOCK_TRANSFERS, Sequencer, UnknownSemantics
from fenykl.dsp.state import R14_BELOW, R14_EXACT, BootParams
from fenykl.dsp.units import C2, CtlLo
from fenykl.dsp.word import IDLE, Word

VECTORS = Path(__file__).resolve().parent / "data" / "dsp"
SRC_AREA = 0xA0FCA800
"""The base of the bench harness's src area, which its patterns are indexed from."""
K = 0x9E3779B1


def pattern(kind: str, seed: int, src: int, n: int) -> bytes:
    """The bench harness's src patterns: u32 words by byte offset from SRC_AREA."""

    def word(o: int) -> int:
        match kind:
            case "const":
                return seed
            case "ramp":
                return (seed + (o >> 2)) & 0xFFFFFFFF
            case "zero":
                return 0
        v = ((((o >> 2) + 1) * K) & 0xFFFFFFFF) ^ seed
        return v or K

    base = src - SRC_AREA
    w0 = base & ~3
    words = b"".join(struct.pack("<I", word(o)) for o in range(w0, base + n + 3, 4))
    return words[base - w0 : base - w0 + n]


@cache
def stock() -> Program:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HypothesisFieldWarning)
        return memcpy.program()


def edited(edits: list[dict]) -> Program:
    words = list(stock().words)
    for e in edits:
        at = e["at"]
        words[at] = (
            IDLE if e.get("idle") else Word.from_fields(e["set"], base=words[at])
        )
    return Program(tuple(words))


def boot_of(spec: dict) -> Boot:
    r14 = {"exact": R14_EXACT, "below": R14_BELOW, None: BootParams().r14}[spec["r14"]]
    return Boot(BootParams(r14=r14, r29=spec["r29"], r21_format=spec["r21_format"]))


def needs_firmware(vector: dict) -> bool:
    for job in vector["jobs"]:
        if any(
            e.get("set", {}).get("f115_119") in (21, 29) and e["at"] == 67
            for e in job["edits"]
        ):
            return True
    return False


def run_vector(vector: dict, mode: str = Sequencer.POINTER) -> list[str]:
    """Replays the vector's jobs; returns its mismatches, empty when every check holds."""
    boot = boot_of(vector["boot"])
    firmware = (
        back_firmware(boot, stock_image("em10ii")) if needs_firmware(vector) else None
    )
    emu = Emulator(boot, sequencer=Sequencer(mode))
    bad = []
    for k, job in enumerate(vector["jobs"]):
        p = job["pattern"]
        boot.put(
            job["src"],
            pattern(p["kind"], p["seed"], job["src"], job["len"]),
            f"job {k} src",
        )
        boot.put(job["dst"], bytes([job["fill"]]) * job["fill_len"], f"job {k} prefill")
        result = emu.run(
            commands.copy(edited(job["edits"]), job["src"], job["dst"], job["len"])
        )
        expect = job.get("expect")
        if expect is None:
            continue
        if result.status.name.lower() != expect["status"]:
            bad.append(
                f"job {k}: status {result.status.name}, expected {expect['status']}: {result.why}"
            )
        for seg in expect.get("dst", []):
            a, z = seg["at"], seg["end"]
            got = boot.read(job["dst"] + a, z - a)
            where = f"job {k} [{a:#x}, {z:#x}) {seg['kind']}"
            if seg["kind"] == "unknown":
                if any(got.known):
                    bad.append(f"{where}: {sum(got.known)} bytes known")
                continue
            if not all(got.known):
                bad.append(f"{where}: {len(got) - sum(got.known)} bytes unknown")
                continue
            if seg["kind"] == "firmware":
                if set(got.src) != {firmware}:
                    bad.append(f"{where}: not from the firmware")
                continue
            want = base64.b64decode(seg["data"])
            if bytes(got.data) != want:
                bad.append(
                    f"{where}: {sum(x != y for x, y in zip(got.data, want))} bytes differ"
                )
    return bad


@pytest.mark.parametrize("mode", [Sequencer.POINTER, Sequencer.POSITION])
@pytest.mark.parametrize("path", sorted(VECTORS.glob("*.json")), ids=lambda p: p.stem)
def test_camera_vector(path: Path, mode: str):
    vector = json.loads(path.read_text())
    assert run_vector(vector, mode) == [], vector["fact"]


SRC, DST, LEN = 0xA0FCA800, 0xA1113600, 0x400


def copy(boot: Boot, program: Program, data: bytes, strict: bool = True):
    boot.put(SRC, data, "src")
    boot.put(DST, b"\xa5" * LEN, "prefill")
    result = Emulator(boot, strict=strict).run(commands.copy(program, SRC, DST, LEN))
    return result, boot.read(DST, LEN)


def with_reg(word: int, reg: int) -> Program:
    return edited([{"at": word, "set": {"xreg": reg}}])


def test_the_transfer_class_holds_exactly_the_register_transfer_words():
    assert STOCK_TRANSFERS == (
        42,
        43,
        44,
        46,
        47,
        48,
        49,
        54,
        55,
        57,
        66,
        67,
        68,
        *range(70, 79),
    )


def test_strict_mode_raises_on_a_field_without_semantics():
    # The idle word with one control bit set matches no class.
    synthetic = Word(ctl_lo=CtlLo(f0=1))
    program = Program((*stock().words[:3], synthetic, *stock().words[4:]))
    with pytest.raises(
        UnknownSemantics,
        match=r"word 3: no semantics for f0=0x1 \(nearest class: idle\)",
    ):
        copy(Boot(), program, bytes(range(256)) * 4)


def test_strict_mode_raises_on_a_transfer_with_an_opcode_field_set():
    word = Word(stock().words[73], c2=C2(f265_268=0x3))
    program = Program((*stock().words[:73], word, *stock().words[74:]))
    with pytest.raises(
        UnknownSemantics,
        match=r"word 73: no semantics for f265_268=0x3 \(nearest class: transfer\)",
    ):
        copy(Boot(), program, bytes(LEN))


def test_lenient_mode_ignores_it_and_taints():
    synthetic = Word(ctl_lo=CtlLo(f0=1))
    program = Program((*stock().words[:3], synthetic, *stock().words[4:]))
    data = bytes(range(256)) * 4
    result, dst = copy(Boot(), program, data, strict=False)
    assert result.ignored == [(3, ["f0"])]
    assert bytes(dst.data) == data
    assert dst.weakest() is Confidence.HYPOTHESIS


def test_taint_marks_hypothesis_derived_bytes():
    boot = Boot(BootParams(r14=R14_EXACT))
    data = bytes(range(256)) * 4
    _, dst = copy(boot, stock(), data)
    assert dst.weakest() is Confidence.VERIFIED
    # Word 68 at r14 places the load at -0xE0: r14's value is a hypothesis, so are the bytes it placed.
    _, dst = copy(boot, with_reg(68, 14), data[::-1])
    assert bytes(dst.data[: LEN - 0xE0]) == data[::-1][0xE0:]
    assert {dst.taint[i] for i in range(LEN - 0xE0)} == {Confidence.HYPOTHESIS}
    assert {dst.taint[i] for i in range(LEN - 0xE0, LEN)} == {Confidence.VERIFIED}
    # A replay without a load stores the held buffer again, taint and all.
    _, dst = copy(boot, with_reg(66, 0), bytes(LEN))
    assert bytes(dst.data[: LEN - 0xE0]) == data[::-1][0xE0:]
    assert dst.taint[0] == Confidence.HYPOTHESIS


def test_bytes_no_job_wrote_are_unknown_and_say_so():
    boot = Boot(BootParams(r14=R14_EXACT))
    _, dst = copy(boot, with_reg(76, 14), bytes(range(256)) * 4)
    assert dst.ranges(known=False) == [(0, 0xE0)]
    assert boot.sources[dst.src[0]].name == "unwritten"


def test_an_unset_slot_times_out_and_stores_nothing():
    for word in (66, 67, 68, 73, 76, 43, 47):
        program = edited([{"at": word, "idle": True}])
        result, dst = copy(Boot(), program, bytes(range(256)) * 4)
        assert result.status is Status.TIMEOUT, word
        assert bytes(dst.data) == b"\xa5" * LEN
