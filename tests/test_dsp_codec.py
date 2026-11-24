from collections import Counter

import pytest
from conftest import stock_image

from fenykl.dsp.fields import (
    ALU_BUS,
    CONTROL_FLAG,
    CONTROL_OPS,
    DATA_OPS,
    FIELDS,
    FORMATS,
    HEADS,
    IDLE_OP,
    IDLE_VALUE,
    LANE_FILES,
    LANE_FUNCTIONS,
    LANE_LOAD,
    LANE_SOURCES,
    LOAD_LAG,
    PB_FILES,
    PB_SOURCES,
    PORTS,
    SCALAR_READS,
    STAIRCASE,
    STORE_SOURCES,
    WORD_BITS,
    X0_LANE_LOADS,
    X_IMMEDIATES,
    Confidence,
    Operands,
    lookup,
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


def _head_id(unit: str) -> str:
    lo, hi = HEADS[unit]
    return f"f{lo}_{hi}"


def test_program_16_reads_each_scalar_register_the_word_before_wrote(items):
    # Every scalar read head runs under M, one word after a port wrote its file's register.
    words = decode_item(items[16], STOCK[16]).words
    reads = []
    for i, w in enumerate(words):
        for unit, (head, file, nibble) in SCALAR_READS.items():
            if w[_head_id(unit)] != head:
                continue
            reads.append(i)
            assert w["f276"] == 0, i
            before = words[i - 1]
            writes = [
                before[PORTS[e]]
                for e, f in PB_FILES.items()
                if f == file and before[e] != lookup(e).idle
            ]
            assert writes == [w[nibble]], i
    assert reads == [20, 30, 60]


def test_program_13_tap_loads_reach_the_file_s2_reads(items):
    # The ALU bus port of S2's file writes, at each tap load's datum word, the register the
    # matching S2 word four words on reads.
    words = decode_item(items[13], STOCK[13]).words
    _, file, nibble = SCALAR_READS["s2"]
    (enable,) = (e for e, f in PB_FILES.items() if f == file)
    (bus,) = (v for v, src in PB_SOURCES[enable].items() if src == ALU_BUS)
    for i in range(435, 439):
        assert words[i][enable] == bus, i
        assert words[i][PORTS[enable]] == words[i + 4][nibble], i


def test_lane_tables_verified_rows():
    # The rows whose pre-registered lanes all matched on the camera in either of two runs.
    verified = {
        unit: sum(tier is Confidence.VERIFIED for _, tier, _ in rows.values())
        for unit, rows in LANE_FUNCTIONS.items()
    }
    assert verified == {"s0": 72, "s1": 85, "s2": 15, "s3": 60}


def test_a_head_field_is_verified_once_every_value_has_a_verified_row():
    # S2's fifteen values all have verified rows and lags; S0, S1, S3 still have open rows.
    tiers = {
        u: lookup(f"f{lo}_{hi}").confidence
        for u, (lo, hi) in HEADS.items()
        if u[0] == "s"
    }
    assert tiers == {
        "s0": Confidence.INFERRED,
        "s1": Confidence.INFERRED,
        "s2": Confidence.VERIFIED,
        "s3": Confidence.INFERRED,
    }


def test_each_lane_port_writes_the_file_its_nibble_110_below_reads():
    # A unit's lane file has two ports, the grid B partners of its two lane nibbles.
    ports = {}
    for enable in LANE_SOURCES:
        lo = int(PORTS[enable][1:4]) - 110
        ports[f"f{lo}_{lo + 3}"] = enable
    assert sorted(ports) == sorted(n for _, *nibs in LANE_FILES.values() for n in nibs)
    for file, (unit, *nibs) in LANE_FILES.items():
        assert [lookup(n).unit for n in nibs] == [unit, unit], file


def test_every_x0_load_has_its_port_write_four_words_later(items):
    # A first lane port or O's port under enable 0 takes a load; none takes one without a head.
    takers = [
        e for e, rows in LANE_SOURCES.items() if rows.get(0, ("",))[0] == LANE_LOAD
    ]
    assert takers == ["f236_237", "f240_241", "f244_245", "f248_249"]
    assert STORE_SOURCES[0][0] == LANE_LOAD
    takers.append("f252_254")
    loads = 0
    for item, data in items.items():
        words = decode_item(data, STOCK[item]).words
        heads = {i for i, w in enumerate(words) if w["x0op"] in X0_LANE_LOADS}
        takes = {i for i, w in enumerate(words) if any(w[e] == 0 for e in takers)}
        assert {i + LOAD_LAG for i in heads} == takes, item
        loads += len(heads)
    assert loads == 379


def test_every_stock_s0_0x13_hands_its_mask_to_f0_through_m(items):
    # The next word puts S0's held result on X0's bus (M under 0) and F0's port takes it.
    seen = 0
    for item, data in items.items():
        words = decode_item(data, STOCK[item]).words
        for i, word in enumerate(words):
            if word["f278_284"] != 0x13:
                continue
            nxt = words[i + 1]
            assert (nxt["f276"], nxt["f274_275"], nxt["f255"]) == (0, 0, 0), (item, i)
            seen += 1
    assert seen == 18


def test_the_store_file_enable_has_rows_only_for_the_values_run():
    # 5 and 6 were never run, so they have no row and do not lower the tier.
    enable = lookup("f252_254")
    assert sorted(enable.values) == [0, 1, 2, 3, 4, 7]
    assert enable.confidence is Confidence.VERIFIED
    assert PORTS["f252_254"] == "f179_181"


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
