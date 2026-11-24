"""The stream engine as the datapath executor drives it: the values X1's head 0x1c hands it, the
channel registers they program, and the loads and stores between DRAM and the DSP's memories.

The engine sees the values handed and nothing else of the program: which words hand them does not
matter (verified on the camera). X1 0x1c hands B[f115_119] (read before the word's ports write);
X1 0x1d hands nothing and waits for one command (below).

Grammar (verified): the values form groups, each starting with a key v.

- v & 0x21 == 0x21: one register, channel v >> 6 & 3, register v >> 11 & 7; the next value is
  written to it.
- v & 0x21 == 0x20: a block, the next eight values are registers 0..7 of channel v >> 6 & 3.
- v & 0x20 == 0: a command on channel v >> 6 & 3 of kind v & 0x3f, followed by (from, to): 0 a
  load (from a DRAM address, to an offset in the DSP-side memory), 6 a store (from a memory
  offset, to a DRAM address), 1 moves nothing (verified) and counts as a command for a 0x1d
  (inferred: what follows it is capped at inferred). Other kinds were not measured and raise
  `UnknownSemantics`.

A key with bits set outside these fields was not measured.

Channel registers (four channels of eight; they do not survive from one job to the next: a
command on a channel with a register not handed in this job moves nothing and completes,
inferred: the lane memory, DRAM after a store and local memory after a load are then capped at
inferred):

- r0 the DSP-side memory: 1 the lane memory, 2 the local memory (verified both ways);
- r2 elements per unit of that memory: lane memory 0x20, one 16-bit element per lane of a vector
  (verified; 0x10 writes zeros into the vectors on a load, verified for 32-bit elements at the
  count that ends the load, `ZERO_FORMATS`, inferred otherwise); local memory 4 bytes or 2
  16-bit elements per 32-bit word (little-endian);
- r3 units per row (vectors, or 32-bit words);
- r1 the pitch between rows on the memory side in bytes (64 per vector);
- r5 elements per 16-byte DRAM line, each 128 // r5 bits packed least significant first, the
  line's remaining bits 0 (`LOAD_FORMATS`, `STORE_FORMATS`);
- r6 16-byte DRAM lines per row; r4 the pitch between DRAM rows in bytes; r7 the rows (verified:
  loads at up to 4 rows, stores at up to 2 rows; more rows inferred).

Row k of a command runs at DRAM address + k r4 (32-bit wrapping, so a pitch handed as 0xfffffe00 steps
back; verified on loads) and memory offset + k r1. A row's DRAM address is floored to 16 bytes (a
load's pitch of 0x208 steps 0x200, verified), a lane-memory offset to its vector, vectors counted
modulo `fields.LANE_VECTORS` (verified; the wrap inferred). A load row fills r3 units from its n = r5 r6 elements; with more
units than ceil(n / r2) the transfer never ends, with fewer it fills r3 units and ends
(verified). Lane memory: element i goes to lane i % 32 of vector i / 32, its low 16 bits, the
lanes beyond n 0 (verified). Local memory: element i goes to element i, zero-extended or
truncated to the memory's width (`LOCAL_LOADS`). A store row takes its n elements in the same
order, truncated to the DRAM width (`LOCAL_STORES`); with fewer than n elements on the memory
side (r2 r3 < n) it never ends (verified).

Timing (verified): a command starts when its last value is handed and runs beside the sequencer;
a 0x1d waits until the oldest outstanding command is complete ("oldest" inferred); a 0x1d with no
command outstanding never returns once the job has handed one, and returned in a job that had
handed none (inferred, one job: what follows it is capped at inferred); commands still
outstanding at the job's end complete (verified for a store, inferred for a load). The
emulator never predicts a racing read: a load's destination is unknown from its hand until its
0x1d or the job's end (`finish`), a load takes its DRAM source as of the hand (a store handed after
it runs after it), a store takes its source as of the hand and writes DRAM at once (inferred where
the source is rewritten before the store completes: that race was not measured), and the lane
memory read while any command is outstanding is a hypothesis.

What was not measured takes the engine off script: the lane memory is unknown for the rest of the
job and what later commands write is unknown (`off` says why). A command whose destination cannot
be bounded, a value handed after the grammar is lost and a 0x1d among a command's values raise
`UnknownSemantics`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from .emu import LOCAL_BASE, dram_alias
from .fields import LANE_VECTORS, Confidence
from .machine import UNKNOWN_VEC, Machine, Sv, Vec
from .semantics import LANE_COUNT, LOCAL_BYTES, UnknownSemantics
from .state import Bytes

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

CHANNELS, REGISTERS = 4, 8
LOAD, NOTHING_MOVES, STORE = 0, 1, 6
"""The command kinds measured."""
LANE_MEMORY, LOCAL_MEMORY = 1, 2
"""r0's values: the DSP-side memory."""
VECTOR_BYTES = 64
"""A lane-memory offset counts bytes of 16-bit elements: 64 per vector."""
REGISTER_KEY, BLOCK_KEY, COMMAND_KEY = 0x38E1, 0xE0, 0xFF
"""The bits each kind of key may set."""

