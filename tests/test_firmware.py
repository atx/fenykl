import hashlib
import os

import pytest
from conftest import stock_image

from fenykl.firmware import (
    HEAD_TAIL_SIZE,
    Block,
    BlockHeader,
    Firmware,
    FirmwareFormatError,
    load_unpacked,
    pack_firmware,
    save_unpacked,
    scramble,
    unpack_firmware,
    unscramble,
)


def test_scramble_known_vector():
    # Each 4-byte group is permuted: plain 0123 4567 89ab cdef
    # is stored as 2031 5746 ba98 ecdf, then XORed with 0xFF.
    plain = bytes(range(16)) * 2
    stored = bytes.fromhex("02000301050704060b0a09080e0c0d0f") * 2
    expected = bytes(b ^ 0xFF for b in stored)
    assert scramble(plain) == expected
    assert unscramble(expected) == plain


def make_firmware() -> Firmware:
    return Firmware(
        [
            Block(
                BlockHeader(
                    type_sig=b"OE",
                    model_sig=bytes.fromhex("0860"),
                    load_address=0x402C0000,
                    version=0x1600,
                    flags=0x0100,
                    unknown1=bytes.fromhex("01020304"),
                    unknown2=bytes.fromhex("0506"),
                    unknown3=bytes.fromhex("a1a2a3a4a5a6"),
                ),
                os.urandom(256),
            ),
            Block(
                BlockHeader(
                    type_sig=b"OM",
                    model_sig=bytes.fromhex("0860"),
                    load_address=0,
                    version=0x0001,
                    flags=0x0000,
                ),
                os.urandom(64),
            ),
        ]
    )


def test_unknown_fields_round_trip(tmp_path):
    firmware = make_firmware()
    packed = pack_firmware(firmware)

    head = packed[:HEAD_TAIL_SIZE]
    assert head[0x04:0x08] == bytes.fromhex("01020304")
    assert head[0x12:0x14] == bytes.fromhex("0506")
    assert head[0x14:0x16] == bytes.fromhex("0001")
    assert head[0x16:0x1C] == bytes.fromhex("a1a2a3a4a5a6")

    assert unpack_firmware(packed) == firmware
    save_unpacked(firmware, tmp_path)
    assert load_unpacked(tmp_path) == firmware
    assert pack_firmware(load_unpacked(tmp_path)) == packed


def test_flags_zero_is_stored_unscrambled():
    firmware = make_firmware()
    packed = pack_firmware(firmware)
    second = packed[2 * HEAD_TAIL_SIZE + 256 :]
    assert second[HEAD_TAIL_SIZE : HEAD_TAIL_SIZE + 64] == firmware.blocks[1].body
    first_body = packed[HEAD_TAIL_SIZE : HEAD_TAIL_SIZE + 256]
    assert first_body != firmware.blocks[0].body


def test_bad_checksum_rejected():
    packed = bytearray(pack_firmware(make_firmware()))
    packed[2 * HEAD_TAIL_SIZE + 256 - 1] ^= 0x01
    with pytest.raises(FirmwareFormatError, match="checksum"):
        unpack_firmware(bytes(packed))


def test_head_tail_mismatch_rejected():
    packed = bytearray(pack_firmware(make_firmware()))
    packed[HEAD_TAIL_SIZE + 256 + 0x08] ^= 0x01
    with pytest.raises(FirmwareFormatError, match="differ"):
        unpack_firmware(bytes(packed))


def test_truncated_rejected():
    packed = pack_firmware(make_firmware())
    with pytest.raises(FirmwareFormatError, match="exceeds"):
        unpack_firmware(packed[:-1])
    with pytest.raises(FirmwareFormatError, match="truncated"):
        unpack_firmware(packed + bytes(4))


def test_round_trip(tmp_path):
    original = stock_image("em10ii")
    firmware = unpack_firmware(original)
    assert pack_firmware(firmware) == original

    save_unpacked(firmware, tmp_path)
    assert pack_firmware(load_unpacked(tmp_path)) == original


# These pin the unpack format of the stock E-M10 II v1.4 image: (file, size, sha256) of every file.
STOCK_UNPACKED = [
    (
        "00.main-code-01.bin",
        0x145FFC0,
        "309682c05a96d3079ebcae598169d487fea7f8b2531d8ae99445d63e80ea7a39",
    ),
    (
        "01.main-lang.bin",
        0x19FFFC0,
        "9060299579236c099ec3f318d186cddab004f27eb373b1e6da75e72c4e08179b",
    ),
    (
        "02.main-b106.bin",
        0x13FFFC0,
        "5666caec18c928175d09244fe7f48f061fde3f118b113fe6d81e40afd2e6e64c",
    ),
    (
        "manifest.json",
        0x366,
        "45f269972758877646febb0462ff103671eb89a66eb6325803e100ca3808a573",
    ),
]


def test_stock_unpack_is_pinned(tmp_path):
    save_unpacked(unpack_firmware(stock_image("em10ii")), tmp_path)
    unpacked = [
        (path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(tmp_path.iterdir())
    ]
    assert unpacked == STOCK_UNPACKED


def test_modified_body_repacks_with_new_checksum():
    original = stock_image("em10ii")
    firmware = unpack_firmware(original)
    block_index, byte_index = 0, 0x1234
    block = firmware.blocks[block_index]
    body = bytearray(block.body)
    body[byte_index] ^= 0x5A
    block.body = bytes(body)

    packed = pack_firmware(firmware)
    assert len(packed) == len(original)
    # Verifies all checksums and that the change survives scrambling.
    assert unpack_firmware(packed).blocks[block_index].body == block.body

    body_start = HEAD_TAIL_SIZE
    chunk = body_start + byte_index // 16 * 16
    checksum = body_start + len(body) + HEAD_TAIL_SIZE - 4
    assert packed[:chunk] == original[:chunk]
    assert packed[chunk + 16 : checksum] == original[chunk + 16 : checksum]
    assert packed[checksum + 4 :] == original[checksum + 4 :]
    changed = [
        a != b for a, b in zip(packed[chunk : chunk + 16], original[chunk : chunk + 16])
    ]
    assert sum(changed) == 1
    assert packed[checksum : checksum + 4] != original[checksum : checksum + 4]
