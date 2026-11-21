"""Program 16, the stock copy (command 0xF): its model, built from the stock program at runtime.

The driver passes src, dst and len in DSP local memory words 0..2. No word of the program is
decoded; what is known comes from camera runs that replaced a word by the idle word ("idled") or
swept its register field 115..119, and is stated here by word index:

- `_PARTS`: what the job does without each undecoded stock word;
- `_CHANNELS`: the register-transfer words, grouped by their register context, and the role of
  each slot (load address, offsets, count, store format, ...);
- `_FORMATS`: word 47's store formats by register.

`Copy16Model` takes the stock program (from the user's firmware, `from_image`) and derives the
word classes from it: each undecoded stock word is a class matched by its value, the register
transfers one class matched by their signature, with the register contexts and the register
group B and route values of the stock transfers as the reference. No stock word is part of this
code. The model resolves a job's trace into its one load and one store (`resolve`).

Hazards: idling word 62 lost the camera's link and idling 64 or 72 failed the job with status
-5; these register values lost the link too: word 44 at 0, 1, 4, 8, 10; word 47 at 16, 18, 19,
20, 24, 26, 27, 28; word 70 at 26; word 71 at 0.
"""

from dataclasses import dataclass, field

from .fields import FIELDS, STAIRCASE, Confidence, lookup
from .program import Program, stock_programs
from .semantics import (
    IDLE_CLASS,
    Channel,
    OpaqueClass,
    Outcome,
    Part,
    Role,
    Sequencer,
    Trace,
    Transfer,
    UnknownSemantics,
    Use,
    WordClass,
)
from .state import JobView, RegisterFile
from .units import UNITS
from .word import IDLE, Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

ITEM = 16
MAX_COUNT = 0x40000
"""A count above this is an address or junk: the store never ends (hypothesis)."""

# -- the undecoded stock words

_STORE_PATH = "idled, dst is not written (prefill intact)"
_COMPLETION = "idled, the job times out"
_NONE = "no effect seen when idled, also with len, dst and src changing between jobs"


def _store_path(token: str = "store path") -> Part:
    return Part(token, Outcome.NO_STORE, _STORE_PATH)


_PARTS: dict[int, Part] = {
    0: _store_path("prologue store path"),
    5: Part("prologue completion", Outcome.TIMEOUT, _COMPLETION),
    15: Part(
        "store count and data",
        Outcome.ZERO_FILL,
        "idled, the job writes 0x4000 zero bytes from dst at 1 and 4 KiB, whatever the length",
    ),
    27: Part(
        "load address (hypothesis)",
        Outcome.DATA_UNKNOWN,
        "idled, dst gets foreign data",
    ),
    28: Part("completion", Outcome.TIMEOUT, _COMPLETION),
    31: Part(
        "store-side buffer address (hypothesis)",
        Outcome.DATA_UNKNOWN,
        "idled, dst gets data of an older job, 16 bytes off",
    ),
    45: Part(
        "completion",
        Outcome.TIMEOUT,
        f"{_COMPLETION}; its register field is don't-care (copies at all 31 values)",
        operand={r: Outcome.OK for r in range(31)},
    ),
    58: Part(
        "fixed-content load address",
        Outcome.DATA_UNKNOWN,
        "idled, dst gets a fixed 1 KiB",
    ),
    60: Part("stream completion", Outcome.TIMEOUT, _COMPLETION),
    61: Part(
        "src loader",
        Outcome.DATA_UNKNOWN,
        "idled, the job emits data held from an older 4 KiB job, then 0x5A",
    ),
    62: Part("link keeper", Outcome.HAZARD, "idled, the camera lost its link"),
    63: Part("stream completion", Outcome.TIMEOUT, _COMPLETION),
    64: Part("wedge guard", Outcome.HAZARD, "idled, the job wedged (status -5)"),
    65: Part("stream completion", Outcome.TIMEOUT, _COMPLETION),
    69: Part(
        "store side",
        Outcome.NO_STORE,
        f"{_STORE_PATH}; its register: dst not written at r0 and r4, a copy at the others",
        operand={r: Outcome.NO_STORE if r in (0, 4) else Outcome.OK for r in range(31)},
    ),
    79: Part("stream end", Outcome.TIMEOUT, _COMPLETION),
    **{i: _store_path() for i in (12, 13, 14, 16, 17, 21, 22, 23, 24, *range(32, 42))},
    **{
        i: Part("no effect seen", Outcome.OK, _NONE)
        for i in (1, 2, 10, 11, 18, 19, 20, 25, 26, 29, 30, 50, 51, 52, 53, 56, 59, 80)
    },
    **{i: Part("drain", Outcome.OK, _NONE) for i in (81, 82, 83)},
}

# -- the register transfers

ROUTES = tuple(f.id for f in FIELDS if f.lo in STAIRCASE)
CONTEXT = ("f100_104", "f105_109", "f110_114", "f123_127", "f128_132")
"""The register group A fields besides xreg: they select the transfer's channel."""
REGS_B = tuple(f.id for f in UNITS["regs_b"].fields if f.kind == "reg")
TRANSFER_OPERANDS = frozenset({*CONTEXT, "f115_119", *REGS_B, *ROUTES, "f308_309"})

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
                "the store's element format, applied by the store, not the load: bytes at r6, "
                "u8 -> u32 zero-extended at r14 and r29, a 10-bit pack or zeros at r21 (by "
                "boot), zeros at the others; timeout at r9; the camera lost its link at r16, "
                "r18, r19, r20, r24, r26, r27, r28 (not registered)",
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
                "stored) at 11 values; timeout at the T set; a fixed 1 KiB at r8",
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
                "to 32 bytes (towards zero, hypothesis for negative values)",
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
                "down to 32 bytes (hypothesis for negative values)",
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
    ),
)
"""Channels cmd 0xF does not need (`needed=False`): idling their transfers has no effect, with
len, dst and src changing between jobs. Word 57 is a holding transfer (bit 308 set) of its own
register context and takes no slot."""

