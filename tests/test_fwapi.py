from pathlib import Path

import pytest
from conftest import EPL3, needs_toolchain, stock_image

from fenykl import fwapi, sdk
from fenykl.firmware import unpack_firmware


def func(args: list[str], saves=(), frame=4, returns="int") -> fwapi.Function:
    return fwapi._parse_function(
        "f",
        {
            "address": 0x6E600000,
            "saves": list(saves),
            "frame": frame,
            "evidence": "high",
            "returns": returns,
            "args": args,
            "note": "",
        },
    )


def test_classes_are_counted_independently():
    p = fwapi.plan(func(["a void *buf", "d int size", "d int n", "a void *fp"]))
    assert p.regs == {"a0": 0, "d0": 1, "d1": 2, "a1": 3}
    assert p.stack == ()


def test_exhausted_class_goes_to_the_stack_in_parameter_order():
    p = fwapi.plan(
        func(
            [
                "a char *name",
                "a fw_task_entry e",
                "d int mode",
                "d int x",
                "d int prio",
                "d int size",
                "a void *base",
            ]
        )
    )
    assert p.regs == {"a0": 0, "a1": 1, "d0": 2, "d1": 3}
    assert p.stack == (4, 5, 6)


def test_variadic_last_named_goes_to_the_first_stack_slot():
    p = fwapi.plan(func(["d int level", "a const char *fmt", "..."]))
    assert p.regs == {"d0": 0}
    assert p.stack == (1, *range(2, 2 + fwapi.VARIADIC_WORDS))


def test_register_argument_after_stack_argument_is_refused():
    with pytest.raises(fwapi.ApiError, match="follow a stack argument"):
        fwapi.plan(func(["d int a", "d int b", "d int c", "a void *p"]))


@pytest.mark.parametrize(
    ("arg", "match"),
    [
        ("d char *p", "integer class"),
        ("d short x", "integer class"),
        ("a short x", "pointer or 32-bit integer"),
        ("x int y", "cannot parse"),
    ],
)
def test_bad_argument_declarations(arg, match):
    with pytest.raises(fwapi.ApiError, match=match):
        func([arg])


def test_thunk_reads_gcc_stack_arguments_above_its_own_frame():
    asm = fwapi.thunk_asm(
        func(["d int a", "a void *p", "d int b"], saves=["d2"], frame=8)
    )
    assert asm.splitlines()[4:] == [
        "\tadd -4,sp",
        "\tmov d1,a0",
        "\tmov (16,sp),d1",
        "\tcall fw_addr_f,[d2],8",
        "\tret [],4",
    ]


# ---- Cores ----


def cored(**fields) -> fwapi.Function:
    d = {
        "address": 0x6E600000,
        "saves": [],
        "frame": 4,
        "evidence": "high",
        "returns": "int",
        "args": ["d unsigned n"],
        "note": "",
    }
    return fwapi._parse_function("f", d | fields)


@pytest.mark.parametrize(
    "fields, match",
    [
        ({"cores": ["cpu1"], "cores_evidence": "x"}, "cores must be"),
        ({"cores": ["cpu0", "cpu2"], "cores_evidence": "x"}, "cores must be"),
        ({"cores": ["cpu0", "cpu0"], "cores_evidence": "x"}, "cores must be"),
        ({"cores": "cpu0", "cores_evidence": "x"}, "cores must be"),
        ({"cores": ["cpu0", "cpu1"]}, "go together"),
        ({"cores_evidence": "x"}, "go together"),
        ({"cores": ["cpu0", "cpu1"], "cores_evidence": " "}, "cores_evidence must say"),
    ],
)
def test_bad_cores_declarations(fields, match):
    with pytest.raises(fwapi.ApiError, match=match):
        cored(**fields)


