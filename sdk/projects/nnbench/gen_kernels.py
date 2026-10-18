#!/usr/bin/env python3
"""Writes a plugin's nb_gen.S and nb_gen.c: conv kernels with the weights compiled into the code.

Usage: python3 gen_kernels.py nnbench|nnbench2 [OUTDIR]   (default: the plugin's directory)

The weights are the ones nb_conv_weights (fam_conv.c) builds at run time for the same parameters;
this script repeats that computation, so the C reference and the generated code agree (the
simulator test tests/sim/nnbench checks it bit-exactly).

Two code shapes, both called once per group of output pixels for all output channels:
- int8 (any weights but ternary): padded HWC input (A0 = the receptive field of the first pixel),
  per weight `movbu` + `udf00 #w` + `add` (+-1: `add`/`sub` only, 0: nothing), 1, 2 or 4 pixels
  per call (4: the accumulators of pixels 2 and 3 in A2, A3), then shift, clamp and store (HWC
  output at A1), or the int32 sums for the head.
- ternary: pair words over padded CHW planes (two neighbouring pixels per word, 16-bit lanes of
  0x8000 + sum), per non-zero weight two `mov` + two `add`/`sub` for 4 pixels, CHW output at A1.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
MASK = 0xFFFFFFFF
NB_F_CONV = 0
FL_DW, FL_TERN, FL_I4, FL_RAW = 1, 2, 4, 8


@dataclass(frozen=True)
class Conv:
    cin: int
    cout: int
    h: int
    w: int
    kh: int = 3
    kw: int = 3
    s: int = 1
    d: int = 1
    fl: int = 0
    nz: int = 0

    @property
    def params(self) -> tuple[int, ...]:
        return (
            self.cin,
            self.cout,
            self.h,
            self.w,
            self.kh,
            self.kw,
            self.s,
            self.d,
            self.fl,
            self.nz,
        )

    @property
    def ph(self) -> int:
        return self.d * (self.kh // 2)

    @property
    def pw(self) -> int:
        return self.d * (self.kw // 2)

    @property
    def hp(self) -> int:
        return self.h + 2 * self.ph

    @property
    def wp(self) -> int:
        return self.w + 2 * self.pw

    @property
    def ho(self) -> int:
        return (self.hp - self.d * (self.kh - 1) - 1) // self.s + 1

    @property
    def wo(self) -> int:
        return (self.wp - self.d * (self.kw - 1) - 1) // self.s + 1

    @property
    def nc(self) -> int:
        """Input channels per output channel."""
        return 1 if self.fl & FL_DW else self.cin

    @property
    def taps(self) -> int:
        return self.nc * self.kh * self.kw


# nb_rand, nb_seed, nb_shift and nb_conv_weights of cases.c / fam_conv.c.
def nb_rand(state: list[int]) -> int:
    state[0] = (state[0] * 1664525 + 1013904223) & MASK
    return state[0] >> 8


def nb_seed(params: tuple[int, ...], fam: int) -> int:
    x = 0x9E3779B9 ^ fam
    for p in params:
        x = ((x ^ p) * 0x01000193) & MASK
    return x


def nb_shift(n: int, base: int) -> int:
    s = base
    while n >= 4:
        n >>= 2
        s += 1
    return s


@dataclass
class Weights:
    w: list[list[int]]  # [o][tap], tap = (c * kh + ky) * kw + kx
    bias: list[int]  # with the rounding term
    shift: list[int]


def conv_weights(cv: Conv) -> Weights:
    x = [nb_seed(cv.params, NB_F_CONV)]
    out = Weights([], [], [])
    for _ in range(cv.cout):
        left, row = cv.nz, []
        for t in range(cv.taps):
            v = nb_rand(x)
            if cv.fl & FL_TERN:
                wt = 0
                if v % (cv.taps - t) < left:
                    left -= 1
                    wt = 1 if nb_rand(x) & 1 else -1
            elif cv.fl & FL_I4:
                wt = v % 15 - 7
            else:
                wt = v % 255 - 127
            row.append(wt)
        if cv.fl & FL_TERN:
            sh = nb_shift(cv.nz, 1)
            bias = nb_rand(x) % 201 - 100
        else:
            sh = nb_shift(cv.taps, 3 if cv.fl & FL_I4 else 7)
            bias = nb_rand(x) % 2001 - 1000
        if cv.fl & FL_RAW:
            sh = 0
        out.w.append(row)
        out.shift.append(sh)
        out.bias.append(bias + (1 << (sh - 1) if sh else 0))
    return out


def taps(cv: Conv, o: int):
    """(c, ky, kx) of every tap of output channel o, in weight order."""
    for t in range(cv.taps):
        c = o if cv.fl & FL_DW else t // (cv.kh * cv.kw)
        r = t % (cv.kh * cv.kw)
        yield t, c, r // cv.kw, r % cv.kw


def body_int8(cv: Conv, wt: Weights, px: int) -> list[str]:
    raw = bool(cv.fl & FL_RAW)
    ob = 4 if raw else 1
    out = []
    for o in range(cv.cout):
        nzt = [
            (((ky * cv.d) * cv.wp + kx * cv.d) * cv.cin + c, wt.w[o][t])
            for t, c, ky, kx in taps(cv, o)
            if wt.w[o][t]
        ]
        accs = ["d2", "d3", "a2", "a3"][:px]
        out += [f"mov {wt.bias[o]},{a}" for a in accs]
        step = cv.s * cv.cin
        if px == 4:
            out += [
                f"M4 {off},{off + step},{off + 2 * step},{off + 3 * step},{w}"
                for off, w in nzt
            ]
        elif px == 2:
            out += [f"M2 {off},{off + step},{w}" for off, w in nzt]
        else:
            out += [
                f"M1 {o1},{w1},{o2},{w2}"
                for (o1, w1), (o2, w2) in zip(nzt[::2], nzt[1::2])
            ]
            if len(nzt) % 2:
                out.append(f"M1S {nzt[-1][0]},{nzt[-1][1]}")
        for j, a in enumerate(accs):
            at = (j * cv.cout + o) * ob
            if a.startswith("a"):
                out.append(f"mov {a},d0")
                a = "d0"
            out.append(f"mov {a},({at},a1)" if raw else f"RQ {a},{wt.shift[o]},{at}")
    return out


def body_tern(cv: Conv, wt: Weights) -> list[str]:
    plane = cv.ho * cv.wo
    out = []
    for o in range(cv.cout):
        init = ((0x8000 + wt.bias[o]) * 0x10001) & MASK
        out += [f"mov {init},d2", f"mov {init},d3"]
        for t, c, ky, kx in taps(cv, o):
            w = wt.w[o][t]
            if w:
                out.append(
                    f"{'TA' if w > 0 else 'TS'} {4 * ((c * cv.hp + ky * cv.d) * cv.wp + kx * cv.d)}"
                )
        for j, a in enumerate(["d2", "d3"]):
            at = o * plane + 2 * j
            out += [
                f"LANE0 {a},{wt.shift[o]},{at}",
                f"LANE1 {a},{wt.shift[o]},{at + 1}",
            ]
    return out


@dataclass(frozen=True)
class Gen:
    cv: Conv
    px: int  # 1, 2 or 4 (int8), 4 (ternary pairs)


NNBENCH = [
    # int8 3x3: the stem, a small channel grid (the code-size sweep for int8), 1 vs 2 pixels
    Gen(Conv(1, 12, 72, 96, s=2), 2),
    Gen(Conv(1, 12, 72, 96, s=2), 1),
    Gen(Conv(4, 8, 16, 16), 2),
    Gen(Conv(8, 8, 16, 16), 2),
    Gen(Conv(8, 8, 16, 16), 1),
    Gen(Conv(8, 16, 16, 16), 2),
    # depthwise, pointwise, head
    Gen(Conv(16, 16, 16, 16, fl=FL_DW), 2),
    Gen(Conv(32, 32, 16, 16, fl=FL_DW), 2),
    Gen(Conv(8, 16, 16, 16, 1, 1), 2),
    Gen(Conv(16, 32, 16, 16, 1, 1), 2),
    Gen(Conv(32, 1, 9, 12, 1, 1, fl=FL_RAW), 2),
    Gen(Conv(16, 1, 9, 12, 1, 1, fl=FL_RAW), 2),
    # other shapes
    Gen(Conv(8, 8, 16, 16, 1, 3), 2),
    Gen(Conv(8, 8, 16, 16, 3, 1), 2),
    # ternary: the shipped network's shapes, then the code-size sweep (cout 16, cin 4..64)
    Gen(Conv(12, 16, 18, 24, fl=FL_TERN, nz=38), 4),
    Gen(Conv(32, 32, 9, 12, fl=FL_TERN, nz=40), 4),
    Gen(Conv(4, 16, 12, 12, fl=FL_TERN, nz=12), 4),
    Gen(Conv(8, 16, 12, 12, fl=FL_TERN, nz=24), 4),
    Gen(Conv(16, 16, 12, 12, fl=FL_TERN, nz=48), 4),
    Gen(Conv(32, 16, 12, 12, fl=FL_TERN, nz=96), 4),
    Gen(Conv(64, 16, 12, 12, fl=FL_TERN, nz=120), 4),
    Gen(Conv(32, 32, 16, 16, fl=FL_DW | FL_TERN, nz=6), 4),
    Gen(Conv(32, 32, 12, 12, 1, 1, fl=FL_TERN, nz=11), 4),
    Gen(Conv(16, 16, 12, 12, 5, 5, fl=FL_TERN, nz=64), 4),
    Gen(Conv(16, 16, 12, 12, 1, 3, fl=FL_TERN, nz=16), 4),
]

# nnbench2: int8 with 4 pixels per call, on cases nnbench has gen (2 px) and gen1 for
NNBENCH2 = [
    Gen(Conv(1, 12, 72, 96, s=2), 4),
    Gen(Conv(4, 8, 16, 16), 4),
    Gen(Conv(16, 16, 16, 16, fl=FL_DW), 4),
    Gen(Conv(8, 16, 16, 16, 1, 1), 4),
]

GENS = {"nnbench": NNBENCH, "nnbench2": NNBENCH2}

MACROS = """\
/* acc += reg * w for a loaded reg: +-1 add / sub, else udf00 #w (1 cycle) and add */
	.macro	MAC w, r, acc
	.if	\\w == 1
	add	\\r,\\acc
	.elseif	\\w == -1
	sub	\\r,\\acc
	.else
	udf00	\\w,\\r
	add	\\r,\\acc
	.endif
	.endm

