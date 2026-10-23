"""The code emitters of gen_model.py: MN103 assembly with the weights compiled into the code.

Two code shapes, each a function called once per group of output pixels for its output channels:
- int8 (int8_body, the stem and the head): per weight `movbu` + `udf00 #w` + `add` (+-1: `add` /
  `sub` only, 0: nothing), 1, 2 or 4 pixels per call (4: the accumulators of pixels 2 and 3 in
  A2, A3), then shift, clamp and store, or the int32 sums for the head.
- power-of-two ({0, +-1, +-2, +-4}, body_pot): pair words over padded CHW planes (two
  neighbouring pixels per word, 16-bit lanes), per non-zero weight two `mov` + two `add` / `sub`
  for 4 pixels, Horner by magnitude (the +-4 taps, both accumulators << 1, the +-2 taps, << 1, the
  +-1 taps; POT_MACROS), lanes from 0x2000 to 0x8000 + sum, then the mul epilogue (MUL_MACROS:
  one `udf00` per output) with the bias in its constant.

A power-of-two layer is split into blocks of output channels, one function per block, of about
equal code (balanced_cost on the estimate of pot_mul_cost10) within the I-cache (SPLIT_BUDGET).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Conv:
    """A 3x3 stride 1 convolution with zero padding 1 over CHW planes: h x w in and out."""

    cin: int
    cout: int
    h: int
    w: int

    @property
    def hp(self) -> int:
        return self.h + 2

    @property
    def wp(self) -> int:
        return self.w + 2


@dataclass
class Weights:
    w: list[list[int]]  # [o][tap], tap = (c * 3 + ky) * 3 + kx
    bias: list[int]  # without the rounding term
    shift: list[int]
    mult: list[int]  # the mul epilogue: (acc * m + round) >> s


def int8_body(
    taps: list[list[tuple[int, int]]],
    bias: list[int],
    shift: list[int] | None,
    step: int,
    px: int,
    at,
) -> list[str]:
    """int8 code for all output channels. taps[o]: (byte offset from A0 for pixel 0, weight) per
    weight (zeros are skipped), pixel j at offset + j * step; bias[o] with the rounding term;
    shift None: raw int32 sums (the head); at(j, o): byte offset from A1 of output j, o."""
    out = []
    for o, ot in enumerate(taps):
        nzt = [(off, w) for off, w in ot if w]
        assert all(-127 <= w <= 127 for _, w in nzt), "udf00 imm8 weights only"
        accs = ["d2", "d3", "a2", "a3"][:px]
        out += [f"mov {bias[o]},{a}" for a in accs]
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
            if a.startswith("a"):
                out.append(f"mov {a},d0")
                a = "d0"
            if shift is None:
                out.append(f"mov {a},({at(j, o)},a1)")
            else:
                out.append(f"RQ {a},{shift[o]},{at(j, o)}")
    return out


def tap_line(cv: Conv, t: int, w: int) -> str:
    """The pair-word tap of weight w's sign at tap t = (c * 3 + ky) * 3 + kx."""
    c, ky, kx = t // 9, t // 3 % 3, t % 3
    return f"{'TA' if w > 0 else 'TS'} {4 * ((c * cv.hp + ky) * cv.wp + kx)}"


def lane_epilogue(cv: Conv, wt: Weights, o: int, at) -> list[str]:
    """Stores the 4 output pixels of channel o from the lanes 0x8000 + acc of d2, d3; the constant
    k = round + (bias - 0x8000) * m folds the bias into the mul. at(o, j): the byte offsets from A1
    that output pixel j is stored to (None: one byte, CHW planes)."""
    m, s = wt.mult[o], wt.shift[o]
    k = (1 << (s - 1)) + (wt.bias[o] - 0x8000) * m
    out = []
    for j in range(4):
        first, *more = [o * cv.h * cv.w + j] if at is None else at(o, j)
        out.append(f"LANE{j % 2}M {'d3' if j > 1 else 'd2'},{m},{k},{s},{first}")
        # RQ leaves the clamped byte in d0
        out += [f"movbu d0,({off},a1)" for off in more]
    return out


POT_GROUPS = (4, 2, 1)


def pot_levels(row: list[int]) -> int:
    """Magnitude groups a power-of-two channel runs: from its largest |w| (4, 2 or 1) down to 1."""
    top = max((abs(w) for w in row), default=1)
    return POT_GROUPS[::-1].index(top) + 1


def pot_lane_range(row: list[int], xmax: int = 255) -> list[tuple[int, int]]:
    """The worst-case lane range [lo, hi] over all inputs 0..xmax at the end of each group the
    channel runs (any partial sum inside a group lies in the same range)."""
    levels = pot_levels(row)
    lo = hi = 0x8000 >> (levels - 1)
    out = []
    for j, g in enumerate(POT_GROUPS[-levels:]):
        if j:
            lo, hi = 2 * lo, 2 * hi
        lo -= xmax * sum(1 for w in row if w == -g)
        hi += xmax * sum(1 for w in row if w == g)
        out.append((lo, hi))
    return out


def body_pot(cv: Conv, wt: Weights, chans: list[int], at) -> list[str]:
    """Power-of-two code for the output channels chans, output layout as lane_epilogue. Both lanes
    start at 0x8000 >> (levels - 1), the +-4 taps, a shift left, the +-2 taps, a shift, the +-1
    taps (groups above the channel's largest |w| skipped): the lanes end at 0x8000 + sum w * x."""
    out = []
    for o in chans:
        row = wt.w[o]
        levels = pot_levels(row)
        init = (0x8000 >> (levels - 1)) * 0x10001
        out += [f"mov {init},d2", f"mov {init},d3"]
        for j, g in enumerate(POT_GROUPS[-levels:]):
            if j:
                out.append("TSHL")
            out += [tap_line(cv, t, w) for t, w in enumerate(row) if abs(w) == g]
        out += lane_epilogue(cv, wt, o, at)
    return out


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

