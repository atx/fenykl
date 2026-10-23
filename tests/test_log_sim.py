"""Logging library (sdk/lib/log.c) in the GDB simulator (tests/sim/log/): ring wrap and
overflow, the CPU1 ring refilled while the flusher drains it, record formatting and continuation
records, [lost N] from sequence gaps, the 30-bit timer wrap and gaps longer than it, the size cap
and the failed-append error, against mocks of the file RPC, sprintf and the PSW calls.
test_log.c checks the counters (exit status); this file checks the text it dumps after each boot."""

import re

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

LOG = SIM / "log"
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
]
# DRAM and its uncached alias, the timer, the boot cause byte, the console capture and its count, the
# power-on factor word, fat_mounted (the card gate).
MEMORY = [
    DRAM_ALIAS,
    region("0x98900000,0x100"),
    region("0x60440000,0x10000"),
    region("0x8ff00000,0x100"),
    region("0xa07c0000,0x8000"),
    POWERON,
    region("0x6059c000,0x1000"),
]
STAMP = r"\[ *(\d+)\.(\d{6})\]"


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> tuple[list[str], str]:
    """The file dumps (one per simulated boot) and the FENYKL.ERR text."""
    result = run_in_simulator(
        tmp_path_factory.mktemp("logsim"),
        "logsim",
        [
            LOG / "test_log.c",
            LOG / "cpu1.c",
            LOG / "fw.S",
            SIM / "hooks" / "sim_start.S",
        ],
        MEMORY,
        mocked=MOCKED,
        timeout=120,
        cpu1_sources=[LOG / "cpu1.c"],
    )
    assert_exit(result, 0, "tests/sim/log/test_log.c")
    out = result.stdout.decode("latin-1")
    *dumps, err = out.split("\n#### ")[1:]
    assert err.startswith("err\n")
    return [d.removeprefix("dump\n") for d in dumps], err


def lines(dump: str) -> list[str]:
    return dump.split("\r\n")


def find(dump: str, pattern: str) -> list[str]:
    return [line for line in lines(dump) if re.search(pattern, line)]


def stamp(line: str) -> int:
    s, us = re.match(STAMP, line).groups()
    return int(s) * 1_000_000 + int(us)


def test_records_are_formatted_and_merged_by_stamp(run):
    first = run[0][0]
    assert find(
        first,
        r"I log    : fenykl log boot: build 00000000, boot cause 00, "
        r"timer 004c4b40, file 0 bytes before this boot$",
    )
    body = [line for line in lines(first) if re.search(r" test   : ", line)][:5]
    assert body == [
        "[     0.000100] I test   : fmt 7 ab str z",
        "[     0.000150] i test   : c1 1",
        "[     0.000200] I test   : text-0123456789abcdefghijklmnopqrstuvwxyz-end",
        "[     0.000250] e test   : cpu1-text-0123456789-end!",
        "[     0.000300] W test   : after 2",
    ]
    assert not find(first, "filtered")


def test_long_text_is_cut_and_marked(run):
    (line,) = find(run[0][0], r"\[truncated\]$")
    text = line.split("test   : ")[1].removesuffix(" [truncated]")
    assert text == "".join(chr(ord("a") + i % 26) for i in range(255))


def test_bad_pointers_and_formats_do_not_reach_sprintf(run):
    first = run[0][0]
    assert find(first, r"I test   : <12345678>\|$")
    assert len(find(first, r"I test   : <bad format ")) == 2
    assert len(find(first, r"I \?      : <bad record ")) == 1


def test_timer_wrap_keeps_stamps_increasing(run):
    (pre,) = find(run[0][0], "pre-wrap")
    (post,) = find(run[0][0], "post-wrap")
    assert stamp(post) - stamp(pre) == 0x200


def sequence(dump: str, word: str) -> list[int]:
    return [int(line.rsplit(" ", 1)[1]) for line in find(dump, rf": {word} \d+$")]


def test_ring0_keeps_the_newest_and_reports_the_gap(run):
    first = run[0][0]
    assert find(first, r"W log    : \[lost 88 records, ring 0\]")
    assert sequence(first, "burst0") == list(range(88, 600))


