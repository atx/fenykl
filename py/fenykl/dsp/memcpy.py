"""Program 16, the stock copy (command 0xF): its model, built from the stock program at runtime.

The driver passes src, dst and len in DSP local memory words 0..2. No word of the program is
decoded; what is known comes from camera runs that replaced a word by the idle word ("idled") or
swept one of its fields (the register field 115..119, word 60's f76_79), and is stated here by word
index:

- `_PARTS`: what the job does without each undecoded stock word;
- `_CHANNELS`: the register-transfer words, grouped by their register context, and the role of
  each slot (load address, offsets, count, store format, ...);
- `_FORMATS`: word 47's store formats by register.

`Copy16Model` takes the stock program (from the user's firmware, `from_image`) and derives the
word classes from it: each undecoded stock word is a class matched by its value, the register
transfers one class matched by their signature, with the register contexts and the register
group B and route values of the stock transfers as the reference. No stock word is part of this
code. The model resolves a job's trace into its one load and one store (`resolve`) and applies
them to the boot (`apply`):

    load:   buffer[i + o68] = S[i] for i in [0, len), S read at word 67's address
    store:  dst[i] = format(buffer)[i + o76] for i in [0, n73); dst[n73, len) not written

A job that does not complete stores nothing. A placement out of range, a register of unknown
value or a load that may or may not have run makes its bytes UNKNOWN.

Hazards: idling word 62 lost the camera's link and idling 64 or 72 failed the job with status
-5; these register values lost the link too: word 44 at 0, 1, 4, 8, 10; word 47 at 16, 18, 19,
20, 24, 26, 27, 28; word 70 at 26; word 71 at 0.
"""

from dataclasses import dataclass, field

from .emu import (
    HALT_OP,
    Boot,
    Core,
    Emulator,
    Job,
    JobResult,
    Status,
    Stop,
    dram_alias,
    start_copy,
)
from .fields import FIELDS, STAIRCASE, TARGET_OPS, Confidence, Kind, lookup
from .program import Program, stock_programs
from .semantics import (
    IDLE_CLASS,
    KIB,
    Channel,
    Given,
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
from .state import Bytes, JobView, RegisterFile
from .units import UNITS
from .word import IDLE, Word

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED

ITEM = 16
MAX_COUNT = 0x40000
"""A count above this is an address or junk: the store never ends (hypothesis)."""
ZERO_FILL = 0x4000

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
        seen=(KIB, 4 * KIB),
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
    **{i: Part("return delay slot", Outcome.OK, _NONE) for i in (81, 82, 83)},
}

# -- the register transfers

