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
law, per plane c, output line i with entry (a, b), output pixel x [inferred: a model fit that
reproduces every byte it gives of 626 camera jobs (stock and own taps, b 0.5, 1, 1 + 1/16, 2,
W 16, 48, 64, H 1, 16), 0 off; the tags below are those of the measurements behind it]:

- the kernel: plane 0 uses the job's taps, phase p = taps (p, 0..3), s16 with 256 = 1.0, word
  2p = tap 0 | tap 1 << 16, word 2p + 1 = tap 2 | tap 3 << 16; planes 1 and 2 ignore the taps
  and use the fixed (0, 256 - 16p, 16p, 0) [verified: tap patches reach plane 0 only]; every
  tap of the s16 range acts as its value at b = 1, one-tap gains to 32767 exact and partial
  sums of +-2 x 32767 x 252 unsaturated [verified]; at b != 1 only taps in -637..2048 ran and
  with an edited program at b = 1 only taps in -1840..2048, so there the bytes a tap outside
  them reaches are a hypothesis;
- b = 1: the kernel acts down the lines, phase p = frac(a) x 16, tap k on source line
  floor(a) - 1 + k, line -1 read as line 0 [verified: impulse responses];
- b != 1 (integer a only): the kernel acts along the line at u(x) = u0 + b x, u0 = 31.5 (1 - b)
  held in [-16, 16) (beyond that range a hypothesis: one run at b = 2), phase p = frac(u) x 16,
  tap k on pixel floor(u) - 1 + k, a window starting left of pixel 0 reads 32 pixels further
  right. Plane 0 is two passes: down the lines at phase 0 (taps[0]) to I = R4(sum of tap x
  pixel), unclamped, then along the line T = sum of tap x I, where horizontal phase 0 is the
  unit (0, 256, 0, 0) and not taps[0], and the byte is clamp(R16384(T), 0, 255): one rounding
  [verified: designed runs at b = 1 + 1/16 and 0.5 on sources constant down the lines, with a
  non-unit taps[0] and sums that separate one rounding from two, and ties]. I's rounding is
  [inferred: it fits every byte of earlier b != 1 runs on sources varying down the lines; no
  designed run separated it];
- b = 1: the sum S of tap x pixel becomes clamp(R64(R4(S)), 0, 255), R_n = divide by n to
  nearest, ties to even: a Q6 intermediate, then 8 bits (the b != 1 arithmetic with a unit
  horizontal pass, R16384(256 I) = R64(I));
- unknown: a nonzero tap on a line past H - 1 (the bottom edge reads irregularly: the next
  plane, or other lines) or on a pixel past 63 (the right edge reads stale content at b != 1),
  a fractional a with b != 1 and W < 64 with b != 1 (never ran).

Column blocks: the job runs in 32-pixel column blocks with a held line pair per block. In a
partial block (W not a multiple of 32) of an H 16 identity job with a unit phase-0 kernel
(the stock one) line i gets held line i % 2, which a job fills from its source lines H - 2 and
H - 1 [verified at H 16 after identity jobs with the stock taps, which cannot tell source from
output lines: after other jobs a hypothesis]. In other jobs the partial block holds other
content (at W 48 with a one-tap kernel per line, lines that read the same held line differ), so
it is unknown. Pixels W..63 are not written. On a fresh boot
the pair is unknown. H 1: plane 0 is written as a line pair (line 0 twice, or line 0 and a
read of table entry 1: the run cannot tell, so line 1 is a hypothesis), then planes 1 and 2,
64 bytes past 3 x 64 x H [verified: one run]; an odd H above 1 never ran, so its dst is
unknown.

