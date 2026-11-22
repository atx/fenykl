"""The FENYKL.BIN boot loader patches (sdk/arch/<camera>/loader/, py/fenykl/patching.py).

For every camera with a loader: the pinned bytes must be what the source assembles to; every CALL in
the stub must use the register list and frame of the firmware's own call sites; the patch must change
only the hook and the stub; the loader firmware built from firmware/ must stay byte-identical to the
pinned build. The pinned bytes must behave as specified in the GDB simulator
(tests/loader_sim_em10ii/). Run this file as a script to print the bytes
and rewrite the listings after changing a loader.S; paste the bytes into its patch.toml (and, if an
edit changes length, the sha256 of the stock bytes it now covers into its original_sha256).
"""

import hashlib
import re
import struct
import subprocess
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
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
    "em10ii": Loader(
        "em10ii",
        "0c42be0e6d151e2d28c5a377574ccd182e3de4178d6e5cd8229a2f4d43859b02",
        frozenset({"task_self_end", "fileif_exists", "pwr_wake_input_levels"}),
    ),
}


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


# --- E-M10 Mark II simulator --------------------------------------------------------------------

EM10II = sdk.target("em10ii")
SIM_EM10II = Path(__file__).resolve().parent / "loader_sim_em10ii"
# Image (stub, mocks, hook, sec3 mocks, payload area, harness), the gate and Usr3's stack, the
# uncached words (battery test, power-on factor, loaded-section mask). CHCTR (0x8070) is in the
# simulator's default memory 0..0xFFFFF.
SIM_MEMORY_EM10II = [
    "0x60000000,0x1000000",
    "0x61800000,0x100000",
    "0xa1b00000,0x200000",
]
# Usr3 after the hooked CALL: ret [d2,a2,a3],16
USR3_TAIL_RET = bytes.fromhex("df b0 10")
SECTRIES = 300
TRIES = 300
SEC45 = 0x18
FACTOR_SWITCH = 12
FACTOR_RELAY = 0x1000001
USB_NONE, USB_CABLE, USB_UNKNOWN = 1, 0, 0xFFFF
NOT_MOUNTED = 0x0600
DCF_MOUNT_RUNNING = 0x4700


@dataclass(frozen=True)
class Em10iiScenario:
    bin: bytes | None
    """FENYKL.BIN on the card (None: absent)."""
    tst: bytes | None = None
    """FENYKL.TST on the card (None: absent)."""
    gate: int = 1
    """u16 0x61843F64 at the hook."""
    factor: int = FACTOR_SWITCH
    levels: int = 0x10
    """pwr_wake_input_levels(): bit 0 = power switch (0 = on)."""
    batsrc: int = 0x100
    batlvl: int = 0
    usb_key: int = USB_NONE
    """ui_key_get_state(0x400)."""
    loaded0: int = 0x1F
    """Loaded-section mask at the hook; it becomes loaded1 after sec_after stub sleeps."""
    loaded1: int = 0x1F
    sec_after: int = 1
    mount_after: int = 0
    """Task sleeps before the media status reads ready (0 = ready at the start)."""
    busy_status: int = NOT_MOUNTED
    remove_result: int = 0
    exists_after: int = 0


def _accepted(file: bytes | None) -> bool:
    return (
        file is not None
        and payload.check_image(file) is None
        and payload.parse_header(file).model == payload.model_field(EM10II.model)
    )


