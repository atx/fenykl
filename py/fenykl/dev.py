"""`fenykl dev`: the PC side of the dev monitor (sdk/docs/devmon.md). Framed protocol v1 over TCP
to the camera's access point, standard library only."""

import argparse
import os
import re
import socket
import struct
import subprocess
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import screen, sdk, tp

HOST = "192.168.0.10"
PORT = 5555
REQ_MAGIC = 0x31444B46
REP_MAGIC = 0x31524B46
HEADER = struct.Struct("<IHhII")
READ_MAX = 0x100000
IO_DATA = 0x4000
NAME_FIELD = 12
NAME = re.compile(r"FENYKL\.[A-Z0-9]{1,3}")
# Read only: the screenshot library's files and the camera's photos (in the path field).
PATH_FIELD = 32
SHOT = re.compile(r"SHOT[0-9]{4}\.PPM")
SHOT_FILES = 10000
DCIM_DIR = re.compile(r"DCIM(/[0-9]{3}[A-Z0-9]{5})?")
PHOTO = re.compile(r"DCIM/[0-9]{3}[A-Z0-9]{5}/[A-Z0-9_]{1,8}\.(JPG|ORF|MOV)")
PHOTO_EXTS = ("JPG", "ORF", "MOV")
ENTRY = struct.Struct("<12sII")
POWER_ENV = {"on": "FENYKL_DEV_POWER_ON", "off": "FENYKL_DEV_POWER_OFF"}

OP_PING = 0x00
OP_READ = 0x01
OP_WRITE = 0x02
OP_LOG = 0x03
OP_TIMING = 0x04
OP_TPLOAD = 0x10
OP_TPRUN = 0x11
OP_FPUT = 0x20
OP_FGET = 0x21
OP_FDEL = 0x22
OP_FLIST = 0x23
OP_POWEROFF = 0x30
OP_KEY = 0x31
OP_DIAL = 0x32
# KEY: the buttons in the camera's order (enum fdm_key); FULL is a half press plus the full press.
KEYS = (
    "OK", "UP", "DOWN", "LEFT", "RIGHT", "INFO", "MENU", "PLAY", "DEL",
    "FN", "FN2", "FN3", "REC", "HALF", "FULL",
)  # fmt: skip
# DIAL: the mode-dial positions in the camera's order (enum fdm_dial).
DIALS = ("P", "A", "S", "M", "MOVIE", "STORY", "SCN", "ART", "IAUTO")
HOLD_DEFAULT = 100
HOLD_MAX = 10000
SSID = "FENYKL-DEV"

ERRORS = {
    -1: "bad frame",
    -2: "unknown op",
    -3: "bad arguments",
    -4: "address range",
    -5: "file name not allowed for this op",
    -6: "busy (a CPU0 job or a test program is still running)",
    -7: "timeout",
    -8: "card I/O or key inject refused",
    -9: "card gate closed",
    -10: "size or CRC-32 of the written file differs",
    -11: "test program header invalid",
    -12: "I-cache invalidate failed",
    -13: "no such file (or its open failed)",
    -14: "FENYKL.BIN is installed by hand only",
    -15: "an earlier Wi-Fi stop timed out: no power-off this boot",
}
TIMEOUT = -7
# The last PUT chunk carries the commit (check, copy, check): the camera's own wait is 30 s.
COMMIT_TIMEOUT = 35.0
# A listing rescans the directory for every entry: up to the camera's own wait.
LIST_TIMEOUT = 35.0
NO_DIAL = "this monitor build has no DIAL"
NO_PHOTO_FETCH = (
    "this monitor build serves FENYKL.* only: load a build with photo fetch"
)


class DevmonError(Exception):
    pass


class DevmonStatus(DevmonError):
    """The camera refused a request."""

    def __init__(self, op: int, status: int):
        self.status = status
        super().__init__(
            f"op 0x{op:02x}: {ERRORS.get(status, 'error')} (status {status})"
        )


