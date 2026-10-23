"""Generate every model-dependent source of birddetect6 from a fenykl-nn handoff directory (model.npz,
model.json, testvectors.npz, source.json): bd6_model.c (the power-of-two layers' block tables, fire
threshold, centroid table and shift, self-test sums and post-processing), bd6_gen.S (the stem and
head as generated int8 code, the power-of-two layers (weights {0, +-1, +-2, +-4}, Horner by
magnitude) with the mul epilogue as generated code split into blocks of output channels, all by
the gen_kernels.py emitters) and the simulator vectors
tests/sim/birddetect6/vectors.json (the self-test patterns, the constant inputs and NOISE other
vectors: every layer and the post-processing; the post-processing of the synthetic heads).
Needs NumPy; run with any python3 that has it:

    python3 gen_model.py --handoff <handoff dir>
"""

import argparse
import base64
import json
from dataclasses import dataclass
from pathlib import Path

import gen_kernels as gk
import numpy as np

HERE = Path(__file__).resolve().parent
VECTORS = HERE.parents[2] / "tests" / "sim" / "birddetect6" / "vectors.json"
MASK = 0xFFFFFFFF

# net.h
PIN_PITCH = 2 * 96 + 4
L0_PLANE = 36 * 48
CELLS = 9 * 12
HEAD_C = 2  # BD6_HEAD_C: heat map, presence
CENTROID_N = 16  # BD6_CENTROID_N
P3_PLANE = 14 * 11  # BD6_P3_PLANE
# Output pixels per call of the stem and the head (BD6_GEN_PX): 2 keeps the stem in the I-cache.
PX = 2
SHAPES = {
    "L0": ("conv", [3, 72, 96], [12, 36, 48], 3, 2, "int8"),
    "L1": ("maxpool2", [12, 36, 48], [12, 18, 24]),
    "L2": ("conv", [12, 18, 24], [16, 18, 24], 3, 1, "pot"),
    "L3": ("maxpool2", [16, 18, 24], [16, 9, 12]),
    "L4": ("conv", [16, 9, 12], [32, 9, 12], 3, 1, "pot"),
    **{f"L{i}": ("conv", [32, 9, 12], [32, 9, 12], 3, 1, "pot") for i in range(5, 10)},
    "L10": ("conv", [32, 9, 12], [HEAD_C, 9, 12], 1, 1, "int8"),
}
HEAD = "L10"
# Power-of-two layers and whether they write pair words of padded planes (the next layer is
# power-of-two) or
# u8 CHW planes (a pool or the head reads them).
POT = {
    "L2": False,
    "L4": True,
    "L5": True,
    "L6": True,
    "L7": True,
    "L8": True,
    "L9": False,
}
# Blocks per power-of-two layer: the output channels in this many functions of about equal code
# (gen_kernels.balanced_cost), 1 = the whole layer as one function. L6-L8 in 7: in 6 blocks L6 and L7
# assembled to blocks of up to 7344 and 7180 B, over the 7168 B budget, and L8 to 7111 B.
BLOCKS = {"L2": 3, "L4": 5, "L5": 7, "L6": 7, "L7": 7, "L8": 7, "L9": 6}
SELFTEST = [
    "hgradient",
    "vgradient",
    "checker",
    "hgradient_neutral",
    "u0_v255",
]  # enum bd6_pattern
# Vectors with input planes in vectors.json; the other random-noise vectors are left out.
NOISE = 2


def mix_sum(values) -> int:
    """Rotate-add checksum of 32-bit values (bd6_logit_sum, the simulator test's layer sums)."""
    s = 0
    for v in values:
        s = (((s << 1) | (s >> 31)) + (int(v) & MASK)) & MASK
    return s


