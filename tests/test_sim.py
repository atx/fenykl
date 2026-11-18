"""SDK runtime, generated code and plugin parts in the GDB simulator (tests/sim/), against mock
firmware functions placed at their real firmware addresses. Every C test program exits 0 on success
(otherwise a failure mask or 1 + the failed check, see its source).

- main_fmt: the main plugin's distance formatter (sdk/projects/main/main_fmt.c): fixed cases at
  every unit bound and rounding carry, and a sweep checking digits, units, half-up rounding and the
  split into a number line of at most 4 characters over the unit line.
- hud_rle: the HUD library's fmt 0x03 bitmap encoder (sdk/lib/hud_rle.c), read back with a decoder
  written from the format: pixels, runs within rows, the stock size rule, the buffer bounds.
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
- main_rbswap_base: the main plugin's E-M10 II R/B swap registry work
  (sdk/projects/main/main_rbswap_base.c) on a fake property registry: first store under the
  interrupt mask, no store while the copy holds, a rebuilt registry, a stock changed in place, a slot
  moved to the override table (also one holding the copy), invalid objects and entries refused, the
  restore.
- afc_sm: the contrast client's attach/detach state machine (sdk/lib/afc_sm.c): boot delay, settle
  and cooldown, the stop on a gate, the callback's stop, a stop pending while live view is off, and
  every error path ending passive.
- hooks_em10ii: the E-M10 II hook runtime (sdk/arch/em10ii/hooks.c) on the real appevent_post and
  cw_updateDisplay spans: install, calls through stub and trampoline, remove, EBYTES and the
  bounded I-cache wait (stuck ICBUSY -> EICACHE, stock bytes back), and a one-byte code patch.
- key_em10ii: the key layer with the E-M10 II key map over the event layer and the E-M10 II hook
  runtime: Fn1 claimed in live view and the menu, Fn2/Fn3/REC/L-Fn/RIGHT decoded, the E-PL3's POWER
  code and code 0 unknown, POWER refused by add and inject.
- shot_em10ii: the screenshot on the E-M10 II body (sdk/lib/shot_pix.c): the EVF's 640 x 480 OSD
  scaled 1.6x by nearest pixel and the LCD's 1:1, RGB palettes with alpha 0..32 and the global
  alpha, blending, the start and abort rules, the LCD and EVF layouts in PRIM block 0.
- shot_em10ii run: the E-M10 II screenshot (sdk/lib/shot.c) on a fake LCD display against mocks
  of the image pool and the file server: the body's lend of PRIM block 0 (another block given
  back, a refused alloc), the start rules (a PRIM frame in flight, prop 0xE951, a pool over block
  0, movie, playback), a whole shot with the raw pieces while a still takes block 1, and aborts
  during the PPM (block 0 back first, the partial PPM removed) and during the raw pieces (the PPM
  kept).
- ui_em10ii: the E-M10 II settings page (sdk/lib/ui_em10ii.c) entered through its slot words and
  screen methods: install all or nothing, the push onto a fake menu app, the slot 0x38 image,
  keys, an edit, MENU through the app's pop method, the delete, the default handler.
"""

from pathlib import Path

