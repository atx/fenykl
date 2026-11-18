import hashlib
import socket
import subprocess
import threading
from collections.abc import Sequence
from functools import cache
from pathlib import Path

import pytest

from fenykl import dev, fwapi, plugin, sdk
from fenykl.cli import read_bin
from fenykl.firmware import TARGETS, StockMissingError


@cache
def stock_image(name: str) -> bytes:
    """The stock image of target `name` from firmware/; skips the test when it is absent."""
    target = TARGETS[name]
    try:
        path = target.stock_path()
    except StockMissingError as e:
        pytest.skip(str(e))
    data = read_bin(path)
    digest = hashlib.sha256(data).hexdigest()
    if digest != target.sha256:
        pytest.fail(
            f"{path} is not the stock image: sha256 {digest}, expected {target.sha256}"
        )
    return data


SIM = Path(__file__).resolve().parent / "sim"
EM10II = sdk.target("em10ii")


def _toolchain_missing() -> str:
    """Why the cross toolchain is unusable, "" when it is there."""
    try:
        EM10II.tool("")
    except sdk.TargetError as e:
        return str(e)
    return ""


TOOLCHAIN_MISSING = _toolchain_missing()
needs_toolchain = pytest.mark.skipif(bool(TOOLCHAIN_MISSING), reason=TOOLCHAIN_MISSING)
MOCK_BODY = 0x6EB00000


def region(spec: str) -> tuple[str, str]:
    return ("--memory-region", spec)


def run_in_simulator(
    workdir: Path,
    name: str,
    sources: list[Path],
    memory: list[tuple[str, str]],
    mocked: Sequence[str] = (),
    ldflags: Sequence[str] = (),
    firmware: Path | None = None,
    timeout: int = 60,
    target: sdk.SdkTarget = EM10II,
    **link,
) -> subprocess.CompletedProcess:
    """Link `sources` with the plugin runtime of `target` and run them in the GDB simulator. Entry is _sim_start, `.mock` goes to MOCK_BODY and the `mocked` firmware functions to
    their firmware.toml addresses."""
    funcs = fwapi.load(firmware or target.dir / "firmware.toml").functions
    address = {f.name: f.address for f in funcs}
    placement = [f"-Wl,--section-start=.fw.{n}=0x{address[n]:x}" for n in mocked]
    elf = plugin.link(
        sources,
        workdir,
        target,
        name=name,
        firmware=firmware,
        extra_ldflags=(
            "-Wl,-e,_sim_start",
            "-Wl,--no-gc-sections",
            "-Wl,--orphan-handling=place",
            f"-Wl,--section-start=.mock=0x{MOCK_BODY:x}",
            *placement,
            *ldflags,
        ),
        **link,
    )
    return subprocess.run(
        [target.tool("run"), *(arg for r in memory for arg in r), str(elf)],
        check=False,
        capture_output=True,
        timeout=timeout,
    )


def assert_exit(result: subprocess.CompletedProcess, expected: int, see: str) -> None:
    out = result.stdout.decode("latin-1")
    assert result.returncode == expected, (
        f"exit status {result.returncode} (see {see})\n{out[-3000:]}"
        f"{result.stderr.decode('latin-1')}"
    )


# The `fenykl dev` client against a fake camera on a local socket pair.
class FakeCamera:
    """Answers framed requests on one end of a socket pair: `handler(op, payload)` returns
    (status, reply payload); every request is kept in `requests`."""

    def __init__(self, sock: socket.socket, handler):
        self.sock = sock
        self.handler = handler
        self.requests: list[tuple[int, bytes]] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _recv(self, n: int) -> bytes | None:
        data = b""
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _serve(self) -> None:
        while True:
            head = self._recv(dev.HEADER.size)
            if head is None:
                return
            magic, op, _, rid, n = dev.HEADER.unpack(head)
            assert magic == dev.REQ_MAGIC
            payload = self._recv(n) if n else b""
            self.requests.append((op, payload))
            status, reply = self.handler(op, payload)
            try:
                self.sock.sendall(
                    dev.HEADER.pack(dev.REP_MAGIC, op, status, rid, len(reply)) + reply
                )
            except OSError:
                return  # the test closed its end first


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    a.settimeout(2)
    yield dev.Client(a), b
    a.close()
    b.close()
