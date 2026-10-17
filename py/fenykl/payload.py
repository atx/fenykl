"""FENYKL.BIN payload images.

```
0x00 u32     magic  0x324C4E46 ("FNL2")
0x04 u32     load   must equal the loader's address (target.toml `load`)
0x08 u32     size   whole image incl. header, multiple of 4, 0x1C <= size <= target.toml `max_size`
0x0C u32     entry  offset of the entry point from load, 0x1C <= entry < size
0x10 u32     sum    sum (mod 2^32) of the little-endian words in [0x1C, size)
0x14 char[8] model  camera model, ASCII zero-padded (target.toml `model`), must be the loader's
0x1C ...            code and data, linked at load + 0x1C
```

The whole file is read to `load`, so the code after the header runs at `load + HEADER_SIZE`. The
camera is the SDK target named by the model field. FNL1 (until 2026-10-15) had no model field and a
0x14-byte header; the loader rejects it by its magic.
"""

import struct
from dataclasses import dataclass

from .sdk import SdkTarget, all_targets, target_at

MAGIC = 0x324C4E46
OLD_MAGIC = 0x314C4E46
MODEL_SIZE = 8
HEADER_SIZE = 0x1C
_HEADER = struct.Struct(f"<5I{MODEL_SIZE}s")

_ELF_MAGIC = b"\x7fELF"
_PT_LOAD = 1


class PayloadError(Exception):
    pass


@dataclass(frozen=True)
class Header:
    magic: int
    load: int
    size: int
    entry: int
    sum: int
    model: bytes


def model_field(model: str) -> bytes:
    """The header model field for `model`."""
    field = model.encode("ascii")
    if not field or len(field) > MODEL_SIZE:
        raise PayloadError(f"model {model!r} is not 1..{MODEL_SIZE} ASCII characters")
    return field.ljust(MODEL_SIZE, b"\0")


def _model_name(field: bytes) -> str:
    return field.rstrip(b"\0").decode("ascii", errors="backslashreplace")


def target_for_model(field: bytes) -> SdkTarget | None:
    """The target whose loader accepts the header model field `field`."""
    return next((t for t in all_targets() if model_field(t.model) == field), None)


def word_sum(data: bytes) -> int:
    """Sum (mod 2^32) of the little-endian 32-bit words of `data` (length multiple of 4)."""
    return sum(struct.unpack(f"<{len(data) // 4}I", data)) & 0xFFFFFFFF


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER_SIZE:
        raise PayloadError(f"file too short for a header ({len(data)} bytes)")
    return Header(*_HEADER.unpack_from(data))


def _unknown_model(field: bytes) -> str:
    known = ", ".join(t.model for t in all_targets())
    return f"model {_model_name(field)!r} is no target's ({known})"


def _unknown_load(load: int) -> str:
    known = ", ".join(f"{t.name} 0x{t.load:08x}" for t in all_targets())
    return f"load address 0x{load:08x} is no target's ({known})"


def check_image(data: bytes) -> str | None:
    """Return why the loader would reject `data` as the file content, None if it accepts it.

    The loader is the one of the target named by the header's model field. Same checks in the same
    order as the loader: it reads at most max_size bytes, then checks magic, model, load address,
    size <= bytes read, size >= header, size % 4, entry < size, entry >= header and the word sum.
    """
    if len(data) < HEADER_SIZE:
        return f"file shorter than the header ({len(data)} bytes)"
    h = parse_header(data)
    if h.magic == OLD_MAGIC:
        return "bad magic: old FNL1 format, rebuild the payload"
    if h.magic != MAGIC:
        return f"bad magic 0x{h.magic:08x}"
    target = target_for_model(h.model)
    if target is None:
        return _unknown_model(h.model)
    if h.load != target.load:
        return f"load address 0x{h.load:08x} is not {target.name}'s 0x{target.load:08x}"
    read = data[: target.max_size]
    if h.size > len(read):
        return f"size 0x{h.size:x} > bytes read 0x{len(read):x}"
    if h.size < HEADER_SIZE:
        return f"size 0x{h.size:x} < header"
    if h.size % 4:
        return f"size 0x{h.size:x} not a multiple of 4"
    if h.entry >= h.size:
        return f"entry offset 0x{h.entry:x} >= size 0x{h.size:x}"
    if h.entry < HEADER_SIZE:
        return f"entry offset 0x{h.entry:x} inside the header"
    computed = word_sum(read[HEADER_SIZE : h.size])
    if computed != h.sum:
        return f"word sum 0x{computed:08x} != header 0x{h.sum:08x}"
    return None


