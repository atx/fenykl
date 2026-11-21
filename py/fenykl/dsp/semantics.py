"""Semantics: what a word does, registered per word class, and the hidden sequencer.

A word class is matched by field values (`match`) and names the fields it reads or ignores
(`operands`). A word gets the semantics of the class it matches. Every field a class neither
matches nor names as an operand must be idle; a word that matches no class raises
`UnknownSemantics`, naming the fields that differ from the nearest class (strict mode). In the
lenient mode the word runs as its nearest class, the differing fields are ignored and every byte
the job produces is tainted "hypothesis".

"Undecoded" means "no registered semantics", not "below verified": semantics registered at
hypothesis confidence run and taint what they touch.

The classes of program 16 (the stock copy, command 0xF):
- the idle word, a no-op;
- the register-transfer words: one class, matched by their signature (every field idle but the
  two register groups, the route nibbles and f308_309); they hand a register to the sequencer;
- every other stock word as its own class, matched by its value: no single field of these words
  is decoded, but what the job does without each of them was seen on the camera (`PARTS`).

The hidden sequencer gives each register transfer its role (load address, offset, count, store
format, ...) by position, since no field of the word names it (`Sequencer`).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from . import memcpy
from .fields import FIELDS, STAIRCASE, Confidence, Field, lookup
from .units import UNITS
from .word import IDLE, Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


class UnknownSemantics(Exception):
    """A word sets a field (or a field value) that no registered semantics covers."""

    def __init__(
        self, index: int, nearest: str, fields: list[tuple[Field, int]], why: str = ""
    ) -> None:
        self.index, self.nearest, self.fields = index, nearest, fields
        names = ", ".join(f"{f.id}={v:#x}" for f, v in fields)
        text = f"word {index}: no semantics for {names} (nearest class: {nearest})"
        super().__init__(f"{text}; {why}" if why else text)


class Outcome(Enum):
    """What a word, a missing word or a role at a register does to the job."""

    OK = "copies"
    NO_STORE = "dst not written"
    NO_LOAD = "no load: the store emits the held buffer"
    TIMEOUT = "the job never completes (timeout-abort)"
    HAZARD = "the job wedges or the camera loses its link"
    ZERO_FILL = "the store writes 0x4000 zero bytes from dst"
    DATA_UNKNOWN = "dst is written with data the model does not know"


@dataclass(frozen=True)
class Transfer:
    """A register transfer as the sequencer sees it."""

    index: int
    reg: int
    taint: Confidence
    """Weaker than verified when the word moves fields the transfer is assumed to ignore."""


@dataclass
class Trace:
    """What the words of one job leave for the resolution at its end."""

    parts: list[str] = field(default_factory=list)
    channels: dict[str, list[Transfer]] = field(default_factory=dict)
    operands: dict[str, int] = field(default_factory=dict)
    """Operands of the opaque words that matter (e.g. word 69's register), by part."""
    ignored: list[tuple[int, list[str]]] = field(default_factory=list)
    taint: Confidence = V
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WordClass:
    name: str
    fact: str
    match: Mapping[str, int]
    operands: frozenset[str]
    mask: int = field(init=False, repr=False)
    bits: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        mask = bits = 0
        for key, value in self.match.items():
            f = lookup(key)
            mask, bits = f.put(mask, f.mask), f.put(bits, value)
        object.__setattr__(self, "mask", mask)
        object.__setattr__(self, "bits", bits)

    def matches(self, word: Word) -> bool:
        return word.value & self.mask == self.bits

    def mismatches(self, word: Word) -> list[tuple[Field, int]]:
        return [(lookup(k), word[k]) for k, v in self.match.items() if word[k] != v]

    def act(self, trace: Trace, word: Word, index: int) -> None:
        raise NotImplementedError


# -- the idle word


class IdleClass(WordClass):
    def act(self, trace: Trace, word: Word, index: int) -> None:
        pass


IDLE_CLASS = IdleClass(
    "idle",
    "the no-op of the stock programs (program 16 holds it at 3, 4, 6..9) [verified]",
    {f.id: f.idle for f in FIELDS},
    frozenset(),
)

# -- the opaque stock words of program 16


@dataclass(frozen=True)
class Part:
    """A stock word as an undecoded part of the job: what the job does without it."""

    missing: Outcome
    fact: str
    operand: dict[int, Outcome] | None = None
    """Outcomes by the value of its register field 115..119, where that field was swept."""


_STORE_PATH = "store path: idled, dst is not written (prefill intact)"
_COMPLETION = "completion: idled, the job times out"
_NONE = "no effect seen when idled, also with len, dst and src changing between jobs"

PARTS: dict[int, Part] = {
    0: Part(Outcome.NO_STORE, f"prologue; {_STORE_PATH}"),
    5: Part(Outcome.TIMEOUT, f"prologue; {_COMPLETION}"),
    15: Part(
        Outcome.ZERO_FILL,
        "store count and data: idled, the job writes 0x4000 zero bytes from dst at 1 and 4 KiB, "
        "whatever the length",
    ),
    27: Part(
        Outcome.DATA_UNKNOWN,
        "a load address (hypothesis): idled, dst gets foreign data",
    ),
    28: Part(Outcome.TIMEOUT, _COMPLETION),
    31: Part(
        Outcome.DATA_UNKNOWN,
        "a store-side buffer address (hypothesis): idled, dst gets data of an older job, 16 bytes off",
    ),
    45: Part(
        Outcome.TIMEOUT,
        f"{_COMPLETION}; its register field is don't-care (copies at all 31 values)",
        operand={r: Outcome.OK for r in range(31)},
    ),
    58: Part(
        Outcome.DATA_UNKNOWN,
        "a load address with fixed content: idled, dst gets a fixed 1 KiB",
    ),
    60: Part(Outcome.TIMEOUT, f"stream; {_COMPLETION}"),
    61: Part(
        Outcome.DATA_UNKNOWN,
        "the src loader: idled, the job emits data held from an older 4 KiB job, then 0x5A",
    ),
    62: Part(Outcome.HAZARD, "idled, the camera lost its link"),
    63: Part(Outcome.TIMEOUT, f"stream; {_COMPLETION}"),
    64: Part(Outcome.HAZARD, "idled, the job wedged (status -5)"),
    65: Part(Outcome.TIMEOUT, f"stream; {_COMPLETION}"),
    69: Part(
        Outcome.NO_STORE,
        f"store side; {_STORE_PATH}; its register: dst not written at r0 and r4, a copy at the others",
        operand={r: Outcome.NO_STORE if r in (0, 4) else Outcome.OK for r in range(31)},
    ),
    79: Part(Outcome.TIMEOUT, f"stream end; {_COMPLETION}"),
    **{
        i: Part(Outcome.NO_STORE, _STORE_PATH)
        for i in (12, 13, 14, 16, 17, 21, 22, 23, 24)
    },
    **{i: Part(Outcome.NO_STORE, _STORE_PATH) for i in range(32, 42)},
    **{
        i: Part(Outcome.OK, _NONE)
        for i in (
            1,
            2,
            10,
            11,
            18,
            19,
            20,
            25,
            26,
            29,
            30,
            50,
            51,
            52,
            53,
            56,
            59,
            80,
            81,
            82,
            83,
        )
    },
}


class OpaqueClass(WordClass):
    def act(self, trace: Trace, word: Word, index: int) -> None:
        trace.parts.append(self.name)
        if self.operands:
            trace.operands[self.name] = word["xreg"]


def _opaque(index: int, word: Word) -> OpaqueClass:
    part = PARTS[index]
    operands = frozenset({"f115_119"}) if part.operand else frozenset()
    match = {f.id: f.get(word.value) for f in FIELDS if f.id not in operands}
    return OpaqueClass(f"word{index}", part.fact, match, operands)


# -- the register transfers

ROUTES = frozenset(f.id for f in FIELDS if f.lo in STAIRCASE)
CONTEXT = ("f100_104", "f105_109", "f110_114", "f123_127", "f128_132")
"""The register group A fields besides xreg: they select the transfer's channel."""
REGS_B = tuple(f.id for f in UNITS["regs_b"].fields if f.kind == "reg")
TRANSFER_OPERANDS = frozenset({*CONTEXT, "f115_119", *REGS_B, *ROUTES, "f308_309"})


def _ctx(index: int) -> tuple[int, ...]:
    word = memcpy.WORDS[index]
    return tuple(word[k] for k in CONTEXT)


# Channels, by the register-group-A context of their transfers, and their roles in slot order.
CHANNEL_OF: dict[tuple[int, ...], str] = {
    _ctx(42): "completion A",
    _ctx(46): "completion B",
    _ctx(54): "spare A",
    _ctx(57): "spare B",
    _ctx(66): "stream",
}
if _ctx(66) != _ctx(70):
    raise ValueError("words 66 and 70 no longer share a channel context")


class TransferClass(WordClass):
    def matches(self, word: Word) -> bool:
        return super().matches(word) and word["f308_309"] in (0, 1)

    def act(self, trace: Trace, word: Word, index: int) -> None:
        ctx = tuple(word[k] for k in CONTEXT)
        channel = CHANNEL_OF.get(ctx)
        if channel is None:
            raise UnknownSemantics(
                index,
                self.name,
                [(lookup(k), word[k]) for k in CONTEXT],
                "no channel has this register context",
            )
        if word["f308_309"] == 1:
            # 74 and 78 differ from 73 and 77 only here and copy at every register (hypothesis:
            # a transfer that does not take a slot).
            trace.notes.append(f"word {index}: a holding transfer, no slot")
            return
        ref = _REGS_B[channel]
        off = [k for k in REGS_B if word[k] not in ref[k]]
        # Single-bit flips of 206..215 in words 67 and 68 left the copy unchanged [verified];
        # other values elsewhere are assumed ignored too, and taint.
        taint = H if off else V
        trace.channels.setdefault(channel, []).append(
            Transfer(index, word["xreg"], taint)
        )


TRANSFER_CLASS = TransferClass(
    "transfer",
    "register transfer: hands the register named in 115..119 to the stream configuration; its "
    "role (address, offset, count, enable, format) is set by its position [inferred, strong]",
    {f.id: f.idle for f in FIELDS if f.id not in TRANSFER_OPERANDS},
    TRANSFER_OPERANDS,
)


def is_transfer(word: Word) -> bool:
    return TRANSFER_CLASS.matches(word)


STOCK_TRANSFERS = tuple(i for i, w in enumerate(memcpy.WORDS) if is_transfer(w))
# The register group B values of each channel's stock transfers.
_REGS_B: dict[str, dict[str, set[int]]] = {}
for _i in STOCK_TRANSFERS:
    _ref = _REGS_B.setdefault(CHANNEL_OF[_ctx(_i)], {k: set() for k in REGS_B})
    for _k in REGS_B:
        _ref[_k].add(memcpy.WORDS[_i][_k])


# -- roles of the transfers, by channel and slot


@dataclass(frozen=True)
class Role:
    name: str
    word: int
    """The stock word that holds the role."""
    fact: str
    table: Mapping[int, Outcome] = field(default_factory=dict)
    """Outcome by register, seen on the camera at 1 KiB [verified]; checked before `value`."""
    value: str = ""
    """How the role uses the register's value: "address", "offset68", "count", "offset76",
    "format"; empty when only the table is known."""


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

CHANNELS: dict[str, tuple[Role, ...]] = {
    "completion A": (
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
            "dst not written at r3, r5, r7, timeout at r6; the camera lost its link at r0, r1, "
            "r4, r8, r10 (not registered)",
            _table(None, ok=(9,), no_store=(3, 5, 7), timeout=(6,)),
        ),
    ),
    "completion B": (
        Role("completion", 46, "don't-care: copies at all 31 values", DONT_CARE),
        Role(
            "store format",
            47,
            "the store's element format, applied by the store, not the load: bytes at r6, "
            "u8 -> u32 zero-extended at r14 and r29, a 10-bit pack or zeros at r21 (by boot), "
            "zeros at the others; timeout at r9; the camera lost its link at r16, r18, r19, r20, "
            "r24, r26, r27, r28 (not registered)",
            _table(None, timeout=(9,)),
            value="format",
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
    "spare A": (
        Role("unused", 54, "no effect seen when idled", DONT_CARE),
        Role("unused", 55, "no effect seen when idled", DONT_CARE),
    ),
    "spare B": (),
    "stream": (
        Role(
            "load enable",
            66,
            "a load at r1, r3, r4, r5, r10, r12, r13, r17; no load (the held buffer is stored) "
            "at 11 values; timeout at the T set; a fixed 1 KiB at r8",
            _table(
                None,
                ok=(1, 3, 4, 5, 10, 12, 13, 17),
                no_load=(0, 2, 6, 11, 14, 15, 21, 22, 23, 25, 29),
                timeout=(*T_SET, 26),
                data_unknown=(8,),
            ),
        ),
        Role(
            "load address",
            67,
            "an absolute address at r3 (src), r21 and r29 (firmware text); low registers read a "
            "fixed memory at value / 16; dst not written at r2 and r15",
            _table(None, no_store=(2, 15)),
            value="address",
        ),
        Role(
            "load offset",
            68,
            "the buffer index of the load: buffer[i + o] = S[i], o = value / 32 truncated to "
            "32 bytes (towards zero, hypothesis for negative values)",
            value="offset68",
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
            "the store's count in output bytes: dst[n, len) is not written; it does not limit the "
            "load; timeout at registers holding addresses or unknown values",
            _table(None, timeout=(2, 6, 8, 11, 14, *range(15, 31))),
            value="count",
        ),
        Role(
            "stream",
            75,
            "copies at r6 and r11; timeout at the T set, r3, r8, r15; dst not written elsewhere",
            _table(Outcome.NO_STORE, ok=(6, 11), timeout=(*T_SET, 3, 8, 15)),
        ),
        Role(
            "store offset",
            76,
            "the buffer index of the store: dst[i] = buffer[i + o], o = value / 32 rounded down to "
            "32 bytes (hypothesis for negative values)",
            value="offset76",
        ),
        Role(
            "stream",
            77,
            "only its stock r2 copies; dst not written at every other value",
            _table(Outcome.NO_STORE, ok=(2,)),
        ),
    ),
}

STOCK_ROLE: dict[int, tuple[str, int]] = {
    role.word: (channel, k)
    for channel, roles in CHANNELS.items()
    for k, role in enumerate(roles)
}

# Word 47's formats by register; r21's is a boot parameter.
FORMATS: dict[int, str] = {
    6: "bytes",
    14: "u32",
    29: "u32",
    **{r: "zero" for r in (0, 1, 2, 3, 4, 5, 7, 8, 10, 11, 12, 13, 15, 17, 22, 23, 25)},
}


class Sequencer:
    """Hidden state that gives the register transfers their roles [hypothesis].

    POINTER (the default): each channel has a slot pointer; a transfer with bit 308 clear takes
    the slot at the pointer and advances it, so roles follow the order of the transfers. Idling
    a transfer leaves its channel's last slot unset, and an unset slot stalls the job; this
    reproduces the camera: every idled counted transfer of 42..49 and 66..77 times out, while
    74 and 78 (bit 308 set) have no effect. The pointers start at slot 0 with every job.

    POSITION (the alternative considered): the role is the stock word index's role; a transfer
    at another index has none. It gives the same results on every edit run so far (field sweeps
    and idled words), so the camera has not told the two apart; an inserted or moved transfer
    would.
    """

    POINTER = "pointer"
    POSITION = "position"

    def __init__(self, mode: str = POINTER) -> None:
        if mode not in (self.POINTER, self.POSITION):
            raise ValueError(f"no sequencer mode {mode!r}")
        self.mode = mode

    def assign(
        self, channels: dict[str, list[Transfer]]
    ) -> tuple[list[tuple[Role, Transfer, Confidence]], list[str]]:
        """(role, transfer, assignment taint) per filled slot, and the unset slots.

        A channel with an unset slot stalls the job, so its filled slots are left out: what the
        shifted transfers would do there is not modelled.
        """
        by_channel: dict[str, list[tuple[Role, Transfer, Confidence]]] = {
            c: [] for c in CHANNELS
        }
        if self.mode == self.POINTER:
            for channel, slots in channels.items():
                roles = CHANNELS[channel]
                if len(slots) > len(roles):
                    extra = ", ".join(str(t.index) for t in slots[len(roles) :])
                    raise UnknownSemantics(
                        slots[len(roles)].index,
                        "transfer",
                        [],
                        f"{channel}: no slot for words {extra}",
                    )
                for role, t in zip(roles, slots):
                    by_channel[channel].append(
                        (role, t, V if t.index == role.word else H)
                    )
        else:
            for transfers in channels.values():
                for t in transfers:
                    if t.index not in STOCK_ROLE:
                        raise UnknownSemantics(
                            t.index, "transfer", [], "no role at this index"
                        )
                    channel, k = STOCK_ROLE[t.index]
                    by_channel[channel].append((CHANNELS[channel][k], t, V))
        filled, unset = [], []
        for channel, roles in CHANNELS.items():
            have = {role.word for role, _, _ in by_channel[channel]}
            if self.mode == self.POINTER:
                have = {r.word for r in roles[: len(by_channel[channel])]}
            missing = [
                f"{channel} slot {k} ({role.name}, stock word {role.word})"
                for k, role in enumerate(roles)
                if role.word not in have
            ]
            unset += missing
            if not missing:
                filled += by_channel[channel]
        return filled, unset


# -- the registry


def _classes() -> list[WordClass]:
    classes: list[WordClass] = [IDLE_CLASS]
    seen: dict[int, int] = {}
    for index, word in enumerate(memcpy.WORDS):
        if word == IDLE or is_transfer(word):
            continue
        if index not in PARTS:
            raise ValueError(f"stock word {index} has no part")
        if word.value in seen:
            # Equal words (the drain 81..83) are one class; they must share their part.
            if PARTS[seen[word.value]] != PARTS[index]:
                raise ValueError(
                    f"stock words {seen[word.value]} and {index} are equal"
                )
            continue
        seen[word.value] = index
        classes.append(_opaque(index, word))
    missing = {
        i for i in set(PARTS) - set(seen.values()) if memcpy.WORDS[i].value not in seen
    }
    if missing:
        raise ValueError(f"parts without a stock word: {sorted(missing)}")
    return [*classes, TRANSFER_CLASS]


CLASSES: tuple[WordClass, ...] = tuple(_classes())
PART_OF: dict[str, Part] = {
    c.name: PARTS[int(c.name[4:])] for c in CLASSES if isinstance(c, OpaqueClass)
}


def classify(word: Word, index: int, trace: Trace, strict: bool = True) -> WordClass:
    """The class of `word`; raises UnknownSemantics, or in lenient mode ignores the fields that
    differ from the nearest class (recorded in `trace`, which is tainted)."""
    for c in CLASSES:
        if c.matches(word):
            return c
    best, best_miss = None, None
    for c in CLASSES:
        miss = c.mismatches(word)
        if best_miss is None or len(miss) < len(best_miss):
            best, best_miss = c, miss
    assert best is not None and best_miss is not None
    if strict:
        raise UnknownSemantics(index, best.name, best_miss)
    trace.ignored.append((index, [f.id for f, _ in best_miss]))
    trace.taint = H
    return best
