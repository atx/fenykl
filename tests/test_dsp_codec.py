from collections import Counter
from dataclasses import replace

import pytest
from conftest import stock_image

from fenykl.dsp.disasm import WIDTH, listing, word_parts
from fenykl.dsp.fields import (
    ALU_BUS,
    CONTROL_FLAG,
    CONTROL_OPS,
    DATA_OPS,
    FIELDS,
    FORMATS,
    HEAD_VIEWS,
    HEADS,
    IDLE_OP,
    IDLE_VALUE,
    LANE_FILES,
    LANE_FUNCTIONS,
    LANE_LAG,
    LANE_LOAD,
    LANE_SOURCES,
    LOAD_LAG,
    NO_LANE,
    PB_FILES,
    PB_SOURCES,
    PORTS,
    SCALAR_READS,
    STAIRCASE,
    STORE_SOURCES,
    WORD_BYTES,
    X0_LANE_LOADS,
    X_IMMEDIATES,
    Bits,
    Confidence,
    Operands,
    _head_tier,
    _index,
    lane_file,
    lookup,
    op,
)
from fenykl.dsp.program import (
    SLOT_BYTES,
    STOCK,
    Program,
    ProgramFormatError,
    StockProgram,
    decode_item,
    encode_item,
    stock_items,
)
from fenykl.dsp.units import UNITS
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


@pytest.mark.parametrize(
    "edit, match",
    [
        ({"hi": 320}, "outside the word"),
        ({"idle": 2}, "does not fit"),
        ({"lo": 0, "hi": 1}, "bit 0 in both"),
        ({"lo": 1, "hi": 0}, "outside the word"),
    ],
)
def test_the_index_refuses_a_field_off_the_partition(edit, match):
    # f1 (bit 1) moved past the word's end, given an idle wider than itself, onto f0's bit and
    # turned around.
    fields = tuple(replace(f, **edit) if f.id == "f1" else f for f in FIELDS)
    with pytest.raises(ValueError, match=match):
        _index(fields)


def test_the_index_refuses_a_gap():
    with pytest.raises(ValueError, match=r"bits without a field: \[1\]"):
        _index(tuple(f for f in FIELDS if f.id != "f1"))


@pytest.mark.parametrize("name", ["f2_6", "f0x", "rb", "class"])
def test_a_name_never_reads_as_an_id_or_another_key(name):
    # f1 renamed: to another field's id, to an id-like name, to a taken name and to a keyword.
    fields = tuple(replace(f, name=name) if f.id == "f1" else f for f in FIELDS)
    with pytest.raises(ValueError, match=name):
        _index(fields)


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


def test_stock_words_rebuild_from_names_and_head_views(items):
    for item, data in items.items():
        for index, word in enumerate(decode_item(data, STOCK[item]).words):
            named = {f.label: v for f, v in word.changed()}
            assert Word.from_fields(named) == word, (item, index)
            rest = {k: v for k, v in named.items() if lookup(k).unit not in HEAD_VIEWS}
            built = Word.from_fields(rest).value
            for view in HEAD_VIEWS.values():
                built = view.encode(built, view.decode(word.value))
            assert built == word.value, (item, index)


def test_a_head_view_covers_its_unit():
    for unit, view in HEAD_VIEWS.items():
        span = 0
        for o in view.operands.values():
            span |= o.span
        assert span == sum(f.span for f in UNITS[unit].fields), unit


def head(unit: str, **operands: int) -> Word:
    return Word(HEAD_VIEWS[unit].encode(IDLE_VALUE, operands))


def test_a_head_view_composes_split_immediates():
    word = head("s0", imm=0xABCD)
    assert (word["f23_30"], word["f37_40"], word["f76_79"]) == (0xCD, 0xB, 0xA)
    word = head("s2", imm=0x15)  # f88_91's bit 0 keeps its idle 1
    assert (word["f88_91"], word["f53_56"]) == (0xB, 0x2)
    word = head("s1", op=0x04, imm=0x20)
    assert (word["f0"], word["f84_87"], word["f80_83"] >> 2) == (0, 0, 1)
    word = head("s3", imm=0x3ABC - 0x4000)
    assert (word["f92_95"], word["f31_34"] >> 2, word["f65_68"], word["f96_99"]) == (
        0xC,
        3,
        0xA,
        0xE,
    )
    assert HEAD_VIEWS["s3"].decode(word.value)["imm"] == -0x544


@pytest.mark.parametrize(
    "unit, operands, error",
    [
        ("s1", {"t": 1, "imm": 0}, "disagrees"),
        ("x1", {"b": 3, "k": 0x100}, "disagrees"),
        ("s2", {"imm": 64}, "does not fit 7 signed bits"),
        ("x0", {"k": -1}, "does not fit 11 bits"),
        ("s0", {"t": 1}, "no operand 't'"),
    ],
)
def test_a_head_view_rejects_bad_operands(unit, operands, error):
    with pytest.raises((ValueError, KeyError), match=error):
        head(unit, **operands)


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