def test_only_cpu0_only_prototypes_carry_the_cpu1_error():
    assert fwapi.prototype(cored()).endswith(' FENYKL_CPU0_ONLY("fw_f");')
    explicit = cored(cores=["cpu0"], cores_evidence="pool allocator")
    assert 'FENYKL_CPU0_ONLY("fw_f")' in fwapi.prototype(explicit)
    both = cored(cores=["cpu1", "cpu0"], cores_evidence="per core")
    assert both.cores == ("cpu0", "cpu1")
    assert fwapi.prototype(both) == "int fw_f(unsigned n);"


# ---- Hooks ----

RELOCATABLE_SPAN = "42 08 46 0c f1 d0"  # mov d0,(8,sp); mov d1,(12,sp); mov a0,d0


def hooked(args=("d unsigned e",), returns="int", relocate=RELOCATABLE_SPAN, **hook):
    base = {
        "address": 0x6E600000,
        "saves": [],
        "frame": 0x20,
        "evidence": "high",
        "returns": returns,
        "args": list(args),
        "note": "",
        "hook": {
            "entry": "call",
            "relocate": relocate,
            "cpu1": "cpu0-only",
            "cpu1_evidence": "test",
            "note": "test",
            **hook,
        },
    }
    return fwapi._parse_function("f", base)


def test_hook_requires_a_cpu1_scope_with_evidence():
    with pytest.raises(fwapi.ApiError, match="cpu1 must be one of"):
        hooked(cpu1="maybe")
    with pytest.raises(fwapi.ApiError, match="cpu1_evidence"):
        hooked(cpu1="unproven", cpu1_evidence="  ")


def test_hook_refuses_a_non_call_entry():
    with pytest.raises(fwapi.ApiError, match="only 'call'"):
        hooked(entry="jmp")


def test_hook_refuses_a_variadic_target():
    with pytest.raises(fwapi.ApiError, match="variadic"):
        hooked(args=["a const char *fmt", "..."])


@pytest.mark.parametrize("relocate", ["42 08", "42 08 46 0c"])
def test_hook_refuses_too_short_a_relocate_span(relocate):
    with pytest.raises(fwapi.ApiError, match="relocate must be"):
        hooked(relocate=relocate)


@needs_toolchain
def test_relocator_refuses_a_span_that_ends_inside_an_instruction(tmp_path):
    # 'fc ac ..' (movhu abs32) is 6 bytes; cut to 5 bytes -> no instruction boundary.
    func = hooked(relocate="42 08 46 0c fc ac be 1a 69")
    with pytest.raises(fwapi.ApiError, match="inside an instruction"):
        fwapi.relocated_instructions(func, EPL3.tool("objdump"), tmp_path)


@needs_toolchain
def test_relocator_refuses_control_flow_in_the_span(tmp_path):
    # ui_key_dispatch's entry: mov 0,d1 (5 byte reg), then a conditional branch 'c9 05'.
    func = hooked(relocate="82 aa 00 c9 05")
    with pytest.raises(fwapi.ApiError, match="cannot relocate"):
        fwapi.relocated_instructions(func, EPL3.tool("objdump"), tmp_path)


@needs_toolchain
def test_relocator_refuses_a_special_register_operand(tmp_path):
    # mov d1,psw (f2 f7) touches PSW: not position-independent in a trampoline.
    func = hooked(relocate="42 08 46 0c f2 f7")
    with pytest.raises(fwapi.ApiError, match="cannot relocate"):
        fwapi.relocated_instructions(func, EPL3.tool("objdump"), tmp_path)


@needs_toolchain
def test_relocator_accepts_the_reviewed_span(tmp_path):
    insns = fwapi.relocated_instructions(hooked(), EPL3.tool("objdump"), tmp_path)
    assert insns == ["mov d0,(8,sp)", "mov d1,(12,sp)", "mov a0,d0"]


def test_select_hooks_needs_an_acknowledgement_for_an_unproven_cpu1_scope():
    api = fwapi.Api([hooked(cpu1="unproven")], {}, {}, {}, {})
    with pytest.raises(fwapi.ApiError, match="CPU1 never runs it"):
        fwapi.select(api, hooks=["f"])
    sel = fwapi.select(api, hooks=["f"], cpu1_unproven=["f"])
    assert [f.name for f in sel.hooks] == ["f"]


