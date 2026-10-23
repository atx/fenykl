"""birddetect6 (sdk/projects/birddetect6) in the GDB simulator: the network (generated code)
bit-exact against the fenykl-nn test vectors, every layer (tests/sim/birddetect6/vectors.json,
written by the project's gen_model.py from the model's handoff directory: the input, each layer's
checksum, both head channels and their post-processing), the post-processing of the synthetic
heads, plus the self-test patterns with their checksums and post-processing, the band sequence and
the injection target (see test_birddetect6.c for the exit mask). A vector's input is its planes, a
constant fill or, for a self-test pattern, bd6_net_pattern's. The vectors live in a section outside
the payload region (the network leaves too little room in it)."""

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

PROJECT = sdk.SDK / "projects" / "birddetect6"
VECTORS = SIM / "birddetect6" / "vectors.json"
IN_W, IN_H = 96, 72
LOGITS = 2 * 9 * 12  # BD6_LOGITS: heat map, presence
# The vectors' section: simulated DRAM past the payload region and .mock.
SIMVEC = 0x6EC00000
VEC = '__attribute__((section(".simvec"), aligned(4)))'
DOC = json.loads(VECTORS.read_text())


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
    """score, fire, row, col, x_q4, y_q4 (sim_post in test_birddetect6.c)."""
    return (
        "{ " + c_list([v["score"], int(v["fire"]), *v["cell"], *v["point_q4"]]) + " }"
    )


def vector_input(v: dict) -> tuple[bytes | None, int]:
    """(IPU frame, -1) from the planes or the constant fill, else (None, enum bd6_pattern)."""
    if "y" in v:
        return yuyv(base64.b64decode(v["y"]), base64.b64decode(v["uv"])), -1
    if "fill" in v:
        return bytes([v["fill"]]) * (2 * IN_W * IN_H), -1
    return None, DOC["selftest"].index(v["kind"])


def vectors_c(path: Path) -> Path:
    vecs = DOC["vectors"]
    inputs = [vector_input(v) for v in vecs]
    out = ["#include <stdint.h>\n"]
    for i, (frame, _) in enumerate(inputs):
        if frame is not None:
            out.append(f"static const uint8_t f{i}[] {VEC} = {{ {c_list(frame)} }};\n")
    frames = [
        f"f{i}" if frame is not None else "0" for i, (frame, _) in enumerate(inputs)
    ]
    out += [
        f"const unsigned int sim_nvec = {len(vecs)};\n",
        "const char *const sim_kind[] = { "
        + c_list(f'"{v["kind"]}"' for v in vecs)
        + " };\n",
        f"const uint8_t *const sim_frame[] = {{ {c_list(frames)} }};\n",
        f"const int sim_pattern[] = {{ {c_list(p for _, p in inputs)} }};\n",
        f"const uint32_t sim_sums[][{len(DOC['layers'])}] = {{\n"
        + "".join(f"\t{{ {c_list(f'{s}u' for s in v['sums'])} }},\n" for v in vecs)
        + "};\n",
        f"const int32_t sim_head[][{LOGITS}] {VEC} = {{\n"
        + "".join(f"\t{{ {c_list(v['head'])} }},\n" for v in vecs)
        + "};\n",
        "const int32_t sim_post[][6] = {\n"
        + "".join(f"\t{post_row(v)},\n" for v in vecs)
        + "};\n",
        f"const unsigned int sim_npost = {len(DOC['post'])};\n",
        f"const int32_t sim_post_head[][{LOGITS}] {VEC} = {{\n"
        + "".join(f"\t{{ {c_list(v['head'])} }},\n" for v in DOC["post"])
        + "};\n",
        "const int32_t sim_post_want[][6] = {\n"
        + "".join(f"\t{post_row(v)},\n" for v in DOC["post"])
        + "};\n",
    ]
    path.write_text("".join(out))
    return path


def test_birddetect6_in_the_simulator(tmp_path):
    result = run_in_simulator(
        tmp_path,
        "birddetect6sim",
        [
            SIM / "birddetect6" / "test_birddetect6.c",
            PROJECT / "net.c",
            PROJECT / "bd6_model.c",
            PROJECT / "bd6_gen.S",
            PROJECT / "inject.c",
            vectors_c(tmp_path / "vectors.c"),
            SIM / "hooks" / "sim_start.S",
        ],
        [DRAM_ALIAS, region("0x98900000,0x100"), POWERON],
        ldflags=[f"-Wl,--section-start=.simvec=0x{SIMVEC:x}"],
        timeout=300,
    )
    assert_exit(result, 0, "tests/sim/birddetect6/test_birddetect6.c")
    assert b"all layers bit-exact" in result.stdout
    assert b"post-processing bit-exact" in result.stdout
