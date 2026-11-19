"""The dev monitor (sdk/lib/devmon_*.c, project devmon1): the protocol helpers and an example test
program in the GDB simulator, the rule that the code other cores run reads no cached .data/.bss,
the opt-in link, and the `fenykl dev` client against a fake camera on a local socket pair."""

import argparse
import re
import socket
import struct
import subprocess
import threading
import zlib

import pytest
from conftest import (
    SIM,
    FakeCamera,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

from fenykl import dev, plugin, screen, sdk, tp
from fenykl.plugin import BuildError

EM10II = sdk.target("em10ii")
PROJECT = sdk.SDK / "projects" / "devmon1"
LIB = sdk.SDK / "lib"
EM10II_DRAM = ("--memory-alias", "0x60000000,0x2000000,0xa0000000")


@needs_toolchain
def test_devmon_protocol_runs_in_the_simulator(tmp_path):
    result = run_in_simulator(
        tmp_path,
        "dmsim",
        [
            SIM / "devmon" / "test_proto.c",
            SIM / "devmon" / "sim_write.S",
            LIB / "devmon_proto.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        [EM10II_DRAM, region("0x6eb00000,0x10000")],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/devmon/test_proto.c")


def _symbol_sections(objdump: str, path) -> dict[str, str]:
    out = subprocess.run(
        [objdump, "-t", str(path)], check=True, capture_output=True, text=True
    )
    sections = {}
    for line in out.stdout.splitlines():
        m = re.match(r"^[0-9a-f]{8} .{7} (\S+)\s+[0-9a-f]{8} (\S+)$", line)
        if m:
            sections[m.group(2)] = m.group(1)
    return sections


@needs_toolchain
def test_devmon_cpu1_code_reads_no_cached_data(tmp_path):
    """devmon_cpu1_em10ii.c and devmon_proto.c run on CPU1: every symbol they reference must live in .text,
    .rodata or .shared, never in .data or .bss (written by CPU0 through its cache)."""
    plugin.build(PROJECT, tmp_path, EM10II)
    objdump = EM10II.tool("objdump")
    final = _symbol_sections(objdump, tmp_path / "plugin.elf")
    for name in ("devmon_cpu1_em10ii", "devmon_proto"):
        obj = tmp_path / "obj" / f"lib_{name}.c.o"
        local = _symbol_sections(objdump, obj)
        relocs = subprocess.run(
            [objdump, "-r", str(obj)], check=True, capture_output=True, text=True
        ).stdout
        targets = {
            m.group(1)
            for m in re.finditer(r"^[0-9a-f]{8} \S+\s+([^\s+]+)", relocs, re.MULTILINE)
        }
        assert targets
        for sym in targets:
            section = final.get(sym) or local.get(sym, sym)
            assert not re.match(r"\.(s?data|s?bss)", section), (
                f"{name}: {sym} in {section}"
            )


def _frame(disasm: str, func: str) -> int:
    """Bytes a function's entry takes off the stack: its movm registers and its add -N,sp."""
    body = disasm.split(f"<{func}>:\n", 1)[1].split("\n\n", 1)[0]
    regs = re.search(r"movm\s+\[([^\]]*)\],\(sp\)", body)
    add = re.search(r"add\s+-(\d+),sp", body)
    assert add, f"{func}: no add -N,sp"
    return (len(regs.group(1).split(",")) * 4 if regs else 0) + int(add.group(1))


@needs_toolchain
def test_the_server_stack_fill_starts_inside_the_stack(tmp_path):
    """fdm_stack_fill puts the stack top FDM_STACK_ABOVE (128) bytes above the task entry's
    marker: the kernel enters with SP = top - 4, so the entry trampoline and the task function
    may take at most 124 bytes, or the fill would start below the 0x800-byte stack."""
    plugin.build(PROJECT, tmp_path, EM10II)
    disasm = subprocess.run(
        [EM10II.tool("objdump"), "-d", str(tmp_path / "plugin.elf")],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "<_fdm_srv>:" not in disasm, (
        "fdm_srv is out of line: its frame is not counted"
    )
    used = _frame(disasm, "_fenykl_cb_kernel_task_fdm_srv") + _frame(
        disasm, "_fenykl_cbwrap_kernel_task_fdm_srv"
    )
    assert 4 + used <= 128, f"{used} bytes above the marker"


@needs_toolchain
def test_devmon_is_linked_only_on_opt_in(tmp_path):
    """A plugin without [capabilities.devmon] gets neither the monitor's code nor its options."""
    plugin.build(sdk.SDK / "projects" / "hello", tmp_path, EM10II)
    syms = _symbol_sections(EM10II.tool("objdump"), tmp_path / "plugin.elf")
    assert not [s for s in syms if s.startswith("_fdm_")]
    assert b"devmon.ap" not in (tmp_path / "FENYKL.BIN").read_bytes()


@needs_toolchain
def test_example_test_program_runs_in_the_simulator(tmp_path):
    image = tp.build(
        [sdk.SDK / "tp" / "examples" / "peek.c"], tmp_path / "tp", EM10II, 2
    )
    h = tp.check(image.read_bytes())
    assert h.base == EM10II.tp.slot(2) and tp.slot_of(EM10II, h) == 2
    place = tmp_path / "tpimg.S"
    place.write_text(f'\t.section .tpimg,"a"\n\t.incbin "{image}"\n')
    result = run_in_simulator(
        tmp_path,
        "tpsim",
        [
            SIM / "devmon" / "test_tp.c",
            LIB / "devmon_proto.c",
            SIM / "hooks_em10ii" / "sim_start.S",
            place,
        ],
        [EM10II_DRAM, region("0x6eb00000,0x10000")],
        ldflags=[f"-Wl,--section-start=.tpimg=0x{h.base:x}"],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/devmon/test_tp.c")


def test_client_matches_replies_and_raises_refusals(pair):
    client, cam = pair
    fake = FakeCamera(
        cam, lambda op, p: (-5, b"") if op == dev.OP_FDEL else (0, b"ok\n")
    )
    assert client.ping() == "ok"
    with pytest.raises(dev.DevmonStatus, match="file name") as e:
        client.call(dev.OP_FDEL, b"\0" * 12)
    assert e.value.status == -5
    assert [op for op, _ in fake.requests] == [dev.OP_PING, dev.OP_FDEL]


def test_client_rejects_a_reply_to_another_request(pair):
    client, cam = pair
    cam.sendall(dev.HEADER.pack(dev.REP_MAGIC, dev.OP_PING, 0, 99, 0))
    with pytest.raises(dev.DevmonError, match="bad reply"):
        client.ping()


def test_client_truncated_reply_raises(pair):
    client, cam = pair
    cam.sendall(dev.HEADER.pack(dev.REP_MAGIC, dev.OP_PING, 0, 1, 10) + b"abc")
    cam.shutdown(socket.SHUT_WR)
    with pytest.raises(dev.DevmonError, match="closed after 3 of 10"):
        client.ping()


def test_read_splits_at_the_reply_limit(pair):
    client, cam = pair

    def handler(op, p):
        addr, n, _, _ = struct.unpack("<4I", p)
        return 0, bytes([addr & 0xFF]) * n

    fake = FakeCamera(cam, handler)
    data = client.read(0x60021000, dev.READ_MAX + 4, piece=1460, pace_ms=2)
    assert data == b"\x00" * dev.READ_MAX + b"\x00" * 4
    assert [struct.unpack("<4I", p) for _, p in fake.requests] == [
        (0x60021000, dev.READ_MAX, 1460, 2),
        (0x60021000 + dev.READ_MAX, 4, 1460, 2),
    ]


def test_put_sends_offsets_total_crc_and_name(pair):
    client, cam = pair
    fake = FakeCamera(cam, lambda op, p: (0, b""))
    data = bytes(range(256)) * 80  # 20480 bytes: two requests
    client.put("FENYKL.DAT", data)
    heads = [struct.unpack_from("<3I12s", p) for _, p in fake.requests]
    name = b"FENYKL.DAT\0\0"
    crc = zlib.crc32(data)
    assert heads == [(0, len(data), crc, name), (dev.IO_DATA, len(data), crc, name)]
    assert b"".join(p[24:] for _, p in fake.requests) == data


def test_put_of_an_empty_file_is_one_request(pair):
    client, cam = pair
    fake = FakeCamera(cam, lambda op, p: (0, b""))
    client.put("FENYKL.TST", b"")
    assert fake.requests == [
        (dev.OP_FPUT, struct.pack("<3I", 0, 0, 0) + b"FENYKL.TST\0\0")
    ]


def test_get_reads_until_a_short_chunk(pair):
    client, cam = pair
    content = b"x" * (dev.IO_DATA + 5)

    def handler(op, p):
        off, n = struct.unpack_from("<2I", p)
        return 0, content[off : off + n]

    fake = FakeCamera(cam, handler)
    assert client.get("FENYKL.LOG") == content
    assert [struct.unpack_from("<2I", p)[0] for _, p in fake.requests] == [
        0,
        dev.IO_DATA,
    ]


@pytest.mark.parametrize(
    "name",
    [
        "fenykl.bin",
        "FENYKL.",
        "FENYKL.BINX",
        "FENYKLX.BIN",
        "DCIM\\FENYKL.BIN",
        "C:\\FENYKL.BIN",
        "FENYKL.B/N",
        "..\\FENYKL.BIN",
        "FENYKL.*",
        "DCOLYMP",
        "FENYKL.B\0N",
    ],
)
def test_client_refuses_names_outside_fenykl(name):
    with pytest.raises(dev.DevmonError, match="FENYKL"):
        dev.name_field(name)


# The wire layout, written out (sdk/lib/devmon_int.h; tests/sim/devmon/test_proto.c pins it there).
PHOTO_FIELD = b"DCIM/100OLYMP/P1180001.JPG" + bytes(6)  # the 32-byte path field
FOLDER_FIELD = b"DCIM/100OLYMP" + bytes(19)
OP_FGET = 0x21
OP_FLIST = 0x23
CHUNK = 0x4000


def entry(name: bytes, size: int, mtime: int) -> bytes:
    """One FLIST entry: the 12-byte name field, u32 size, u32 write time."""
    assert len(name) <= 12
    return name + bytes(12 - len(name)) + struct.pack("<2I", size, mtime)


def test_get_of_a_photo_sends_the_path_field_at_each_offset(pair):
    client, cam = pair
    content = bytes(range(256)) * 130  # 33280 bytes: three requests

    def handler(op, p):
        off, n = struct.unpack_from("<2I", p)
        return 0, content[off : off + n]

    fake = FakeCamera(cam, handler)
    seen = []
    assert client.get("DCIM/100OLYMP/P1180001.JPG", seen.append) == content
    assert fake.requests == [
        (OP_FGET, struct.pack("<2I", off, CHUNK) + PHOTO_FIELD)
        for off in (0, CHUNK, 2 * CHUNK)
    ]
    assert seen == [CHUNK, 2 * CHUNK, len(content)]


def test_a_monitor_without_photo_fetch_is_named(pair):
    client, cam = pair
    FakeCamera(cam, lambda op, p: (-3 if len(p) == 40 else -5, b""))
    with pytest.raises(dev.DevmonError, match="photo fetch"):
        client.get("DCIM/100OLYMP/P1180001.JPG")
    with pytest.raises(dev.DevmonError, match="photo fetch"):
        client.get("SHOT0001.PPM")


@pytest.mark.parametrize(
    "name",
    [
        "DCIM/100OLYMP/E1200000.BIN",
        "DCOLYMP/E1200000.BIN",
        "DCIM\\100OLYMP\\P1180001.JPG",
        "DCIM/100OLYMP\\P1180001.JPG",
        "DCIM/100OLYMP/../FENYKL.BIN",
        "dcim/100olymp/p1180001.jpg",
        "DCIM/100OLYMP",
        "SHOT0001.LV",
    ],
)
def test_client_refuses_names_it_may_not_read(name):
    with pytest.raises(dev.DevmonError, match="reads only"):
        dev.read_field(name)


def test_list_follows_next_until_zero(pair):
    client, cam = pair
    mtime = (46 << 25 | 11 << 21 | 18 << 16) | (7 << 11 | 36 << 5 | 6)
    pages = {
        0: struct.pack("<I", 2)
        + entry(b"P1180001.JPG", 100, mtime)
        + entry(b"P1180001.ORF", 200, mtime),
        2: struct.pack("<I", 0) + entry(b"P1180002.JPG", 300, mtime),
    }
    fake = FakeCamera(cam, lambda op, p: (0, pages[struct.unpack_from("<I", p)[0]]))
    entries = client.list("DCIM/100OLYMP")
    assert [(e.name, e.size) for e in entries] == [
        ("P1180001.JPG", 100),
        ("P1180001.ORF", 200),
        ("P1180002.JPG", 300),
    ]
    assert entries[0].time() == "2026-11-18 07:36:12"
    assert fake.requests == [
        (OP_FLIST, struct.pack("<I", 0) + FOLDER_FIELD),
        (OP_FLIST, struct.pack("<I", 2) + FOLDER_FIELD),
    ]


def test_latest_photo_is_the_last_written_in_the_last_folder(pair):
    client, cam = pair

    def entries(*rows):
        return struct.pack("<I", 0) + b"".join(entry(n, 1, t) for n, t in rows)

    listing = {
        b"DCIM": entries((b"100OLYMP", 5), (b"101OLYMP", 6), (b"102OLYMP", 7)),
        b"DCIM/101OLYMP": entries(
            (b"P1180007.JPG", 9),
            (b"P1180009.JPG", 8),
            (b"P1180008.JPG", 9),
            (b"P1180008.ORF", 9),
        ),
        b"DCIM/102OLYMP": entries(),
        b"DCIM/100OLYMP": entries((b"P1180001.JPG", 99)),
    }
    FakeCamera(cam, lambda op, p: (0, listing[p[4:].rstrip(b"\0")]))
    assert dev.latest_photo(client)[0] == "DCIM/101OLYMP/P1180008.ORF"
    assert dev.latest_photo(client, "JPG")[0] == "DCIM/101OLYMP/P1180008.JPG"
    with pytest.raises(dev.DevmonError, match="no MOV"):
        dev.latest_photo(client, "MOV")


def _get_args(tmp_path, name=None, **kw):
    args = {"name": name, "latest": False, "latest_shot": False, "ext": None}
    return argparse.Namespace(**{**args, **kw, "output": tmp_path / "out"})


def test_get_of_a_photo_checks_the_size_in_its_folder_listing(pair, tmp_path):
    """rpc_fread answers 0 at the end and on an error: a short photo must not count as read."""
    client, cam = pair
    content = b"j" * 3000

    def handler(op, p):
        if op == OP_FLIST:
            return 0, struct.pack("<I", 0) + entry(b"P1180001.JPG", 5000, 0)
        off, n = struct.unpack_from("<2I", p)
        return 0, content[off : off + n]

    FakeCamera(cam, handler)
    with pytest.raises(dev.DevmonError, match="3000 bytes read, the listing says 5000"):
        dev.cmd_get(_get_args(tmp_path, "DCIM/100OLYMP/P1180001.JPG"), client)
    assert not (tmp_path / "out").exists()
    content = b"j" * 5000
    dev.cmd_get(_get_args(tmp_path, "DCIM/100OLYMP/P1180001.JPG"), client)
    assert (tmp_path / "out").read_bytes() == content


def test_get_of_an_unlisted_photo_is_refused(pair, tmp_path):
    client, cam = pair
    FakeCamera(cam, lambda op, p: (0, struct.pack("<I", 0)))
    with pytest.raises(dev.DevmonError, match="not in the listing"):
        dev.cmd_get(_get_args(tmp_path, "DCIM/100OLYMP/P1180001.JPG"), client)


def test_get_of_a_root_file_warns_that_its_size_is_unchecked(pair, tmp_path, capsys):
    client, cam = pair
    FakeCamera(
        cam, lambda op, p: (0, b"P6" if struct.unpack_from("<I", p)[0] == 0 else b"")
    )
    dev.cmd_get(_get_args(tmp_path, "SHOT0001.PPM"), client)
    assert "size unchecked" in capsys.readouterr().err


def test_ext_needs_latest(pair, tmp_path):
    client, _ = pair
    with pytest.raises(dev.DevmonError, match="--ext"):
        dev.cmd_get(_get_args(tmp_path, "SHOT0001.PPM", ext="JPG"), client)


@pytest.mark.parametrize("count", [0, 1, 2, 5, 64, 65, 1000, 10000])
def test_shot_end_finds_the_end_of_the_run(count):
    probes = []

    def exists(n):
        probes.append(n)
        return n < count

    assert dev.shot_end(exists) == count
    assert len(probes) <= 30


def test_exists_maps_no_such_file_to_false(pair):
    client, cam = pair
    FakeCamera(cam, lambda op, p: (-13, b"") if b"SHOT0002" in p else (0, b"P"))
    assert client.exists("SHOT0001.PPM") and not client.exists("SHOT0002.PPM")


def test_exec_uploads_in_chunks_then_runs(pair):
    client, cam = pair

    def handler(op, p):
        if op == dev.OP_TPRUN:
            return 0, struct.pack("<iI", -2, 1234)
        return 0, b""

    fake = FakeCamera(cam, handler)
    image = b"\xaa" * (dev.IO_DATA + 8)
    assert client.exec(image, 5, 0x60021020, 3000) == (-2, 1234)
    assert [(op, p[:8]) for op, p in fake.requests[:2]] == [
        (dev.OP_TPLOAD, struct.pack("<2I", 5, 0)),
        (dev.OP_TPLOAD, struct.pack("<2I", 5, dev.IO_DATA)),
    ]
    assert fake.requests[2] == (dev.OP_TPRUN, struct.pack("<3I", 5, 0x60021020, 3000))


def test_log_returns_the_end_and_the_text(pair):
    client, cam = pair
    fake = FakeCamera(cam, lambda op, p: (0, struct.pack("<I", 700) + b"line\n"))
    assert client.log(695) == (700, b"line\n")
    assert fake.requests == [(dev.OP_LOG, struct.pack("<I", 695))]


def test_power_runs_the_configured_commands(tmp_path, monkeypatch):
    marks = tmp_path / "marks"
    monkeypatch.setenv("FENYKL_DEV_POWER_ON", f"echo on >> {marks}")
    monkeypatch.setenv("FENYKL_DEV_POWER_OFF", f"echo off >> {marks}")
    dev.power("cycle", delay=0)
    assert marks.read_text() == "off\non\n"
    monkeypatch.setenv("FENYKL_DEV_POWER_OFF", "exit 3")
    with pytest.raises(dev.DevmonError, match="exited with 3"):
        dev.power("off")
    monkeypatch.delenv("FENYKL_DEV_POWER_ON")
    with pytest.raises(dev.DevmonError, match="FENYKL_DEV_POWER_ON"):
        dev.power("on")


def test_wait_gives_up_after_its_timeout():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(dev.DevmonError, match="no monitor answer"):
        dev.wait("127.0.0.1", port, timeout=0.3, interval=0.1)


def test_wait_returns_the_ping_text():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def accept():
            conn, _ = server.accept()
            FakeCamera(conn, lambda op, p: (0, b"devmon proto=1\n")).thread.join(2)
            conn.close()

        t = threading.Thread(target=accept, daemon=True)
        t.start()
        assert dev.wait("127.0.0.1", port, timeout=5) == "devmon proto=1"
        t.join(2)


def test_put_and_del_of_the_stable_image_are_refused_locally(pair):
    client, cam = pair
    fake = FakeCamera(cam, lambda op, p: (0, b""))
    with pytest.raises(dev.DevmonError, match="by hand"):
        client.put("FENYKL.BIN", b"x")
    with pytest.raises(dev.DevmonError, match="by hand"):
        client.delete("FENYKL.BIN")
    assert fake.requests == []


def test_a_commit_timeout_reports_an_unknown_state(pair):
    client, cam = pair
    FakeCamera(cam, lambda op, p: (-7, b"") if p[0:4] != b"\0\0\0\0" else (0, b""))
    with pytest.raises(dev.DevmonError, match="state unknown"):
        client.put("FENYKL.DAT", b"y" * (dev.IO_DATA + 1))


def test_no_reply_to_the_commit_reports_an_unknown_state(pair, monkeypatch):
    client, cam = pair
    monkeypatch.setattr(dev, "COMMIT_TIMEOUT", 0.2)
    client.sock.settimeout(0.1)

    def handler(op, p):
        if p[0:4] != b"\0\0\0\0":
            threading.Event().wait(1.0)
        return 0, b""

    FakeCamera(cam, handler)
    with pytest.raises(dev.DevmonError, match="state unknown"):
        client.put("FENYKL.DAT", b"y" * (dev.IO_DATA + 1))
    assert client.sock.gettimeout() == 0.1


def test_a_lost_connection_is_a_devmon_error(pair):
    client, cam = pair
    cam.close()
    with pytest.raises(dev.DevmonError):
        client.ping()


def test_an_oversized_reply_is_refused(pair):
    client, cam = pair
    cam.sendall(dev.HEADER.pack(dev.REP_MAGIC, dev.OP_PING, 0, 1, dev.READ_MAX + 1))
    with pytest.raises(dev.DevmonError, match="more than any op"):
        client.ping()


def test_the_access_point_key_comes_from_the_build_environment(monkeypatch, capsys):
    monkeypatch.delenv(plugin.DEVMON_KEY_ENV, raising=False)
    assert plugin.devmon_key_flags() == []
    assert "public default" in capsys.readouterr().err
    monkeypatch.setenv(plugin.DEVMON_KEY_ENV, "lab-key-1234")
    assert plugin.devmon_key_flags() == ['-DFENYKL_DEVMON_KEY="lab-key-1234"']
    for bad in ("short", 'quo"te-key', "back\\slash-key", "x" * 64, "tab\tkey-12"):
        monkeypatch.setenv(plugin.DEVMON_KEY_ENV, bad)
        with pytest.raises(BuildError, match="8 to 63"):
            plugin.devmon_key_flags()


def test_key_sends_the_index_and_the_hold(pair):
    client, cam = pair
    fake = FakeCamera(cam, lambda op, p: (0, b""))
    client.key("MENU", 300)
    client.key("FULL")
    assert fake.requests == [
        (dev.OP_KEY, struct.pack("<2I", dev.KEYS.index("MENU"), 300)),
        (dev.OP_KEY, struct.pack("<2I", dev.KEYS.index("FULL"), dev.HOLD_DEFAULT)),
    ]
    for name, hold in (("POWER", 100), ("MENU", 0), ("MENU", dev.HOLD_MAX + 1)):
        with pytest.raises(dev.DevmonError):
            client.key(name, hold)


def test_key_names_follow_the_camera_order():
    assert dev.KEYS[0] == "OK" and dev.KEYS[-1] == "FULL" and len(dev.KEYS) == 15


def test_dial_sends_the_index_and_returns_the_previous_value(pair):
    client, cam = pair
    fake = FakeCamera(cam, lambda op, p: (0, struct.pack("<I", 0x01)))
    assert client.dial("SCN") == 0x01
    assert fake.requests == [(dev.OP_DIAL, struct.pack("<I", 6))]
    with pytest.raises(dev.DevmonError, match="unknown position"):
        client.dial("C1")
    assert len(fake.requests) == 1


def test_dial_names_a_monitor_without_it_and_refuses_a_bad_reply(pair):
    client, cam = pair
    replies = [(-2, b""), (0, b"\x01\x00")]
    FakeCamera(cam, lambda op, p: replies.pop(0))
    with pytest.raises(dev.DevmonError, match="has no DIAL"):
        client.dial("P")
    with pytest.raises(dev.DevmonError, match="2 bytes, not 4"):
        client.dial("P")


def test_key_sequences_parse_names_holds_and_pauses():
    assert dev.parse_keys("menu 300 DOWN:50  ok half:1500") == [
        ("MENU", dev.HOLD_DEFAULT),
        300,
        ("DOWN", 50),
        ("OK", dev.HOLD_DEFAULT),
        ("HALF", 1500),
    ]
    for bad in ("LEVER", "MENU:x", "-5"):
        with pytest.raises(dev.DevmonError, match="bad key step"):
            dev.parse_keys(bad)


def test_wait_asks_iwd_to_rejoin_while_nothing_answers(monkeypatch):
    joins = []
    monkeypatch.setattr(
        dev,
        "connect_ap",
        lambda iface, ssid, timeout: joins.append((iface, ssid, timeout)),
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(dev.DevmonError, match="no monitor answer"):
        dev.wait(
            "127.0.0.1", port, 0.35, interval=0.1, iface="wlan0", ssid="AP", rejoin=0.2
        )
    assert joins[0][:2] == ("wlan0", "AP") and 1 <= len(joins) <= 3
    # iwctl gets only the time left before the deadline
    assert all(0 < j[2] <= 0.35 for j in joins)


# iwctl's table, colour codes included as iwd prints them.
IWD_SHOW = (
    "                                 Station: wlan0\n"
    "--------------------------------------------------------------------------------\n"
    "  Settable  Property              Value\n"
    "--------------------------------------------------------------------------------\n"
    "            Scanning              no\n"
    "            State                 \x1b[1;90m{}\x1b[0m\n"
    "            Connected network     {}\n"
)


@pytest.mark.parametrize(
    ("state", "network", "connects"),
    [
        ("disconnected", "", True),
        ("connecting", "AP", False),
        ("connected", "AP", False),
        ("connected", "Home Net", True),
    ],
)
def test_rejoin_never_interrupts_an_association(monkeypatch, state, network, connects):
    calls = []

    def run(cmd, timeout, **kw):
        calls.append((cmd[1:], timeout))
        out = IWD_SHOW.format(state, network) if cmd[3] == "show" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(dev.subprocess, "run", run)
    dev.connect_ap("wlan0", "AP", 4.0)
    assert calls[0][0] == ["station", "wlan0", "show"]
    joined = [c for c in calls if c[0] == ["station", "wlan0", "connect", "AP"]]
    assert bool(joined) == connects
    assert all(timeout <= 4.0 for _, timeout in calls)


def test_rejoin_rescans_when_iwd_does_not_know_the_network(monkeypatch, capsys):
    calls = []

    def run(cmd, timeout, **kw):
        calls.append(cmd[1:])
        if cmd[3] == "show":
            return subprocess.CompletedProcess(
                cmd, 0, IWD_SHOW.format("disconnected", ""), ""
            )
        if cmd[3] == "connect":
            return subprocess.CompletedProcess(
                cmd, 1, "Invalid network name 'AP'\n", ""
            )
        return subprocess.CompletedProcess(
            cmd, 1, "Operation already in progress\n", ""
        )

    monkeypatch.setattr(dev.subprocess, "run", run)
    dev.connect_ap("wlan0", "AP", 4.0)
    assert calls[-1] == ["station", "wlan0", "scan"]
    assert capsys.readouterr().err == ""


def test_rejoin_reports_iwctl_failures(monkeypatch, capsys):
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "No such device\n"),
    )
    dev.connect_ap("wlan9", "AP")
    assert "station wlan9 show: exit 1: No such device" in capsys.readouterr().err


@needs_toolchain
def test_main_builds_with_the_dev_monitor_on_request(tmp_path):
    main = sdk.SDK / "projects" / "main"
    image = plugin.build(main, tmp_path, EM10II, with_capabilities=["devmon"])
    assert b"devmon.ap" in image.read_bytes()
    syms = _symbol_sections(EM10II.tool("objdump"), tmp_path / "plugin.elf")
    assert "_fenykl_devmon_start" in syms
    with pytest.raises(BuildError, match="opt-in"):
        plugin.build(main, tmp_path / "key", EM10II, with_capabilities=["key"])


# The grab's firmware words in the uncached alias (firmware.toml, v1.4).
BPP = 0xA0BC51CC  # gpi_layer_bpp
DRAW = 0xA15E6DC0  # gpi_draw_buf
HIDE = 0xA15E6DDC  # gpi_hide_mask
DEVICE = 0xA1C19FB8  # disp_device
STATE = 0xA1C19FC2  # disp_state
SHADOW = 0xA1C1A070  # osd_shadow: L1, L0, L2
PAL = 0xA19AE019  # osd_palette: L1 at +0, L0 at +0x400, L2 at +0x480
# OSD buffers as the display shadow holds them (bit 31 clear) and where READ finds them.
L0, L1, L2, L1_BACK = 0x3E100000, 0x3E000000, 0x3E200000, 0x3E300000
PLANES = (0xBE100000, 0xBE000000, 0xBE200000, 0xBE300000)


class FakeRam:
    """Byte ranges of a camera's uncached DRAM; a READ outside them is refused (-4).
    `on_plane()` runs after each READ of an OSD buffer, to flip like the GUI."""

    def __init__(self, device=1, state=2, hidden=0, bpp=b"\x04\x08\x04"):
        self.mem = {
            BPP: bytearray(bpp),
            DRAW: bytearray(0x20),  # .. gpi_hide_mask
            DEVICE: bytearray(SHADOW + 12 - DEVICE),  # .. disp_state .. osd_shadow
            PAL: bytearray(0x500),
        }
        self.planes = {a: bytearray(896 * 600) for a in PLANES}
        self.poke(DEVICE, struct.pack("<2I", device, 0))
        self.poke(STATE, bytes([state]))
        self.poke(HIDE, struct.pack("<H", hidden))
        self.show(L1, L0, L2)
        self.on_plane = lambda: None

    def poke(self, addr: int, data: bytes) -> None:
        for base, mem in {**self.mem, **self.planes}.items():
            if base <= addr < base + len(mem):
                mem[addr - base : addr - base + len(data)] = data
                return
        raise AssertionError(f"0x{addr:08x} not mapped")

    def show(self, l1: int, l0: int, l2: int) -> None:
        self.poke(SHADOW, struct.pack("<3I", l1, l0, l2))

    def handler(self, op: int, p: bytes) -> tuple[int, bytes]:
        assert op == dev.OP_READ
        addr, n = struct.unpack("<2I", p)
        for base, mem in {**self.mem, **self.planes}.items():
            if base <= addr and addr + n <= base + len(mem):
                reply = bytes(mem[addr - base : addr - base + n])
                if base in self.planes:
                    self.on_plane()
                return 0, reply
        return -4, b""


def _grab(pair, ram: FakeRam) -> tuple[screen.Screen, list[tuple[int, int]]]:
    client, cam = pair
    fake = FakeCamera(cam, ram.handler)
    shot = screen.grab(client.read, screen.Symbols.load())
    return shot, [struct.unpack("<2I", p) for _, p in fake.requests]


def _rgb(shot: screen.Screen, x: int, y: int) -> bytes:
    i = 3 * (y * shot.w + x)
    return bytes(screen.compose(shot.w, shot.h, shot.layers)[i : i + 3])


def test_screen_stacks_l2_l0_l1_with_their_palettes(pair, tmp_path):
    ram = FakeRam()
    ram.poke(PAL + 0x480 + 4 * 1, bytes((0, 255, 0, 32)))
    ram.poke(PAL + 0x400 + 4 * 3, bytes((255, 0, 0, 16)))
    ram.poke(PAL + 4 * 200, bytes((255, 255, 255, 16)))
    ram.poke(PAL + 4 * 7, bytes((0, 0, 255, 40)))  # alpha clamped to 32
    ram.poke(PLANES[2] + 5 * 896 + 10, b"\x01")  # pixel (10, 5)
    ram.poke(PLANES[0] + 5 * 896 + 10, b"\x03")
    ram.poke(PLANES[1] + 5 * 896 + 10, bytes([200]))
    ram.poke(PLANES[0] + 5 * 896 + 11, bytes([40]))  # past L0's 32 entries: transparent
    ram.poke(PLANES[1] + 5 * 896 + 12, b"\x07")
    shot, reads = _grab(pair, ram)
    assert (shot.w, shot.h, shot.stable, shot.tries) == (720, 480, True, 1)
    assert _rgb(shot, 10, 5) == bytes((191, 191, 127))
    assert _rgb(shot, 11, 5) == bytes(3)
    assert _rgb(shot, 12, 5) == bytes((0, 0, 255))
    assert all(a >= 0xA0000000 for a, _ in reads)
    assert (PAL, 0x500) in reads
    paths = screen.save(shot, tmp_path / "s.png")
    assert [p.name for p in paths] == ["s.png", "s-L0.png", "s-L1.png", "s-L2.png"]


def test_screen_skips_a_hidden_layer(pair):
    ram = FakeRam(hidden=0x100)
    ram.poke(PAL + 0x480 + 4 * 1, bytes((0, 255, 0, 32)))
    ram.poke(PLANES[2], b"\x01")
    shot, reads = _grab(pair, ram)
    assert sorted(shot.layers) == [0, 1]
    assert _rgb(shot, 0, 0) == bytes(3)
    assert not [a for a, _ in reads if PLANES[2] <= a < PLANES[2] + 896 * 600]


def test_screen_reads_the_evf_source_size(pair):
    ram = FakeRam(device=6)
    ram.poke(PAL + 4 * 9, bytes((1, 2, 3, 32)))
    ram.poke(PLANES[1] + 479 * 896 + 639, b"\x09")
    shot, reads = _grab(pair, ram)
    assert (shot.w, shot.h, shot.device) == (640, 480, 6)
    assert _rgb(shot, 639, 479) == bytes((1, 2, 3))
    l1 = [(a, n) for a, n in reads if PLANES[1] <= a < PLANES[1] + 896 * 600]
    assert l1[-1][0] + l1[-1][1] == PLANES[1] + 479 * 896 + 640
    assert max(n for _, n in l1) == 32 * 896


@pytest.mark.parametrize(
    ("ram", "error"),
    [
        (FakeRam(bpp=b"\x08\x08\x08"), "gpi_layer_bpp"),
        (FakeRam(state=0), "not live"),
        (FakeRam(device=5), "device 5 not supported"),
    ],
)
def test_screen_refuses_an_unknown_display(pair, ram, error):
    with pytest.raises(screen.ScreenError, match=error):
        _grab(pair, ram)


@pytest.mark.parametrize("shown", [0x9C000048, 0x3E100002, 0x00010000])
def test_screen_refuses_a_shadow_that_is_no_dram_buffer(pair, shown):
    ram = FakeRam()
    ram.show(L1, shown, L2)
    with pytest.raises(screen.ScreenError, match="L0: shown buffer"):
        _grab(pair, ram)


def test_screen_reads_again_after_a_flip(pair):
    ram = FakeRam()
    ram.poke(PAL + 4 * 1, bytes((9, 9, 9, 32)))
    ram.poke(PLANES[3], b"\x01")

    def flip_once():
        ram.show(L1_BACK, L0, L2)
        ram.on_plane = lambda: None

    ram.on_plane = flip_once
    shot, _ = _grab(pair, ram)
    assert (shot.stable, shot.tries, shot.layers[1].shown) == (True, 2, L1_BACK)
    assert _rgb(shot, 0, 0) == bytes((9, 9, 9))


def test_screen_sees_two_flips_within_one_layer(pair):
    ram = FakeRam()
    chunks = iter(range(1000))

    def flip_twice():
        n = next(chunks)
        if n in (3, 4):  # draw buffer 1 after the 4th chunk, 0 again after the 5th
            ram.poke(DRAW, bytes([n == 3]))

    ram.on_plane = flip_twice
    shot, _ = _grab(pair, ram)
    assert (shot.stable, shot.tries) == (True, 2)


def test_screen_keeps_the_second_try_when_the_gui_keeps_flipping(pair):
    ram = FakeRam()
    chunks = iter(range(1000))
    ram.on_plane = lambda: ram.poke(DRAW, bytes([next(chunks) & 1]))
    shot, _ = _grab(pair, ram)
    assert (shot.stable, shot.tries, len(shot.layers)) == (False, 2, 3)


def test_screen_takes_the_new_size_after_an_output_switch(pair):
    ram = FakeRam()

    def switch():
        ram.poke(DEVICE, struct.pack("<I", 6))
        ram.on_plane = lambda: None

    ram.on_plane = switch
    shot, _ = _grab(pair, ram)
    assert (shot.w, shot.device, shot.stable, shot.tries) == (640, 6, True, 2)


def test_screen_fails_when_the_output_keeps_changing(pair):
    ram = FakeRam()
    chunks = iter(range(1000))
    ram.on_plane = lambda: ram.poke(DEVICE, struct.pack("<I", (1, 6)[next(chunks) & 1]))
    with pytest.raises(screen.ScreenError, match="changed during every try"):
        _grab(pair, ram)