Edits: an edited program runs only as one of the edits measured on the camera
(`resample_edits`), on the job classes it ran on; see `Resample13Model._effects`.
"""

import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import floor

from . import commands
from .commands import RESAMPLE_REQUEST, RESAMPLE_TAPS, TAP_WORDS
from .emu import LOCAL_BASE, Boot, Emulator, Job, JobResult, Status, dram_alias
from .fields import FIELDS, Confidence
from .program import Program, stock_programs
from .resample_edits import (
    DROP0,
    FACTS,
    FLAT,
    STOCK,
    SUBSTITUTE_KEEPERS,
    SUBSTITUTE_MOVER,
    Changed,
    Edit,
    Effect,
    Fact,
    Operand,
    Operands,
    Phase,
    Planes,
    Probe,
    Stock,
    Substitute,
    TapLoad,
)
from .semantics import (
    IDLE_CLASS,
    OpaqueClass,
    Outcome,
    Part,
    Trace,
    UnknownSemantics,
    WordClass,
)
from .state import Bytes, Source
from .taps import Taps
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
TAPS_RAN = (-0x8000, 0x7FFF)
"""The taps that ran on plane 0 with the stock program at b = 1: the whole s16 range."""
NARROW_TAPS_RAN = (-637, 2048)
"""The taps that ran on plane 0 at b != 1: gains -2.49 .. 8.0."""
EDITED_TAPS_RAN = (-1840, 2048)
"""The taps that ran on plane 0 at b = 1 with an edited program: gains -7.19 .. 8.0."""


def chroma(p: int) -> tuple[int, int, int, int]:
    return (0, 256 - 16 * p, 16 * p, 0)


def rhe(n: int, d: int) -> int:
    """n / d rounded to nearest, ties to even."""
    q, r = divmod(n, d)
    return q + (1 if 2 * r > d or (2 * r == d and q & 1) else 0)


def out_of(s: int) -> int:
    """The output byte of a sum of tap x pixel."""
    return max(0, min(255, rhe(rhe(s, 4), 64)))


def footprint(h: int) -> int:
    """The dst bytes a job of height h writes (pixels W..63 of each line included)."""
    return PLANES * STRIDE * h + (STRIDE if h % 2 else 0)


def _unit(kernel: tuple[int, ...]) -> bool:
    return tuple(kernel) == chroma(0)


def _gain_taint(kernel: tuple[int, ...], ran: tuple[int, int]) -> Confidence:
    lo, hi = ran
    return H if any(not lo <= t <= hi for t in kernel) else V


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
    "frac": "a fractional a with b != 1 never ran",
    "narrow": "W < 64 with b != 1 never ran",
    "range": "a table entry outside the range that ran",
    "input": "the job reads unknown bytes (src, table or taps)",
    "operand": "an edit's operand that is not known reaches this byte",
    "partial": "a partial column block of a job that is not an H 16 identity with a unit phase-0 "
    "kernel: it holds other content than the held lines",
}


class Resample13Model:
    """The model of program 13, built from the stock program (`from_image` reads it from a
    stock E-M10 II `.BIN`). Its classes are the idle word, one class per distinct stock word and
    one per measured edited word, matched by value; the law applies to the stock program run as
    it is, and to the measured edits (`facts`, default the camera's) on the job classes they
    ran on."""

    def __init__(self, program: Program, facts: Sequence[Fact] = FACTS) -> None:
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
        self.edits = _measured_edits(program, facts)
        words = program.words
        self.mover = {(i, k.apply(words[i]).value) for i, k in (SUBSTITUTE_MOVER,)}
        self.keepers = {(i, k.apply(words[i]).value) for i, k in SUBSTITUTE_KEEPERS}
        edited = [
            OpaqueClass(
                f"word {i}, {e.kind.name}",
                "an edited stock word of the resampler, measured on the camera",
                {f.id: f.get(value) for f in FIELDS},
                frozenset(),
                confidence=I,
                part=part,
                positions=frozenset({i}),
            )
            for (i, value), e in self.edits.items()
        ]
        self.classes: tuple[WordClass, ...] = (IDLE_CLASS, *edited, *self.opaque)

    @classmethod
    def from_image(
        cls, image: bytes, facts: Sequence[Fact] = FACTS
    ) -> "Resample13Model":
        return cls(stock_programs(image)[ITEM], facts)

    def job(
        self,
        taps: Taps,
        src: int,
        dst: int,
        table: int,
        width: int,
        height: int,
        edits: Sequence[tuple[int, Edit]] = (),
    ) -> Job:
        """The cmd 0xC job of the stock program with `taps` as its tap words, at stride 64, and
        each (index, kind) of `edits` applied to its stock word."""
        words = list(self.program.words)
        for i, kind in edits:
            words[i] = kind.apply(words[i])
        program = Program(tuple(words), taps.pack())
        return commands.resample(program, src, dst, table, STRIDE, width, height)

    def apply(self, emu: Emulator, job: Job, trace: Trace) -> JobResult:
        boot = emu.boot
        req = Request.read(boot)
        req.check()
        h, w = req.height, req.width
        result = JobResult(Status.OK, ignored=trace.ignored, notes=trace.notes)
        n = footprint(h)
        result.stored = (req.dst, req.dst + n)
        edited = self._edited(emu, job)
        taps_b = boot.local.read(LOCAL_BASE + RESAMPLE_TAPS, 4 * TAP_WORDS)
        taps = Taps.unpack(bytes(taps_b.data)).phases if all(taps_b.known) else None
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
        probe = _probe(px, entries, w) if edited else None
        effects = (
            self._effects(emu, job, edited, probe, result) if edited else STOCK_PLANES
        )
        self._move_substitute(boot, job, edited, probe)
        cap = result.taint = H if effects is None else min(I, trace.taint)
        effects = effects or STOCK_PLANES
        if h % 2 and h > 1:
            why = boot.source("resampler odd H", "an odd H above 1 never ran")
            boot.dram.write(dram_alias(req.dst), Bytes.unknown(n, why))
            boot.lines.forget(why)
            return result
        law = boot.source("resampler", "program 13's measured law")
        why = {k: boot.source(f"resampler {k}", v) for k, v in _WHY.items()}
        base_taint = min(cap, taps_taint, table_taint)
        full = w // BLOCK * BLOCK
        identity = h == HELD and all(e == (i << 16, ONE) for i, e in enumerate(entries))
        partial = why["partial"]
        # H 1: plane 0 is a line pair, the chroma planes follow it. Line 1 as a copy of line 0
        # and as a read of table entry 1 gave the same bytes: a hypothesis.
        lines0 = max(h, 2)
        ran = EDITED_TAPS_RAN if edited else TAPS_RAN
        for c in range(PLANES):
            plane = 0 if c == 0 else STRIDE * (lines0 + (c - 1) * h)
            effect = effects[c]
            for i in range(lines0 if c == 0 else h):
                if isinstance(effect, Changed):
                    changed = boot.source("resampler edit", effect.why)
                    line = Bytes.unknown(w, changed)
                    boot.dram.write(dram_alias(req.dst + plane + STRIDE * i), line)
                    continue
                entry = entries[min(i, h - 1)]
                line = Bytes(w)
                for x in range(full):
                    try:
                        if entry is None or (c == 0 and taps is None):
                            raise _Miss("input")
                        v, t = self._pixel(px, taps, c, entry, x, w, effect, ran)
                    except _Miss as m:
                        line.src[x] = why[m.why]
                        continue
                    line.data[x], line.known[x] = v, 1
                    if i >= h:
                        t = H
                    line.taint[x], line.src[x] = min(base_taint, t), law
                    line.off[x] = plane + STRIDE * i + x
                unit = c != 0 or taps is not None and _unit(taps[0])
                if full < w and identity and unit and not edited:
                    held = boot.lines.read((2 * c + i % 2) * STRIDE + full, w - full)
                    line = line[:full] + held
                elif full < w:
                    line = line[:full] + Bytes.unknown(w - full, partial)
                boot.dram.write(dram_alias(req.dst + plane + STRIDE * i), line)
        if edited:
            boot.lines.forget(
                boot.source(
                    "resampler held lines", "what an edited job holds is not known"
                )
            )
        else:
            self._hold(boot, req, entries, taps, px, full)
        return result

    def _edited(self, emu: Emulator, job: Job) -> list[int]:
        """The indices of the words that differ from the stock program. In strict mode a word
        that is no measured edit raises UnknownSemantics."""
        stock, words = self.program.words, job.program.words
        both = min(len(stock), len(words))
        edited = [
            i
            for i in range(max(len(stock), len(words)))
            if i >= both or words[i] != stock[i]
        ]
        for at in edited:
            if not emu.strict or at < both and (at, words[at].value) in self.edits:
                continue
            raise UnknownSemantics(
                at,
                f"stock word {at}",
                words[at].changed(stock[at]) if at < both else [],
                "no word of program 13 is decoded and no measured edit gives this word",
            )
        return edited

    def _effects(
        self,
        emu: Emulator,
        job: Job,
        edited: list[int],
        probe: Probe | None,
        result: JobResult,
    ) -> Planes | None:
        """What each plane does in a job with these edited words: one measured edit on a class
        it ran on gives its planes [verified]; anything else is unknown in strict mode and None
        in lenient mode (the stock law, as a hypothesis)."""
        words = job.program.words
        edits = [
            self.edits.get((i, words[i].value)) if i < len(words) else None
            for i in edited
        ]
        e = edits[0]
        if len(edited) > 1:
            why = "edits of two or more words never ran together"
        elif e is None:
            why = "no measured edit gives this word"
        elif probe not in e.planes:
            ran = "; ".join(str(p) for p in e.planes)
            why = f"word {edited[0]} with its {e.kind.name} ran only on: {ran}"
        else:
            result.edits.append(
                (f"word {edited[0]}, {e.kind.name}, on {probe}: {e.fact}", V)
            )
            p0, p1, p2 = (_substituted(emu.boot, p) for p in e.planes[probe])
            return p0, p1, p2
        result.notes.append(why)
        return (Changed(why),) * PLANES if emu.strict else None

    def _move_substitute(
        self, boot: Boot, job: Job, edited: list[int], probe: Probe | None
    ) -> None:
        """A stock job keeps the tap-0 substitute; on a flat source at W 64 the mover sets it
        to 0 from its boot value or 0 and a keeper keeps it; any other edited job makes it
        unknown, and the mover does not bring it back from there."""
        if not edited:
            return
        words = job.program.words
        i = edited[0]
        key = (i, words[i].value) if len(edited) == 1 and i < len(words) else None
        if probe == FLAT and key in self.keepers:
            return
        if probe == FLAT and key in self.mover:
            if boot.substitute_at_boot or boot.substitute == 0:
                boot.substitute = 0
        else:
            boot.substitute = None
        boot.substitute_at_boot = False

    def _pixel(
        self,
        px: _Pixels,
        taps: tuple[tuple[int, ...], ...] | None,
        c: int,
        entry: tuple[int, int],
        x: int,
        w: int,
        effect: Effect,
        ran: tuple[int, int],
    ) -> tuple[int, Confidence]:
        """The output byte at pixel x of plane c for table entry (a, b) under an edit's
        `effect` on the plane, and its taint; at b = 1 taps outside `ran` taint it."""
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
            if isinstance(effect, TapLoad):
                kernel = effect.kernel(kernel)
            taint = min(Confidence(taint), _gain_taint(kernel, ran))
            if isinstance(effect, Phase):
                return _phase(get, base, kernel, x, effect.q), taint
            if isinstance(effect, Operands):
                return _operands(effect, kernel, lambda j: get(base - 1 + j, x)), taint
            s = sum(t * get(base - 1 + k, x) for k, t in enumerate(kernel) if t)
            return out_of(s), taint
        assert isinstance(effect, Stock), "edit laws hold at b = 1 only"
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
        if c != 0:
            s = sum(t * get(base, fu - 1 + k) for k, t in enumerate(chroma(q)) if t)
            return out_of(s), Confidence(taint)
        assert taps is not None
        kernel = taps[q] if q else chroma(0)
        narrow = NARROW_TAPS_RAN
        taint = min(taint, _gain_taint(kernel, narrow), _gain_taint(taps[0], narrow))
        s = 0
        for k, t in enumerate(kernel):
            if not t:
                continue
            xx = fu - 1 + k
            v = sum(tv * get(base - 1 + j, xx) for j, tv in enumerate(taps[0]) if tv)
            # R4 fits the runs that vary down the lines; the designed runs did not test it.
            s += t * rhe(v, 4)
        return max(0, min(255, rhe(s, 1 << 14))), Confidence(taint)

    def _hold(
        self,
        boot: Boot,
        req: Request,
        entries: list[tuple[int, int] | None],
        taps: tuple[tuple[int, ...], ...] | None,
        px: _Pixels,
        full: int,
    ) -> None:
        """The held line pairs after the job: source lines H - 2 and H - 1 of the full blocks.

        Inferred only where source and output lines are the same (an H 16 identity table with
        a unit phase-0 kernel), since the camera has not told them apart."""
        h = req.height
        if h < 2:
            boot.lines.forget(
                boot.source(
                    "resampler held lines", "what an H 1 job holds is not known"
                )
            )
            return
        identity = h == HELD and all(e == (i << 16, ONE) for i, e in enumerate(entries))
        for c in range(PLANES):
            unit = c != 0 or (taps is not None and _unit(taps[0]))
            taint = I if identity and unit else H
            for r in range(2):
                y = h - 2 + r
                o = (c * h + y) * STRIDE
                boot.lines.write(
                    (2 * c + r) * STRIDE, px.data[o : o + full].capped(taint)
                )


STOCK_PLANES: Planes = (STOCK, STOCK, STOCK)


def _substituted(boot: Boot, effect: Effect) -> Effect:
    """`effect` with the boot's tap-0 substitute resolved: the drop of tap 0's product where it
    is 0, unknown bytes where it is not known."""
    if not isinstance(effect, Substitute):
        return effect
    if boot.substitute == 0:
        return DROP0
    return Changed(
        "tap 0's product replaced by the boot's tap-0 substitute, which is not known on this "
        "boot: its boot value varies per boot"
    )


def _measured_edits(
    program: Program, facts: Sequence[Fact]
) -> dict[tuple[int, int], Fact]:
    """The measured edits by (index, edited word value); the facts of one edited word merge."""
    out: dict[tuple[int, int], Fact] = {}
    for fact in facts:
        for i in fact.words:
            stock = program.words[i]
            word = fact.kind.apply(stock)
            if word == stock:
                raise ValueError(f"word {i}: {fact.kind.name} leaves it unchanged")
            old = out.get((i, word.value))
            if old is None:
                out[i, word.value] = fact
                continue
            if any(old.planes.get(p, v) != v for p, v in fact.planes.items()):
                raise ValueError(f"word {i}: facts of one edited word disagree")
            facts = "; ".join(f for f in (old.fact, fact.fact) if f)
            out[i, word.value] = Fact(
                old.kind, (i,), {**old.planes, **fact.planes}, facts
            )
    return out


def _probe(px: _Pixels, entries: list[tuple[int, int] | None], w: int) -> Probe | None:
    """The job's class: None if the source is unknown, the height is not 16 or b varies."""
    data = px.data
    if px.h != HELD or None in entries or not all(data.known):
        return None
    steps = {e[1] for e in entries if e is not None}
    if len(steps) != 1:
        return None
    flat = all(
        len(set(data.data[o : o + STRIDE])) == 1 for o in range(0, len(data), STRIDE)
    )
    return Probe(flat, steps.pop(), w)


def _operands(
    effect: Operands, kernel: tuple[int, ...], line: Callable[[int], int]
) -> int:
    """The output byte of a kernel whose tap k multiplies effect.ops[k]; `line(j)` reads tap j's
    source line."""
    taps = [(k, t) for k, t in enumerate(kernel) if t]
    if any(isinstance(effect.ops[k], Operand) for k, _ in taps):
        if len(taps) == 1:
            ((k, t),) = taps
            if effect.ops[k] is Operand.LOW and 0 < t <= 256:
                return 0
        raise _Miss("operand")
    s = 0
    for k, t in taps:
        j = effect.ops[k]
        if j is not None:
            assert isinstance(j, int)
            s += t * line(j)
    return out_of(s)


def _phase(
    get: Callable[[int, int], int], base: int, kernel: tuple[int, ...], x: int, q: int
) -> int:
    """A chroma byte sampled along the line at phase q / 16 (see `Phase`)."""
    s = 0
    for k, t in enumerate(chroma(q)):
        if t:
            xx = x - 1 + k
            v = sum(tv * get(base - 1 + j, xx) for j, tv in enumerate(kernel) if tv)
            s += t * rhe(v, 4)
    return max(0, min(255, rhe(s, 1 << 14)))


@dataclass(frozen=True)
class Prediction:
    """A job's dst footprint as the model predicts it on a fresh boot."""

    dst: Bytes
    """Value, known flag and taint per byte."""
    sources: list[Source]
    """The boot's sources: `dst.src` indexes them."""
    result: JobResult

    def confidence(self) -> list[Confidence | None]:
        """Per byte its confidence, None where it is unknown."""
        return [
            Confidence(t) if k else None for t, k in zip(self.dst.taint, self.dst.known)
        ]

    def unknown(self) -> dict[str, bytes]:
        """Per reason (its source's name, e.g. "resampler right"; the source's fact says why) the
        mask of the unknown bytes it explains."""
        masks: dict[str, bytearray] = {}
        for i, (k, s) in enumerate(zip(self.dst.known, self.dst.src)):
            if not k:
                masks.setdefault(self.sources[s].name, bytearray(len(self.dst)))[i] = 1
        return {name: bytes(m) for name, m in masks.items()}


_SRC, _DST, _TABLE = 0x10000000, 0x10100000, 0x10200000
"""Where `predict` places its buffers; the law does not depend on them."""


def predict(
    model: Resample13Model,
    taps: Taps,
    src: bytes,
    table: list[tuple[int, int]],
    width: int = STRIDE,
    edits: Sequence[tuple[int, Edit]] = (),
    strict: bool = True,
) -> Prediction:
    """The dst of the stock program, with `edits` applied (see `model.job`), run with `taps` on
    image `src` (three planes of 64 x H at stride 64) and the line table (one entry (a, b) per
    output line, H = len(table)).

    Bytes the job does not write are unknown ("not written"), as are the bytes the law does not
    give; a partial column block is unknown since a fresh boot holds no line pairs."""
    h = len(table)
    if len(src) != PLANES * STRIDE * h:
        raise ValueError(
            f"{len(src)} src bytes for H {h}: expected {PLANES * STRIDE * h}"
        )
    boot = Boot()
    boot.put(_SRC, src, "src")
    boot.put(_TABLE, b"".join(struct.pack("<iI", a, b) for a, b in table), "table")
    n = footprint(h)
    unwritten = boot.source("not written", "the job does not write it")
    boot.dram.write(dram_alias(_DST), Bytes.unknown(n, unwritten))
    job = model.job(taps, _SRC, _DST, _TABLE, width, h, edits)
    result = Emulator(boot, model, strict).run(job)
    return Prediction(boot.read(_DST, n), boot.sources, result)
