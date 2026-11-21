"""The word-level emulator: a `Job` (a program and its request in local memory) run on a `Boot`.

The words run in order; each gets the semantics of its class (`semantics.classify`), which
record what it contributes: an undecoded stock word its part of the job, a register transfer
its register for the sequencer. At the end the sequencer gives the transfers their roles, and
the job resolves into an outcome and the one load and one store that program 16 makes:

    load:   buffer[i + o68] = S[i] for i in [0, len), S read at word 67's address
    store:  dst[i] = format(buffer)[i + o76] for i in [0, n73); dst[n73, len) not written

A job that does not complete stores nothing. Whatever the model cannot tell (a placement out of
range, a register of unknown value, a load that may or may not have run) becomes UNKNOWN, with
a source that says why; a later job that reads it gets UNKNOWN bytes, never guessed ones.
"""

from dataclasses import dataclass, field
from enum import Enum

from ..firmware import unpack_firmware
from .fields import Confidence, lookup
from .program import Program
from .semantics import (
    FORMATS,
    PART_OF,
    Outcome,
    Role,
    Sequencer,
    Trace,
    UnknownSemantics,
    classify,
)
from .state import (
    BootParams,
    Bytes,
    JobView,
    RegisterFile,
    Source,
    Space,
    SparseSpace,
)
from . import memcpy

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

LOCAL_BASE = 0x9D000000
LOCAL_SIZE = 0x2000
"""Local memory reaches at least 0x9D001D1C (the resampler's setup writes there)."""
BUFFER_LO, BUFFER_HI = -0x1000, 0x10000
"""The modelled buffer indices: it holds at least 16 KiB and reaches below index 0."""
MAX_COUNT = 0x40000
"""A count above this is an address or junk: the store never ends (hypothesis)."""
ZERO_FILL = 0x4000
LOAD_ROLES = ("load enable", "load address", "load offset")


def dram_alias(addr: int) -> int:
    """0x6xxxxxxx (cached) and 0xAxxxxxxx (uncached) are the same DRAM."""
    return addr & 0x3FFFFFFF


class Status(Enum):
    OK = "ok"
    TIMEOUT = "timeout-abort (0xA111)"
    HAZARD = "wedged or link lost"


@dataclass(frozen=True)
class Job:
    """A program and its request: (offset, bytes) writes to DSP local memory."""

    program: Program
    local: tuple[tuple[int, bytes], ...]


@dataclass
class JobResult:
    status: Status
    why: list[str] = field(default_factory=list)
    """The outcomes that decided the status and the store."""
    stored: tuple[int, int] | None = None
    """The dst range the store wrote, None when it wrote nothing."""
    taint: Confidence = V
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Boot:
    """The state of one camera boot; it persists across the jobs run on it."""

    def __init__(self, params: BootParams | None = None) -> None:
        self.params = params or BootParams()
        self.sources: list[Source] = []
        never = self.source("unwritten", "no job on this boot wrote it")
        self.local = Space("local", LOCAL_BASE, LOCAL_SIZE, never)
        self.buffer = Space("buffer", BUFFER_LO, BUFFER_HI - BUFFER_LO, never)
        self.dram = SparseSpace("dram", self.source("dram", "not in the DRAM view"))

    def source(self, name: str, fact: str = "") -> int:
        self.sources.append(Source(name, fact))
        return len(self.sources) - 1

    def put(self, addr: int, data: bytes, name: str, taint: Confidence = V) -> int:
        """Known DRAM bytes (a src, a prefill); returns their source."""
        src = self.source(name)
        self.dram.write(dram_alias(addr), Bytes.of(data, src, taint))
        return src

    def back(self, addr: int, data: bytes, name: str) -> int:
        """A read-only DRAM image under `addr` (e.g. firmware text), read only where touched."""
        src = self.source(name)
        self.dram.back(dram_alias(addr), data, src)
        return src

    def read(self, addr: int, n: int) -> Bytes:
        return self.dram.read(dram_alias(addr), n)

    def u32(self, offset: int) -> int | None:
        b = self.local.read(LOCAL_BASE + offset, 4)
        return int.from_bytes(b.data, "little") if all(b.known) else None


@dataclass
class _Plan:
    """The resolved job: outcomes, and the parameters of the load and the store."""

    outcomes: list[tuple[Outcome, str]] = field(default_factory=list)
    load_taint: Confidence = V
    store_taint: Confidence = V
    address: int | None = None
    address_why: str = ""
    """Set with address None: the load reads data the model does not know."""
    o68: int | None = 0
    o76: int | None = 0
    count: int | None = None
    format: str | None = "bytes"

    def has(self, o: Outcome) -> bool:
        return any(x is o for x, _ in self.outcomes)


def _trunc32(v: int) -> int:
    """v / 32 truncated towards zero, to a multiple of 32."""
    q = abs(v) // 32 & ~0x1F
    return q if v >= 0 else -q


def _floor32(v: int) -> int:
    """v / 32 rounded down, to a multiple of 32."""
    return v // 32 & ~0x1F


