"""Builds the example plugins end to end (compile, link, pack). The build tests need the cross
toolchain, the ISA check tests also GHIDRA_INSTALL_DIR and JAVA_HOME; they are skipped without them."""

import dataclasses
import os
import re

import pytest
from conftest import needs_toolchain

from fenykl import plugin, sdk
from fenykl.payload import (
    HEADER_SIZE,
    check_image,
    describe,
    elf_section,
    parse_header,
)

EXAMPLES = sorted(p.parent.name for p in (sdk.SDK / "projects").glob("*/plugin.toml"))
# (example, target) for every target an example's plugin.toml lists.
EXAMPLE_TARGETS = [
    (e, t.name)
    for e in EXAMPLES
    for t in plugin.plugin_targets(sdk.SDK / "projects" / e)
]
needs_ghidra = pytest.mark.skipif(
    not (os.environ.get("GHIDRA_INSTALL_DIR") and os.environ.get("JAVA_HOME")),
    reason="the ISA check needs GHIDRA_INSTALL_DIR and JAVA_HOME",
)


@needs_toolchain
@pytest.mark.parametrize(("example", "target"), EXAMPLE_TARGETS)
def test_example_builds(example, target, tmp_path):
    image = plugin.build(
        sdk.SDK / "projects" / example, tmp_path, sdk.target(target)
    ).read_bytes()
    assert check_image(image) is None
    header = parse_header(image)
    assert header.entry == HEADER_SIZE
    assert header.load == sdk.target(target).load
    assert b"C:\\FENYKL.LOG" in image
    assert not (tmp_path / "isacheck").exists()


@needs_toolchain
@needs_ghidra
@pytest.mark.parametrize(("example", "target"), EXAMPLE_TARGETS)
def test_example_passes_the_isa_check(example, target, tmp_path):
    plugin.build(
        sdk.SDK / "projects" / example, tmp_path, sdk.target(target), isa_check=True
    )
    assert (tmp_path / "isacheck").is_dir()


def test_feature_libraries_follow_the_target_capabilities():
    def names(target):
        return {p.name for p in plugin.lib_sources(sdk.target(target))}

    assert names("epl3") == {p.name for p in plugin.LIB.glob("*.c")}
    assert names("em10ii") == {"cfg.c", "log.c", "log_rec.c", "start.c", "string.c"}


@needs_toolchain
def test_log_and_cfg_link_for_the_em10ii(tmp_path):
    (tmp_path / "plugin.toml").write_text('targets = ["em10ii"]\n')
    (tmp_path / "main.c").write_text(
        '#include "cfg.h"\n#include "log.h"\n'
        'static FENYKL_LOG_MODULE(m, "m", FENYKL_LOG_INFO);\n'
        'FENYKL_CFG_BOOL(opt, "opt", "Opt", NULL, 0);\n'
        "void plugin_main(uint32_t b, uint32_t s)\n"
        "{ fenykl_log_init(NULL); fenykl_log_task_start(); fenykl_cfg_init();\n"
        '  fenykl_cfg_task_start(); fenykl_log_info(&m, "%u", opt); fenykl_log_flush(); }\n'
    )
    image = plugin.build(tmp_path, tmp_path / "out", sdk.target("em10ii")).read_bytes()
    assert check_image(image) is None
    symbols = (tmp_path / "out" / "plugin.map").read_text()
    for name in ("flog_flusher_task", "fcfg_saver_task", "fenykl_card_io_allowed"):
        assert f" {name}\n" in symbols


def test_plugin_targets_refuses_an_unlisted_target(tmp_path):
    (tmp_path / "plugin.toml").write_text('targets = ["epl3"]\n')
    assert [t.name for t in plugin.plugin_targets(tmp_path)] == ["epl3"]
    with pytest.raises(plugin.BuildError, match="not for em5"):
        plugin.plugin_targets(tmp_path, "em5")


@needs_toolchain
def test_plugin_may_not_list_a_hook_the_sdk_binds(tmp_path):
    (tmp_path / "plugin.toml").write_text(
        'targets = ["epl3"]\nhooks = ["appevent_post"]\n'
    )
    (tmp_path / "main.c").write_text("void plugin_main(void) {}\n")
    with pytest.raises(plugin.BuildError, match="the SDK binds it itself"):
        plugin.build(tmp_path, tmp_path / "out", sdk.target("epl3"))


# ---- CPU1 sources ----

WORKER = """#include <fenykl.h>
FENYKL_CPU1_WORKER(work);
void work(void *arg)
{
	while (!fenykl_cpu1_stop_requested())
		fw_OlyTaskSleep(1);
	CALL;
}
"""
MAIN = """#include <fenykl.h>
void work_cpu1(void);
void plugin_main(uint32_t load_base, uint32_t image_size)
{
	fenykl_cpu1_start(work_cpu1, 0);
}
"""


