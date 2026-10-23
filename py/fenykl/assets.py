"""Dump the GUI fonts, inline icons and images built into a stock firmware.

Every bitmap is a u32 header
`{h-1:12, w-1:12, fmt:8}` followed by run-length `(colour, run)` codes that the OSD
blitter reads natively. The dumped PNGs are copyrighted firmware content.
"""

import csv
import struct
import tomllib
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .firmware import TARGETS, Target, identify, unpack_firmware
from .sdk import ARCH

type Rgba = tuple[int, int, int, int]
type Palette = tuple[Rgba, ...]

# 2 bpp glyphs and icons: 0 background, 3 body, 1/2 anti-alias shades.
GREY_RAMP: Palette = (
    (0, 0, 0, 0),
    (110, 110, 110, 255),
    (180, 180, 180, 255),
    (255, 255, 255, 255),
)
BITS_PER_PIXEL = {0x01: 2, 0x09: 2, 0x02: 4, 0x03: 8}


class AssetError(ValueError):
    pass


@dataclass(frozen=True)
class AssetLayout:
    """Where one firmware keeps its GUI assets. Addresses are runtime (DRAM) addresses."""

    lang_block: str
    lang_flash_base: int
    """Flash address of the lang block body; its directory holds `{u32 flash_addr, u32 size, 8x 0}`."""
    common_dir: int
    common_addr: int
    icon_table: int
    """Common-resource header offset of the icon table pointer."""
    icon_count: int
    icon_code_base: int
    image_table: int
    """Common-resource header offset of the image table pointer (`[0]` unused)."""
    image_count: int
    string_dirs: range
    string_addr: int
    font_dir_offset: int
    """Font resource dir = string dir + this."""
    font_addr: int
    font_desc_size: int
    font_list_end: int
    code_block: str
    palette_table: int
    """Code-block address of the GUI palette pointer table."""
    palette_count: int
    palette_4bpp: int
    palette_8bpp: int
    image_slot_dir: int | None = None
    """Lang dir of a second common-resource slot that image records also point into."""
    image_slot_addr: int | None = None


def _load_layouts() -> dict[str, AssetLayout]:
    """The `[assets]` table of each sdk/arch/<camera>/target.toml that has one."""
    layouts = {}
    for path in sorted(ARCH.glob("*/target.toml")):
        toml = tomllib.loads(path.read_text()).get("assets")
        if toml is None:
            continue
        start, stop = toml.pop("string_dirs")
        layouts[path.parent.name] = AssetLayout(**toml, string_dirs=range(start, stop))
    return layouts


ASSET_LAYOUTS = _load_layouts()


@dataclass(frozen=True)
class Bitmap:
    width: int
    height: int
    fmt: int
    pixels: bytes
    """One colour index per byte, row-major."""


@dataclass(frozen=True)
class Asset:
    kind: str
    """One of "icon", "image", "glyph"."""
    name: str
    """File stem: hex char code (icons), decimal id (images), U+XXXX (glyphs)."""
    font: str
    """Glyph set directory for glyphs, empty otherwise."""
    address: int
    """DRAM address of the encoded bitmap while its resource is loaded."""
    bitmap: Bitmap
    palette: Palette

    @property
    def path(self) -> Path:
        if self.kind == "glyph":
            return Path("fonts", self.font, f"{self.name}.png")
        return Path(f"{self.kind}s", f"{self.name}.png")


@dataclass(frozen=True)
class Mem:
    """A resource image mapped at its runtime address."""

    data: bytes
    base: int

    def off(self, addr: int) -> int:
        offset = addr - self.base
        if not 0 <= offset < len(self.data):
            raise AssetError(
                f"address {addr:#x} outside {self.base:#x}+{len(self.data):#x}"
            )
        return offset

    def u16(self, addr: int) -> int:
        return struct.unpack_from("<H", self.data, self.off(addr))[0]

    def u32(self, addr: int) -> int:
        return struct.unpack_from("<I", self.data, self.off(addr))[0]

    def bitmap(self, addr: int) -> Bitmap:
        return decode_bitmap(self.data, self.off(addr))


def _codes(data: bytes, start: int, fmt: int) -> Iterator[tuple[int, int]]:
    """Yield the (colour, run) codes of a bitmap body."""
    if fmt == 0x09:  # 2 bpp, 8-bit codes: colour 7..6, run-1 5..0
        for b in data[start:]:
            yield b >> 6, (b & 0x3F) + 1
    elif fmt == 0x01:  # 2 bpp, 4-bit codes low nibble first: colour 3..2, run-1 1..0
        for b in data[start:]:
            yield (b >> 2) & 3, (b & 3) + 1
            yield b >> 6, ((b >> 4) & 3) + 1
    elif fmt == 0x02:  # 4 bpp, 8-bit codes: colour 7..4, run-1 3..0
        for b in data[start:]:
            yield b >> 4, (b & 0xF) + 1
    elif (
        fmt == 0x03
    ):  # 8 bpp, six 10-bit codes per little-endian u64: colour 9..2, run-1 1..0
        for o in range(start, len(data) - 7, 8):
            word = int.from_bytes(data[o : o + 8], "little")
            for k in range(6):
                code = (word >> (10 * k)) & 0x3FF
                yield code >> 2, (code & 3) + 1
    else:
        raise AssetError(f"unknown bitmap format {fmt:#x}")


