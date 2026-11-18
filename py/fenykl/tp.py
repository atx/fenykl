"""`fenykl tp build`: a dev monitor test program (sdk/include/tp.h) linked at one slot of the
target's exec region, as a flat image with its header's CRC filled in (sdk/docs/devmon.md)."""

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .plugin import BuildError, _run
from .sdk import SDK, SdkTarget

TP_DIR = SDK / "tp"
MAGIC = 0x50544B46
API = 1
HEADER = struct.Struct("<8I")
CRC_OFFSET = 24


@dataclass(frozen=True)
class TpHeader:
    magic: int
    api: int
    base: int
    size: int
    mem: int
    entry: int
    crc: int

    @classmethod
    def parse(cls, image: bytes) -> "TpHeader":
        if len(image) < HEADER.size:
            raise BuildError(f"test program of {len(image)} bytes: no header")
        return cls(*HEADER.unpack_from(image)[:7])


def image_crc(image: bytes) -> int:
    return zlib.crc32(image[HEADER.size :])


def check(image: bytes) -> TpHeader:
    """The header of a test program image, checked as the monitor checks it (less the slot)."""
    h = TpHeader.parse(image)
    if h.magic != MAGIC or h.api != API:
        raise BuildError(f"not a test program (magic 0x{h.magic:x}, api {h.api})")
    if h.size != len(image) or h.mem < h.size:
        raise BuildError(f"header size {h.size}/mem {h.mem}, image {len(image)} bytes")
    if not h.base + HEADER.size <= h.entry < h.base + h.size:
        raise BuildError(f"entry 0x{h.entry:x} outside the image at 0x{h.base:x}")
    if h.crc != image_crc(image):
        raise BuildError("test program CRC mismatch")
    return h


def build(sources: list[Path], outdir: Path, target: SdkTarget, slot: int) -> Path:
    """Compile `sources` with tp_crt.c, link them at `slot` with tp.ld, write
    `outdir`/<first source>.bin with the header CRC; also the .elf and .map."""
    if target.tp is None:
        raise BuildError(f"target {target.name} has no exec region (target.toml [tp])")
    base = target.tp.slot(slot)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = sources[0].stem
    elf = outdir / f"{stem}.elf"
    _run(
        [
            target.tool("gcc"),
            *target.cflags,
            f"-I{SDK / 'include'}",
            "-nostdlib",
            "-T",
            str(TP_DIR / "tp.ld"),
            f"-Wl,--defsym=TP_BASE=0x{base:X}",
            f"-Wl,--defsym=TP_SLOT_SIZE=0x{target.tp.slot_size:X}",
            "-Wl,--gc-sections",
            "-Wl,--orphan-handling=error",
            "-Wl,--no-warn-rwx-segments",
            f"-Wl,-Map={elf.with_suffix('.map')}",
            "-o",
            str(elf),
            str(TP_DIR / "tp_crt.c"),
            *map(str, sources),
            "-lgcc",
        ]
    )
    flat = outdir / f"{stem}.raw"
    _run([target.tool("objcopy"), "-O", "binary", "-R", ".bss", str(elf), str(flat)])
    image = bytearray(flat.read_bytes())
    flat.unlink()
    struct.pack_into("<I", image, CRC_OFFSET, image_crc(bytes(image)))
    h = check(bytes(image))
    if h.base != base:
        raise BuildError(f"linked at 0x{h.base:x}, slot {slot} is 0x{base:x}")
    out = outdir / f"{stem}.bin"
    out.write_bytes(image)
    return out


def slot_of(target: SdkTarget, h: TpHeader) -> int:
    """The slot a test program was linked for."""
    if target.tp is None:
        raise BuildError(f"target {target.name} has no exec region (target.toml [tp])")
    slot, rest = divmod(h.base - target.tp.base, target.tp.slot_size)
    if rest or not 0 <= slot < target.tp.slots:
        raise BuildError(f"0x{h.base:x} is no slot of {target.name}")
    return slot
