"""Program 13, the resampler (command 0xC): its model, built from the stock program at runtime.

No word of the program is decoded. The model is the arithmetic measured from outside, with the
stock program unmodified on host-written buffers and own taps; it applies to the stock program
as a whole, so every byte it computes is at most inferred.

The request (cmd 0xC's CPU setup, `commands.resample`): the 32 tap words in local words
0x00..0x1F, src, dst, the table pointer and the geometry halfwords (src stride, width, height;
dst stride, width, height) in local words 0x742..0x747. The table lives in DRAM: one entry
{s32 a, u32 b} per output line, a the source line in 16.16 and b the horizontal step in 16.16.

The image is format 0x306 as the job sees it: three planes of W x H bytes at stride 64, each
resampled from its own lines [verified: line maps of identity and constant-line tables]. The
law, per plane c, output line i with entry (a, b), output pixel x [inferred: model fit, every
byte of the camera runs the README's replay covers, unless tagged]:

- the kernel: plane 0 uses the job's taps, phase p = taps (p, 0..3), s16 with 256 = 1.0, word
  2p = tap 0 | tap 1 << 16, word 2p + 1 = tap 2 | tap 3 << 16; planes 1 and 2 ignore the taps
  and use the fixed (0, 256 - 16p, 16p, 0) [verified: tap patches reach plane 0 only];
- b = 1: the kernel acts down the lines, phase p = frac(a) x 16, tap k on source line
  floor(a) - 1 + k, line -1 read as line 0 [verified: impulse responses];
- b != 1 (integer a only): the kernel acts along the line at u(x) = u0 + b x, u0 = 31.5 (1 - b)
  held in [-16, 16) (beyond that range a hypothesis: one run at b = 2), phase p = frac(u) x 16,
  tap k on pixel floor(u) - 1 + k, a window starting left of pixel 0 reads 32 pixels further
  right. On plane 0 a vertical pass at phase 0 comes first: the model knows a byte only where
  that pass is exact (each pixel's sum a multiple of 256 inside 0..255, as with the stock phase
  0 kernel or a source constant down the lines), since no rounding of its intermediate fits
  every byte of a source that varies down the lines;
- the sum S of tap x pixel becomes clamp(R64(R4(S)), 0, 255), R_n = divide by n to nearest,
  ties to even: a Q6 intermediate, then 8 bits;
- unknown: a nonzero tap on a line past H - 1 (the bottom edge reads irregularly: the next
  plane, or other lines) or on a pixel past 63 (the right edge reads stale content at b != 1),
  a fractional a with b != 1 and W < 64 with b != 1 (never ran).

Column blocks: the job runs in 32-pixel column blocks with a held line pair per block. A partial
block (W not a multiple of 32) is not computed: its pixels are the held pair, line i getting
held line i % 2, which a job fills from its source lines H - 2 and H - 1 [verified at H 16
after identity jobs, which cannot tell source from output lines: after other jobs and at
other heights a hypothesis]. Pixels W..63 are not written. On a fresh boot the pair is
unknown. H 1: plane 0 is written as a line pair (line 0 twice), then planes 1 and 2, 64 bytes
past 3 x 64 x H [verified: one run]; an odd H above 1 never ran, so its dst is unknown.
"""

import struct
from dataclasses import dataclass
from fractions import Fraction
from math import floor

from .commands import RESAMPLE_REQUEST, RESAMPLE_TAPS, TAP_WORDS
from .emu import LOCAL_BASE, Boot, Emulator, Job, JobResult, Status, dram_alias
from .fields import FIELDS, Confidence
from .program import Program, stock_programs
from .semantics import (
    IDLE_CLASS,
    OpaqueClass,
    Outcome,
    Part,
    Trace,
    UnknownSemantics,
    WordClass,
)
from .state import Bytes
from .word import IDLE

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

ITEM = 13
PLANES = 3
STRIDE = 64
"""The only stride that ran: the model holds at stride 64."""
H_MAX = 16
BLOCK = 32
ONE = 0x10000
B_RANGE = (0x4000, 0x20000)
"""The steps that ran lie in this range (0.25 .. 2)."""
HELD = H_MAX
"""The height at which the held line pairs were seen."""


def chroma(p: int) -> tuple[int, int, int, int]:
    return (0, 256 - 16 * p, 16 * p, 0)


def rhe(n: int, d: int) -> int:
    """n / d rounded to nearest, ties to even."""
    q, r = divmod(n, d)
    return q + (1 if 2 * r > d or (2 * r == d and q & 1) else 0)


def out_of(s: int) -> int:
    """The output byte of a sum of tap x pixel."""
    return max(0, min(255, rhe(rhe(s, 4), 64)))


def taps_phases(data: bytes) -> list[tuple[int, ...]]:
    """16 phases of 4 s16 taps from the 32 tap words."""
    v = struct.unpack("<64h", data)
    return [v[4 * p : 4 * p + 4] for p in range(16)]


