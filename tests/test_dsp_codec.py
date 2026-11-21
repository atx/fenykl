from collections import Counter

import pytest
from conftest import stock_image

from fenykl.dsp.fields import (
    CONTROL_FLAG,
    CONTROL_OPS,
    DATA_OPS,
    FIELDS,
    IDLE_OP,
    STAIRCASE,
    WORD_BITS,
    op,
)
from fenykl.dsp.program import STOCK, decode_item, encode_item, stock_items
from fenykl.dsp.word import IDLE, Word


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