def test_ring1_drops_the_newest_and_reports_the_gap(run):
    first = run[0][0]
    assert find(first, r"w log    : \[lost 72 records, ring 1\]")
    assert sequence(first, "burst1") == list(range(128))


def test_cpu1_refill_during_the_drain_loses_nothing(run):
    first = run[0][0]
    assert sequence(first, "spsc") == list(range(160))
    spsc = [i for i, line in enumerate(lines(first)) if ": spsc " in line]
    assert not any("[lost" in line for line in lines(first)[spsc[0] : spsc[-1]])


def test_eviction_during_the_drain_is_counted(run):
    first = lines(run[0][0])
    assert sequence(run[0][0], "pre") == list(range(32))
    assert sequence(run[0][0], "during") == list(range(88, 600))
    gap = first.index(find(run[0][0], ": pre 31$")[0]) + 1
    assert re.search(r"\[lost 156 records, ring 0\]$", first[gap])


def test_console_tee_splits_lines(run):
    tee = [line.split("fwcon  : ")[1] for line in find(run[0][0], "fwcon  : ")]
    assert tee == [
        "abc",
        "def",
        "xyz",
        "q" * 255,
        "q" * 255,
        "q" * 255,
        "qq",
        "[tee overflow: console characters lost]",
        "[tee: console capture is off (uart_state[1] >= 2): UART1 camera?]",
    ]


def test_size_cap_deletes_and_starts_fresh(run):
    fourth = lines(run[0][3])
    assert fourth[0] == "" and fourth[1].startswith("=====")
    assert re.search(r"file 0 bytes before this boot$", fourth[2])
    assert re.search(
        r"C:\\FENYKL.LOG was 4194305 bytes \(cap 4194304\): deleted, started fresh$",
        fourth[3],
    )


def test_failed_append_is_reported_not_worked_around(run):
    dumps, err = run
    assert dumps[4].startswith(dumps[3])
    assert find(dumps[4], "after a failed init")
    assert 'rpc_fopen("C:\\FENYKL.LOG", "a") failed 3 times' in err
    text = err.removeprefix("err\n")
    assert text[126:128] == "\r\n", "the padded line must end within FLOG_ERR_SIZE"


def this_boot(dump: str) -> list[str]:
    """The lines of the last boot: from its boot header on."""
    out = lines(dump)
    start = max(i for i, line in enumerate(out) if line.startswith("====="))
    return [line for line in out[start + 1 :] if line]


def test_init_before_the_card_mount_writes_header_and_records_once_it_opens(run):
    sixth = this_boot(run[0][5])
    assert sixth[0].split("] ", 1)[1].startswith("I log    : fenykl log boot: ")
    assert [line.split("] ", 1)[1] for line in sixth[1:]] == [
        "W log    : card gate closed (card not mounted): log on hold",
        "I test   : gate 0",
        "I test   : gate 1",
        "I log    : card gate open again after 5000 ms, 3 flushes skipped",
    ]


def test_a_cable_keeps_the_gate_closed_after_it_is_pulled(run):
    """A reopened gate after a USB session wrote the camera's stale FAT back over the PC's changes:
    nothing after the cable reaches the file."""
    boot = this_boot(run[0][6])
    seventh = "\r\n".join(boot)
    assert boot[0].split("] ", 1)[1].startswith("I log    : fenykl log boot: ")
    assert sequence(seventh, "gate") == list(range(100))
    assert not find(seventh, "card gate|lost")


def test_stamps_keep_gaps_longer_than_one_timer_wrap(run):
    """timer_now wraps every 2^30 us (1073.74 s): a ring without records for longer than that, with
    the card gate open and closed, and a record queued for longer than that keep their stamps."""
    boot = this_boot(run[0][7])

    def at(pattern: str) -> int:
        (line,) = [line for line in boot if re.search(pattern, line)]
        return stamp(line)

    idle = [at(rf": idle {i}$") for i in range(3)]
    closed = at(r"W log    : card gate closed \(card not mounted\)")
    reopened = at(
        r"I log    : card gate open again after 1600000 ms, 16 flushes skipped$"
    )
    assert idle[1] - idle[0] == 1_200_000_000
    assert closed - idle[1] == 1_300_000_000
    assert idle[2] - closed == 500_000_000
    assert reopened - idle[2] == 1_100_000_000
