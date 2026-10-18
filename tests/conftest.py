import hashlib
import subprocess
from collections.abc import Sequence
from functools import cache
from pathlib import Path

import pytest

from fenykl import fwapi, plugin, sdk
from fenykl.cli import read_bin
from fenykl.firmware import TARGETS, StockMissingError


@cache
def stock_image() -> bytes:
    """The stock E-PL3 v1.6 image from firmware/; skips the test when it is absent."""
    target = TARGETS["epl3"]
    try:
        path = target.stock_path()
    except StockMissingError as e:
        pytest.skip(str(e))
    data = read_bin(path)
    digest = hashlib.sha256(data).hexdigest()
    if digest != target.sha256:
        pytest.fail(f"{path} is not the stock image: sha256 {digest}, expected {target.sha256}")
    return data


SIM = Path(__file__).resolve().parent / "sim"
EPL3 = sdk.target("epl3")


def _toolchain_missing() -> str:
    """Why the cross toolchain is unusable, "" when it is there."""
    try:
        EPL3.tool("")
    except sdk.TargetError as e:
        return str(e)
    return ""


TOOLCHAIN_MISSING = _toolchain_missing()
needs_toolchain = pytest.mark.skipif(bool(TOOLCHAIN_MISSING), reason=TOOLCHAIN_MISSING)
MOCK_BODY = 0x6EB00000
# The runtime patches and reads .shared through the uncached alias (addr & 0x3FFFFFFF) | 0x80000000;
# on hardware it aliases the same DRAM, so the simulator maps both to the same cells.
DRAM_ALIAS = ("--memory-alias", "0x6e600000,0xa00000,0xae600000")
# The power-on factor word the runtime reads at startup (arch/epl3/usb.c); zero = not a USB boot.
POWERON = ("--memory-region", "0x60551000,0x1000")


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
    **link,
) -> subprocess.CompletedProcess:
    """Link `sources` with the plugin runtime and run them in the GDB simulator. Entry is _sim_start, `.mock` goes to MOCK_BODY and the `mocked` firmware functions to
    their firmware.toml addresses."""
    funcs = fwapi.load(firmware or EPL3.dir / "firmware.toml").functions
    address = {f.name: f.address for f in funcs}
    placement = [f"-Wl,--section-start=.fw.{n}=0x{address[n]:x}" for n in mocked]
    elf = plugin.link(
        sources,
        workdir,
        EPL3,
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
        [EPL3.tool("run"), *(arg for r in memory for arg in r), str(elf)],
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
