"""Builds the example plugins end to end (compile, link, pack). The build tests need the cross
toolchain, the ISA check tests also GHIDRA_INSTALL_DIR and JAVA_HOME; they are skipped without them."""

import os

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
needs_ghidra = pytest.mark.skipif(
    not (os.environ.get("GHIDRA_INSTALL_DIR") and os.environ.get("JAVA_HOME")),
    reason="the ISA check needs GHIDRA_INSTALL_DIR and JAVA_HOME",
)


@needs_toolchain
@pytest.mark.parametrize("example", EXAMPLES)
def test_example_builds(example, tmp_path):
    image = plugin.build(
        sdk.SDK / "projects" / example, tmp_path, sdk.target("epl3")
    ).read_bytes()
    assert check_image(image) is None
    assert parse_header(image).entry == HEADER_SIZE
    assert b"C:\\FENYKL.LOG" in image
    assert not (tmp_path / "isacheck").exists()


@needs_toolchain
@needs_ghidra
@pytest.mark.parametrize("example", EXAMPLES)
def test_example_passes_the_isa_check(example, tmp_path):
    plugin.build(
        sdk.SDK / "projects" / example, tmp_path, sdk.target("epl3"), isa_check=True
    )
    assert (tmp_path / "isacheck").is_dir()


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


def cpu1_plugin(tmp_path, call, cpu1_sources='["work.c"]'):
    src = tmp_path / "src"
    src.mkdir()
    (src / "plugin.toml").write_text(
        f'targets = ["epl3"]\ncpu1_sources = {cpu1_sources}\n'
    )
    (src / "work.c").write_text(WORKER.replace("CALL", call))
    (src / "main.c").write_text(MAIN)
    return plugin.build(src, tmp_path / "out", sdk.target("epl3"))


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
