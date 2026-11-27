"""The emulator's state: tri-state bytes with provenance and taint, and the spaces they live in.

A byte is known (a value) or UNKNOWN. A known byte carries its provenance, a source (a job's src,
a prefill, a format's zero bits, ...) and the offset in it, and a taint: the weakest confidence
of the semantics that produced or moved it. An UNKNOWN byte carries the source that explains why
(never written on this boot, data from a memory that is not modelled, ...).

The spaces of one boot (`emu.Boot`), persistent across its jobs:
- `local`: DSP local memory at 0x9D000000, where the CPU setup of a command writes its request;
- `dram`: a sparse view of DRAM, unknown except where written or backed by an image.

The stock models keep their own spaces and the transfer registers (`stock.model.StockBoot`).
"""

from array import array
from collections.abc import Iterator
from dataclasses import dataclass

from .fields import Confidence

V = Confidence.VERIFIED

LOCAL_BASE = 0x9D000000


DRAM_BYTES = 0x40000000
"""The DRAM alias space: addresses that differ above it name the same bytes (`dram_alias`)."""


def dram_alias(addr: int) -> int:
    """0x6xxxxxxx (cached) and 0xAxxxxxxx (uncached) are the same DRAM."""
    return addr & DRAM_BYTES - 1


@dataclass(frozen=True)
class Source:
    """Where bytes come from, or why they are unknown."""

    name: str
    fact: str = ""


class Bytes:
    """A run of tri-state bytes: value, known flag, taint and provenance per byte."""

    __slots__ = ("data", "known", "off", "src", "taint")

    def __init__(self, n: int = 0) -> None:
        self.data = bytearray(n)
        self.known = bytearray(n)
        self.taint = bytearray(n)
        self.src = array("i", [-1]) * n
        self.off = array("i", [0]) * n

    @classmethod
    def of(
        cls, data: bytes, src: int, taint: Confidence = V, start: int = 0
    ) -> "Bytes":
        """Known bytes `data` from source `src` at offsets start, start + 1, ..."""
        b = cls(len(data))
        b.data[:] = data
        b.known[:] = b"\1" * len(data)
        b.taint[:] = bytes([taint]) * len(data)
        b.src = array("i", [src]) * len(data)
        b.off = array("i", range(start, start + len(data)))
        return b

    @classmethod
    def unknown(cls, n: int, src: int) -> "Bytes":
        b = cls(n)
        b.src = array("i", [src]) * n
        return b

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, s: slice) -> "Bytes":
        b = Bytes()
        b.data, b.known, b.taint = self.data[s], self.known[s], self.taint[s]
        b.src, b.off = self.src[s], self.off[s]
        return b

    def __add__(self, other: "Bytes") -> "Bytes":
        b = Bytes()
        b.data, b.known = self.data + other.data, self.known + other.known
        b.taint, b.src, b.off = (
            self.taint + other.taint,
            self.src + other.src,
            self.off + other.off,
        )
        return b

    def capped(self, taint: Confidence) -> "Bytes":
        """These bytes with their taint lowered to at most `taint`."""
        b = self[:]
        if taint < V:
            b.taint = bytearray(min(t, taint) for t in b.taint)
        return b

    def ranges(self, known: bool) -> list[tuple[int, int]]:
        """[start, end) runs of known (or of unknown) bytes."""
        out = []
        flag = 1 if known else 0
        i, n = 0, len(self)
        while i < n:
            if self.known[i] != flag:
                i += 1
                continue
            j = i
            while j < n and self.known[j] == flag:
                j += 1
            out.append((i, j))
            i = j
        return out

    def weakest(self) -> Confidence | None:
        """The weakest taint of the known bytes, None if none is known."""
        known = [t for t, k in zip(self.taint, self.known) if k]
        return Confidence(min(known)) if known else None


class Space:
    """Tri-state bytes at addresses [base, base + size); everything starts UNKNOWN."""

    def __init__(self, name: str, base: int, size: int, unwritten: int) -> None:
        self.name, self.base, self.size = name, base, size
        self.bytes = Bytes.unknown(size, unwritten)

    def _index(self, addr: int, n: int) -> int:
        i = addr - self.base
        if not 0 <= i <= self.size - n:
            raise IndexError(
                f"{self.name}: [{addr:#x}, {addr + n:#x}) outside the modelled "
                f"[{self.base:#x}, {self.base + self.size:#x})"
            )
        return i

    def contains(self, addr: int, n: int) -> bool:
        return self.base <= addr and addr + n <= self.base + self.size

    def read(self, addr: int, n: int) -> Bytes:
        i = self._index(addr, n)
        return self.bytes[i : i + n]

    def write(self, addr: int, b: Bytes) -> None:
        i = self._index(addr, len(b))
        n = len(b)
        own = self.bytes
        own.data[i : i + n], own.known[i : i + n] = b.data, b.known
        own.taint[i : i + n], own.src[i : i + n], own.off[i : i + n] = (
            b.taint,
            b.src,
            b.off,
        )

    def forget(self, src: int) -> None:
        """Every byte UNKNOWN, explained by `src`."""
        self.bytes = Bytes.unknown(self.size, src)


PAGE = 0x1000


class SparseSpace:
    """A 32-bit address space in pages made on first touch, filled from backing images."""

    def __init__(self, name: str, unwritten: int) -> None:
        self.name, self.unwritten = name, unwritten
        self.pages: dict[int, Space] = {}
        self.backing: list[tuple[int, bytes, int]] = []

    def back(self, base: int, data: bytes, src: int) -> None:
        """Known bytes `data` at `base` (e.g. firmware text), below anything written."""
        self.backing.append((base, data, src))
        for page in self.pages.values():
            self._fill(page, base, data, src)

    def _fill(self, page: Space, base: int, data: bytes, src: int) -> None:
        """The backing image's bytes in `page`, in the runs where nothing was written."""
        lo, hi = max(base, page.base), min(base + len(data), page.base + PAGE)
        own = page.bytes.src
        a = lo
        while a < hi:
            if own[a - page.base] != self.unwritten:
                a += 1
                continue
            b = a
            while b < hi and own[b - page.base] == self.unwritten:
                b += 1
            page.write(a, Bytes.of(data[a - base : b - base], src, V, a - base))
            a = b

    def _page(self, number: int) -> Space:
        page = self.pages.get(number)
        if page is not None:
            return page
        page = Space(f"{self.name}:{number:#x}", number * PAGE, PAGE, self.unwritten)
        for base, data, src in self.backing:
            self._fill(page, base, data, src)
        self.pages[number] = page
        return page

    def _chunks(self, addr: int, n: int) -> Iterator[tuple[int, int, int]]:
        """(page number, address, length) pieces of [addr, addr + n)."""
        end = addr + n
        while addr < end:
            number = addr // PAGE
            stop = min(end, (number + 1) * PAGE)
            yield number, addr, stop - addr
            addr = stop

    def read(self, addr: int, n: int) -> Bytes:
        out = Bytes()
        for number, a, m in self._chunks(addr, n):
            part = self._page(number).read(a, m)
            for name in Bytes.__slots__:
                getattr(out, name).extend(getattr(part, name))
        return out

    def write(self, addr: int, b: Bytes) -> None:
        pos = 0
        for number, a, m in self._chunks(addr, len(b)):
            self._page(number).write(a, b[pos : pos + m])
            pos += m