# Word 47's formats by register; r21's is a boot parameter.
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
class TransferClass(WordClass):
    """The register transfers: they hand the register named in 115..119 to the sequencer."""

    channel_of: dict[tuple[int, ...], str] = field(default_factory=dict)
    reference: dict[tuple[int, ...], Reference] = field(default_factory=dict)
    holding: frozenset[int] = frozenset()
    """The indices of the stock holding transfers (bit 308 set)."""

    def matches(self, word: Word) -> bool:
        return super().matches(word) and word["f308_309"] in (0, 1)

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
        if word["f308_309"] == 1:
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


def _transfer_class(words: tuple[Word, ...]) -> TransferClass:
    """The transfer class, with the contexts and reference values of the stock transfers."""
    cls = TransferClass(
        "transfer",
        "register transfer: hands the register named in 115..119 to the stream "
        "configuration; its role (address, offset, count, enable, format) is set by its "
        "position [inferred, strong]",
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
                or w["f308_309"] != 0
                or context(w) != context(words[c.anchor])
            ):
                raise ValueError(
                    f"stock word {role.word} is no counted transfer of {c.name}"
                )
    roles = {r.word for c in _CHANNELS for r in c.roles}
    for i, w in enumerate(words):
        if cls.matches(w) and w["f308_309"] == 0 and i not in roles:
            raise ValueError(f"stock transfer {i} has no role")
    holding = frozenset(
        i for i, w in enumerate(words) if cls.matches(w) and w["f308_309"] == 1
    )
    return TransferClass(
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
    words: tuple[Word, ...], transfer: TransferClass
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
        operands = frozenset({"f115_119"}) if part.operand else frozenset()
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

    def result_taint(self) -> Confidence:
        """The confidence of the job's status and of whether and what it stored: that of the
        outcomes that decided it, else that of the load and the store."""
        deciding = self.deciding()
        if deciding:
            return min(self.cap, *(c for _, _, c in deciding))
        return min(self.load_taint, self.store_taint)


def _trunc32(v: int) -> int:
    """v / 32 truncated towards zero, to a multiple of 32."""
    q = abs(v) // 32 & ~0x1F
    return q if v >= 0 else -q


def _floor32(v: int) -> int:
    """v / 32 rounded down, to a multiple of 32."""
    return v // 32 & ~0x1F


class Copy16Model:
    """The model of program 16, built from the stock program (`from_image` reads it from a
    stock E-M10 II `.BIN`)."""

    def __init__(self, program: Program) -> None:
        self.program = program
        words = program.words
        self.transfer = _transfer_class(words)
        self.opaque = _opaque_classes(words, self.transfer)
        self.classes: tuple[WordClass, ...] = (IDLE_CLASS, *self.opaque, self.transfer)
        self.channels = _CHANNELS
        self.formats = _FORMATS

    @classmethod
    def from_image(cls, image: bytes) -> "Copy16Model":
        return cls(stock_programs(image)[ITEM])

    def resolve(
        self,
        trace: Trace,
        regs: RegisterFile,
        view: JobView,
        sequencer: Sequencer,
        edits: int,
    ) -> Plan:
        """The plan of a job of `edits` words that differ from the stock program."""
        length = view.length
        assert length is not None
        # The defaults are what every stock copy shows: load at src, offsets 0, count len, bytes.
        plan = Plan(count=length, cap=min(trace.taint, I if edits >= 2 else V))
        for c in self.opaque:
            part = c.part
            assert part is not None
            if trace.parts[c] < len(c.positions):
                at = ", ".join(str(i) for i in sorted(c.positions))
                plan.outcomes.append(
                    (
                        part.missing,
                        f"{part.token} (stock word {at}) missing: {part.fact}",
                        part.at(length),
                    )
                )
            elif part.operand is not None:
                index, reg = trace.operands[c]
                o = part.operand.get(reg)
                if o is None:
                    raise UnknownSemantics(index, c.name, [(lookup("xreg"), reg)])
                stock = reg == self.program.words[min(c.positions)]["xreg"]
                conf = V if stock else part.at(length)
                plan.outcomes.append((o, f"{part.token} at r{reg}: {o.value}", conf))
        filled, unset = sequencer.assign(self.channels, trace.channels)
        for slot in unset:
            plan.outcomes.append(
                (Outcome.TIMEOUT, f"unset {slot}: the stream never completes", H)
            )
        roles = []
        for role, t, assigned in filled:
            pre = min(assigned, t.taint)
            conf = min(pre, self._role(plan, role, t, pre, regs, view))
            roles.append((role, conf))
        base = min(plan.cap, trace.classes, *(c for _, _, c in plan.outcomes))
        plan.load_taint = min([base, *(c for r, c in roles if r.load)])
        plan.store_taint = min([base, *(c for r, c in roles if not r.load)])
        return plan

    def _role(
        self,
        plan: Plan,
        role: Role,
        t: Transfer,
        pre: Confidence,
        regs: RegisterFile,
        view: JobView,
    ) -> Confidence:
        """Apply `role` at the transfer's register; returns the confidence of what it did.

        An outcome it records gets `pre`, the confidence of the assignment, at most.
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
