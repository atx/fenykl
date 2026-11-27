"""Program 16, the stock copy (command 0xF): its model, built from the stock program at runtime.

The driver passes src, dst and len in DSP local memory words 0..2. No word of the program is
decoded; what is known comes from camera runs that replaced a word by the idle word ("idled") or
swept one of its fields (the register field 115..119, word 60's f76_79), and is stated here by word
index:

- `_PARTS`: what the job does without each undecoded stock word;
- `_CHANNELS`: the register-transfer words, grouped by their register context, and the role of
  each slot (load address, offsets, count, store format, ...);
- `_FORMATS`: word 47's store formats by register.

`Copy16Model` takes the stock program (from the user's firmware, `from_image`) and derives what
covers a job's words from it: each undecoded stock word is the row of its stock index, the
register transfers are X1's semantic at any index, matched by their signature, with the
register contexts and the register group B and route values of the stock transfers as the
reference. A job with words inserted into the stock program is covered by content (`align`,
`Copy16Model.cover_job`). No stock word is part of this code. The model resolves a job's trace
into its one load and one store (`resolve`) and applies
them to the boot (`apply`):

    load:   buffer[i + o68] = S[i] for i in [0, len), S read at word 67's address
    store:  dst[i] = format(buffer)[i + o76] for i in [0, n73); dst[n73, len) not written

The store is the whole command's: dst's 16-byte-aligned middle from the DSP job and the head
and tail bytes outside it from the command's CPU wrapper (`commands`), as the camera runs, which
went through the wrapper, read dst back. A job alone (`machine.Machine`) stores only the middle.

A job that does not complete stores nothing, unless its outcome says that dst is written first
(`Outcome.STORE_TIMEOUT`). A placement out of range, a register of unknown value or a load that
may or may not have run makes its bytes UNKNOWN.

The copy also carries own kernels in one job (verified on the camera, pre-registered; this model
does not run them, `machine.Machine` with `stream.Engine` does): its load is complete once word 74 has run (three vectors read, 8 lanes
each, 0, 1 and 0x1ff, held the source bytes between 74 and 75 at len 0x4000:
word 74 is the load's 0x1d, which waits for it), own words between 74 and 75
read and write the lane memory before the store (a dense layer of 32 neurons x 64 inputs and a
16-bit fixed-point kernel came out exact, DRAM to DRAM), and words 0..78 followed by idles and a
halt copy as the whole program does: word 79, the return, is not needed.

The guard 45 skips the loop 50..57 in every job tested, so what is known of 50..57 alone was seen
with the loop skipped: an edit of 28 or 45 together with one of 50..57 has the outcome of
`_LOOP_ENTRIES`, where measured, and no semantics otherwise.

Hazards: idling word 62 lost the camera's link and idling 64 or 72 failed the job with status
-5; these register values lost the link too: word 44 at 0, 1, 4, 8, 10; word 47 at 16, 18, 19,
20, 24, 26, 27, 28; word 70 at 26; word 71 at 0.
"""

from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

from ..emu import HALT_OP, Boot, Executor, Job, Stop, dram_alias
from ..fields import (
    DELAY_SLOTS,
    FIELDS,
    OP_FIELD,
    STAIRCASE,
    TARGET,
    Confidence,
    Kind,
    lookup,
    op,
)
from ..job import Flow
from ..machine import Machine
from ..program import Program, stock_programs
from ..semantics import OP_SEMANTICS, Given, OpSemantics, UnknownSemantics
from ..state import Bytes
from ..stream import Engine
from ..units import UNITS
from ..word import IDLE, Word
from .intervals import STORES, Effect, RegSet, destinations, op_effect
from .model import (
    BUFFER_HI,
    KIB,
    Channel,
    JobResult,
    ModelRun,
    OpaqueClass,
    Outcome,
    Part,
    Role,
    Status,
    StockBoot,
    Trace,
    Transfer,
    Use,
    WordClass,
    assign_roles,
)
from .registers import READOUT, JobView, Reg, RegisterFile

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