def check_model(desc) -> None:
    inp = desc["input"]
    assert (inp["width"], inp["height"], inp["source"], inp["chroma"]) == (
        96,
        72,
        "ipu",
        "G",
    ), inp
    layers = desc["layers"]
    assert [lay["name"] for lay in layers] == list(SHAPES), (
        "layer list differs from net.h"
    )
    for lay in layers:
        want = SHAPES[lay["name"]]
        got = (lay["op"], lay["in_shape"], lay["out_shape"])
        if lay["op"] == "conv":
            got += (lay["k"], lay["stride"], lay["wtype"])
        assert got == want, (lay["name"], got, want)
    assert layers[-1]["act"] == "none"
    # The head this port implements (intspec.md "Head post-processing"): heat map and presence sum.
    cfg = desc["config"]
    assert cfg.get("presence") == "mean", (
        f"presence head {cfg.get('presence')!r} not supported"
    )
    assert not cfg.get("fine", False), "fine heat map not supported"
    assert cfg.get("requant") == "mul", f"requant {cfg.get('requant')!r} not supported"
    # gen_kernels.pot_lane_range: inputs 0..255.
    assert cfg.get("act_max") == 255, f"act_max {cfg.get('act_max')!r} not supported"
    assert layers[-1]["out_shape"][0] == HEAD_C, (
        f"head has {layers[-1]['out_shape'][0]} channels"
    )
    # inject.c: peak - logit and the presence sum over all cells fit int32.
    assert CELLS * layers[-1]["acc_bound"] < 1 << 31, layers[-1]["acc_bound"]
    pp = desc["postprocess"]
    table = pp["centroid_table"]
    # inject.c: den >= table[0] > 0.
    assert (
        len(table) == CENTROID_N and table[0] > 0 and all(0 <= v <= 255 for v in table)
    ), table
    assert 0 <= pp["centroid_k"] < 31, pp["centroid_k"]
    for lay in layers[:-1]:
        if lay["op"] == "conv":
            want = (
                "mul_shift_round_clamp_u8"
                if lay["name"] in POT
                else "shift_round_clamp_u8"
            )
            assert lay["act"] == want, (lay["name"], lay["act"])


def rounded_bias(arrays, name) -> list[int]:
    shift = arrays[name + ".shift"].astype(np.int64)
    rnd = np.where(shift > 0, np.left_shift(1, np.maximum(shift - 1, 0)), 0)
    return [int(v) for v in arrays[name + ".b"].astype(np.int64) + rnd]


def c_array(
    ctype: str, name: str, values, per_line: int = 12, storage: str = "static "
) -> str:
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
class Pot:
    name: str
    cv: gk.Conv
    wt: gk.Weights
    nz: list[int]  # non-zero weights per output channel
    blocks: list[list[int]]  # output channels per generated function

    def fn(self, b: int) -> str:
        return f"bd6_{self.name.lower()}_{b}"


def pot_layer(arrays, desc, name: str) -> Pot:
    """A power-of-two layer with the mul epilogue, split into BLOCKS[name] blocks of output channels
    of about equal code, each within the I-cache budget unless the layer is one function.
    model_layer asserts the Horner lane ranges and the input offset range."""
    cv, wt = gk.model_layer(arrays, desc, name)
    nz = [sum(1 for v in row if v) for row in wt.w]
    cost = gk.pot_mul_cost10(wt.w)
    blocks = gk.balanced_cost(cost, BLOCKS[name])
    for b in blocks:
        est = sum(cost[o] for o in b) // 10
        assert len(blocks) == 1 or est <= gk.SPLIT_BUDGET, (
            f"{name}: a block of ~{est} B"
        )
    return Pot(name, cv, wt, nz, blocks)


def chan_text(chans: list[int]) -> str:
    if chans == list(range(chans[0], chans[-1] + 1)):
        return f"{chans[0]}-{chans[-1]}"
    return ",".join(map(str, chans))


def pair_at(o: int, j: int) -> list[int]:
    """Pair-word output, A1 at word (x, y + 1) of the padded planes: pixel x + j is the low half of
    word x + j + 1 and the high half of word x + j."""
    return [4 * (o * P3_PLANE + j + 1), 4 * (o * P3_PLANE + j) + 2]


def pot_asm(t: Pot) -> list[str]:
    at = pair_at if POT[t.name] else None
    last = pair_at(t.cv.cout - 1, 3)[0] if at else t.cv.cout * t.cv.h * t.cv.w
    assert last < 1 << 15, f"{t.name}: output offsets beyond d16"
    asm = []
    for b, chans in enumerate(t.blocks):
        est = sum(gk.pot_mul_cost10(t.wt.w)[o] for o in chans) // 10
        note = f"{t.name} block {b}: channels {chan_text(chans)}, 4 output pixels, ~{est} B"
        asm += gk.kernel(t.fn(b), note, gk.body_pot(t.cv, t.wt, chans, at), False)
    return asm


