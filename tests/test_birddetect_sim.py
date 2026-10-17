"""birddetect (sdk/projects/birddetect) in the GDB simulator: the network bit-exact against the
fenykl-nn test vectors (tests/sim/birddetect/vectors.json, written by the project's gen_weights.py:
every layer's checksum and the full head of 3 noise and 5 structured vectors), the self-test
checksums, the point sampling and the injection glide (see test_birddetect.c for the exit mask)."""

import base64
import json
from pathlib import Path

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

PROJECT = sdk.SDK / "projects" / "birddetect"
VECTORS = SIM / "birddetect" / "vectors.json"
SRC_W, SRC_H, IN_W, IN_H = 640, 480, 96, 72


def c_list(values) -> str:
    return ", ".join(str(v) for v in values)


def vectors_c(path: Path) -> Path:
    doc = json.loads(VECTORS.read_text())
    vecs = doc["vectors"]
    out = ["#include <stdint.h>\n#include <stddef.h>\n"]
    inputs = []
    for i, v in enumerate(vecs):
        if "input" not in v:
            inputs.append("NULL")
            continue
        data = base64.b64decode(v["input"])
        assert len(data) == IN_W * IN_H
        out.append(f"static const uint8_t in{i}[] = {{ {c_list(data)} }};\n")
        inputs.append(f"in{i}")
    xs = [(2 * x + 1) * SRC_W // (2 * IN_W) for x in range(IN_W)]
    ys = [min((2 * y + 1) * SRC_H // (2 * IN_H), SRC_H - 2) for y in range(IN_H)]
    out += [
        f"const unsigned int sim_nvec = {len(vecs)};\n",
        "const char *const sim_kind[] = { " + c_list(f'"{v["kind"]}"' for v in vecs) + " };\n",
        f"const uint8_t *const sim_input[] = {{ {c_list(inputs)} }};\n",
        "const uint32_t sim_sums[][10] = {\n"
        + "".join(f"\t{{ {c_list(f'{s}u' for s in v['sums'])} }},\n" for v in vecs)
        + "};\n",
        "const int32_t sim_head[][108] = {\n"
        + "".join(f"\t{{ {c_list(v['head'])} }},\n" for v in vecs)
        + "};\n",
        f"const uint16_t sim_xs[] = {{ {c_list(xs)} }};\n",
        f"const uint16_t sim_ys[] = {{ {c_list(ys)} }};\n",
    ]
    path.write_text("".join(out))
    return path


def test_birddetect_in_the_simulator(tmp_path):
    result = run_in_simulator(
        tmp_path,
        "birddetectsim",
        [
            SIM / "birddetect" / "test_birddetect.c",
            PROJECT / "net.c",
            PROJECT / "bd_model.c",
            PROJECT / "inject.c",
            vectors_c(tmp_path / "vectors.c"),
            SIM / "hooks" / "sim_start.S",
        ],
        [DRAM_ALIAS, region("0x98900000,0x100"), POWERON],
        timeout=300,
    )
    assert_exit(result, 0, "tests/sim/birddetect/test_birddetect.c")