def cpu1_plugin(tmp_path, call, cpu1_sources='["work.c"]', target=None):
    src = tmp_path / "src"
    src.mkdir()
    (src / "plugin.toml").write_text(
        f'targets = ["epl3"]\ncpu1_sources = {cpu1_sources}\n'
    )
    (src / "work.c").write_text(WORKER.replace("CALL", call))
    (src / "main.c").write_text(MAIN)
    return plugin.build(src, tmp_path / "out", target or sdk.target("epl3"))


@needs_toolchain
def test_cpu1_source_calling_a_cpu0_only_function_does_not_build(tmp_path):
    with pytest.raises(
        plugin.BuildError,
        match="fw_malloc is CPU0-only \\(firmware.toml cores\\), not callable from cpu1_sources",
    ):
        cpu1_plugin(tmp_path, "fw_malloc(16)")


@needs_toolchain
def test_cpu1_source_calling_cpu1_safe_functions_builds(tmp_path):
    image = cpu1_plugin(tmp_path, 'fw_dbg_puts("x"); fw_dcache_purge_all_irqsafe()')
    assert check_image(image.read_bytes()) is None
    elf = (tmp_path / "out" / "plugin.elf").read_bytes()
    assert "shared     0x" in describe(image.read_bytes(), elf)


@needs_toolchain
def test_cpu1_worker_outside_cpu1_sources_does_not_build(tmp_path):
    with pytest.raises(
        plugin.BuildError, match="list its file in plugin.toml cpu1_sources"
    ):
        cpu1_plugin(tmp_path, "(void)arg", cpu1_sources="[]")


def test_cpu1_sources_must_be_plugin_files(tmp_path):
    with pytest.raises(plugin.BuildError, match="cpu1_sources lists nope.c"):
        cpu1_plugin(tmp_path, "(void)arg", cpu1_sources='["work.c", "nope.c"]')


def test_cpu1_sources_need_a_target_with_the_cpu1_capability(tmp_path):
    no_cpu1 = dataclasses.replace(sdk.target("epl3"), capabilities=())
    with pytest.raises(plugin.BuildError, match="target epl3 has no cpu1 capability"):
        cpu1_plugin(tmp_path, "(void)arg", target=no_cpu1)


@needs_toolchain
def test_target_and_capability_macros_reach_the_plugin(tmp_path):
    (tmp_path / "plugin.toml").write_text('targets = ["epl3"]\n')
    (tmp_path / "main.c").write_text(
        "#include <fenykl.h>\n"
        '_Static_assert(FENYKL_TARGET_EPL3 && FENYKL_HAS_CPU1, "target macros");\n'
        "void plugin_main(uint32_t b, uint32_t s) { (void)b; (void)s; }\n"
    )
    plugin.build(tmp_path, tmp_path / "out", sdk.target("epl3"))


@needs_toolchain
def test_cpu1_shared_data_is_whole_lines_in_the_image(tmp_path):
    out = tmp_path / "out"
    image = plugin.build(
        sdk.SDK / "projects" / "cpu1frame", out, sdk.target("epl3")
    ).read_bytes()
    address, size = elf_section((out / "plugin.elf").read_bytes(), ".shared")
    assert address % 64 == 0 and size % 64 == 0 and size > 0
    start = address - parse_header(image).load
    assert len(image) >= start + size
    assert image[start : start + size] == bytes(size)


# ---- SDK libraries ----


@needs_toolchain
def test_log_call_with_more_than_four_arguments_does_not_build(tmp_path):
    (tmp_path / "plugin.toml").write_text('targets = ["epl3"]\n')
    (tmp_path / "main.c").write_text(
        '#include "log.h"\n'
        'static FENYKL_LOG_MODULE(m, "m", FENYKL_LOG_INFO);\n'
        "void plugin_main(uint32_t b, uint32_t s)\n"
        '{ fenykl_log_info(&m, "%u %u %u %u %u", 1, 2, 3, 4, 5); }\n'
    )
    with pytest.raises(plugin.BuildError, match="at most 4 log arguments"):
        plugin.build(tmp_path, tmp_path / "out", sdk.target("epl3"))


def sprintf_plugin(tmp_path, words):
    """A plugin whose fw_sprintf calls pass `words` variadic words, and none, in expression context."""
    args = "".join(f", {i}" for i in range(words))
    (tmp_path / "plugin.toml").write_text('targets = ["epl3"]\n')
    (tmp_path / "main.c").write_text(
        "#include <fenykl.h>\n"
        "static char buf[64];\n"
        "void plugin_main(uint32_t b, uint32_t s)\n"
        f'{{ b ? fw_sprintf(buf, "x") : fw_sprintf(buf, "%u"{args}); }}\n'
    )
    return plugin.build(tmp_path, tmp_path / "out", sdk.target("epl3"))


@needs_toolchain
def test_fw_sprintf_with_more_than_eight_variadic_words_does_not_build(tmp_path):
    with pytest.raises(plugin.BuildError, match="fw_sprintf: at most 8 variadic words"):
        sprintf_plugin(tmp_path, 9)