@dataclass(frozen=True)
class Timing:
    """The camera's timing of the last READ reply (struct fdm_timing)."""

    bytes: int
    us: int
    sends: int
    max_send_us: int
    stalls: int
    stall_us: int
    piece: int
    pace_ms: int
    sock: int

    def __str__(self) -> str:
        rate = self.bytes / self.us * 1e6 / 1024 if self.us else 0.0
        return (
            f"{self.bytes} bytes in {self.us} us ({rate:.1f} KiB/s), {self.sends} sends of "
            f"{self.piece} (pause {self.pace_ms} ms), longest {self.max_send_us} us, "
            f"{self.stalls} stalls totalling {self.stall_us} us, socket {self.sock}"
        )


def writable_field(name: str) -> bytes:
    """The name field of a file PUT or DEL may touch: FENYKL.BIN, the loader's stable image, is
    installed by hand only."""
    if name == "FENYKL.BIN":
        raise DevmonError(
            "FENYKL.BIN is installed by hand only: send test builds as FENYKL.TST"
        )
    return name_field(name)


def name_field(name: str) -> bytes:
    if not NAME.fullmatch(name):
        raise DevmonError(
            f"{name!r}: the monitor only handles FENYKL.<1-3 of A-Z 0-9> at the card root"
        )
    return name.encode().ljust(NAME_FIELD, b"\0")


def read_field(name: str) -> bytes:
    """The name field of a file GET may read: FENYKL.* and SHOTnnnn.PPM in the 12-byte field,
    DCIM/<dir>/<file> photos in the path field."""
    if NAME.fullmatch(name) or SHOT.fullmatch(name):
        return name.encode().ljust(NAME_FIELD, b"\0")
    if PHOTO.fullmatch(name):
        return name.encode().ljust(PATH_FIELD, b"\0")
    raise DevmonError(
        f"{name!r}: the monitor reads only FENYKL.<1-3 of A-Z 0-9>, SHOTnnnn.PPM and "
        "DCIM/<3 digits, 5 of A-Z 0-9>/<1-8 of A-Z 0-9 _>.JPG, .ORF or .MOV"
    )


@dataclass(frozen=True)
class Entry:
    """A listed card file or directory; mtime is the FAT write time, date << 16 | time."""

    name: str
    size: int
    mtime: int

    def time(self) -> str:
        date, t = self.mtime >> 16, self.mtime & 0xFFFF
        return (
            f"{1980 + (date >> 9):04d}-{(date >> 5) & 15:02d}-{date & 31:02d} "
            f"{t >> 11:02d}:{(t >> 5) & 63:02d}:{(t & 31) * 2:02d}"
        )


class Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.next_id = 1

    @classmethod
    def connect(
        cls, host: str = HOST, port: int = PORT, timeout: float = 10.0
    ) -> "Client":
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise DevmonError(f"{host}:{port}: {e}") from e
        return cls(sock)

    def close(self) -> None:
        self.sock.close()

    def _recv(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except TimeoutError as e:
                raise DevmonError(f"timeout with {len(data)} of {n} bytes") from e
            except OSError as e:
                raise DevmonError(
                    f"connection lost with {len(data)} of {n} bytes: {e}"
                ) from e
            if not chunk:
                raise DevmonError(f"connection closed after {len(data)} of {n} bytes")
            data += chunk
        return bytes(data)

    def call(self, op: int, payload: bytes = b"") -> bytes:
        """One request; the reply payload. DevmonStatus on a refusal."""
        rid = self.next_id
        self.next_id = (self.next_id + 1) & 0xFFFFFFFF
        try:
            self.sock.sendall(
                HEADER.pack(REQ_MAGIC, op, 0, rid, len(payload)) + payload
            )
        except OSError as e:
            raise DevmonError(f"op 0x{op:02x}: send failed: {e}") from e
        magic, rop, status, rrid, n = HEADER.unpack(self._recv(HEADER.size))
        if magic != REP_MAGIC or rop != op or rrid != rid:
            raise DevmonError(
                f"bad reply: magic 0x{magic:08x}, op 0x{rop:x}, id {rrid} for op 0x{op:x} id {rid}"
            )
        if status:
            raise DevmonStatus(op, status)
        if n > READ_MAX:
            raise DevmonError(
                f"op 0x{op:02x}: reply of {n} bytes, more than any op sends"
            )
        return self._recv(n)

    def ping(self) -> str:
        return self.call(OP_PING).decode("ascii", "replace").strip()

    def read(self, addr: int, n: int, piece: int = 0, pace_ms: int = 0) -> bytes:
        out = bytearray()
        while len(out) < n:
            k = min(READ_MAX, n - len(out))
            args = [addr + len(out), k] + ([piece, pace_ms] if piece or pace_ms else [])
            chunk = self.call(OP_READ, struct.pack(f"<{len(args)}I", *args))
            if len(chunk) != k:
                raise DevmonError(f"READ of {k} bytes returned {len(chunk)}")
            out += chunk
        return bytes(out)

    def write(self, addr: int, data: bytes) -> None:
        for off in range(0, len(data), IO_DATA):
            self.call(
                OP_WRITE, struct.pack("<I", addr + off) + data[off : off + IO_DATA]
            )

    def log(self, since: int = 0) -> tuple[int, bytes]:
        """The log text from byte `since` (or the oldest kept) and the byte count after it."""
        reply = self.call(OP_LOG, struct.pack("<I", since))
        (end,) = struct.unpack_from("<I", reply)
        return end, reply[4:]

    def timing(self) -> Timing:
        return Timing(*struct.unpack("<9I", self.call(OP_TIMING)))

    def load(self, image: bytes, slot: int) -> None:
        """Upload a test program to `slot` (TPLOAD, through CPU0's cache)."""
        for off in range(0, len(image), IO_DATA):
            self.call(
                OP_TPLOAD, struct.pack("<2I", slot, off) + image[off : off + IO_DATA]
            )

    def run(self, slot: int, arg: int, timeout_ms: int) -> tuple[int, int]:
        """Run the test program in `slot`; its result and run time in us. A program still running
        after timeout_ms answers TIMEOUT and keeps running."""
        old = self.sock.gettimeout()
        self.sock.settimeout(max(old or 0.0, timeout_ms / 1000 + 5.0))
        try:
            reply = self.call(OP_TPRUN, struct.pack("<3I", slot, arg, timeout_ms))
        finally:
            self.sock.settimeout(old)
        result, us = struct.unpack("<iI", reply)
        return result, us

    def exec(
        self, image: bytes, slot: int, arg: int, timeout_ms: int
    ) -> tuple[int, int]:
        """Upload a test program to `slot` and run it; its result and run time in us."""
        self.load(image, slot)
        return self.run(slot, arg, timeout_ms)

    def put(self, name: str, data: bytes) -> None:
        """Write a FENYKL.* file (not FENYKL.BIN). A timeout or a lost connection on the last
        chunk leaves the card in an unknown state: the commit may still run on the camera."""
        field = writable_field(name)
        crc = zlib.crc32(data)
        off = 0
        while True:
            chunk = data[off : off + IO_DATA]
            payload = struct.pack("<3I", off, len(data), crc) + field + chunk
            if off + len(chunk) < len(data):
                self.call(OP_FPUT, payload)
                off += len(chunk)
                continue
            old = self.sock.gettimeout()
            self.sock.settimeout(max(old or 0.0, COMMIT_TIMEOUT))
            try:
                self.call(OP_FPUT, payload)
            except DevmonStatus as e:
                if e.status != TIMEOUT:
                    raise
                raise DevmonError(
                    f"PUT {name}: the camera timed out in the commit: card state unknown, "
                    "wait and check with get before anything else (never power-cycle)"
                ) from e
            except DevmonError as e:
                raise DevmonError(
                    f"PUT {name}: no reply to the commit ({e}): card state unknown, "
                    "wait and check with get before anything else (never power-cycle)"
                ) from e
            finally:
                self.sock.settimeout(old)
            return

    def get(self, name: str, progress: Callable[[int], None] | None = None) -> bytes:
        """A file in chunks at increasing offsets, until a short chunk; progress(bytes so far)
        after each."""
        field = read_field(name)
        out = bytearray()
        while True:
            try:
                chunk = self.call(
                    OP_FGET, struct.pack("<2I", len(out), IO_DATA) + field
                )
            except DevmonStatus as e:
                if out or NAME.fullmatch(name) or e.status not in (-3, -5):
                    raise
                raise DevmonError(f"GET {name}: {e}: {NO_PHOTO_FETCH}") from e
            out += chunk
            if progress:
                progress(len(out))
            if len(chunk) < IO_DATA:
                return bytes(out)

    def photo_size(self, name: str) -> int:
        """The size of DCIM/<dir>/<file> in its folder's listing."""
        folder, file = name.rsplit("/", 1)
        for e in self.list(folder):
            if e.name == file:
                return e.size
        raise DevmonError(f"{name}: not in the listing of {folder}")

    def exists(self, name: str) -> bool:
        """False on -13, which is also what a failed open of an existing file answers."""
        try:
            self.call(OP_FGET, struct.pack("<2I", 0, 1) + read_field(name))
        except DevmonStatus as e:
            if e.status == -13:
                return False
            raise
        return True

    def list(self, path: str = "DCIM") -> list[Entry]:
        """The directories of DCIM, or the photos of DCIM/<dir>, in the card's order."""
        if not DCIM_DIR.fullmatch(path):
            raise DevmonError(f"{path!r}: the monitor lists only DCIM and DCIM/<dir>")
        field = path.encode().ljust(PATH_FIELD, b"\0")
        entries: list[Entry] = []
        start = 0
        old = self.sock.gettimeout()
        self.sock.settimeout(max(old or 0.0, LIST_TIMEOUT))
        try:
            while True:
                try:
                    reply = self.call(OP_FLIST, struct.pack("<I", start) + field)
                except DevmonStatus as e:
                    if e.status != -2:
                        raise
                    raise DevmonError(f"LIST {path}: {e}: {NO_PHOTO_FETCH}") from e
                (start,) = struct.unpack_from("<I", reply)
                for off in range(4, len(reply), ENTRY.size):
                    name, size, mtime = ENTRY.unpack_from(reply, off)
                    entries.append(
                        Entry(name.rstrip(b"\0").decode("ascii"), size, mtime)
                    )
                if not start:
                    return entries
        finally:
            self.sock.settimeout(old)

    def delete(self, name: str) -> None:
        self.call(OP_FDEL, writable_field(name))

    def poweroff(self) -> None:
        self.call(OP_POWEROFF)

    def key(self, name: str, hold_ms: int = HOLD_DEFAULT) -> None:
        """Press, hold and release a camera button (the camera waits out the hold)."""
        if name not in KEYS:
            raise DevmonError(f"unknown key {name!r} (keys: {' '.join(KEYS)})")
        if not 1 <= hold_ms <= HOLD_MAX:
            raise DevmonError(f"hold {hold_ms} ms outside 1..{HOLD_MAX}")
        self.call(OP_KEY, struct.pack("<2I", KEYS.index(name), hold_ms))

    def dial(self, name: str) -> int:
        """Set the effective mode-dial position; the dial value before it."""
        if name not in DIALS:
            raise DevmonError(
                f"unknown position {name!r} (positions: {' '.join(DIALS)})"
            )
        try:
            reply = self.call(OP_DIAL, struct.pack("<I", DIALS.index(name)))
        except DevmonStatus as e:
            if e.status != -2:
                raise
            raise DevmonError(f"DIAL {name}: {e}: {NO_DIAL}") from e
        if len(reply) != 4:
            raise DevmonError(f"DIAL {name}: reply of {len(reply)} bytes, not 4")
        (prev,) = struct.unpack("<I", reply)
        return prev


def parse_keys(text: str) -> list[tuple[str, int] | int]:
    """A key sequence: key names (NAME or NAME:hold_ms) and pauses in ms, e.g.
    "MENU 300 DOWN 200 OK HALF:1500"; ("NAME", hold) or the pause."""
    steps: list[tuple[str, int] | int] = []
    for word in text.split():
        if word.isdigit():
            steps.append(int(word))
            continue
        name, _, hold = word.upper().partition(":")
        if name not in KEYS or (hold and not hold.isdigit()):
            raise DevmonError(f"bad key step {word!r} (keys: {' '.join(KEYS)})")
        steps.append((name, int(hold) if hold else HOLD_DEFAULT))
    return steps


def latest_photo(c: Client, ext: str | None = None) -> tuple[str, Entry]:
    """The newest photo (of ext) in the last DCIM folder that has one: latest write time, then
    the file number (the last 4 characters of the name)."""
    for folder in sorted((e.name for e in c.list("DCIM")), reverse=True):
        files = [
            e
            for e in c.list(f"DCIM/{folder}")
            if ext is None or e.name.endswith("." + ext)
        ]
        if files:
            best = max(files, key=lambda e: (e.mtime, e.name[-8:-4], e.name))
            return f"DCIM/{folder}/{best.name}", best
    raise DevmonError(f"no {ext or 'photo'} file in DCIM")


def shot_end(exists: Callable[[int], bool]) -> int:
    """The end of the run of SHOTnnnn.PPM from SHOT0000 (the screenshot library numbers its next
    file the same way): doubling, then bisection."""
    if not exists(0):
        return 0
    hi = 1
    while hi < SHOT_FILES and exists(hi):
        hi *= 2
    lo, hi = hi // 2, min(hi, SHOT_FILES)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if exists(mid):
            lo = mid
        else:
            hi = mid
    return hi


def latest_shot(c: Client) -> str:
    end = shot_end(lambda n: c.exists(f"SHOT{n:04d}.PPM"))
    if not end:
        raise DevmonError("no SHOT0000.PPM on the card")
    return f"SHOT{end - 1:04d}.PPM"


def power(action: str, delay: float = 3.0) -> None:
    """Run the power command of $FENYKL_DEV_POWER_ON / _OFF (shell commands); cycle = off, delay,
    on."""
    if action == "cycle":
        power("off")
        time.sleep(delay)
        power("on")
        return
    var = POWER_ENV[action]
    cmd = os.environ.get(var)
    if not cmd:
        raise DevmonError(f"no power command: set {var} to a shell command")
    result = subprocess.run(cmd, shell=True, check=False)
    if result.returncode:
        raise DevmonError(f"{var} ({cmd!r}) exited with {result.returncode}")


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
IWD_STATE = re.compile(r"^\s*State\s+(\S+)", re.MULTILINE)
IWD_NETWORK = re.compile(r"^[ \t]*Connected network[ \t]+(\S.*?)[ \t]*$", re.MULTILINE)
IWCTL_SHOW_S = 10.0
IWCTL_CONNECT_S = 60.0


def _iwctl(*args: str, timeout: float) -> subprocess.CompletedProcess | None:
    """iwctl's result, None after a timeout (reported)."""
    cmd = ["iwctl", *args]
    try:
        return subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise DevmonError("--connect needs iwctl (iwd)") from e
    except subprocess.TimeoutExpired:
        print(
            f"--connect: {' '.join(cmd)}: no answer in {timeout:g} s", file=sys.stderr
        )
        return None


def _iwctl_failed(r: subprocess.CompletedProcess) -> None:
    text = ANSI.sub("", r.stderr or r.stdout).strip()
    print(
        f"--connect: {' '.join(r.args)}: exit {r.returncode}: {text}", file=sys.stderr
    )


def station(iface: str, timeout: float) -> tuple[str, str | None] | None:
    """iwd's State of the station on iface (connected, connecting, disconnected, ...) and its
    connected network; None when iwctl cannot tell (reported)."""
    r = _iwctl("station", iface, "show", timeout=timeout)
    if r is None:
        return None
    if r.returncode:
        _iwctl_failed(r)
        return None
    text = ANSI.sub("", r.stdout)
    m = IWD_STATE.search(text)
    if not m:
        print(f"--connect: no State in iwctl station {iface} show", file=sys.stderr)
        return None
    net = IWD_NETWORK.search(text)
    return m.group(1), net.group(1) if net else None


def connect_ap(iface: str, ssid: str, timeout: float = IWCTL_CONNECT_S) -> None:
    """Ask iwd to join the camera's access point on iface when the station is disconnected or
    connected to another network, never while it is connecting: a connect during an association
    aborts it. Both iwctl calls end within `timeout` s. A failure is not an error (the camera may
    still be booting) but is reported."""
    end = time.monotonic() + timeout
    st = station(iface, min(IWCTL_SHOW_S, timeout))
    if st is None:
        return
    state, net = st
    if state == "connected" and net != ssid:
        print(f"--connect: {iface} is on {net}, joining {ssid}", file=sys.stderr)
    elif state != "disconnected":
        return
    left = min(IWCTL_CONNECT_S, end - time.monotonic())
    if left <= 0:
        return
    r = _iwctl("station", iface, "connect", ssid, timeout=left)
    if r is None or not r.returncode:
        return
    if "Invalid network name" not in ANSI.sub("", r.stderr or r.stdout):
        _iwctl_failed(r)
        return
    # iwd joins only networks of its last scan, and rescans rarely after a long disconnect.
    r = _iwctl(
        "station", iface, "scan", timeout=min(IWCTL_SHOW_S, end - time.monotonic())
    )
    if (
        r is not None
        and r.returncode
        and "in progress" not in ANSI.sub("", r.stderr or r.stdout)
    ):
        _iwctl_failed(r)


def wait(
    host: str,
    port: int,
    timeout: float,
    interval: float = 1.0,
    iface: str | None = None,
    ssid: str = SSID,
    rejoin: float = 3.0,
) -> str:
    """Poll until the monitor answers PING; its text. With iface, an attempt that fails asks iwd
    to join ssid if the station is disconnected or on another network, at most every `rejoin`
    seconds and within the deadline (iwd does not always rejoin by itself)."""
    deadline = time.monotonic() + timeout
    last_join = -rejoin
    while True:
        try:
            client = Client.connect(host, port, timeout=min(5.0, timeout))
            try:
                return client.ping()
            finally:
                client.close()
        except DevmonError as e:
            if time.monotonic() >= deadline:
                raise DevmonError(f"no monitor answer within {timeout:g} s: {e}") from e
            if iface and time.monotonic() - last_join >= rejoin:
                last_join = time.monotonic()
                connect_ap(iface, ssid, deadline - last_join)
        time.sleep(interval)


def _int(text: str) -> int:
    return int(text, 0)


def _client(args: argparse.Namespace) -> Client:
    return Client.connect(args.host, args.port, args.timeout)


def cmd_ping(args: argparse.Namespace, c: Client) -> None:
    print(c.ping())


def cmd_read(args: argparse.Namespace, c: Client) -> None:
    data = c.read(args.addr, args.len, args.piece, args.pace)
    if args.output:
        args.output.write_bytes(data)
        print(f"{len(data)} bytes to {args.output}")
    else:
        for off in range(0, len(data), 16):
            row = data[off : off + 16]
            print(f"{args.addr + off:08x}  {row.hex(' ')}")
    if args.timing:
        print(c.timing())


def cmd_write(args: argparse.Namespace, c: Client) -> None:
    data = args.input.read_bytes() if args.input else bytes.fromhex(args.hex or "")
    if not data:
        raise DevmonError("nothing to write: give HEX bytes or -i FILE")
    c.write(args.addr, data)
    print(f"{len(data)} bytes at 0x{args.addr:08x}")


def cmd_timing(args: argparse.Namespace, c: Client) -> None:
    print(c.timing())


def cmd_exec(args: argparse.Namespace, c: Client) -> None:
    image = args.image.read_bytes()
    slot = tp.slot_of(sdk.target(args.target), tp.check(image))
    result, us = c.exec(image, slot, args.arg, args.run_timeout)
    print(f"slot {slot}: result {result} (0x{result & 0xFFFFFFFF:08x}) in {us} us")


def cmd_put(args: argparse.Namespace, c: Client) -> None:
    data = args.file.read_bytes()
    c.put(args.name or args.file.name.upper(), data)
    print(f"{len(data)} bytes to C:\\{args.name or args.file.name.upper()}")


PROGRESS_STEP = 1 << 20


def _progress(n: int) -> None:
    if n >= PROGRESS_STEP:
        print(f"\r{n >> 20} MiB", end="", file=sys.stderr, flush=True)


def cmd_get(args: argparse.Namespace, c: Client) -> None:
    """A read error ends a file early like its end does (rpc_fread answers 0 for both): a photo
    is checked against its folder listing's size; other names have no size source."""
    if args.ext and not args.latest:
        raise DevmonError("--ext goes with --latest")
    size = None
    if args.latest:
        name, entry = latest_photo(c, args.ext)
        size = entry.size
    elif args.latest_shot:
        name = latest_shot(c)
    else:
        name = args.name
    if args.latest or args.latest_shot:
        print(name, file=sys.stderr)
    if size is None and PHOTO.fullmatch(name):
        try:
            size = c.photo_size(name)
        except DevmonStatus as e:
            if e.status != -7:
                raise
            print(f"warning: {name}: its folder is too large to list", file=sys.stderr)
    data = c.get(name, _progress)
    if len(data) >= PROGRESS_STEP:
        print(file=sys.stderr)
    if size is None:
        print(
            f"warning: {name}: size unchecked (a card read error would end it early)",
            file=sys.stderr,
        )
    elif len(data) != size:
        raise DevmonError(f"{name}: {len(data)} bytes read, the listing says {size}")
    out = args.output or Path(PurePosixPath(name).name)
    out.write_bytes(data)
    print(f"{len(data)} bytes to {out}")


def cmd_ls(args: argparse.Namespace, c: Client) -> None:
    for e in c.list(args.dir):
        print(f"{e.name:12}  {e.size:10}  {e.time()}")


def cmd_del(args: argparse.Namespace, c: Client) -> None:
    c.delete(args.name)
    print(f"C:\\{args.name} deleted")


def cmd_log(args: argparse.Namespace, c: Client) -> None:
    since = 0
    while True:
        since, text = c.log(since)
        sys.stdout.write(text.decode("latin-1"))
        sys.stdout.flush()
        if not args.follow:
            return
        time.sleep(args.interval)


def cmd_poweroff(args: argparse.Namespace, c: Client) -> None:
    c.poweroff()
    print("power-off requested: Wi-Fi stop, then the power switch's Off level")


def run(args: argparse.Namespace) -> None:
    if args.dev_func is cmd_power:
        cmd_power(args)
        return
    if args.dev_func is cmd_wait:
        cmd_wait(args)
        return
    client = _client(args)
    try:
        args.dev_func(args, client)
    finally:
        client.close()


def cmd_key(args: argparse.Namespace, c: Client) -> None:
    c.key(args.name.upper(), args.hold)


def cmd_dial(args: argparse.Namespace, c: Client) -> None:
    name = args.position.upper()
    prev = c.dial(name)
    print(f"mode dial 0x{prev:x} -> {name}")


def cmd_keys(args: argparse.Namespace, c: Client) -> None:
    for step in parse_keys(args.sequence):
        if isinstance(step, int):
            time.sleep(step / 1000)
            continue
        print(f"{step[0]} {step[1]} ms")
        c.key(*step)


def cmd_screen(args: argparse.Namespace, c: Client) -> None:
    t0 = time.monotonic()
    shot = screen.grab(c.read, screen.Symbols.load())
    seconds = time.monotonic() - t0
    skipped = [
        f"L{n} {'hidden' if shot.hidden & 1 << 4 * n else 'off'}"
        for n in range(screen.LAYERS)
        if n not in shot.layers
    ]
    print(
        f"device {shot.device}, OSD {shot.w} x {shot.h}, layers {sorted(shot.layers)}"
        + (f" ({', '.join(skipped)})" if skipped else "")
    )
    print(
        f"{shot.bytes_read} bytes in {seconds:.1f} s "
        f"({shot.bytes_read / 1024 / seconds:.0f} KiB/s), {shot.tries} tries, "
        f"flip state sampled at most {shot.max_gap * 1000:.0f} ms apart"
    )
    if not shot.stable:
        print("warning: the GUI flipped during every try: rows may mix two frames")
    l1 = shot.layers.get(1)
    if l1 and max(l1.pixels) >= 0x80:
        print("warning: L1 uses indices >= 0x80; the display's L1 palette holds 128")
    for path in screen.save(shot, args.output):
        print(path)


def cmd_power(args: argparse.Namespace) -> None:
    power(args.action, args.delay)


def cmd_wait(args: argparse.Namespace) -> None:
    print(
        wait(
            args.host, args.port, args.wait_timeout, iface=args.connect, ssid=args.ssid
        )
    )


def add_parser(commands: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """`fenykl dev`; its subcommands, for more of them (sampler.add_parser)."""
    dev = commands.add_parser(
        "dev",
        help="drive the dev monitor on the camera (sdk/docs/devmon.md)",
        description="Talk to the dev monitor over the camera's access point. Power commands "
        f"come from ${POWER_ENV['on']} and ${POWER_ENV['off']} (shell commands).",
    )
    dev.add_argument("--host", default=HOST)
    dev.add_argument("--port", type=int, default=PORT)
    dev.add_argument(
        "--timeout", type=float, default=10.0, help="seconds per socket operation"
    )
    dev.set_defaults(func=run)
    sub = dev.add_subparsers(required=True, metavar="COMMAND")

    def add(name: str, func, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(dev_func=func)
        return p

    add("ping", cmd_ping, "the monitor's status line")
    p = add("read", cmd_read, "read RAM (image and RAM, either alias)")
    p.add_argument("addr", type=_int)
    p.add_argument("len", type=_int)
    p.add_argument(
        "-o", "--output", type=Path, help="write the bytes here, else a hex dump"
    )
    p.add_argument(
        "--piece", type=_int, default=0, help="send() piece (default 16 KiB)"
    )
    p.add_argument("--pace", type=_int, default=0, help="pause after each piece, ms")
    p.add_argument(
        "--timing", action="store_true", help="also print the camera's timing"
    )
    p = add("write", cmd_write, "write RAM (same ranges as read, never MMIO)")
    p.add_argument("addr", type=_int)
    p.add_argument("hex", nargs="?", help="bytes as hex")
    p.add_argument("-i", "--input", type=Path, help="bytes from this file")
    add("timing", cmd_timing, "the camera's timing of the last read")
    p = add("exec", cmd_exec, "upload and run a test program (fenykl tp build)")
    p.add_argument("image", type=Path)
    p.add_argument("--arg", type=_int, default=0)
    p.add_argument(
        "--run-timeout", type=_int, default=10000, help="ms before a timeout reply"
    )
    p.add_argument("-t", "--target", default="em10ii")
    p = add("put", cmd_put, "write a FENYKL.* file at the card root")
    p.add_argument("file", type=Path)
    p.add_argument(
        "name", nargs="?", help="card name (default: the file's name, upper case)"
    )
    p = add("get", cmd_get, "read a FENYKL.*, SHOTnnnn.PPM or DCIM/<dir>/<file> file")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("name", nargs="?")
    which.add_argument(
        "--latest", action="store_true", help="the newest photo in DCIM (fenykl dev ls)"
    )
    which.add_argument(
        "--latest-shot", action="store_true", help="the last SHOTnnnn.PPM"
    )
    p.add_argument("--ext", choices=PHOTO_EXTS, help="--latest: only this kind")
    p.add_argument("-o", "--output", type=Path)
    p = add("ls", cmd_ls, "list DCIM or a DCIM/<dir> folder: name, size, write time")
    p.add_argument("dir", nargs="?", default="DCIM")
    p = add("del", cmd_del, "delete a FENYKL.* file")
    p.add_argument("name")
    p = add("log", cmd_log, "the monitor's text log")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--interval", type=float, default=1.0)
    add("poweroff", cmd_poweroff, "stop Wi-Fi and switch the camera off")
    p = add("power", cmd_power, "run the power command (on, off, cycle)")
    p.add_argument("action", choices=["on", "off", "cycle"])
    p.add_argument("--delay", type=float, default=3.0, help="cycle: seconds off")
    p = add("wait", cmd_wait, "poll until the monitor answers")
    p.add_argument("--wait-timeout", type=float, default=120.0, help="seconds")
    p.add_argument(
        "--connect",
        metavar="IFACE",
        help="while there is no answer and IFACE is disconnected, ask iwd to join the access "
        "point (iwctl)",
    )
    p.add_argument(
        "--ssid", default=SSID, help=f"access point for --connect (default {SSID})"
    )
    p = add("screen", cmd_screen, "grab the GUI's OSD layers as PNG")
    p.add_argument("-o", "--output", type=Path, required=True, help="composite PNG")
    p = add("key", cmd_key, f"press a camera button ({' '.join(KEYS)})")
    p.add_argument("name")
    p.add_argument("--hold", type=int, default=HOLD_DEFAULT, help="ms (default 100)")
    p = add(
        "dial",
        cmd_dial,
        f"set the mode dial's position until it is turned ({' '.join(DIALS)})",
    )
    p.add_argument("position")
    p = add("keys", cmd_keys, 'a key sequence, e.g. "MENU 300 DOWN 200 OK HALF:1500"')
    p.add_argument("sequence")
    return sub
