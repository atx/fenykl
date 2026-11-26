"""The stream engine as the datapath executor drives it: the values X1's head 0x1c hands it, the
channel registers they program, and the loads and stores between DRAM and the DSP's memories.

The engine sees the values handed and nothing else of the program: which words hand them does not
matter (verified on the camera). X1 0x1c hands B[f115_119] (read before the word's ports write);
X1 0x1d hands nothing and waits for one command (below); both with register a 0
(`fields.STREAM_SELECT`, which `Machine` checks).

Grammar (verified): the values form groups, each starting with a key v.

- v & 0x21 == 0x21: one register, channel v >> 6 & 3, register v >> 11 & 7; the next value is
  written to it.
- v & 0x21 == 0x20: a block, the next eight values are registers 0..7 of channel v >> 6 & 3.
- v & 0x20 == 0: a command on channel v >> 6 & 3 of kind v & 0x3f, followed by (from, to): 0 a
  load (from a DRAM address, to an offset in the DSP-side memory), 6 a store (from a memory
  offset, to a DRAM address), 1 moves nothing (verified) and counts as a command for a 0x1d
  (inferred: what follows it is capped at inferred). Kinds 2 and 4 load and store as 0 and 6,
  kinds 3 and 5 move nothing as 1, kind 8 fills (under r0 1 r3 lane-memory vectors from the one
  "to" names, every lane "from"'s low 16 bits; under r0 2 r3 local words from "to", each filled
  with copies of "from"'s low r2-wide element: `LOCAL_FILLS`; no DRAM read or written), each only
  in the shapes it ran in (`KIND_SHAPES`, by tier). Other kinds and shapes were not measured and raise
  `UnknownSemantics`.

A key with bits set outside these fields was not measured.

Channel registers (four channels of eight; they do not survive from one job to the next: a
command on a channel with a register not handed in this job moves nothing and completes,
inferred: the lane memory, DRAM after a store and local memory after a load are then capped at
inferred):

- r0 the DSP-side memory: 1 the lane memory, 2 the local memory (verified both ways); a store
  under r0 0x00, 0x10 or 0x40 writes nothing (`NO_MEMORY`);
- r2 elements per unit of that memory: lane memory 0x20, one 16-bit element per lane of a vector
  (verified; 0x10 writes zeros into the vectors on a load, verified for 32-bit elements at the
  count that ends the load, `ZERO_FORMATS`, inferred otherwise); local memory 4 bytes or 2
  16-bit elements per 32-bit word (little-endian);
- r3 units per row (vectors, or 32-bit words);
- r1 the pitch between rows on the memory side in bytes (64 per vector);
- r5 elements per 16-byte DRAM line, each 128 // r5 bits packed least significant first, the
  line's remaining bits 0 (`LOAD_FORMATS`, `STORE_FORMATS`);
- r6 16-byte DRAM lines per row; r4 the pitch between DRAM rows in bytes; r7 the rows (verified:
  `LANE_LOAD_ROWS`, `LANE_STORE_ROWS`, local memory loads at up to 4 rows and stores at up to 2;
  other counts inferred).

Row k of a command runs at DRAM address + k r4 (32-bit wrapping, so a pitch handed as
0xfffffe00 steps back; verified on loads) and memory offset + k r1. A row's DRAM address is
floored to 16 bytes (a load's pitch of 0x208 steps 0x200, verified; a store's of 0x108 at two rows
steps 0x100, verified, other store pitches off 16 bytes not run), a lane-memory offset to its
vector, vectors counted modulo `fields.LANE_VECTORS` (verified; the wrap inferred). Of the lane
pitches that are not whole vectors only `HALF_VECTOR_ROWS` ran: both rows write one vector, and
the later row lands (verified). A load row
fills r3 units from its n = r5 r6 elements; with more units than ceil(n / r2) the transfer never
ends, with fewer it fills r3 units and ends (verified). Lane memory: element i goes to lane
i % 32 of vector i / 32, its low 16 bits, the lanes beyond n 0 (verified). Local memory: element
i goes to element i, zero-extended or truncated to the memory's width (`LOCAL_LOADS`). A store
row takes its n elements in the same order, truncated to the DRAM width (`LOCAL_STORES`); with
fewer than n elements on the memory side (r2 r3 < n) it never ends (verified).

Zero counts: a lane-memory load of r3 units with r6 0 ended (inferred, three jobs; what it writes
was not seen, so its vectors are unknown), a lane-memory load with r3 0 ended in the shapes of
`ZERO_LOADS` (inferred; the vectors read back after it unchanged, the rest of its footprint
unknown), a lane-memory store with r3 0 of n > 0 elements ended and wrote nothing (inferred,
five jobs), and a lane-memory store with r6 0 ended and wrote nothing in the shapes of
`ZERO_STORES` (inferred). Every other load or store with r3 or r6 0, on either memory, was not
measured and goes off script.

Timing (verified): a command starts when its last value is handed and runs beside the sequencer;
a 0x1d waits until the oldest outstanding command is complete ("oldest" inferred); a 0x1d with
no command outstanding never returns once the job has handed one; in a job that has handed none
it was not measured (it raises `UnknownSemantics`); commands still outstanding at the job's end
complete (verified for a store, inferred for a load). Of commands handed back to back before any
0x1d (32-byte loads) the first 19 ran in one job (measured); the rest wait in the engine and run
as 0x1d's free room: 32 loads handed, then 32 0x1d, all returned (verified) and the loads read
back had landed (measured); with no 0x1d the 20th to 32nd had not run 8 us later (measured). So
a 0x1d counts the commands handed, run or held (inferred). A command of kind 2 counted for a
0x1d in one job (measured: a load joined, then kind 2, then a 0x1d that returned) and in another
loaded 32 bytes from its "from" into the vector its "to" names, as kind 0 does (measured; the
vector its "from" names kept its content). The emulator queues the held commands as the engine
does, up to `DEPTH` outstanding; handing past it wedges the job, so more raises. The 19 running
at once change only when a held load lands, not what it writes. The emulator never predicts a
racing read: a load's destination is unknown from its hand until its 0x1d or the job's end
(`finish`), a load takes its DRAM source as of the hand (a store handed after it runs after it),
a store takes its source as of the hand and writes DRAM at once (where the source is rewritten
before the store completes, inferred for a store of one vector and unknown for the rewritten
vectors of a longer store: one of 0x80 vectors took the new bytes, measured), the order of
commands on one DRAM range while one of them is outstanding is inferred, and the lane memory read
while any command is outstanding is a hypothesis, but outside every outstanding command's
vectors: there verified beside one-row byte loads and stores of kinds 0 / 6, inferred beside
other lane-memory shapes (`Engine.lane_tier`).

What was not measured takes the engine off script (so does a command that moves more DRAM bytes
than a job's time allows, `MOST_BYTES`): the lane memory is unknown for the rest of the job, what
later commands write is unknown (`off` says why), and so is whether the job ends: a command it
holds may never complete, so the job's flow stops at `job.Stop.OPEN_END`, not at its halt
(`Machine.open_end`). A command whose destination cannot be bounded (among them a store whose rows
write more than the DRAM alias space), a value handed after the grammar is lost and a 0x1d among a
command's values (but right after a register or store key with a command outstanding: it waits and
the group goes on, `Engine._joins_in_group`) raise `UnknownSemantics`, as do kinds and shapes not run and a job that never ends
or wedges; on a lenient machine they lapse instead (`Machine.lapse`): a command that never ends
goes off script, a kind or shape not run and what cannot be bounded forget every memory, and a 0x1d
that never returns or was not measured and a queue past `DEPTH` go on as if they had returned."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from math import gcd

from .fields import LANE_VECTORS, VECTOR_BYTES, Confidence
from .fields import STREAM_CHANNELS as CHANNELS
from .fields import STREAM_LANE_MEMORY as LANE_MEMORY
from .fields import STREAM_LOAD as LOAD
from .fields import STREAM_LOCAL_MEMORY as LOCAL_MEMORY
from .fields import STREAM_NOTHING_MOVES as NOTHING_MOVES
from .fields import STREAM_STORE as STORE
from .machine import UNKNOWN_VEC, Machine, Sv, Vec
from .semantics import LANE_COUNT, LOCAL_BYTES
from .state import DRAM_BYTES, LOCAL_BASE, Bytes, dram_alias

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

REGISTERS = 8
FILL = 8
"""Kind 8: fills the lane-memory vectors at "to" with "from"'s low 16 bits, or under r0 2 the
local words at "to" with copies of "from"'s low r2-wide element (no DRAM side)."""
KIND_AS = {
    2: LOAD,
    3: NOTHING_MOVES,
    4: STORE,
    5: NOTHING_MOVES,
    7: NOTHING_MOVES,
    FILL: FILL,
}
"""Kinds that ran as another kind, only in the shapes of `KIND_SHAPES`: 2 a load, 4 a store
(both as kinds 0 / 6), 3, 5 and 7 move nothing (as kind 1; counted by a 0x1d, inferred), 8 a
fill."""
BASE_SHAPE = (1, None, 0x20, 1, None, 0x10, 2, 1)
"""Registers 0..7 (None: any): the lane memory, byte elements, one vector, two 16-byte lines,
one row."""
HALF_SHAPE = (1, None, 0x20, 1, None, 0x08, 4, 1)
"""One vector of 32 16-bit elements (four lines)."""
ROWS_SHAPE = (1, 0x80, 0x20, 1, 0x100, 0x10, 2, 3)
"""Three rows of one vector, memory pitch two vectors, DRAM pitch 0x100."""
STORE_ROWS2 = (1, 0x40, 0x20, 1, 0x100, 0x10, 2, 2)
STORE_ROWS3 = (1, 0x40, 0x20, 1, 0x200, 0x10, 2, 3)
"""Two rows at DRAM pitch 0x100 and three at 0x200 of one vector each, memory pitch one vector:
the store rows run for kind 4, one job each (inferred)."""
LOCAL_STORE = (2, 0x490, 4, 0x10, 0x400, 0x10, 4, 3)
"""Three rows of 16 local words (64 bytes) at local pitch 0x490 to DRAM pitch 0x400: the resampler's
kind 4 store at its 64-wide request (verified on the camera, three jobs, from two local offsets)."""
LOCAL_STORE_ROW = (2, 0x490, 4, 0x10, 0x400, 0x10, 4, 1)
LOCAL_STORE_NARROW = (2, 0x490, 4, 0xC, 0x100, 0x10, 3, 3)
"""One row of `LOCAL_STORE`, and three rows of 12 words at DRAM pitch 0x100 (inferred: one job
each)."""
LOCAL_STORES_ANY = (2, 0x490, 4, None, None, 0x10, None, 3)
"""Three rows at local pitch 0x490 of any width and DRAM pitch (inferred: the widths and pitches the
resampler hands at other requests, none run as such)."""
LOCAL_SHAPE = (2, None, 4, 8, None, 0x10, 2, 1)
"""32 bytes as bytes in local memory (eight words)."""
QUAD_SHAPE = (1, None, 0x20, 4, None, 0x10, 8, 1)
"""Four vectors of byte elements (eight lines)."""
PAIR_SHAPE = (1, None, 0x20, 2, None, 0x10, 4, 1)
"""Two vectors of byte elements (four lines)."""
TRIPLE_SHAPE = (1, None, 0x20, 3, None, 0x10, 6, 1)
"""Three vectors of byte elements (six lines)."""
LOCAL_FILLS: dict[tuple[int, int, int], tuple[int, Confidence]] = {
    (4, 3, 2): (0x400, I),
    (4, 5, 2): (0x500, I),
    (4, 8, 2): (0x400, I),
    (2, 4, 4): (0x700, I),
    (2, 8, 2): (0x600, I),
    (1, 6, 3): (0x600, I),
    (1, 3, 5): (0x700, I),
}
"""Kind 8 under r0 2 on channel 0 (r1 0, r4 0, r5 0x10, r7 1) by the (r2, r3, r6) that ran: the
"to" it ran at and its tier: inferred, each shape having run in one job (the rule itself, r3 words of
copies of "from"'s low r2-wide element, matched on pre-registered jobs at every r2). Another word-aligned
"to" in `LOCAL_FILL_TO` is capped at inferred; other registers, channels and offsets, and a "from"
at 0x8000 or above or whose element has its top bit set (every value that ran was below 0x8000,
its element's top bit clear), raise
`UnknownSemantics`."""
LOCAL_FILL_TO = range(0x400, 0x701)
WRAPPED, DIRECT = "wrapped", "direct"
"""How a kind's memory-side offset was handed where it ran: a DRAM address counted modulo
`fields.LANE_VECTORS` vectors (the job's dst or src), or an offset inside the memory."""
KIND_SHAPES: dict[int, dict[tuple[int | None, ...], tuple[Confidence, str | None]]] = {
    2: {
        BASE_SHAPE: (I, WRAPPED),
        HALF_SHAPE: (V, WRAPPED),
        ROWS_SHAPE: (V, WRAPPED),
        LOCAL_SHAPE: (V, DIRECT),
    },
    3: {BASE_SHAPE: (I, WRAPPED)},
    4: {
        BASE_SHAPE: (I, WRAPPED),
        HALF_SHAPE: (V, DIRECT),
        LOCAL_SHAPE: (V, DIRECT),
        PAIR_SHAPE: (I, DIRECT),
        TRIPLE_SHAPE: (I, DIRECT),
        STORE_ROWS2: (I, DIRECT),
        STORE_ROWS3: (I, DIRECT),
        LOCAL_STORE: (V, DIRECT),
        LOCAL_STORE_ROW: (I, DIRECT),
        LOCAL_STORE_NARROW: (I, DIRECT),
        LOCAL_STORES_ANY: (I, DIRECT),  # last: the measured rows above take precedence
    },
    5: {BASE_SHAPE: (I, WRAPPED)},
    7: {BASE_SHAPE: (I, WRAPPED)},
    FILL: {
        BASE_SHAPE: (V, WRAPPED),
        HALF_SHAPE: (V, WRAPPED),
        QUAD_SHAPE: (V, WRAPPED),
        **{
            (2, 0, r2, r3, 0, 0x10, r6, 1): (tier, DIRECT)
            for (r2, r3, r6), (_, tier) in LOCAL_FILLS.items()
        },
    },
}
"""The shapes each kind ran in: (tier, the memory-side offset's form; both "from" and "to"
for kinds that move nothing; a lane fill's "to" also vector-aligned, as every one ran). Verified where two jobs (one at least pre-registered) matched, inferred for one job
(local fills: `LOCAL_FILLS`);
a wrapped offset that ran takes the shape's tier, not the wrap's; the first shape that matches
gives the tier. Rows ran for kind 2 (loads) and kind 4 (`STORE_ROWS2`, `STORE_ROWS3`, the local
stores). Each kind 4 command, in every shape run, takes exactly one 0x1d (verified).
Another shape, or the other offset form, raises `UnknownSemantics`."""
DEPTH = 44
"""Commands outstanding (handed, not yet joined by a 0x1d) that all completed: 40 and 44 did
(measured, 44 again beside the wedges below); 45 and 46 wedged the job on two boots and 48
twice, also with only 44 0x1d after them, so the depth is 44 and handing past it kills the job
(measured). More than `DEPTH` raises `UnknownSemantics`."""
MOST_BYTES = 32 << 20
"""DRAM bytes a command may move and still be known to end: at the fastest rate timed (0.42 us per
KiB, `timing.RATES`) 32 MiB take 13.8 ms, past the 8 ms after which the firmware aborts the job
(`timing.LONGEST_JOB_US`). A command of more takes the engine off script: whether the job ends,
and what it moved before the abort, are not known."""
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
ZERO_LOADS: dict[tuple[int, int, int], int] = {
    (0x20, 0x10, 0x40): 32,
    (0x20, 0x10, 0x100): 32,
}
"""A lane-memory load with r3 0 (one row) by the (r2, r5, r6) that ran: the vectors from its "to"
seen unchanged after it, of the ceil(r5 r6 / r2) a full count fills. Measured on the camera: the
job ended, and a store from the same "to" emitted what those vectors held before the job, byte
for byte (five jobs at r6 0x40, two of them at a src other than the one the held bytes came from:
the whole footprint; two at r6 0x100: its first 32 vectors, the other 96 read back as bytes of
no known origin, so they are unknown). Inferred: the jobs idled a whole program word, not the
count alone."""
ZERO_STORES = frozenset((0x20, r3, 0x10) for r3 in (0, 0x20, 0x80, 0x88, 0x200))
"""A lane-memory store with r6 0 (one row) by the (r2, r3, r5) that ran. Measured on the camera,
29 jobs, each with this store as its only one, at dst: every job ended, dst read back as its fill
over the whole length and the guards around it were intact (at r3 0x200 the length was 1 KiB of
the 16 KiB a full count fills). Inferred: it writes nothing; the DRAM bytes a full r3 count would
store keep their content at most inferred."""
LANE_LOAD_ROWS = frozenset((*range(1, 6), 8))
"""Rows of a lane-memory load measured on the camera (verified; pre-registered: five rows at DRAM
pitch 0x100 of one vector each, eight at 0x200 at a lane pitch of two vectors, each row at its
pitch); 5 and 8 in `ROW_SHAPE` only. Other counts inferred."""
LANE_STORE_ROWS = range(1, 5)
"""Rows of a lane-memory store measured on the camera (verified; pre-registered: three rows at DRAM
pitch 0x200 and four at 0x100, one vector each, each row at its pitch); 3 and 4 in `ROW_SHAPE`
only. More rows inferred."""
STORE_PITCH_OFF16 = 0x108
"""The only store DRAM pitch off 16 bytes that ran, at two rows (verified, pre-registered: row 1 at
+0x100, not +0x108 or +0x110); other such pitches, or more rows, were not measured."""
ROW_SHAPE = (1, None, 0x20, 1, None, 0x10, 2, None)
"""Rows of one vector of bytes each: the shape of `LANE_LOAD_ROWS` and `LANE_STORE_ROWS` past the
counts measured before (loads 1..4, stores 1..2, any shape)."""
HALF_VECTOR_ROWS = (1, 0x20, 0x20, 1, None, 0x10, 2, 2)
"""A lane-memory load of two rows of one vector at a lane pitch of 0x20 bytes from a vector-aligned
offset: row 1's offset floors to row 0's vector and row 1's elements land there, the next vector untouched (verified,
pre-registered: one job, each row from its own DRAM line pair)."""
NO_MEMORY = (0x00, 0x10, 0x40)
NO_MEMORY_STORE = (None, None, 0x20, 1, None, 0x10, 2, 1)
"""A store under r0 0x00, 0x10 or 0x40 in this shape (one row of one vector of bytes) ended and
wrote nothing: dst kept its nonzero fill over its whole length (verified, pre-registered against
zeros and a lane-memory store; one job each). Other shapes, other r0 values, and loads under
these, were not measured."""
LOCAL_LOAD_NEVER_ENDS = (2, None, 1, 8, None, 0x10, 2, 1)
"""A local-memory load of 32 bytes into eight 32-bit words (r2 1, r5 0x10, one row): the camera's
job never ended (one job; the firmware timed it out and the store of those words after it, a
measured shape, wrote nothing), so this shape never ends (inferred). Other byte or 16-bit loads
into 32-bit words were not run."""
ROWS_LOAD_BESIDE = (1, 0x1000, 0x20, 0x40, 0x800, 0x10, 0x80, 3)
LOCAL_LOAD_BESIDE = (2, 0, 4, 0x200, 0, 0x10, 0x80, 1)
"""Loads beside which own lane words outside their vectors were measured on the camera: a 3-row
byte load of 0x40 vectors per row, and a local-memory byte load of 0x800 bytes (which writes no
lane vector). While each was outstanding, an X0 store into a vector outside it, and lane reads of
such a vector, gave the bytes as alone (verified, pre-registered; one job each)."""
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
    (2, 0x08): V,
    (1, 0x04): V,
}
"""(r2, r5) measured on a store from local memory (verified): bytes copied, 16-bit elements
truncated to their low byte (r5 0x10) or copied as 16 bits (r5 0x08: pre-registered against
the low bytes, one job), 32-bit elements copied. Other pairs were not measured."""

