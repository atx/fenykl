import base64
import json
import random
import struct
import warnings
from dataclasses import replace
from functools import cache
from pathlib import Path

import pytest
from conftest import CONF, stock_image

from fenykl.dsp.asm import OP, Bundle, G, Label, raw
from fenykl.dsp.emu import Boot, dram_alias
from fenykl.dsp.fields import FORMATS, Confidence
from fenykl.dsp.machine import Machine, Sv
from fenykl.dsp.program import HypothesisFieldWarning, Program, assemble
from fenykl.dsp.semantics import OP_SEMANTICS, UnknownSemantics, ring_length
from fenykl.dsp.stock import commands
from fenykl.dsp.stock.copy import Copy16Model
from fenykl.dsp.stock.intervals import RegSet, destinations, op_effect
from fenykl.dsp.stock.items import stock_items
from fenykl.dsp.stock.model import Status, StockBoot, run_model
from fenykl.dsp.stock.registers import (
    MAIN_CODE,
    R14_BELOW,
    R14_EXACT,
    BootParams,
    Reg,
    RegisterFile,
    back_firmware,
)
from fenykl.dsp.word import IDLE, Word
from fenykl.firmware import unpack_firmware

VECTORS = Path(__file__).resolve().parent / "data" / "dsp"
SRC_AREA = 0xA0FCA800
"""The base of the src area of the camera runs, which their patterns are indexed from."""
K = 0x9E3779B1


def pattern(kind: str, seed: int, src: int, n: int) -> bytes:
    """The src patterns of the camera runs: u32 words by byte offset from SRC_AREA."""

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
def model() -> Copy16Model:
    return Copy16Model.from_image(stock_image("em10ii"))


def stock() -> Program:
    return model().program


def edited(edits: list[dict]) -> Program:
    words = list(stock().words)
    for e in edits:
        at = e["at"]
        words[at] = (
            IDLE if e.get("idle") else Word.from_fields(e["set"], base=words[at])
        )
    return Program(tuple(words))


def boot_of(spec: dict) -> StockBoot:
    r14 = {"exact": R14_EXACT, "below": R14_BELOW, None: BootParams().r14}[spec["r14"]]
    return StockBoot(
        BootParams(r14=r14, r29=spec["r29"], r21_format=spec["r21_format"])
    )


def needs_firmware(vector: dict) -> bool:
    for job in vector["jobs"]:
        if any(
            e.get("set", {}).get("f115_119") in (21, 29) and e["at"] == 67
            for e in job["edits"]
        ):
            return True
    return False


VECTOR_KEYS = {"name", "fact", "boot", "jobs"}
BOOT_KEYS = {"r14", "r29", "r21_format"}
JOB_KEYS = {"edits", "src", "dst", "len", "pattern", "fill", "fill_len"}
SEGMENT_KEYS = {
    "bytes": {"at", "end", "kind", "data"},
    "firmware": {"at", "end", "kind", "address"},
    "unknown": {"at", "end", "kind"},
}

# Per job: the weakest taint of the known checked dst bytes ("-" none known or no dst checked,
# "." job not checked), then the result's taint, by stock.model's confidence rule: a copy is at
# most inferred; r6, r11, r14 and r21's format are hypotheses, and so is what they placed or
# formatted, also when a later job stores it again; regs_b off the stock values and an unset
# slot (idled 66) are hypotheses; an idled word or a role's register that alone stops the store
# at 1 KiB is verified (dst keeps the verified prefill), at most inferred at other lengths or
# with a second edit; a job that stores is at most inferred, whatever decided it.
TAINTS = {
    "abort-idle66-b": "II -H II",
    "abort-idle66": "II II -H II HH HH HH HH II",
    "compose-68-76": "II II II HH II HH",
    "compose-count-r29": "II II II HH II HH",
    "count-1k": "II II II II",
    "count-4k": "II II II II",
    "firmware-r21": "II II II II II II",
    "format47-2k": "II II II HH II II II",
    "format47-pack": "II II II II HH II II II II II",
    "format47-store-side": "II II HH II II II",
    "format47-zero-boot": "II II HH HH II II II II",
    "format47-zero-cells": "II II II II II II II",
    "idle-no-store": "VV VV VV VV VV VV VV VV VV II",
    "idle15-zero-fill": ".I II",
    "idle61": "II II II -I",
    "offset68-2k": "II II HH II II HH",
    "offset68-512": "II II HH II II HH",
    "place-1k": "II II HH II HH II HH II HH",
    "place-firmware": "II II II HH II HH II II II",
    "r14-below": "HH",
    "r14-exact": "II II HH HH .I HH HH HH HH HH II",
    "regs-b-flips": "II II HH HH HH HH",
    "replay-4k": "II II",
    "replay-after-count": "II II II II II II II II HH HI",
    "stock-copy": "II",
}


def check_keys(vector: dict) -> list[str]:
    """Keys the fixture holds that the replay does not check, or misses."""
    bad = []
    if set(vector) != VECTOR_KEYS or set(vector["boot"]) != BOOT_KEYS:
        bad.append(f"vector keys {sorted(vector)}, boot {sorted(vector['boot'])}")
    for k, job in enumerate(vector["jobs"]):
        extra = set(job) - JOB_KEYS - {"expect", "follow_up"}
        if extra or not JOB_KEYS <= set(job):
            bad.append(f"job {k}: keys {sorted(job)}")
        for e in job["edits"]:
            if set(e) not in ({"at", "idle"}, {"at", "set"}):
                bad.append(f"job {k}: edit keys {sorted(e)}")
        expect = job.get("expect", {})
        if set(expect) - {"status", "dst"}:
            bad.append(f"job {k}: expect keys {sorted(expect)}")
        for seg in expect.get("dst", []):
            if set(seg) != SEGMENT_KEYS[seg["kind"]]:
                bad.append(f"job {k}: segment keys {sorted(seg)}")
    return bad


