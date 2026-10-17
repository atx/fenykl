import hashlib

import pytest
from conftest import stock_image

from fenykl.firmware import (
    TARGETS,
    Block,
    BlockHeader,
    Firmware,
    Target,
    pack_firmware,
    unpack_firmware,
)
from fenykl.patching import ByteEdit, Patch, PatchError, apply_patches


def _synthetic() -> tuple[bytes, Target]:
    code = bytes(range(64)) + b"hello\0world\0hello\0" + bytes(14)
    lang = b"menu text\0" + bytes(22)
    firmware = Firmware(
        [
            Block(BlockHeader(b"OE", b"\x08\x60", 0x1000, 0x1600, 0x0100), code),
            Block(BlockHeader(b"OE", b"\x08\x60", 0x2000, 0x0000, 0x0101), lang),
        ]
    )
    data = pack_firmware(firmware)
    target = Target(
        "test", "TEST", "1.0", hashlib.sha256(data).hexdigest(), len(data), "TEST.BIN"
    )
    return data, target


def _edit(block: str, offset: int, original: bytes, replacement: bytes) -> ByteEdit:
    return ByteEdit(block, offset, hashlib.sha256(original).hexdigest(), replacement)


def _patch(target: Target, *edits, name: str = "test") -> Patch:
    return Patch(name, "", target, tuple(edits))


def test_applies_edits_to_their_blocks():
    data, target = _synthetic()
    patch = _patch(
        target,
        _edit("main-code-01", 2, b"\x02\x03", b"\xaa\xbb"),
        _edit("main-lang", 0, b"menu", b"MENU"),
    )
    patched = unpack_firmware(apply_patches(data, [patch]))
    assert patched.blocks[0].body[:5] == b"\x00\x01\xaa\xbb\x04"
    assert patched.blocks[1].body[:10] == b"MENU text\0"


def test_rejects_wrong_input_hash():
    data, _ = _synthetic()
    other = Target("other", "TEST", "1.1", "00" * 32, len(data), "OTHER.BIN")
    edit = _edit("main-code-01", 0, b"\x00", b"\x01")
    with pytest.raises(PatchError, match="sha256"):
        apply_patches(data, [_patch(other, edit)])


def test_rejects_mismatching_original():
    data, target = _synthetic()
    edit = _edit("main-code-01", 2, b"\x01\x02", b"\xff\xff")
    with pytest.raises(PatchError, match="offset 0x2: expected original sha256"):
        apply_patches(data, [_patch(target, edit)])


def test_rejects_edit_past_block_end():
    data, target = _synthetic()
    edit = _edit("main-lang", 30, b"\x00" * 4, b"\xff" * 4)
    with pytest.raises(PatchError, match="past the block end 0x20"):
        apply_patches(data, [_patch(target, edit)])


def test_rejects_overlap_across_patches():
    data, target = _synthetic()
    a = _patch(target, _edit("main-code-01", 4, b"\x04\x05\x06", b"abc"), name="a")
    b = _patch(target, _edit("main-code-01", 6, b"\x06\x07", b"xy"), name="b")
    with pytest.raises(PatchError, match="overlapping edits in block 0: a .* b"):
        apply_patches(data, [a, b])


def test_adjacent_edits_are_not_overlapping():
    data, target = _synthetic()
    patch = _patch(
        target,
        _edit("main-code-01", 6, b"\x06", b"c"),
        _edit("main-code-01", 4, b"\x04\x05", b"ab"),
    )
    assert unpack_firmware(apply_patches(data, [patch])).blocks[0].body[4:7] == b"abc"


def test_epl3_round_trip_changes_only_edited_bytes():
    stock = stock_image()
    stock_firmware = unpack_firmware(stock)
    code_original = stock_firmware.blocks[0].body[0x581BC:0x581BE]
    lang_original = stock_firmware.blocks[1].body[0x1000:0x1004]
    lang_replacement = bytes(b ^ 0xFF for b in lang_original)
    patch = _patch(
        TARGETS["epl3"],
        _edit("main-code-01", 0x581BC, code_original, b"X\0"),
        _edit("main-lang", 0x1000, lang_original, lang_replacement),
    )

    # Unpacking verifies the recomputed checksums.
    patched = unpack_firmware(apply_patches(stock, [patch]))

    expected = [bytearray(block.body) for block in stock_firmware.blocks]
    expected[0][0x581BC:0x581BE] = b"X\0"
    expected[1][0x1000:0x1004] = lang_replacement
    assert [block.header for block in patched.blocks] == [
        block.header for block in stock_firmware.blocks
    ]
    assert [block.body for block in patched.blocks] == [bytes(b) for b in expected]
