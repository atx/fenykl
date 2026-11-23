from collections import Counter

import pytest
from conftest import stock_image

from fenykl.dsp.fields import (
    CONTROL_FLAG,
    CONTROL_OPS,
    DATA_OPS,
    FIELDS,
    FORMATS,
    IDLE_OP,
    IDLE_VALUE,
    STAIRCASE,
    WORD_BITS,
    X_IMMEDIATES,
    Operands,
    op,
)
from fenykl.dsp.program import STOCK, decode_item, encode_item, stock_items
from fenykl.dsp.word import IDLE, Word


@pytest.mark.parametrize(
    "unit, k, parts",
    [
        ("x0", 0x5A5, {"f1": 1, "f2_6": 0x12, "f105_109": 0x16}),
        ("x1", 0x1ABC, {"f7_8": 0, "f9_14": 0x2F, "f115_119": 0x1A}),
    ],
)
def test_x_immediates_pack_into_their_fields(unit, k, parts):
    word = Word.from_int(X_IMMEDIATES[unit].put(IDLE_VALUE, k))
    assert {key: word[key] for key in parts} == parts
    assert X_IMMEDIATES[unit].get(word.value) == k


def test_x1_head_is_f310_312_over_f308_309():
    # The transfer form 0x1d: 7 in 310..312, 1 in 308..309.
    word = Word.from_int(IDLE_VALUE & ~(0x1F << 308) | (7 << 310) | (1 << 308))
    assert word["x1op"] == 0x1D


def test_every_bit_in_exactly_one_field():
    owners = Counter(bit for f in FIELDS for bit in range(f.lo, f.hi + 1))
    assert sorted(owners) == list(range(WORD_BITS))
    assert set(owners.values()) == {1}


@pytest.fixture(scope="module")
def items() -> dict[int, bytes]:
    return stock_items(stock_image("em10ii"))


def test_stock_items_found(items):
    assert sorted(items) == sorted(STOCK)
    counts = {item: len(decode_item(data, STOCK[item])) for item, data in items.items()}
    assert counts == {7: 1589, 13: 704, 15: 666, 16: 84, 17: 1150}


def test_words_round_trip_through_fields(items):
    # Rebuilt from the field values alone on an all-zero base, so the table must hold every bit.
    zero = Word.from_int(0)
    for item, data in items.items():
        for index, word in enumerate(decode_item(data, STOCK[item]).words):
            rebuilt = Word.from_fields(word.fields(), base=zero)
            assert rebuilt.to_bytes() == word.to_bytes(), (item, index)


def test_items_round_trip(items):
    for item, data in items.items():
        assert encode_item(decode_item(data, STOCK[item]), STOCK[item]) == data, item


def test_stock_words_pass_the_assembler_checks(items):
    for item, data in items.items():
        for word in decode_item(data, STOCK[item]).words:
            word.check()


def test_idle_word_is_the_copy_programs_no_op(items):
    words = decode_item(items[16], STOCK[16]).words
    assert [i for i, w in enumerate(words) if w == IDLE] == [3, 4, 6, 7, 8, 9]


def test_slots_rebuild_the_word(items):
    for word in decode_item(items[13], STOCK[13]).words:
        assert Word(**word.slots()) == word


def test_program_16_route_staircase(items):
    # Each staircase nibble is 0 from its word to the end of program 16, and not just before.
    words = decode_item(items[16], STOCK[16]).words
    for f in FIELDS:
        if f.lo not in STAIRCASE:
            continue
        start = STAIRCASE[f.lo]
        assert words[start - 1][f.id] != 0, f.id
        assert {w[f.id] for w in words[start:]} == {0}, f.id


def test_control_word_targets_lie_inside_their_program(items):
    # (control words, returns) per program; every op 0..3 word names a word of its program.
    counts = {}
    for item, data in items.items():
        words = decode_item(data, STOCK[item]).words
        control = [w for w in words if w.control]
        counts[item] = (len(control), sum(w.op == 4 for w in control))
        for index, word in enumerate(words):
            if word.target is not None:
                assert word.target < len(words), (item, index)
    assert counts == {7: (72, 9), 13: (24, 2), 15: (15, 2), 16: (5, 1), 17: (36, 4)}


def test_the_op_field_decodes_every_stock_word(items):
    # f229 is clear exactly in the control ops, the immediate is 0 whenever the op is idle, halt
    # is word 5 alone, and every other op is a data op.
    for item, data in items.items():
        words = decode_item(data, STOCK[item]).words
        for index, w in enumerate(words):
            flag_clear = CONTROL_FLAG.get(w.value) == 0
            assert flag_clear == (w.op in CONTROL_OPS), (item, index)
            if w.op == IDLE_OP:
                assert w["imm"] == 0, (item, index)
        assert [i for i, w in enumerate(words) if w.op == 7] == [5], item
        others = {w.op for w in words} - set(CONTROL_OPS) - {7, IDLE_OP}
        assert others <= set(DATA_OPS), item


def test_no_delay_slot_is_a_target_or_a_control_word(items):
    for item, data in items.items():
        program = decode_item(data, STOCK[item])
        targets = {w.target for w in program.words if w.target is not None}
        control = {i for i, w in enumerate(program.words) if w.control}
        assert not program.delay_slots() & (targets | control), item