ITEM = 16
ENGINE_AFTER = 64
"""Inserted data ops right after this stock word (before 65) are the measured site: their
operands and their results through the PR ports are modelled there only."""
XREG_DISTANCE = 4
"""The shortest distance in executed words from an inserted word's xreg-set write to the stock
transfer that read it, seen working in the copy on the camera; nearer is not measured."""
ENGINE_OPERANDS = (
    (
        2,
        lambda length: ((length + 0x1F) // READOUT,) * 2,
        (
            "(len + 0x10..0x1f) / 16, the one integer there (inferred): the favoured model of "
            "the count, floored to 16 B, needs r2 at least (len + 0x10) / 16, which also "
            "explains a full copy then a timeout at 0x3f imm 0; r2 = len / 16 with counts "
            "rounded up is not excluded"
        ),
    ),
    (
        7,
        lambda length: (0x4001, 0x4041) if length in (0x2000, 0x4000) else None,
        (
            "a value near 0x4000, read through ra - rb at two lengths, 0x2000 and 0x4000; "
            "not what word 64 writes to the xreg's r7, len / 2 as the transfers use it "
            "(inferred); at shorter "
            "lengths it lay further above (unknown there)"
        ),
    ),
)
"""The operand set's registers at the engine site (inferred, on the camera), by job length, in
the units the ops compute in."""
MAX_COUNT = 0x40000
"""A count above this is an address or junk: the store never ends (hypothesis)."""
ZERO_FILL = 0x4000

# -- the undecoded stock words

_STORE_PATH = "idled, dst is not written (prefill intact)"
_COMPLETION = "idled, the job times out"
_END = "idled, dst is written (a copy), then the job times out"
_NONE = "no effect seen when idled, also with len, dst and src changing between jobs"


def _store_path(token: str = "store path") -> Part:
    return Part(token, Outcome.NO_STORE, _STORE_PATH)


_PARTS: dict[int, Part] = {
    0: _store_path("prologue store path"),
    5: Part("prologue completion", Outcome.STORE_TIMEOUT, _END),
    15: Part(
        "store count and data",
        Outcome.ZERO_FILL,
        "idled, the job writes 0x4000 zero bytes from dst at 1 and 4 KiB, whatever the length",
        seen=(KIB, 4 * KIB),
    ),
    27: Part(
        "load address (hypothesis)",
        Outcome.DATA_UNKNOWN,
        "idled, dst gets foreign data; its target moved from 79 to 32, its own fall-through, "
        "copies: the target bits have no other effect",
        flips={"imm": {79 ^ 32: Outcome.OK}},
    ),
    28: Part(
        "completion",
        Outcome.STORE_TIMEOUT,
        f"{_END}, also with 53 idled; whether it is needed for the end in its own right or "
        "makes 45 fall through into 50..57 is open",
    ),
    31: Part(
        "store-side buffer address (hypothesis)",
        Outcome.DATA_UNKNOWN,
        "idled, dst gets data of an older job, 16 bytes off",
    ),
    45: Part(
        "guard",
        Outcome.TIMEOUT,
        f"{_COMPLETION} with dst not written; its register field is don't-care (copies at all "
        "31 values); its target moved from 58 to 50, the loop's top: dst is written, then the "
        "job times out (the target is live, so 45 is taken in a copy); moved to 51: the job "
        "times out with dst not written (whether 51 is reached as an index is open)",
        operand={r: Outcome.OK for r in range(31)},
        flips={"imm": {58 ^ 50: Outcome.STORE_TIMEOUT, 58 ^ 51: Outcome.TIMEOUT}},
    ),
    58: Part(
        "fixed-content load address",
        Outcome.DATA_UNKNOWN,
        "idled, dst gets a fixed 1 KiB",
    ),
    60: Part(
        "stream completion",
        Outcome.TIMEOUT,
        f"{_COMPLETION}; its f76_79: a copy at 0 only, the job times out before any store at "
        "each of 1..15, so every bit matters",
        operand={v: Outcome.OK if v == 0 else Outcome.TIMEOUT for v in range(16)},
        operand_field="f76_79",
    ),
    61: Part(
        "src loader",
        Outcome.DATA_UNKNOWN,
        "idled, the job emits data held from an older 4 KiB job, then 0x5A",
    ),
    62: Part("link keeper", Outcome.HAZARD, "idled, the camera lost its link"),
    63: Part("stream completion", Outcome.TIMEOUT, _COMPLETION),
    64: Part("wedge guard", Outcome.HAZARD, "idled, the job wedged (status -5)"),
    65: Part(
        "stream completion",
        Outcome.TIMEOUT,
        f"{_COMPLETION}; bit 6 of its immediate cleared: dst is written, then the job times "
        "out, so its constant is consumed on the end side, not by the store's count, data or "
        "address (inferred)",
        flips={"imm": {0x40: Outcome.STORE_TIMEOUT}},
    ),
    69: Part(
        "store side",
        Outcome.NO_STORE,
        f"{_STORE_PATH}; its register: dst not written at r0 and r4, a copy at the others",
        operand={r: Outcome.NO_STORE if r in (0, 4) else Outcome.OK for r in range(31)},
    ),
    79: Part("stream end", Outcome.STORE_TIMEOUT, _END),
    **{i: _store_path() for i in (12, 13, 14, 16, 17, 21, 22, 23, 24, *range(32, 42))},
    **{
        i: Part("no effect seen", Outcome.OK, _NONE)
        for i in (1, 2, 10, 11, 18, 19, 20, 25, 26, 29, 30, 50, 51, 52, 53, 56, 59, 80)
    },
    **{i: Part("return delay slot", Outcome.OK, _NONE) for i in (81, 82, 83)},
}

# -- the register transfers

ROUTES = tuple(f.id for f in FIELDS if f.lo in STAIRCASE)
CONTEXT = ("f100_104", "f105_109", "f110_114", "f123_127", "f128_132")
"""The register group A fields besides xreg: they select the transfer's channel."""
# f221_223, R's register, stays idle in a transfer.
REGS_B = tuple(
    f.id for f in UNITS["pr"].fields if f.kind == "reg" and f.id != "f221_223"
)
TRANSFER_OPERANDS = frozenset({*CONTEXT, "f115_119", *REGS_B, *ROUTES, "f308_312"})
COUNTED, HOLDING = 0x1C, 0x1D
"""The X1 heads of the counted and the holding register transfers."""

T_SET = (7, 9, 16, 18, 19, 20, 24, 27, 28, 30)
Z_SET = (4, 5, 10, 12, 13)


def _table(default: Outcome | None, **sets: tuple[int, ...]) -> dict[int, Outcome]:
    """Outcome by register 0..30: the named sets, `default` for the rest (None: none)."""
    out = {r: default for r in range(31)} if default else {}
    for name, regs in sets.items():
        for r in regs:
            out[r] = Outcome[name.upper()]
    return out


def _except(table: dict[int, Outcome], *regs: int) -> dict[int, Outcome]:
    return {r: o for r, o in table.items() if r not in regs}


DONT_CARE = _table(Outcome.OK)

_CHANNELS: tuple[Channel, ...] = (
    Channel(
        "completion A",
        42,
        (
            Role(
                "store enable",
                42,
                "copies at r0, r4, r8, r27; dst not written at the rest",
                _table(Outcome.NO_STORE, ok=(0, 4, 8, 27)),
            ),
            Role("completion", 43, "don't-care: copies at all 31 values", DONT_CARE),
            Role(
                "completion (hazard)",
                44,
                "dst not written at r3, r5, r7, timeout at r6; the camera lost its link at r0, "
                "r1, r4, r8, r10 (not registered)",
                _table(None, ok=(9,), no_store=(3, 5, 7), timeout=(6,)),
            ),
        ),
    ),
    Channel(
        "completion B",
        46,
        (
            Role("completion", 46, "don't-care: copies at all 31 values", DONT_CARE),
            Role(
                "store format",
                47,
                "the store's element format, applied by the store, not the load: the number of "
                "elements per 16-byte line (`_FORMATS`); bytes at r6, u8 -> u32 zero-extended "
                "at r14 and r29, a 10-bit pack or zeros at r21 (by boot), zeros at the others; "
                "timeout at r9; the camera lost its link at r16, r18, r19, r20, r24, r26, r27, "
                "r28 (not registered)",
                _table(None, timeout=(9,)),
                use=Use.FORMAT,
            ),
            Role("completion", 48, "don't-care: copies at all 31 values", DONT_CARE),
            Role(
                "completion",
                49,
                "copies at 16 values, dst not written at r10, r12, r13, timeout at 12",
                _table(
                    Outcome.OK,
                    no_store=(10, 12, 13),
                    timeout=(3, 14, 15, 17, 19, 21, 22, 23, 24, 25, 26, 29),
                ),
            ),
        ),
    ),
    Channel(
        "spare A",
        54,
        (
            Role("unused", 54, "no effect seen when idled", DONT_CARE),
            Role("unused", 55, "no effect seen when idled", DONT_CARE),
        ),
        needed=False,
    ),
    Channel(
        "stream",
        66,
        (
            Role(
                "load enable",
                66,
                "a load at r1, r3, r4, r5, r10, r12, r13, r17; no load (the held buffer is "
                "stored) at 11 values; timeout at the T set; a fixed 1 KiB at r8; on register 0 "
                "(which holds 1 at that word) the copy skips its load and stores the lane "
                "memory as it is "
                "(verified on the camera, a pre-registered pipeline)",
                _table(
                    None,
                    ok=(1, 3, 4, 5, 10, 12, 13, 17),
                    no_load=(0, 2, 6, 11, 14, 15, 21, 22, 23, 25, 29),
                    timeout=(*T_SET, 26),
                    data_unknown=(8,),
                ),
                load=True,
            ),
            Role(
                "load address",
                67,
                "an absolute address at r3 (src), r21 and r29 (firmware text); low registers "
                "read a fixed memory at value / 16; dst not written at r2 and r15",
                _table(None, no_store=(2, 15)),
                use=Use.ADDRESS,
                load=True,
            ),
            Role(
                "load offset",
                68,
                "the buffer index of the load: buffer[i + o] = S[i], o = value / 32 truncated "
                "to 32 bytes (`READOUT`, verified); the address counts modulo the lane memory "
                "(inferred), which the model does not wrap: it truncates a negative value "
                "towards zero (hypothesis)",
                use=Use.LOAD_OFFSET,
                load=True,
            ),
            Role(
                "stream setup",
                70,
                "copies at r3, r8; timeout at the T set; dst not written at the rest",
                _except(_table(Outcome.NO_STORE, ok=(3, 8), timeout=T_SET), 26),
            ),
            Role(
                "stream",
                71,
                "copies at most values; dst not written at the Z set; r0 lost the link",
                _except(_table(Outcome.OK, no_store=Z_SET), 0),
            ),
            Role(
                "stream",
                72,
                "only its stock r8 copies; timeout or dst not written elsewhere; -5 at r1",
                _except(
                    _table(
                        Outcome.NO_STORE,
                        ok=(8,),
                        timeout=(3, 6, 7, 9, 11, 15, 16, 18, 19, 20, 24, 27, 28, 30),
                    ),
                    1,
                ),
            ),
            Role(
                "store count",
                73,
                "the store's count in output bytes: dst[n, len) is not written; it does not "
                "limit the load; timeout at registers holding addresses or unknown values",
                _table(None, timeout=(2, 6, 8, 11, 14, *range(15, 31))),
                use=Use.COUNT,
            ),
            Role(
                "stream",
                75,
                "copies at r6 and r11; timeout at the T set, r3, r8, r15; dst not written "
                "elsewhere",
                _table(Outcome.NO_STORE, ok=(6, 11), timeout=(*T_SET, 3, 8, 15)),
            ),
            Role(
                "store offset",
                76,
                "the buffer index of the store: dst[i] = buffer[i + o], o = value / 32 rounded "
                "down to 32 bytes (`READOUT`, verified); the address counts modulo the lane "
                "memory (inferred), which the model does not wrap: it rounds a negative value "
                "down (hypothesis)",
                use=Use.STORE_OFFSET,
                beyond=H,  # its other registers ran only at 1 KiB
            ),
            Role(
                "stream",
                77,
                "only its stock r2 copies; dst not written at every other value",
                _table(Outcome.NO_STORE, ok=(2,)),
            ),
        ),
        stalls=(78,),
    ),
)
"""Channels cmd 0xF does not need (`needed=False`): idling their transfers has no effect, with
len, dst and src changing between jobs. Word 57 is a holding transfer (head 0x1d) of its own
register context and takes no slot.

What the transfers hand, read from the B file after each prefix of the program at one length
(measured on the camera; the reading inferred): words 32 and 41 a header of the load and of the
store side, the two apart in bit 6; 33..40 and 42..49 a group of eight values each, the same
for both sides, len / 2, the element format and len among them; 59, 63..65 and 70..73 two
(key, value) pairs of the load and of the store side, keys apart in bit 6, the second pair's
value len / 16; 66..68 the load's command, source address and lane memory placement; 75..77
the store's command, lane memory offset and destination address. A transfer
reads its register before the same word's port writes it (inferred). So 47 is the store's
element format and 38 by position the load's (hypothesis): 38 on a register holding 8, alone
or with 47 on 8 too, never ended (measured on the camera, one run each)."""

# Word 47's formats by register; r21's is a boot parameter. The format is the register's value,
# the number of elements per 16-byte line (4, 8, 9, 10 and 12 each verified on the camera against
# alternatives written before the run, 1024..2048 16-bit elements each): 0x10, the stock r6's,
# each element's low byte; 0x0c each line 12 elements' low 10 bits, packed (the bit order, least
# significant first, the last byte 0, fitted after the run: inferred); 0x0a each line 10
# elements' low 12 bits, so packed; 0x09 each line 9 elements' low 14 bits, least significant
# first, the top 2 bits 0; 0x08 each element as 16 bits little-endian; 0x04 (and 0x44,
# measured) each element zero-extended to a 32-bit word; 0x0d is no line count: the store
# writes zeros over the whole length (verified on the camera, pre-registered against a 13 x 9-bit
# pack); 0x14 and 0x18 never complete (the job times out; that they have no line is inferred).
# 16-bit lanes also leave as bytes, interleaved by S3 0xa / 0xb with a shift by 8 (verified on
# the camera). The model has the formats of the registers above.

_FORMATS: dict[int, str] = {
    6: "bytes",
    14: "u32",
    29: "u32",
    **{r: "zero" for r in (0, 1, 2, 3, 4, 5, 7, 8, 10, 11, 12, 13, 15, 17, 22, 23, 25)},
}


def context(word: Word) -> tuple[int, ...]:
    return tuple(word[k] for k in CONTEXT)


@dataclass(frozen=True)
class Reference:
    """The register group B and route values of the stock transfers of one register context."""

    regs_b: dict[str, frozenset[int]]
    routes: dict[str, frozenset[int]]


@dataclass(frozen=True, eq=False)
class TransferSemantic(WordClass):
    """The register transfers at any index, X1 heads 0x1c and 0x1d: they hand the register
    named in 115..119 to the sequencer."""

    channel_of: dict[tuple[int, ...], str] = field(default_factory=dict)
    reference: dict[tuple[int, ...], Reference] = field(default_factory=dict)
    holding: frozenset[int] = frozenset()
    """The indices of the stock holding transfers (head 0x1d)."""

    def matches(self, word: Word) -> bool:
        return super().matches(word) and word["f308_312"] in (COUNTED, HOLDING)

    def act(self, trace: Trace, word: Word, index: int, strict: bool) -> None:
        ctx = context(word)
        ref = self.reference.get(ctx)
        if ref is None:
            raise UnknownSemantics(
                index,
                self.name,
                [(lookup(k), word[k]) for k in CONTEXT],
                "no stock transfer has this register context",
            )
        routes = [(lookup(k), word[k]) for k in ROUTES if word[k] not in ref.routes[k]]
        if routes:
            if strict:
                raise UnknownSemantics(
                    index,
                    self.name,
                    routes,
                    "route nibbles off the stock transfers' values",
                )
            trace.ignored.append((index, [f.id for f, _ in routes]))
            trace.taint = H
        if word["f308_312"] == HOLDING:
            # 74 and 78 differ from 73 and 77 only here and copy at every register (hypothesis:
            # a transfer that does not take a slot).
            trace.notes.append(f"word {index}: a holding transfer, no slot")
            if index not in self.holding:
                trace.taint = H
            return
        channel = self.channel_of.get(ctx)
        if channel is None:
            raise UnknownSemantics(
                index,
                self.name,
                [(lookup(k), word[k]) for k in CONTEXT],
                "no channel has this register context",
            )
        off = [k for k in REGS_B if word[k] not in ref.regs_b[k]]
        # Single-bit flips of 206..215 in words 67 and 68 left the copy unchanged [verified];
        # other values elsewhere are assumed ignored too, and taint.
        taint = H if off else V
        trace.channels.setdefault(channel, []).append(
            Transfer(index, word["xreg"], taint)
        )


def _transfer_semantic(words: tuple[Word, ...]) -> TransferSemantic:
    """The transfer semantic, with the contexts and reference values of the stock transfers."""
    cls = TransferSemantic(
        "transfer",
        "register transfer: hands the register named in 115..119 to the stream "
        "configuration; its role (address, offset, count, enable, format) is given by the "
        "sequencer, by transfer order [inferred, strong for moves within 73..77; a counted "
        "transfer at 78 stalls the job, see `assign_roles`]",
        {f.id: f.idle for f in FIELDS if f.id not in TRANSFER_OPERANDS},
        TRANSFER_OPERANDS,
    )
    stock = [w for w in words if cls.matches(w)]
    reference: dict[tuple[int, ...], Reference] = {}
    for ctx in dict.fromkeys(context(w) for w in stock):
        mine = [w for w in stock if context(w) == ctx]
        reference[ctx] = Reference(
            {k: frozenset(w[k] for w in mine) for k in REGS_B},
            {k: frozenset(w[k] for w in mine) for k in ROUTES},
        )
    channel_of = {context(words[c.anchor]): c.name for c in _CHANNELS}
    for c in _CHANNELS:
        for role in c.roles:
            w = words[role.word]
            if (
                not cls.matches(w)
                or w["f308_312"] != COUNTED
                or context(w) != context(words[c.anchor])
            ):
                raise ValueError(
                    f"stock word {role.word} is no counted transfer of {c.name}"
                )
    roles = {r.word for c in _CHANNELS for r in c.roles}
    for i, w in enumerate(words):
        if cls.matches(w) and w["f308_312"] == COUNTED and i not in roles:
            raise ValueError(f"stock transfer {i} has no role")
    holding = frozenset(
        i for i, w in enumerate(words) if cls.matches(w) and w["f308_312"] == HOLDING
    )
    return TransferSemantic(
        cls.name,
        cls.fact,
        cls.match,
        cls.operands,
        confidence=I,
        channel_of=channel_of,
        reference=reference,
        holding=holding,
    )


def _opaque_classes(
    words: tuple[Word, ...], transfer: TransferSemantic
) -> list[OpaqueClass]:
    """One class per distinct undecoded stock word, matched by its full value."""
    positions: dict[int, list[int]] = {}
    for index, word in enumerate(words):
        if word == IDLE or transfer.matches(word):
            continue
        if index not in _PARTS:
            raise ValueError(f"stock word {index} has no part")
        positions.setdefault(word.value, []).append(index)
    classes = []
    for at in positions.values():
        part = _PARTS[at[0]]
        if any(_PARTS[i] != part for i in at):
            raise ValueError(f"stock words {at} are equal but have different parts")
        operands = frozenset(
            lookup(k).id
            for k in (*((part.operand_field,) if part.operand else ()), *part.flips)
        )
        word = words[at[0]]
        match = {f.id: f.get(word.value) for f in FIELDS if f.id not in operands}
        classes.append(
            OpaqueClass(
                part.token,
                part.fact,
                match,
                operands,
                confidence=I,
                part=part,
                positions=frozenset(at),
            )
        )
    extra = set(_PARTS) - {i for at in positions.values() for i in at}
    if extra:
        raise ValueError(f"parts without a stock word: {sorted(extra)}")
    return classes


STREAM = range(58, 80)
"""The stream words: a halt before them stores nothing."""
HALTS_SEEN = (11,)
"""The indices stock word 5, the halt, ran at on the camera besides its own (in place of 11)."""

GUARD = 45
LOOP = range(50, 58)
"""The loop the guard 45 skips in every tested job."""
LOOP_GROUP = frozenset({28, GUARD, *LOOP})


def _edit(stock: Word, word: Word) -> str:
    """An edit of a stock word: "idle", or the fields it flips against the stock value."""
    if word == IDLE:
        return "idle"
    return ", ".join(
        f"{f.label} ^ {f.get(word.value) ^ f.get(stock.value):#x}"
        for f in FIELDS
        if f.get(word.value) != f.get(stock.value)
    )


def _key(word: Word) -> int:
    """The word's value with its control target cleared, as the alignment compares words."""
    return word.value if word.target is None else TARGET.put(word.value, 0)


def align(stock: tuple[Word, ...], words: tuple[Word, ...]) -> list[int | None] | None:
    """The stock index of each word of a longer program that is the stock program with words
    inserted and some edited in place (None at an inserted word), matched by content with the
    control targets left out; None for any other program."""
    if len(words) <= len(stock):
        return None
    matcher = SequenceMatcher(
        None, [_key(w) for w in stock], [_key(w) for w in words], autojunk=False
    )
    at: list[int | None] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            at += [None] * (j2 - j1)
        elif i2 - i1 != j2 - j1:
            return None
        else:
            at += range(i1, i2)
    # An insert into a run of equal words is the same program wherever in the run it is: put
    # it after the run, so that it lies in delay slots only if every place in the run does.
    for j in range(len(at) - 2, -1, -1):
        k = j
        while (
            k + 1 < len(at)
            and at[k] is None
            and at[k + 1] is not None
            and words[k] == words[k + 1]
        ):
            at[k], at[k + 1] = at[k + 1], None
            k += 1
    return at


_LOOP_ENTRIES: dict[frozenset[tuple[int, str]], tuple[Outcome, str]] = {
    frozenset({(GUARD, "idle"), (53, "idle")}): (
        Outcome.NO_STORE,
        "45 and 53 idled: the job ends through the done path, so idle 45's timeout needs 53",
    ),
    frozenset({(GUARD, f"imm ^ {58 ^ 50:#x}"), (53, "idle")}): (
        Outcome.STORE_TIMEOUT,
        "45's target moved to 50 and 53 idled: as with 53 stock, 53 not its cause",
    ),
    frozenset({(28, "idle"), (53, "idle")}): (
        Outcome.STORE_TIMEOUT,
        "28 and 53 idled: as idle 28 alone, so its timeout does not need 53",
    ),
}
"""The measured edits of 28 or 45 with words of the loop (verified on the camera at 1 KiB)."""

# -- resolution


@dataclass
class Plan:
    """The resolved job: outcomes, the parameters of the load and the store, and taints."""

    outcomes: list[tuple[Outcome, str, Confidence]] = field(default_factory=list)
    """(outcome, why, confidence) of the parts and roles that decided something."""
    cap: Confidence = V
    """Hypothesis for ignored fields and words off their stock position; at most inferred for
    two or more edited words."""
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
        return any(x is o for x, _, _ in self.outcomes)

    def deciding(self) -> list[tuple[Outcome, str, Confidence]]:
        return [x for x in self.outcomes if x[0] is not Outcome.OK]

    def result_taint(self, stores: bool) -> Confidence:
        """The confidence of what this job did: its status and whether it stored, from the
        outcomes that decided them; when it stored, also the store (and the load, when nothing
        decided). Stored bytes can still be weaker through the buffer they come from."""
        deciding = [c for _, _, c in self.deciding()]
        if not deciding:
            return min(self.load_taint, self.store_taint)
        if stores:
            deciding.append(self.store_taint)
        return min(self.cap, *deciding)


def start_copy(boot: StockBoot) -> JobView:
    """Set the transfer registers that the copy's register facts give for the request in local
    memory (src, dst and len at words 0..2); returns that view of the job."""
    view = JobView(boot.u32(0), boot.u32(4), boot.u32(8), boot.params)
    if view.dst is None or view.length is None:
        raise ValueError("local memory holds no dst and len (no command setup ran)")
    boot.regs.start_job(view)
    return view


def _readout(value: Reg | None) -> Reg | None:
    """An op's result as the copy's transfers use it (`READOUT`)."""
    if value is None:
        return None
    fact = f"{READOUT} x ({value.fact})"
    return Reg(READOUT * value.lo, READOUT * value.hi, value.taint, fact)


def _trunc32(v: int) -> int:
    """v / 32 truncated towards zero, to a multiple of 32."""
    q = abs(v) // 32 & ~0x1F
    return q if v >= 0 else -q


def _floor32(v: int) -> int:
    """v / 32 rounded down, to a multiple of 32."""
    return v // 32 & ~0x1F


@dataclass
class Operands16:
    """The operand set as inserted words at the engine site see it: `ENGINE_OPERANDS` and
    their own writes, each read from the next word on."""

    regs: RegisterFile = field(default_factory=RegisterFile)
    seeded: bool = False

    def seed(self, length: int) -> None:
        for r, value, fact in ENGINE_OPERANDS:
            v = value(length)
            self.regs[r] = None if v is None else Reg(v[0], v[1], I, fact)
        self.seeded = True


class Copy16Model:
    """The model of program 16, built from the stock program (`from_image` reads it from a
    stock E-M10 II `.BIN`). `ops` gives the inserted words' op semantics."""

    def __init__(
        self, program: Program, ops: dict[int, OpSemantics] = OP_SEMANTICS
    ) -> None:
        self.program = program
        self.ops = ops
        words = program.words
        self.transfer = _transfer_semantic(words)
        self.opaque = _opaque_classes(words, self.transfer)
        self.rows = {i: c for c in self.opaque for i in c.positions}
        """The row of each undecoded stock word, by its stock index."""
        self.halt = next(w for w in words if w.op == HALT_OP)
        self.late_writes = {
            r
            for w in words[ENGINE_AFTER + 1 : STREAM.stop]
            if not w.control
            for to, r in destinations(w)[0]
            if to is RegSet.XREG
        }
        """The xreg-set registers that stock words after the engine site write."""
        self.channels = _CHANNELS
        self.formats = _FORMATS

    @classmethod
    def from_image(cls, image: bytes) -> "Copy16Model":
        return cls(stock_programs(image)[ITEM])

    def cover_job(self, run: ModelRun, job: Job, trace: Trace) -> Job:
        """Cover the job's words and return it in the stock program's index space. A job that is
        the stock program with words inserted (`align`) has its stock words covered at their
        stock indices, their control targets moved back through the alignment, and its inserted
        words by their content (`_cover_insert`); it returns without its inserted words, so the
        indices of what it resolves are stock indices. Words off their stock indices make it a
        hypothesis. An insert in a control word's delay slots, a target on an inserted word and
        an inserted control or halt word raise UnknownSemantics, also in lenient mode."""
        words = job.program.words
        at = align(self.program.words, words)
        if at is None:
            for index, word in enumerate(words):
                self.cover(trace, index, word, run.strict)
            return job
        trace.inserted = [j for j, s in enumerate(at) if s is None]
        for j in trace.inserted:
            word = words[j]
            if word.control or word.op == HALT_OP:
                raise UnknownSemantics(
                    j,
                    op(word.op).name,
                    [(OP_FIELD, word.op)],
                    "an inserted control or halt word: the flow around it is not modelled",
                )
            c = next(
                (i for i in range(max(j - DELAY_SLOTS, 0), j) if words[i].control),
                None,
            )
            if c is not None:
                raise UnknownSemantics(
                    j,
                    "insert",
                    [],
                    f"inserted in the delay slots of word {c}, it pushes a stock word out "
                    "of them",
                )
        length = run.boot.u32(8)
        operands = Operands16()
        prev = -1
        stocked = []
        for j, s in enumerate(at):
            word = words[j]
            if s is None:
                site = prev == ENGINE_AFTER and length is not None
                if site and not operands.seeded:
                    operands.seed(length)
                self._cover_insert(run, trace, j, word, operands if site else None)
                continue
            prev = s
            t = word.target
            if t is not None:
                to = at[t] if t < len(at) else None
                if to is None:
                    raise UnknownSemantics(
                        j,
                        op(word.op).name,
                        [],
                        f"its target {t} is no stock word of the job",
                    )
                word = Word.from_int(TARGET.put(word.value, to))
            self.cover(trace, s, word, run.strict)
            stocked.append(word)
        if trace.writes:
            # The writer's distance in words to stock word ENGINE_AFTER + 1.
            j65 = at.index(ENGINE_AFTER + 1)
            trace.writes = {r: (v, j65 - j) for r, (v, j) in trace.writes.items()}
        shifted = [s for j, s in enumerate(at) if s is not None and s != j]
        listed = ", ".join(map(str, trace.inserted))
        trace.notes.append(
            f"words inserted at job indices {listed}; the other words are named by their "
            "stock indices"
        )
        if shifted:
            trace.taint = H
            trace.notes.append(
                f"stock words {shifted[0]}..{shifted[-1]} off their stock indices"
            )
        return replace(job, program=replace(job.program, words=tuple(stocked)))

    def _cover_insert(
        self,
        run: ModelRun,
        trace: Trace,
        j: int,
        word: Word,
        operands: "Operands16 | None",
    ) -> None:
        """An inserted word by its content: the idle word, or a data op with only its op slot's
        and PR ports' fields off idle that its op semantics (`ops`, `op_effect`) covers, at the
        semantics' confidence. A load is recorded only (where its datum goes is open). A
        result goes through the word's PR ports (`destinations`): at the engine site
        (`ENGINE_AFTER`, `operands` given) to the operand set, which later inserts read from
        the next word on, or to the xreg set, which the stock transfers after the site read
        (`Trace.writes`); elsewhere the operands are unknown and a result has no modelled
        reader. Anything else raises UnknownSemantics (strict) or is ignored and taints."""
        if word == IDLE:
            return
        changed = word.changed(IDLE)
        fields = [(f, v) for f, v in changed if f.unit not in ("op_slot", "pr")]
        sem = self.ops.get(word.op)
        name = op(word.op).name
        effect = None
        if self.transfer.matches(word):
            why = "an inserted register transfer: which slot it takes is not modelled"
        elif fields:
            why = "fields outside the op slot and the PR ports"
        elif sem is None:
            fields, why = [(OP_FIELD, word.op)], f"{name} has no op semantics"
        elif word.op in STORES:
            fields = [(OP_FIELD, word.op)]
            why = f"{name}: a local store, the copy's request in local memory may change"
        else:
            fields = [(OP_FIELD, word.op)]
            regs = RegisterFile() if operands is None else operands.regs
            effect = op_effect(sem, word, regs)
            why = self._insert_effect(word, name, effect, operands, trace, j)
            if effect is None:
                fields = [(f, word[f.id]) for f in UNITS["op_slot"].fields]
        if not why:
            assert sem is not None and effect is not None
            trace.classes = min(trace.classes, sem.confidence)
            if not effect.computes:
                note = (
                    f"{effect.note or 'no effect'}; where a datum it reads goes is open"
                )
            else:
                note = effect.note
            trace.notes.append(f"inserted word {j}: {name}, {note}")
            return
        if run.strict:
            raise UnknownSemantics(j, f"inserted word {j}", fields, why)
        trace.ignored.append((j, [f.id for f, _ in changed]))
        trace.taint = H

    def _insert_effect(
        self,
        word: Word,
        name: str,
        effect: Effect | None,
        operands: "Operands16 | None",
        trace: Trace,
        j: int,
    ) -> str:
        """Apply an inserted word's effect; returns why it is not covered ("" when it is)."""
        if effect is None:
            return f"{name}: outside what its op semantics covers"
        if not effect.computes:
            if any(f.unit == "pr" for f, _ in word.changed(IDLE)):
                return f"{name}: a PR port enabled, where its datum goes is open"
            return ""
        dests, why = destinations(word)
        if why:
            return f"{name} ({effect.note}): {why}"
        if operands is None:
            return (
                f"{name} ({effect.note}): its result has no modelled reader off the "
                f"site after word {ENGINE_AFTER}"
            )
        for to, r in dests:
            if to is RegSet.OPERAND:
                operands.regs[r] = effect.result
            elif r in self.late_writes:
                trace.writes[r] = (None, j)
                trace.notes.append(
                    f"inserted word {j} writes r{r}, which a stock word after it writes too"
                )
            else:
                trace.writes[r] = (_readout(effect.result), j)
        return ""

    def cover(self, trace: Trace, index: int, word: Word, strict: bool) -> None:
        """Run the job's word at `index` by what covers it: the idle word; X1's transfer semantic, a
        register transfer at any index; the row of the stock word at its stock index, which
        matches the word on its fields besides its operands; the stock halt word at another
        index, which ends the job where stepping reaches it (`_halt`) and else is a hypothesis.
        Any other word raises UnknownSemantics, naming the fields that differ from what covers
        the stock word there (strict); lenient mode ignores every field that differs from the
        stock word, runs the stock word and taints (a stock transfer run so yields to the
        transfers of its channel that the job carries, `resolve`)."""
        if word == IDLE:
            return
        if self.transfer.matches(word):
            trace.classes = min(trace.classes, self.transfer.confidence)
            self.transfer.act(trace, word, index, strict)
            return
        row = self.rows.get(index)
        if row is not None and row.matches(word):
            trace.classes = min(trace.classes, row.confidence)
            row.act(trace, word, index, strict)
            return
        if word == self.halt:
            trace.classes = trace.taint = H
            trace.notes.append(f"word {index}: the halt off its stock position")
            return
        stock = self.program.words[index] if index < len(self.program.words) else IDLE
        nearest = row or (self.transfer if self.transfer.matches(stock) else None)
        if strict:
            miss = word.changed(IDLE) if nearest is None else nearest.mismatches(word)
            name = "idle" if nearest is None else nearest.name
            raise UnknownSemantics(index, name, miss)
        trace.ignored.append((index, [f.id for f, _ in word.changed(stock)]))
        trace.taint = H
        if nearest is self.transfer:
            trace.standins.add(index)
        if nearest is not None:
            trace.classes = min(trace.classes, nearest.confidence)
            nearest.act(trace, stock, index, strict)

    def apply(self, run: ModelRun, job: Job, trace: Trace) -> JobResult:
        """Resolve the job and apply its one load and one store to the boot."""
        boot = run.boot
        view = start_copy(boot)
        halted = self._halt(job, trace)
        if halted is not None:
            return halted
        late = boot.regs
        if trace.writes:
            late = RegisterFile(list(boot.regs.regs))
            for r, (v, _) in trace.writes.items():
                late[r] = v
        plan = self.resolve(trace, boot.regs, view, job.program.words, late)
        return self._apply(boot, plan, trace, view)

    def _halt(self, job: Job, trace: Trace) -> JobResult | None:
        """The result of a job that an op 7 off the stock halt ends before the stream:
        status 0, dst not written (word 5 in place of 11 on the camera [verified], at any
        length; elsewhere inferred, or weaker through the path to it); None when no such
        halt runs. The job is stepped on a `Machine`, lenient (`_halt_flow`): the halt is
        its first op 7 and the three words behind it still run. A halt at or after a
        stream word, or in a job whose stream engine stored (the tail included) or went
        off script, raises: whether the stream drains first is open (at 58 the machine
        has no command handed, but a halt in place of a word shifts the values its tail
        hands, a state not measured). So does a halt after a loop word (the loop's words
        were measured with the loop skipped only), and a path that leaves the stock path
        the rows were measured on (`_STOCK_PATH`) without reaching the off-stock op 7."""
        stock = {i for i, w in enumerate(self.program.words) if w.op == HALT_OP}
        words = job.program.words
        if all(w.op != HALT_OP or i in stock for i, w in enumerate(words)):
            return None
        flow, machine = self._halt_flow(job)
        h = next(k for k, s in enumerate(flow.steps) if s.op == HALT_OP)
        at = flow.steps[h].index
        if at in stock:
            path = _STOCK_PATH
            left = [
                s.index
                for s in flow.steps
                if s.index in path and s.taken is not None and s.taken != path[s.index]
            ]
            if not left:
                return None
            why = (
                f"the path leaves the stock path at word {left[0]} and does not reach the op 7 "
                "off the stock halt: the rows hold on the stock path"
            )
            raise UnknownSemantics(left[0], "halt", [], why)
        engine = machine.stream
        if any(s.index in STREAM for s in flow.steps[: h + 1]) or engine.stored:
            why = "a halt after the stream started: whether the stream drains first is open"
            raise UnknownSemantics(at, "halt", [], why)
        if engine.off:
            why = f"the stream off script before the halt ({engine.off}): what it stores is open"
            raise UnknownSemantics(at, "halt", [], why)
        if any(s.index in LOOP for s in flow.steps[:h]):
            why = "a halt after a loop word: the loop's words were measured with the loop skipped"
            raise UnknownSemantics(at, "halt", [], why)
        # Edited words that run, the tail's included, and inserted words (`cover_job` strips
        # them, so the machine steps without them) are not modelled.
        stock_words = self.program.words
        edited = bool(trace.inserted) or any(
            s.index >= len(stock_words) or words[s.index] != stock_words[s.index]
            for k, s in enumerate(flow.steps)
            if k != h
        )
        seen = at in HALTS_SEEN and words[at] == self.halt
        conf = H if trace.ignored or edited or machine.lapses else V if seen else I
        # The halt's own tier is `seen`'s; the other steps carry the path's and the tail's.
        tiers = [s.confidence for k, s in enumerate(flow.steps) if k != h]
        conf = min([conf, engine.cap, *(t for t in tiers if t is not None)])
        why = f"op 7 at word {at} halts the job before the stream: {Outcome.NO_STORE.value}"
        result = JobResult(Status.OK, [why], ignored=trace.ignored, notes=trace.notes)
        result.taint = conf
        return result

    @staticmethod
    def _halt_flow(job: Job) -> tuple[Flow, Machine]:
        """The job stepped on a lenient `Machine`, which decides the branches; where it cannot,
        `_STOCK_PATH` decides, a hypothesis from there on. Raises where neither does."""
        given: dict[int, bool] = {}
        fallback = _STOCK_PATH
        while True:
            boot = Boot()
            machine = Machine(boot, None, Engine())
            branching = (
                Given(given, H, "as in the copies run on the camera") if given else None
            )
            flow = Executor(boot, machine, strict=False, branching=branching).step(job)
            at = flow.steps[-1].index
            if flow.stop is not Stop.UNDECIDED or at not in fallback or at in given:
                break
            given[at] = fallback[at]
        # Off the stock halt `_halt` refuses an open end; at it `_halt` returns None and
        # `resolve` takes over.
        if flow.stop not in (Stop.HALT, Stop.OPEN_END):
            raise UnknownSemantics(0, "halt", [], f"the job does not halt: {flow.why}")
        return flow, machine

    def resolve(
        self,
        trace: Trace,
        regs: RegisterFile,
        view: JobView,
        words: tuple[Word, ...],
        late: RegisterFile | None = None,
    ) -> Plan:
        """The plan of a job of the program `words`; `late` holds the registers as the
        transfers after the engine site see them (`Trace.writes`)."""
        length = view.length
        assert length is not None
        stock = self.program.words
        edits = (
            sum(a != b for a, b in zip(words, stock))
            + abs(len(words) - len(stock))
            + len(trace.inserted)
        )
        # The defaults are what every stock copy shows: load at src, offsets 0, count len, bytes.
        plan = Plan(count=length, cap=min(trace.taint, I if edits >= 2 else V))
        entry = self._loop_entry(words, length)
        if entry is not None:
            plan.outcomes.append(entry)
        for c in self.opaque:
            part = c.part
            assert part is not None
            if entry is not None and c.positions & LOOP_GROUP:
                continue
            if trace.parts[c] < len(c.positions):
                at = ", ".join(str(i) for i in sorted(c.positions))
                plan.outcomes.append(
                    (
                        part.missing,
                        f"{part.token} (stock word {at}) missing: {part.fact}",
                        part.at(length),
                    )
                )
            elif c.operands:
                edit = self._operand_edit(c, *trace.operands[c])
                if edit is not None:
                    o, at = edit
                    why = f"{part.token} at {at}: {o.value}"
                    plan.outcomes.append((o, why, part.at(length)))
        # Stand-in transfers yield to the transfers the job carries where they overfill its
        # channel (a transfer moved onto another word's index): as many as overfill it, the
        # first ones in program order, so that a moved transfer before them leaves the later
        # stand-ins in their stock roles (a hypothesis).
        transfers = dict(trace.channels)
        for c in self.channels:
            slots = transfers.get(c.name, [])
            over = len(slots) - len(c.roles)
            if over <= 0:
                continue
            drop = sorted(t.index for t in slots if t.index in trace.standins)[:over]
            transfers[c.name] = [t for t in slots if t.index not in drop]
        filled, stalls = assign_roles(self.channels, transfers)
        for why, conf in stalls:
            plan.outcomes.append(
                (Outcome.TIMEOUT, f"{why}: the stream never completes", conf)
            )
        roles = []
        for role, t, assigned in filled:
            pre = min(assigned, t.taint)
            at = late if late is not None and t.index > ENGINE_AFTER else regs
            lead = trace.writes.get(t.reg) if t.index > ENGINE_AFTER else None
            written = None if lead is None else t.index - ENGINE_AFTER - 1 + lead[1]
            conf = min(pre, self._role(plan, role, t, pre, at, view, written))
            roles.append((role, conf))
        base = min(plan.cap, trace.classes, *(c for _, _, c in plan.outcomes))
        plan.load_taint = min([base, *(c for r, c in roles if r.load)])
        plan.store_taint = min([base, *(c for r, c in roles if not r.load)])
        return plan

    def _operand_edit(
        self, c: OpaqueClass, index: int, values: dict[str, int]
    ) -> tuple[Outcome, str] | None:
        """The outcome of the edit of an operand field of `c`'s word and how it reads; None
        for the stock word. Edits of two fields of one word are not measured."""
        part = c.part
        assert part is not None
        stock = self.program.words[min(c.positions)]
        changed = [(lookup(k), v) for k, v in values.items() if v != stock[k]]
        if not changed:
            return None
        if len(changed) > 1:
            raise UnknownSemantics(
                index, c.name, changed, "edits of two fields of one word"
            )
        f, value = changed[0]
        if part.operand is not None and f is lookup(part.operand_field):
            o = part.operand.get(value)
            at = f"r{value}" if f.kind is Kind.REG else f"{f.label}={value:#x}"
        else:
            flipped = value ^ stock[f.id]
            flips = {lookup(k).id: t for k, t in part.flips.items()}
            o = flips.get(f.id, {}).get(flipped)
            at = f"{f.label} ^ {flipped:#x}"
        if o is None:
            raise UnknownSemantics(index, c.name, [(f, value)])
        return o, at

    def _loop_entry(
        self, words: tuple[Word, ...], length: int
    ) -> tuple[Outcome, str, Confidence] | None:
        """The outcome of an edit of 28 or 45 together with one of the loop 50..57, from
        `_LOOP_ENTRIES`; None without such a pair. Unmeasured pairs have no semantics."""
        stock = self.program.words
        edits = frozenset(
            (i, _edit(stock[i], words[i])) for i in LOOP_GROUP if words[i] != stock[i]
        )
        at = {i for i, _ in edits}
        if not (at & {28, GUARD} and at & set(LOOP)):
            return None
        entry = _LOOP_ENTRIES.get(edits)
        if entry is None:
            listed = ", ".join(f"{i} {e}" for i, e in sorted(edits))
            raise UnknownSemantics(
                min(at & set(LOOP)),
                "loop",
                [],
                f"{listed}: the loop's words were measured with the loop skipped only",
            )
        o, fact = entry
        return o, f"{fact}: {o.value}", V if length == KIB else I

    def _role(
        self,
        plan: Plan,
        role: Role,
        t: Transfer,
        pre: Confidence,
        regs: RegisterFile,
        view: JobView,
        written: int | None = None,
    ) -> Confidence:
        """Apply `role` at the transfer's register; returns the confidence of what it did.

        An outcome it records gets `pre`, the confidence of the assignment, at most. A value an
        inserted word wrote `written` words before the transfer that the role cannot place
        raises UnknownSemantics.
        """
        length = view.length
        assert length is not None
        reg, index = t.reg, t.index
        stock = reg == self.program.words[role.word]["xreg"]
        o = role.table.get(reg)
        table = V if stock else role.table_at(length)
        if o is not None and o is not Outcome.OK:
            why = f"word {index} ({role.name}) at r{reg}: {o.value}"
            plan.outcomes.append((o, why, min(pre, table)))
            return table
        if role.use is Use.NONE:
            if o is None:
                raise UnknownSemantics(
                    index, role.name, [(lookup("xreg"), reg)], role.fact
                )
            return table
        conf = role.use_at(stock, length)
        value = regs[reg]
        if written is not None:
            self._check_written(role, t, value, length, written)
        if value is not None:
            conf = min(conf, value.taint)
        match role.use:
            case Use.ADDRESS:
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
            case Use.LOAD_OFFSET:
                plan.o68 = None if value is None else value.image(_trunc32)
            case Use.STORE_OFFSET:
                plan.o76 = None if value is None else value.image(_floor32)
            case Use.COUNT:
                n = None if value is None else value.exact
                if n is not None and n > MAX_COUNT:
                    why = f"word {index} at r{reg}: a count of {n:#x}"
                    plan.outcomes.append((Outcome.TIMEOUT, why, H))
                    return H
                plan.count = n
            case Use.FORMAT:
                # The format is a table by register: the register's value does not enter.
                conf = role.use_at(stock, length)
                if reg == 21:
                    plan.format, conf = view.boot.r21_format, H
                elif reg in self.formats:
                    plan.format = self.formats[reg]
                else:
                    raise UnknownSemantics(
                        index, role.name, [(lookup("xreg"), reg)], role.fact
                    )
        return conf

    def _check_written(
        self, role: Role, t: Transfer, value: Reg | None, length: int, distance: int
    ) -> None:
        """Raise unless the load offset or count role can place `value`, written by an
        inserted word: known and not negative (word 68's rounding of negative values is open:
        truncation fits the register sweep, flooring the op sweep); for the load offset one
        placement over the interval, inside the buffer (beyond it no load was seen, and the
        store then emits an older job's bytes); for the count an exact multiple of 16 up to
        len (whether a count off 16-byte steps is floored or rounded up is open: a floor fits
        the inserted counts, not an odd-length copy's; above len a full copy then a timeout was
        seen, and so was an overrun on another route). Only these two roles were measured
        reading such a value, `XREG_DISTANCE` or more words after the write."""
        what = None
        if role.use not in (Use.LOAD_OFFSET, Use.COUNT):
            what = "a value for a role not measured with one"
        elif distance < XREG_DISTANCE:
            what = f"a value written {distance} words before, nearer than measured"
        elif value is None:
            what = "a value the model does not know"
        elif value.lo < 0:
            what = "a negative value"
        elif role.use is Use.LOAD_OFFSET and value.image(_floor32) is None:
            what = "a value interval with two placements"
        elif role.use is Use.LOAD_OFFSET and _floor32(value.hi) + length > BUFFER_HI:
            what = "a load offset beyond the buffer"
        elif role.use is Use.COUNT and (value.exact is None or value.exact % 16):
            what = "a count that is no exact multiple of 16"
        elif role.use is Use.COUNT and value.exact > length:
            what = "a count above len"
        if what is not None:
            raise UnknownSemantics(
                t.index,
                role.name,
                [(lookup("xreg"), t.reg)],
                f"r{t.reg} holds {what}, written by an inserted word",
            )

    def _apply(
        self, boot: StockBoot, plan: Plan, trace: Trace, view: JobView
    ) -> JobResult:
        if plan.has(Outcome.HAZARD):
            status = Status.HAZARD
        elif plan.has(Outcome.TIMEOUT) or plan.has(Outcome.STORE_TIMEOUT):
            status = Status.TIMEOUT
        else:
            status = Status.OK
        result = JobResult(
            status,
            [why for _, why, _ in plan.outcomes],
            ignored=trace.ignored,
            notes=trace.notes,
        )
        length = view.length
        assert length is not None and view.dst is not None
        loads = not plan.has(Outcome.NO_LOAD)
        stores = not (
            status is Status.HAZARD
            or plan.has(Outcome.TIMEOUT)
            or plan.has(Outcome.NO_STORE)
        )
        result.taint = plan.result_taint(stores)
        unknown_data = plan.has(Outcome.DATA_UNKNOWN)

        if loads:
            self._load(boot, plan, view, certain=stores and not unknown_data)
        if not stores:
            return result
        dst = view.dst
        if plan.has(Outcome.ZERO_FILL):
            zero = boot.source("zero fill", "word 15 missing: the store writes zeros")
            data = Bytes.of(bytes(ZERO_FILL), zero, plan.store_taint)
            boot.dram.write(dram_alias(dst), data)
            result.stored = (dst, dst + ZERO_FILL)
            return result
        n = plan.count
        if unknown_data or n is None or plan.o76 is None or (plan.format is None):
            why = "; ".join(
                w for o, w, _ in plan.outcomes if o is Outcome.DATA_UNKNOWN
            ) or ("the store's count, offset or format is not known")
            n = length if n is None else n
            boot.dram.write(
                dram_alias(dst),
                Bytes.unknown(n, boot.source("store not modelled", why)),
            )
            result.stored = (dst, dst + n)
            return result
        data = self._format(boot, plan.format, plan.o76, n)
        boot.dram.write(dram_alias(dst), data.capped(plan.store_taint))
        result.stored = (dst, dst + n)
        return result

    def _load(self, boot: StockBoot, plan: Plan, view: JobView, certain: bool) -> None:
        """The load into the buffer; UNKNOWN where it may or may not have run, or with unknown data."""
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

    def _format(self, boot: StockBoot, fmt: str, o76: int, n: int) -> Bytes:
        """n store bytes from the buffer at index o76, in format `fmt`."""
        if fmt == "bytes":
            return self._buffer(boot, o76, n)
        if fmt == "zero":
            return Bytes.of(
                bytes(n), boot.source("format zero", "word 47's zero format"), V
            )
        zero = boot.source("format zero bits", "zero bits of a widening format")
        if fmt == "u32":
            src = self._buffer(boot, o76, n // 4)
            out = Bytes.of(bytes(4 * len(src)), zero, V)
            for name in Bytes.__slots__:
                getattr(out, name)[0::4] = getattr(src, name)
            return out
        if fmt == "pack10":
            return self._pack10(boot, o76, n, zero)
        raise ValueError(f"no store format {fmt!r}")

    def _buffer(self, boot: StockBoot, o: int, n: int) -> Bytes:
        if not boot.buffer.contains(o, n):
            return Bytes.unknown(
                n, boot.source("store not modelled", "outside the modelled buffer")
            )
        return boot.buffer.read(o, n)

    def _pack10(self, boot: StockBoot, o76: int, n: int, zero: int) -> Bytes:
        """16 output bytes per 12 buffer bytes: each zero-extended to 10 bits, LSB first, top
        8 bits 0. An output byte is known when every input byte it draws on is known."""
        lines = n // 16
        src = self._buffer(boot, o76, lines * 12)
        derived = boot.source("format pack10", "word 47's 10-bit pack of buffer bytes")
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


_STOCK_PATH = {27: False, 45: True}
"""The decisions of the copy's two forward branches as the camera runs show them: 27 (to the
return 79) not taken (inferred: the store words after it act) and 45 (over the loop 50..57)
taken (inferred, strong: idling its delay slots 46..49 times the job out, idling 50..57 has no
effect, moving its target to 50 changes the copy into a timeout, and a halt at 50 with 51..57
idled has no effect in an aligned copy and with each of src, dst, len, src + len, dst + len and
src - dst odd, so no low-bit test of a job value decides it). The loop's back-edge 53 is never
reached."""