def decode_bitmap(data: bytes, offset: int) -> Bitmap:
    """Decode the RLE bitmap at `data[offset:]`; codes after the last pixel are ignored."""
    (head,) = struct.unpack_from("<I", data, offset)
    height = (head & 0xFFF) + 1
    width = ((head >> 12) & 0xFFF) + 1
    fmt = head >> 24
    total = width * height
    pixels = bytearray()
    column = 0
    for colour, run in _codes(data, offset + 4, fmt):
        if column + run > width:
            raise AssetError(
                f"bitmap at {offset:#x}: run of {run} crosses the row end at "
                f"pixel {len(pixels)} ({width}x{height})"
            )
        pixels += bytes([colour]) * run
        column = (column + run) % width
        if len(pixels) == total:
            return Bitmap(width, height, fmt, bytes(pixels))
    raise AssetError(
        f"bitmap at {offset:#x}: data ends after {len(pixels)} of {width}x{height} pixels"
    )


def _lang_dir(lang: bytes, layout: AssetLayout, index: int) -> bytes:
    addr, size = struct.unpack_from("<II", lang, index * 16)
    start = addr - layout.lang_flash_base
    if not 0 <= start <= start + size <= len(lang):
        raise AssetError(
            f"lang dir {index:#x} ({addr:#x}+{size:#x}) outside the lang block"
        )
    return lang[start : start + size]