LOAD_FORMATS: dict[int, int] = {0x10: 8, 0x08: 16, 0x04: 32, 0x0A: 12}
"""r5 on a load: bits per DRAM element, by value (verified on the camera; a 32-bit element's low
16 bits reach a lane). Other values were not measured."""
LANE_UNITS: dict[int, Confidence] = {0x20: V, 0x10: I}
"""r2 on a lane-memory load: 0x20 one element per lane (verified), 0x10 writes zeros into
every lane (inferred, but see `ZERO_FORMATS`)."""
ZERO_FORMATS = (0x04,)
"""r5 measured under r2 0x10 on a lane-memory load with zeros named before the run, at
r3 = ceil(r5 r6 / 16): zeros (verified at that count; bytes gave zeros unforeseen, inferred)."""
LOCAL_WIDTHS = {4: 8, 2: 16, 1: 32}
"""r2 on local memory: the element width in bits."""
LOCAL_LOADS: dict[tuple[int, int], Confidence] = {
    (8, 8): V,
    (8, 16): V,
    (16, 8): V,
    (16, 16): V,
    (32, 32): V,
}
"""(DRAM element bits, local element bits) measured on a local-memory load (verified): bytes to
bytes, bytes zero-extended to 16 bits, 16-bit elements truncated to their low byte, 16-bit and
32-bit elements unchanged. Other pairs were not measured."""
LOCAL_STORES: dict[tuple[int, int], Confidence] = {
    (4, 0x10): V,
    (2, 0x10): V,
    (1, 0x04): V,
}
"""(r2, r5) measured on a store from local memory (verified): bytes copied, 16-bit elements
truncated to their low byte, 32-bit elements copied. Other pairs were not measured."""

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
"""r5 on a store: 128 // bits elements per 16-byte line, each element's low bits packed least
significant first and the line's last bits 0, by value: (bits per element, tier) (verified on the
camera, pre-registered: 0x10 the low byte, 0x08 16 bits, 0x04 zero-extended to 32 bits, 0x0c /
0x0a / 0x09 the 10-, 12- and 14-bit packs). 0x44 gave the 0x04 layout on byte elements only
(inferred); 0x0d wrote zeros (bits 0). Other values were not run or never ended."""


@dataclass(frozen=True)
class Event:
    """A command handed (`kind` and its channel's registers) or a 0x1d (`kind` None) at executed
    word `t` of the job, for the timing estimate (`timing`)."""

    t: int
    kind: int | None = None
    regs: tuple[int | None, ...] = ()


class _Off(Exception):
    """A command the engine cannot run: what it does was not measured."""


@dataclass
class _Command:
    channel: int
    kind: int
    regs: list[Sv | None]
    frm: Sv
    to: Sv
    tier: Confidence
    where: str
    index: int


def _known(v: Sv | None, what: str) -> int:
    if v is None or v.value is None:
        raise _Off(f"{what} unknown")
    return v.value