STORE_FORMATS: dict[int, tuple[int, Confidence]] = {
    0x10: (8, V),
    0x0C: (10, V),
    0x0A: (12, V),
    0x09: (14, V),
    0x08: (16, V),
    0x04: (32, V),
    0x44: (32, I),  # measured on byte elements: a wider element is not predicted
    0x0D: (0, V),
    0x00: (0, V),
    0x40: (0, V),
    **dict.fromkeys((0x01, 0x46, 0x80, 0x200, 0x400), (0, I)),
}
"""r5 on a store: 128 // bits elements per 16-byte line, each element's low bits packed least
significant first and the line's last bits 0, by value: (bits per element, tier) (verified on the
camera, pre-registered: 0x10 the low byte, 0x08 16 bits, 0x04 zero-extended to 32 bits, 0x0c /
0x0a / 0x09 the 10-, 12- and 14-bit packs). 0x44 gave the 0x04 layout on byte elements only
(inferred); 0x0d wrote zeros (bits 0). 0x00 and 0x40 wrote zeros (verified: pre-registered against
"nothing written", one vector over two lines, the rest of a nonzero fill kept). 0x01, 0x46, 0x80,
0x200 and 0x400 wrote zeros too (inferred: measured on the camera after the run, one to five jobs
each over a dst prefilled with a nonzero byte, the job ending and every byte of the length 0).
Other values were not run or never ended."""


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
    wrap_tier: Confidence = I
    """The tier of a lane-memory offset counted modulo `fields.LANE_VECTORS` (inferred, unless
    the kind ran so)."""


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
    spans: list[tuple[frozenset[int], Confidence] | None] = field(default_factory=list)
    """Per outstanding command the lane-memory vectors it reads or writes; None where not
    bounded (a local-memory command, one that moves nothing, the engine off script)."""
    drams: list["_Dram"] = field(default_factory=list)
    """Per outstanding command the DRAM it reads and writes (`_behind`)."""
    commands: int = 0
    """The commands handed in this job. A 0x1d with none outstanding never ends after one
    (verified); before the first it was not measured."""
    off: str = ""
    """Why the engine went off script; empty while it follows the measured model."""
    lost: bool = False
    """The grammar is lost: later values are not parsed."""
    cap: Confidence = V
    stored: list[tuple[int, int]] = field(default_factory=list)
    """(DRAM address, length) of every store row written."""
    events: list[Event] = field(default_factory=list)
    """Every command handed and every 0x1d, in order."""
    unwritten: list[tuple[str, int]] = field(default_factory=list)
    """(where, vector) for each lane vector a store read that nothing had written in the job (no
    load, fill or X0 store; nor carried from an earlier job): a check for programs whose own
    stores went elsewhere, not a camera fact."""

    # ------------------------------------------------------------------ the stream protocol

    def transfer(self, m: Machine, index: int, head: int, value: Sv, t: int) -> None:
        k = self.count
        self.count += 1
        where = f"transfer {k} (word {index})"
        if self.lost and head != 0x1D:
            why = f"{where}: a value after the grammar was lost"
            m.lapse(index, "stream", why)
            m.lose_memories(why)
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

    def lane_tier(self, vector: int | None = None) -> Confidence:
        """Own words beside an outstanding command meet the engine at work (hypothesis), unless
        `vector` lies outside every outstanding command's lane-memory vectors: lane loads and
        stores on one bank while a byte load of one row into the other and a byte store of one
        row from it were outstanding gave the pre-registered bytes on the camera (verified for
        those shapes and `ROWS_LOAD_BESIDE` / `LOCAL_LOAD_BESIDE`); beside any other lane-memory
        shape outside its vectors inferred."""
        if not self.outstanding:
            return self.cap
        if vector is None or any(s is None or vector in s[0] for s in self.spans):
            return H
        return min([self.cap, *(s[1] for s in self.spans)])

    def _pop(self, m: Machine, cap: Confidence) -> None:
        self.spans.pop(0)
        self.outstanding.pop(0)(m, min(cap, self.drams.pop(0).cap))

    def _span(
        self, cmd: _Command, plain: bool
    ) -> tuple[frozenset[int], Confidence] | None:
        """The lane vectors a lane-memory load, store or fill writes or reads, as the command
        does (a load r3 per row, a store its elements' vectors per row, a fill r3), and the tier
        of words beside it elsewhere: verified for a plain (kind 0 / 6) one-row byte command at
        r2 0x20 and the loads of `ROWS_LOAD_BESIDE`, inferred for other shapes; no vectors at
        verified for the local load of `LOCAL_LOAD_BESIDE`; None for other memory."""
        r = cmd.regs
        if r[0].value != LANE_MEMORY:
            if plain and cmd.kind != STORE and _fits(cmd, LOCAL_LOAD_BESIDE):
                return frozenset(), V
            return None
        if cmd.kind == FILL:
            base = cmd.to.value // VECTOR_BYTES
            return frozenset((base + v) % LANE_VECTORS for v in range(r[3].value)), I
        ks = None
        if cmd.kind == STORE:
            ks = _store_reach(r)[1]
            if len(ks) < r[7].value:
                return None  # the rows not written last read vectors too
        rows, _ = self._rows(cmd, LANE_MEMORY, ks)
        if cmd.kind == STORE:
            bits, _ = STORE_FORMATS[r[5].value]
            per = -(-(128 // bits if bits else 0) * r[6].value // LANE_COUNT)
        else:
            # A load at r3 0 that ran (`ZERO_LOADS`) spans the vectors a full count fills.
            per = r[3].value or -(-r[5].value * r[6].value // r[2].value)
        starts = [off // VECTOR_BYTES for _, off in rows]
        vectors = frozenset(
            (s + v) % LANE_VECTORS
            for s in starts
            for v in range(min(per, LANE_VECTORS))
        )
        last = max((s + per - 1 for s in starts if per), default=0)
        byte_row = (
            plain
            and r[7].value == 1
            and r[2].value == 0x20
            and r[5].value == 0x10
            and last < LANE_VECTORS  # a wrap is only inferred
        )
        rows_beside = plain and cmd.kind != STORE and _fits(cmd, ROWS_LOAD_BESIDE)
        return vectors, V if byte_row or rows_beside else I

    def finish(self, m: Machine) -> None:
        """The job ends: the commands still outstanding complete (inferred for loads: only a
        store outstanding at the end was measured)."""
        while self.outstanding:
            self._pop(m, min(self.cap, I))

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
        if self._joins_in_group():
            # Measured with one load outstanding: a 0x1d right after a register key, and right
            # after a store's key, waited for the load, and the group went on (verified, one
            # job each).
            self._pop(m, self.cap)
            return
        if self.key is not None:
            if self.key.value & 0x20 == 0:
                m.lapse(index, "x1 0x1d", f"{where}: a 0x1d inside a command's values")
            self._go_off(m, f"{where}: a 0x1d inside a group", lost=True)
            return
        if self.outstanding:
            self._pop(m, self.cap)
        elif not self.commands:
            # Lenient: as if it returned.
            why = "a 0x1d in a job that handed no command: not measured"
            m.lapse(index, "x1 0x1d", why)
        else:
            m.lapse(index, "x1 0x1d", "a 0x1d with no command outstanding never ends")

    def _joins_in_group(self) -> bool:
        if self.key is None or self.group or not self.outstanding:
            return False
        v = self.key.value
        return v & 0x21 == 0x21 or (v & 0x21 == 0 and v & 0x3F == STORE)

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
        plain = cmd.kind in (LOAD, STORE)
        if cmd.kind in KIND_AS:
            shape = tuple(None if r is None else r.value for r in cmd.regs)
            run = next(
                (
                    t
                    for want, t in KIND_SHAPES[cmd.kind].items()
                    if all(
                        w is None or v == w for v, w in zip(shape, want, strict=True)
                    )
                ),
                None,
            )
            if run is None:
                why = f"command kind {cmd.kind:#x} outside the shapes it ran in"
                self._unmeasured(m, cmd, why)
                return
            tier, form = run
            kind = KIND_AS[cmd.kind]
            offs = {STORE: (cmd.frm,), NOTHING_MOVES: (cmd.frm, cmd.to)}.get(
                kind, (cmd.to,)
            )
            for off in offs:
                wrapped = (
                    off.value is not None and off.value >= LANE_VECTORS * VECTOR_BYTES
                )
                if off.value is None or (form == WRAPPED) != wrapped:
                    why = (
                        f"command kind {cmd.kind:#x} with a memory offset "
                        f"not {form} as where it ran"
                    )
                    self._unmeasured(m, cmd, why)
                    return
            if kind == FILL and form == WRAPPED and cmd.to.value % VECTOR_BYTES:
                why = (
                    f"a fill to {cmd.to.value:#x}, not vector-aligned as every fill ran"
                )
                self._unmeasured(m, cmd, why)
                return
            if form == WRAPPED:
                cmd = replace(cmd, wrap_tier=tier)
            self.cap = min(self.cap, tier)
            cmd = replace(cmd, kind=KIND_AS[cmd.kind])
        if cmd.kind not in (LOAD, NOTHING_MOVES, STORE, FILL):
            self._unmeasured(m, cmd, f"command kind {cmd.kind:#x}")
            return
        if len(self.outstanding) >= DEPTH:
            m.lapse(
                cmd.index,
                "stream",
                f"{cmd.where}: more than {DEPTH} commands outstanding: the camera wedged at 48",
            )
        self.commands += 1
        self.outstanding.append(_nothing)
        self.spans.append(None)
        self.drams.append(_Dram())
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
            if cmd.kind == FILL:
                self.outstanding[-1] = self._fill(m, cmd)
            elif cmd.kind == LOAD:
                self.outstanding[-1] = self._load(m, cmd)
            else:
                self.outstanding[-1] = self._store(m, cmd)
            self.spans[-1] = self._span(cmd, plain)
        except _Off as e:
            self._go_off(m, f"{cmd.where} {e}")
            self._spoil(m, cmd)

    def _fill(self, m: Machine, cmd: _Command) -> Callable[[Machine, Confidence], None]:
        """Kind 8: r3 vectors from the one "to" names, every lane "from"'s low 16 bits."""
        if cmd.regs[0].value == LOCAL_MEMORY:
            return self._local_fill(m, cmd)
        value = _known(cmd.frm, "the fill value") & 0xFFFF
        base = _known(cmd.to, "a memory offset") // VECTOR_BYTES
        r3 = _known(cmd.regs[3], "r3")
        tier = min([self.cap, cmd.tier, *(r.tier for r in cmd.regs if r is not None)])
        held: dict[int, Vec] = {}
        for v in range(r3):
            held[(base + v) % LANE_VECTORS] = Vec(UNKNOWN_VEC.lanes)
        if not m.lanes_lost:
            m.memory.update(held)

        def complete(m: Machine, cap: Confidence) -> None:
            if m.lanes_lost:
                return
            for v in range(r3):
                at = (base + v) % LANE_VECTORS
                wrap = cmd.wrap_tier if base + v >= LANE_VECTORS else V
                racing = m.memory.get(at) is not held[at]
                m.memory[at] = (
                    UNKNOWN_VEC
                    if racing
                    else Vec((value,) * LANE_COUNT, min(tier, cap, wrap))
                )

        return complete

    def _local_fill(
        self, m: Machine, cmd: _Command
    ) -> Callable[[Machine, Confidence], None]:
        """Kind 8 under r0 2: r3 local words from "to", each copies of "from"'s low element of
        r2's width (32 / r2 bits)."""
        r = cmd.regs
        r2, r3, r6 = (r[i].value for i in (2, 3, 6))  # the shape matched: known
        to, frm = cmd.to.value, _known(cmd.frm, "the fill value")

        def unmeasured(why: str) -> _Off:
            m.lapse(cmd.index, "stream", f"{cmd.where}: {why}")
            return _Off(why)

        ran_to, tier = LOCAL_FILLS[(r2, r3, r6)]
        if cmd.channel != 0:
            raise unmeasured(f"a local fill on channel {cmd.channel}: not measured")
        if to % 4 or to not in LOCAL_FILL_TO:
            raise unmeasured(f"a local fill to {to:#x}: not measured")
        width = LOCAL_WIDTHS[r2]
        element = frm & (1 << width) - 1
        if element >> (width - 1) or frm >> 15:
            # Every "from" that ran was below 0x8000, its element's top bit clear.
            raise unmeasured(f"a {width}-bit local fill of {frm:#x}: not measured")
        word = sum(element << k for k in range(0, 32, width))
        size = 4 * r3
        tier = min([tier, self.cap, cmd.tier, *(x.tier for x in r)])
        if to != ran_to:
            tier = min(tier, I)
        src = m.boot.source("stream fill in flight", cmd.where)
        m.boot.local.write(LOCAL_BASE + to, Bytes.unknown(size, src))
        filled = m.boot.source(
            "stream fill", "the stream engine's fill of local memory"
        )

        def complete(m: Machine, cap: Confidence) -> None:
            out = Bytes.of(word.to_bytes(4, "little") * r3, filled, min(tier, cap))
            now = m.boot.local.read(LOCAL_BASE + to, size)
            for i in range(size):
                if now.src[i] != src:  # written while the fill ran: raced
                    out.known[i] = 0
            m.boot.local.write(LOCAL_BASE + to, out)

        return complete

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
        self, cmd: _Command, memory: int, ks: range | None = None
    ) -> tuple[list[tuple[int, int]], Confidence]:
        """Each row's (DRAM address, memory offset), the DRAM address floored to 16 bytes, and
        the rows' tier; of the rows `ks` only (default all)."""
        r = cmd.regs
        rows = _known(r[7], "r7")
        if rows < 1:
            raise _Off("no rows: not measured")
        if 16 * rows * _known(r[6], "r6") > MOST_BYTES:
            raise _Off(f"{rows:#x} rows of {r[6].value:#x} lines: more than a job's time moves")
        ran = LANE_STORE_ROWS if cmd.kind == STORE else LANE_LOAD_ROWS
        before = rows in range(1, 3 if cmd.kind == STORE else 5)
        now = memory == LANE_MEMORY and rows in ran and _fits(cmd, ROW_SHAPE)
        tier = V if before or now else I
        dram, mem = (cmd.to, cmd.frm) if cmd.kind == STORE else (cmd.frm, cmd.to)
        dram, mem = _known(dram, "a DRAM address"), _known(mem, "a memory offset")
        pitch, dpitch = 0, 0
        if rows > 1:
            pitch, dpitch = _known(r[1], "r1"), _known(r[4], "r4")
            unit = VECTOR_BYTES if memory == LANE_MEMORY else 4
            half = cmd.kind == LOAD and _fits(cmd, HALF_VECTOR_ROWS)
            if pitch % unit and not (half and mem % VECTOR_BYTES == 0):
                raise _Off(f"memory pitch {pitch:#x}: not measured")
            if dpitch % 16 and cmd.kind == STORE and (dpitch, rows) != (STORE_PITCH_OFF16, 2):
                raise _Off(f"DRAM pitch {dpitch:#x} on a store: not measured")
            if dpitch % 16 and rows > 2:
                # Two rows cannot tell a floored pitch from floored row addresses.
                tier = min(tier, I)
        ks = range(rows) if ks is None else ks
        out = [((dram + k * dpitch) & 0xFFFFFFF0, mem + k * pitch) for k in ks]
        return out, tier

    def _behind(self, rows: list[tuple[int, int]], n: int, store: bool) -> Confidence:
        """The newest command's DRAM rows of `n` bytes each, recorded; inferred where they overlap
        a store handed earlier and still outstanding, and a store's caps an outstanding load of
        those bytes at inferred: the emulator runs them in order, as handed, which was not measured
        for one DRAM range."""
        mine = [(dram_alias(dram), n) for dram, _ in rows]
        tier = V
        for d in self.drams[:-1]:
            if _overlap(d.writes, mine):
                tier = I
            if store and _overlap(d.reads, mine):
                d.cap = I
        if store:
            self.drams[-1].writes = mine
        else:
            self.drams[-1].reads = mine
        return tier

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
        tier = min([tier, self.cap, cmd.tier, self._behind(rows, 16 * r6, False)])
        tier = min([tier, *(x.tier for x in r)])
        if memory == LANE_MEMORY:
            if r2 not in LANE_UNITS:
                raise _Off(f"r2 {r2:#x} on a lane-memory load: not measured")
            per = r2
        elif memory == LOCAL_MEMORY:
            if _fits(cmd, LOCAL_LOAD_NEVER_ENDS):
                why = "a local load of bytes into 32-bit words never ends (inferred)"
                m.lapse(cmd.index, "stream", f"{cmd.where}: {why}")
                raise _Off(why)
            if r2 not in LOCAL_WIDTHS or (bits, LOCAL_WIDTHS[r2]) not in LOCAL_LOADS:
                raise _Off(
                    f"a local load of {bits}-bit elements at r2 {r2:#x}: not measured"
                )
            per = r2
        else:
            raise _Off(f"r0 {memory:#x}: not measured")
        needed = -(-n // per)
        if memory == LANE_MEMORY and r3 and not r6:
            self.cap = min(self.cap, I)  # it ended on the camera (inferred)
            return self._lane_load(m, cmd, rows, bits, 0, r2, r3, tier)
        seen = ZERO_LOADS.get((r2, r5, r6))
        if memory == LANE_MEMORY and not r3 and seen is not None and len(rows) == 1:
            self.cap = min(self.cap, I)  # it ended on the camera (inferred)
            return self._zero_load(m, rows[0][1], seen, needed)
        if not r3 or not r6:
            raise _Off(f"a {_side(memory)} load at r3 {r3}, r6 {r6}: not measured")
        if r3 > needed:
            why = f"a load of {r3} units for {needed} never ends"
            m.lapse(cmd.index, "stream", f"{cmd.where}: {why}")
            raise _Off(why)
        if memory == LANE_MEMORY:
            if r2 != 0x10 or r5 not in ZERO_FORMATS or r3 != needed:
                tier = min(tier, LANE_UNITS[r2])
            return self._lane_load(m, cmd, rows, bits, r6, r2, r3, tier)
        width = LOCAL_WIDTHS[r2]
        tier = min(tier, LOCAL_LOADS[(bits, width)])
        return self._local_load(m, cmd, rows, bits, r6, width, r2, r3, tier)

    def _lane_load(self, m, cmd, rows, bits, lines, r2, r3, tier):
        # Each vector's only (row, v), None for one the load writes twice: the second write
        # sees the first as a race, so it ends unknown and needs no DRAM bytes.
        # Rows at a lane pitch of half a vector (`HALF_VECTOR_ROWS`) share it: the later lands.
        later = _fits(cmd, HALF_VECTOR_ROWS)
        once: dict[int, tuple[int, int] | None] = {}
        for k, (_, off) in enumerate(rows):
            for v in range(min(r3, LANE_VECTORS)):
                at = (off // VECTOR_BYTES + v) % LANE_VECTORS
                twice = (at in once and not later) or v + LANE_VECTORS < r3
                once[at] = None if twice else (k, v)
        held = {at: Vec(UNKNOWN_VEC.lanes) for at in once}
        if not m.lanes_lost:
            m.memory.update(held)
        # The queue runs in order: a store handed later writes DRAM after this load read it.
        wanted: list[list[int]] = [[] for _ in rows]
        for kv in once.values():
            if kv is not None:
                wanted[kv[0]].append(kv[1])
        sources = [
            self._lane_elements(m, dram, bits, lines, vectors)
            for (dram, _), vectors in zip(rows, wanted, strict=True)
        ]

        def complete(m: Machine, cap: Confidence) -> None:
            if m.lanes_lost:
                return
            for at, kv in once.items():
                # What a load of no lines writes was not seen.
                if kv is None or not lines:
                    m.memory[at] = UNKNOWN_VEC
                    continue
                k, v = kv
                base = rows[k][1] // VECTOR_BYTES
                wrap = cmd.wrap_tier if base + v >= LANE_VECTORS else V
                first, elements = sources[k]
                e = v * LANE_COUNT - first
                chunk = elements[e : e + LANE_COUNT]
                chunk += [(0, V)] * (LANE_COUNT - len(chunk))
                if r2 != 0x20:
                    chunk = [(0, V)] * LANE_COUNT  # zeros (verified)
                lanes = tuple(None if x is None else x & 0xFFFF for x, _ in chunk)
                t = min([tier, cap, wrap, *(t for x, t in chunk if x is not None)])
                # A vector written while the load ran raced it: not known.
                racing = m.memory.get(at) is not held[at]
                m.memory[at] = UNKNOWN_VEC if racing else Vec(lanes, t)

        return complete

    @staticmethod
    def _zero_load(m: Machine, off: int, seen: int, needed: int):
        """A lane-memory load at r3 0 (`ZERO_LOADS`): the `seen` vectors from `off` keep their
        content at most inferred, the rest of the `needed` its footprint takes are unknown from
        its hand."""
        base = off // VECTOR_BYTES
        kept = [(base + v) % LANE_VECTORS for v in range(min(seen, needed))]
        unseen = [(base + v) % LANE_VECTORS for v in range(seen, needed)]

        def complete(m: Machine, cap: Confidence) -> None:
            if m.lanes_lost:
                return
            for k in kept:
                vec = m.memory.get(k, UNKNOWN_VEC)
                m.memory[k] = replace(vec, tier=min(vec.tier, I))
            m.memory.update(dict.fromkeys(unseen, UNKNOWN_VEC))

        complete(m, I)
        return complete

    @staticmethod
    def _lane_elements(
        m: Machine, dram: int, bits: int, lines: int, vectors: list[int]
    ) -> tuple[int, list[tuple[int | None, Confidence]]]:
        """The row's elements from DRAM `dram` that `vectors` take, in whole lines of the row's
        `lines`: (index of the first, the elements)."""
        if not vectors:
            return 0, []
        per = 128 // bits
        first = min(vectors) * LANE_COUNT // per
        end = min(lines, -(-(max(vectors) + 1) * LANE_COUNT // per))
        n = max(end - first, 0)
        data = m.boot.dram.read(dram_alias(dram) + 16 * first, 16 * n)
        return first * per, _unpack(data, bits, n)

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
        used = min(lines, -(-r2 * r3 // (128 // bits)))  # the lines its r2 * r3 take
        sources = [
            _unpack(m.boot.read(dram, 16 * used), bits, used) for dram, _ in rows
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
        if memory in NO_MEMORY and _fits(cmd, NO_MEMORY_STORE):
            return _nothing  # it ended and wrote nothing (verified)
        n = (128 // bits if bits else 0) * r6
        _, ks = _store_reach(r)
        rows, tier = self._rows(cmd, memory, ks)
        tier = min([tier, ftier, self.cap, cmd.tier, *(x.tier for x in r)])
        if len(ks) < r[7].value:
            tier = min(tier, I)  # rows that rewrite one address: not measured which lands
        tier = min(tier, self._behind(rows, 16 * r6, True))
        if memory == LANE_MEMORY:
            if r2 != 0x20:
                raise _Off(f"r2 {r2:#x} on a lane-memory store: not measured")
        elif memory == LOCAL_MEMORY:
            if (r2, r5) not in LOCAL_STORES:
                raise _Off(f"a local store at r2 {r2:#x}, r5 {r5:#x}: not measured")
            tier = min(tier, LOCAL_STORES[(r2, r5)])
        else:
            raise _Off(f"r0 {memory:#x}: not measured")
        if memory == LANE_MEMORY and not r3 and n:
            # It ended on the camera, writing nothing (inferred).
            self.cap = min(self.cap, I)
            return _nothing
        zero = (r2, r3, r5) in ZERO_STORES and len(rows) == 1
        if memory == LANE_MEMORY and not r6 and zero:
            self.cap = min(self.cap, I)  # it ended on the camera (inferred)
            self._zero_store(m, rows[0][0], r2 * r3 * bits // 8)
            return _nothing
        if not r3 or not r6:
            raise _Off(f"a {_side(memory)} store at r3 {r3}, r6 {r6}: not measured")
        if r2 * r3 < n:
            why = f"a store of {n} elements from {r2 * r3} never ends"
            m.lapse(cmd.index, "stream", f"{cmd.where}: {why}")
            raise _Off(why)
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
                if not m.lanes_lost:
                    base = off // VECTOR_BYTES
                    self.unwritten += [
                        (cmd.where, (base + v) % LANE_VECTORS)
                        for v, x in enumerate(taken[-1])
                        if x is None
                    ]
                element = _lane_elements(m, off, r5)
            width = 8 * nb if memory == LOCAL_MEMORY else 16
            out = pack(src, bits, 16 * r6, element, tier, width)
            _write_dram(m, dram, out)
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
            # The source was rewritten while the store was queued. Measured on the camera with an
            # X0 store into the store's last vector in the words right after its hand: a store of
            # one vector had taken the old bytes, a store of 0x80 vectors took the new ones into
            # its last vector. So the store keeps the old bytes (inferred) when it reads one vector;
            # with more, a rewritten vector's bytes are unknown.
            many = memory == LANE_MEMORY and sum(len(a) for a in taken) > 1
            for (dram, _), was, new in zip(rows, taken, now, strict=True):
                got = _read_dram(m, dram, 16 * r6)
                for i, owner in enumerate(got.src):
                    if owner == src:
                        got.taint[i] = min(got.taint[i], I)
                if many:
                    per = 16 * r6 // len(was) if 16 * r6 % len(was) == 0 else 16 * r6
                    for v, (x, y) in enumerate(zip(was, new, strict=True)):
                        if x is not y:
                            for i in range(v * per, min((v + 1) * per, 16 * r6)):
                                got.known[i] = 0
                _write_dram(m, dram, got)

        return complete

    @staticmethod
    def _zero_store(m: Machine, dram: int, n: int) -> None:
        """A lane-memory store at r6 0 (`ZERO_STORES`): the `n` DRAM bytes from `dram` a full
        count would write keep their content, at most inferred."""
        got = _read_dram(m, dram, n)
        got.taint[:] = bytes(min(t, I) for t in got.taint)
        _write_dram(m, dram, got)

    # ------------------------------------------------------------------ off script

    def _unmeasured(self, m: Machine, cmd: _Command, why: str) -> None:
        """A command of a kind or shape not run (`Machine.lapse`): lenient, it counts as a
        command, the engine goes off script, and since what such a command reaches is not known,
        every memory is forgotten."""
        m.lapse(cmd.index, "stream", f"{cmd.where}: {why}")
        self.commands += 1
        self.outstanding.append(_nothing)
        self.spans.append(None)
        self.drams.append(_Dram())
        self._go_off(m, f"{cmd.where} {why}")
        m.lose_memories(f"{cmd.where} {why}")

    def _spoil(self, m: Machine, cmd: _Command) -> None:
        """What a command the engine cannot run writes is not known: its destination becomes
        unknown, and a destination that cannot be bounded lapses (`Machine.lapse`)."""
        try:
            self._spoil_reach(m, cmd)
        except _Off as e:
            why = f"{cmd.where} off script: {e}"
            m.lapse(cmd.index, "stream", why)
            m.lose_memories(why)

    def _spoil_reach(self, m: Machine, cmd: _Command) -> None:
        lost = m.boot.source("stream off script", self.off)
        r = cmd.regs
        rows = _known(r[7], "r7")
        if rows < 1:
            raise _Off("no rows")
        if cmd.kind == STORE:
            lines, ks = _store_reach(r)
            pitch = _known(r[4], "r4") if rows > 1 else 0
            at = _known(cmd.to, "a DRAM address")
            for k in ks:
                dram = (at + k * pitch) & ~0xF
                _write_dram(m, dram, Bytes.unknown(16 * lines, lost))
            return
        memory = _known(r[0], "r0")
        if memory == LANE_MEMORY:
            return  # the lane memory is lost already
        if memory != LOCAL_MEMORY:
            raise _Off(f"r0 {memory:#x}")
        size = 4 * _known(r[3], "r3")
        pitch = _known(r[1], "r1") if rows > 1 else 0
        at = _known(cmd.to, "a memory offset")
        # Offsets do not wrap: a last row past the end is not measured, and at pitch 0 the rows
        # repeat the first.
        self._local_offset(at + (rows - 1) * pitch, size)
        for k in range(rows if pitch else 1):
            off = self._local_offset(at + k * pitch, size)
            m.boot.local.write(LOCAL_BASE + off, Bytes.unknown(size, lost))

    def _spoil_stores(self, m: Machine) -> None:
        lost = m.boot.source("stream off script", self.off)
        for dram, n in self.stored:
            _write_dram(m, dram, Bytes.unknown(n, lost))


@dataclass
class _Dram:
    """The DRAM rows (alias address, length) an outstanding command reads and writes, and the cap
    on what it lands (`Engine._behind`)."""

    reads: list[tuple[int, int]] = field(default_factory=list)
    writes: list[tuple[int, int]] = field(default_factory=list)
    cap: Confidence = V


def _fits(cmd: _Command, shape: tuple[int | None, ...]) -> bool:
    """The command's registers are `shape`'s (None: any)."""
    return all(
        w is None or (v is not None and v.value == w)
        for v, w in zip(cmd.regs, shape, strict=True)
    )


def _overlap(xs: list[tuple[int, int]], ys: list[tuple[int, int]]) -> bool:
    return any(a < b + m and b < a + n for a, n in xs for b, m in ys)


def _store_reach(r: list[Sv | None]) -> tuple[int, range]:
    """A store's r6, its 16-byte lines per row, and the rows that write DRAM last: a row the
    pitch r4 brings back to an earlier row's address (modulo the DRAM alias space) rewrites it.
    Rows that write more than the DRAM alias space are not bounded."""
    lines, rows = _known(r[6], "r6"), _known(r[7], "r7")
    pitch = _known(r[4], "r4") % DRAM_BYTES if rows > 1 else 0
    distinct = min(rows, DRAM_BYTES // gcd(pitch, DRAM_BYTES))
    if 16 * lines * distinct > DRAM_BYTES:
        raise _Off(f"{distinct} rows of {16 * lines:#x} bytes: more than all of DRAM")
    return lines, range(rows - distinct, rows)


def _write_dram(m: Machine, dram: int, data: Bytes) -> None:
    """Write a store row at `dram`, wrapping at the top of the DRAM alias space."""
    at = dram_alias(dram)
    head = DRAM_BYTES - at
    if len(data) <= head:
        m.boot.dram.write(at, data)
        return
    m.boot.dram.write(at, data[:head])
    m.boot.dram.write(0, data[head:])


def _read_dram(m: Machine, dram: int, n: int) -> Bytes:
    at = dram_alias(dram)
    head = DRAM_BYTES - at
    if n <= head:
        return m.boot.dram.read(at, n)
    return m.boot.dram.read(at, head) + m.boot.dram.read(0, n - head)


def _side(memory: int) -> str:
    return "lane-memory" if memory == LANE_MEMORY else "local"


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