def u0(b: int) -> tuple[Fraction, bool]:
    """The horizontal source position of pixel 0 at step b, and whether it was held into
    [-16, 16) (the hypothesis part)."""
    raw = Fraction(63, 2) * (1 - Fraction(b, ONE))
    held = (raw + 16) % 32 - 16
    return held, held != raw


@dataclass(frozen=True)
class Request:
    """The request words as the setup writes them to local 0x742..0x747."""

    src: int
    dst: int
    table: int
    stride: int
    width: int
    height: int
    dst_geometry: tuple[int, int, int]

    @classmethod
    def read(cls, boot: Boot) -> "Request":
        words = [boot.u32(RESAMPLE_REQUEST + 4 * k) for k in range(6)]
        if None in words:
            raise ValueError("local memory holds no resampler request (no setup ran)")
        src, dst, table, w3, w4, w5 = words
        assert w3 is not None and w4 is not None and w5 is not None
        return cls(
            src,
            dst,
            table,
            w3 & 0xFFFF,
            w3 >> 16,
            w4 & 0xFFFF,
            (w4 >> 16, w5 & 0xFFFF, w5 >> 16),
        )

    def check(self) -> None:
        if self.dst_geometry != (self.stride, self.width, self.height):
            raise ValueError(f"{self}: dst geometry differs from src (never ran)")
        if self.stride != STRIDE or not 1 <= self.width <= STRIDE:
            raise ValueError(f"{self}: only stride 64 and W <= 64 ran")
        if not 1 <= self.height <= H_MAX:
            raise ValueError(f"{self}: only H 1..16 ran")


class _Pixels:
    """The source image of a job: plane c, line y, pixel x."""

    def __init__(self, data: Bytes, h: int) -> None:
        self.data, self.h = data, h

    def at(self, c: int, y: int, x: int) -> tuple[int, int] | None:
        """(value, taint), None if unknown."""
        i = (c * self.h + y) * STRIDE + x
        if not self.data.known[i]:
            return None
        return self.data.data[i], self.data.taint[i]


class _Miss(Exception):
    """A byte the law does not give; `why` names the unknown source."""

    def __init__(self, why: str) -> None:
        self.why = why


_WHY = {
    "bottom": "a nonzero tap on a line past H - 1: the bottom edge reads irregularly",
    "right": "a nonzero tap on a pixel past 63: the right edge reads stale content",
    "2d": "plane 0 at b != 1: the vertical pass is not exact here, its rounding not known",
    "frac": "a fractional a with b != 1 never ran",
    "narrow": "W < 64 with b != 1 never ran",
    "range": "a table entry outside the range that ran",
    "input": "the job reads unknown bytes (src, table or taps)",
}