def _palette(code: Mem, layout: AssetLayout, index: int) -> Palette:
    """GUI palette record `{s16 count, s16, u32 entries}`, entries ARGB8888."""
    if not 0 < index < layout.palette_count:
        raise AssetError(f"palette {index} not in 1..{layout.palette_count - 1}")
    record = code.u32(layout.palette_table + 4 * index)
    count = code.u16(record)
    entries = code.u32(record + 4)
    argb = [code.u32(entries + 4 * k) for k in range(count)]
    return tuple(((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF, v >> 24) for v in argb)


def _icons(common: Mem, layout: AssetLayout) -> Iterator[Asset]:
    """Icon table: u32 -> record `{u16 w, u16 h, u32 bitmap}`."""
    table = common.u32(layout.common_addr + layout.icon_table)
    for i in range(layout.icon_count):
        record = common.u32(table + 4 * i)
        address = common.u32(record + 4)
        bitmap = common.bitmap(address)
        if (bitmap.width, bitmap.height) != (
            common.u16(record),
            common.u16(record + 2),
        ):
            raise AssetError(f"icon {i:#x}: record size differs from the bitmap header")
        yield Asset(
            "icon", f"{layout.icon_code_base + i:04X}", "", address, bitmap, GREY_RAMP
        )


def _images(
    common: Mem, layout: AssetLayout, palettes: dict[int, Palette]
) -> Iterator[Asset]:
    """Image table: u32 per id -> record `{u16 type, w, h, bpp, u32 0, u32 size, u32 bitmap}`."""
    table = common.u32(layout.common_addr + layout.image_table)
    for image_id in range(1, layout.image_count + 1):
        record = common.u32(table + 4 * image_id)
        address = common.u32(record + 0x10)
        bitmap = common.bitmap(address)
        bpp = BITS_PER_PIXEL[bitmap.fmt]
        fields = (
            common.u16(record + 2),
            common.u16(record + 4),
            common.u16(record + 6),
        )
        if fields != (bitmap.width, bitmap.height, bpp):
            raise AssetError(
                f"image {image_id}: record w/h/bpp {fields} differ from the bitmap"
            )
        yield Asset("image", f"{image_id:04d}", "", address, bitmap, palettes[bpp])


def _glyphs(lang: bytes, layout: AssetLayout) -> Iterator[Asset]:
    """Per-language glyph sets, from each language's font resource (`font_res`)."""
    fonts = set()
    for string_dir in layout.string_dirs:
        strings = Mem(_lang_dir(lang, layout, string_dir), layout.string_addr)
        # Strings word 1 = font descriptor list; record 1 names the language.
        desc = strings.off(strings.u32(layout.string_addr + 4) + layout.font_desc_size)
        name = strings.data[desc : desc + 0x40].decode("utf-16-le").split("\0")[0]
        language = name.removeprefix("OlympusLanguage_").removesuffix("tft")
        res = Mem(
            _lang_dir(lang, layout, string_dir + layout.font_dir_offset),
            layout.font_addr,
        )
        record = res.u32(layout.font_addr + 4)
        while res.u32(record) != layout.font_list_end:
            font_id, key, sets, index = struct.unpack_from(
                "<IIII", res.data, res.off(record)
            )
            record += 16
            font = f"{language}_id{font_id}_k{key}"
            if font in fonts:
                raise AssetError(f"glyph set {font} occurs twice")
            fonts.add(font)
            pages = res.u32(sets + 4 * index)
            for page in range(256):
                glyphs = res.u32(pages + 4 * page)
                if glyphs == 0:
                    continue
                for c in range(256):
                    address = res.u32(glyphs + 8 * c + 4)
                    if address == 0:
                        continue
                    bitmap = res.bitmap(address)
                    yield Asset(
                        "glyph",
                        f"U+{page * 256 + c:04X}",
                        font,
                        address,
                        bitmap,
                        GREY_RAMP,
                    )


def layout_for(target: Target | None) -> AssetLayout:
    if target is None or target.name not in ASSET_LAYOUTS:
        supported = ", ".join(
            f"{TARGETS[name].model} v{TARGETS[name].version} ({TARGETS[name].file})"
            for name in ASSET_LAYOUTS
        )
        found = (
            "unknown firmware"
            if target is None
            else f"{target.model} v{target.version}"
        )
        raise AssetError(f"{found}: assets dump supports only {supported}")
    return ASSET_LAYOUTS[target.name]


def _common(lang: bytes, layout: AssetLayout) -> Mem:
    """The common resource at its runtime address; a second image slot is mapped at its own address,
    the gap between them zero."""
    common = _lang_dir(lang, layout, layout.common_dir)
    if layout.image_slot_dir is None or layout.image_slot_addr is None:
        return Mem(common, layout.common_addr)
    gap = layout.image_slot_addr - layout.common_addr - len(common)
    if gap < 0:
        raise AssetError(
            f"image slot {layout.image_slot_addr:#x} overlaps the common resource"
        )
    slot = _lang_dir(lang, layout, layout.image_slot_dir)
    return Mem(common + bytes(gap) + slot, layout.common_addr)


def common_resource(data: bytes) -> Mem:
    """The common GUI resource (icons, images) of a supported stock `.BIN`, mapped at its runtime
    address as the firmware loads it."""
    layout = layout_for(identify(data))
    firmware = unpack_firmware(data)
    lang = firmware.blocks[firmware.find_block(layout.lang_block)].body
    return _common(lang, layout)


def read_assets(data: bytes) -> list[Asset]:
    """Decode every icon, image and glyph of a supported stock `.BIN`."""
    target = identify(data)
    layout = layout_for(target)
    firmware = unpack_firmware(data)
    code_block = firmware.blocks[firmware.find_block(layout.code_block)]
    lang = firmware.blocks[firmware.find_block(layout.lang_block)].body
    code = Mem(code_block.body, target.load_addresses[layout.code_block])
    common = _common(lang, layout)
    palettes = {
        4: _palette(code, layout, layout.palette_4bpp),
        8: _palette(code, layout, layout.palette_8bpp),
    }
    return [
        *_icons(common, layout),
        *_images(common, layout, palettes),
        *_glyphs(lang, layout),
    ]


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def encode_png(bitmap: Bitmap, palette: Palette) -> bytes:
    """Indexed 8-bit PNG: the pixel values are the raw colour indices, `palette` gives PLTE + tRNS."""
    if max(bitmap.pixels) >= len(palette):
        raise AssetError(
            f"colour index {max(bitmap.pixels)} beyond the {len(palette)}-entry palette"
        )
    w = bitmap.width
    rows = b"".join(
        b"\0" + bitmap.pixels[y * w : (y + 1) * w] for y in range(bitmap.height)
    )
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            png_chunk(
                b"IHDR", struct.pack(">IIBBBBB", w, bitmap.height, 8, 3, 0, 0, 0)
            ),
            png_chunk(b"PLTE", bytes(c for r, g, b, _ in palette for c in (r, g, b))),
            png_chunk(b"tRNS", bytes(a for *_, a in palette)),
            png_chunk(b"IDAT", zlib.compress(rows)),
            png_chunk(b"IEND", b""),
        ]
    )


def dump_assets(data: bytes, out: Path) -> list[Asset]:
    """Write every asset as an indexed PNG under `out`, plus `out/index.csv`."""
    assets = read_assets(data)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "index.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["kind", "id", "font", "address", "width", "height", "fmt", "file"]
        )
        for asset in assets:
            path = out / asset.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encode_png(asset.bitmap, asset.palette))
            bm = asset.bitmap
            writer.writerow(
                [
                    asset.kind,
                    asset.name,
                    asset.font,
                    f"0x{asset.address:08X}",
                    bm.width,
                    bm.height,
                    f"0x{bm.fmt:02X}",
                    asset.path.as_posix(),
                ]
            )
    return assets
