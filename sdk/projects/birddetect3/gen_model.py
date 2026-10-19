"""Generate every model-dependent source of birddetect3 from a fenykl-nn handoff directory (model.npz,
model.json, testvectors.npz, source.json): bd3_model.c (the ternary layers' block tables, fire
threshold, centroid table and shift, self-test sums, shape checks), bd3_gen.S (the stem and head as
generated int8 code, the ternary layers as generated code split into blocks of output channels, both
by nnbench's gen_kernels.py emitters) and the simulator vectors
tests/sim/birddetect3/vectors.json (every vector, every layer, the post-processing of the network
vectors and of the synthetic heads).
Needs NumPy; run with any python3 that has it:

    python3 gen_model.py <handoff dir>
"""

import argparse
import base64
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
VECTORS = HERE.parents[2] / "tests" / "sim" / "birddetect3" / "vectors.json"
MASK = 0xFFFFFFFF

# net.h
PIN_PITCH = 2 * 96 + 4
L0_PLANE = 36 * 48
CELLS = 9 * 12
CENTROID_N = 16  # BD3_CENTROID_N
P3_PLANE = 14 * 11  # BD3_P3_PLANE
# Output pixels per call of the stem and the head (BD3_GEN_PX): 2 keeps the stem in the I-cache.
PX = 2
SHAPES = {
    "L0": ("conv", [3, 72, 96], [12, 36, 48], 3, 2, "int8"),
    "L1": ("maxpool2", [12, 36, 48], [12, 18, 24]),
    "L2": ("conv", [12, 18, 24], [16, 18, 24], 3, 1, "ternary"),
    "L3": ("maxpool2", [16, 18, 24], [16, 9, 12]),
    "L4": ("conv", [16, 9, 12], [28, 9, 12], 3, 1, "ternary"),
    **{f"L{i}": ("conv", [28, 9, 12], [28, 9, 12], 3, 1, "ternary") for i in range(5, 9)},
    "L9": ("conv", [28, 9, 12], [1, 9, 12], 1, 1, "int8"),
}
TRUNK = ["L4", "L5", "L6", "L7", "L8"]
# Ternary layers and whether they write pair words of padded planes (the next layer is ternary) or
# u8 CHW planes (a pool or the head reads them).
TERN = {"L2": False, "L4": True, "L5": True, "L6": True, "L7": True, "L8": False}
SELFTEST = ["hgradient", "vgradient", "checker", "hgradient_neutral", "u0_v255"]  # enum bd3_pattern