def test_a_head_field_is_verified_once_every_value_has_a_verified_row():
    # S2's and S3's values all have verified rows and lags (S3's last, 0x6's lag); S0, S1 still have open rows.
    tiers = {
        u: lookup(f"f{lo}_{hi}").confidence
        for u, (lo, hi) in HEADS.items()
        if u[0] == "s"
    }
    assert tiers == {
        "s0": Confidence.INFERRED,
        "s1": Confidence.INFERRED,
        "s2": Confidence.VERIFIED,
        "s3": Confidence.VERIFIED,
    }


def test_each_lane_file_has_the_two_ports_of_its_nibbles():
    # Measured on the camera: L3 written through f250_251, L2 through f244_245, L0 and L1 by
    # their first ports.
    assert Counter(map(lane_file, LANE_SOURCES)) == dict.fromkeys(LANE_FILES, 2)
    assert [lane_file(e) for e in ("f236_237", "f240_241", "f244_245", "f250_251")] == [
        "L0",
        "L1",
        "L2",
        "L3",
    ]


def test_every_x0_load_has_its_port_write_four_words_later(items):
    # A first lane port or O's port under enable 0 takes a load; none takes one without a head.
    takers = [
        e for e, rows in LANE_SOURCES.items() if rows.get(0, ("",))[0] == LANE_LOAD
    ]
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
    assert sorted(STORE_SOURCES) == [0, 1, 2, 3, 4]
    assert lookup("f252_254").confidence is Confidence.VERIFIED
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
        (0x05, Operands(ra=0)),  # an op value that is no data op has no format
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


@pytest.mark.parametrize("edit", ["lag", "row"])
def test_a_head_is_verified_only_while_every_value_has_a_verified_row_and_lag(
    monkeypatch, edit
):
    # S2's values all have verified rows and lags; one open lag or a missing row leaves it
    # inferred.
    assert _head_tier("s2", 0xF) is Confidence.VERIFIED
    head = next(h for h, row in LANE_FUNCTIONS["s2"].items() if row[0] != NO_LANE)
    if edit == "lag":
        lag, _ = LANE_LAG["s2"][head]
        monkeypatch.setitem(LANE_LAG["s2"], head, (lag, Confidence.INFERRED))
    else:
        monkeypatch.delitem(LANE_FUNCTIONS["s2"], head)
    assert _head_tier("s2", 0xF) is Confidence.INFERRED


def test_a_word_repr_evaluates_back_to_the_word():
    # A register field prints decimal, the others hex, an unnamed one by its id.
    word = Word.from_fields({"op": 0x1F, "ra": 3, "imm": 0x12, "f35_36": 1, "s0a": 9})
    assert eval(repr(word), {"Word": Word}) == word


def test_bits_refuse_a_value_wider_than_themselves():
    with pytest.raises(ValueError, match="does not fit 4 bits"):
        Bits(lookup("imm"), 0, 3).put(IDLE_VALUE, 0x10)


SPEC = StockProgram(0, 0, 0, 4 * SLOT_BYTES, 2 * SLOT_BYTES, 2, "")
PROGRAM = Program((IDLE, Word(1)), bytes(range(8)))


def test_an_item_decodes_back_to_its_program():
    assert decode_item(encode_item(PROGRAM, SPEC), SPEC) == PROGRAM


@pytest.mark.parametrize(
    "at, match",
    [
        (None, "bytes, expected"),
        (4 * SLOT_BYTES - 1, "non-zero bytes after"),
        (WORD_BYTES, "slot 0 has bytes past the word"),
    ],
)
def test_an_item_off_the_layout_is_refused(at, match):
    data = bytearray(encode_item(PROGRAM, SPEC))
    if at is None:
        data.append(0)
    else:
        data[at] = 1
    with pytest.raises(ProgramFormatError, match=match):
        decode_item(bytes(data), SPEC)


@pytest.mark.parametrize(
    "program", [Program((IDLE,) * 3, bytes(8)), Program((IDLE,), bytes(4))]
)
def test_a_program_that_does_not_fit_the_layout_is_refused(program):
    # Three words past the constants at slot 2, or constants of the wrong size.
    with pytest.raises(ProgramFormatError, match="does not fit"):
        encode_item(program, SPEC)


def test_an_image_that_is_no_stock_firmware_is_refused():
    with pytest.raises(ProgramFormatError, match="not the stock"):
        stock_items(bytes(4096))


def test_a_head_whose_reads_are_not_known_shows_every_operand_of_its_view():
    # S0 0x40's reads are not known: its view's s and imm show; 0x1 does not read s, which
    # shows raw.
    assert word_parts(Word.from_fields({"s0op": 0x40, "s0a": 3, "s0s": 5}))[1] == (
        "s0: op=40~ a=3! s=5! imm=0xf3ff!"
    )
    assert word_parts(Word.from_fields({"s0op": 0x1, "s0a": 3, "s0s": 5}))[1] == (
        "s0: op=1~ a=3! s0s=5!"
    )


def test_a_listing_wraps_a_long_word_under_its_index():
    word = Word.from_fields({f.id: 1 for f in FIELDS if f.kind == "reg"})
    lines = listing(Program((word,))).splitlines()[3:]
    assert len(lines) > 1
    assert all(len(line) <= WIDTH for line in lines)
    assert all(line.startswith(" " * 12) and line[12] != " " for line in lines[1:])
