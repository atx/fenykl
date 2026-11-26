"""The emulator's state: tri-state bytes with provenance and taint, its spaces and the register file.

A byte is known (a value) or UNKNOWN. A known byte carries its provenance, a source (a job's src,
a prefill, a format's zero bits, ...) and the offset in it, and a taint: the weakest confidence
of the semantics that produced or moved it. An UNKNOWN byte carries the source that explains why
(never written on this boot, data from a memory that is not modelled, ...).

The spaces of one boot, all persistent across its jobs:
- `local`: DSP local memory at 0x9D000000, where the CPU setup of a command writes its request;
- `buffer`: the internal buffer between the load and the store, position-addressed;
- `lines`: the resampler's held line pair per plane and column (its partial column blocks);
- `substitute`: the resampler's tap-0 substitute, a per-lane value, kept as 0 or unknown;
- `dram`: a sparse view of DRAM, unknown except where written or backed by an image;
- the op slot's registers (ra, rb), kept apart from the transfer registers: nothing shows the
  two to be one file;
- the transfer registers (xreg); when a job starts, `REGISTER_FACTS` set the registers they
  cover from the job's request and the boot's parameters (unknown where a fact has no value for
  the job).
"""

from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from .fields import Confidence

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

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

    def segments(self) -> Iterator[tuple[int, int, int, int]]:
        """(start, end, source, offset of start) runs that continue one source contiguously."""
        i, n = 0, len(self)
        while i < n:
            j = i + 1
            while (
                j < n
                and self.src[j] == self.src[i]
                and self.known[j] == self.known[i]
                and (not self.known[i] or self.off[j] == self.off[j - 1] + 1)
            ):
                j += 1
            yield i, j, self.src[i], self.off[i]
            i = j

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


@dataclass(frozen=True)
class Reg:
    """A register's value as a closed interval [lo, hi], exact when lo == hi."""

    lo: int
    hi: int
    taint: Confidence
    fact: str

    @property
    def exact(self) -> int | None:
        return self.lo if self.lo == self.hi else None

    def image(self, f: Callable[[int], int]) -> int | None:
        """f of the value when f is the same at both ends (f monotonic), else None."""
        a, b = f(self.lo), f(self.hi)
        return a if a == b else None


@dataclass(frozen=True)
class JobView:
    """What the register facts may read: the request in local memory and the boot parameters."""

    src: int | None
    dst: int | None
    length: int | None
    boot: "BootParams"


@dataclass(frozen=True)
class RegisterFact:
    reg: int
    taint: Confidence
    fact: str
    value: Callable[[JobView], tuple[int, int] | None]


def _exact(
    f: Callable[[JobView], int | None],
) -> Callable[[JobView], tuple[int, int] | None]:
    def value(job: JobView) -> tuple[int, int] | None:
        v = f(job)
        return None if v is None else (v, v)

    return value


def _const(v: int) -> Callable[[JobView], tuple[int, int] | None]:
    return lambda job: (v, v)


def _half(job: JobView) -> int | None:
    return None if job.length is None else job.length // 2


# Values seen through program 16's register-transfer words on the camera (copy jobs of
# 0.5..4 KiB): word 73 counts a register's value in bytes, word 68 offsets the load by the
# value / 32 (rounded to 32 bytes) and word 67 reads at it as an address. The program's data ops
# write some of them through PR port 267 (word 64 writes r7, inferred); the model sets them when
# the job starts with the values its transfers read.
REGISTER_FACTS: tuple[RegisterFact, ...] = (
    RegisterFact(
        0, I, "0x10: word 73 counts 0x10 bytes, word 68 offsets by 0", _const(0x10)
    ),
    RegisterFact(
        1,
        V,
        "the job length: word 73 counts len bytes, word 68 offsets by len / 32",
        _exact(lambda job: job.length),
    ),
    RegisterFact(
        3,
        V,
        "the job's src: the only register that makes word 67 load the job's src",
        _exact(lambda job: job.src),
    ),
    *(
        RegisterFact(
            r,
            I,
            "0: word 73 counts nothing (dst not written), word 68 offsets by 0",
            _const(0),
        )
        for r in (4, 5, 10, 12, 13)
    ),
    RegisterFact(
        6,
        H,
        "0x400..0x41F: word 68 offsets by a fixed +0x20 at 0.5..4 KiB; word 73 at r6 times out, "
        "so the low bits are not all zero or the register is no plain count",
        lambda job: (0x400, 0x41F),
    ),
    RegisterFact(
        7, V, "half the job length: word 73 counts len / 2 bytes", _exact(_half)
    ),
    RegisterFact(
        9, I, "0x200: word 73 counts 0x200 bytes, word 68 offsets by 0", _const(0x200)
    ),
    RegisterFact(
        11,
        H,
        "as r6 in words 68 and 76 at 1 KiB (+0x20), seen at no other length",
        lambda job: (0x400, 0x41F) if job.length == 0x400 else None,
    ),
    RegisterFact(
        14,
        H,
        "negative, a boot parameter: word 68 offsets by -0xE0 on every boot, word 76 by -0xE0 or "
        "-0x100 depending on the boot",
        lambda job: job.boot.r14,
    ),
    RegisterFact(
        21,
        V,
        "0x606CC000 on every boot read: word 67 loads the firmware text there",
        _const(0x606CC000),
    ),
    RegisterFact(
        29,
        V,
        "0x60E58B00 or 0x60E58B40, a boot parameter: word 67 loads the firmware text there",
        lambda job: None if job.boot.r29 is None else (job.boot.r29, job.boot.r29),
    ),
)


@dataclass(frozen=True)
class BootParams:
    """State that differs between boots and that no job is known to set; None = not known.

    - r14: the interval of r14. Word 68 truncates r14 / 32 towards zero and word 76 rounds it
      down (hypothesis), so -0x1C00 gives both -0xE0, while (-0x2000, -0x1C00) gives 68 -0xE0
      and 76 -0x100; both were seen, on different boots.
    - r29: word 67's load address at r29.
    - r21_format: word 47's store format at r21, "pack10" or "zero"; it differs between boots
      with nothing else seen to differ.
    """

    r14: tuple[int, int] | None = (-0x1FFF, -0x1C00)
    r29: int | None = None
    r21_format: str | None = None


R14_EXACT = (-0x1C00, -0x1C00)
"""r14 on the boots where word 76 at r14 offsets by -0xE0."""
R14_BELOW = (-0x1FFF, -0x1C01)
"""r14 on the boots where word 76 at r14 offsets by -0x100."""


@dataclass
class RegisterFile:
    """The registers of one boot; they persist across its jobs."""

    regs: list[Reg | None] = field(default_factory=lambda: [None] * 32)

    def start_job(self, job: JobView) -> None:
        """Set the registers the facts give when a job starts; a fact without a value for this
        job leaves its register unknown."""
        for f in REGISTER_FACTS:
            v = f.value(job)
            self.regs[f.reg] = None if v is None else Reg(v[0], v[1], f.taint, f.fact)

    def __getitem__(self, r: int) -> Reg | None:
        return self.regs[r]

    def __setitem__(self, r: int, value: Reg | None) -> None:
        self.regs[r] = value
