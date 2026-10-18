"""The E-PL3 FENYKL.BIN boot loader patch (sdk/arch/epl3/loader/, py/fenykl/patching.py).

The pinned bytes must be what the source assembles to; every CALL in the stub must use the register
list and frame of the firmware's own call sites; the pinned bytes must behave as specified in the
GDB simulator (tests/loader_sim/); the loader firmware built from firmware/ must stay byte-identical
to the one in README.md. Run this file as a script to print the bytes and rewrite the
listing after changing loader.S; paste the bytes into patch.toml (and, if an edit changes length,
the sha256 of the stock bytes it now covers into its original_sha256).
"""

import hashlib
import re
import struct
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import needs_toolchain, stock_image

from fenykl import cli, payload, sdk
from fenykl.firmware import TARGETS, unpack_firmware
from fenykl.patching import PATCHES, apply_patches

EPL3 = sdk.target("epl3")
SOURCE = EPL3.dir / "loader" / "loader.S"
LINKER_SCRIPT = EPL3.dir / "loader" / "loader.ld"
LISTING = EPL3.dir / "loader" / "loader.lst"
# README.md: the loader firmware with the FNL2 header (model check), reads C:\FENYKL.BIN.
LOADER_SHA256 = "ac1db3e8cc54a1f41c4daba261a5058c228f94640548a53ba4be7d93ad509c32"
SIM = Path(__file__).resolve().parent / "loader_sim"
LOADER = PATCHES["epl3/fenykl-loader"]
HOOK, STUB = LOADER.edits
BASE = TARGETS["epl3"].load_addresses["main-code-01"]
# The image and the harness; fat_mounted at 0x6059C978.
SIM_MEMORY = ["0x6e600000,0x600000", "0x60500000,0x100000"]
# usr1_task after the hooked CALL: ret [d2],8
USR1_TAIL_RET = bytes.fromhex("df 80 08")


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    assert result.returncode == 0, f"{' '.join(cmd)}\n{result.stdout}{result.stderr}"
    return result.stdout


@dataclass(frozen=True)
class Assembled:
    hook: bytes
    stub: bytes
    listing: str


def assemble(workdir: Path) -> Assembled:
    obj = workdir / "loader.o"
    elf = workdir / "loader.elf"
    _run([EPL3.tool("as"), "-o", str(obj), str(SOURCE)])
    _run(
        [
            EPL3.tool("ld"),
            "-T",
            str(LINKER_SCRIPT),
            "--orphan-handling=error",
            "-o",
            str(elf),
            str(obj),
        ]
    )
    sections = {}
    for name in [".hook", ".text"]:
        out = workdir / f"{name[1:]}.bin"
        _run([EPL3.tool("objcopy"), "-O", "binary", "-j", name, str(elf), str(out)])
        sections[name] = out.read_bytes()
    disassembly = _run([EPL3.tool("objdump"), "-d", "-z", str(elf)])
    listing = disassembly[disassembly.index("Disassembly of section") :]
    return Assembled(sections[".hook"], sections[".text"], listing)


def firmware_symbols() -> dict[str, int]:
    text = LINKER_SCRIPT.read_text()
    return {
        name: int(value, 16)
        for name, value in re.findall(r"^(\w+) = (0x[0-9a-f]+);$", text, re.MULTILINE)
    }


def main_code() -> bytes:
    firmware = unpack_firmware(stock_image())
    return firmware.blocks[firmware.find_block("main-code-01")].body


def call_encodings(code: bytes, base: int, target: int) -> Counter[tuple[int, int]]:
    """(register list byte, frame) of every CALL (d16 or d32 form) in `code` that targets `target`."""
    found: Counter[tuple[int, int]] = Counter()
    for opcode, disp_size in [(0xCD, 2), (0xDD, 4)]:
        for match in re.finditer(bytes([opcode]), code):
            i = match.start()
            end = i + 1 + disp_size + 2
            if end > len(code):
                continue
            disp = int.from_bytes(
                code[i + 1 : i + 1 + disp_size], "little", signed=True
            )
            if (base + i + disp) & 0xFFFFFFFF == target:
                found[code[end - 2], code[end - 1]] += 1
    return found


@pytest.fixture(scope="module")
def assembled(tmp_path_factory) -> Assembled:
    return assemble(tmp_path_factory.mktemp("loader"))


@needs_toolchain
def test_pinned_bytes_are_the_assembled_source(assembled):
    assert assembled.hook == HOOK.replacement, assembled.hook.hex(" ")
    assert assembled.stub == STUB.replacement, assembled.stub.hex(" ")


@needs_toolchain
def test_listing_is_up_to_date(assembled):
    assert LISTING.read_text() == assembled.listing


