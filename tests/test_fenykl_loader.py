"""The FENYKL.BIN boot loader patches (sdk/arch/<camera>/loader/, py/fenykl/patching.py).

For every camera with a loader: the pinned bytes must be what the source assembles to; every CALL in
the stub must use the register list and frame of the firmware's own call sites; the patch must change
only the hook and the stub; the loader firmware built from firmware/ must stay byte-identical to the
pinned build. The pinned bytes must behave as specified in the GDB simulator (tests/loader_sim/ for the
E-PL3, tests/loader_sim_em10ii/ for the E-M10 Mark II). Run this file as a script to print the bytes
and rewrite the listings after changing a loader.S; paste the bytes into its patch.toml (and, if an
edit changes length, the sha256 of the stock bytes it now covers into its original_sha256).
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
from fenykl.patching import PATCHES, Patch, apply_patches


@dataclass(frozen=True)
class Loader:
    name: str
    sha256: str
    """sha256 of `fenykl patch apply <name>/fenykl-loader` on the stock image."""
    private: frozenset[str]
    """loader.ld symbols that firmware.toml does not export to plugins."""

    @property
    def target(self) -> sdk.SdkTarget:
        return sdk.target(self.name)

    @property
    def dir(self) -> Path:
        return self.target.dir / "loader"

    @property
    def patch(self) -> Patch:
        return PATCHES[f"{self.name}/fenykl-loader"]

    @property
    def base(self) -> int:
        return TARGETS[self.name].load_addresses["main-code-01"]


LOADERS = {
    # README.md: the loader firmware with the FNL2 header (model check), reads C:\FENYKL.BIN.
    "epl3": Loader(
        "epl3",
        "ac1db3e8cc54a1f41c4daba261a5058c228f94640548a53ba4be7d93ad509c32",
        frozenset({"OlyTaskSelfEnd"}),
    ),
    "em10ii": Loader(
        "em10ii",
        "6eb1bb10ea34bf58142e6f6554d76d75ca9915fe151a8d83a205b5d4e2a9f371",
        frozenset({"task_self_end"}),
    ),
}

EPL3 = sdk.target("epl3")
SIM = Path(__file__).resolve().parent / "loader_sim"
HOOK, STUB = LOADERS["epl3"].patch.edits
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


def assemble(loader: Loader, workdir: Path) -> Assembled:
    target = loader.target
    obj = workdir / "loader.o"
    elf = workdir / "loader.elf"
    _run([target.tool("as"), "-o", str(obj), str(loader.dir / "loader.S")])
    _run(
        [
            target.tool("ld"),
            "-T",
            str(loader.dir / "loader.ld"),
            "--orphan-handling=error",
            "-o",
            str(elf),
            str(obj),
        ]
    )
    sections = {}
    for name in [".hook", ".text"]:
        out = workdir / f"{name[1:]}.bin"
        _run([target.tool("objcopy"), "-O", "binary", "-j", name, str(elf), str(out)])
        sections[name] = out.read_bytes()
    disassembly = _run([target.tool("objdump"), "-d", "-z", str(elf)])
    listing = disassembly[disassembly.index("Disassembly of section") :]
    return Assembled(sections[".hook"], sections[".text"], listing)


def firmware_symbols(loader: Loader) -> dict[str, int]:
    text = (loader.dir / "loader.ld").read_text()
    return {
        name: int(value, 16)
        for name, value in re.findall(r"^(\w+) = (0x[0-9a-f]+);$", text, re.MULTILINE)
    }


def main_code(name: str) -> bytes:
    firmware = unpack_firmware(stock_image(name))
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


@pytest.fixture(scope="module", params=list(LOADERS))
def loader(request) -> Loader:
    return LOADERS[request.param]


@pytest.fixture(scope="module")
def assembled(loader, tmp_path_factory) -> Assembled:
    return assemble(loader, tmp_path_factory.mktemp(f"loader_{loader.name}"))


@needs_toolchain
def test_pinned_bytes_are_the_assembled_source(loader, assembled):
    hook, stub = loader.patch.edits
    assert assembled.hook == hook.replacement, assembled.hook.hex(" ")
    assert assembled.stub == stub.replacement, assembled.stub.hex(" ")


@needs_toolchain
def test_listing_is_up_to_date(loader, assembled):
    assert (loader.dir / "loader.lst").read_text() == assembled.listing


def test_firmware_symbols_agree_with_the_sdk(loader):
    toml = tomllib.loads((loader.target.dir / "firmware.toml").read_text())
    for name, address in firmware_symbols(loader).items():
        if name not in loader.private:
            assert address == toml[name]["address"], name


def test_calls_match_the_firmware_call_sites(loader):
    code = main_code(loader.name)
    stub = loader.patch.edits[1]
    stub_calls = 0
    for name, address in firmware_symbols(loader).items():
        in_stub = call_encodings(stub.replacement, loader.base + stub.offset, address)
        if not in_stub:
            continue
        stub_calls += in_stub.total()
        in_firmware = call_encodings(code, loader.base, address)
        assert in_firmware.keys() == in_stub.keys(), (name, in_firmware, in_stub)
    stub_listing = (
        (loader.dir / "loader.lst")
        .read_text()
        .split("Disassembly of section .text:")[1]
    )
    assert stub_calls == len(re.findall(r"\tcall\t", stub_listing))


def test_patch_changes_only_the_hook_and_the_stub(loader):
    stock = stock_image(loader.name)
    patched = unpack_firmware(apply_patches(stock, [loader.patch]))
    index = patched.find_block("main-code-01")
    after = patched.blocks[index].body
    expected = bytearray(main_code(loader.name))
    for edit in loader.patch.edits:
        expected[edit.offset : edit.end] = edit.replacement
    assert after == bytes(expected)


def test_loader_firmware_matches_the_pinned_build(loader, tmp_path):
    stock_image(loader.name)
    output = tmp_path / "loader.BIN"

    cli.main(
        ["patch", "apply", f"{loader.name}/fenykl-loader", "--output", str(output)]
    )

    assert hashlib.sha256(output.read_bytes()).hexdigest() == loader.sha256


# --- simulator ---------------------------------------------------------------------------------


def _image(test_payload: bytes, target: sdk.SdkTarget, pad_to: int = 0) -> bytes:
    body = test_payload + bytes(
        max(0, pad_to - payload.HEADER_SIZE - len(test_payload))
    )
    return payload.make_image(body, payload.HEADER_SIZE, target)


def _with_header(image: bytes, **fields: int | bytes) -> bytes:
    header = payload.parse_header(image)
    values = [
        fields.get(f, getattr(header, f))
        for f in ("magic", "load", "size", "entry", "sum", "model")
    ]
    return struct.pack("<5I8s", *values) + image[payload.HEADER_SIZE :]


def _fnl1_image(image: bytes, target: sdk.SdkTarget) -> bytes:
    """`image` in the old FNL1 format: 0x14-byte header without the model, otherwise valid."""
    body = image[payload.HEADER_SIZE :]
    header = struct.pack(
        "<5I", 0x314C4E46, target.load, 0x14 + len(body), 0x14, payload.word_sum(body)
    )
    return header + body


def _flip(field: bytes, index: int) -> bytes:
    return field[:index] + bytes([field[index] ^ 1]) + field[index + 1 :]


def file_cases(
    test_payload: bytes, target: sdk.SdkTarget
) -> dict[str, tuple[bytes, bool]]:
    """FENYKL.BIN contents a mounted card can hold: name -> (file, accepted by the loader)."""
    good = _image(test_payload, target)
    size = len(good)
    model = payload.model_field(target.model)
    return {
        "valid": (good, True),
        "valid with trailing bytes": (good + b"junk" * 3, True),
        "maximum size": (_image(test_payload, target, target.max_size), True),
        "empty file": (b"", False),
        "truncated header": (good[:0x10], False),
        "truncated image": (good[:-4], False),
        "bad magic": (_with_header(good, magic=0x334C4E46), False),
        "old FNL1 image": (_fnl1_image(good, target), False),
        "other model": (_with_header(good, model=_flip(model, 4)), False),
        "model differs in the first word": (
            _with_header(good, model=_flip(model, 3)),
            False,
        ),
        "model not zero-padded": (_with_header(good, model=model[:7] + b" "), False),
        "bad load address": (_with_header(good, load=target.load + 4), False),
        "size below header": (_with_header(good, size=0x10), False),
        "size not a multiple of 4": (_with_header(good, size=size - 2), False),
        "entry at size": (_with_header(good, entry=size), False),
        "entry in the header": (_with_header(good, entry=0x10), False),
        "entry in the model field": (_with_header(good, entry=0x18), False),
        "entry at the last header byte": (_with_header(good, entry=0x1B), False),
        "bad sum": (_with_header(good, sum=payload.parse_header(good).sum ^ 1), False),
        "file larger than the region": (
            _with_header(good + bytes(target.max_size), size=size + target.max_size),
            False,
        ),
    }


@dataclass(frozen=True)
class Scenario:
    file: bytes
    accepted: bool
    fail_opens: int = 0
    """Opens that fail before the card is mounted."""
    file_present: bool = True


def scenarios(test_payload: bytes) -> dict[str, Scenario]:
    good = _image(test_payload, EPL3)
    return {
        **{
            name: Scenario(file, ok)
            for name, (file, ok) in file_cases(test_payload, EPL3).items()
        },
        "valid after 5 failed opens": Scenario(good, True, fail_opens=5),
        "valid on the last try": Scenario(good, True, fail_opens=99),
        "no card": Scenario(good, True, fail_opens=1000),
        "no file": Scenario(good, True, file_present=False),
        "no file, mounted after 5 tries": Scenario(
            good, True, fail_opens=5, file_present=False
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
        for name, address in firmware_symbols(LOADERS["epl3"]).items()
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


# --- E-M10 Mark II simulator --------------------------------------------------------------------

EM10II = sdk.target("em10ii")
SIM_EM10II = Path(__file__).resolve().parent / "loader_sim_em10ii"
# Image (stub, mocks, hook, sec3 mocks, payload area, harness), the gate and Usr3's stack, the
# uncached words (loaded-section mask, fat_mounted). CHCTR (0x8070) is in the simulator's default
# memory 0..0xFFFFF.
SIM_MEMORY_EM10II = [
    "0x60000000,0x1000000",
    "0x61800000,0x100000",
    "0xa1c00000,0x100000",
]
# Usr3 after the hooked CALL: ret [d2,a2,a3],16
USR3_TAIL_RET = bytes.fromhex("df b0 10")
SECTRIES = 300
TRIES = 100
SEC45 = 0x18


@dataclass(frozen=True)
class Em10iiScenario:
    file: bytes
    accepted: bool
    gate: int = 1
    """u16 0x61843F64 at the hook."""
    loaded0: int = 0x1F
    """Loaded-section mask at the hook; it becomes loaded1 after sec_after stub sleeps."""
    loaded1: int = 0x1F
    sec_after: int = 1
    mount_after: int = 0
    """Task sleeps before fat_mounted[0] becomes 1 (0 = mounted at the start)."""
    file_present: bool = True


def em10ii_scenarios(test_payload: bytes) -> dict[str, Em10iiScenario]:
    good = _image(test_payload, EM10II)
    epl3 = _image(test_payload, EPL3)
    return {
        **{
            name: Em10iiScenario(file, ok)
            for name, (file, ok) in file_cases(test_payload, EM10II).items()
        },
        "E-PL3 image": Em10iiScenario(epl3, False),
        "gate 0 (wake boot)": Em10iiScenario(good, True, gate=0),
        "gate 2": Em10iiScenario(good, True, gate=2),
        "gate 0x101": Em10iiScenario(good, True, gate=0x101),
        "sec4 and sec5 loaded after 5 sleeps": Em10iiScenario(
            good, True, loaded0=0x07, sec_after=5
        ),
        "sec5 loaded on the last try": Em10iiScenario(
            good, True, loaded0=0x0F, sec_after=SECTRIES - 1
        ),
        "sec5 never loaded": Em10iiScenario(good, True, loaded0=0x0F, sec_after=10000),
        "sec4 not loaded, sec5 bit set": Em10iiScenario(
            good, True, loaded0=0x17, sec_after=10000
        ),
        "valid after 5 sleeps for the mount": Em10iiScenario(good, True, mount_after=5),
        "valid, mounted on the last try": Em10iiScenario(
            good, True, mount_after=TRIES - 1
        ),
        "no card": Em10iiScenario(good, True, mount_after=10000),
        "no file": Em10iiScenario(good, True, file_present=False),
        "no file, mounted after 5 sleeps": Em10iiScenario(
            good, True, mount_after=5, file_present=False
        ),
    }


@dataclass(frozen=True)
class Em10iiExpected:
    created: int
    stub_sleeps: int
    task_sleeps: int
    fopens: int
    opened: int
    called: int


def em10ii_expected(scenario: Em10iiScenario) -> Em10iiExpected:
    if scenario.gate != 1:
        return Em10iiExpected(0, 0, 0, 0, 0, 0)
    if scenario.loaded0 & SEC45 == SEC45:
        stub_sleeps = 0
    elif scenario.sec_after < SECTRIES and scenario.loaded1 & SEC45 == SEC45:
        stub_sleeps = scenario.sec_after
    else:
        return Em10iiExpected(0, SECTRIES, 0, 0, 0, 0)
    if scenario.mount_after >= TRIES:
        return Em10iiExpected(1, stub_sleeps, TRIES, 0, 0, 0)
    opened = int(scenario.file_present)
    called = int(opened and scenario.accepted)
    return Em10iiExpected(1, stub_sleeps, scenario.mount_after, 1, opened, called)


@pytest.fixture(scope="module")
def em10ii_sim_objects(tmp_path_factory) -> tuple[Path, list[Path]]:
    """Return the test payload body and the harness objects."""
    work = tmp_path_factory.mktemp("loader_sim_em10ii")
    payload_obj = work / "payload.o"
    payload_elf = work / "payload.elf"
    payload_bin = work / "payload.bin"
    _run([EM10II.tool("as"), "-o", str(payload_obj), str(SIM_EM10II / "payload.S")])
    _run(
        [
            EM10II.tool("ld"),
            f"-Ttext=0x{EM10II.load + payload.HEADER_SIZE:x}",
            "-o",
            str(payload_elf),
            str(payload_obj),
        ]
    )
    _run(
        [
            EM10II.tool("objcopy"),
            "-O",
            "binary",
            "-j",
            ".text",
            str(payload_elf),
            str(payload_bin),
        ]
    )
    objects = [work / "harness.o", work / "check.o"]
    _run([EM10II.tool("as"), "-o", str(objects[0]), str(SIM_EM10II / "harness.S")])
    _run(
        [
            EM10II.tool("gcc"),
            *EM10II.cflags,
            "-c",
            str(SIM_EM10II / "check.c"),
            "-o",
            str(objects[1]),
        ]
    )
    return payload_bin, objects


def _em10ii_scenario_source(scenario: Em10iiScenario, file: Path) -> str:
    expected = em10ii_expected(scenario)
    values = {
        "gate": scenario.gate,
        "loaded0": scenario.loaded0,
        "loaded1": scenario.loaded1,
        "sec_after": scenario.sec_after,
        "mount_after": scenario.mount_after,
        "file_present": int(scenario.file_present),
        "expect_created": expected.created,
        "expect_stub_sleeps": expected.stub_sleeps,
        "expect_task_sleeps": expected.task_sleeps,
        "expect_fopens": expected.fopens,
        "expect_opened": expected.opened,
        "expect_called": expected.called,
        "expect_size": payload.parse_header(scenario.file).size
        if expected.called
        else 0,
        "file_len": len(scenario.file),
    }
    hook, stub = LOADERS["em10ii"].patch.edits
    hook_bytes = ",".join(f"0x{b:02x}" for b in hook.replacement + USR3_TAIL_RET)
    stub_bytes = ",".join(f"0x{b:02x}" for b in stub.replacement)
    lines = [
        '\t.section .hook,"ax"',
        f"\t.byte {hook_bytes}",
        '\t.section .stub,"ax"',
        f"\t.byte {stub_bytes}",
        "\t.data",
        "\t.balign 4",
    ]
    for name, value in values.items():
        lines += [f"\t.global _scn_{name}", f"_scn_{name}:\t.long {value}"]
    lines += ["\t.global _scn_file", f'_scn_file:\t.incbin "{file}"', ""]
    return "\n".join(lines)


@needs_toolchain
@pytest.mark.parametrize("name", list(em10ii_scenarios(bytes(4))))
def test_em10ii_loader_in_simulator(name, em10ii_sim_objects, tmp_path):
    payload_bin, objects = em10ii_sim_objects
    scenario = em10ii_scenarios(payload_bin.read_bytes())[name]
    accepted = payload.check_image(scenario.file) is None and payload.parse_header(
        scenario.file
    ).model == payload.model_field(EM10II.model)
    assert accepted == scenario.accepted

    file = tmp_path / "FENYKL.BIN"
    file.write_bytes(scenario.file)
    source = tmp_path / "scenario.S"
    source.write_text(_em10ii_scenario_source(scenario, file))
    scenario_obj = tmp_path / "scenario.o"
    _run([EM10II.tool("as"), "-o", str(scenario_obj), str(source)])
    placement = [
        f"--section-start=.fw.{symbol}=0x{address:x}"
        for symbol, address in firmware_symbols(LOADERS["em10ii"]).items()
    ]
    elf = tmp_path / "sim.elf"
    _run(
        [
            EM10II.tool("ld"),
            "-T",
            str(SIM_EM10II / "sim.ld"),
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
            EM10II.tool("run"),
            *(
                arg
                for region in SIM_MEMORY_EM10II
                for arg in ("--memory-region", region)
            ),
            str(elf),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"exit status {result.returncode} (see tests/loader_sim_em10ii/check.c, harness.S)\n"
        f"{result.stdout}{result.stderr}"
    )


if __name__ == "__main__":
    import tempfile

    for loader in LOADERS.values():
        with tempfile.TemporaryDirectory() as tmp:
            out = assemble(loader, Path(tmp))
        (loader.dir / "loader.lst").write_text(out.listing)
        print(
            f"{loader.name}\nHOOK = {out.hook.hex(' ')}\n"
            f"STUB ({len(out.stub)} bytes) = {out.stub.hex(' ')}"
        )