def make_image(body: bytes, entry: int, target: SdkTarget) -> bytes:
    """Prefix `body` (linked at load + HEADER_SIZE) with the header; `entry` is an offset from load."""
    body += bytes(-len(body) % 4)
    size = HEADER_SIZE + len(body)
    if size > target.max_size:
        raise PayloadError(f"image size 0x{size:x} > 0x{target.max_size:x}")
    if not HEADER_SIZE <= entry < size:
        raise PayloadError(
            f"entry offset 0x{entry:x} outside the code [0x{HEADER_SIZE:x}, 0x{size:x})"
        )
    header = _HEADER.pack(
        MAGIC, target.load, size, entry, word_sum(body), model_field(target.model)
    )
    return header + body


@dataclass(frozen=True)
class Segment:
    vaddr: int
    data: bytes
    memsz: int


def elf_segments(elf: bytes) -> tuple[int, int, list[Segment]]:
    """Return (machine, entry address, PT_LOAD segments) of a 32-bit little-endian ELF."""
    if elf[:4] != _ELF_MAGIC or elf[4] != 1 or elf[5] != 1:
        raise PayloadError("not a 32-bit little-endian ELF")
    (machine,) = struct.unpack_from("<H", elf, 0x12)
    entry, phoff = struct.unpack_from("<II", elf, 0x18)
    phentsize, phnum = struct.unpack_from("<HH", elf, 0x2A)
    segments = []
    for i in range(phnum):
        p_type, offset, vaddr, _, filesz, memsz = struct.unpack_from(
            "<6I", elf, phoff + i * phentsize
        )
        if p_type != _PT_LOAD or memsz == 0:
            continue
        segments.append(Segment(vaddr, elf[offset : offset + filesz], memsz))
    return machine, entry, sorted(segments, key=lambda s: s.vaddr)


def elf_section(elf: bytes, name: str) -> tuple[int, int] | None:
    """(address, size) of the section `name` of a 32-bit little-endian ELF, None if it has none."""
    shoff = struct.unpack_from("<I", elf, 0x20)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", elf, 0x2E)
    headers = [
        struct.unpack_from("<10I", elf, shoff + i * shentsize) for i in range(shnum)
    ]
    strtab = headers[shstrndx][4]
    for h in headers:
        end = elf.index(b"\0", strtab + h[0])
        if elf[strtab + h[0] : end].decode() == name:
            return h[3], h[5]
    return None


def describe_shared(elf: bytes) -> str | None:
    """The placement of .shared (FENYKL_SHARED data), None if the payload has none."""
    section = elf_section(elf, ".shared")
    if section is None or section[1] == 0:
        return None
    address, size = section
    uncached = (address & 0x3FFFFFFF) | 0x80000000
    return (
        f"shared     0x{address:08x}-0x{address + size:08x} ({size} bytes, "
        f"uncached alias 0x{uncached:08x})"
    )


def pack_elf(elf: bytes) -> bytes:
    """Build a FENYKL.BIN image from a payload ELF linked with a target's link.ld.

    The target is the one whose load address + HEADER_SIZE is the first segment's address.
    """
    machine, entry, segments = elf_segments(elf)
    if not segments:
        raise PayloadError("ELF has no loadable segments")
    load = segments[0].vaddr - HEADER_SIZE
    target = target_at(load)
    if target is None:
        raise PayloadError(
            f"first segment at 0x{segments[0].vaddr:08x}: {_unknown_load(load)}"
        )
    if machine != target.elf_machine:
        raise PayloadError(
            f"ELF machine {machine} is not {target.name}'s {target.elf_machine}"
        )
    end = max(s.vaddr + s.memsz for s in segments)
    if end > target.region_end:
        raise PayloadError(
            f"payload incl. .bss ends at 0x{end:08x}, above 0x{target.region_end:08x}"
        )
    base = load + HEADER_SIZE
    body = bytearray()
    for s in segments:
        start = s.vaddr - base
        if start < len(body):
            raise PayloadError(f"overlapping segments at 0x{s.vaddr:08x}")
        body += bytes(start - len(body)) + s.data
    return make_image(bytes(body), entry - load, target)


def describe(data: bytes, elf: bytes | None = None) -> str:
    """Header and loader verdict of a FENYKL.BIN; with its ELF also the .shared placement."""
    h = parse_header(data)
    problem = check_image(data)
    if h.magic == OLD_MAGIC:
        return f"file size  0x{len(data):x} ({len(data)} bytes)\nloader     rejects: {problem}"
    target = target_for_model(h.model)
    lines = [
        f"file size  0x{len(data):x} ({len(data)} bytes)",
        f"magic      0x{h.magic:08x}" + (" (FNL2)" if h.magic == MAGIC else ""),
        f"model      {_model_name(h.model)!r}"
        + (f" (target {target.name})" if target else ""),
        f"load       0x{h.load:08x}",
        f"size       0x{h.size:x} ({h.size} bytes)",
        f"entry      +0x{h.entry:x} = 0x{(h.load + h.entry) & 0xFFFFFFFF:08x}",
        f"sum        0x{h.sum:08x}",
        *([shared] if elf and (shared := describe_shared(elf)) else []),
        "loader     " + ("accepts" if problem is None else f"rejects: {problem}"),
    ]
    return "\n".join(lines)