import pytest
from conftest import (
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

from fenykl import fwapi, sdk

pytestmark = needs_toolchain

PROJECTS = sdk.SDK / "projects"
# The timer block (timer_now 0x98900074); the core id 0x8050 is in the simulator's default memory.
TIMER = region("0x98900000,0x100")
EM10II = sdk.target("em10ii")
# E-M10 II DRAM up to the GUI and menu data with its uncached alias (the mock bodies are apart).
EM10II_DRAM = ("--memory-alias", "0x60000000,0x2000000,0xa0000000")


PROGRAMS = {
    "main_fmt": {
        "sources": [
            SIM / "main" / "test_main_fmt.c",
            PROJECTS / "main" / "main_fmt.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
    "hud_rle": {
        "sources": [
            SIM / "hud" / "test_hud_rle.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
    "main_dof": {
        "sources": [
            SIM / "main" / "test_main_dof.c",
            PROJECTS / "main" / "main_dof.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
    "main_batt": {
        "sources": [
            SIM / "main" / "test_main_batt.c",
            PROJECTS / "main" / "main_batt.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
    "feat_focus": {
        "sources": [
            SIM / "main" / "test_feat_focus.c",
            PROJECTS / "main" / "feat_focus_filter.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
    "main_fmeter": {
        "sources": [
            SIM / "main" / "test_main_fmeter.c",
            PROJECTS / "main" / "main_fmeter.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
    "main_rbswap_base": {
        "sources": [
            SIM / "main" / "test_main_rbswap_base.c",
            PROJECTS / "main" / "main_rbswap_base.c",
            SIM / "main" / "rbswap_fw.S",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "mocked": ["psw_get", "irq_disable_all", "irq_mask_restore"],
        "target": EM10II,
    },
    "afc_sm": {
        "sources": [
            SIM / "afc" / "test_afc_sm.c",
            SIM / "hooks_em10ii" / "sim_start.S",
        ],
        "memory": [EM10II_DRAM, region("0x6eb00000,0x10000")],
        "target": EM10II,
    },
}


@pytest.mark.parametrize("name", list(PROGRAMS))
def test_program_in_the_simulator(name, tmp_path):
    program = PROGRAMS[name]
    result = run_in_simulator(tmp_path, f"{name}sim", **program)
    assert_exit(result, 0, program["sources"][0].relative_to(SIM.parent.parent))


# appevent_post's span under a sim-only name: the SDK binds appevent_post itself (target.toml
# sdk_hooks), so the hook mechanism test hooks the same bytes under another name.
SIMEVENT_EM10II_TOML = """
[simevent_post]
address = 0x60042428
saves = ["d2", "d3", "a2"]
frame = 0x10
evidence = "high"
returns = "int"
args = ["d unsigned event", "d unsigned param", "a unsigned flag"]
note = "sim-only alias of the E-M10 II appevent_post mock"

[simevent_post.hook]
entry = "call"
relocate = "83 f1 e6 fc a9 da 84 c1 a1"
cpu1 = "cpu0-only"
cpu1_evidence = "synthetic simulator-only target"
note = "appevent_post's entry bytes"
"""


def _firmware_toml(tmp_path: Path, extra: str) -> Path:
    """The real firmware.toml plus sim-only declarations."""
    toml = tmp_path / "firmware.toml"
    toml.write_text((EM10II.dir / "firmware.toml").read_text() + extra)
    return toml


def test_em10ii_hooks_run_in_the_simulator(tmp_path):
    """E-M10 II hooks.c on the real appevent_post and cw_updateDisplay spans, incl. the I-cache path."""
    hooks = SIM / "hooks_em10ii"
    result = run_in_simulator(
        tmp_path,
        "hooksim_em10ii",
        [hooks / "test_hooks.c", hooks / "sim_start.S", hooks / "fw.S"],
        # DRAM (targets, payload, stack) with its uncached alias, the byte appevent_post's span
        # reads, the mock bodies. CHCTR 0x8070 is in the simulator's default memory.
        [
            ("--memory-alias", "0x60000000,0x1000000,0xa0000000"),
            region("0xa1c18000,0x1000"),
            region("0x6eb00000,0x10000"),
        ],
        mocked=[
            "appevent_post",
            "cw_updateDisplay",
            "irq_lock",
            "irq_unlock",
            "dcache_flush_all",
        ],
        firmware=_firmware_toml(tmp_path, SIMEVENT_EM10II_TOML),
        target=EM10II,
        hooks=["simevent_post", "cw_updateDisplay"],
        cpu1_unproven=["cw_updateDisplay"],
    )
    assert_exit(result, 0, "tests/sim/hooks_em10ii/test_hooks.c")


def test_em10ii_key_map_runs_in_the_simulator(tmp_path):
    """The key layer with the E-M10 II key map, through the patched appevent_post on a recording
    mock at its real address."""
    key = SIM / "key_em10ii"
    result = run_in_simulator(
        tmp_path,
        "keysim_em10ii",
        [key / "test_key.c", SIM / "hooks_em10ii" / "sim_start.S", key / "fw.S"],
        # DRAM up to the GUI bytes and the init byte appevent_post's span reads, with its uncached
        # alias; the timer; the mock bodies.
        [
            ("--memory-alias", "0x60000000,0x2000000,0xa0000000"),
            TIMER,
            region("0x6eb00000,0x10000"),
        ],
        mocked=[
            "appevent_post",
            "psw_get",
            "irq_disable_all",
            "irq_mask_restore",
            "irq_lock",
            "irq_unlock",
            "dcache_flush_all",
        ],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/key_em10ii/test_key.c")


def test_em10ii_ui_runs_in_the_simulator(tmp_path):
    """E-M10 II settings page against mocks of the GUI data store, the menu app's default handler
    and its pop method (at its real address), with the real cfg and log libraries."""
    pop = fwapi.load(EM10II.dir / "firmware.toml").data["menu_app_pop_fn"].address
    ui = SIM / "ui_em10ii"
    result = run_in_simulator(
        tmp_path,
        "uisim_em10ii",
        [ui / "test_ui.c", SIM / "hooks_em10ii" / "sim_start.S", ui / "fw.S"],
        # DRAM up to the GUI and menu data with its uncached alias, the timer, the mock bodies.
        [
            EM10II_DRAM,
            TIMER,
            region("0x6eb00000,0x10000"),
        ],
        mocked=[
            "psw_get",
            "irq_disable_all",
            "irq_mask_restore",
            "irq_lock",
            "irq_unlock",
            "dcache_flush_all",
            "gui_data_set",
            "menu_app_default",
        ],
        ldflags=[f"-Wl,--section-start=.fw.app_pop=0x{pop:x}"],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/ui_em10ii/test_ui.c")


def test_em10ii_shot_runs_in_the_simulator(tmp_path):
    """E-M10 II screenshot rules and pixel work: shot_pix.c built with the E-M10 II body."""
    result = run_in_simulator(
        tmp_path,
        "shotsim_em10ii",
        [SIM / "shot" / "test_shot_em10ii.c", SIM / "hooks_em10ii" / "sim_start.S"],
        # DRAM (payload, stack) with its uncached alias, the mock section.
        [
            ("--memory-alias", "0x60000000,0x1000000,0xa0000000"),
            region("0x6eb00000,0x10000"),
        ],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/shot/test_shot_em10ii.c")


def test_em10ii_shot_run_in_the_simulator(tmp_path):
    """E-M10 II screenshot (shot.c, the body's lend) on a fake LCD display against mocks of the
    image pool, the file server and the kernel."""
    run = SIM / "shot_em10ii"
    result = run_in_simulator(
        tmp_path,
        "shotrun_em10ii",
        [run / "test_shot_run.c", SIM / "hooks_em10ii" / "sim_start.S", run / "fw.S"],
        # DRAM up to the GUI, display and pool data with its uncached alias, PRIM block 0, the
        # live-view and OSD buffers with theirs, the display registers, the timer, the mock bodies.
        [
            ("--memory-alias", "0x60000000,0x2000000,0xa0000000"),
            ("--memory-alias", "0x63000000,0x2000000,0xa3000000"),
            ("--memory-alias", "0x7e000000,0x2000000,0xbe000000"),
            region("0x9c000000,0x2000"),
            TIMER,
            region("0x6eb00000,0x10000"),
        ],
        mocked=[
            "psw_get",
            "irq_disable_all",
            "irq_mask_restore",
            "irq_lock",
            "irq_unlock",
            "dcache_flush_all",
            "OlyTaskSleep",
            "sprintf",
            "ui_key_get_state",
            "fileif_get_media_status",
            "rpc_fopen",
            "rpc_fclose",
            "rpc_fwrite",
            "rpc_fremove",
            "imgbuff_alloc",
            "imgbuff_addr",
            "imgbuff_free",
        ],
        target=EM10II,
        timeout=300,
    )
    assert_exit(result, 0, "tests/sim/shot_em10ii/test_shot_run.c")