def run_vector(vector: dict) -> list[str]:
    """Replays the vector's jobs; returns its mismatches, empty when every check holds."""
    bad = check_keys(vector)
    boot = boot_of(vector["boot"])
    firmware = (
        back_firmware(boot, stock_image("em10ii")) if needs_firmware(vector) else None
    )
    taints = TAINTS[vector["name"]].split()
    if len(taints) != len(vector["jobs"]):
        bad.append(f"{len(taints)} taints for {len(vector['jobs'])} jobs")
    status = None
    for k, (job, taint) in enumerate(zip(vector["jobs"], taints)):
        if job.get("follow_up") and (job["edits"] or status is not Status.TIMEOUT):
            bad.append(f"job {k}: a follow-up must be a stock job after a timeout")
        p = job["pattern"]
        boot.put(
            job["src"],
            pattern(p["kind"], p["seed"], job["src"], job["len"]),
            f"job {k} src",
        )
        fill = bytes([job["fill"]]) * job["fill_len"]
        prefill = boot.put(job["dst"], fill, f"job {k} prefill")
        result = run_model(
            boot,
            model(),
            commands.copy(edited(job["edits"]), job["src"], job["dst"], job["len"]),
        )
        status = result.status
        if CONF[taint[1]] is not result.taint:
            bad.append(
                f"job {k}: result taint {result.taint.name}, expected {taint[1]}"
            )
        if status is Status.TIMEOUT and result.stored is None:
            got = boot.read(job["dst"], job["fill_len"])
            if (
                bytes(got.data) != fill
                or not all(got.known)
                or set(got.src) != {prefill}
            ):
                bad.append(f"job {k}: a timeout changed the prefill")
        expect = job.get("expect")
        if expect is None:
            if taint[0] != ".":
                bad.append(f"job {k}: not checked, expected taint {taint[0]}")
            continue
        if status.name.lower() != expect["status"]:
            bad.append(
                f"job {k}: status {status.name}, expected {expect['status']}: {result.why}"
            )
        weakest = boot.read(job["dst"], job["fill_len"]).weakest()
        if "dst" not in expect:
            weakest = None
        if (weakest is None and taint[0] not in "-.") or (
            weakest is not None and CONF.get(taint[0]) is not weakest
        ):
            bad.append(f"job {k}: dst taint {weakest}, expected {taint[0]}")
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
                start = dram_alias(seg["address"]) - dram_alias(MAIN_CODE)
                if set(got.src) != {firmware}:
                    bad.append(f"{where}: not from the firmware")
                elif list(got.off) != list(range(start, start + z - a)):
                    bad.append(f"{where}: not the firmware at {seg['address']:#x}")
                continue
            want = base64.b64decode(seg["data"])
            if bytes(got.data) != want:
                bad.append(
                    f"{where}: {sum(x != y for x, y in zip(got.data, want))} bytes differ"
                )
    return bad


@pytest.mark.parametrize("path", sorted(VECTORS.glob("*.json")), ids=lambda p: p.stem)
def test_camera_vector(path: Path):
    vector = json.loads(path.read_text())
    assert vector["name"] == path.stem
    assert run_vector(vector) == [], vector["fact"]


def test_every_vector_has_its_taints():
    assert sorted(TAINTS) == sorted(p.stem for p in VECTORS.glob("*.json"))


def test_the_vectors_hold_no_firmware_bytes():
    # No 16-byte window of the firmware's unpacked blocks or program items at a 16-byte offset
    # is in a vector's dst bytes, so no run of 31 or more firmware bytes is (windows of fewer than 4 distinct byte values
    # left out: zero runs with a stray byte, which the patterns hold too).
    windows = set()
    for path in VECTORS.glob("*.json"):
        for job in json.loads(path.read_text())["jobs"]:
            for seg in job.get("expect", {}).get("dst", []):
                if "data" not in seg:
                    continue
                blob = base64.b64decode(seg["data"])
                windows |= {
                    blob[o : o + 16]
                    for o in range(len(blob) - 15)
                    if len(set(blob[o : o + 16])) >= 4
                }
    image = stock_image("em10ii")
    bodies = [b.body for b in unpack_firmware(image).blocks]
    bodies += stock_items(image).values()
    for body in bodies:
        assert not any(
            body[o : o + 16] in windows for o in range(0, len(body) - 15, 16)
        )


SRC, DST, LEN = 0xA0FCA800, 0xA1113600, 0x400


def copy(boot: StockBoot, program: Program, data: bytes, strict: bool = True):
    """`run` of `data`; (result, dst bytes)."""
    result, boot = run(program, strict=strict, boot=boot, data=data)
    return result, boot.read(DST, LEN)


def with_reg(word: int, reg: int) -> Program:
    return edited([{"at": word, "set": {"xreg": reg}}])