/* int8, 2 pixels: weight w at byte offsets off (pixel 0) and off1 (pixel 1) into d2, d3 */
	.macro	M2 off, off1, w
	movbu	(\\off,a0),d0
	movbu	(\\off1,a0),d1
	.if	(\\w != 1) && (\\w != -1)
	udf00	\\w,d0
	udf00	\\w,d1
	.endif
	.if	\\w == -1
	sub	d0,d2
	sub	d1,d3
	.else
	add	d0,d2
	add	d1,d3
	.endif
	.endm

/* int8, 4 pixels: weight w at offsets o0..o3 into d2, d3, a2, a3 */
	.macro	M4S r, acc, w
	.if	\\w == -1
	sub	\\r,\\acc
	.else
	add	\\r,\\acc
	.endif
	.endm

	.macro	M4 o0, o1, o2, o3, w
	movbu	(\\o0,a0),d0
	movbu	(\\o1,a0),d1
	.if	(\\w != 1) && (\\w != -1)
	udf00	\\w,d0
	udf00	\\w,d1
	.endif
	M4S	d0,d2,\\w
	M4S	d1,d3,\\w
	movbu	(\\o2,a0),d0
	movbu	(\\o3,a0),d1
	.if	(\\w != 1) && (\\w != -1)
	udf00	\\w,d0
	udf00	\\w,d1
	.endif
	M4S	d0,a2,\\w
	M4S	d1,a3,\\w
	.endm

