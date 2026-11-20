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
- fnfunc_em10ii: the Button Function entries (sdk/lib/fnfunc_em10ii.c) on the stock image's tables
  (skipped without it), over the event layer and the E-M10 II hook runtime: the stock checks and
  the argument refusals, the install with consistent tables at every swap and its rollback, lists
  and labels for the five buttons, the lazy event install and the press callbacks.
- shot_em10ii: the screenshot on the E-M10 II body (sdk/lib/shot_pix.c): the EVF's 640 x 480 OSD
  scaled 1.6x by nearest pixel and the LCD's 1:1, RGB palettes with alpha 0..32 and the global
  alpha, blending, the start and abort rules, the LCD and EVF layouts in PRIM block 0.
- shot_em10ii run: the E-M10 II screenshot (sdk/lib/shot.c) on a fake LCD display against mocks
  of the image pool and the file server: the body's lend of PRIM block 0 (another block given
  back, a refused alloc), the start rules (a PRIM frame in flight, prop 0xE951, a pool over block
  0, movie, playback), a whole shot with the raw pieces while a still takes block 1, and aborts
  during the PPM (block 0 back first, the partial PPM removed) and during the raw pieces (the PPM
  kept).
- ui_em10ii: the E-M10 II settings tab (sdk/lib/ui.c, ui_em10ii.c) on the stock image's bytes of
  every table it checks or rewires: the option limits, the waits and stock refusals, the install
  with a consistency check at every flush, the rows, labels and styles, the pickers through the
  words the firmware calls, cfg sets, the value-column refresh, a late install and the rollback.
