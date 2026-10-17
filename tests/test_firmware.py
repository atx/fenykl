import hashlib
import os
import struct

import pytest
from conftest import stock_image

from fenykl.firmware import (
    HEAD_TAIL_SIZE,
    Block,
    BlockHeader,
    Firmware,
    FirmwareFormatError,
    compute_checksum,
    load_unpacked,
    pack_firmware,
    save_unpacked,
    scramble,
    unpack_firmware,
    unscramble,
)


def test_checksum_negates_wrapping_sum():
    data = struct.pack("<3I", 0xFFFFFFFF, 0x00000002, 0x12345678)
    expected = (-(0xFFFFFFFF + 2 + 0x12345678)) & 0xFFFFFFFF
    assert compute_checksum(data) == expected
    # Appending the checksum makes the total sum zero.
    assert compute_checksum(data + struct.pack("<I", expected)) == 0


def test_checksum_is_little_endian():
    assert compute_checksum(bytes([1, 0, 0, 0])) == 0xFFFFFFFF


def test_scramble_known_vector():
    # Each 4-byte group is permuted: plain 0123 4567 89ab cdef
    # is stored as 2031 5746 ba98 ecdf, then XORed with 0xFF.
    plain = bytes(range(16)) * 2
    stored = bytes.fromhex("02000301050704060b0a09080e0c0d0f") * 2
    expected = bytes(b ^ 0xFF for b in stored)
    assert scramble(plain) == expected
    assert unscramble(expected) == plain


def test_scramble_inverse():
    data = os.urandom(16 * 1000)
    assert unscramble(scramble(data)) == data
    assert scramble(unscramble(data)) == data
    assert scramble(data) != data


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
    original = stock_image()
    firmware = unpack_firmware(original)
    assert pack_firmware(firmware) == original

    save_unpacked(firmware, tmp_path)
    assert pack_firmware(load_unpacked(tmp_path)) == original


# These pin the unpack format of the stock E-PL3 v1.6 image: (file, size, sha256) of every file.
STOCK_UNPACKED = [
    ("00.main-code-01.bin", 0xD7FFC0, "c291722827a5120fc06ca288b439cae82ebf597ac2cd83eba30a7f0fe6a72687"),
    ("01.main-lang.bin", 0x15FFFC0, "af5a35e02304183c9e716af99b9fb619abb13f5a71e41146986bf9d833e2e20f"),
    ("02.main-b106.bin", 0x25FFC0, "ae9d7a1cc5f0d3bfeedb4dfecedc70be8a423e9bea7070cee16363fb30eaacfb"),
    ("03.main-imgs-02.bin", 0x67FFC0, "218b6003e490efc432851e831086cf8f1237f2fde3fade4be93dd89425459941"),
    ("04.ibis-code-01.bin", 0x10000, "e131370def9d69fe4484592f0cf4bc84c865162007c0cae11405a4911c8b07a2"),
    ("manifest.json", 0x596, "e81b2f4bad0a1227bcf7e2134b680db27f76173f2ef455d4f4404a1e5933678b"),
]


def test_stock_unpack_is_pinned(tmp_path):
    save_unpacked(unpack_firmware(stock_image()), tmp_path)
    unpacked = [
        (path.name, path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(tmp_path.iterdir())
    ]
    assert unpacked == STOCK_UNPACKED


def test_modified_body_repacks_with_new_checksum():
    original = stock_image()
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
