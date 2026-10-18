import dataclasses
import struct

import pytest

from fenykl import sdk
from fenykl.payload import (
    HEADER_SIZE,
    MAGIC,
    PayloadError,
    check_image,
    describe,
    make_image,
    pack_elf,
    parse_header,
)

EPL3 = sdk.target("epl3")
LOAD = EPL3.load
MAX_SIZE = EPL3.max_size
BASE = LOAD + HEADER_SIZE


def make_elf(
    entry: int, segments: list[tuple[int, bytes, int]], machine: int = 89
) -> bytes:
    """Minimal ELF32 LE MN10300 executable with PT_LOAD segments (vaddr, data, memsz)."""
    phoff = 52
    data_off = phoff + 32 * len(segments)
    ehdr = struct.pack(
        "<4s5B7x2H5I6H",
        b"\x7fELF",
        1,
        1,
        1,
        0,
        0,
        2,
        machine,
        1,
        entry,
        phoff,
        0,
        0,
        52,
        32,
        len(segments),
        40,
        0,
        0,
    )
    phdrs, blobs = b"", b""
    for vaddr, data, memsz in segments:
        phdrs += struct.pack(
            "<8I", 1, data_off + len(blobs), vaddr, vaddr, len(data), memsz, 7, 4
        )
        blobs += data
    return ehdr + phdrs + blobs


def test_pack_layout_and_round_trip():
    code = bytes(range(1, 11))
    image = pack_elf(make_elf(BASE + 4, [(BASE, code, 0x100)]))
    h = parse_header(image)
    assert (h.magic, h.load, h.size, h.entry, h.model) == (
        MAGIC,
        LOAD,
        HEADER_SIZE + 12,
        HEADER_SIZE + 4,
        b"E-PL3\0\0\0",
    )
    assert image[:4] == b"FNL2"
    assert image[0x14:0x1C] == b"E-PL3\0\0\0"
    assert image[HEADER_SIZE:] == code + bytes(2)
    words = struct.unpack("<3I", image[HEADER_SIZE:])
    assert h.sum == sum(words) & 0xFFFFFFFF
    assert check_image(image) is None


def test_pack_fills_gaps_between_segments():
    image = pack_elf(
        make_elf(BASE, [(BASE + 8, b"\xaa" * 4, 4), (BASE, b"\x01" * 4, 4)])
    )
    assert image[HEADER_SIZE:] == b"\x01" * 4 + bytes(4) + b"\xaa" * 4


def test_pack_rejects_wrong_link_address():
    with pytest.raises(PayloadError, match="first segment"):
        pack_elf(make_elf(LOAD, [(LOAD, bytes(8), 8)]))


def test_pack_rejects_other_cpu_for_the_target():
    with pytest.raises(PayloadError, match="machine 40"):
        pack_elf(make_elf(BASE, [(BASE, bytes(8), 8)], machine=40))


def test_pack_rejects_bss_into_loader_stack():
    with pytest.raises(PayloadError, match="above"):
        pack_elf(make_elf(BASE, [(BASE, bytes(8), MAX_SIZE)]))


def test_pack_rejects_too_big_image():
    with pytest.raises(PayloadError, match="image size"):
        make_image(bytes(MAX_SIZE - HEADER_SIZE + 4), HEADER_SIZE, EPL3)


def _valid() -> bytearray:
    return bytearray(make_image(bytes(range(256)) * 4, HEADER_SIZE, EPL3))


def _with_header(field: int, value: int) -> bytes:
    image = _valid()
    struct.pack_into("<I", image, 4 * field, value)
    return bytes(image)


def _with_model(model: bytes) -> bytes:
    image = _valid()
    image[0x14:0x1C] = model
    return bytes(image)


def _fnl1() -> bytes:
    """A valid image of the old format: FNL1, 0x14-byte header, no model."""
    body = bytes(range(256)) * 4
    sum_ = sum(struct.unpack(f"<{len(body) // 4}I", body)) & 0xFFFFFFFF
    return struct.pack("<5I", 0x314C4E46, LOAD, 0x14 + len(body), 0x14, sum_) + body


def _flip_body_bit() -> bytes:
    image = _valid()
    image[0x100] ^= 0x10
    return bytes(image)


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        (_flip_body_bit(), "word sum"),
        (bytes(_valid()[:-4]), "bytes read"),
        (bytes(_valid()[:0x10]), "shorter than the header"),
        (_with_header(0, 0x334C4E46), "magic"),
        (_fnl1(), "old FNL1 format"),
        (_with_model(b"E-PL5\0\0\0"), "model 'E-PL5' is no target's (E-M10II, E-PL3)"),
        (_with_model(b"E-PL3\0\0 "), "is no target's"),
        (_with_header(1, LOAD + 0x1000), "load address"),
        (_with_header(2, 0x10), "< header"),
        (_with_header(2, len(_valid()) - 2), "multiple of 4"),
        (_with_header(3, len(_valid())), "entry offset"),
        (_with_header(4, 0), "word sum"),
    ],
)
def test_loader_rejects_corrupted_images(image, reason):
    assert reason in check_image(image)


def test_loader_reads_at_most_max_size():
    # A header claiming more than the loader reads is rejected even if the file is that long.
    body = bytes(MAX_SIZE)
    image = (
        struct.pack("<5I8s", MAGIC, LOAD, MAX_SIZE + 4, HEADER_SIZE, 0, b"E-PL3") + body
    )
    assert "bytes read" in check_image(image)
    # Trailing bytes after a valid image are ignored, as the loader only checks [0, size).
    assert check_image(bytes(_valid()) + b"junk") is None


def test_info_names_the_model_and_rejects_old_images():
    info = describe(bytes(_valid()))
    assert "model      'E-PL3' (target epl3)" in info
    assert info.endswith("loader     accepts")
    assert describe(_fnl1()).endswith(
        "rejects: bad magic: old FNL1 format, rebuild the payload"
    )


def test_pack_rejects_a_model_that_does_not_fit_the_field():
    target = dataclasses.replace(EPL3, model="E-PL3-LONG")
    with pytest.raises(PayloadError, match="1..8 ASCII"):
        make_image(bytes(4), HEADER_SIZE, target)
