"""birddetect3 (sdk/projects/birddetect3) in the GDB simulator: the network (generated code)
bit-exact against every fenykl-nn test vector, every layer (tests/sim/birddetect3/vectors.json,
written by the project's gen_model.py from the model's handoff directory: the input planes, each
layer's checksum, the full head and its post-processing), the post-processing of the synthetic
heads, plus the self-test patterns and checksums, the band sequence and the injection target (see
test_birddetect3.c for the exit mask). The vectors run in chunks: their frames do not fit the
payload region at once; every chunk checks all synthetic heads."""

import base64
import json
from pathlib import Path

import pytest
from conftest import (
    DRAM_ALIAS,
    POWERON,
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

from fenykl import sdk

pytestmark = needs_toolchain

PROJECT = sdk.SDK / "projects" / "birddetect3"
VECTORS = SIM / "birddetect3" / "vectors.json"
IN_W, IN_H = 96, 72
CHUNK = 8
DOC = json.loads(VECTORS.read_text())
CHUNKS = range(0, len(DOC["vectors"]), CHUNK)


def c_list(values) -> str:
    return ", ".join(str(v) for v in values)


def yuyv(y: bytes, uv: bytes) -> bytes:
    """The IPU frame of the planes: Y0 U Y1 V per pixel pair."""
    plane = IN_H * IN_W // 2
    out = bytearray(2 * IN_W * IN_H)
    for r in range(IN_H):
        for k in range(IN_W // 2):
            o = r * 2 * IN_W + 4 * k
            out[o : o + 4] = bytes(
                (
                    y[r * IN_W + 2 * k],
                    uv[r * IN_W // 2 + k],
                    y[r * IN_W + 2 * k + 1],
                    uv[plane + r * IN_W // 2 + k],
                )
            )
    return bytes(out)


def post_row(v: dict) -> str:
    """score, fire, row, col, x_q4, y_q4 (sim_post in test_birddetect3.c)."""
    return "{ " + c_list([v["score"], int(v["fire"]), *v["cell"], *v["point_q4"]]) + " }"


def vectors_c(path: Path, first: int) -> Path:
    vecs = DOC["vectors"][first : first + CHUNK]
    out = ["#include <stdint.h>\n"]
    for i, v in enumerate(vecs):
        frame = yuyv(base64.b64decode(v["y"]), base64.b64decode(v["uv"]))
        out.append(
            f"static const uint8_t f{i}[] __attribute__((aligned(4))) = {{ {c_list(frame)} }};\n"
        )
    pattern = [DOC["selftest"].index(v["kind"]) if v["kind"] in DOC["selftest"] else -1 for v in vecs]
    out += [
        f"const unsigned int sim_nvec = {len(vecs)};\n",
        "const char *const sim_kind[] = { " + c_list(f'"{v["kind"]}"' for v in vecs) + " };\n",
        f"const uint8_t *const sim_frame[] = {{ {c_list(f'f{i}' for i in range(len(vecs)))} }};\n",
        f"const int sim_pattern[] = {{ {c_list(pattern)} }};\n",
        "const uint32_t sim_sums[][10] = {\n"
        + "".join(f"\t{{ {c_list(f'{s}u' for s in v['sums'])} }},\n" for v in vecs)
        + "};\n",
        "const int32_t sim_head[][108] = {\n"
        + "".join(f"\t{{ {c_list(v['head'])} }},\n" for v in vecs)
        + "};\n",
        "const int32_t sim_post[][6] = {\n" + "".join(f"\t{post_row(v)},\n" for v in vecs) + "};\n",
        f"const unsigned int sim_npost = {len(DOC['post'])};\n",
        "const int32_t sim_post_head[][108] = {\n"
        + "".join(f"\t{{ {c_list(v['head'])} }},\n" for v in DOC["post"])
        + "};\n",
        "const int32_t sim_post_want[][6] = {\n"
        + "".join(f"\t{post_row(v)},\n" for v in DOC["post"])
        + "};\n",
    ]
    path.write_text("".join(out))
    return path


@pytest.mark.parametrize("first", CHUNKS, ids=[f"vectors{i}" for i in CHUNKS])
def test_birddetect3_in_the_simulator(tmp_path, first):
    result = run_in_simulator(
        tmp_path,
        "birddetect3sim",
        [
            SIM / "birddetect3" / "test_birddetect3.c",
            PROJECT / "net.c",
            PROJECT / "bd3_model.c",
            PROJECT / "bd3_gen.S",
            PROJECT / "inject.c",
            vectors_c(tmp_path / "vectors.c", first),
            SIM / "hooks" / "sim_start.S",
        ],
        [DRAM_ALIAS, region("0x98900000,0x100"), POWERON],
        timeout=300,
    )
    assert_exit(result, 0, "tests/sim/birddetect3/test_birddetect3.c")
    assert b"all layers bit-exact" in result.stdout
    assert b"post-processing bit-exact" in result.stdout