@dataclass
class Engine:
    """The stream engine of one job (its channel registers do not survive the job)."""

    count: int = 0
    """The values handed so far (0x1d included)."""
    regs: list[list[Sv | None]] = field(
        default_factory=lambda: [[None] * REGISTERS for _ in range(CHANNELS)]
    )
    key: Sv | None = None
    group: list[Sv] = field(default_factory=list)
    need: int = 0
    outstanding: list[Callable[[Machine, Confidence], None]] = field(
        default_factory=list
    )
    """The completions of the commands handed and not yet waited for, oldest first, each
    called with the cap on what it writes."""
    commands: int = 0
    """The commands handed in this job. A 0x1d with none outstanding never ends after one
    (verified); in a job that handed none it returned (inferred, one job)."""
    off: str = ""
    """Why the engine went off script; empty while it follows the measured model."""
    lost: bool = False
    """The grammar is lost: later values are not parsed."""
    cap: Confidence = V
    stored: list[tuple[int, int]] = field(default_factory=list)
    """(DRAM address, length) of every store row written."""
    events: list[Event] = field(default_factory=list)
    """Every command handed and every 0x1d, in order."""

    # ------------------------------------------------------------------ the stream protocol

    def transfer(self, m: Machine, index: int, head: int, value: Sv, t: int) -> None:
        k = self.count
        self.count += 1
        where = f"transfer {k} (word {index})"
        if self.lost and head != 0x1D:
            raise UnknownSemantics(
                index, "stream", [], f"{where}: a value after the grammar was lost"
            )
        if self.lost:
            return
        if head == 0x1D:
            self.events.append(Event(t))
            self._wait(m, index, where)
        elif self.key is None:
            self._key(m, value, where)
        else:
            self.group.append(value)
            if len(self.group) == self.need:
                self._group(m, where, index, t)

    def lane_tier(self) -> Confidence:
        """Own words beside an outstanding command meet the engine at work (hypothesis)."""
        return H if self.outstanding else self.cap

    def finish(self, m: Machine) -> None:
        """The job ends: the commands still outstanding complete (inferred for loads: only a
        store outstanding at the end was measured)."""
        while self.outstanding:
            self.outstanding.pop(0)(m, min(self.cap, I))

    # ------------------------------------------------------------------ the grammar

    def _key(self, m: Machine, key: Sv, where: str) -> None:
        v = key.value
        if v is None:
            self._go_off(m, f"{where} hands an unknown key", lost=True)
            return
        if v & 0x21 == 0x21:
            allowed, need = REGISTER_KEY, 1
        elif v & 0x21 == 0x20:
            allowed, need = BLOCK_KEY, REGISTERS
        else:
            allowed, need = COMMAND_KEY, 2
        if v & ~allowed:
            self._go_off(m, f"{where} key {v:#x}: bits outside its fields", lost=True)
            return
        self.key, self.need = key, need

    def _group(self, m: Machine, where: str, index: int, t: int) -> None:
        key, values = self.key, self.group
        self.key, self.group = None, []
        v = key.value
        channel = v >> 6 & 3
        if v & 0x21 == 0x21:
            self.regs[channel][v >> 11 & 7] = _weaker(values[0], key.tier)
        elif v & 0x21 == 0x20:
            self.regs[channel][:] = [_weaker(x, key.tier) for x in values]
        else:
            regs = list(self.regs[channel])
            tier = min(key.tier, values[0].tier, values[1].tier)
            cmd = _Command(
                channel, v & 0x3F, regs, values[0], values[1], tier, where, index
            )
            known = tuple(None if r is None else r.value for r in regs)
            self.events.append(Event(t, cmd.kind, known))
            self._command(m, cmd)

    def _wait(self, m: Machine, index: int, where: str) -> None:
        if self.key is not None:
            if self.key.value & 0x20 == 0:
                raise UnknownSemantics(
                    index, "x1 0x1d", [], f"{where}: a 0x1d inside a command's values"
                )
            self._go_off(m, f"{where}: a 0x1d inside a group", lost=True)
            return
        if self.outstanding:
            self.outstanding.pop(0)(m, self.cap)
        elif not self.commands:
            self.cap = min(self.cap, I)  # a lone 0x1d returns (inferred)
        else:
            raise UnknownSemantics(
                index, "x1 0x1d", [], "a 0x1d with no command outstanding never ends"
            )

    def _go_off(self, m: Machine, why: str, lost: bool = False) -> None:
        if not self.off:
            self.off = why
        self.lost = self.lost or lost
        m.lose_lanes()
        if lost:
            # What later commands store is not known: neither is what this job stored.
            self._spoil_stores(m)

    # ------------------------------------------------------------------ commands

    def _command(self, m: Machine, cmd: _Command) -> None:
        if cmd.kind not in (LOAD, NOTHING_MOVES, STORE):
            raise UnknownSemantics(
                cmd.index, "stream", [], f"{cmd.where}: command kind {cmd.kind:#x}"
            )
        self.commands += 1
        self.outstanding.append(_nothing)
        if cmd.kind == NOTHING_MOVES:
            self.cap = min(self.cap, I)  # that a 0x1d counts it is inferred
            return
        if any(r is None for r in cmd.regs):
            self._moves_nothing(m, cmd)
            return
        if self.off:
            self._spoil(m, cmd)
            return
        try:
            if cmd.kind == LOAD:
                self.outstanding[-1] = self._load(m, cmd)
            else:
                self.outstanding[-1] = self._store(m, cmd)
        except _Off as e:
            self._go_off(m, f"{cmd.where} {e}")
            self._spoil(m, cmd)

    def _moves_nothing(self, m: Machine, cmd: _Command) -> None:
        """A register not handed: nothing moves (inferred). What the command could reach, which
        the unset registers would bound, is capped at inferred."""
        self.cap = min(self.cap, I)
        if cmd.kind == STORE:
            for page in m.boot.dram.pages.values():
                page.write(page.base, page.bytes.capped(I))
            return
        r0 = cmd.regs[0]
        if r0 is None or r0.value != LANE_MEMORY:
            local = m.boot.local.read(LOCAL_BASE, LOCAL_BYTES)
            m.boot.local.write(LOCAL_BASE, local.capped(I))

    def _rows(
        self, cmd: _Command, memory: int
    ) -> tuple[list[tuple[int, int]], Confidence]:
        """Each row's (DRAM address, memory offset), the DRAM address floored to 16 bytes, and
        the rows' tier."""
        r = cmd.regs
        rows = _known(r[7], "r7")
        if rows < 1:
            raise _Off("no rows: not measured")
        tier = I if rows > (2 if cmd.kind == STORE else 4) else V
        dram, mem = (cmd.to, cmd.frm) if cmd.kind == STORE else (cmd.frm, cmd.to)
        dram, mem = _known(dram, "a DRAM address"), _known(mem, "a memory offset")
        pitch, dpitch = 0, 0
        if rows > 1:
            pitch, dpitch = _known(r[1], "r1"), _known(r[4], "r4")
            unit = VECTOR_BYTES if memory == LANE_MEMORY else 4
            if pitch % unit:
                raise _Off(f"memory pitch {pitch:#x}: not measured")
            if dpitch % 16 and cmd.kind == STORE:
                raise _Off(f"DRAM pitch {dpitch:#x} on a store: not measured")
            if dpitch % 16 and rows > 2:
                # Two rows cannot tell a floored pitch from floored row addresses.
                tier = min(tier, I)
        out = [((dram + k * dpitch) & 0xFFFFFFF0, mem + k * pitch) for k in range(rows)]
        return out, tier

    def _local_offset(self, off: int, n: int) -> int:
        if off % 4:
            raise _Off(f"local offset {off:#x} within a word: not measured")
        if off < 0 or off + n > LOCAL_BYTES:
            raise _Off(f"local bytes {off:#x}..{off + n:#x}: not measured")
        return off

    def _load(self, m: Machine, cmd: _Command) -> Callable[[Machine, Confidence], None]:
        """Checks the load, makes its destination unknown and returns its completion."""
        r = cmd.regs
        memory = _known(r[0], "r0")
        r2, r3, r5, r6 = (_known(r[i], f"r{i}") for i in (2, 3, 5, 6))
        if r5 not in LOAD_FORMATS:
            raise _Off(f"load format {r5:#x}: not measured")
        bits = LOAD_FORMATS[r5]
        n = r5 * r6
        rows, tier = self._rows(cmd, memory)
        tier = min([tier, self.cap, cmd.tier, *(x.tier for x in r)])
        if memory == LANE_MEMORY:
            if r2 not in LANE_UNITS:
                raise _Off(f"r2 {r2:#x} on a lane-memory load: not measured")
            per = r2
        elif memory == LOCAL_MEMORY:
            if r2 not in LOCAL_WIDTHS or (bits, LOCAL_WIDTHS[r2]) not in LOCAL_LOADS:
                raise _Off(
                    f"a local load of {bits}-bit elements at r2 {r2:#x}: not measured"
                )
            per = r2
        else:
            raise _Off(f"r0 {memory:#x}: not measured")
        needed = -(-n // per)
        if r3 > needed:
            raise UnknownSemantics(
                cmd.index,
                "stream",
                [],
                f"{cmd.where}: a load of {r3} units for {needed} never ends",
            )
        if memory == LANE_MEMORY:
            if r2 != 0x10 or r5 not in ZERO_FORMATS or r3 != needed:
                tier = min(tier, LANE_UNITS[r2])
            return self._lane_load(m, cmd, rows, bits, r6, r2, r3, tier)
        width = LOCAL_WIDTHS[r2]
        tier = min(tier, LOCAL_LOADS[(bits, width)])
        return self._local_load(m, cmd, rows, bits, r6, width, r2, r3, tier)

    def _lane_load(self, m, cmd, rows, bits, lines, r2, r3, tier):
        held: dict[int, Vec] = {}
        for _, off in rows:
            for v in range(r3):
                at = (off // VECTOR_BYTES + v) % LANE_VECTORS
                held[at] = Vec(UNKNOWN_VEC.lanes)
        if not m.lanes_lost:
            m.memory.update(held)
        # The queue runs in order: a store handed later writes DRAM after this load read it.
        sources = [
            _unpack(m.boot.read(dram, 16 * lines), bits, lines) for dram, _ in rows
        ]

        def complete(m: Machine, cap: Confidence) -> None:
            if m.lanes_lost:
                return
            for (_, off), elements in zip(rows, sources, strict=True):
                base = off // VECTOR_BYTES
                for v in range(r3):
                    at = (base + v) % LANE_VECTORS
                    wrap = I if base + v >= LANE_VECTORS else V
                    chunk = elements[v * LANE_COUNT : (v + 1) * LANE_COUNT]
                    chunk += [(0, V)] * (LANE_COUNT - len(chunk))
                    if r2 != 0x20:
                        chunk = [(0, V)] * LANE_COUNT  # zeros (verified)
                    lanes = tuple(None if x is None else x & 0xFFFF for x, _ in chunk)
                    t = min([tier, cap, wrap, *(t for x, t in chunk if x is not None)])
                    # A vector written while the load ran raced it: not known.
                    racing = m.memory.get(at) is not held[at]
                    m.memory[at] = UNKNOWN_VEC if racing else Vec(lanes, t)

        return complete

    def _local_load(self, m, cmd, rows, bits, lines, width, r2, r3, tier):
        size = 4 * r3
        src = m.boot.source("stream load in flight", f"{cmd.where}")
        for _, off in rows:
            self._local_offset(off, size)
        for _, off in rows:
            m.boot.local.write(LOCAL_BASE + off, Bytes.unknown(size, src))
        loaded = m.boot.source(
            "stream load", "the stream engine's load to local memory"
        )
        sources = [
            _unpack(m.boot.read(dram, 16 * lines), bits, lines) for dram, _ in rows
        ]

        def complete(m: Machine, cap: Confidence) -> None:
            nb = width // 8
            for (_, off), elements in zip(rows, sources, strict=True):
                out = Bytes.unknown(size, loaded)
                for i, (x, t) in enumerate(elements[: r2 * r3]):
                    if x is None:
                        continue
                    data = (x & (1 << width) - 1).to_bytes(nb, "little")
                    for b in range(nb):
                        out.data[nb * i + b] = data[b]
                        out.known[nb * i + b] = 1
                        out.taint[nb * i + b] = min(tier, cap, t)
                now = m.boot.local.read(LOCAL_BASE + off, size)
                for i in range(size):
                    if now.src[i] != src:  # written while the load ran: raced
                        out.known[i] = 0
                m.boot.local.write(LOCAL_BASE + off, out)

        return complete

    def _store(
        self, m: Machine, cmd: _Command
    ) -> Callable[[Machine, Confidence], None]:
        """Writes the store's DRAM rows from its source as of now and returns its completion."""
        r = cmd.regs
        memory = _known(r[0], "r0")
        r2, r3, r5, r6 = (_known(r[i], f"r{i}") for i in (2, 3, 5, 6))
        if r5 not in STORE_FORMATS:
            raise _Off(f"store format {r5:#x}: not measured")
        bits, ftier = STORE_FORMATS[r5]
        n = (128 // bits if bits else 0) * r6
        rows, tier = self._rows(cmd, memory)
        tier = min([tier, ftier, self.cap, cmd.tier, *(x.tier for x in r)])
        if memory == LANE_MEMORY:
            if r2 != 0x20:
                raise _Off(f"r2 {r2:#x} on a lane-memory store: not measured")
        elif memory == LOCAL_MEMORY:
            if (r2, r5) not in LOCAL_STORES:
                raise _Off(f"a local store at r2 {r2:#x}, r5 {r5:#x}: not measured")
            tier = min(tier, LOCAL_STORES[(r2, r5)])
        else:
            raise _Off(f"r0 {memory:#x}: not measured")
        if r2 * r3 < n:
            raise UnknownSemantics(
                cmd.index,
                "stream",
                [],
                f"{cmd.where}: a store of {n} elements from {r2 * r3} never ends",
            )
        if memory == LOCAL_MEMORY:
            nb = 4 // r2
            for _, off in rows:
                self._local_offset(off, n * nb)
        src = m.boot.source("store", f"the stream engine's store, {cmd.where}")
        taken = []
        for dram, off in rows:
            if memory == LOCAL_MEMORY:
                data = m.boot.local.read(LOCAL_BASE + off, n * nb)
                taken.append(data)
                element = _local_elements(data, nb)
            else:
                taken.append(_lane_source(m, off, n))
                element = _lane_elements(m, off, r5)
            width = 8 * nb if memory == LOCAL_MEMORY else 16
            out = pack(src, bits, 16 * r6, element, tier, width)
            m.boot.dram.write(dram_alias(dram), out)
            self.stored.append((dram, 16 * r6))

        def complete(m: Machine, cap: Confidence) -> None:
            if memory == LOCAL_MEMORY:
                now = [m.boot.local.read(LOCAL_BASE + off, n * nb) for _, off in rows]
                same = all(_same_bytes(a, b) for a, b in zip(now, taken, strict=True))
            else:
                now = [_lane_source(m, off, n) for _, off in rows]
                same = not m.lanes_lost and all(
                    x is y
                    for a, b in zip(now, taken, strict=True)
                    for x, y in zip(a, b)
                )
            if same:
                return
            # The source was rewritten while the store was queued: a race not measured.
            for dram, _ in rows:
                got = m.boot.read(dram, 16 * r6)
                for i, owner in enumerate(got.src):
                    if owner == src:
                        got.taint[i] = min(got.taint[i], I)
                m.boot.dram.write(dram_alias(dram), got)

        return complete

    # ------------------------------------------------------------------ off script

    def _spoil(self, m: Machine, cmd: _Command) -> None:
        """What a command the engine cannot run writes is not known: its destination becomes
        unknown, and a destination that cannot be bounded raises."""
        try:
            self._spoil_reach(m, cmd)
        except _Off as e:
            raise UnknownSemantics(
                cmd.index, "stream", [], f"{cmd.where} off script: {e}"
            ) from e

    def _spoil_reach(self, m: Machine, cmd: _Command) -> None:
        lost = m.boot.source("stream off script", self.off)
        r = cmd.regs
        rows = _known(r[7], "r7")
        if rows < 1:
            raise _Off("no rows")
        if cmd.kind == STORE:
            lines = _known(r[6], "r6")
            pitch = _known(r[4], "r4") if rows > 1 else 0
            at = _known(cmd.to, "a DRAM address")
            for k in range(rows):
                dram = (at + k * pitch) & ~0xF
                m.boot.dram.write(dram_alias(dram), Bytes.unknown(16 * lines, lost))
            return
        memory = _known(r[0], "r0")
        if memory == LANE_MEMORY:
            return  # the lane memory is lost already
        if memory != LOCAL_MEMORY:
            raise _Off(f"r0 {memory:#x}")
        size = 4 * _known(r[3], "r3")
        pitch = _known(r[1], "r1") if rows > 1 else 0
        at = _known(cmd.to, "a memory offset")
        for k in range(rows):
            off = self._local_offset(at + k * pitch, size)
            m.boot.local.write(LOCAL_BASE + off, Bytes.unknown(size, lost))

    def _spoil_stores(self, m: Machine) -> None:
        lost = m.boot.source("stream off script", self.off)
        for dram, n in self.stored:
            m.boot.dram.write(dram_alias(dram), Bytes.unknown(n, lost))


def _nothing(m: Machine, cap: Confidence) -> None:
    pass


def _weaker(v: Sv, tier: Confidence) -> Sv:
    return v if v.tier <= tier else Sv(v.value, tier)


def _unpack(data: Bytes, bits: int, lines: int) -> list[tuple[int | None, Confidence]]:
    """The elements of `lines` 16-byte DRAM lines at `bits` each, least significant first:
    (value or None, tier); an element is known when every byte it draws on is known."""
    per, mask = 128 // bits, (1 << bits) - 1
    out = []
    for line in range(lines):
        chunk = data[16 * line : 16 * line + 16]
        acc = int.from_bytes(chunk.data, "little")
        for j in range(per):
            span = range(j * bits // 8, ((j + 1) * bits - 1) // 8 + 1)
            if all(chunk.known[b] for b in span):
                tier = min(Confidence(chunk.taint[b]) for b in span)
                out.append((acc >> (j * bits) & mask, tier))
            else:
                out.append((None, V))
    return out


def _same_bytes(a: Bytes, b: Bytes) -> bool:
    return a.data == b.data and a.known == b.known and a.src == b.src


def _lane_source(m: Machine, off: int, n: int) -> list[Vec | None]:
    """The vectors a lane-memory store of n elements from offset `off` reads."""
    base = off // VECTOR_BYTES
    return [m.memory.get((base + v) % LANE_VECTORS) for v in range(-(-n // LANE_COUNT))]


def _local_elements(
    data: Bytes, nb: int
) -> Callable[[int], tuple[int | None, Confidence]]:
    """Element i of a local-memory store: `nb` little-endian bytes at nb i."""

    def element(i: int) -> tuple[int | None, Confidence]:
        b = data[nb * i : nb * i + nb]
        if not all(b.known):
            return None, V
        return int.from_bytes(b.data, "little"), b.weakest()

    return element


def _lane_elements(
    m: Machine, off: int, fmt: int
) -> Callable[[int], tuple[int | None, Confidence]]:
    """Element i of a lane-memory store from offset `off`: lane i % 32 of vector i / 32."""
    base = off // VECTOR_BYTES

    def element(i: int) -> tuple[int | None, Confidence]:
        at = base + i // LANE_COUNT
        v = m.memory.get(at % LANE_VECTORS, UNKNOWN_VEC)
        x = v.lanes[i % LANE_COUNT]
        if fmt == 0x44 and x is not None and x > 0xFF:
            x = None
        return x, min(v.tier, I if at >= LANE_VECTORS else V)

    return element


def pack(
    src: int,
    bits: int,
    n: int,
    element: Callable[[int], tuple[int | None, Confidence]],
    tier: Confidence,
    width: int = 16,
) -> Bytes:
    """n store bytes of the elements `element`(i) = (value or None, tier), `width` bits wide in
    the memory, at `bits` each: 128 // bits per 16-byte line, least significant first, the rest
    of the line 0; bits 0 writes zeros. A byte is known when every element it draws on is known;
    every byte comes from source `src`."""
    out = Bytes.unknown(n, src)
    per = 128 // bits if bits else 0
    mask = (1 << min(bits, width)) - 1
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
