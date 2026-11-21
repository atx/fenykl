"""The word-level emulator: a `Job` (a program and its request in local memory) run on a `Boot`.

The words run in order; each gets the semantics of its class (`semantics.classify`), which
record what it contributes to the job's `Trace`. At the end the model of the program
(`memcpy.Copy16Model`, `resample.Resample13Model`) applies the trace: it reads the command's
request from local memory, resolves the job and writes its result to the boot's state.

Whatever a model cannot tell becomes UNKNOWN, with a source that says why; a later job that
reads it gets UNKNOWN bytes, never guessed ones.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..firmware import unpack_firmware
from .fields import Confidence
from .program import Program
from .semantics import Sequencer, Trace, WordClass, classify
from .state import BootParams, Bytes, RegisterFile, Source, Space, SparseSpace

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

LOCAL_BASE = 0x9D000000
LOCAL_SIZE = 0x2000
"""Local memory reaches at least 0x9D001D1C (the resampler's setup writes there)."""
BUFFER_LO, BUFFER_HI = -0x1000, 0x10000
"""The modelled buffer indices: it holds at least 16 KiB and reaches below index 0."""
LINE_PAIRS = 3 * 2 * 64
"""The resampler's held line pairs: plane c, line parity r, pixel x at (2 c + r) x 64 + x."""


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
    """The confidence of what this job did: its status, whether it stored and the store itself;
    stored bytes can be weaker still through the buffer they come from."""
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Boot:
    """The state of one camera boot; it persists across the jobs run on it."""

    def __init__(self, params: BootParams | None = None) -> None:
        self.params = params or BootParams()
        self.sources: list[Source] = []
        self._source_ids: dict[Source, int] = {}
        self.regs = RegisterFile()
        never = self.source("unwritten", "no job on this boot wrote it")
        self.local = Space("local", LOCAL_BASE, LOCAL_SIZE, never)
        self.buffer = Space("buffer", BUFFER_LO, BUFFER_HI - BUFFER_LO, never)
        self.lines = Space("line pairs", 0, LINE_PAIRS, never)
        self.dram = SparseSpace("dram", self.source("dram", "not in the DRAM view"))

    def source(self, name: str, fact: str = "") -> int:
        """The id of source (name, fact). Equal sources share one id, so the list stays bounded
        by the distinct sources; give puts distinct names to tell them apart."""
        s = Source(name, fact)
        if s not in self._source_ids:
            self._source_ids[s] = len(self.sources)
            self.sources.append(s)
        return self._source_ids[s]

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


class Model(Protocol):
    """The model of one program: its word classes, and what a job's trace does."""

    program: Program
    classes: tuple[WordClass, ...]

    def apply(self, emu: "Emulator", job: Job, trace: Trace) -> JobResult: ...


class Emulator:
    """Runs jobs of `model`'s program on `boot`. strict=False ignores fields without semantics
    (and taints)."""

    def __init__(
        self,
        boot: Boot,
        model: Model,
        strict: bool = True,
        sequencer: Sequencer | None = None,
    ) -> None:
        self.boot, self.model, self.strict = boot, model, strict
        self.sequencer = sequencer or Sequencer()

    def run(self, job: Job) -> JobResult:
        boot = self.boot
        request = boot.source("request", "written by the command's CPU setup")
        for offset, data in job.local:
            boot.local.write(LOCAL_BASE + offset, Bytes.of(data, request))
        trace = Trace()
        classes = self.model.classes
        for index, word in enumerate(job.program.words):
            c = classify(classes, word, index, trace, self.strict)
            c.act(trace, word, index, self.strict)
        return self.model.apply(self, job, trace)


MAIN_CODE = 0x60021020
"""Where the main code runs: word 67 at r21 reads its bytes at 0x606CC000 (offset 0x6AAFE0)."""


def back_firmware(boot: Boot, image: bytes) -> int:
    """Back the DRAM view with the main code of the stock `.BIN` `image`; returns its source."""
    firmware = unpack_firmware(image)
    body = firmware.blocks[firmware.find_block("main-code-01")].body
    return boot.back(MAIN_CODE, body, "firmware main code")