/* int8, 1 pixel: two weights per step (loads two ahead), one for an odd tail */
	.macro	M1 o1, w1, o2, w2
	movbu	(\\o1,a0),d0
	movbu	(\\o2,a0),d1
	MAC	\\w1,d0,d2
	MAC	\\w2,d1,d2
	.endm

	.macro	M1S o, w
	movbu	(\\o,a0),d0
	MAC	\\w,d0,d2
	.endm

/* ternary pair words: +1 / -1 at byte offset off for pixels 0-1 (d2) and 2-3 (d3) */
	.macro	TA off
	mov	(\\off,a0),d0
	mov	(\\off+8,a0),d1
	add	d0,d2
	add	d1,d3
	.endm

	.macro	TS off
	mov	(\\off,a0),d0
	mov	(\\off+8,a0),d1
	sub	d0,d2
	sub	d1,d3
	.endm

/* shift, clamp to 0..255, store byte */
	.macro	RQ r, s, off
	.if	\\s
	asr	\\s,\\r
	.endif
	cmp	0,\\r
	bge	1f
	clr	\\r
	bra	2f
1:	cmp	255,\\r
	ble	2f
	mov	255,\\r
2:	movbu	\\r,(\\off,a1)
	.endm

/* the low / high 16-bit lane of a pair accumulator, minus 0x8000, through RQ */
	.macro	LANE0 r, s, off
	mov	\\r,d0
	exthu	d0
	add	-32768,d0
	RQ	d0,\\s,\\off
	.endm

	.macro	LANE1 r, s, off
	mov	\\r,d0
	lsr	16,d0
	add	-32768,d0
	RQ	d0,\\s,\\off
	.endm