def test_firmware_symbols_agree_with_the_sdk():
    toml = tomllib.loads((EPL3.dir / "firmware.toml").read_text())
    for name, address in firmware_symbols().items():
        if name != "OlyTaskSelfEnd":  # not exported to plugins
            assert address == toml[name]["address"], name


def test_calls_match_the_firmware_call_sites():
    code = main_code()
    stub_calls = 0
    for name, address in firmware_symbols().items():
        in_stub = call_encodings(STUB.replacement, BASE + STUB.offset, address)
        if not in_stub:
            continue
        stub_calls += in_stub.total()
        in_firmware = call_encodings(code, BASE, address)
        assert in_firmware.keys() == in_stub.keys(), (name, in_firmware, in_stub)
    stub_listing = LISTING.read_text().split("Disassembly of section .text:")[1]
    assert stub_calls == len(re.findall(r"\tcall\t", stub_listing))


def test_patch_changes_only_the_hook_and_the_stub():
    stock = stock_image()
    patched = unpack_firmware(apply_patches(stock, [LOADER]))
    index = patched.find_block("main-code-01")
    after = patched.blocks[index].body
    expected = bytearray(main_code())
    for edit in LOADER.edits:
        expected[edit.offset : edit.end] = edit.replacement
    assert after == bytes(expected)


def test_loader_firmware_matches_the_documented_build(tmp_path):
    stock_image()
    output = tmp_path / "loader.BIN"

    cli.main(["patch", "apply", "epl3/fenykl-loader", "--output", str(output)])

    assert hashlib.sha256(output.read_bytes()).hexdigest() == LOADER_SHA256


# --- simulator ---------------------------------------------------------------------------------


def _image(test_payload: bytes, pad_to: int = 0) -> bytes:
    body = test_payload + bytes(
        max(0, pad_to - payload.HEADER_SIZE - len(test_payload))
    )
    return payload.make_image(body, payload.HEADER_SIZE, EPL3)


def _with_header(image: bytes, **fields: int | bytes) -> bytes:
    header = payload.parse_header(image)
    values = [
        fields.get(f, getattr(header, f))
        for f in ("magic", "load", "size", "entry", "sum", "model")
    ]
    return struct.pack("<5I8s", *values) + image[payload.HEADER_SIZE :]


def _fnl1_image(image: bytes) -> bytes:
    """`image` in the old FNL1 format: 0x14-byte header without the model, otherwise valid."""
    body = image[payload.HEADER_SIZE :]
    header = struct.pack(
        "<5I", 0x314C4E46, EPL3.load, 0x14 + len(body), 0x14, payload.word_sum(body)
    )
    return header + body


@dataclass(frozen=True)
class Scenario:
    file: bytes
    accepted: bool
    fail_opens: int = 0
    """Opens that fail before the card is mounted."""
    file_present: bool = True


def scenarios(test_payload: bytes) -> dict[str, Scenario]:
    good = _image(test_payload)
    size = len(good)
    return {
        "valid": Scenario(good, True),
        "valid after 5 failed opens": Scenario(good, True, fail_opens=5),
        "valid on the last try": Scenario(good, True, fail_opens=99),
        "no card": Scenario(good, True, fail_opens=1000),
        "no file": Scenario(good, True, file_present=False),
        "no file, mounted after 5 tries": Scenario(
            good, True, fail_opens=5, file_present=False
        ),
        "valid with trailing bytes": Scenario(good + b"junk" * 3, True),
        "maximum size": Scenario(_image(test_payload, EPL3.max_size), True),
        "empty file": Scenario(b"", False),
        "truncated header": Scenario(good[:0x10], False),
        "truncated image": Scenario(good[:-4], False),
        "bad magic": Scenario(_with_header(good, magic=0x334C4E46), False),
        "old FNL1 image": Scenario(_fnl1_image(good), False),
        "other model": Scenario(_with_header(good, model=b"E-PL5\0\0\0"), False),
        "model differs in the first word": Scenario(
            _with_header(good, model=b"E-PM3\0\0\0"), False
        ),
        "model not zero-padded": Scenario(
            _with_header(good, model=b"E-PL3\0\0 "), False
        ),
        "bad load address": Scenario(_with_header(good, load=0x6EA70004), False),
        "size below header": Scenario(_with_header(good, size=0x10), False),
        "size not a multiple of 4": Scenario(_with_header(good, size=size - 2), False),
        "entry at size": Scenario(_with_header(good, entry=size), False),
        "entry in the header": Scenario(_with_header(good, entry=0x10), False),
        "entry in the model field": Scenario(_with_header(good, entry=0x18), False),
        "entry at the last header byte": Scenario(_with_header(good, entry=0x1B), False),
        "bad sum": Scenario(
            _with_header(good, sum=payload.parse_header(good).sum ^ 1), False
        ),
        "file larger than the region": Scenario(
            _with_header(good + bytes(EPL3.max_size), size=size + EPL3.max_size),
            False,
        ),
    }