"""

from pathlib import Path

import pytest
from conftest import (
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
    stock_image,
)

from fenykl import fwapi, sdk
from fenykl.firmware import TARGETS, unpack_firmware

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


# (run address, bytes) of every stock table lib/fnfunc_em10ii.c checks or copies.
FNFUNC_REGIONS = [
    (0x603AA9E0, 4 * 0x404),  # settings map {prop, element}
    (0x6038EEB4, 0x6038EFBA - 0x6038EEB4),  # Fn1, REC, Fn2, arrows, L-Fn value lists
    (0x6038FB40, 28 * 2),  # Fn3's value list
    (0x60390B7C, 0x60390BF4 - 0x60390B7C),  # Fn1, REC, Fn2, arrows, L-Fn store records
    (0x60392B5C, 0x14),  # Fn3's store record
    (0x60D70174, 38 * 12),  # row value cells
    (0x60D7243C, 8 * 0x24),  # Custom B button rows
    (0x60D3435C, 12 * 8),  # picker init entries
    (
        0x60D78A10,
        0x60D79058 - 0x60D78A10,
    ),  # names, icons, help, footer, the 15 key entries
]


def _fnfunc_image(workdir: Path) -> Path:
    """sim_image[]: the stock bytes of FNFUNC_REGIONS at their run addresses."""
    fw = unpack_firmware(stock_image("em10ii"))
    code = fw.blocks[fw.find_block("main-code-01")].body
    base = TARGETS["em10ii"].load_addresses["main-code-01"]
    arrays, entries = [], []
    for i, (address, size) in enumerate(FNFUNC_REGIONS):
        chunk = code[address - base : address - base + size]
        arrays.append(
            f"static const uint8_t r{i}[] = {{{', '.join(map(str, chunk))}}};"
        )
        entries.append(f"\t{{0x{address:x}u, {size}u, r{i}}},")
    path = workdir / "image.c"
    path.write_text(
        "#include <stdint.h>\n\n"
        "struct sim_region {\n\tuint32_t addr, len;\n\tconst uint8_t *bytes;\n};\n\n"
        + "\n".join(arrays)
        + "\n\nconst struct sim_region sim_image[] = {\n"
        + "\n".join(entries)
        + f"\n}};\nconst unsigned int sim_image_n = {len(FNFUNC_REGIONS)};\n"
    )
    return path


def test_em10ii_fnfunc_runs_in_the_simulator(tmp_path):
    """The Button Function entries on the stock tables, with presses through the patched
    appevent_post on a recording mock at its real address."""
    sim = SIM / "fnfunc_em10ii"
    result = run_in_simulator(
        tmp_path,
        "fnfuncsim",
        [
            sim / "test_fnfunc.c",
            _fnfunc_image(tmp_path),
            SIM / "hooks_em10ii" / "sim_start.S",
            sim / "fw.S",
        ],
        # DRAM with its uncached alias; the timer; the mock bodies.
        [EM10II_DRAM, TIMER, region("0x6eb00000,0x10000")],
        mocked=[
            "appevent_post",
            "psw_get",
            "irq_disable_all",
            "irq_mask_restore",
            "irq_lock",
            "irq_unlock",
            "dcache_flush_all",
            "OlyTaskSleep",
        ],
        target=EM10II,
    )
    assert_exit(result, 0, "tests/sim/fnfunc_em10ii/test_fnfunc.c")


# (run address, bytes) of every stock region the settings tab checks, copies or swaps.
UI_REGIONS = [
    (0x60D2E2AC, 0x60D2E4BC - 0x60D2E2AC),  # letter list RIGHT and OK records
    (0x60D2E81C, 0x60),  # config 0x40's RIGHT and OK records
    (0x60D31468, 0x60D31540 - 0x60D31468),  # configs 0x30 .. 0x40
    (0x60D33E9E, 0x60D34020 - 0x60D33E9E),  # tab ids, tab list, row-id lists
    (0x60D36A5C, 14 * 8),  # tab -> config
    (0x60D3C5B4, 0x60D3C8B4 - 0x60D3C5B4),  # tab records, icons, row styles
    (0x60D4F020, 0x60D4F9C4 - 0x60D4F020),  # letter titles, helps, page records
    (0x60B003FC, 0x220 * 12),  # ModeCheck rules
    (0x615F3FBC, 4),  # letter list object pointer (.data1)
    (0x615F4B14, 28),  # option picker handlers (.data1)
]
# .data1 runs at DATA1 and sits at DATA1_FILE in the main code block.
DATA1, DATA1_FILE = 0x615DC540, 0x1AEFE0


def _ui_image_source(workdir: Path) -> Path:
    """sim_image[]: the stock bytes of UI_REGIONS at their run addresses."""
    fw = unpack_firmware(stock_image("em10ii"))
    code = fw.blocks[fw.find_block("main-code-01")].body
    base = TARGETS["em10ii"].load_addresses["main-code-01"]
    arrays, entries = [], []
    for i, (address, size) in enumerate(UI_REGIONS):
        start = address - DATA1 + DATA1_FILE if address >= DATA1 else address - base
        chunk = code[start : start + size]
        arrays.append(
            f"static const uint8_t r{i}[] = {{{', '.join(map(str, chunk))}}};"
        )
        entries.append(f"\t{{0x{address:x}u, {size}u, r{i}}},")
    path = workdir / "image.c"
    path.write_text(
        "#include <stdint.h>\n\n"
        "struct sim_region {\n\tuint32_t addr, len;\n\tconst uint8_t *bytes;\n};\n\n"
        + "\n".join(arrays)
        + "\n\nconst struct sim_region sim_image[] = {\n"
        + "\n".join(entries)
        + f"\n}};\nconst unsigned int sim_image_n = {len(UI_REGIONS)};\n"
    )
    return path


def test_em10ii_ui_runs_in_the_simulator(tmp_path):
    """E-M10 II settings tab on the stock bytes of its tables, against mocks of the picker factory,
    the picker's stock handler (at its real address) and the card gate's USB and media reads, with
    the real cfg and log libraries."""
    stock_fn = (
        fwapi.load(EM10II.dir / "firmware.toml").data["menu_picker_stock_fn"].address
    )
    ui = SIM / "ui_em10ii"
    result = run_in_simulator(
        tmp_path,
        "uisim_em10ii",
        [
            ui / "test_ui.c",
            _ui_image_source(tmp_path),
            SIM / "hooks_em10ii" / "sim_start.S",
            ui / "fw.S",
        ],
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
            "OlyTaskSleep",
            "menu_picker_create",
            "ui_key_get_state",
            "fileif_get_media_status",
            "malloc",
            "free",
        ],
        ldflags=[f"-Wl,--section-start=.fw.picker_stock=0x{stock_fn:x}"],
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