class Emulator:
    """Runs jobs on `boot`. strict=False ignores fields without semantics (and taints)."""

    def __init__(
        self, boot: Boot, strict: bool = True, sequencer: Sequencer | None = None
    ) -> None:
        self.boot, self.strict = boot, strict
        self.sequencer = sequencer or Sequencer()

    def run(self, job: Job) -> JobResult:
        boot = self.boot
        request = boot.source("request", "written by the command's CPU setup")
        for offset, data in job.local:
            boot.local.write(LOCAL_BASE + offset, Bytes.of(data, request))
        view = JobView(boot.u32(0), boot.u32(4), boot.u32(8), boot.params)
        if view.dst is None or view.length is None:
            raise ValueError("local memory holds no dst and len (no command setup ran)")
        regs = RegisterFile.at_job(view)
        trace = Trace()
        for index, word in enumerate(job.program.words):
            classify(word, index, trace, self.strict).act(trace, word, index)
        plan = self._resolve(trace, regs, view)
        return self._apply(plan, trace, view)

    # -- resolution

    def _resolve(self, trace: Trace, regs: RegisterFile, view: JobView) -> _Plan:
        # The stock transfers at their own slots leave these defaults: they are what every
        # stock copy shows (load at src, offsets 0, count len, bytes).
        plan = _Plan(count=view.length)
        present = set(trace.parts)
        for name, part in PART_OF.items():
            if name not in present:
                plan.outcomes.append((part.missing, f"{name} missing: {part.fact}"))
            elif part.operand is not None:
                reg = trace.operands[name]
                o = part.operand.get(reg)
                if o is None:
                    raise UnknownSemantics(int(name[4:]), name, [(lookup("xreg"), reg)])
                if o is not Outcome.OK:
                    plan.outcomes.append((o, f"{name} at r{reg}: {o.value}"))
        filled, unset = self.sequencer.assign(trace.channels)
        for slot in unset:
            plan.outcomes.append(
                (Outcome.TIMEOUT, f"unset {slot}: the stream never completes")
            )
            plan.store_taint = H
        for role, t, taint in filled:
            stock = memcpy.WORDS[role.word]["xreg"] == t.reg and taint is V
            taint = min(taint, t.taint)
            if not stock:
                taint = min(taint, self._role(plan, role, t.reg, t.index, regs, view))
            if role.name in LOAD_ROLES:
                plan.load_taint = min(plan.load_taint, taint)
            else:
                plan.store_taint = min(plan.store_taint, taint)
        plan.store_taint = min(plan.store_taint, trace.taint)
        return plan

    def _role(
        self,
        plan: _Plan,
        role: Role,
        reg: int,
        index: int,
        regs: RegisterFile,
        view: JobView,
    ) -> Confidence:
        """Apply `role` at register `reg`; returns the confidence of what it did."""
        o = role.table.get(reg)
        if o is not None and o is not Outcome.OK:
            plan.outcomes.append(
                (o, f"word {index} ({role.name}) at r{reg}: {o.value}")
            )
            return V
        if not role.value:
            if o is None:
                raise UnknownSemantics(
                    index, role.name, [(lookup("xreg"), reg)], role.fact
                )
            return V
        value = regs[reg]
        taint = V if value is None else value.taint
        match role.value:
            case "address":
                plan.address = None
                if value is None:
                    plan.address_why = (
                        f"word {index} at r{reg}: the register's value is not known"
                    )
                elif value.lo >= 0x20000000 and value.exact is not None:
                    plan.address = value.exact
                elif value.hi < 0x10000:
                    plan.address_why = f"word {index} at r{reg}: a fixed memory at value / 16, not modelled"
                else:
                    plan.address_why = f"word {index} at r{reg}: not an exact address"
            case "offset68":
                plan.o68 = None if value is None else value.image(_trunc32)
            case "offset76":
                plan.o76 = None if value is None else value.image(_floor32)
                if view.length != 0x400:
                    taint = H  # off-stock values of word 76 ran only at 1 KiB
            case "count":
                n = None if value is None else value.exact
                if n is not None and n > MAX_COUNT:
                    plan.outcomes.append(
                        (Outcome.TIMEOUT, f"word {index} at r{reg}: a count of {n:#x}")
                    )
                    return H
                plan.count = n
            case "format":
                fmt = self.boot.params.r21_format if reg == 21 else FORMATS.get(reg)
                if fmt is None and reg != 21:
                    raise UnknownSemantics(
                        index, role.name, [(lookup("xreg"), reg)], role.fact
                    )
                plan.format = fmt
                taint = V if reg != 21 else H
        return taint

    # -- application

    def _apply(self, plan: _Plan, trace: Trace, view: JobView) -> JobResult:
        boot = self.boot
        if plan.has(Outcome.HAZARD):
            status = Status.HAZARD
        elif plan.has(Outcome.TIMEOUT):
            status = Status.TIMEOUT
        else:
            status = Status.OK
        result = JobResult(
            status,
            [why for o, why in plan.outcomes],
            ignored=trace.ignored,
            notes=trace.notes,
        )
        length = view.length
        assert length is not None and view.dst is not None
        loads = not plan.has(Outcome.NO_LOAD)
        stores = status is Status.OK and not plan.has(Outcome.NO_STORE)
        unknown_data = plan.has(Outcome.DATA_UNKNOWN)

        if loads:
            self._load(plan, view, certain=stores and not unknown_data)
        if not stores:
            return result
        dst = view.dst
        if plan.has(Outcome.ZERO_FILL):
            zero = boot.source("zero fill", "word 15 missing: the store writes zeros")
            boot.dram.write(dram_alias(dst), Bytes.of(bytes(ZERO_FILL), zero, V))
            result.stored = (dst, dst + ZERO_FILL)
            return result
        n = plan.count
        if unknown_data or n is None or plan.o76 is None or (plan.format is None):
            why = "; ".join(
                w for o, w in plan.outcomes if o is Outcome.DATA_UNKNOWN
            ) or ("the store's count, offset or format is not known")
            n = length if n is None else n
            boot.dram.write(
                dram_alias(dst),
                Bytes.unknown(n, boot.source("store not modelled", why)),
            )
            result.stored = (dst, dst + n)
            return result
        data = self._format(plan.format, plan.o76, n)
        result.taint = plan.store_taint
        boot.dram.write(dram_alias(dst), data.capped(plan.store_taint))
        result.stored = (dst, dst + n)
        return result

    def _load(self, plan: _Plan, view: JobView, certain: bool) -> None:
        """The load into the buffer; UNKNOWN where it may or may not have run, or with unknown data."""
        boot = self.boot
        length = view.length
        assert length is not None
        o = plan.o68
        if o is None or not boot.buffer.contains(o, length):
            boot.buffer.forget(
                boot.source("load placement", "a load to an unknown buffer index")
            )
            return
        if not certain:
            why = "the job did not store: whether its load ran is not known"
            if plan.has(Outcome.DATA_UNKNOWN):
                why = "the load read data the model does not know"
            boot.buffer.write(
                o, Bytes.unknown(length, boot.source("load not modelled", why))
            )
            return
        if plan.address is None and plan.address_why:
            data = Bytes.unknown(
                length, boot.source("load not modelled", plan.address_why)
            )
        else:
            address = view.src if plan.address is None else plan.address
            if address is None:
                data = Bytes.unknown(
                    length, boot.source("load not modelled", "no src in local memory")
                )
            else:
                data = boot.read(address, length)
        boot.buffer.write(o, data.capped(plan.load_taint))

    def _format(self, fmt: str, o76: int, n: int) -> Bytes:
        """n store bytes from the buffer at index o76, in format `fmt`."""
        boot = self.boot
        if fmt == "bytes":
            return self._buffer(o76, n)
        if fmt == "zero":
            return Bytes.of(
                bytes(n), boot.source("format zero", "word 47's zero format"), V
            )
        zero = boot.source("format zero bits", "zero bits of a widening format")
        if fmt == "u32":
            src = self._buffer(o76, n // 4)
            out = Bytes.of(bytes(4 * len(src)), zero, V)
            for name in Bytes.__slots__:
                getattr(out, name)[0::4] = getattr(src, name)
            return out
        if fmt == "pack10":
            return self._pack10(o76, n, zero)
        raise ValueError(f"no store format {fmt!r}")

    def _buffer(self, o: int, n: int) -> Bytes:
        if not self.boot.buffer.contains(o, n):
            return Bytes.unknown(
                n, self.boot.source("store not modelled", "outside the modelled buffer")
            )
        return self.boot.buffer.read(o, n)

    def _pack10(self, o76: int, n: int, zero: int) -> Bytes:
        """16 output bytes per 12 buffer bytes: each zero-extended to 10 bits, LSB first, top
        8 bits 0. An output byte is known when every input byte it draws on is known."""
        lines = n // 16
        src = self._buffer(o76, lines * 12)
        derived = self.boot.source(
            "format pack10", "word 47's 10-bit pack of buffer bytes"
        )
        out = Bytes.unknown(lines * 16, derived)
        for j in range(lines):
            chunk = src[12 * j : 12 * j + 12]
            acc = sum(b << (10 * k) for k, b in enumerate(chunk.data))
            line = acc.to_bytes(16, "little")
            for k in range(16):
                lo, hi = (8 * k) // 10, min(11, (8 * k + 7) // 10)
                ins = range(lo, hi + 1) if lo < 12 else range(0)
                i = 16 * j + k
                out.data[i] = line[k]
                if all(chunk.known[x] for x in ins):
                    out.known[i] = 1
                    out.taint[i] = min((chunk.taint[x] for x in ins), default=V)
                    out.src[i] = chunk.src[lo] if ins else zero
                    out.off[i] = chunk.off[lo] if ins else 0
        return out


MAIN_CODE = 0x60021020
"""Where the main code runs: word 67 at r21 reads its bytes at 0x606CC000 (offset 0x6AAFE0)."""


def back_firmware(boot: Boot, image: bytes) -> int:
    """Back the DRAM view with the main code of the stock `.BIN` `image`; returns its source."""
    firmware = unpack_firmware(image)
    body = firmware.blocks[firmware.find_block("main-code-01")].body
    return boot.back(MAIN_CODE, body, "firmware main code")
