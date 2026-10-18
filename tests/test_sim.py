"""SDK runtime, generated code and plugin parts in the GDB simulator (tests/sim/), against mock
firmware functions placed at their real firmware addresses. Every C test program exits 0 on success
(otherwise a failure mask or 1 + the failed check, see its source).

- api (test_api.c): crt0, .bss clearing, FENYKL_TASK and every generated firmware thunk, each called
  once against the mock of its ABI shape (mocks.S: one ARGS_ macro per argument pattern; the return
  kind and the CALL operands come from the declaration). A new firmware.toml function needs no new
  mock unless its argument pattern is new.
- event: the event layer (sdk/lib/event.c) through the SDK's appevent_post hook on the mock at its
  real address: handler order, edits, consume, inject, nested entries, add errors, remove, CPU1.
- key: the key layer (sdk/lib/key.c) over the event layer, on a recording appevent_post mock:
  claim, SHORT, LONG, the long_stock replay, the tick and its hand-off, timer wrap, dials, busy.
- main_fmt: the main plugin's distance formatter (sdk/projects/main/main_fmt.c): fixed cases at
  every unit bound and rounding carry, and a sweep checking digits, units, half-up rounding and the
  split into a number line of at most 4 characters over the unit line.
- main_dof: the main plugin's depth of field (sdk/projects/main/main_dof.c): fixed cases against
  ExifTool's Composite:DOF formula (kit lens distances, s = inf, far = inf), the f-number from Av,
  invalid inputs, and a sweep checking near <= s <= far and that stopping down deepens and zooming
  in shallows the depth.
- main_batt: the main plugin's battery model (sdk/projects/main/main_batt.c): the open-circuit
  curve at its points, between them and clamped outside, the counts-to-mV scale, no flicker with
  noisy reads or AF dips, and a slow discharge stepping down 1 % at a time.
- feat_focus: the main plugin's focus show/hide and outlier rules
  (sdk/projects/main/feat_focus_filter.c) over a sequence of reads.
- main_fmeter: the main plugin's focus meter (sdk/projects/main/main_fmeter.c): area choice
  (single target, centre fallback, all-target maximum), the fixed-point log2, the absolute scale's
  cell boundaries and the half-height pattern offset, the peak cell, the hold, MF.
- main_shot: the main plugin's screenshot pixel work (sdk/projects/main/main_shot.c): YCbCr to RGB
  against a BT.601 reference, palettes, live view placement, alpha blending, 4-bpp nibble order,
  layer order, the 16:9 stretch and the PPM header.
- main_rbswap: the main plugin's R/B swap hook (sdk/projects/main/main_rbswap_hook.c) on img_exec's
  real entry bytes: option off, P x M on live view and stills, the class and identity filters, the
  restore and a matrix kept when it changed during the pass, no IMG_PARAM, the counters.
- afc_sm: the contrast client's attach/detach state machine (sdk/lib/afc_sm.c): boot delay, settle
  and cooldown, the stop on a gate, the callback's stop, a stop pending while live view is off, and
  every error path ending passive.
- ui: the settings page (sdk/lib/ui.c) over fake option tables: the flat list (more than 20
  options rejected), the stock list slot 0xF for 1, 7, 8 and 20 rows, cursor wrap and page flips,
  BOOL/ENUM/INT editing with the value text, the "not saved" header with the real cfg library
  saving to a mock card, and the borrowed lang ids on a fake language table, given back on every
  way out of the page.
- birddetect_hook: birddetect's fd_hw_shutdown hook (sdk/projects/birddetect/hook.c) on the
  function's real entry bytes: install, a live cycle and a still call through the original, remove.
- cpu1: the API contract of the CPU1 worker runtime (sdk/arch/epl3/cpu1.c): start, state,
  stop and its timeout, restart, refusals; against a fake TCB and mocks of OlyTaskStart/Stop/Sleep.
- usb: a boot started by the USB cable skips plugin_main unless plugin.toml sets run_on_usb.
- hooks: the hook generator and runtime, plus a synthetic stack-argument hook target.
- slots: callbacks, slots and cloned vtables, plus synthetic callback shapes.
"""

