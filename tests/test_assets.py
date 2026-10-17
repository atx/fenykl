import struct
import zlib

import pytest
from conftest import stock_image

from fenykl.assets import AssetError, Bitmap, decode_bitmap, encode_png, read_assets


def _header(width: int, height: int, fmt: int) -> bytes:
    return struct.pack("<I", (height - 1) | (width - 1) << 12 | fmt << 24)


def _codes10(*codes: int) -> bytes:
    word = sum(code << (10 * k) for k, code in enumerate(codes))
    return word.to_bytes(8, "little")


@pytest.mark.parametrize(
    ("width", "height", "fmt", "body", "pixels"),
    [
        # 8-bit codes, colour in bits 7..6: (3, 2) (1, 1) | (2, 3)
        (3, 2, 0x09, bytes([0xC1, 0x40, 0x82]), [3, 3, 1, 2, 2, 2]),
        # 4-bit codes, low nibble first: (1, 2) | (3, 1) (2, 1); the last nibble is padding
        (2, 2, 0x01, bytes([0xC5, 0x08]), [1, 1, 3, 2]),
        # 4 bpp: (0xA, 3) (0xF, 2)
        (5, 1, 0x02, bytes([0xA2, 0xF1]), [10, 10, 10, 15, 15]),
        # 8 bpp, 10-bit codes colour << 2 | run-1: (200, 4) | (7, 1) (255, 3); 3 codes of padding
        (
            4,
            2,
            0x03,
            _codes10(200 << 2 | 3, 7 << 2, 255 << 2 | 2),
            [200] * 4 + [7] + [255] * 3,
        ),
    ],
)
def test_decode_formats(width, height, fmt, body, pixels):
    data = b"\xee" * 4 + _header(width, height, fmt) + body
    assert decode_bitmap(data, 4) == Bitmap(width, height, fmt, bytes(pixels))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_header(2, 2, 0x09) + bytes([0xC0, 0xC2]), "crosses the row end"),
        (_header(2, 2, 0x09) + bytes([0xC1]), "data ends after 2 of 2x2"),
        (_header(2, 2, 0x04) + bytes([0xC1, 0xC1]), "unknown bitmap format 0x4"),
    ],
)
def test_decode_rejects_inconsistent_bitmaps(data, message):
    with pytest.raises(AssetError, match=message):
        decode_bitmap(data, 0)


def test_png_keeps_raw_indices():
    bitmap = Bitmap(3, 2, 0x09, bytes([0, 1, 2, 3, 3, 0]))
    palette = ((0, 0, 0, 0), (1, 2, 3, 255), (4, 5, 6, 128), (7, 8, 9, 255))
    png = encode_png(bitmap, palette)
    chunks = {}
    offset = 8
    while offset < len(png):
        (length,) = struct.unpack_from(">I", png, offset)
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack_from(">I", png, offset + 8 + length)
        assert crc == zlib.crc32(kind + payload)
        chunks[kind] = payload
        offset += 12 + length
    assert chunks[b"PLTE"] == bytes([0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert chunks[b"tRNS"] == bytes([0, 255, 128, 255])
    assert zlib.decompress(chunks[b"IDAT"]) == bytes([0, 0, 1, 2, 0, 3, 3, 0])


def test_png_rejects_index_beyond_palette():
    with pytest.raises(AssetError, match="beyond the 2-entry palette"):
        encode_png(Bitmap(1, 1, 0x09, bytes([2])), ((0, 0, 0, 0),) * 2)


def test_unknown_firmware_names_supported():
    with pytest.raises(AssetError, match=r"unknown firmware: .*E-PL3 v1\.6"):
        read_assets(b"not a firmware")


def test_epl3_stock_assets():
    assets = read_assets(stock_image())
    kinds = [asset.kind for asset in assets]
    assert kinds.count("icon") == 239
    assert kinds.count("image") == 2193
    languages = {asset.font.split("_")[0] for asset in assets if asset.kind == "glyph"}
    assert len(languages) == 34
    assert all(max(asset.bitmap.pixels) < len(asset.palette) for asset in assets)