def test_select_hooks_rejects_an_undeclared_target():
    api = fwapi.load(sdk.target("epl3").dir / "firmware.toml")
    with pytest.raises(fwapi.ApiError, match="no hook declared"):
        fwapi.select(api, hooks=["dbg_puts"])


def test_declared_relocate_bytes_match_the_stock_image():
    """Every hook's relocate span must be the bytes actually at the entry in the stock firmware."""
    fw = unpack_firmware(stock_image())
    code = fw.blocks[fw.find_block("main-code-01")].body
    base = 0x6E601420
    for f in fwapi.load(sdk.target("epl3").dir / "firmware.toml").functions:
        if f.hook is None:
            continue
        off = f.address - base
        assert code[off : off + len(f.hook.relocate)] == f.hook.relocate, f.name


# ---- Callbacks, slots and vtables ----

EPL3_TOML = sdk.target("epl3").dir / "firmware.toml"
CALLBACK = """
[callbacks.cb]
entry = "calls"
evidence = "high"
returns = "int"
args = ["a void *self", "d unsigned id"]
note = ""
"""
SLOT = """
[slots.s]
address = 0x60000008
callback = "cb"
expected = 0x6E600000
cpu1 = "unproven"
cpu1_evidence = "test"
note = ""
"""
VTABLE = """
[vtables.vt]
address = 0x60000000
count = 4
cpu1 = "cpu0-only"
cpu1_evidence = "test"
note = ""
"""


def load_toml(tmp_path: Path, text: str) -> fwapi.Api:
    path = tmp_path / "firmware.toml"
    path.write_text(text)
    return fwapi.load(path)


@pytest.mark.parametrize(
    "edit, match",
    [
        (('entry = "calls"', 'entry = "jmp"'), "entry must be"),
        # "call" needs the CALL operands, "calls" must not have them
        (('entry = "calls"', 'entry = "call"'), "fields must be exactly"),
        (
            ('entry = "calls"', 'entry = "calls"\nsaves = []\nframe = 0'),
            "fields must be",
        ),
        (('"d unsigned id"]', '"d unsigned id", "..."]'), "variadic"),
        (('returns = "int"', 'returns = "long long"'), "return type"),
    ],
)
def test_bad_callback_declarations(tmp_path, edit, match):
    with pytest.raises(fwapi.ApiError, match=match):
        load_toml(tmp_path, CALLBACK.replace(*edit))


@pytest.mark.parametrize(
    "edit, match",
    [
        (('callback = "cb"', 'callback = "nope"'), "no \\[callbacks.nope\\]"),
        (("0x60000008", "0x60000006"), "not word-aligned"),
        (('cpu1 = "unproven"', 'cpu1 = "maybe"'), "cpu1 must be one of"),
        (('cpu1_evidence = "test"', 'cpu1_evidence = " "'), "cpu1_evidence"),
        (("[vtables.vt]", "[vtables.s]"), "names used twice"),
    ],
)
def test_bad_slot_declarations(tmp_path, edit, match):
    text = (CALLBACK + SLOT + VTABLE).replace(*edit)
    with pytest.raises(fwapi.ApiError, match=match):
        load_toml(tmp_path, text)


def test_select_gates_slots_and_vtables_like_hooks(tmp_path):
    api = load_toml(tmp_path, CALLBACK + SLOT + VTABLE)
    with pytest.raises(fwapi.ApiError, match="s: it is not proven"):
        fwapi.select(api, slots=["s"])
    with pytest.raises(fwapi.ApiError, match="no vtable declared"):
        fwapi.select(api, vtables=["s"])
    with pytest.raises(fwapi.ApiError, match="cpu1_unproven lists vt2"):
        fwapi.select(api, vtables=["vt"], cpu1_unproven=["vt2"])
    sel = fwapi.select(api, slots=["s"], vtables=["vt"], cpu1_unproven=["s"])
    assert [s.name for s in sel.slots] == ["s"]