def write_gen(arrays, pots: list[Pot]) -> int:
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
    wh = arrays[HEAD + ".w"]
    head = [
        [(c * CELLS, int(wh[o, c, 0, 0])) for c in range(wh.shape[1])]
        for o in range(HEAD_C)
    ]
    hbias = [int(b) for b in arrays[HEAD + ".b"]]
    hbody = gk.int8_body(head, hbias, None, 1, PX, lambda j, o: 4 * (o * CELLS + j))
    asm = [
        "/* Generated by gen_model.py. Do not edit. Layout: net.h. */",
        "",
        gk.MACROS,
        gk.MUL_MACROS,
        gk.POT_MACROS,
        *gk.kernel("bd6_stem", f"L0: {PX} output pixels, all channels", body, PX == 4),
        *gk.kernel(
            "bd6_head", f"{HEAD}: {PX} cells, {HEAD_C} channels, int32", hbody, PX == 4
        ),
    ]
    for t in pots:
        asm += pot_asm(t)
    (HERE / "bd6_gen.S").write_text("\n".join(asm) + "\n")
    return sum(1 for t in stem for _, v in t if v)


def pot_table(t: Pot) -> str:
    low = t.name.lower()
    fns = [t.fn(b) for b in range(len(t.blocks))]
    chans = "; ".join(chan_text(b) for b in t.blocks)
    return (
        f"/* {t.name}: {sum(t.nz)} non-zero weights, blocks of output channels {chans} */\n"
        f"bd6_gen_fn {', '.join(fns)};\n"
        f"static bd6_gen_fn *const bd6_{low}_blk[{len(fns)}] = {{ {', '.join(fns)} }};\n\n"
    )


def pot_init(t: Pot) -> str:
    return f"{{ {len(t.blocks)}, bd6_{t.name.lower()}_blk }}"


def det_init(tv, i: int) -> str:
    """struct bd6_det of network vector i."""
    p = post(tv, "", i)
    return f"{{ {p['score']}, {str(p['fire']).lower()}, {', '.join(map(str, p['cell'] + p['point_q4']))} }}"


def write_model(src: dict, arrays, desc, tv, pots: list[Pot]) -> None:
    kinds = list(tv["kind"])
    pats = [kinds.index(k) for k in SELFTEST]
    sums = [mix_sum(tv[HEAD][i].reshape(-1)) for i in pats]
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
        '#include "inject.h"\n#include "net.h"\n\n',
        f'const char bd6_model_name[] = "{name}";\n',
        f'const char bd6_model_commit[] = "{commit[:12]}";\n',
        f"const int32_t bd6_threshold = {int(desc['threshold']['int32'])};\n",
        f"const uint8_t bd6_centroid_k = {int(pp['centroid_k'])};\n",
        c_array("uint8_t", "bd6_centroid_table", pp["centroid_table"], CENTROID_N, ""),
        "\n",
    ]
    out += [pot_table(t) for t in pots]
    out += [
        f"const struct bd6_pot bd6_l2 = {pot_init(pots[0])};\n",
        "const struct bd6_pot bd6_trunk[BD6_TRUNK] = {\n"
        + "".join(f"\t{pot_init(t)},\n" for t in pots[1:])
        + "};\n\n",
        f"/* {', '.join(SELFTEST)} */\n",
        f"const uint32_t bd6_selftest_sum[BD6_PATTERNS] = {{ {', '.join(f'0x{s:08x}u' for s in sums)} }};\n",
        "const struct bd6_det bd6_selftest_det[BD6_PATTERNS] = {\n"
        + "".join(f"\t{det_init(tv, i)},\n" for i in pats)
        + "};\n",
    ]
    (HERE / "bd6_model.c").write_text("".join(out))
    total = sum(sum(t.nz) for t in pots)
    print(
        f"bd6_model.c: {name} {commit[:12]}, {total} power-of-two non-zero weights, threshold "
        f"{desc['threshold']['int32']}, centroid k {pp['centroid_k']} table {pp['centroid_table']}, "
        f"self-test sums {[hex(s) for s in sums]}"
    )