def em10ii_scenarios(test_payload: bytes) -> dict[str, Em10iiScenario]:
    good = _image(test_payload, EM10II)
    # A distinct valid TST: its size tells which file ran.
    tst = _image(test_payload, EM10II, len(good) + 0x40)
    # A valid image for another camera: its model and load address.
    other = _image(test_payload, replace(EM10II, model="E-M5", load=0x6EA70000))
    bad_tst = _with_header(tst, sum=payload.parse_header(tst).sum ^ 1)
    relay = {"gate": 0, "factor": FACTOR_RELAY}
    return {
        **{
            name: Em10iiScenario(file)
            for name, (file, _) in file_cases(test_payload, EM10II).items()
        },
        "another camera's image": Em10iiScenario(other),
        # Boot kinds.
        "gate 0 (wake boot)": Em10iiScenario(good, gate=0),
        "gate 2": Em10iiScenario(good, gate=2),
        "gate 0x101": Em10iiScenario(good, gate=0x101),
        "switch boot, switch input reads off later": Em10iiScenario(good, levels=0x11),
        "switch boot with a cable": Em10iiScenario(good, usb_key=USB_CABLE),
        "relay": Em10iiScenario(good, **relay),
        "relay, battery level 50": Em10iiScenario(good, **relay, batsrc=0, batlvl=50),
        "relay, battery level 49": Em10iiScenario(good, **relay, batsrc=0, batlvl=49),
        "relay, battery source 0x101": Em10iiScenario(
            good, **relay, batsrc=0x101, batlvl=0
        ),
        "relay, switch off": Em10iiScenario(good, **relay, levels=0x11),
        "relay, cable in": Em10iiScenario(good, **relay, usb_key=USB_CABLE),
        "relay, unknown cable state": Em10iiScenario(
            good, **relay, usb_key=USB_UNKNOWN
        ),
        "relay, card mounted after 250 sleeps": Em10iiScenario(
            good, **relay, mount_after=250
        ),
        "relay, no card": Em10iiScenario(good, **relay, mount_after=10000),
        "relay, TST": Em10iiScenario(good, tst, **relay),
        "watchdog factor 0x1000002": Em10iiScenario(good, gate=0, factor=0x1000002),
        "cable boot factor 0x400": Em10iiScenario(good, gate=0, factor=0x400),
        "card insertion factor 0x1506": Em10iiScenario(good, gate=0, factor=0x1506),
        "factor 12 without the gate": Em10iiScenario(
            good, gate=0, factor=FACTOR_SWITCH
        ),
        "factor 0x1000003": Em10iiScenario(good, gate=0, factor=0x1000003),
        # Sections and card.
        "sec4 and sec5 loaded after 5 sleeps": Em10iiScenario(
            good, loaded0=0x07, sec_after=5
        ),
        "sec5 loaded on the last try": Em10iiScenario(
            good, loaded0=0x0F, sec_after=SECTRIES - 1
        ),
        "sec5 never loaded": Em10iiScenario(good, loaded0=0x0F, sec_after=10000),
        "sec4 not loaded, sec5 bit set": Em10iiScenario(
            good, loaded0=0x17, sec_after=10000
        ),
        "valid after 5 sleeps for the mount": Em10iiScenario(good, mount_after=5),
        "valid, mounted on the last try": Em10iiScenario(good, mount_after=TRIES - 1),
        "DCF mount running until the last try": Em10iiScenario(
            good, mount_after=TRIES - 1, busy_status=DCF_MOUNT_RUNNING
        ),
        "DCF mount never ends": Em10iiScenario(
            good, mount_after=10000, busy_status=DCF_MOUNT_RUNNING
        ),
        "no card": Em10iiScenario(good, mount_after=10000),
        "no file": Em10iiScenario(None),
        "no file, mounted after 5 sleeps": Em10iiScenario(None, mount_after=5),
        # FENYKL.TST.
        "TST and BIN": Em10iiScenario(good, tst),
        "TST only": Em10iiScenario(None, tst),
        "bad TST, BIN": Em10iiScenario(good, bad_tst),
        "bad TST only": Em10iiScenario(None, bad_tst),
        "empty TST, BIN": Em10iiScenario(good, b""),
        "another camera's TST, BIN": Em10iiScenario(good, other),
        "TST not removed, BIN": Em10iiScenario(good, tst, remove_result=-1),
        "TST not removed, no BIN": Em10iiScenario(None, tst, remove_result=-1),
        "TST still there after the remove, BIN": Em10iiScenario(
            good, tst, exists_after=1
        ),
        "TST and bad BIN": Em10iiScenario(_with_header(good, magic=0), tst),
        "bad TST and bad BIN": Em10iiScenario(_with_header(good, magic=0), bad_tst),
        "TST, no card": Em10iiScenario(good, tst, mount_after=10000),
        "TST on a wake boot": Em10iiScenario(good, tst, gate=0),
        "TST, relay with a cable": Em10iiScenario(
            good, tst, **relay, usb_key=USB_CABLE
        ),
        "TST, switch boot with a cable": Em10iiScenario(good, tst, usb_key=USB_CABLE),
        "TST only, switch boot with a cable": Em10iiScenario(
            None, tst, usb_key=USB_CABLE
        ),
        "TST, switch boot, unknown cable state": Em10iiScenario(
            good, tst, usb_key=USB_UNKNOWN
        ),
    }


def test_em10ii_file_cases_agree_with_the_payload_checker():
    for name, (file, ok) in file_cases(bytes(4), EM10II).items():
        assert _accepted(file) == ok, name