@needs_toolchain
def test_fw_sprintf_with_eight_variadic_words_builds(tmp_path):
    assert check_image(sprintf_plugin(tmp_path, 8).read_bytes()) is None


# ---- capability tables and the shared main plugin ----

MAIN_DIR = sdk.SDK / "projects" / "main"
BODY_CAPABILITIES = ("afc", "lens", "batt", "shot", "img_exec")


def test_main_names_no_firmware_symbol():
    """main reaches the firmware only through the SDK (include/body.h and fenykl.h), so that every
    body with the capabilities builds it unchanged. Only main's own files are scanned: target values
    that SDK headers carry (log.h's timer, hud.h's canvas and palette) are not seen here."""
    found = [
        f"{p.name}:{n}: {m.group()}"
        for p in sorted(MAIN_DIR.glob("*.[chS]"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        for m in re.finditer(r"\b(?:fw|FW)_\w+", line)
    ]
    assert found == []


def capability_plugin(tmp_path, tables):
    (tmp_path / "plugin.toml").write_text(
        f'targets = ["epl3"]\nhooks = ["cw_updateDisplay"]\n{tables}'
    )
    for name in ("main.c", "lens.c", "shot.S"):
        (tmp_path / name).write_text("")
    config = plugin._plugin_toml(tmp_path)
    sources = sorted(tmp_path.glob("*.[cS]"))
    return config, sources


def test_capability_tables_select_sources_and_hooks(tmp_path):
    config, sources = capability_plugin(
        tmp_path,
        '[capabilities.lens]\nsources = ["lens.c"]\n'
        '[capabilities.img_exec]\nsources = ["shot.S"]\nhooks = ["img_exec"]\n'
        'cpu1_unproven = ["img_exec"]\n',
    )
    epl3 = sdk.target("epl3")
    built, hooks, unproven = plugin.select_capabilities(config, tmp_path, sources, epl3)
    assert [p.name for p in built] == ["lens.c", "main.c", "shot.S"]
    assert (hooks, unproven) == (["cw_updateDisplay", "img_exec"], ["img_exec"])
    lens_only = dataclasses.replace(epl3, capabilities=("lens",))
    built, hooks, unproven = plugin.select_capabilities(
        config, tmp_path, sources, lens_only
    )
    assert [p.name for p in built] == ["lens.c", "main.c"]
    assert (hooks, unproven) == (["cw_updateDisplay"], [])


@pytest.mark.parametrize(
    ("tables", "error"),
    [
        (
            '[capabilities.lenz]\nsources = ["lens.c"]\n',
            "no target has capability 'lenz'",
        ),
        ('[capabilities.lens]\nfiles = ["lens.c"]\n', "unknown key files"),
        (
            '[capabilities.lens]\nsources = ["lens.h"]\n',
            "lists lens.h, not a .c or .S file",
        ),
        (
            '[capabilities.lens]\nsources = ["lens.c"]\n[capabilities.batt]\nsources = ["lens.c"]\n',
            "lists lens.c, already under lens",
        ),
        (
            '[capabilities.img_exec]\nhooks = ["cw_updateDisplay"]\n',
            "lists hook cw_updateDisplay, already listed under the top level",
        ),
        (
            '[capabilities.lens]\nsources = "lens.c"\n',
            "[capabilities.lens] sources must be a list of strings, not 'lens.c'",
        ),
        ('capabilities = ["lens"]\n', "plugin.toml capabilities must be tables"),
        (
            "[capabilities.img_exec]\nhooks = [1]\n",
            "[capabilities.img_exec] hooks must be a list of strings",
        ),
    ],
)
def test_capability_tables_are_checked(tmp_path, tables, error):
    config, sources = capability_plugin(tmp_path, tables)
    with pytest.raises(plugin.BuildError, match=re.escape(error)):
        plugin.select_capabilities(config, tmp_path, sources, sdk.target("epl3"))


@needs_toolchain
def test_main_builds_without_the_body_capabilities(tmp_path):
    """A body with the HUD, settings page and key layer but none of main's body capabilities builds
    main with no feature, no Fn action and no R/B swap."""
    epl3 = sdk.target("epl3")
    bare = dataclasses.replace(
        epl3,
        capabilities=tuple(c for c in epl3.capabilities if c not in BODY_CAPABILITIES),
    )
    image = plugin.build(MAIN_DIR, tmp_path, bare)
    assert check_image(image.read_bytes()) is None
    symbols = (tmp_path / "plugin.map").read_text()
    assert " plugin_main\n" in symbols
    for name in (
        "feat_focus",
        "feat_batt",
        "feat_fmeter",
        "act_shot",
        "main_rbswap_hk",
    ):
        assert f" {name}\n" not in symbols
    assert " fenykl_hook_img_exec\n" not in symbols
