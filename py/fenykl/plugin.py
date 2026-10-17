"""`fenykl plugin build`: C plugin directory -> plugin.elf -> optional ISA check -> FENYKL.BIN."""

import os
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

from . import fwapi
from .payload import pack_elf
from .sdk import SDK, SdkTarget, target

# The SDK library: runtime, logging, configuration, HUD. Every plugin links all of it; --gc-sections drops
# what the plugin does not use.
LIB = SDK / "lib"
# Keeps GCC from turning the runtime's copy/clear loops into calls to memcpy/memset themselves.
RUNTIME_CFLAGS = ["-fno-tree-loop-distribute-patterns"]


class BuildError(Exception):
    pass


def _plugin_toml(plugin_dir: Path) -> dict:
    path = plugin_dir / "plugin.toml"
    if not path.is_file():
        raise BuildError(
            f'{path} missing (it lists the cameras, e.g. targets = ["epl3"])'
        )
    return tomllib.loads(path.read_text())


def plugin_targets(plugin_dir: Path, only: str | None = None) -> list[SdkTarget]:
    """The targets `plugin_dir`/plugin.toml lists, or just `only` if it is one of them."""
    names = _plugin_toml(plugin_dir)["targets"]
    if only is None:
        return [target(n) for n in names]
    if only not in names:
        raise BuildError(
            f"{plugin_dir} is not for {only} (plugin.toml: {', '.join(names)})"
        )
    return [target(only)]


def _run(cmd: list[str], env: dict[str, str] | None = None) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise BuildError(f"{' '.join(cmd)}\n{result.stdout}{result.stderr}")
    return result.stdout


def link(
    sources: list[Path],
    outdir: Path,
    target: SdkTarget,
    name: str = "plugin",
    extra_ldflags: tuple[str, ...] = (),
    hooks: Sequence[str] = (),
    cpu1_unproven: Sequence[str] = (),
    firmware: Path | None = None,
    slots: Sequence[str] = (),
    vtables: Sequence[str] = (),
    cpu1_sources: Sequence[Path] = (),
    run_on_usb: bool = False,
) -> Path:
    """Compile the plugin sources with the runtime, the firmware thunks, the stubs of `hooks` and of
    the target's `sdk_hooks` and the descriptors of `slots` and `vtables` and link them. Sources in
    `cpu1_sources` are built with FENYKL_CPU1 (CPU1 code: CPU0-only firmware calls do not
    compile). The sdk/lib files get FENYKL_PLUGIN_CPU1 = 1 if the plugin has `cpu1_sources`, else
    0. `firmware` replaces the target's firmware.toml (simulator tests).
    `run_on_usb` lets the runtime call plugin_main on a boot started by the USB cable."""
    gen = outdir / "gen"
    try:
        fwapi.generate(
            firmware or target.dir / "firmware.toml",
            gen,
            target.tool("objdump"),
            hooks,
            cpu1_unproven,
            slots,
            vtables,
            target.sdk_hooks,
        )
    except fwapi.ApiError as e:
        raise BuildError(str(e)) from e
    objdir = outdir / "obj"
    objdir.mkdir(parents=True, exist_ok=True)
    flags = [
        *target.cflags,
        f"-I{SDK / 'include'}",
        f"-I{target.dir}",
        f"-I{gen}",
        f"-DFENYKL_LOAD_BASE=0x{target.load:X}u",
        f"-DFENYKL_REGION_END=0x{target.region_end:X}u",
        f"-DFENYKL_RUN_ON_USB={1 if run_on_usb else 0}",
    ]
    lib_flags = [*RUNTIME_CFLAGS, f"-DFENYKL_PLUGIN_CPU1={1 if cpu1_sources else 0}"]
    lib_sources = sorted(LIB.glob("*.c"))
    runtime = [target.dir / "crt0.S", *sorted(target.dir.glob("*.c"))]
    objects = []
    for src in [*runtime, *lib_sources, gen / "fw_thunks.S", gen / "fw_hooks.S", *sources]:
        obj = objdir / f"{src.parent.name}_{src.name}.o"
        extra = RUNTIME_CFLAGS if src in runtime else []
        if src in lib_sources:
            extra = lib_flags
        if src in cpu1_sources:
            extra = [*extra, "-DFENYKL_CPU1"]
        _run([target.tool("gcc"), *flags, *extra, "-c", str(src), "-o", str(obj)])
        objects.append(obj)
    elf = outdir / f"{name}.elf"
    _run(
        [
            target.tool("gcc"),
            *target.cflags,
            "-nostdlib",
            "-T",
            str(target.dir / "link.ld"),
            "-Wl,--gc-sections",
            "-Wl,--orphan-handling=error",
            "-Wl,--no-warn-rwx-segments",
            f"-Wl,-Map={elf.with_suffix('.map')}",
            *extra_ldflags,
            "-o",
            str(elf),
            *map(str, objects),
            str(gen / "fw_symbols.ld"),
            "-lgcc",
        ]
    )
    return elf


def check_isa(elf: Path, workdir: Path, target: SdkTarget) -> str:
    """Run the target's ISA check (toolchain/isacheck/check_isa.py, needs Ghidra) and return its
    report; fails on any ISA error."""
    if target.isa_check is None:
        raise BuildError(f"target {target.name} has no ISA check (target.toml isa_check)")
    env = dict(os.environ, MN103_BINUTILS=target.tool(""))
    return _run(
        [
            str(target.isa_check),
            "--workdir",
            str(workdir),
            str(elf),
        ],
        env,
    )


def build(
    plugin_dir: Path, outdir: Path, target: SdkTarget, isa_check: bool = False
) -> Path:
    """Build the .c and .S files of `plugin_dir` for `target` into `outdir`/FENYKL.BIN (plus plugin.elf/.map,
    gen/, obj/); `isa_check` also runs the target's ISA check on plugin.elf."""
    sources = sorted([*plugin_dir.glob("*.c"), *plugin_dir.glob("*.S")])
    if not sources:
        raise BuildError(f"no .c or .S files in {plugin_dir}")
    config = _plugin_toml(plugin_dir)
    cpu1_sources = [plugin_dir / n for n in config.get("cpu1_sources", [])]
    unknown = [p.name for p in cpu1_sources if p not in sources]
    if unknown:
        raise BuildError(
            f"cpu1_sources lists {', '.join(unknown)}, not a .c or .S file in {plugin_dir}"
        )
    run_on_usb = config.get("run_on_usb", False)
    if not isinstance(run_on_usb, bool):
        raise BuildError(
            f"plugin.toml: run_on_usb must be true or false, not {run_on_usb!r}"
        )
    image = outdir / "FENYKL.BIN"
    image.unlink(missing_ok=True)
    elf = link(
        sources,
        outdir,
        target,
        hooks=config.get("hooks", []),
        cpu1_unproven=config.get("cpu1_unproven", []),
        slots=config.get("slots", []),
        vtables=config.get("vtables", []),
        cpu1_sources=cpu1_sources,
        run_on_usb=run_on_usb,
    )
    if isa_check:
        print(check_isa(elf, outdir / "isacheck", target), end="")
    image.write_bytes(pack_elf(elf.read_bytes()))
    return image