class Resample13Model:
    """The model of program 13, built from the stock program (`from_image` reads it from a
    stock E-M10 II `.BIN`). Its classes are the idle word and one class per distinct stock word,
    matched by value; the law applies to the stock program run as it is."""

    def __init__(self, program: Program) -> None:
        self.program = program
        positions: dict[int, list[int]] = {}
        for index, word in enumerate(program.words):
            if word != IDLE:
                positions.setdefault(word.value, []).append(index)
        part = Part(
            "program 13 word",
            Outcome.DATA_UNKNOWN,
            "not decoded: what the job does without it is not known",
        )
        self.opaque = [
            OpaqueClass(
                f"word {at[0]}",
                "an undecoded stock word of the resampler",
                {f.id: f.get(program.words[at[0]].value) for f in FIELDS},
                frozenset(),
                confidence=I,
                part=part,
                positions=frozenset(at),
            )
            for at in positions.values()
        ]
        self.classes: tuple[WordClass, ...] = (IDLE_CLASS, *self.opaque)

    @classmethod
    def from_image(cls, image: bytes) -> "Resample13Model":
        return cls(stock_programs(image)[ITEM])

    def apply(self, emu: Emulator, job: Job, trace: Trace) -> JobResult:
        boot = emu.boot
        cap = min(I, trace.taint)
        stock = self.program.words
        edited = [
            i
            for i in range(max(len(stock), len(job.program.words)))
            if i >= len(stock)
            or i >= len(job.program.words)
            or job.program.words[i] != stock[i]
        ]
        if edited:
            at = edited[0]
            if emu.strict:
                fields = (
                    job.program.words[at].changed(stock[at])
                    if at < min(len(stock), len(job.program.words))
                    else []
                )
                raise UnknownSemantics(
                    at,
                    f"stock word {at}",
                    fields,
                    "no word of program 13 is decoded: an edited program has no semantics",
                )
            trace.notes.append(f"words {edited} differ from the stock program")
            cap = H
        req = Request.read(boot)
        req.check()
        h, w = req.height, req.width
        result = JobResult(Status.OK, ignored=trace.ignored, notes=trace.notes)
        result.taint = cap
        footprint = PLANES * STRIDE * h + (STRIDE if h % 2 else 0)
        result.stored = (req.dst, req.dst + footprint)
        if h % 2 and h > 1:
            why = boot.source("resampler odd H", "an odd H above 1 never ran")
            boot.dram.write(dram_alias(req.dst), Bytes.unknown(footprint, why))
            boot.lines.forget(why)
            return result
        taps_b = boot.local.read(LOCAL_BASE + RESAMPLE_TAPS, 4 * TAP_WORDS)
        taps = taps_phases(bytes(taps_b.data)) if all(taps_b.known) else None
        taps_taint = min(taps_b.taint) if taps is not None else V
        table_b = boot.read(req.table, 8 * h)
        entries = [
            struct.unpack_from("<iI", table_b.data, 8 * i)
            if all(table_b.known[8 * i : 8 * i + 8])
            else None
            for i in range(h)
        ]
        table_taint = min(table_b.taint) if all(table_b.known) else V
        px = _Pixels(boot.read(req.src, PLANES * STRIDE * h), h)
        law = boot.source("resampler", "program 13's measured law")
        why = {k: boot.source("resampler unknown", v) for k, v in _WHY.items()}
        base_taint = min(cap, taps_taint, table_taint)
        full = w // BLOCK * BLOCK
        # H 1: plane 0 is a line pair (line 0 twice), the chroma planes follow it.
        lines0 = max(h, 2)
        for c in range(PLANES):
            plane = 0 if c == 0 else STRIDE * (lines0 + (c - 1) * h)
            for i in range(lines0 if c == 0 else h):
                entry = entries[min(i, h - 1)]
                line = Bytes(w)
                for x in range(full):
                    try:
                        if entry is None or (c == 0 and taps is None):
                            raise _Miss("input")
                        v, t = self._pixel(px, taps, c, entry, x, w)
                    except _Miss as m:
                        line.src[x] = why[m.why]
                        continue
                    line.data[x], line.known[x] = v, 1
                    line.taint[x], line.src[x] = min(base_taint, t), law
                    line.off[x] = plane + STRIDE * i + x
                if full < w:
                    held = boot.lines.read((2 * c + i % 2) * STRIDE + full, w - full)
                    line = line[:full] + held
                boot.dram.write(dram_alias(req.dst + plane + STRIDE * i), line)
        self._hold(boot, req, entries, px, full)
        return result

    def _pixel(
        self,
        px: _Pixels,
        taps: list[tuple[int, ...]] | None,
        c: int,
        entry: tuple[int, int],
        x: int,
        w: int,
    ) -> tuple[int, Confidence]:
        """The output byte at pixel x of plane c for table entry (a, b), and its taint."""
        a, b = entry
        h = px.h
        if not 0 <= a <= (h - 1) << 16 or not B_RANGE[0] <= b <= B_RANGE[1]:
            raise _Miss("range")
        base = a >> 16
        taint = V

        def get(y: int, xx: int) -> int:
            nonlocal taint
            if y > h - 1:
                raise _Miss("bottom")
            if xx > STRIDE - 1:
                raise _Miss("right")
            p = px.at(c, max(0, y), xx)
            if p is None:
                raise _Miss("input")
            taint = min(taint, p[1])
            return p[0]

        if b == ONE:
            if a & 0xFFF:
                taint = H  # a phase between the 16 never ran
            p = (a & 0xFFFF) >> 12
            kernel = taps[p] if c == 0 else chroma(p)
            s = sum(t * get(base - 1 + k, x) for k, t in enumerate(kernel) if t)
            return out_of(s), Confidence(taint)
        if a & 0xFFFF:
            raise _Miss("frac")
        if w != STRIDE:
            raise _Miss("narrow")
        start, wrapped = u0(b)
        if wrapped:
            taint = H
        u = start + Fraction(b, ONE) * x
        fu = floor(u)
        q = floor((u - fu) * 16)
        if fu - 1 < 0:
            fu += 32
        kernel = taps[q] if c == 0 else chroma(q)
        s = 0
        for k, t in enumerate(kernel):
            if not t:
                continue
            xx = fu - 1 + k
            if c != 0:
                s += t * get(base, xx)
                continue
            assert taps is not None
            v = sum(tv * get(base - 1 + j, xx) for j, tv in enumerate(taps[0]) if tv)
            if v % 256 or not 0 <= v <= 255 * 256:
                raise _Miss("2d")
            s += t * (v // 256)
        return out_of(s), Confidence(taint)

    def _hold(
        self,
        boot: Boot,
        req: Request,
        entries: list[tuple[int, int] | None],
        px: _Pixels,
        full: int,
    ) -> None:
        """The held line pairs after the job: source lines H - 2 and H - 1 of the full blocks."""
        h = req.height
        if h < 2:
            boot.lines.forget(
                boot.source(
                    "resampler held lines", "what an H 1 job holds is not known"
                )
            )
            return
        identity = all(e == (i << 16, ONE) for i, e in enumerate(entries))
        taint = I if h == HELD and identity else H
        for c in range(PLANES):
            for r in range(2):
                y = h - 2 + r
                o = (c * h + y) * STRIDE
                boot.lines.write(
                    (2 * c + r) * STRIDE, px.data[o : o + full].capped(taint)
                )
