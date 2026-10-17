"""Regular Olympus firmware `.BIN` files: blocks, scrambling, checksums, (un)packing, and the known
stock images.

Every block is `<head><body><tail>`. Head and tail are identical 32 byte structures, except that the
head checksum is zero and the tail carries the block checksum. Multi-byte integers are
little-endian. Block header layout::

    | Off | Size | Field                                                  |
    |-----|------|--------------------------------------------------------|
    | 00  | 2    | block type signature, e.g. b"OE"                       |
    | 02  | 2    | model signature, e.g. 08 60 for the E-PL3              |
    | 04  | 4    | unknown1, normally zero (0/1/2 on some lens firmwares) |
    | 08  | 4    | load address                                           |
    | 0C  | 4    | body length                                            |
    | 10  | 2    | version, BCD (0x1600 = v1.6.0.0)                       |
    | 12  | 2    | unknown2, normally zero                                |
    | 14  | 2    | flags (content type, scrambling)                       |
    | 16  | 6    | unknown3, zero on old firmwares, non-zero on e.g. E-M1 |
    | 1C  | 4    | checksum                                               |

The checksum is the negated 32-bit wrapping sum of the little-endian u32 words of head +
unscrambled body + tail, with the tail checksum field excluded. Scrambling is not encryption: every
body byte is XORed with 0xFF and the bytes of each 16 byte chunk are permuted.
"""

import hashlib
import json
import struct
import sys
import tomllib
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from .sdk import TARGETS as SDK_TARGETS

HEAD_TAIL_SIZE = 32
_STRUCT = struct.Struct("<2s2s4sII H2sH6sI")

BLOCK_TYPES = {
    b"OE": "main",
    b"OM": "ibis",
    b"OA": "lang",
    b"OL": "lens",
}

FLAGS_CODE_UNSCRAMBLED = 0x0000
# File name suffix per block flags value.
FLAGS_NAMES = {
    FLAGS_CODE_UNSCRAMBLED: "code-uu",
    0x0100: "code-01",
    0x0101: "lang",
    0x0102: "imgs-01",
    0x0103: "snds",
    0x0104: "imgs-02",
    0x0105: "code-02",
    0x0106: "b106",
}

# SCRAMBLE_ORDER[i] is the plain byte index stored at scrambled index i.
SCRAMBLE_ORDER = (2, 0, 3, 1, 5, 7, 4, 6, 11, 10, 9, 8, 14, 12, 13, 15)
UNSCRAMBLE_ORDER = tuple(SCRAMBLE_ORDER.index(i) for i in range(16))
_XOR_FF = bytes(b ^ 0xFF for b in range(256))

FIRMWARE_DIR = Path(__file__).resolve().parents[2] / "firmware"
MANIFEST_NAME = "manifest.json"
MANIFEST_FORMAT = 1


class FirmwareFormatError(ValueError):
    pass


class StockMissingError(Exception):
    pass


def _permute(data: bytes, order: tuple[int, ...]) -> bytes:
    if len(data) % 16 != 0:
        raise ValueError(f"data length {len(data)} is not a multiple of 16")
    out = bytearray(len(data))
    for dst, src in enumerate(order):
        out[dst::16] = data[src::16]
    return bytes(out).translate(_XOR_FF)


def scramble(data: bytes) -> bytes:
    return _permute(data, SCRAMBLE_ORDER)


def unscramble(data: bytes) -> bytes:
    return _permute(data, UNSCRAMBLE_ORDER)


def compute_checksum(data: bytes) -> int:
    if len(data) % 4 != 0:
        raise ValueError(f"data length {len(data)} is not a multiple of 4")
    words = array("I", data)
    if sys.byteorder == "big":
        words.byteswap()
    return -sum(words) & 0xFFFFFFFF


