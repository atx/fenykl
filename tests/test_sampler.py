"""The memory sampler (sdk/tp/sampler/, py/fenykl/sampler.py): the test program's ring, overrun,
drop, change and refusal logic in the GDB simulator, run with a control block the host code
wrote; the config refusals; the host loop against a fake camera."""

import os
import signal
import struct
import tomllib

import pytest
from conftest import (
    SIM,
    FakeCamera,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

from fenykl import dev, sampler, sdk, tp

EM10II = sdk.target("em10ii")
EM10II_DRAM = ("--memory-alias", "0x60000000,0x2000000,0xa0000000")
T_DATA = 0x6EB00000
T_CFG = 0x6EB01000
# The layout of sdk/tp/sampler/sampler.h (pinned there by _Static_assert) for a sampler in slot 0.
CTL = 0x60F92800  # slot 1, cached
CTL_U = 0xA0F92800  # its uncached alias
JOB_BUFFER = 0x6100A800
STOP, TAIL, STATE, HEAD, DROPS, LOOP_US = 0x18, 0x1C, 0x20, 0x24, 0x28, 0x30
RING = 0x280


def _config(text: str) -> sampler.Config:
    return sampler.parse_config(tomllib.loads(text))


@needs_toolchain
def test_sampler_runs_in_the_simulator(tmp_path):
    image = tp.build([sampler.SOURCE], tmp_path / "tp", EM10II, 2)
    cfg = _config(
        f"""
        mode = "periodic"
        period_us = 10
        [[word]]
        name = "a"
        addr = 0x{T_DATA:x}
        [[word]]
        name = "b"
        addr = 0x{T_DATA + 6:x}
        width = 16
        [[word]]
        name = "c"
        addr = 0x{T_DATA + 9:x}
        width = 8
        [[word]]
        name = "p"
        ptr = 0x{T_DATA + 0x10:x}
        offset = 4
        [[word]]
        name = "dsp"
        ptr = 0x{T_DATA + 0x14:x}
        width = 16
        """
    )
    block = tmp_path / "ctl.bin"
    block.write_bytes(sampler.ctl_bytes(cfg, 1, 150))
    place = tmp_path / "place.S"
    place.write_text(
        f'\t.section .tpimg,"a"\n\t.incbin "{image}"\n'
        f'\t.section .smpcfg,"a"\n\t.incbin "{block}"\n'
    )
    result = run_in_simulator(
        tmp_path,
        "smpsim",
        [
            SIM / "devmon" / "test_sampler.c",
            sdk.SDK / "lib" / "devmon_proto.c",
            SIM / "hooks_em10ii" / "sim_start.S",
            place,
        ],
        [EM10II_DRAM, region("0x6eb00000,0x10000")],
        ldflags=[
            f"-Wl,--section-start=.tpimg=0x{EM10II.tp.slot(2):x}",
            f"-Wl,--section-start=.smpcfg=0x{T_CFG:x}",
        ],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/devmon/test_sampler.c")


@pytest.mark.parametrize(
    ("words", "message"),
    [
        ('name = "x"\naddr = 0x9D000100', "DSP local memory"),
        ('name = "x"\naddr = 0xDD000000\nwidth = 8', "DSP local memory"),
        ('name = "x"\nptr = 0x9DFFFFFC', "DSP local memory"),
        ('name = "x"\naddr = 0x60100002', "not aligned to 32"),
        ('name = "x"\naddr = 0x60100001\nwidth = 16', "not aligned to 16"),
        ('name = "x"\nptr = 0x60100002\nwidth = 8', "not aligned to 32"),
        ('name = "x"\naddr = 0x60100000\nwidth = 12', "width 12"),
        ('name = "x"\naddr = 0x60100000\nptr = 0x60100000', "addr or ptr"),
        ('name = "x"\naddr = 0x60100000\noffset = 4', "offset needs ptr"),
        ('name = "x"\naddr = 0x60100000\ntrigger = true', "need mode change"),
        ('name = "x"\naddr = 0x60100000\nmask = 1', "mask needs trigger"),
        ('name = "x,y"\naddr = 0x60100000', "name"),
        ('name = "lost"\naddr = 0x60100000', "unique"),
        ('name = "x"\naddr = 0x60100000\nadr = 1', "unknown keys adr"),
    ],
)
def test_config_refusals(words, message):
    with pytest.raises(sampler.ConfigError, match=message):
        _config(f"period_us = 100\n[[word]]\n{words}\n")


def test_change_mode_needs_a_trigger_and_masks_fit_the_width():
    with pytest.raises(sampler.ConfigError, match="at least one word with trigger"):
        _config(
            'mode = "change"\nperiod_us = 100\n[[word]]\nname = "x"\naddr = 0x60100000'
        )
    with pytest.raises(sampler.ConfigError, match="mask must be"):
        _config(
            'mode = "change"\nperiod_us = 100\n[[word]]\nname = "x"\naddr = 0x60100000\n'
            "width = 8\ntrigger = true\nmask = 0x100"
        )
    cfg = _config(
        'mode = "change"\nperiod_us = 100\n[[word]]\nname = "x"\nptr = 0x8FF00194\n'
        "offset = 0x44\nwidth = 8\ntrigger = true"
    )
    assert cfg.words[0] == sampler.Word("x", 0x8FF00194, 8, True, 0x44, True, 0xFF)
    cfg = _config(
        'mode = "change"\nperiod_us = 100\ntrigger = { addr = 0x60100000, mask = 0xF0 }\n'
        '[[word]]\nname = "x"\naddr = 0x60100004'
    )
    assert cfg.words[1] == sampler.Word(
        "trigger", 0x60100000, 32, trigger=True, mask=0xF0
    )


def test_the_ring_ends_before_the_job_buffer():
    # 32 words: records of 4 + 32 u32, (0x6100A800 - 0x60F92800 - 0x280) // 144 of them
    assert sampler.ring_layout(EM10II, 0, 32) == (CTL, 3408)
    assert CTL + RING + 3408 * 144 <= JOB_BUFFER < CTL + RING + 3409 * 144
    with pytest.raises(sampler.ConfigError, match="no slot after it"):
        sampler.ring_layout(EM10II, 15, 1)


def test_rows_mark_bad_pointers():
    words = (sampler.Word("a", 0, 8), sampler.Word("p", 0, 16, ptr=True))
    data = struct.pack("<12I", 0, 5, 0, 0, 0x7, 0x12, 1, 25, 2, 0b10, 0xFF, 0x9D000000)
    rows = [sampler.csv_row(r, words) for r in sampler.decode(data, 2)]
    assert rows == [[5, 0, "0x07", "0x0012"], [25, 2, "0xff", "bad:0x9d000000"]]


class FakeSampler:
    """The camera side of a run of a one-word sampler in slot 0: memory from slot 1 on, and a
    program that, while running, adds up to `burst` records per status READ (dropping when the
    host has not taken them) until `total` passes or the stop word. Record i holds i + 1."""

    def __init__(self, total=40, burst=7, cap=5, **how):
        self.mem = bytearray(0x10000)
        self.total, self.burst, self.cap = total, burst, cap
        self.how = how
        self.head = 0
        self.passes = 0
        self.reads = 0
        self.writes: list[int] = []
        self.keys: list[int] = []

    def word(self, off: int, v: int | None = None) -> int:
        if v is not None:
            struct.pack_into("<i" if v < 0 else "<I", self.mem, off, v)
        return struct.unpack_from("<I", self.mem, off)[0]

    def _program(self) -> None:
        if self.word(STOP) and not self.how.get("ignore_stop"):
            self.word(STATE, 2)
            return
        for _ in range(self.burst):
            if self.passes == self.total:
                break
            self.passes += 1
            if self.head - self.word(TAIL) >= self.cap:
                self.word(DROPS, self.word(DROPS) + 1)
                continue
            rec = RING + (self.head % self.cap) * 20
            struct.pack_into(
                "<5I", self.mem, rec, self.head, 10 * self.passes, 0, 0, self.passes
            )
            self.head += 1
        self.word(HEAD, self.head)
        if self.passes == self.total:
            self.word(STATE, 2)

    def __call__(self, op: int, p: bytes) -> tuple[int, bytes]:
        if op == dev.OP_WRITE:
            (addr,) = struct.unpack_from("<I", p)
            self.writes.append(addr)
            off = (addr | 0x80000000) - CTL_U
            self.mem[off : off + len(p) - 4] = p[4:]
            return 0, b""
        if op == dev.OP_TPRUN:
            if "refuse" in self.how:
                self.word(STATE, self.how["refuse"])
                self.word(LOOP_US, 40)
                return 0, struct.pack("<iI", self.how["refuse"], 5)
            self.word(STATE, 1)
            return dev.TIMEOUT, b""
        if op == dev.OP_READ:
            addr, n = struct.unpack_from("<2I", p)
            off = addr - CTL_U
            if off == STOP and self.word(STATE) == 1:
                self.reads += 1
                if self.reads == self.how.get("sigint_at"):
                    os.kill(os.getpid(), signal.SIGINT)
                self._program()
                if self.reads == self.how.get("torn_at"):
                    # a head read while it changes: one record more than is written
                    status = bytearray(self.mem[off : off + n])
                    struct.pack_into("<I", status, HEAD - STOP, self.head + 1)
                    return 0, bytes(status)
            return 0, bytes(self.mem[off : off + n])
        if op == dev.OP_KEY:
            self.keys.append(struct.unpack_from("<I", p)[0])
            return self.how.get("key_status", 0), b""
        return 0, b""


@pytest.fixture
def run_fake(pair, tmp_path, monkeypatch):
    """sample() against a FakeSampler(**how) with its 5-record ring; the CSV rows."""
    client, cam = pair
    image = tp.build([sampler.SOURCE], tmp_path / "tp", EM10II, 0).read_bytes()
    monkeypatch.setattr(sampler, "ring_layout", lambda target, slot, n: (CTL, 5))
    cfg = _config('period_us = 10\n[[word]]\nname = "n"\naddr = 0x60100000')
    out = tmp_path / "out.csv"

    def run(fake: FakeSampler, duration: float = 1.0, keys: str = ""):
        FakeCamera(cam, fake)
        r = sampler.sample(
            client, cfg, image, EM10II, duration, out, keys, say=lambda s: None
        )
        lines = out.read_text().splitlines()
        assert lines[0] == "timestamp_us,lost,n"
        return r, [line.split(",") for line in lines[1:]]

    return run


@needs_toolchain
def test_the_host_loop_follows_the_ring_and_writes_the_csv(run_fake):
    fake = FakeSampler()
    r, rows = run_fake(fake, keys="HALF")
    assert len(rows) == r.records == r.status.head == fake.head
    assert r.status.drops == 40 - r.records > 0
    numbers = [int(row[2], 16) for row in rows]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
    stamps = [int(row[0]) for row in rows]
    assert stamps == [10 * n for n in numbers]
    # the block through the cached alias (CPU0 writes and writes back), the tail uncached
    assert fake.writes[0] == CTL
    assert set(fake.writes[1:]) == {CTL_U + TAIL}
    assert fake.keys == [dev.KEYS.index("HALF")]


@needs_toolchain
def test_a_torn_head_read_takes_no_unwritten_record(run_fake):
    fake = FakeSampler(burst=2, torn_at=3)
    r, rows = run_fake(fake)
    assert [int(row[2], 16) for row in rows] == list(range(1, 41))
    assert r.status.drops == 0


@needs_toolchain
def test_ctrl_c_sets_the_stop_word(run_fake):
    fake = FakeSampler(total=10**6, burst=1, cap=5, sigint_at=3)
    r, rows = run_fake(fake, duration=60)
    assert CTL_U + STOP in fake.writes and len(rows) == r.records > 0


@needs_toolchain
def test_a_sampler_that_does_not_stop_is_reported(run_fake, monkeypatch):
    monkeypatch.setattr(sampler, "GRACE_S", 0.1)
    fake = FakeSampler(total=10**6, burst=0, ignore_stop=True)
    with pytest.raises(dev.DevmonError, match="did not stop"):
        run_fake(fake, duration=0.05)
    assert CTL_U + STOP in fake.writes


@needs_toolchain
def test_a_refused_config_is_reported_and_not_stopped(run_fake):
    fake = FakeSampler(refuse=-5)
    with pytest.raises(
        dev.DevmonError, match=r"period below .* \(one pass takes 40 us\)"
    ):
        run_fake(fake)
    assert CTL_U + STOP not in fake.writes


@needs_toolchain
def test_an_error_during_the_run_sets_the_stop_word(run_fake):
    fake = FakeSampler(total=10**6, burst=1, key_status=-8)
    with pytest.raises(dev.DevmonStatus, match="card I/O"):
        run_fake(fake, duration=60, keys="200 OK")
    assert fake.writes[-1] == CTL_U + STOP