"""


def check(g: Gen) -> None:
    cv = g.cv
    tern = bool(cv.fl & FL_TERN)
    assert g.px == 4 or not tern
    assert cv.wo % g.px == 0
    if tern:
        assert cv.s == 1 and cv.nz * 255 + 600 < 32768


def main() -> None:
    plugin = sys.argv[1]
    gens = GENS[plugin]
    asm = [
        "/* Generated by gen_kernels.py. Do not edit. */",
        "",
        MACROS,
    ]
    c = [
        "/* Generated by gen_kernels.py. Do not edit. */",
        '#include "nnbench.h"' if plugin == "nnbench" else '#include "../nnbench/nnbench.h"',
        "",
    ]
    table = []
    for i, g in enumerate(gens):
        check(g)
        wt = conv_weights(g.cv)
        tern = bool(g.cv.fl & FL_TERN)
        body = body_tern(g.cv, wt) if tern else body_int8(g.cv, wt, g.px)
        regs = "[d2,d3,a2,a3]" if g.px == 4 and not tern else "[d2,d3]"
        frame = 16 if g.px == 4 and not tern else 8
        name = f"nb_g{i}"
        asm += [
            f'\t.section .text.{name},"ax"',
            f"\t.global _{name}, _{name}_end",
            f"/* {g.cv.params} px {g.px} */",
            f"_{name}:",
            f"\tmovm {regs},(sp)",
            "\tmov d0,a0",
            "\tmov d1,a1",
            *(f"\t{line}" for line in body),
            f"\tret {regs},{frame}",
            f"_{name}_end:",
            "",
        ]
        c.append(f"void {name}(const void *in, void *out);")
        c.append(f"extern const char {name}_end[];")
        params = ", ".join(str(p) for p in g.cv.params)
        table.append(
            f"\t{{ NB_F_CONV, {g.px}, {{ {params} }}, {name}, (const char *){name}, {name}_end }},"
        )
    c += ["", "const struct nb_gen nb_gens[] = {", *table, "};", ""]
    c.append("const unsigned int nb_ngen = sizeof(nb_gens) / sizeof(nb_gens[0]);")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / plugin
    (out / "nb_gen.S").write_text("\n".join(asm) + "\n")
    (out / "nb_gen.c").write_text("\n".join(c) + "\n")


if __name__ == "__main__":
    main()