@dataclass(frozen=True)
class BlockHeader:
    """Head/tail fields of a block, excluding the derived body length and checksum."""

    type_sig: bytes
    model_sig: bytes
    load_address: int
    version: int
    flags: int
    unknown1: bytes = bytes(4)
    unknown2: bytes = bytes(2)
    unknown3: bytes = bytes(6)

    def __post_init__(self) -> None:
        for name, size in (
            ("type_sig", 2),
            ("model_sig", 2),
            ("unknown1", 4),
            ("unknown2", 2),
            ("unknown3", 6),
        ):
            value = getattr(self, name)
            if len(value) != size:
                raise ValueError(f"{name} must be {size} bytes, got {len(value)}")

    @property
    def scrambled(self) -> bool:
        # Possibly should be `flags & 0x0100`.
        return self.flags != FLAGS_CODE_UNSCRAMBLED

    @property
    def kind(self) -> str:
        """Short name like "main-code-01"."""
        block = BLOCK_TYPES.get(self.type_sig, "unkn")
        return f"{block}-{FLAGS_NAMES.get(self.flags, 'unkn')}"

    def encode(self, length: int, checksum: int) -> bytes:
        """The 32 byte head (checksum 0) or tail of a block with a body of `length` bytes."""
        return _STRUCT.pack(
            self.type_sig,
            self.model_sig,
            self.unknown1,
            self.load_address,
            length,
            self.version,
            self.unknown2,
            self.flags,
            self.unknown3,
            checksum,
        )

    @classmethod
    def decode(cls, raw: bytes) -> tuple["BlockHeader", int, int]:
        """Return (header, body length, checksum) of a 32 byte head or tail."""
        (
            type_sig,
            model_sig,
            unknown1,
            load_address,
            length,
            version,
            unknown2,
            flags,
            unknown3,
            checksum,
        ) = _STRUCT.unpack(raw)
        header = cls(
            type_sig=type_sig,
            model_sig=model_sig,
            load_address=load_address,
            version=version,
            flags=flags,
            unknown1=unknown1,
            unknown2=unknown2,
            unknown3=unknown3,
        )
        return header, length, checksum


@dataclass
class Block:
    header: BlockHeader
    body: bytes
    """Unscrambled block body."""

    def checksum(self) -> int:
        head = self.header.encode(len(self.body), 0)
        return compute_checksum(head + self.body + head[:-4])

    def pack(self) -> bytes:
        """Return head + (scrambled) body + tail with a freshly computed checksum."""
        head = self.header.encode(len(self.body), 0)
        tail = self.header.encode(len(self.body), self.checksum())
        body = scramble(self.body) if self.header.scrambled else self.body
        return head + body + tail


@dataclass
class Firmware:
    blocks: list[Block] = field(default_factory=list)

    def find_block(self, kind: str) -> int:
        """Index of the one block of `kind`, e.g. "main-code-01"."""
        matches = [i for i, b in enumerate(self.blocks) if b.header.kind == kind]
        if len(matches) != 1:
            raise FirmwareFormatError(
                f"block kind {kind!r} matches {len(matches)} blocks"
            )
        return matches[0]


def _unpack_block(data: bytes, offset: int) -> Block:
    head_raw = data[offset : offset + HEAD_TAIL_SIZE]
    if len(head_raw) != HEAD_TAIL_SIZE:
        raise FirmwareFormatError(f"truncated block head at 0x{offset:x}")
    header, length, head_checksum = BlockHeader.decode(head_raw)
    if head_checksum != 0:
        raise FirmwareFormatError(
            f"block head at 0x{offset:x} has non-zero checksum 0x{head_checksum:08x}"
        )

    body_offset = offset + HEAD_TAIL_SIZE
    tail_offset = body_offset + length
    tail_raw = data[tail_offset : tail_offset + HEAD_TAIL_SIZE]
    if len(tail_raw) != HEAD_TAIL_SIZE:
        raise FirmwareFormatError(
            f"block at 0x{offset:x} with body length 0x{length:x} exceeds file size"
        )
    if tail_raw[:-4] != head_raw[:-4]:
        raise FirmwareFormatError(f"block head and tail at 0x{offset:x} differ")

    body = data[body_offset:tail_offset]
    if header.scrambled:
        body = unscramble(body)

    _, _, stored = BlockHeader.decode(tail_raw)
    computed = compute_checksum(head_raw + body + tail_raw[:-4])
    if computed != stored:
        raise FirmwareFormatError(
            f"block at 0x{offset:x}: checksum 0x{stored:08x} stored, "
            f"0x{computed:08x} computed"
        )
    return Block(header, body)