ROUTES = tuple(f.id for f in FIELDS if f.lo in STAIRCASE)
CONTEXT = ("f100_104", "f105_109", "f110_114", "f123_127", "f128_132")
"""The register group A fields besides xreg: they select the transfer's channel."""
REGS_B = tuple(f.id for f in UNITS["pr"].fields if f.kind == "reg")
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
        stalls=(78,),
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
        "configuration; its role (address, offset, count, enable, format) is given by the "
        "sequencer, by transfer order [inferred, strong for moves within 73..77; a counted "
        "transfer at 78 stalls the job, see `Sequencer`]",
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
        operands = (
            frozenset({lookup(part.operand_field).id}) if part.operand else frozenset()
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

    def apply(self, emu: Emulator, job: Job, trace: Trace) -> JobResult:
        """Resolve the job and apply its one load and one store to the boot."""
        boot = emu.boot
        view = start_copy(boot)
        stock = self.program.words
        words = job.program.words
        edits = sum(a != b for a, b in zip(words, stock)) + abs(len(words) - len(stock))
        halted = self._halt(job, trace, view)
        if halted is not None:
            return halted
        plan = self.resolve(trace, boot.regs, view, emu.sequencer, edits)
        return self._apply(boot, plan, trace, view)

    def _halt(self, job: Job, trace: Trace, view: JobView) -> JobResult | None:
        """The result of a job that an op 7 off the stock halt ends before the stream: status 0,
        dst not written (word 5 in place of 11 on the camera [verified]; elsewhere inferred, or
        weaker through the branches before it); None when no such halt runs. The halt word
        itself is classified as any word: word 5's value runs as its class anywhere."""
        stock = {i for i, w in enumerate(self.program.words) if w.op == HALT_OP}
        words = job.program.words
        if all(w.op != HALT_OP or i in stock for i, w in enumerate(words)):
            return None
        length = view.length
        assert length is not None
        flow = Core(Boot(), copy_branches(length), strict=False).run(job, start_copy)
        if flow.stop is not Stop.HALT:
            raise UnknownSemantics(0, "halt", [], f"the job does not halt: {flow.why}")
        at = flow.steps[-1].index
        if at in stock:
            return None
        if any(s.index in STREAM for s in flow.steps):
            raise UnknownSemantics(
                at,
                "halt",
                [],
                "a halt after the stream started: whether the stream drains first is open",
            )
        # Words after the halt do not run; edited words before it are not modelled.
        stock_words = self.program.words
        edited = any(
            s.index >= len(stock_words) or words[s.index] != stock_words[s.index]
            for s in flow.steps[:-1]
        )
        halt_word = self.program.words[min(stock)]
        seen = at in HALTS_SEEN and length == KIB and words[at] == halt_word
        conf = H if trace.ignored or edited else V if seen else I
        for s in flow.steps:
            if s.op in TARGET_OPS[:3] and s.confidence is not None:
                conf = min(conf, s.confidence)
        why = f"op 7 at word {at} halts the job before the stream: {Outcome.NO_STORE.value}"
        result = JobResult(Status.OK, [why], ignored=trace.ignored, notes=trace.notes)
        result.taint = conf
        return result

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
                index, value = trace.operands[c]
                f = lookup(part.operand_field)
                o = part.operand.get(value)
                if o is None:
                    raise UnknownSemantics(index, c.name, [(f, value)])
                if value == f.get(self.program.words[min(c.positions)].value):
                    continue
                at = f"r{value}" if f.kind is Kind.REG else f"{f.label}={value:#x}"
                why = f"{part.token} at {at}: {o.value}"
                plan.outcomes.append((o, why, part.at(length)))
        filled, stalls = sequencer.assign(self.channels, trace.channels)
        for why, conf in stalls:
            plan.outcomes.append(
                (Outcome.TIMEOUT, f"{why}: the stream never completes", conf)
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

    def _apply(self, boot: Boot, plan: Plan, trace: Trace, view: JobView) -> JobResult:
        if plan.has(Outcome.HAZARD):
            status = Status.HAZARD
        elif plan.has(Outcome.TIMEOUT):
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
        stores = status is Status.OK and not plan.has(Outcome.NO_STORE)
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

    def _load(self, boot: Boot, plan: Plan, view: JobView, certain: bool) -> None:
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

    def _format(self, boot: Boot, fmt: str, o76: int, n: int) -> Bytes:
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

    def _buffer(self, boot: Boot, o: int, n: int) -> Bytes:
        if not boot.buffer.contains(o, n):
            return Bytes.unknown(
                n, boot.source("store not modelled", "outside the modelled buffer")
            )
        return boot.buffer.read(o, n)

    def _pack10(self, boot: Boot, o76: int, n: int, zero: int) -> Bytes:
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


SEEN_LENGTHS = range(0x200, 0x4001, 0x10)
"""The lengths within the range and alignment of the copies run on the camera: 0.5..16 KiB in
whole 16-byte lines."""


def copy_branches(length: int) -> Given:
    """The decisions of the copy's two forward branches as the camera runs show them, for the
    stepping core: 27 (to the return 79) not taken (inferred: the store words after it act) and
    45 (over the loop 50..57) taken (inferred, strong: idling its delay slots 46..49 times the
    job out, idling 50..57 has no effect). The loop's back-edge 53 is never reached; inferred
    at the lengths the runs had, a hypothesis at others."""
    seen = length in SEEN_LENGTHS
    return Given(
        {27: False, 45: True},
        Confidence.INFERRED if seen else Confidence.HYPOTHESIS,
        "as in the copies run on the camera"
        + ("" if seen else ", at other lengths than this one"),
    )
