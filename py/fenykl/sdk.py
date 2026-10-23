"""Cameras the plugin SDK builds for: one directory per camera in sdk/arch/ (sdk/README.md)."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

FENYKL_ROOT = Path(__file__).resolve().parents[2]
SDK = FENYKL_ROOT / "sdk"
ARCH = SDK / "arch"
TOOLCHAIN_INSTALL = FENYKL_ROOT / "toolchain" / "install"


class TargetError(Exception):
    pass


def toolchain_prefix() -> Path:
    """The cross toolchain prefix: $FENYKL_TOOLCHAIN, else toolchain/install/ (toolchain/build.sh
    installs there). It need not exist yet."""
    env = os.environ.get("FENYKL_TOOLCHAIN")
    return Path(env).absolute() if env else TOOLCHAIN_INSTALL


@dataclass(frozen=True)
class SdkTarget:
    """sdk/arch/<name>/: target.toml, firmware.toml, link.ld, crt0.S, fenykl_target.h, loader/."""

    name: str
    dir: Path
    model: str
    """Camera model name, the FENYKL.BIN header's model field (payload.py)."""
    toolchain: str
    cflags: list[str]
    isa_check: Path | None
    elf_machine: int
    load: int
    max_size: int
    sdk_hooks: tuple[str, ...]
    """Hooks the SDK library binds itself, generated for every plugin (sdk/README.md, Hooks)."""
    sdk_slots: tuple[str, ...]
    """Slots the SDK library binds itself, generated for every plugin (sdk/README.md, Callbacks
    and slots)."""
    sdk_cpu1_unproven: tuple[str, ...]
    """The SDK's acknowledgement of its hooks and slots whose CPU1 scope is unproven."""
    capabilities: tuple[str, ...]
    """SDK features this camera supports; each becomes FENYKL_HAS_<NAME> (sdk/README.md, Layout)."""

    @property
    def region_end(self) -> int:
        """End of the payload incl. .bss."""
        return self.load + self.max_size

    def tool(self, name: str) -> str:
        """Path of toolchain program `name`; "" gives the prefix of all of them."""
        bindir = toolchain_prefix() / "bin"
        gcc = bindir / f"{self.toolchain}-gcc"
        if not gcc.is_file():
            source = (
                "FENYKL_TOOLCHAIN"
                if os.environ.get("FENYKL_TOOLCHAIN")
                else "the default, FENYKL_TOOLCHAIN is not set"
            )
            raise TargetError(
                f"no {self.toolchain} toolchain: {gcc} missing (prefix from {source}); "
                "build it with toolchain/build.sh or set FENYKL_TOOLCHAIN to its prefix"
            )
        return str(bindir / f"{self.toolchain}-{name}")


def target(name: str) -> SdkTarget:
    path = ARCH / name / "target.toml"
    if not path.is_file():
        raise TargetError(f"unknown target {name!r} (no {path})")
    t = tomllib.loads(path.read_text())
    isa_check = t.get("isa_check")
    return SdkTarget(
        name=name,
        dir=path.parent,
        model=t["model"],
        toolchain=t["toolchain"],
        cflags=(FENYKL_ROOT / t["cflags"]).read_text().split(),
        isa_check=FENYKL_ROOT / isa_check if isa_check else None,
        elf_machine=t["elf_machine"],
        load=t["load"],
        max_size=t["max_size"],
        sdk_hooks=tuple(t.get("sdk_hooks", [])),
        sdk_slots=tuple(t.get("sdk_slots", [])),
        sdk_cpu1_unproven=tuple(t.get("sdk_cpu1_unproven", [])),
        capabilities=tuple(t["capabilities"]),
    )


def all_targets() -> list[SdkTarget]:
    return [target(p.parent.name) for p in sorted(ARCH.glob("*/target.toml"))]


def target_at(load: int) -> SdkTarget | None:
    """The target whose loader reads FENYKL.BIN to `load`."""
    matches = [t for t in all_targets() if t.load == load]
    if len(matches) > 1:
        raise TargetError(
            f"targets {', '.join(t.name for t in matches)} share load address 0x{load:08x}"
        )
    return matches[0] if matches else None