def unpack_firmware(data: bytes) -> Firmware:
    """Split a regular `.BIN` into blocks with unscrambled bodies, verifying checksums."""
    firmware = Firmware()
    offset = 0
    while offset < len(data):
        block = _unpack_block(data, offset)
        firmware.blocks.append(block)
        offset += 2 * HEAD_TAIL_SIZE + len(block.body)
    return firmware


def pack_firmware(firmware: Firmware) -> bytes:
    return b"".join(block.pack() for block in firmware.blocks)


# On-disk form of an unpacked firmware: a directory with one unscrambled body file per block (e.g.
# `00.main-code-01.bin`) and `manifest.json` with every head
# field needed for a byte-exact repack. Body length and checksum are derived when packing.


def save_unpacked(firmware: Firmware, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, block in enumerate(firmware.blocks):
        file = f"{index:02d}.{block.header.kind}.bin"
        (directory / file).write_bytes(block.body)
        header = block.header
        entries.append(
            {
                "file": file,
                "type_sig": header.type_sig.hex(),
                "model_sig": header.model_sig.hex(),
                "load_address": f"0x{header.load_address:08x}",
                "version": f"0x{header.version:04x}",
                "flags": f"0x{header.flags:04x}",
                "unknown1": header.unknown1.hex(),
                "unknown2": header.unknown2.hex(),
                "unknown3": header.unknown3.hex(),
            }
        )
    manifest = {"format": MANIFEST_FORMAT, "blocks": entries}
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")


def load_unpacked(directory: Path) -> Firmware:
    manifest = json.loads((directory / MANIFEST_NAME).read_text())
    if manifest["format"] != MANIFEST_FORMAT:
        raise FirmwareFormatError(f"unsupported manifest format {manifest['format']!r}")
    blocks = []
    for entry in manifest["blocks"]:
        header = BlockHeader(
            type_sig=bytes.fromhex(entry["type_sig"]),
            model_sig=bytes.fromhex(entry["model_sig"]),
            load_address=int(entry["load_address"], 16),
            version=int(entry["version"], 16),
            flags=int(entry["flags"], 16),
            unknown1=bytes.fromhex(entry["unknown1"]),
            unknown2=bytes.fromhex(entry["unknown2"]),
            unknown3=bytes.fromhex(entry["unknown3"]),
        )
        blocks.append(Block(header, (directory / entry["file"]).read_bytes()))
    return Firmware(blocks)


@dataclass(frozen=True)
class Target:
    """One exact stock firmware `.BIN` (decompressed), identified by its hash.

    Its identity (model, version, file name, sha256, size) is the `[stock]` table of the camera's
    `sdk/targets/<name>/target.toml`; the user drops the image itself into `firmware/`.
    """

    name: str
    model: str
    version: str
    sha256: str
    size: int
    file: str
    """Official `.BIN` file name, e.g. OLY_E_086_1600_0000_0000.BIN."""
    load_addresses: dict[str, int] = field(default_factory=dict)
    """Real load address per block kind, once known. Header addresses are not trusted."""

    def stock_path(self) -> Path:
        """The stock image in `firmware/`, as `.BIN` or `.BIN.lzma`."""
        for path in (FIRMWARE_DIR / self.file, FIRMWARE_DIR / f"{self.file}.lzma"):
            if path.is_file():
                return path
        raise StockMissingError(
            f"stock firmware for {self.name} ({self.model} v{self.version}) not found: "
            f"put {self.file} (or {self.file}.lzma) into {FIRMWARE_DIR}/, "
            f"expected sha256 of the .BIN {self.sha256} (see README.md)"
        )


def _load_target(path: Path) -> Target:
    toml = tomllib.loads(path.read_text())
    return Target(name=path.parent.name, model=toml["model"], **toml["stock"])


TARGETS = {
    target.name: target
    for target in map(_load_target, sorted(SDK_TARGETS.glob("*/target.toml")))
}


def identify(data: bytes) -> Target | None:
    digest = hashlib.sha256(data).hexdigest()
    return next((t for t in TARGETS.values() if t.sha256 == digest), None)