def test_the_transfer_class_holds_exactly_the_register_transfer_words():
    words = stock().words
    assert tuple(i for i, w in enumerate(words) if model().transfer.matches(w)) == (
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
    synthetic = Word.from_fields({"f0": 1})
    program = Program((*stock().words[:3], synthetic, *stock().words[4:]))
    with pytest.raises(
        UnknownSemantics,
        match=r"word 3: no semantics for f0=0x1 \(nearest class: idle\)",
    ):
        copy(StockBoot(), program, bytes(range(256)) * 4)


def test_strict_mode_raises_on_a_transfer_with_an_opcode_field_set():
    word = Word.from_fields({"f265_266": 0}, base=stock().words[73])
    program = Program((*stock().words[:73], word, *stock().words[74:]))
    with pytest.raises(
        UnknownSemantics,
        match=r"word 73: no semantics for f265_266=0x0 \(nearest class: transfer\)",
    ):
        copy(StockBoot(), program, bytes(LEN))


def test_lenient_mode_ignores_it_and_taints():
    synthetic = Word.from_fields({"f0": 1})
    program = Program((*stock().words[:3], synthetic, *stock().words[4:]))
    data = bytes(range(256)) * 4
    result, dst = copy(StockBoot(), program, data, strict=False)
    assert result.ignored == [(3, ["f0"])]
    assert bytes(dst.data) == data
    assert dst.weakest() is Confidence.HYPOTHESIS


def test_taint_marks_hypothesis_derived_bytes():
    boot = StockBoot(BootParams(r14=R14_EXACT))
    data = bytes(range(256)) * 4
    # No word of the copy is decoded: its bytes are at most inferred, the stock copy's too.
    _, dst = copy(boot, stock(), data)
    assert dst.weakest() is Confidence.INFERRED
    # Word 68 at r14 places the load at -0xE0: r14's value is a hypothesis, so are the bytes it placed.
    _, dst = copy(boot, with_reg(68, 14), data[::-1])
    assert bytes(dst.data[: LEN - 0xE0]) == data[::-1][0xE0:]
    assert {dst.taint[i] for i in range(LEN - 0xE0)} == {Confidence.HYPOTHESIS}
    assert {dst.taint[i] for i in range(LEN - 0xE0, LEN)} == {Confidence.INFERRED}
    # A replay without a load stores the held buffer again, taint and all.
    _, dst = copy(boot, with_reg(66, 0), bytes(LEN))
    assert bytes(dst.data[: LEN - 0xE0]) == data[::-1][0xE0:]
    assert dst.taint[0] == Confidence.HYPOTHESIS


def test_bytes_no_job_wrote_are_unknown_and_say_so():
    boot = StockBoot(BootParams(r14=R14_EXACT))
    _, dst = copy(boot, with_reg(76, 14), bytes(range(256)) * 4)
    assert dst.ranges(known=False) == [(0, 0xE0)]
    assert boot.sources[dst.src[0]].name == "unwritten"


def test_an_unset_slot_times_out_and_stores_nothing():
    for word in (66, 67, 68, 73, 76, 43, 47):
        program = edited([{"at": word, "idle": True}])
        result, dst = copy(StockBoot(), program, bytes(range(256)) * 4)
        assert result.status is Status.TIMEOUT, word
        assert bytes(dst.data) == b"\xa5" * LEN


def test_the_spare_channel_is_not_needed():
    for word in (54, 55):
        result, dst = copy(
            StockBoot(), edited([{"at": word, "idle": True}]), bytes(range(256)) * 4
        )
        assert result.status is Status.OK, word
        assert bytes(dst.data) == bytes(range(256)) * 4


def moved(at: dict[int, int]) -> Program:
    """The stock copy with word `to` holding stock word `src`, for each to: src of `at`."""
    words = list(stock().words)
    for to, src in at.items():
        words[to] = stock().words[src]
    return Program(tuple(words))


@pytest.mark.parametrize(
    "at",
    [{73: 74, 74: 73}, {74: 75, 75: 76, 76: 74}, {74: 75, 75: 76, 76: 77, 77: 78}],
    ids=["count-later", "two-earlier", "three-earlier"],
)
def test_roles_follow_moved_counted_transfers(at: dict[int, int]):
    result, dst = copy(StockBoot(), moved(at), bytes(range(256)) * 4)
    assert result.status is Status.OK and bytes(dst.data) == bytes(range(256)) * 4
    assert result.taint is Confidence.HYPOTHESIS  # off their stock indices


def test_a_counted_transfer_before_the_return_stalls_the_job():
    result, dst = copy(StockBoot(), moved({77: 78, 78: 77}), bytes(range(256)) * 4)
    assert result.status is Status.TIMEOUT and bytes(dst.data) == b"\xa5" * LEN
    assert any("counted transfer at word 78" in why for why in result.why)
    # A holding word at 77 without the counted one leaves 77's slot unset instead.
    result, _ = copy(StockBoot(), moved({77: 78}), bytes(range(256)) * 4)
    assert result.status is Status.TIMEOUT
    assert any("unset stream slot" in why for why in result.why)


def run(
    program: Program,
    length: int = LEN,
    strict: bool = True,
    boot: StockBoot | None = None,
    data: bytes | None = None,
):
    """The copy job of `length` bytes of `data` (default a byte ramp) from SRC onto a prefilled
    dst; (result, boot)."""
    boot = boot or StockBoot()
    boot.put(SRC, bytes(range(256)) * (length // 256) if data is None else data, "src")
    boot.put(DST, b"\xa5" * length, "prefill")
    job = commands.copy(program, SRC, DST, length)
    result = run_model(boot, model(), job, strict=strict)
    return result, boot


def idled(*words: int) -> Program:
    return edited([{"at": w, "idle": True} for w in words])


def test_a_single_seen_edit_is_verified_a_combination_inferred():
    result, boot = run(idled(12))
    assert result.status is Status.OK and result.stored is None
    assert result.taint is Confidence.VERIFIED
    assert bytes(boot.read(DST, LEN).data) == b"\xa5" * LEN
    result, _ = run(idled(13))
    assert result.taint is Confidence.VERIFIED
    result, _ = run(idled(12, 13))
    assert result.stored is None
    assert result.taint is Confidence.INFERRED


def test_table_outcomes_seen_at_1k_are_inferred_at_other_lengths():
    result, _ = run(with_reg(42, 1))
    assert result.stored is None and result.taint is Confidence.VERIFIED
    result, _ = run(with_reg(42, 1), length=0x800)
    assert result.stored is None and result.taint is Confidence.INFERRED


def lenient_program(*words: int) -> Program:
    """The stock copy without `words`, and idle word 3 with control bit 0 set."""
    program = idled(*words) if words else stock()
    synthetic = Word.from_fields({"f0": 1})
    return Program((*program.words[:3], synthetic, *program.words[4:]))


def test_lenient_mode_taints_the_loaded_buffer():
    result, boot = run(lenient_program(), strict=False)
    assert result.ignored == [(3, ["f0"])]
    assert boot.buffer.read(0, LEN).weakest() is Confidence.HYPOTHESIS


def test_lenient_mode_taints_the_zero_fill():
    _, boot = run(lenient_program(15), strict=False)
    zeros = boot.read(DST, 0x4000)
    assert bytes(zeros.data) == bytes(0x4000)
    assert zeros.weakest() is Confidence.HYPOTHESIS


@pytest.mark.parametrize("strict", [True, False])
def test_word_60_copies_only_with_f76_79_at_0(strict: bool):
    for value in (1, 8, 0xF):
        result, boot = run(
            edited([{"at": 60, "set": {"f76_79": value}}]), strict=strict
        )
        assert result.status is Status.TIMEOUT and result.stored is None
        assert result.taint is Confidence.VERIFIED and result.ignored == []
        assert bytes(boot.read(DST, LEN).data) == b"\xa5" * LEN
    result, _ = run(stock(), strict=strict)
    assert result.status is Status.OK and result.stored == (DST, DST + LEN)


def test_a_halt_before_the_stream_ends_the_job_with_dst_unwritten():
    words = list(stock().words)
    words[11] = words[5]
    result, boot = run(Program(tuple(words)))
    assert result.status is Status.OK and result.stored is None
    assert result.taint is Confidence.VERIFIED
    assert bytes(boot.read(DST, LEN).data) == b"\xa5" * LEN
    # The machine's path stands at other lengths than the 1 KiB run.
    for length in (0x408, 0x4000, 0x10):
        result, _ = run(Program(tuple(words)), length=length)
        assert result.stored is None and result.taint is Confidence.VERIFIED
    # At another index before the stream: inferred; behind the halt at 26, the brf 27 is a
    # control word not measured there; from the stream's first word 58 on, open.
    for at, taint in (
        (20, Confidence.INFERRED),
        (26, Confidence.HYPOTHESIS),
        (58, None),
        (60, None),
    ):
        words = list(stock().words)
        words[at] = words[5]
        if taint is None:
            with pytest.raises(UnknownSemantics, match="after the stream started"):
                run(Program(tuple(words)))
            continue
        result, _ = run(Program(tuple(words)))
        assert result.status is Status.OK and result.stored is None
        assert result.taint is taint
    words = list(stock().words)
    words[50 - 4] = words[5]  # in 45's delay slots, after its decision
    result, _ = run(Program(tuple(words)), length=0x408)
    assert result.stored is None and result.taint is Confidence.INFERRED
    # With the guard idled the loop hands the stream values off its grammar.
    words = list(idled(45).words)
    words[52] = words[5]
    with pytest.raises(UnknownSemantics, match="stream off script before the halt"):
        run(Program(tuple(words)))


def test_a_branch_the_machine_cannot_decide_takes_the_stock_path():
    """Idling word 15 leaves r7 unknown at the branch 27: the stock path (not taken) leads to a
    halt at 35, a hypothesis; taken, the job would return at 79 without reaching it."""
    result, boot = run(halted(35, idled(15)))
    assert result.why == [
        "op 7 at word 35 halts the job before the stream: dst not written"
    ]
    assert result.status is Status.OK and result.stored is None
    assert result.taint is Confidence.HYPOTHESIS
    assert bytes(boot.read(DST, LEN).data) == b"\xa5" * LEN


def flipped(at: int, key: str, bits: int, base: Program | None = None) -> Program:
    """`base` (default the stock copy) with `bits` of field `key` of word `at` flipped."""
    words = list((base or stock()).words)
    words[at] = Word.from_fields({key: words[at][key] ^ bits}, base=words[at])
    return Program(tuple(words))


def retargeted(at: int, target: int, base: Program | None = None) -> Program:
    """`base` (default the stock copy) with control word `at`'s target moved to `target`."""
    words = list((base or stock()).words)
    words[at] = Word.from_fields({"imm": target}, base=words[at])
    return Program(tuple(words))


def without(program: Program, *at: int) -> Program:
    words = list(program.words)
    for i in at:
        words[i] = IDLE
    return Program(tuple(words))


RAMP = bytes(range(256)) * 4


TIMEOUTS = {
    "to-50": (lambda: retargeted(45, 50), True),
    "to-50-idle-53": (lambda: without(retargeted(45, 50), 53), True),
    "to-51": (lambda: retargeted(45, 51), False),
    "idle-45": (lambda: idled(45), False),
    "idle-28": (lambda: idled(28), True),
    "idle-28-53": (lambda: idled(28, 53), True),
    "idle-79": (lambda: idled(79), True),
    "imm65": (lambda: flipped(65, "imm", 0x40), True),
}


@pytest.mark.parametrize("name", TIMEOUTS)
def test_timeouts_with_and_without_the_store(name: str):
    program, stores = TIMEOUTS[name]
    result, dst = copy(StockBoot(), program(), RAMP)
    assert result.status is Status.TIMEOUT
    assert bytes(dst.data) == (RAMP if stores else b"\xa5" * LEN)
    assert (result.stored is not None) is stores


def test_the_loop_words_need_the_guard_unless_both_are_idle():
    result, dst = copy(StockBoot(), idled(45, 53), RAMP)
    assert result.status is Status.OK and result.stored is None
    assert bytes(dst.data) == b"\xa5" * LEN
    assert result.taint is Confidence.INFERRED  # two edits
    # The loop's words were measured with the loop skipped: other pairs are open.
    for program in (
        without(retargeted(45, 50), 50),
        idled(45, 56),
        idled(28, 51),
    ):
        with pytest.raises(UnknownSemantics, match="measured with the loop skipped"):
            copy(StockBoot(), program, RAMP)
    # With the guard stock they still have no effect.
    result, dst = copy(StockBoot(), idled(51, 53), RAMP)
    assert result.status is Status.OK and bytes(dst.data) == RAMP


def test_operand_edits_are_measured_one_field_at_a_time():
    # 27's target moved to its own fall-through.
    result, dst = copy(StockBoot(), retargeted(27, 32), RAMP)
    assert result.status is Status.OK and bytes(dst.data) == RAMP
    with pytest.raises(UnknownSemantics, match="word 45: no semantics for f15_22"):
        copy(StockBoot(), flipped(45, "imm", 0x10), RAMP)
    with pytest.raises(UnknownSemantics, match="two fields of one word"):
        copy(StockBoot(), flipped(45, "xreg", 1, retargeted(45, 50)), RAMP)


@pytest.mark.parametrize(
    "src, dst, length",
    [(SRC, DST, LEN), (SRC, DST + 1, 0x40F), (SRC + 1, DST + 1, LEN)],
    ids=["aligned", "odd-dst-len", "odd-src-dst"],
)
def test_a_halt_at_the_loop_top_acts_only_when_the_guard_goes_there(
    src: int, dst: int, length: int
):
    words = list(without(stock(), *range(51, 58)).words)
    words[50] = stock().words[5]
    marker = Program(tuple(words))
    boot = StockBoot()
    data = bytes(i & 0xFF for i in range(length))
    boot.put(src, data, "src")
    boot.put(dst, b"\xa5" * length, "prefill")
    result = run_model(boot, model(), commands.copy(marker, src, dst, length))
    assert result.status is Status.OK and bytes(boot.read(dst, length).data) == data
    result, dst = copy(StockBoot(), retargeted(45, 50, marker), RAMP)
    assert result.status is Status.OK and result.stored is None
    assert bytes(dst.data) == b"\xa5" * LEN


def halted(at: int, base: Program | None = None) -> Program:
    """`base` (default the stock copy) with the halt word in place of word `at`."""
    words = list((base or stock()).words)
    words[at] = stock().words[5]
    return Program(tuple(words))


@pytest.mark.parametrize(
    "program, length, match",
    [
        # The machine takes 27 at len 1: the halt is not reached, and the rows hold on the stock
        # path only.
        *(
            (lambda at=at: halted(at), 1, "leaves the stock path at word 27")
            for at in (50, 60, 78)
        ),
        (lambda: halted(51, idled(28)), LEN, "after a loop word"),
        (lambda: halted(52, idled(28)), LEN, "after a loop word"),
        # 45 to the loop's top: the store at 56 is handed before the halt at 57.
        (
            lambda: halted(57, retargeted(45, 50)),
            LEN,
            "after the stream started",
        ),
    ],
)
def test_a_halt_off_the_measured_path_raises(program, length, match):
    for strict in (True, False):
        with pytest.raises(UnknownSemantics, match=match):
            run(program(), length=length, strict=strict)


def test_a_halt_is_a_hypothesis_after_an_edit_it_runs_or_with_an_insert():
    # Word 12 runs behind the halt at 11; the insert is stepped without.
    for program in (halted(11, idled(12)), halted(20, inserted(len(stock()), IDLE))):
        result, _ = run(program)
        assert result.stored is None and result.taint is Confidence.HYPOTHESIS


def test_route_nibbles_must_hold_the_stock_values():
    word = Word.from_fields({"f76_79": 1}, base=stock().words[73])
    program = Program((*stock().words[:73], word, *stock().words[74:]))
    with pytest.raises(UnknownSemantics, match=r"word 73: no semantics for f76_79=0x1"):
        run(program)
    result, boot = run(program, strict=False)
    assert result.ignored == [(73, ["f76_79"])]
    assert boot.read(DST, LEN).weakest() is Confidence.HYPOTHESIS


@pytest.mark.parametrize(
    "order, stores",
    [
        (list(range(19, -1, -1)), False),
        ([*range(1, 20), 0], False),
        ([1, 0, *range(2, 20)], True),
    ],
    ids=["reversed", "rotated", "swapped"],
)
def test_reordered_prologue_words_are_at_most_hypothesis(
    order: list[int], stores: bool
):
    words = stock().words
    program = Program((*(words[i] for i in order), *words[20:]))
    with pytest.raises(UnknownSemantics, match="word 0: no semantics"):
        run(program)
    result, boot = run(program, strict=False)
    assert result.status is Status.OK and result.taint is Confidence.HYPOTHESIS
    assert (result.stored is not None) is stores
    dst = boot.read(DST, LEN)
    if stores:
        assert dst.weakest() is Confidence.HYPOTHESIS
    else:
        assert bytes(dst.data) == b"\xa5" * LEN


def test_a_stock_word_at_another_index_is_no_row():
    """A row covers the stock word at its own index only: 31's value at 30 is no edit of 30
    that ran, so it raises, and lenient mode ignores what differs from 30's stock word."""
    words = list(stock().words)
    words[30] = words[31]
    moved = Program(tuple(words))
    with pytest.raises(UnknownSemantics, match="word 30: no semantics .*nearest class"):
        run(moved)
    result, boot = run(moved, strict=False)
    differ = [f.id for f, _ in words[31].changed(stock().words[30])]
    assert result.ignored == [(30, differ)] and differ
    assert boot.read(DST, LEN).weakest() is Confidence.HYPOTHESIS


def swapped(a: int, b: int) -> Program:
    words = list(stock().words)
    words[a], words[b] = words[b], words[a]
    return Program(tuple(words))


def test_lenient_mode_runs_the_stock_word_where_nothing_covers():
    """Words 0 and 27 swapped: no row covers either at the other's index, so lenient mode runs
    the stock word at both and the job is the stock copy as a hypothesis."""
    program = swapped(0, 27)
    with pytest.raises(UnknownSemantics):
        run(program)
    result, boot = run(program, strict=False)
    assert result.status is Status.OK and result.stored is not None
    assert [i for i, _ in result.ignored] == [0, 27]
    dst = boot.read(DST, LEN)
    assert bytes(dst.data) == bytes(range(256)) * (LEN // 256)
    assert dst.weakest() is Confidence.HYPOTHESIS


def test_a_stock_transfer_run_in_lenient_mode_yields_to_a_moved_one():
    """The load transfer 66 moved to 0 is a transfer there and takes 66's slot; the stock
    transfer that lenient mode runs for word 0's value at 66 yields to it instead of
    overfilling the channel, and word 0's row is missing (dst not written)."""
    program = swapped(0, 66)
    with pytest.raises(UnknownSemantics):
        run(program)
    result, boot = run(program, strict=False)
    assert result.status is Status.OK and result.stored is None
    assert [i for i, _ in result.ignored] == [66]
    assert any("stock word 0) missing" in why for why in result.why)
    assert bytes(boot.read(DST, LEN).data) == b"\xa5" * LEN
    assert result.taint is Confidence.HYPOTHESIS


def two_stand_ins(*at: int) -> Program:
    """Transfer 42 copied to the idle word 3 and word 0's value at `at`: four transfers for
    completion A's three slots, two of them stand-ins in lenient mode."""
    words = list(stock().words)
    assert words[3] == IDLE
    words[3] = words[42]
    for i in at:
        words[i] = words[0]
    return Program(tuple(words))


@pytest.mark.parametrize("at", [(42, 43), (43, 44)])
def test_only_the_stand_ins_that_overfill_a_channel_yield(at: tuple[int, int]):
    """Of the two stand-ins only the first yields: 3, the other stand-in and the remaining
    real transfer fill the channel, the later stand-in in its stock role, and the copy runs."""
    program = two_stand_ins(*at)
    with pytest.raises(UnknownSemantics):
        run(program)
    result, boot = run(program, strict=False)
    assert result.status is Status.OK and result.stored is not None
    assert [i for i, _ in result.ignored] == list(at)
    dst = boot.read(DST, LEN)
    assert bytes(dst.data) == bytes(range(256)) * (LEN // 256)
    assert dst.weakest() is Confidence.HYPOTHESIS


def test_registers_persist_but_a_fact_without_a_value_forgets():
    boot = StockBoot()
    run(stock(), boot=boot)
    reg = boot.regs[11]
    assert reg is not None and reg.lo == 0x400
    run(stock(), length=0x800, boot=boot)
    assert boot.regs[11] is None
    assert boot.regs[1] is not None and boot.regs[1].exact == 0x800


def test_backing_fills_pages_made_before_it_below_written_bytes():
    boot = Boot()
    boot.put(0x60001000, b"\x11" * 16, "written")
    boot.read(0x60001100, 16)  # makes the page, unknown there
    src = boot.back(0x60001000, bytes(range(256)), "image")
    got = boot.read(0x60001000, 256)
    assert bytes(got.data[:16]) == b"\x11" * 16
    assert bytes(got.data[16:]) == bytes(range(16, 256)) and all(got.known)
    assert set(got.src[16:]) == {src} and list(got.off[16:]) == list(range(16, 256))


def test_sources_stay_bounded_over_jobs():
    boot = StockBoot()
    run(stock(), boot=boot)
    n = len(boot.sources)
    for _ in range(3):
        run(stock(), boot=boot)
    assert len(boot.sources) == n


def statements(program: Program) -> list[Bundle]:
    """`program` as bundles: a label at each target, each control op naming it."""
    words = program.words
    names = {w.target: f"w{w.target}" for w in words if w.target is not None}
    return [Bundle.of(w, names, i) for i, w in enumerate(words)]


def inserted(
    k: int, word: Word, after_label: bool = False, base: Program | None = None
) -> Program:
    """`base` (default the stock copy) assembled with `word` inserted before stock word k,
    which keeps its label (or hands it to the inserted word, so that a control word targeting
    k targets the inserted word)."""
    stmts = statements(base or stock())
    new = raw(word)
    if after_label and k < len(stmts) and stmts[k].label is not None:
        new = Label(stmts[k].label) | new
        stmts[k] = Bundle(tuple(p for p in stmts[k].parts if not isinstance(p, Label)))
    stmts.insert(k, new)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", HypothesisFieldWarning)
        return assemble(stmts)


LOAD = Word.from_fields({"f313_319": 0x1A, "f15_22": 1, "f123_127": 5, "f128_132": 0})
"""A 0x1a read of local word r5 + 1, inside what its op semantics covers."""


@pytest.mark.parametrize("k", [0, 12, 40, 60])
def test_an_idle_insert_keeps_the_copy(k: int):
    """The stock words keep their semantics where an insert moved them, as hypotheses."""
    _, base = run(stock())
    result, boot = run(inserted(k, IDLE))
    dst = boot.read(DST, LEN)
    assert result.status is Status.OK
    assert (dst.data, dst.known) == (
        base.read(DST, LEN).data,
        base.read(DST, LEN).known,
    )
    assert result.taint is Confidence.HYPOTHESIS


def test_an_insert_after_the_last_word_moves_nothing():
    stock_result, base = run(stock())
    result, boot = run(inserted(len(stock()), IDLE))
    assert result.taint is stock_result.taint
    assert boot.read(DST, LEN).taint == base.read(DST, LEN).taint


def any_operands(code: int, imm: int) -> Word:
    """Op `code` on r1 and r2 or imm, in the operand form its family takes."""
    fmt = FORMATS[code]
    given = {"rb": G[2] if fmt.family.rb else None, "imm": None if fmt.imm is None else imm}
    return OP(code, ra=G[1], **{k: v for k, v in given.items() if v is not None}).word()


COMPUTING = sorted(
    {c for c, s in OP_SEMANTICS.items() if s.rule is not None} | {0x0B, 0x38, 0x44}
)
RING_IMMS = [x for x in range(0x200) if ring_length(x)[0] is not None]


@pytest.mark.parametrize("code", COMPUTING, ids=hex)
def test_an_op_over_exact_intervals_is_what_the_machine_computes(code: int):
    """Where the interval evaluation gives a value, it and its tier are the machine's."""
    rng = random.Random(code)
    for _ in range(20):
        a, b = rng.randrange(-0x800, 0x800), rng.randrange(-0x800, 0x800)
        imm = rng.choice(RING_IMMS) if code == 0x44 else rng.randrange(32)
        word = Word.from_fields(
            {"f272_273": 1, "f224_228": 5}, base=any_operands(code, imm)
        )
        m = Machine(Boot())
        m.g[1], m.g[2] = Sv(a & 0xFFFFFFFF), Sv(b & 0xFFFFFFFF)
        m.execute(0, word, 0)
        regs = RegisterFile()
        regs[1] = Reg(a, a, Confidence.VERIFIED, "a")
        regs[2] = Reg(b, b, Confidence.VERIFIED, "b")
        effect = op_effect(OP_SEMANTICS[code], word, regs)
        assert effect is not None and effect.computes
        if effect.result is None:
            continue
        got = Sv(effect.result.lo & 0xFFFFFFFF, effect.result.taint)
        assert got == m.g[5], (a, b, imm)


@pytest.mark.parametrize(
    "r, tier", [(0, Confidence.VERIFIED), (1, Confidence.INFERRED)]
)
def test_a_select_is_the_hull_of_its_inputs_at_the_measured_r_fields(r: int, tier):
    """With the R file not modelled, 0x08 gives the hull of ra and rb; verified at an f120_122
    measured with R both 0 and not, inferred at the others (as the machine has it)."""
    word = Word.from_fields({"f120_122": r}, base=e_op(0x08, 1, 2))
    regs = RegisterFile()
    regs[1] = Reg(3, 5, Confidence.VERIFIED, "a")
    regs[2] = Reg(9, 9, Confidence.VERIFIED, "b")
    effect = op_effect(OP_SEMANTICS[0x08], word, regs)
    assert effect is not None and effect.result is not None
    assert (effect.result.lo, effect.result.hi, effect.result.taint) == (3, 9, tier)


def test_a_result_goes_to_its_port_registers_or_is_open():
    const = OP(0x0B, ra=G[0], imm=1).word()
    both = Word.from_fields(
        {"f267_268": 1, "f211_215": 5, "f272_273": 1, "f224_228": 6}, base=const
    )
    assert destinations(both) == ([(RegSet.XREG, 5), (RegSet.OPERAND, 6)], "")
    # A port that writes nothing (f267_268 = 2) is no destination, open or not.
    idle = Word.from_fields({"f267_268": 2, "f211_215": 5}, base=both)
    assert destinations(idle) == ([(RegSet.OPERAND, 6)], "")
    assert destinations(const)[1] == "no PR port enabled: where the result goes is open"
    other = Word.from_fields({"f272_273": 2, "f224_228": 4}, base=const)
    assert destinations(other) == ([], "where the result goes under f272_273=2 is open")


def test_an_inserted_op_takes_its_semantics_tier():
    end = len(stock())
    stock_result, base = run(stock())
    result, boot = run(inserted(end, LOAD))
    assert result.taint is stock_result.taint
    weak = replace(OP_SEMANTICS[0x1A], confidence=Confidence.HYPOTHESIS)
    boot = StockBoot()
    boot.put(SRC, bytes(range(256)) * (LEN // 256), "src")
    weakened = Copy16Model(stock(), ops={**OP_SEMANTICS, 0x1A: weak})
    result = run_model(
        boot, weakened, commands.copy(inserted(end, LOAD), SRC, DST, LEN)
    )
    dst = boot.read(DST, LEN)
    assert result.status is Status.OK and dst.data == base.read(DST, LEN).data
    assert result.taint is Confidence.HYPOTHESIS
    assert dst.weakest() is Confidence.HYPOTHESIS


@pytest.mark.parametrize(
    "word, at, why",
    [
        (
            lambda: Word.from_fields({"f313_319": 0x4A}, base=IDLE),
            12,
            "op4a has no op semantics",
        ),
        # A word load at r2, (len + 0x1f) / 16 at the engine site: not word-aligned.
        (
            lambda: OP(0x1A, ra=G[2], imm=0).word(),
            65,
            "outside what its op semantics covers",
        ),
        (
            lambda: Word.from_fields({"f313_319": 0x38}, base=IDLE),
            12,
            "no PR port enabled: where the result goes is open",
        ),
        (lambda: stock().words[42], 12, "an inserted register transfer"),
    ],
    ids=["no-row", "unaligned", "no-port", "transfer"],
)
def test_an_insert_without_semantics_is_unknown(word, at: int, why: str):
    program = inserted(at, word())
    with pytest.raises(UnknownSemantics, match=f"word {at}: .*{why}"):
        run(program)
    result, boot = run(program, strict=False)
    assert [i for i, _ in result.ignored] == [at]
    assert boot.read(DST, LEN).weakest() is Confidence.HYPOTHESIS


def test_an_insert_must_keep_the_flow():
    """A word inserted in a control word's delay slots, a target left on the moved word's old
    index and a target on the inserted word have no semantics, in lenient mode too."""
    with pytest.raises(UnknownSemantics, match="delay slots of word 45"):
        run(inserted(47, IDLE), strict=False)
    words = list(stock().words)
    words.insert(20, IDLE)  # 53's target 50 now names stock word 49
    with pytest.raises(UnknownSemantics, match="word 53: no semantics for f15_22"):
        run(Program(tuple(words)))
    with pytest.raises(UnknownSemantics, match="its target 58 is no stock word"):
        run(inserted(58, IDLE, after_label=True), strict=False)


def port(word: Word, enable: str, reg: int) -> Word:
    field = {"f267_268": "f211_215", "f272_273": "f224_228"}[enable]
    return Word.from_fields({enable: 1, field: reg}, base=word)


def engine(words: tuple[Word, ...], reader: int = 68, length: int = 0x1000):
    """The copy with `words` inserted after its word 64 and `reader` (68: load offset, 73:
    count) reading r12 through its xreg; (result, dst bytes)."""
    base = list(stock().words)
    base[reader] = Word.from_fields({"xreg": 12}, base=base[reader])
    program = Program(tuple(base))
    for w in reversed(words):
        program = inserted(65, w, base=program)
    return run(program, length)


def d_op(code: int, ra: int, c: int) -> Word:
    """An immediate op whose constant is c as the transfers read it, imm = c / 16."""
    return OP(code, ra=G[ra], imm=c >> 4).word()


def e_op(code: int, ra: int, rb: int) -> Word:
    return OP(code, ra=G[ra], rb=G[rb]).word()


def test_an_engine_op_sets_the_load_offset():
    """ra = r2 = (len + 0x1f) / 16 after word 64; 0x1f adds c / 16; word 68 offsets the load
    by 16 x the value / 32 in 32-byte steps, so dst[o:] = src[:len - o] and dst[:o] is unknown."""
    result, boot = engine((port(d_op(0x1F, 2, 0x400), "f267_268", 12),))
    o = (0x1000 + 0x400) // 32 & ~0x1F
    dst = boot.read(DST, 0x1000)
    assert result.status is Status.OK and o == 0xA0
    assert bytes(dst.data[o:]) == (bytes(range(256)) * 16)[: 0x1000 - o]
    assert all(dst.known[o:]) and not any(dst.known[:o])
    assert dst.weakest() is Confidence.HYPOTHESIS  # stock words off their indices


@pytest.mark.parametrize("gap", [0, 2])
def test_an_operand_chain_feeds_a_count(gap: int):
    """min(r2, 4) = 4 into r13 through port 272, read `gap` + 1 words on by a compare through
    port 267 into r12: true is 1, so word 73 stores 16 bytes. A port write is read from the
    next word on."""
    words = (
        port(d_op(0x25, 2, 0x40), "f272_273", 13),
        *(IDLE,) * gap,
        port(e_op(0x31, 13, 13), "f267_268", 12),
    )
    result, boot = engine(words, reader=73)
    dst = boot.read(DST, 0x1000)
    assert result.status is Status.OK and all(dst.known)
    assert bytes(dst.data) == bytes(range(16)) + b"\xa5" * (0x1000 - 16)
    with pytest.raises(UnknownSemantics, match="no exact multiple of 16"):
        engine((port(e_op(0x08, 7, 7), "f267_268", 12),), reader=73, length=0x2000)


def test_a_mask_through_the_count_reads_in_16_byte_units():
    """r2 & 0x1080 at len 0x1100: r2 = 0x111 masks to 0, so word 73 stores nothing, as on the
    camera; read in bytes, r2 & 0x1080 would have stored 0x1000 bytes."""
    mask = port(OP(0x38, ra=G[2], imm=0x1080).word(), "f267_268", 12)
    result, boot = engine((mask,), reader=73, length=0x1100)
    assert result.status is Status.OK
    assert bytes(boot.read(DST, 0x1100).data) == b"\xa5" * 0x1100


@pytest.mark.parametrize(
    "words, why",
    [
        ((port(d_op(0x25, 2, -0x400), "f267_268", 12),), "a negative value"),
        (
            (
                port(
                    Word.from_fields(
                        {"f120_122": 0}, base=OP(0x46, ra=G[2], imm=1).word()
                    ),
                    "f267_268",
                    12,
                ),
            ),
            "a value the model does not know",
        ),
        (
            (
                port(d_op(0x24, 2, 0x7FFF0), "f272_273", 13),
                IDLE,
                IDLE,
                port(OP(0x40, ra=G[13], rb=G[13], imm=4).word(), "f267_268", 12),
            ),
            "beyond the buffer",
        ),
    ],
    ids=["negative", "hidden-r", "beyond"],
)
def test_an_engine_value_the_load_cannot_place_is_unknown(words, why):
    with pytest.raises(UnknownSemantics, match=why):
        engine(words)


def test_an_engine_write_off_the_site_is_unknown():
    with pytest.raises(UnknownSemantics, match="no modelled reader off the site"):
        run(inserted(12, port(d_op(0x1F, 2, 0), "f267_268", 12)))


def test_an_op_on_one_register_reads_one_value():
    """0x31 on (r7, r7): r7 is an interval, but equal to itself, so the flag is true."""
    result, boot = engine(
        (port(e_op(0x31, 7, 7), "f267_268", 12),), reader=73, length=0x2000
    )
    assert result.status is Status.OK
    assert bytes(boot.read(DST, 0x1000).data[:16]) == bytes(range(16))


def test_an_engine_value_for_another_reader_is_unknown():
    base = list(stock().words)
    base[76] = Word.from_fields({"xreg": 12}, base=base[76])
    program = inserted(
        65, port(d_op(0x1F, 2, 0x400), "f267_268", 12), base=Program(tuple(base))
    )
    with pytest.raises(UnknownSemantics, match="a role not measured with one"):
        run(program, 0x1000)
