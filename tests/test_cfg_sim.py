"""Configuration library (sdk/lib/cfg.c) in the GDB simulator (tests/sim/cfg/): slot selection
by CRC and seq (torn and short files fall back to the older slot, seq wraps), the line parser
(good, invalid, out-of-range, duplicate, unknown and malformed lines, enum names), lines carried
verbatim through saves, the one-shot import through the same parser, the remove-before-write rule,
the carried-lines overflow and the card gate. test_cfg.c checks return codes and counters (exit
status); this file crafts the input files (CRC from zlib) and checks the files the library wrote."""

import zlib
from pathlib import Path

import pytest
from conftest import (
    DRAM_ALIAS,
    POWERON,
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

pytestmark = needs_toolchain

CFG = SIM / "cfg"
MOCKED = [
    "psw_get",
    "irq_disable_all",
    "irq_mask_restore",
    "sprintf",
    "rpc_fopen",
    "rpc_fclose",
    "rpc_fread",
    "rpc_fwrite",
    "rpc_fremove",
    "malloc",
    "free",
    "OlyTaskSleep",
    "ui_key_get_state",
    "dcache_purge_all_irqsafe",
]
# DRAM and its uncached alias, the timer, the power-on factor word, fat_mounted (the card gate).
MEMORY = [
    DRAM_ALIAS,
    region("0x98900000,0x100"),
    POWERON,
    region("0x6059c000,0x1000"),
]
SLOTS = ("C:\\FENYKL.CF0", "C:\\FENYKL.CF1", "C:\\FENYKL.CFG")


def slot_file(seq: int, lines: list[str], version: int = 1) -> bytes:
    text = f"fenykl-cfg {version} seq={seq}\n" + "".join(f"{line}\n" for line in lines)
    return text.encode() + f"crc={zlib.crc32(text.encode()):08X}\n".encode()


PARSER = [
    "t.bool=on",
    "t.int=7",
    "t.enum=ccc",
    "t.big=abc",
    "future.opt=7",
    "garbage",
    "=x",
    "t.int=200",
    "t.enum=a",
]
CRAFTED = {
    "parser0": slot_file(100, PARSER),
    "parser1": slot_file(200, ["t.bool=off"], version=2),
    "wrap0": slot_file(0xFFFFFFFF, ["t.int=30"]),
    "wrap1": slot_file(0, ["t.int=25"]),
    "import": b"t.enum=a\r\nfuture.opt=9\r\nnew.thing=1\r\n\r\nt.int=abc\r\n",
    "overflow": b"".join(b"x.unknown%03d=%060d\n" % (i, i) for i in range(16)),
}


def crafted_c(path: Path) -> Path:
    out = []
    for name, data in CRAFTED.items():
        body = ", ".join(str(b) for b in data)
        out.append(f"const char sim_{name}[] = {{ {body} }};")
        out.append(f"const unsigned int sim_{name}_len = {len(data)};")
    path.write_text("\n".join(out) + "\n")
    return path


@pytest.fixture(scope="module")
def dumps(tmp_path_factory) -> dict[str, dict[str, str | None]]:
    """The files after each scenario: {scenario: {path: text or None}}."""
    tmp = tmp_path_factory.mktemp("cfgsim")
    result = run_in_simulator(
        tmp,
        "cfgsim",
        [
            CFG / "test_cfg.c",
            crafted_c(tmp / "crafted.c"),
            SIM / "log" / "fw.S",
            CFG / "fw.S",
            SIM / "hooks" / "sim_start.S",
        ],
        MEMORY,
        mocked=MOCKED,
        timeout=120,
    )
    assert_exit(result, 0, "tests/sim/cfg/test_cfg.c")
    out = result.stdout.decode("latin-1")
    scenarios = {}
    for block in out.split("\n#### ")[1:]:
        tag, *files = block.split("\n-- ")
        scenarios[tag] = {}
        for f in files:
            path, text = f.split("\n", 1)
            scenarios[tag][path] = None if text.rstrip("\n") == "(none)" else text
    return scenarios


def body(text: str) -> tuple[int, list[str]]:
    """Checks the header and the CRC of a slot file; returns its seq and option lines."""
    *lines, crc = text.split("\n")[:-1]
    assert crc == f"crc={zlib.crc32(('\n'.join(lines) + '\n').encode()):08X}"
    version, seq = lines[0].split(" seq=")
    assert version == "fenykl-cfg 1"
    return int(seq), lines[1:]


def test_first_save_writes_all_options_into_cf0(dumps):
    files = dumps["fresh"]
    assert body(files[SLOTS[0]]) == (
        1,
        ["t.bool=off", "t.int=15", "t.enum=bb", "t.big=1000"],
    )
    assert files[SLOTS[1]] is None


def test_saves_alternate_between_the_slots(dumps):
    files = dumps["alternate"]
    assert body(files[SLOTS[0]])[0] == 1
    assert body(files[SLOTS[1]]) == (
        2,
        ["t.bool=on", "t.int=15", "t.enum=bb", "t.big=1000"],
    )


def test_a_failed_remove_or_write_leaves_the_current_slot_alone(dumps):
    files = dumps["failures"]
    assert body(files[SLOTS[1]])[0] == 2
    assert body(files[SLOTS[0]]) == (
        3,
        ["t.bool=on", "t.int=20", "t.enum=bb", "t.big=1000"],
    )


def test_unknown_and_invalid_lines_are_carried_verbatim(dumps):
    seq, lines = body(dumps["parser"][SLOTS[1]])
    assert seq == 101
    assert lines == [
        "t.bool=off",
        "t.enum=a",
        "t.big=abc",
        "future.opt=7",
        "t.int=200",
    ]


def test_setting_an_option_replaces_its_carried_line(dumps):
    seq, lines = body(dumps["parser-set"][SLOTS[0]])
    assert seq == 102
    assert lines == ["t.bool=off", "t.int=10", "t.enum=a", "t.big=abc", "future.opt=7"]


def test_import_is_applied_carried_saved_and_deleted(dumps):
    files = dumps["import"]
    seq, lines = body(files[SLOTS[1]])
    assert seq == 103
    assert lines == [
        "t.bool=off",
        "t.enum=a",
        "t.big=abc",
        "future.opt=9",
        "new.thing=1",
        "t.int=abc",
    ]
    assert files[SLOTS[2]] is None


def test_late_load_after_a_closed_gate_keeps_values_set_meanwhile(dumps):
    seq, lines = body(dumps["gate"][SLOTS[0]])
    assert seq == 104
    assert lines == [
        "t.bool=off",
        "t.enum=a",
        "t.big=7",
        "future.opt=9",
        "new.thing=1",
        "t.int=abc",
    ]