/* pair words: +1 / -1 at byte offset off for pixels 0-1 (d2) and 2-3 (d3) */
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


MUL_MACROS = """\
/* the mul epilogue: lane (0x8000 + acc) * m + k with k = round - 0x8000 * m, then RQ */
	.macro	LANE0M r, m, k, s, off
	mov	\\r,d0
	exthu	d0
	udf00	\\m,d0
	add	\\k,d0
	RQ	d0,\\s,\\off
	.endm

	.macro	LANE1M r, m, k, s, off
	mov	\\r,d0
	lsr	16,d0
	udf00	\\m,d0
	add	\\k,d0
	RQ	d0,\\s,\\off
	.endm
"""


POT_MACROS = """\
/* the Horner shift of the power-of-two kernel: both pair accumulators << 1 (1-byte add) */
	.macro	TSHL
	add	d2,d2
	add	d3,d3
	.endm
"""


def kernel(name: str, comment: str, body: list[str], four_acc: bool) -> list[str]:
    """One generated function `void name(const void *in, void *out)`: in to A0, out to A1, d2, d3
    (and a2, a3 for four accumulators) saved; `name_end` marks its end."""
    regs = "[d2,d3,a2,a3]" if four_acc else "[d2,d3]"
    frame = 16 if four_acc else 8
    return [
        f'\t.section .text.{name},"ax"',
        f"\t.global _{name}, _{name}_end",
        f"/* {comment} */",
        f"_{name}:",
        f"\tmovm {regs},(sp)",
        "\tmov d0,a0",
        "\tmov d1,a1",
        *(f"\t{line}" for line in body),
        f"\tret {regs},{frame}",
        f"_{name}_end:",
        "",
    ]


def pot_mul_cost10(rows: list[list[int]]) -> list[int]:
    """Estimated code per power-of-two output channel in tenths of a byte: 11.2 bytes per non-zero
    weight, 128 per channel, 8 for the mul epilogue, 2 per Horner shift."""
    return [
        112 * sum(1 for w in row if w) + 1280 + 80 + 20 * (pot_levels(row) - 1)
        for row in rows
    ]


def balanced_cost(cost: list[int], k: int) -> list[list[int]]:
    """k blocks of about equal code (per channel cost): channels largest first, each into the
    smallest block so far, then moves and swaps out of the largest block while they make it smaller."""
    blocks: list[list[int]] = [[] for _ in range(k)]
    size = [0] * k
    for o in sorted(range(len(cost)), key=lambda o: -cost[o]):
        b = size.index(min(size))
        blocks[b].append(o)
        size[b] += cost[o]
    while (step := better(blocks, size, cost)) is not None:
        b, c, i, j = step
        blocks[b].remove(i)
        blocks[c].append(i)
        size[b] -= cost[i]
        size[c] += cost[i]
        if j is not None:
            blocks[c].remove(j)
            blocks[b].append(j)
            size[c] -= cost[j]
            size[b] += cost[j]
    return [sorted(b) for b in blocks]


def better(blocks, size, cost):
    """(largest block, other block, channel to move, channel to move back or None) that leaves both
    smaller than the largest block was, or None."""
    b = size.index(max(size))
    for c in range(len(blocks)):
        for i in blocks[b] if c != b else ():
            for j in [None, *blocks[c]]:
                d = cost[i] - (0 if j is None else cost[j])
                if d > 0 and size[c] + d < size[b]:
                    return b, c, i, j
    return None


# Estimated code bytes per block at most: one I-cache's worth with room for the driver.
SPLIT_BUDGET = 7168


def model_layer(arrays, desc, name: str) -> tuple[Conv, Weights]:
    """A trained 3x3 power-of-two layer with the mul epilogue (act mul_shift_round_clamp_u8)."""
    lay = next(lay for lay in desc["layers"] if lay["name"] == name)
    assert (lay["op"], lay["k"], lay["stride"], lay["wtype"]) == (
        "conv",
        3,
        1,
        "pot",
    ), lay
    assert lay["act"] == "mul_shift_round_clamp_u8", lay
    cin, h, w = lay["in_shape"]
    cout = lay["out_shape"][0]
    cv = Conv(cin, cout, h, w)
    wa = arrays[name + ".w"]
    assert wa.shape == (cout, cin, 3, 3), (name, wa.shape)
    shift = [int(s) for s in arrays[name + ".shift"]]
    mult = [int(m) for m in arrays[name + ".mult"]]
    assert all(1 <= m <= 255 for m in mult) and all(s >= 1 for s in shift), name
    bias = [int(b) for b in arrays[name + ".b"]]
    rows = [[int(v) for v in wa[o].reshape(-1)] for o in range(cout)]
    assert all(v in (-4, -2, -1, 0, 1, 2, 4) for row in rows for v in row), name
    # Each lane stays in 0..0x7FFF before a shift (bit 15 would cross into the other lane)
    # and in 0..0xFFFF at the end, for any tap order inside a group.
    for row in rows:
        r = pot_lane_range(row)
        assert min(lo for lo, _ in r) >= 0, f"{name}: a lane can go negative"
        assert all(hi < 0x8000 for _, hi in r[:-1]), f"{name}: bit 15 before a shift"
        assert r[-1][1] <= 0xFFFF, f"{name}: a lane can exceed 16 bits"
    assert 4 * cin * cv.hp * cv.wp + 8 < 1 << 15, f"{name}: offsets beyond d16"
    return cv, Weights(rows, bias, shift, mult)