@dataclass(frozen=True)
class Em10iiExpected:
    created: int = 0
    stub_sleeps: int = 0
    levels: int = 0
    task_sleeps: int = 0
    status_calls: int = 0
    keys: int = 0
    paths: tuple[str, ...] = ()
    """Files rpc_fopen is asked for, in order."""
    opened: int = 0
    removes: int = 0
    exists: int = 0
    ran: bytes | None = None


def em10ii_expected(s: Em10iiScenario) -> Em10iiExpected:
    """The loader's specification as a decision table."""
    switch_on = s.levels & 1 == 0
    battery = s.batsrc == 0x100 or s.batlvl >= 50
    relay = s.gate != 1 and s.factor == FACTOR_RELAY
    levels = int(relay)
    if s.gate != 1 and not (relay and switch_on and battery):
        return Em10iiExpected(levels=levels)
    if s.loaded0 & SEC45 == SEC45:
        stub_sleeps = 0
    elif s.sec_after < SECTRIES and s.loaded1 & SEC45 == SEC45:
        stub_sleeps = s.sec_after
    else:
        return Em10iiExpected(stub_sleeps=SECTRIES, levels=levels)
    boot = Em10iiExpected(1, stub_sleeps, levels)
    if s.mount_after >= TRIES:
        return replace(boot, task_sleeps=TRIES, status_calls=TRIES)
    boot = replace(
        boot, task_sleeps=s.mount_after, status_calls=s.mount_after + 1, keys=1
    )
    cable = s.usb_key != USB_NONE
    if cable and relay:
        return boot
    paths, opened, removes, exists = ([] if cable else ["TST"]), 0, 0, 0
    if s.tst is not None and not cable:
        opened, removes = 1, 1
        if s.remove_result == 0:
            exists = 1
            if s.exists_after == 0 and _accepted(s.tst):
                return replace(
                    boot, paths=("TST",), opened=1, removes=1, exists=1, ran=s.tst
                )
    paths.append("BIN")
    opened += s.bin is not None
    ran = s.bin if _accepted(s.bin) else None
    return replace(
        boot, paths=tuple(paths), opened=opened, removes=removes, exists=exists, ran=ran
    )


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


def _em10ii_scenario_source(scenario: Em10iiScenario, tst: Path, bin_: Path) -> str:
    expected = em10ii_expected(scenario)
    values = {
        "gate": scenario.gate,
        "factor": scenario.factor,
        "levels": scenario.levels,
        "batsrc": scenario.batsrc,
        "batlvl": scenario.batlvl,
        "usb_key": scenario.usb_key,
        "loaded0": scenario.loaded0,
        "loaded1": scenario.loaded1,
        "sec_after": scenario.sec_after,
        "mount_after": scenario.mount_after,
        "busy_status": scenario.busy_status,
        "remove_result": scenario.remove_result & 0xFFFFFFFF,
        "exists_after": scenario.exists_after,
        "tst_present": int(scenario.tst is not None),
        "bin_present": int(scenario.bin is not None),
        "tst_len": len(scenario.tst or b""),
        "bin_len": len(scenario.bin or b""),
        "expect_created": expected.created,
        "expect_stub_sleeps": expected.stub_sleeps,
        "expect_task_sleeps": expected.task_sleeps,
        "expect_status_calls": expected.status_calls,
        "expect_levels": expected.levels,
        "expect_keys": expected.keys,
        "expect_fopens": len(expected.paths),
        "expect_paths": sum(1 << i for i, p in enumerate(expected.paths) if p == "BIN"),
        "expect_opened": expected.opened,
        "expect_removes": expected.removes,
        "expect_exists": expected.exists,
        "expect_called": int(expected.ran is not None),
        "expect_size": payload.parse_header(expected.ran).size if expected.ran else 0,
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
    lines += [
        "\t.global _scn_tst",
        f'_scn_tst:\t.incbin "{tst}"',
        "\t.balign 4",
        "\t.global _scn_bin",
        f'_scn_bin:\t.incbin "{bin_}"',
        "",
    ]
    return "\n".join(lines)


@needs_toolchain
@pytest.mark.parametrize("name", list(em10ii_scenarios(bytes(4))))
def test_em10ii_loader_in_simulator(name, em10ii_sim_objects, tmp_path):
    payload_bin, objects = em10ii_sim_objects
    scenario = em10ii_scenarios(payload_bin.read_bytes())[name]

    tst = tmp_path / "FENYKL.TST"
    tst.write_bytes(scenario.tst or b"")
    bin_ = tmp_path / "FENYKL.BIN"
    bin_.write_bytes(scenario.bin or b"")
    source = tmp_path / "scenario.S"
    source.write_text(_em10ii_scenario_source(scenario, tst, bin_))
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
