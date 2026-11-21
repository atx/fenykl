from collections import Counter

import pytest
from conftest import stock_image

from fenykl.dsp.fields import FIELDS, STAIRCASE, WORD_BITS
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