def gen_kernels():
    path = HERE.parent / "nnbench" / "gen_kernels.py"
    spec = importlib.util.spec_from_file_location("gen_kernels", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mix_sum(values) -> int:
    """Rotate-add checksum of 32-bit values (bd3_logit_sum, the simulator test's layer sums)."""
    s = 0
    for v in values:
        s = (((s << 1) | (s >> 31)) + (int(v) & MASK)) & MASK
    return s


def check_model(desc) -> None:
    inp = desc["input"]
    assert (inp["width"], inp["height"], inp["source"], inp["chroma"]) == (96, 72, "ipu", "G"), inp
    layers = desc["layers"]
    assert [lay["name"] for lay in layers] == list(SHAPES), "layer list differs from net.h"
    for lay in layers:
        want = SHAPES[lay["name"]]
        got = (lay["op"], lay["in_shape"], lay["out_shape"])
        if lay["op"] == "conv":
            got += (lay["k"], lay["stride"], lay["wtype"])
        assert got == want, (lay["name"], got, want)
    assert layers[-1]["act"] == "none"
    # Head options this port does not implement (intspec.md "Head post-processing").
    cfg = desc["config"]
    assert cfg.get("presence", "none") == "none", f"presence head {cfg['presence']!r} not supported"
    assert not cfg.get("fine", False), "fine heat map not supported"
    assert layers[-1]["out_shape"][0] == 1, f"head has {layers[-1]['out_shape'][0]} channels, not 1"
    # inject.c: peak - logit fits int32.
    assert layers[-1]["acc_bound"] < 1 << 30, layers[-1]["acc_bound"]
    pp = desc["postprocess"]
    table = pp["centroid_table"]
    # inject.c: den >= table[0] > 0.
    assert len(table) == CENTROID_N and table[0] > 0 and all(0 <= v <= 255 for v in table), table
    assert 0 <= pp["centroid_k"] < 31, pp["centroid_k"]
    assert all(lay["act"] == "shift_round_clamp_u8" for lay in layers[:-1] if lay["op"] == "conv")


def rounded_bias(arrays, name) -> list[int]:
    shift = arrays[name + ".shift"].astype(np.int64)
    rnd = np.where(shift > 0, np.left_shift(1, np.maximum(shift - 1, 0)), 0)
    return [int(v) for v in arrays[name + ".b"].astype(np.int64) + rnd]


def c_array(ctype: str, name: str, values, per_line: int = 12, storage: str = "static ") -> str:
    vals = [v if isinstance(v, str) else str(int(v)) for v in values]
    lines = [", ".join(vals[i : i + per_line]) for i in range(0, len(vals), per_line)]
    body = ",\n\t".join(lines)
    return f"{storage}const {ctype} {name}[{len(vals)}] = {{\n\t{body},\n}};\n"


def stem_taps(w) -> list[list[tuple[int, int]]]:
    """Per output channel (byte offset, weight) from the stem's pointer: the padded YUYV row 2y,
    byte 4x of output x. Y column 2x-1+kx is byte 2 + 2kx, U[x-1] / U[x] bytes 1 / 5, V 3 / 7. The
    taps of the upsampled chroma columns 2x and 2x+1 both read sample x: one weight if the sum fits
    the udf00 immediate, else both."""
    taps = []
    for o in range(w.shape[0]):
        t = []
        for ky in range(3):
            row = ky * PIN_PITCH
            t += [(row + 2 + 2 * kx, int(w[o, 0, ky, kx])) for kx in range(3)]
            for ch, base in ((1, 1), (2, 3)):
                w0, w1, w2 = (int(v) for v in w[o, ch, ky])
                t.append((row + base, w0))
                if -127 <= w1 + w2 <= 127:
                    t.append((row + base + 4, w1 + w2))
                else:
                    t += [(row + base + 4, w1), (row + base + 4, w2)]
        taps.append(t)
    return taps


@dataclass
class Tern:
    name: str
    cv: object  # gen_kernels.Conv
    wt: object  # gen_kernels.Weights
    nz: list[int]  # non-zero weights per output channel
    blocks: list[range]  # output channels per generated function

    def fn(self, b: int) -> str:
        return f"bd3_{self.name.lower()}_{b}"


def tern_layer(gk, arrays, desc, name: str) -> Tern:
    """A ternary layer split into blocks of output channels whose estimated code fits nnbench3's
    split7k budget (7168 B; measured on the camera for L5: 1.27 M cycles in 5 blocks, 1.45 M as one
    function, 3.40 M with the offset-table kernel). model_layer asserts the lane bound and the input
    offset range."""
    cv, wt = gk.model_layer(arrays, desc, name)
    nz = [sum(1 for v in row if v) for row in wt.w]
    return Tern(name, cv, wt, nz, gk.pack(nz, 10 * gk.SPLIT7K.budget))


def pair_at(o: int, j: int) -> list[int]:
    """Pair-word output, A1 at word (x, y + 1) of the padded planes: pixel x + j is the low half of
    word x + j + 1 and the high half of word x + j."""
    return [4 * (o * P3_PLANE + j + 1), 4 * (o * P3_PLANE + j) + 2]


def tern_asm(gk, t: Tern) -> list[str]:
    at = pair_at if TERN[t.name] else None
    last = pair_at(t.cv.cout - 1, 3)[0] if at else t.cv.cout * t.cv.ho * t.cv.wo
    assert last < 1 << 15, f"{t.name}: output offsets beyond d16"
    asm = []
    for b, chans in enumerate(t.blocks):
        est = sum(gk.tern_cost10(t.nz[o]) for o in chans) // 10
        note = f"{t.name} block {b}: channels {chans.start}-{chans.stop - 1}, 4 output pixels, ~{est} B"
        asm += gk.kernel(t.fn(b), note, gk.body_tern(t.cv, t.wt, chans, at), False)
    return asm


def write_gen(gk, arrays, terns: list[Tern]) -> int:
    w0 = arrays["L0.w"]
    stem = stem_taps(w0)
    body = gk.int8_body(
        stem,
        rounded_bias(arrays, "L0"),
        [int(s) for s in arrays["L0.shift"]],
        4,
        PX,
        lambda j, o: o * L0_PLANE + j,
    )
    w9 = arrays["L9.w"]
    head = [[(c * CELLS, int(w9[0, c, 0, 0])) for c in range(w9.shape[1])]]
    hbody = gk.int8_body(head, [int(arrays["L9.b"][0])], None, 1, PX, lambda j, o: 4 * j)
    asm = [
        "/* Generated by gen_model.py. Do not edit. Layout: net.h. */",
        "",
        gk.MACROS,
        *gk.kernel("bd3_stem", f"L0: {PX} output pixels, all channels", body, PX == 4),
        *gk.kernel("bd3_head", f"L9: {PX} cells, int32", hbody, PX == 4),
    ]
    for t in terns:
        asm += tern_asm(gk, t)
    (HERE / "bd3_gen.S").write_text("\n".join(asm) + "\n")
    return sum(1 for t in stem for _, v in t if v)


def tern_table(t: Tern) -> str:
    low = t.name.lower()
    fns = [t.fn(b) for b in range(len(t.blocks))]
    chans = ", ".join(f"{r.start}-{r.stop - 1}" for r in t.blocks)
    return (
        f"/* {t.name}: {sum(t.nz)} non-zero weights, blocks of output channels {chans} */\n"
        f"bd3_gen_fn {', '.join(fns)};\n"
        f"static bd3_gen_fn *const bd3_{low}_blk[{len(fns)}] = {{ {', '.join(fns)} }};\n\n"
    )


def tern_init(t: Tern) -> str:
    return f"{{ {len(t.blocks)}, bd3_{t.name.lower()}_blk }}"


def write_model(src: dict, arrays, desc, tv, terns: list[Tern]) -> None:
    kinds = list(tv["kind"])
    sums = [mix_sum(tv["L9"][kinds.index(k)].reshape(-1)) for k in SELFTEST]
    name = Path(src["model"]).name
    commit = src["fenykl_nn_commit"]
    pp = desc["postprocess"]
    out = [
        (
            f"/* Generated by gen_model.py from fenykl-nn {name} (commit {commit}).\n"
            "   Do not edit. Layout: net.h.\n"
            "   Trained by the separate fenykl-nn project on the bird class of the Open Images Dataset V7\n"
            "   (annotations CC BY 4.0, Google LLC; images listed as CC BY 2.0) and on iNaturalist Open Data\n"
            "   bird photos (licensed per photo: CC0, CC BY or CC BY-NC). No images are included here. */\n"
        ),
        '#include "net.h"\n\n',
        f'const char bd3_model_name[] = "{name}";\n',
        f'const char bd3_model_commit[] = "{commit[:12]}";\n',
        f"const int32_t bd3_threshold = {int(desc['threshold']['int32'])};\n",
        f"const uint8_t bd3_centroid_k = {int(pp['centroid_k'])};\n",
        c_array("uint8_t", "bd3_centroid_table", pp["centroid_table"], CENTROID_N, ""),
        "\n",
    ]
    out += [tern_table(t) for t in terns]
    out += [
        f"const struct bd3_tern bd3_l2 = {tern_init(terns[0])};\n",
        "const struct bd3_tern bd3_trunk[BD3_TRUNK] = {\n"
        + "".join(f"\t{tern_init(t)},\n" for t in terns[1:])
        + "};\n\n",
        f"/* {', '.join(SELFTEST)} */\n",
        f"const uint32_t bd3_selftest_sum[BD3_PATTERNS] = {{ {', '.join(f'0x{s:08x}u' for s in sums)} }};\n",
    ]
    (HERE / "bd3_model.c").write_text("".join(out))
    total = sum(sum(t.nz) for t in terns)
    print(f"bd3_model.c: {name} {commit[:12]}, {total} ternary non-zero weights, threshold "
          f"{desc['threshold']['int32']}, centroid k {pp['centroid_k']} table {pp['centroid_table']}, "
          f"self-test sums {[hex(s) for s in sums]}")


def post(tv, prefix: str, i: int) -> dict:
    """The expected post-processing of head i: score, fire, cell (row, col), point_q4 (x, y)."""
    return {
        "score": int(tv[prefix + "score"][i]),
        "fire": bool(tv[prefix + "fire"][i]),
        "cell": [int(v) for v in tv[prefix + "cell"][i]],
        "point_q4": [int(v) for v in tv[prefix + "point_q4"][i]],
    }


def write_vectors(src: dict, tv) -> None:
    """Per vector: the input planes, every layer's rotate-add checksum (CHW bytes, head int32), the
    head and its post-processing; then the synthetic heads and their post-processing."""
    layers = [f"L{i}" for i in range(10)]
    vectors = []
    for i, kind in enumerate(tv["kind"]):
        vectors.append(
            {
                "kind": str(kind),
                "sums": [mix_sum(tv[n][i].reshape(-1)) for n in layers],
                "head": [int(x) for x in tv["L9"][i].reshape(-1)],
                **post(tv, "", i),
                "y": base64.b64encode(tv["input"][i].tobytes()).decode(),
                "uv": base64.b64encode(tv["uv"][i].tobytes()).decode(),
            }
        )
    doc = {
        "model": Path(src["model"]).name,
        "commit": src["fenykl_nn_commit"],
        "layers": layers,
        "selftest": SELFTEST,
        "vectors": vectors,
        "post": [
            {"head": [int(x) for x in tv["post_head"][i].reshape(-1)], **post(tv, "post_", i)}
            for i in range(len(tv["post_head"]))
        ],
    }
    VECTORS.parent.mkdir(parents=True, exist_ok=True)
    VECTORS.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"{VECTORS}: {len(vectors)} vectors, {len(doc['post'])} synthetic heads")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handoff", type=Path, help="fenykl-nn handoff directory of the model")
    args = ap.parse_args()
    src = json.loads((args.handoff / "source.json").read_text())
    with np.load(args.handoff / "model.npz") as z:
        arrays = {k: z[k] for k in z.files}
    desc = json.loads((args.handoff / "model.json").read_text())
    with np.load(args.handoff / "testvectors.npz") as z:
        tv = {k: z[k] for k in z.files}
    check_model(desc)
    gk = gen_kernels()
    terns = [tern_layer(gk, arrays, desc, n) for n in TERN]
    write_model(src, arrays, desc, tv, terns)
    stem_nz = write_gen(gk, arrays, terns)
    print(f"bd3_gen.S: stem {stem_nz} non-zero taps after the chroma fold")
    for t in terns:
        print(f"bd3_gen.S: {t.name} {len(t.blocks)} blocks, output channels "
              f"{[f'{r.start}-{r.stop - 1}' for r in t.blocks]}")
    write_vectors(src, tv)


if __name__ == "__main__":
    main()