@pytest.fixture(scope="module")
def sim_objects(tmp_path_factory) -> tuple[Path, list[Path]]:
    """Return the test payload body and the harness objects."""
    work = tmp_path_factory.mktemp("loader_sim")
    payload_obj = work / "payload.o"
    payload_elf = work / "payload.elf"
    payload_bin = work / "payload.bin"
    _run([EPL3.tool("as"), "-o", str(payload_obj), str(SIM / "payload.S")])
    _run(
        [
            EPL3.tool("ld"),
            f"-Ttext=0x{EPL3.load + payload.HEADER_SIZE:x}",
            "-o",
            str(payload_elf),
            str(payload_obj),
        ]
    )
    _run(
        [
            EPL3.tool("objcopy"),
            "-O",
            "binary",
            "-j",
            ".text",
            str(payload_elf),
            str(payload_bin),
        ]
    )
    objects = [work / "harness.o", work / "check.o"]
    _run([EPL3.tool("as"), "-o", str(objects[0]), str(SIM / "harness.S")])
    _run(
        [
            EPL3.tool("gcc"),
            *EPL3.cflags,
            "-c",
            str(SIM / "check.c"),
            "-o",
            str(objects[1]),
        ]
    )
    return payload_bin, objects


def _scenario_source(scenario: Scenario, file: Path) -> str:
    timed_out = scenario.fail_opens >= 100
    opened = not timed_out and scenario.file_present
    status = 1 if not opened else 0 if scenario.accepted else 2
    called = int(status == 0)
    values = {
        "fail_opens": scenario.fail_opens,
        "file_present": int(scenario.file_present),
        "expect_fopens": 100 if timed_out else scenario.fail_opens + 1,
        "expect_sleeps": 100 if timed_out else scenario.fail_opens,
        "expect_opened": int(opened),
        "expect_status": status,
        "expect_called": called,
        "expect_size": payload.parse_header(scenario.file).size if called else 0,
        "file_len": len(scenario.file),
    }
    hook = ",".join(f"0x{b:02x}" for b in HOOK.replacement + USR1_TAIL_RET)
    stub = ",".join(f"0x{b:02x}" for b in STUB.replacement)
    lines = [
        '\t.section .hook,"ax"',
        f"\t.byte {hook}",
        '\t.section .stub,"ax"',
        f"\t.byte {stub}",
        "\t.data",
        "\t.balign 4",
    ]
    for name, value in values.items():
        lines += [f"\t.global _scn_{name}", f"_scn_{name}:\t.long {value}"]
    lines += ["\t.global _scn_file", f'_scn_file:\t.incbin "{file}"', ""]
    return "\n".join(lines)


# The names do not depend on the payload; the real one is assembled by the sim_objects fixture.
@needs_toolchain
@pytest.mark.parametrize("name", list(scenarios(bytes(4))))
def test_loader_in_simulator(name, sim_objects, tmp_path):
    payload_bin, objects = sim_objects
    scenario = scenarios(payload_bin.read_bytes())[name]
    assert (payload.check_image(scenario.file) is None) == scenario.accepted

    file = tmp_path / "FENYKL.BIN"
    file.write_bytes(scenario.file)
    source = tmp_path / "scenario.S"
    source.write_text(_scenario_source(scenario, file))
    scenario_obj = tmp_path / "scenario.o"
    _run([EPL3.tool("as"), "-o", str(scenario_obj), str(source)])
    placement = [
        f"--section-start=.fw.{name}=0x{address:x}"
        for name, address in firmware_symbols().items()
    ]
    elf = tmp_path / "sim.elf"
    _run(
        [
            EPL3.tool("ld"),
            "-T",
            str(SIM / "sim.ld"),
            *placement,
            "--orphan-handling=place",
            "--no-warn-rwx-segments",
            "-o",
            str(elf),
            *map(str, objects),
            str(scenario_obj),
        ]
    )
    result = subprocess.run(
        [
            EPL3.tool("run"),
            *(arg for region in SIM_MEMORY for arg in ("--memory-region", region)),
            str(elf),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"exit status {result.returncode} (see tests/loader_sim/check.c, harness.S)\n"
        f"{result.stdout}{result.stderr}"
    )


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        out = assemble(Path(tmp))
    LISTING.write_text(out.listing)
    print(
        f"HOOK = {out.hook.hex(' ')}\nSTUB ({len(out.stub)} bytes) = {out.stub.hex(' ')}"
    )