def test_program_16_control_flow(items):
    # Call the job routine at 10, halt at 5 on its return at 79; 27 may exit early to the
    # return; 45 guards the loop 50..57: it sits at the loop's T - 5 and branches to its exit.
    words = decode_item(items[16], STOCK[16]).words
    flow = {
        i: (op(w.op).name, w.target)
        for i, w in enumerate(words)
        if w.control or w.op == 7
    }
    assert flow == {
        0: ("call", 10),
        5: ("halt", None),
        27: ("brf", 79),
        45: ("brf", 58),
        53: ("br", 50),
        79: ("ret", None),
    }
    assert (45 + 5, 53 + 5) == (words[53].target, words[45].target)


@pytest.mark.parametrize(
    "code, args, fields",
    [
        # Family A: imm13 = imm | f128_132 << 8, signed; f120_122 keeps the base word's value.
        (
            0x1A,
            Operands(ra=5, imm=0x1ABC - 0x2000),
            {"ra": 5, "imm": 0xBC, "f128_132": 0x1A, "f120_122": 7},
        ),
        # -7 is 0x1ff9.
        (0x0C, Operands(ra=3, imm=-7), {"ra": 3, "imm": 0xF9, "f128_132": 0x1F}),
        # Family B: imm16 takes f120_122 as bits 13..15.
        (
            0x0B,
            Operands(ra=1, imm=0xABCD - 0x10000),
            {"ra": 1, "imm": 0xCD, "f128_132": 0x0B, "f120_122": 5},
        ),
        (
            0x37,
            Operands(ra=1, imm=0x7FFF),
            {"imm": 0xFF, "f128_132": 0x1F, "f120_122": 3},
        ),
        # Family C: ra, rb and a 5-bit immediate in imm.
        (
            0x3F,
            Operands(ra=2, rb=9, imm=31),
            {"ra": 2, "f128_132": 9, "imm": 31, "f120_122": 7},
        ),
        # Family E: registers only, imm stays 0.
        (0x49, Operands(ra=4, rb=6), {"ra": 4, "f128_132": 6, "imm": 0}),
    ],
)
def test_data_op_operands_assemble_and_decode(code, args, fields):
    word = Word.data(code, args)
    assert word.op == code
    assert {key: word[key] for key in fields} == fields
    assert word.operands == args


def test_an_immediate_extends_by_its_op():
    assert Word.data(0x1A, Operands(ra=0, imm=-7)).operands.imm == -7
    assert Word.data(0x10, Operands(ra=0, imm=-0x1000)).operands.imm == -0x1000
    assert Word.data(0x1C, Operands(ra=0, imm=-1)).operands.imm == -1
    # 0x1e, 0x1f and 0x23 zero-extend imm16 although family D sign-extends it.
    assert Word.data(0x1E, Operands(ra=0, imm=0xFFFF)).operands.imm == 0xFFFF
    assert Word.data(0x0B, Operands(ra=0, imm=-1)).operands.imm == -1
    assert Word.data(0x0A, Operands(ra=0, imm=0xFFFF)).operands.imm == 0xFFFF


@pytest.mark.parametrize(
    "code, args",
    [
        (0x37, Operands(ra=0, imm=0x8000)),  # 15 bits
        (0x40, Operands(ra=0, rb=0, imm=32)),  # 5 bits
        (0x0C, Operands(ra=0, imm=0x1000)),  # signed 13 bits
        (0x0C, Operands(ra=0, imm=-0x1001)),
        (0x1E, Operands(ra=0, imm=-1)),  # zero-extended
        (0x49, Operands(ra=0)),  # rb missing
        (0x1A, Operands(ra=0, rb=1, imm=0)),  # no rb in family A
        (0x49, Operands(ra=0, rb=1, imm=0)),  # no immediate in family E
        (0x22, Operands(ra=0)),  # a data op value that never occurs has no format
    ],
)
def test_data_op_assembly_rejects_operands_outside_the_format(code, args):
    with pytest.raises(ValueError):
        Word.data(code, args)


def test_every_stock_data_op_fits_its_format(items):
    # Every data op word has a family; re-encoding its operands reproduces it, so the widths hold,
    # and the register forms leave imm 0.
    for item, data in items.items():
        for index, w in enumerate(decode_item(data, STOCK[item]).words):
            if w.op not in DATA_OPS:
                continue
            fmt = FORMATS[w.op]
            assert Word.data(w.op, w.operands, base=w) == w, (item, index)
            if fmt.imm is None:
                assert w["imm"] == 0, (item, index)


def test_program_16_reads_its_local_words_by_immediate(items):
    # The three 0x1a words name the local words of src, dst and len, all through ra = 5.
    words = decode_item(items[16], STOCK[16]).words
    reads = {i: w.operands for i, w in enumerate(words) if w.op == 0x1A}
    assert reads == {
        13: Operands(ra=5, imm=1),
        15: Operands(ra=5, imm=2),
        24: Operands(ra=5, imm=0),
    }