def test_slot_in_a_selected_vtable_gets_its_clone_index(tmp_path):
    api = load_toml(tmp_path, CALLBACK + SLOT + VTABLE)
    with_vt = fwapi.header(
        api, fwapi.select(api, slots=["s"], vtables=["vt"], cpu1_unproven=["s"])
    )
    assert "fenykl_vtable_vt.clone[2] = FENYKL_CB(cb, fn)" in with_vt
    alone = fwapi.header(api, fwapi.select(api, slots=["s"], cpu1_unproven=["s"]))
    assert "FENYKL_VTABLE_SET_s" not in alone


def test_call_thunk_reads_gcc_arguments_after_the_target():
    """fw_call_<sig>(fn, ...): fn is GCC argument 1, so every signature argument moves up by one."""
    cb = fwapi._parse_callback(
        "cb",
        {
            "entry": "call",
            "saves": ["d2"],
            "frame": 8,
            "evidence": "high",
            "returns": "int",
            "args": ["a void *p", "d unsigned a", "d unsigned b", "d unsigned c"],
            "note": "",
        },
    )
    asm = fwapi.call_thunk_asm(cb).splitlines()
    # frame 12: return slot, c on the stack, saved A2. GCC: fn D0, p D1, a (12), b (16), c (20).
    assert asm[4:13] == [
        "\tadd -12,sp",
        "\tmov a2,(8,sp)",
        "\tmov d0,a2",
        "\tmov (32,sp),a0",
        "\tmov a0,(4,sp)",
        "\tmov d1,a0",
        "\tmov (24,sp),d0",
        "\tmov (28,sp),d1",
        "\tcall .Lfw_call_cb_jmp,[d2],8",
    ]


def test_declared_slots_and_vtables_match_the_stock_image():
    """A slot in the loaded image holds its stock value; a vtable has exactly `count` code pointers."""
    fw = unpack_firmware(stock_image())
    code = fw.blocks[fw.find_block("main-code-01")].body
    base = 0x6E601420

    def word(address: int) -> int:
        return int.from_bytes(code[address - base : address - base + 4], "little")

    def is_code(value: int) -> bool:
        return base <= value < base + len(code)

    api = fwapi.load(EPL3_TOML)
    for slot in api.slots.values():
        if base <= slot.address < base + len(code):
            assert word(slot.address) == slot.expected, slot.name
    for vt in api.vtables.values():
        assert all(is_code(word(vt.address + 4 * i)) for i in range(vt.count)), vt.name
        assert not is_code(word(vt.address + 4 * vt.count)), vt.name


# ---- Data ----

DATA = """
[data.gui_super]
address = 0x60702278
type = "u8"
evidence = "high"
note = "gui_sm super state"
"""


@pytest.mark.parametrize(
    "edit, match",
    [
        (("[data.gui_super]", "[data.GUI_SUPER]"), "name must be lower case"),
        (("0x60702278", "0x160702278"), "not 32-bit"),
        (('evidence = "high"', 'evidence = "sure"'), "evidence must be one of"),
        (('type = "u8"', 'type = " "'), "type must say"),
        (('note = "gui_sm super state"', ""), "fields must be exactly"),
    ],
)
def test_bad_data_declarations(tmp_path, edit, match):
    with pytest.raises(fwapi.ApiError, match=match):
        load_toml(tmp_path, DATA.replace(*edit))


def test_data_becomes_an_unsigned_address_constant(tmp_path):
    api = load_toml(tmp_path, DATA)
    header = fwapi.header(api, fwapi.select(api))
    assert "/* gui_super, u8, evidence high: gui_sm super state */" in header
    assert "#define FW_GUI_SUPER 0x60702278u" in header
