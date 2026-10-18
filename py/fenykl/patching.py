"""Patches of stock firmware: byte edits on unscrambled block bodies, and the patches themselves.

A patch targets one exact stock firmware and is a list of `ByteEdit` entries, each verified against the
sha256 of the stock bytes it overwrites before it is applied.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .firmware import (
    TARGETS,
    Block,
    Firmware,
    Target,
    pack_firmware,
    unpack_firmware,
)
from .sdk import ARCH


class PatchError(ValueError):
    pass


@dataclass(frozen=True)
class ByteEdit:
    block: str
    """Block kind, e.g. "main-code-01"."""
    offset: int
    """Offset into the unscrambled block body."""
    original_sha256: str
    """sha256 (hex) of the stock bytes that `replacement` overwrites."""
    replacement: bytes

    def __post_init__(self) -> None:
        if not self.replacement:
            raise ValueError("empty edit")
        if self.offset < 0:
            raise ValueError(f"negative offset {self.offset}")

    @property
    def end(self) -> int:
        return self.offset + len(self.replacement)

    def locate(self, firmware: Firmware) -> int:
        """Return the block index, after checking that the original bytes are there."""
        index = firmware.find_block(self.block)
        body = firmware.blocks[index].body
        where = f"block {self.block!r} offset 0x{self.offset:x}"
        if self.end > len(body):
            raise PatchError(f"{where}: edit ends past the block end 0x{len(body):x}")
        digest = hashlib.sha256(body[self.offset : self.end]).hexdigest()
        if digest != self.original_sha256:
            raise PatchError(
                f"{where}: expected original sha256 {self.original_sha256}, found {digest}"
            )
        return index


@dataclass(frozen=True)
class Patch:
    name: str
    description: str
    target: Target
    edits: tuple[ByteEdit, ...]


def apply_patches(stock: bytes, patches: list[Patch]) -> bytes:
    """Apply `patches` to the stock `.BIN` contents and return the repacked `.BIN`."""
    if not patches:
        raise PatchError("no patches given")
    digest = hashlib.sha256(stock).hexdigest()
    for patch in patches:
        if digest != patch.target.sha256:
            raise PatchError(
                f"patch {patch.name} is for {patch.target.name} "
                f"(sha256 {patch.target.sha256}), input has sha256 {digest}"
            )

    firmware = unpack_firmware(stock)
    edits = sorted(
        [
            (edit.locate(firmware), edit, patch.name)
            for patch in patches
            for edit in patch.edits
        ],
        key=lambda item: (item[0], item[1].offset),
    )
    for (block_a, a, name_a), (block_b, b, name_b) in pairwise(edits):
        if block_a == block_b and a.end > b.offset:
            raise PatchError(
                f"overlapping edits in block {block_a}: {name_a} "
                f"0x{a.offset:x}..0x{a.end:x} and {name_b} 0x{b.offset:x}..0x{b.end:x}"
            )

    bodies = [bytearray(block.body) for block in firmware.blocks]
    for index, edit, _ in edits:
        bodies[index][edit.offset : edit.end] = edit.replacement
    patched = Firmware(
        [
            Block(block.header, bytes(body))
            for block, body in zip(firmware.blocks, bodies)
        ]
    )

    output = pack_firmware(patched)
    # unpack_firmware verifies all block checksums.
    if len(output) != len(stock) or unpack_firmware(output).blocks != patched.blocks:
        raise PatchError("repacked firmware does not match the patched blocks")
    return output


# The FENYKL.BIN boot loader of each camera: sdk/arch/<name>/loader/patch.toml,
# registered as "<name>/fenykl-loader".

LOADER = "fenykl-loader"


def _load_loader(path: Path) -> Patch:
    target = TARGETS[path.parent.parent.name]
    toml = tomllib.loads(path.read_text())
    edits = tuple(
        ByteEdit(
            edit["block"],
            edit["address"] - target.load_addresses[edit["block"]],
            edit["original_sha256"],
            bytes.fromhex(edit["replacement"]),
        )
        for edit in toml["edit"]
    )
    return Patch(LOADER, toml["description"], target, edits)


PATCHES = {
    f"{patch.target.name}/{patch.name}": patch
    for patch in map(_load_loader, sorted(ARCH.glob("*/loader/patch.toml")))
}