import re
from pathlib import Path

import pytest
from conftest import (
    DRAM_ALIAS,
    EPL3,
    POWERON,
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

from fenykl import fwapi, sdk

pytestmark = needs_toolchain

PROJECTS = sdk.SDK / "projects"
FUNCTIONS = [f.name for f in fwapi.load(EPL3.dir / "firmware.toml").functions]
# DRAM with the mocks and the payload.
DRAM = region("0x6e600000,0xb00000")
# The timer block (timer_now 0x98900074); the core id 0x8050 is in the simulator's default memory.
TIMER = region("0x98900000,0x100")
# The GUI state machine's super/state bytes (0x60702278); a 0x100 region left them unmapped.
GUI_SM = region("0x60702000,0x1000")
SUPPORT = [
    "dcache_purge_all_irqsafe", "psw_get", "irq_disable_all", "irq_mask_restore",
    "cache_invalidate", "cache_enable", "cache_busy",
]  # fmt: skip
TCB_TABLE = 0x6E690A50
# tests/sim/log/fw.S, cfg/fw.S and ui/fw.S
UI_MOCKED = [
    "psw_get", "irq_disable_all", "irq_mask_restore", "ui_key_get_state", "sprintf",
    "rpc_fopen", "rpc_fclose", "rpc_fread", "rpc_fwrite", "rpc_fremove", "malloc", "free",
    "OlyTaskSleep", "dcache_purge_all_irqsafe", "gui_data_set",
]  # fmt: skip


MOCKS = SIM / "mocks.S"
# Functions with a hand mock in mocks.S; test_api.c checks them itself.
HAND_MOCKED = re.findall(r"^\tFW\t(\w+)$", MOCKS.read_text(), re.MULTILINE)
API_FIRST = 10  # test_api.c
VARIADIC_WORDS = 8  # ARGS_aa_va in mocks.S


def _api_checks() -> list[tuple[int, fwapi.Function]]:
    funcs = fwapi.load(EPL3.dir / "firmware.toml").functions
    return list(enumerate((f for f in funcs if f.name not in HAND_MOCKED), API_FIRST))


def _shape(func: fwapi.Function) -> str:
    """The argument pattern of func, the ARGS_ macro of its mock in mocks.S."""
    pattern = "".join(a.cls for a in func.args) or "none"
    return f"{pattern}_va" if func.variadic else pattern


def _return_kind(func: fwapi.Function) -> str:
    if func.returns == "void":
        return "v"
    return "a" if func.returns.endswith("*") else "d"


def _api_mocks(workdir: Path) -> Path:
    """mocks.S with a shape mock for every firmware function without a hand mock."""
    lines = [f'#include "{MOCKS}"']
    for check, f in _api_checks():
        operands = [
            f.name,
            _shape(f),
            _return_kind(f),
            str(check),
            str(f.frame),
            *f.saves,
        ]
        lines.append(f"\tMOCK\t{', '.join(operands)}")
    path = workdir / "mocks.S"
    path.write_text("\n".join(lines) + "\n")
    return path


def _api_calls(workdir: Path) -> Path:
    """test_api.c with api_calls(): every thunk called once with distinct argument words."""
    body = []
    for check, f in _api_checks():
        n = len(f.args) + (VARIADIC_WORDS if f.variadic else 0)
        words = [f"0x{0xA5000000 | check << 8 | i:08x}u" for i in range(n)]
        args = [f"({a.ctype}){w}" for a, w in zip(f.args, words)] + words[len(f.args) :]
        call = f"fw_{f.name}({', '.join(args)})"
        returns = _return_kind(f) != "v"
        body.append(
            f"\tPRESERVED({check}, {'r = (uint32_t)' if returns else ''}{call});"
        )
        body.append(f"\tCALLED({check});")
        if words:
            body.append(f"\tEXPECT({check}, {', '.join(words)});")
        if returns:
            body.append(f"\tRESULT({check}, r);")
    path = workdir / "api.c"
    path.write_text(
        f'#include "{SIM / "test_api.c"}"\n\n'
        "static void api_calls(void)\n{\n\tuint32_t r;\n\n" + "\n".join(body) + "\n}\n"
    )
    return path


def test_every_thunk_against_its_shape_mock(tmp_path):
    sources = [_api_calls(tmp_path), SIM / "sim_start.S", _api_mocks(tmp_path)]
    result = run_in_simulator(
        tmp_path, "apisim", sources, [DRAM, POWERON], mocked=FUNCTIONS
    )
    assert_exit(result, 0, f"tests/sim/test_api.c, {tmp_path / 'api.c'}")


PROGRAMS = {
    "event": {
        "sources": [
            SIM / "event" / "test_event.c",
            SIM / "hooks" / "appevent.S",
            SIM / "hooks" / "sim_start.S",
            SIM / "hooks" / "support.S",
        ],
        "memory": [DRAM_ALIAS, POWERON],
        "mocked": ["appevent_post", *SUPPORT],
    },
    "key": {
        "sources": [
            SIM / "key" / "test_key.c",
            SIM / "key" / "appevent.S",
            SIM / "hooks" / "sim_start.S",
            SIM / "hooks" / "support.S",
        ],
        "memory": [DRAM_ALIAS, TIMER, GUI_SM, POWERON],
        "mocked": ["appevent_post", *SUPPORT],
    },
    "main_fmt": {
        "sources": [
            SIM / "main" / "test_main_fmt.c",
            PROJECTS / "main" / "main_fmt.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "main_dof": {
        "sources": [
            SIM / "main" / "test_main_dof.c",
            PROJECTS / "main" / "main_dof.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "main_batt": {
        "sources": [
            SIM / "main" / "test_main_batt.c",
            PROJECTS / "main" / "main_batt.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "feat_focus": {
        "sources": [
            SIM / "main" / "test_feat_focus.c",
            PROJECTS / "main" / "feat_focus_filter.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "main_fmeter": {
        "sources": [
            SIM / "main" / "test_main_fmeter.c",
            PROJECTS / "main" / "main_fmeter.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "main_shot": {
        "sources": [
            SIM / "main" / "test_main_shot.c",
            PROJECTS / "main" / "main_shot.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "main_rbswap": {
        "sources": [
            SIM / "main" / "test_main_rbswap.c",
            PROJECTS / "main" / "main_rbswap_hook.c",
            SIM / "hooks" / "sim_start.S",
            SIM / "main" / "img_exec.S",
            SIM / "hooks" / "support.S",
        ],
        "memory": [DRAM_ALIAS, POWERON],
        "mocked": ["img_exec", *SUPPORT],
        "hooks": ["img_exec"],
        "cpu1_unproven": ["img_exec"],
    },
    "afc_sm": {
        "sources": [
            SIM / "afc" / "test_afc_sm.c",
            SIM / "hooks" / "sim_start.S",
        ],
        "memory": [DRAM, POWERON],
    },
    "ui": {
        "sources": [
            SIM / "ui" / "test_ui.c",
            SIM / "log" / "fw.S",
            SIM / "cfg" / "fw.S",
            SIM / "ui" / "fw.S",
            SIM / "hooks" / "sim_start.S",
        ],
        # DRAM, the timer, fat_mounted, the language table pointer, the GUI app pointer, the
        # current panel and menu flags, the GUI state, SS_MENU_MAIN's event map (empty).
        "memory": [
            DRAM_ALIAS,
            TIMER,
            POWERON,
            region("0x6059c000,0x1000"),
            region("0x605fc000,0x1000"),
            region("0x6034e000,0x1000"),
            region("0x60352000,0x2000"),
            region("0x60702000,0x1000"),
            region("0x6f1c9000,0x1000"),
        ],
        "mocked": UI_MOCKED,
    },
    "birddetect_hook": {
        "sources": [
            SIM / "birddetect" / "test_hook.c",
            PROJECTS / "birddetect" / "hook.c",
            PROJECTS / "birddetect" / "inject.c",
            SIM / "hooks" / "sim_start.S",
            SIM / "birddetect" / "fw_hook.S",
            SIM / "hooks" / "support.S",
        ],
        # The raw candidate list and the live cycle counter.
        "memory": [
            DRAM_ALIAS,
            region("0x60608000,0x2000"),
            region("0x6034c000,0x1000"),
            TIMER,
            POWERON,
        ],
        "mocked": ["fd_hw_shutdown", *SUPPORT],
        "hooks": ["fd_hw_shutdown"],
    },
    "cpu1": {
        "sources": [
            SIM / "cpu1" / "test_cpu1.c",
            SIM / "cpu1" / "worker.c",
            SIM / "cpu1" / "fw.S",
            SIM / "hooks" / "sim_start.S",
        ],
        "cpu1_sources": [SIM / "cpu1" / "worker.c"],
        "memory": [DRAM_ALIAS, region("0xa07f0000,0x2000"), TIMER, POWERON],
        "mocked": [
            "OlyTaskStart",
            "OlyTaskStop",
            "OlyTaskSleep",
            "dcache_purge_all_irqsafe",
        ],
        "ldflags": [f"-Wl,--section-start=.fw.tcbtab=0x{TCB_TABLE:x}"],
    },
}


@pytest.mark.parametrize("name", list(PROGRAMS))
def test_program_in_the_simulator(name, tmp_path):
    program = PROGRAMS[name]
    result = run_in_simulator(tmp_path, f"{name}sim", **program)
    assert_exit(result, 0, program["sources"][0].relative_to(SIM.parent.parent))


POWERON_USB = 0x400
RAN = 0
SKIPPED = 7


@pytest.mark.parametrize(("run_on_usb", "expected"), [(False, SKIPPED), (True, RAN)])
def test_usb_boot_skips_plugin_main(tmp_path, run_on_usb, expected):
    usb = SIM / "usb"
    result = run_in_simulator(
        tmp_path,
        "usbsim",
        [usb / "test_usb.c", usb / "sim_start.S", _api_mocks(tmp_path)],
        [DRAM, POWERON],
        mocked=FUNCTIONS,
        ldflags=[f"-Wl,--defsym=_sim_poweron_factor=0x{POWERON_USB:x}"],
        run_on_usb=run_on_usb,
    )
    assert_exit(result, expected, "tests/sim/usb/sim_start.S")


STACKSUM_ADDR = 0x6E650000
# A synthetic stack-argument hook target.
STACKSUM_TOML = f"""
[stacksum]
address = 0x{STACKSUM_ADDR:08X}
saves = []
frame = 0x10
evidence = "high"
returns = "int"
args = ["d unsigned a", "d unsigned b", "d unsigned c", "d unsigned d"]
note = "synthetic test target: a + b + c + d, c/d on the stack"

[stacksum.hook]
entry = "call"
relocate = "42 08 46 0c f1 d0"
cpu1 = "cpu0-only"
cpu1_evidence = "synthetic simulator-only target"
note = "mov d0,(8,sp); mov d1,(12,sp); mov a0,d0: no interior branch target, no stored pointer"
"""


# A sim-only name for the appevent_post mock (tests/sim/hooks/fw.S): the SDK binds appevent_post itself
# (target.toml sdk_hooks), so the hook mechanism test hooks the same bytes under another name.
SIMEVENT_TOML = """
[simevent_post]
address = 0x6EA6D213
saves = []
frame = 0x20
evidence = "high"
returns = "int"
args = ["d unsigned event", "d unsigned param", "a unsigned flag"]
note = "sim-only alias of the appevent_post mock: 3*event + param + flag"

[simevent_post.hook]
entry = "call"
relocate = "42 08 46 0c f1 d0"
cpu1 = "cpu0-only"
cpu1_evidence = "synthetic simulator-only target"
note = "appevent_post's entry bytes"
"""


def _firmware_toml(tmp_path: Path, extra: str) -> Path:
    """The real firmware.toml plus sim-only declarations."""
    toml = tmp_path / "firmware.toml"
    toml.write_text((EPL3.dir / "firmware.toml").read_text() + extra)
    return toml


def test_hooks_run_in_the_simulator(tmp_path):
    hooks = SIM / "hooks"
    hooked = ["simevent_post", "stacksum", "gpi_flush", "mov_yc_resize_pass"]
    result = run_in_simulator(
        tmp_path,
        "hooksim",
        [
            hooks / "test_hooks.c",
            hooks / "sim_start.S",
            hooks / "appevent.S",
            hooks / "fw.S",
            hooks / "support.S",
        ],
        # gpi_draw_buf/gpi_dirty and 0x6034CACC are read by the relocated entries of gpi_flush and
        # mov_yc_resize_pass.
        [DRAM_ALIAS, region("0x6034c000,0x2000"), POWERON],
        mocked=["appevent_post", *hooked[1:], *SUPPORT],
        firmware=_firmware_toml(tmp_path, STACKSUM_TOML + SIMEVENT_TOML),
        hooks=hooked,
        cpu1_unproven=["gpi_flush", "mov_yc_resize_pass"],
    )
    assert_exit(result, 0, "tests/sim/hooks/test_hooks.c")


SLOTS_TOML = """
[callbacks.cbmix]
entry = "calls"
evidence = "high"
returns = "int"
args = ["d unsigned a", "a unsigned *p", "d unsigned b", "a unsigned *q", "d unsigned c", "a unsigned *r"]
note = "synthetic: a@D0 p@A0 b@D1 q@A1 c@(4,SP) r@(8,SP)"

[callbacks.cbptr]
entry = "calls"
evidence = "high"
returns = "unsigned *"
args = ["a unsigned *base", "d unsigned index"]
note = "synthetic: pointer result in A0"

[callbacks.cbcall]
entry = "call"
saves = ["d2", "a2"]
frame = 12
evidence = "high"
returns = "int"
args = ["d unsigned a", "d unsigned b", "d unsigned c"]
note = "synthetic: CALL-entered with [d2,a2],12, c@(4,SP)"
"""


def test_callbacks_slots_and_vtables_run_in_the_simulator(tmp_path):
    """The real slot and vtable declarations are used; their stock values are mocks placed at their
    real addresses. Synthetic callbacks add stack arguments, a pointer result and a CALL entry."""
    firmware = _firmware_toml(tmp_path, SLOTS_TOML)
    api = fwapi.load(firmware)
    stock = {
        "change_cb": api.slots["usrset_change_cb"].expected,
        "hfw_get": api.slots["hfw_setting_proxy_get"].expected,
        "hfw_set": api.slots["hfw_setting_proxy_set"].expected,
        "vtable": api.vtables["hfw_setting_proxy"].address,
    }
    slots = ["appevent_hook1", "usrset_change_cb", "hfw_setting_proxy_get"]
    result = run_in_simulator(
        tmp_path,
        "slotsim",
        [
            SIM / "slots" / "test_slots.c",
            SIM / "slots" / "fw.S",
            SIM / "hooks" / "sim_start.S",
            SIM / "hooks" / "support.S",
        ],
        # DRAM with the mocks and the payload, the RAM words of the hook-1 and change-callback
        # slots.
        [
            region("0x6e600000,0xa00000"),
            region("0x605fd000,0x1000"),
            region("0x6034c000,0x1000"),
            POWERON,
        ],
        mocked=SUPPORT[:4],
        ldflags=[f"-Wl,--section-start=.fw.{n}=0x{a:x}" for n, a in stock.items()],
        firmware=firmware,
        slots=slots,
        vtables=["hfw_setting_proxy"],
        cpu1_unproven=[*slots, "hfw_setting_proxy"],
    )
    assert_exit(result, 0, "tests/sim/slots/test_slots.c")
