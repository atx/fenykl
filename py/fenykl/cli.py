"""The `fenykl` command line interface."""

import argparse
import hashlib
import lzma
import sys
from collections import Counter
from pathlib import Path

from .assets import AssetError, dump_assets
from .firmware import (
    FIRMWARE_DIR,
    TARGETS,
    BlockHeader,
    FirmwareFormatError,
    StockMissingError,
    identify,
    load_unpacked,
    pack_firmware,
    save_unpacked,
    unpack_firmware,
)
from .patching import PATCHES, PatchError, apply_patches
from .payload import PayloadError, describe, describe_shared, pack_elf
from .plugin import BuildError, build, plugin_targets
from .sdk import TargetError, toolchain_prefix


def read_bin(path: Path) -> bytes:
    """Read a `.BIN`, decompressing it if it is stored as `.BIN.lzma`."""
    data = path.read_bytes()
    return lzma.decompress(data) if path.suffix == ".lzma" else data


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    print(
        f"wrote {path} ({len(data)} bytes, sha256 {hashlib.sha256(data).hexdigest()})"
    )


def _describe_header(header: BlockHeader) -> str:
    text = (
        f"{header.kind:<13} sig={header.type_sig.decode('latin-1')} "
        f"model={header.model_sig.hex()} addr=0x{header.load_address:08x} "
        f"version=0x{header.version:04x} flags=0x{header.flags:04x}"
    )
    for name in ("unknown1", "unknown2", "unknown3"):
        value = getattr(header, name)
        if any(value):
            text += f" {name}={value.hex()}"
    return text


def cmd_info(args: argparse.Namespace) -> None:
    data = read_bin(args.file)
    firmware = unpack_firmware(data)
    target = identify(data)
    print(f"size    {len(data)}")
    print(f"sha256  {hashlib.sha256(data).hexdigest()}")
    print(f"target  {target.name if target else 'unknown'}")
    for index, block in enumerate(firmware.blocks):
        # unpack_firmware already rejected any checksum mismatch.
        print(
            f"{index:2d} {_describe_header(block.header)} "
            f"length=0x{len(block.body):x} checksum=0x{block.checksum():08x} ok"
        )


def cmd_unpack(args: argparse.Namespace) -> None:
    firmware = unpack_firmware(read_bin(args.file))
    save_unpacked(firmware, args.outdir)
    print(f"unpacked {len(firmware.blocks)} blocks to {args.outdir}")


def cmd_pack(args: argparse.Namespace) -> None:
    _write(args.output, pack_firmware(load_unpacked(args.directory)))


def cmd_patch_list(args: argparse.Namespace) -> None:
    for name, patch in PATCHES.items():
        print(f"{name:<24} {patch.description}")


def cmd_patch_apply(args: argparse.Namespace) -> None:
    unknown = [name for name in args.patches if name not in PATCHES]
    if unknown:
        raise PatchError(f"unknown patches: {', '.join(unknown)}")
    patches = [PATCHES[name] for name in args.patches]
    # apply_patches rejects patches whose target differs from the first one's.
    stock = args.input or patches[0].target.stock_path()
    _write(args.output, apply_patches(read_bin(stock), patches))


def cmd_assets_dump(args: argparse.Namespace) -> None:
    data = read_bin(args.file or TARGETS["epl3"].stock_path())
    assets = dump_assets(data, args.out)
    kinds = Counter(asset.kind for asset in assets)
    fonts = len({asset.font for asset in assets if asset.kind == "glyph"})
    print(
        f"{identify(data).name}: {kinds['icon']} icons, {kinds['image']} images, "
        f"{kinds['glyph']} glyphs in {fonts} glyph sets -> {args.out}"
    )


def cmd_payload_pack(args: argparse.Namespace) -> None:
    elf = args.elf.read_bytes()
    _write(args.output, pack_elf(elf))
    shared = describe_shared(elf)
    if shared:
        print(shared)


def cmd_payload_info(args: argparse.Namespace) -> None:
    elf = args.elf.read_bytes() if args.elf else None
    print(describe(args.file.read_bytes(), elf))


def cmd_plugin_build(args: argparse.Namespace) -> None:
    outroot = args.output or args.directory / "build"
    for target in plugin_targets(args.directory, args.target):
        image = build(args.directory, outroot / target.name, target, args.isa_check)
        print(f"{image}:")
        print(describe(image.read_bytes(), (image.parent / "plugin.elf").read_bytes()))