def post(tv, prefix: str, i: int) -> dict:
    """The expected post-processing of head i: score, fire, cell (row, col), point_q4 (x, y)."""
    return {
        "score": int(tv[prefix + "score"][i]),
        "fire": bool(tv[prefix + "fire"][i]),
        "cell": [int(v) for v in tv[prefix + "cell"][i]],
        "point_q4": [int(v) for v in tv[prefix + "point_q4"][i]],
    }


def vector_input(tv, i: int, kind: str) -> dict:
    """Nothing for a self-test pattern (the simulator test computes it with bd6_net_pattern), the
    value of a constant input, else the planes (base64)."""
    if kind in SELFTEST:
        return {}
    y, uv = tv["input"][i], tv["uv"][i]
    if y.min() == y.max() == uv.min() == uv.max():
        return {"fill": int(y.flat[0])}
    return {
        "y": base64.b64encode(y.tobytes()).decode(),
        "uv": base64.b64encode(uv.tobytes()).decode(),
    }


def compact_json(doc: dict) -> str:
    """JSON with one line per item of the lists vectors and post."""
    out = []
    for k, v in doc.items():
        if k in ("vectors", "post"):
            items = ",\n  ".join(json.dumps(x, separators=(",", ":")) for x in v)
            out.append(f'"{k}": [\n  {items}\n ]')
        else:
            out.append(f"{json.dumps(k)}: {json.dumps(v)}")
    return "{\n " + ",\n ".join(out) + "\n}\n"


def write_vectors(src: dict, tv) -> None:
    """Per vector (the self-test patterns, the constant inputs, the first NOISE others): every
    layer's rotate-add checksum (CHW bytes, head int32), the head and its post-processing, the
    input (vector_input); then the synthetic heads and their post-processing."""
    layers = list(SHAPES)
    vectors = []
    for i, kind in enumerate(map(str, tv["kind"])):
        inp = vector_input(tv, i, kind)
        if "y" in inp and sum("y" in v for v in vectors) == NOISE:
            continue
        vectors.append(
            {
                "kind": kind,
                "sums": [mix_sum(tv[n][i].reshape(-1)) for n in layers],
                "head": [int(x) for x in tv[HEAD][i].reshape(-1)],
                **post(tv, "", i),
                **inp,
            }
        )
    doc = {
        "model": Path(src["model"]).name,
        "commit": src["fenykl_nn_commit"],
        "layers": layers,
        "selftest": SELFTEST,
        "vectors": vectors,
        "post": [
            {
                "head": [int(x) for x in tv["post_head"][i].reshape(-1)],
                **post(tv, "post_", i),
            }
            for i in range(len(tv["post_head"]))
        ],
    }
    VECTORS.parent.mkdir(parents=True, exist_ok=True)
    VECTORS.write_text(compact_json(doc))
    print(
        f"{VECTORS}: {len(vectors)} of {len(tv['kind'])} vectors, {len(doc['post'])} synthetic heads"
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--handoff",
        type=Path,
        required=True,
        help="fenykl-nn handoff directory of the model",
    )
    args = ap.parse_args()
    src = json.loads((args.handoff / "source.json").read_text())
    with np.load(args.handoff / "model.npz") as z:
        arrays = {k: z[k] for k in z.files}
    desc = json.loads((args.handoff / "model.json").read_text())
    with np.load(args.handoff / "testvectors.npz") as z:
        tv = {k: z[k] for k in z.files}
    check_model(desc)
    pots = [pot_layer(arrays, desc, n) for n in POT]
    write_model(src, arrays, desc, tv, pots)
    stem_nz = write_gen(arrays, pots)
    print(f"bd6_gen.S: stem {stem_nz} non-zero taps after the chroma fold")
    for t in pots:
        print(
            f"bd6_gen.S: {t.name} {len(t.blocks)} blocks, output channels "
            f"{[chan_text(b) for b in t.blocks]}"
        )
    write_vectors(src, tv)


if __name__ == "__main__":
    main()
