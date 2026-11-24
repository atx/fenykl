"""The copy engine's stream as the datapath executor drives it: what X1's transfer heads hand the
engine, and the load and the store that the stock copy's transfers program.

X1's heads 0x1c..0x1e hand B[f115_119] (the register read before the word's ports write) to the
engine; they put no result on any bus and change no register (verified on the camera). The stock
copy (program 16) hands one fixed transcript per request (34 values when its loop is skipped, as
in every job run): headers, two eight-value stream groups, two (key, value) pair groups, the load
group (command, DRAM source, lane memory placement), a 0x1d after which the load is complete, the
store group (command, lane memory source, DRAM destination) and a 0x1d that stores. A job that
keeps those transfers and runs own words between them loads, computes and stores in one job
(verified on the camera: kernels between the load's 0x1d and the store group came out exact,
DRAM to DRAM).

`CopyStream` follows a job's transcript against the stock copy's transcript for the same request
(`Transcript`, recorded by running the stock program): where they agree, or differ only in a
value whose variation was measured (the store format, the load placement, the store source), it
loads and stores; at the first other difference it goes off script, and what the engine does to
the lane memory and to DRAM from there on is not modelled (the lane memory becomes unknown, no
store is applied).

Load: source byte i goes to element i of the lane memory from the placement's vector on, zero-
extended to 16 bits, vectors counted modulo `fields.LANE_VECTORS` (verified on the camera at
placements of whole vectors; the placement is in bytes of 16-bit elements, 64 per vector).
Store: `STORE_FORMATS`, from the source's vector on, len bytes.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from .emu import Boot, dram_alias
from .fields import LANE_VECTORS, Confidence
from .machine import UNKNOWN_VEC, Machine, Sv, Vec
from .semantics import LANE_COUNT, UnknownSemantics
from .state import Bytes

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

LOAD_FORMAT, STORE_FORMAT, LOAD_LINES, STORE_LINES = 6, 15, 21, 28
LOAD_COMMAND, SOURCE, PLACEMENT, LOADED = 22, 23, 24, 29
STORE_SOURCE, DESTINATION, STORED = 31, 32, 33
"""Positions in the stock copy's transcript: the load's element format (0x10, bytes), the store's
format, the load's and the store's length in 16-byte lines (len / 16), the load's command, its
DRAM source and
lane memory placement, the 0x1d after which the load is complete, the store's lane memory source
and DRAM destination, the 0x1d that stores."""
MEASURED: dict[int, frozenset[int] | None] = {
    STORE_FORMAT: None,
    PLACEMENT: None,
    STORE_SOURCE: None,
    LOAD_COMMAND: frozenset({0, 1}),
}
"""Positions whose value was varied on the camera, with the values run (None: any). The load
command 0 loads, 1 does not (verified on the camera: the store wrote the lane memory an earlier
job had filled)."""
VECTOR_BYTES = 64
"""The placement and the store source count bytes of 16-bit elements: 64 per vector."""

STORE_FORMATS: dict[int, tuple[int, Confidence]] = {
    0x10: (8, V),
    0x0C: (10, V),
    0x0A: (12, V),
    0x09: (14, V),
    0x08: (16, V),
    0x04: (32, V),
    0x44: (32, I),  # measured on byte elements: a wider element is not predicted
    0x0D: (0, V),
}
"""The store format (word 47's value): the number of elements per 16-byte line, each element's low
bits packed least significant first and the line's last bits 0, by value: (bits per element, tier)
(verified on the camera, pre-registered: 0x10 the low byte, 0x08 16 bits, 0x04 zero-extended to 32
bits, 0x0c / 0x0a / 0x09 the 10-, 12- and 14-bit packs). 0x44 gave the 0x04 layout on byte
elements only (inferred); 0x0d wrote zeros (bits 0). Other values were not run or never ended."""


@dataclass
class Transcript:
    """The (head, value) pairs a job's transfers hand the engine, in order; None where a value is
    not known."""

    values: list[tuple[int, int | None]] = field(default_factory=list)

    def transfer(self, m: Machine, index: int, head: int, value: Sv) -> None:
        self.values.append((head, value.value))

    def lane_tier(self) -> Confidence:
        return V


@dataclass
class CopyStream:
    """The engine under a job whose transcript the stock copy's `reference` gives."""

    reference: list[tuple[int, int | None]]
    values: list[Sv] = field(default_factory=list)
    off: str = ""
    """Why the job went off script; empty while it follows the stock transcript."""
    stored: tuple[int, int] | None = None
    """(DRAM address, length) the store wrote."""

    def transfer(self, m: Machine, index: int, head: int, value: Sv) -> None:
        k = len(self.values)
        self.values.append(value)
        if self.off:
            return
        ref = self.reference[k] if k < len(self.reference) else None
        if ref is None or ref[0] != head:
            self._go_off(m, f"transfer {k} (word {index}) is not the stock copy's")
        elif value.value is None or not self._measured(k, value.value, ref[1]):
            self._go_off(m, f"transfer {k} (word {index}) hands another value")
        elif k == LOADED:
            self._load(m, index)
        elif k == STORED:
            self._store(m, index)

    @staticmethod
    def _measured(k: int, value: int, stock: int | None) -> bool:
        if k not in MEASURED:
            return value == stock
        return MEASURED[k] is None or value in MEASURED[k]

    def _go_off(self, m: Machine, why: str) -> None:
        """What the engine does from here on is not known: the lane memory, the stock store's
        DST and what this job stored become unknown."""
        self.off = why
        m.lose_lanes()
        lost = m.boot.source("stream off script", why)
        dst, lines = self.reference[DESTINATION][1], self.reference[STORE_LINES][1]
        for addr, n in {(dst, 16 * lines), self.stored or (dst, 0)}:
            m.boot.dram.write(dram_alias(addr), Bytes.unknown(n, lost))

    def _tier(self) -> Confidence:
        """The weakest value handed so far: every one of them programs the engine."""
        return min(v.tier for v in self.values)

    def lane_tier(self) -> Confidence:
        """Own words between the load command and the load's 0x1d, or within the store group,
        meet the engine at work (not measured: hypothesis); after the load's 0x1d and before the
        store group they read the loaded vectors (verified)."""
        k = len(self.values)
        return H if LOAD_COMMAND < k <= LOADED or LOADED + 1 < k <= STORED else V

    def _vector(self, index: int, k: int) -> int:
        v = self.values[k].value
        if v % VECTOR_BYTES:
            raise UnknownSemantics(
                index, "stream", [], f"a lane memory offset {v:#x} within a vector"
            )
        return v // VECTOR_BYTES

    def _length(self, k: int) -> int:
        return 16 * self.values[k].value

    def _load(self, m: Machine, index: int) -> None:
        if self.values[LOAD_COMMAND].value == 1:
            return
        if self.values[LOAD_FORMAT].value != 0x10:
            raise UnknownSemantics(index, "stream", [], "a load format but bytes")
        n = self._length(LOAD_LINES)
        data = m.boot.read(self.values[SOURCE].value, n)
        tier = self._tier()
        base = self._vector(index, PLACEMENT)
        for v in range(-(-n // LANE_COUNT)):
            # The vector address wraps modulo the lane memory's size (inferred).
            wrap = I if base + v >= LANE_VECTORS else V
            chunk = data[v * LANE_COUNT : (v + 1) * LANE_COUNT]
            lanes = [x if k else None for x, k in zip(chunk.data, chunk.known)]
            lanes += [None] * (LANE_COUNT - len(lanes))
            taints = [Confidence(t) for t, k in zip(chunk.taint, chunk.known) if k]
            m.memory[(base + v) % LANE_VECTORS] = Vec(
                tuple(lanes), min([tier, wrap, *taints])
            )

    def _store(self, m: Machine, index: int) -> None:
        fmt = self.values[STORE_FORMAT].value
        if fmt not in STORE_FORMATS:
            raise UnknownSemantics(
                index, "stream", [], f"store format {fmt:#x}: not measured"
            )
        bits, tier = STORE_FORMATS[fmt]
        n = self._length(STORE_LINES)
        tier = min(tier, self._tier())
        base = self._vector(index, STORE_SOURCE)

        def element(i: int) -> tuple[int | None, Confidence]:
            at = base + i // LANE_COUNT
            v = m.memory.get(at % LANE_VECTORS, UNKNOWN_VEC)
            x = v.lanes[i % LANE_COUNT]
            if fmt == 0x44 and x is not None and x > 0xFF:
                x = None
            return x, min(v.tier, I if at >= LANE_VECTORS else V)

        out = pack(m.boot, bits, n, element, tier)
        dst = self.values[DESTINATION].value
        m.boot.dram.write(dram_alias(dst), out)
        self.stored = (dst, n)


def pack(
    boot: Boot,
    bits: int,
    n: int,
    element: Callable[[int], tuple[int | None, Confidence]],
    tier: Confidence,
) -> Bytes:
    """n store bytes of the elements `element`(i) = (value or None, tier) at `bits` each: 128 //
    bits per 16-byte line, least significant first, the rest of the line 0; bits 0 writes
    zeros. A byte is known when every element it draws on is known."""
    out = Bytes.unknown(n, boot.source("store", "the copy engine's store"))
    per = 128 // bits if bits else 0
    mask = (1 << min(bits, 16)) - 1
    for line in range(n // 16):
        elements = [element(line * per + k) for k in range(per)]
        acc = 0
        for k, (value, _) in enumerate(elements):
            if value is not None:
                acc |= (value & mask) << (k * bits)
        data = acc.to_bytes(16, "little")
        for b in range(16):
            ins = elements[8 * b // bits : (8 * b + 7) // bits + 1] if bits else []
            i = 16 * line + b
            out.data[i] = data[b]
            if all(value is not None for value, _ in ins):
                out.known[i] = 1
                out.taint[i] = min([tier, *(t for _, t in ins)])
    return out