def cmd_toolchain_path(args: argparse.Namespace) -> None:
    print(toolchain_prefix())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fenykl", description="Unpack, repack and patch Olympus camera firmware."
    )
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    info = commands.add_parser("info", help="show blocks, header fields and checksums")
    info.add_argument("file", type=Path, metavar="FILE.BIN")
    info.set_defaults(func=cmd_info)

    unpack = commands.add_parser(
        "unpack", help="unpack into a directory of block bodies"
    )
    unpack.add_argument("file", type=Path, metavar="FILE.BIN")
    unpack.add_argument("outdir", type=Path, metavar="OUTDIR")
    unpack.set_defaults(func=cmd_unpack)

    pack = commands.add_parser("pack", help="pack an unpacked directory into a .BIN")
    pack.add_argument("directory", type=Path, metavar="DIR")
    pack.add_argument("output", type=Path, metavar="OUT.BIN")
    pack.set_defaults(func=cmd_pack)

    patch = commands.add_parser("patch", help="list or apply patches")
    patch_commands = patch.add_subparsers(required=True, metavar="COMMAND")
    patch_list = patch_commands.add_parser("list", help="list available patches")
    patch_list.set_defaults(func=cmd_patch_list)
    apply = patch_commands.add_parser("apply", help="apply patches to a stock firmware")
    apply.add_argument("patches", nargs="+", metavar="PATCH")
    apply.add_argument(
        "--input",
        type=Path,
        metavar="STOCK.BIN",
        help="stock firmware (default: the patch target's image in firmware/)",
    )
    apply.add_argument("--output", type=Path, required=True, metavar="OUT.BIN")
    apply.set_defaults(func=cmd_patch_apply)

    assets = commands.add_parser(
        "assets",
        help="dump the built-in fonts, icons and GUI images of a stock firmware",
    )
    assets_commands = assets.add_subparsers(required=True, metavar="COMMAND")
    assets_dump = assets_commands.add_parser(
        "dump",
        help="write every icon, image and font glyph as an indexed PNG plus index.csv",
        description="Write every icon, image and font glyph of a stock firmware as an "
        "indexed PNG, plus index.csv. The output is copyrighted firmware content: "
        "never commit it.",
    )
    assets_dump.add_argument(
        "file",
        nargs="?",
        type=Path,
        metavar="FILE.BIN",
        help="stock firmware (default: the E-PL3 v1.6 image in firmware/)",
    )
    assets_dump.add_argument(
        "-o",
        "--out",
        type=Path,
        default=FIRMWARE_DIR.parent / "out" / "assets",
        metavar="DIR",
        help="output directory (default: the gitignored out/assets of the fenykl "
        "checkout); copyrighted content, never commit it",
    )
    assets_dump.set_defaults(func=cmd_assets_dump)

    payload = commands.add_parser(
        "payload", help="pack or inspect FENYKL.BIN payload images"
    )
    payload_commands = payload.add_subparsers(required=True, metavar="COMMAND")
    payload_pack = payload_commands.add_parser(
        "pack", help="payload ELF (linked with a target's link.ld) -> FENYKL.BIN"
    )
    payload_pack.add_argument("elf", type=Path, metavar="PAYLOAD.ELF")
    payload_pack.add_argument("output", type=Path, metavar="FENYKL.BIN")
    payload_pack.set_defaults(func=cmd_payload_pack)
    payload_info = payload_commands.add_parser(
        "info", help="show the header and whether the loader accepts the image"
    )
    payload_info.add_argument("file", type=Path, metavar="FENYKL.BIN")
    payload_info.add_argument(
        "--elf",
        type=Path,
        metavar="PLUGIN.ELF",
        help="its ELF (build/<target>/plugin.elf): also show the .shared placement",
    )
    payload_info.set_defaults(func=cmd_payload_info)

    plugin = commands.add_parser("plugin", help="build C plugins")
    plugin_commands = plugin.add_subparsers(required=True, metavar="COMMAND")
    plugin_build = plugin_commands.add_parser(
        "build",
        help="compile, link and pack a plugin directory into FENYKL.BIN",
    )
    plugin_build.add_argument("directory", type=Path, metavar="DIR")
    plugin_build.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="OUTDIR",
        help="build directory (default DIR/build); each target builds into OUTDIR/TARGET",
    )
    plugin_build.add_argument(
        "-t",
        "--target",
        metavar="TARGET",
        help="build only this camera (default: every target in DIR/plugin.toml)",
    )
    plugin_build.add_argument(
        "--isa-check",
        action="store_true",
        help="also run the target's ISA check on plugin.elf (epl3: needs Ghidra, toolchain/README.md)",
    )
    plugin_build.set_defaults(func=cmd_plugin_build)

    toolchain = commands.add_parser("toolchain", help="locate the cross toolchain")
    toolchain_commands = toolchain.add_subparsers(required=True, metavar="COMMAND")
    toolchain_path = toolchain_commands.add_parser(
        "path",
        help="print the toolchain prefix: $FENYKL_TOOLCHAIN, else toolchain/install/ (need not exist)",
    )
    toolchain_path.set_defaults(func=cmd_toolchain_path)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (
        FirmwareFormatError,
        PatchError,
        PayloadError,
        BuildError,
        StockMissingError,
        TargetError,
        AssetError,
    ) as error:
        sys.exit(f"error: {error}")
