"""The field table of the 320-bit DSP word: the single source of truth for encoding.

Bit k of a word is bit (k % 8) of byte k // 8 of its 40 bytes. Every bit belongs to exactly one
field. A field has a permanent positional id that names both ends of its bits (`f115_119`, `f229`),
an optional semantic name that may be renamed at will, a kind, its value in the
idle word, a confidence and a one-line fact.

Confidence qualifies the field itself: that these bits form one field of this kind. Facts about
single values or words carry their own tags in `fact` and `values`. The emulator does not use
it: it propagates the confidence of the semantics a result depends on (see `semantics`).

Bits 313..319 are one op field in every word (`OPS` names its values). In a control word, op
0..4, bits 15..22 and 128..132 hold the target T, which `TARGET` composes from the positional
fields f15_22 and f128_132; those fields stay as they are in every word, so the table is one
partition of the bits for all words. The sequencer rule that spans words, four delay slots after
every control op, is `DELAY_SLOTS`.

A data op 8..73 holds its operands in the op slot: the register ra (f123_127), then f128_132 as a
register rb or as bits 8..12 of the immediate, f120_122 as an operand or as bits 13..15. Which,
and how wide the immediate is, is the op's family (`FAMILIES`, `FORMATS`); as with T, the
operands compose from the positional fields.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum

WORD_BITS = 320
WORD_BYTES = WORD_BITS // 8


class Kind(StrEnum):
    REG = "reg"  # a register specifier r0..r31
    OPCODE = "opcode"  # an operation or enable code
    ROUTE = "route"  # a source or path select
    FLAG = "flag"  # a single control bit
    CONST = "const"  # always the same value in every stock word
    IMM = "imm"  # an immediate operand
    RAW = "raw"  # bits not decoded yet


class Confidence(IntEnum):
    """Ordered so that min() of several is the weakest."""

    HYPOTHESIS = 0
    INFERRED = 1
    VERIFIED = 2

    @property
    def mark(self) -> str:
        """Listing mark; hypothesis, the common case, stays unmarked."""
        return {0: "", 1: "~", 2: "!"}[self.value]


H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


@dataclass(frozen=True)
class Field:
    lo: int
    hi: int
    kind: Kind
    unit: str
    idle: int
    confidence: Confidence
    fact: str
    name: str = ""
    values: Mapping[int, str] = field(default_factory=dict)
    """Known values and their meaning (documentation, not a restriction)."""
    allowed: tuple[int, ...] | None = None
    """The only values an assembled word may hold, when known."""

    @property
    def id(self) -> str:
        if self.lo == self.hi:
            return f"f{self.lo}"
        return f"f{self.lo}_{self.hi}"

    @property
    def label(self) -> str:
        """The semantic name, or the id while there is none."""
        return self.name or self.id

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    def literal(self, value: int) -> str:
        """`value` as a Python literal: decimal for registers, hex otherwise."""
        return str(value) if self.kind is Kind.REG else f"{value:#x}"

    def get(self, word: int) -> int:
        return (word >> self.lo) & self.mask

    def put(self, word: int, value: int) -> int:
        if not 0 <= value <= self.mask:
            raise ValueError(f"{self.label}: {value:#x} does not fit {self.width} bits")
        return (word & ~(self.mask << self.lo)) | (value << self.lo)

    def check(self, value: int) -> None:
        """Raise ValueError unless `value` is one an assembled word may hold."""
        if not 0 <= value <= self.mask:
            raise ValueError(f"{self.label}: {value:#x} does not fit {self.width} bits")
        if self.allowed is not None and value not in self.allowed:
            raise ValueError(f"{self.label}: {value:#x} not in {self.allowed}")


def _const(lo: int, hi: int) -> Field:
    ones = (1 << (hi - lo + 1)) - 1
    return Field(
        lo,
        hi,
        Kind.CONST,
        "const",
        ones,
        V,
        "always all-ones in every word of the five stock programs, and across later firmware "
        "versions of the same family but for one word that may be data",
        allowed=(ones,),
    )


IDLE_OP = 0x7F
SEGMENTED = "boundary from a value-entropy segmentation of the stock programs"


def _ctl(
    lo: int, hi: int, unit: str, idle: int, fact: str = "", basis: str = SEGMENTED
) -> Field:
    kind = Kind.FLAG if lo == hi else Kind.RAW
    base = f"control bits; {basis}"
    return Field(lo, hi, kind, unit, idle, H, f"{base}. {fact}" if fact else base)


GRID_FACT = (
    "grid field, a source or path select (all-ones = none); 0 is its most common value in "
    "the compute sections; boundaries from segmentation and the lag-110 repeat of grid A "
    "in grid B"
)
# Program 16 sets these grid nibbles to 0 in a staircase and holds them to its end.
STAIRCASE = {194: 19, 84: 20, 198: 29, 88: 30, 182: 59, 76: 60}


GRID_PAIR = (
    "2-bit grid field, a source or path select, at lag 110 like grids A and B; all-ones in every "
    "word of the five stock programs, cleared in hundreds of words of some programs across later "
    "firmware versions of the same family"
)


def _grid(lo: int, hi: int, unit: str, extra: str) -> Field:
    fact = GRID_PAIR if hi - lo == 1 else GRID_FACT
    if lo in STAIRCASE:
        fact += (
            f"; program 16 sets it to 0 from word {STAIRCASE[lo]} on, held to the end "
            "(the plain pass-through a copy needs, hypothesis)"
        )
    if lo == 76:
        fact += f"; {F76_FACT}"
    fact += f". {extra[0].upper()}{extra[1:]}"
    return Field(
        lo,
        hi,
        Kind.ROUTE,
        unit,
        (1 << (hi - lo + 1)) - 1,
        H,
        fact,
        values={0xF: "none"} if hi - lo == 3 else {},
    )


# The register groups that differ between program 0x10's words 308..458 and 802..952.
KERNEL_REMAPPED = (110, 115, 123, 128, 211, 216, 224)


def _reg(
    lo: int,
    unit: str,
    idle: int,
    fact: str = "",
    name: str = "",
    confidence: Confidence = H,
) -> Field:
    base = (
        "5-bit register specifier: its bit probabilities fall from the low to the high bit, "
        "the signature of small integers"
    )
    if lo in KERNEL_REMAPPED:
        base += (
            "; remapped between the two copies of program 0x10's kernel, which are "
            "otherwise identical"
        )
    fact = f"{base}. {fact}" if fact else base
    return Field(lo, lo + 4, Kind.REG, unit, idle, confidence, fact, name=name)


# Program 16 never moves the C2 bits outside this set.
P16_C2_BITS = (
    255,
    260,
    261,
    262,
    265,
    266,
    267,
    268,
    269,
    272,
    273,
    274,
    275,
    276,
    278,
    280,
    281,
    283,
    285,
    286,
    288,
    290,
    292,
)
WORD504 = (
    "program 13 word 504 has bits of this subfield clear; idling any one of 296, 297, 299, 300 "
    "moves its last plane's horizontal sample to x + 14/16 at b = 1, as idling all of C2's "
    "free bits does (verified on the camera)"
)
WORD504_NONE = (
    "program 13 word 504 has bits of this subfield clear; idling 237, 239 or 254 alone changed "
    "no byte (verified on the camera, an x-varying source at b = 1)"
)
RARE = (
    "rarely used: all-ones in every word of the five stock programs, clear in 1..12 words of "
    "some programs across later firmware versions of the same family"
)
CONTROL_FACT = (
    "clear exactly in the words whose op (f313_319) is 0..4, the control ops, set in all others "
    "(verified in the bytes of the five stock programs and across later firmware versions of the "
    "same family); a control op 0..3 holds its target T in bits 15..22 and 128..132"
)
IMM_FACT = (
    "the 8-bit immediate of the op in f313_319: 0 in every word whose op is idle (verified in the "
    "bytes of the five stock programs and across later firmware versions of the same family); in "
    "a control op 0..3 it is the low byte of the target T (verified in the bytes); in a data op the "
    "low bits of an immediate 5, 13, 15 or 16 bits wide by op, and 0 in every register-form op "
    "(verified in the bytes, `FAMILIES`)"
)
T_HIGH = (
    "in a control op 0..3 bits 8..12 of the target T, not a register (verified in the bytes); in a "
    "return mostly 1 (in every return of the five stock programs but three, which hold 2, 3 and "
    "7), meaning open. In a data op it is the register rb of the register forms (families A', C, "
    "E) or bits 8..12 of the immediate (families A, B, D, F) (verified in the bytes across later "
    "firmware versions of the same family: in 31878 of 32911 idle-op words it holds the last "
    "register-form word's value, which survives the immediate forms in between); in family A "
    "its bits 11..12 (imm13 bits 11..12 of a word op, bit 12 of a halfword op) are a flag that "
    "does not move the address on r0 (inferred). In families "
    "A', C and E a source that stays fixed across consecutive register-form ops while ra changes "
    "(same rb with another ra in 59 % of such pairs, 10 % by chance), live into the routine more "
    "often than chance; a register named as ra reaches a later rb no more often than chance "
    "(verified in the bytes across later firmware versions of the same family)"
)
RA_FACT = (
    "the op slot's register ra: it changes in 1 of 33175 idle-op words and in 19884 of 33290 "
    "data-op words across later firmware versions of the same family, so only a word that "
    "carries an op writes it, a data op or a control op 0..2, while a call holds it (verified "
    "in the bytes). Its role by family: in A (0x0c..0x1a even) an address base the op reads, "
    "imm13 the offset (inferred: in 58..74 % of A's namings the next reference to the register "
    "is the same op naming it again with another immediate, which as writes would be dead); "
    "0x0b names it just before family A uses it (24 % against 7 % by chance), whether as a "
    "write or a read is open (hypothesis); in B, C, D, E and F a source operand that the "
    "result does not go to: the result goes to the register of the word's enabled PR port "
    "(inferred, strong on the camera: the result reached the port's register and not ra); in "
    "ops 0..2 a loop or condition operand, the same register in a guard and "
    "its loop end in 47 % of the pairs against 13 % by chance (verified in the bytes for the "
    "count, the reading inferred). Counted across later firmware versions of the same family"
)
XREG_FACT = (
    "In program 16 the register-transfer words name the register they use here: as a load "
    "address (word 67), a buffer offset (68 load side, 76 store side), a store count (73), "
    "an enable (42, 66, 69, 70) or the store element format (47) (verified on the camera "
    "per word); r3 holds the job source address. xreg and the op slot's ra and rb read two "
    "register sets (inferred, strong): on the camera a data op's result written through PR "
    "port 267 (f211_215 under f267_268 = 1) reached the xreg of words 68 and 73 and never an "
    "operand, while one written through port 272 under enable 1 (f224_228) reached a later "
    "op's ra and rb and not word 68's xreg; the operand r7 just after program 16's word 64 "
    "held a value near 0x40000, not the len / 2 that 64 writes through port 267 and that 68 "
    "and 73 read at r7. Per routine the registers xreg changes to and those the op slot names "
    "overlap only slightly more than chance (Jaccard 0.51 against 0.44 across later firmware "
    "versions of the same family). The emulator keeps the two apart (`Boot.regs`, "
    "`Boot.slot`)"
)
FLAGS_FACT = (
    "3 bits between the register specifiers; bits 120..121 are 0 in every op 0..3 word and bit 122 "
    "is a flag in them, set in some words of each control op and mostly constant within a routine "
    "(verified in the bytes across later firmware versions of the same family), its meaning open. "
    "In a data op bits 13..15 of the immediate in ops 0x0a, 0x0b, 0x38 and 13..14 in 0x37 "
    "(verified in the bytes); in family D bits 13..15 of a signed 16-bit immediate (inferred, "
    "strong: on the camera, values 1 and 7 moved the constant by 0x2000 and -0x2000 in units "
    "of 16, as predicted); 0 in 99 % of family D's stock words, small positive immediates. An "
    "operand of open meaning in families A, A', C and E, where it varies apart from the "
    "immediate (verified in the bytes); in 0x47 it selects among hidden per-boot values "
    "(inferred), and the E rules measured on the camera hold at 7"
)
SELECTOR = (
    "the loop-end selector pair f270_271 / f272_273: in every op 0 word of the five stock programs exactly "
    "one of f270_271 and f272_273 is 1, and in 386 of 395 across later firmware versions of the "
    "same family; ops 1..3 mostly hold 3 in both; a loop counter select (hypothesis)"
)
OP_FACT = (
    "the op, one 7-bit field in every word (verified in the bytes of the five stock programs and "
    "across later firmware versions of the same family): 0..4 the control ops with f229 clear, 7 "
    "halt, 8..73 data ops, 127 idle; 5, 6, 74..126 and the data op values 0x22, 0x26, 0x27, "
    "0x2d, 0x39, 0x3b and 0x45 never occur. Bits 17, 18 set with 128, "
    "273, 318 and 319 clear, once read as a control signature, is a coincidence of fields that "
    "several ops share; the word it marks at the end of programs 6, 0xC and 0xF is data op 0x1f "
    "in a return's delay slot, its 13-bit immediate (f15_22 | f128_132 << 8) a multiple of 4 in all "
    "300 such words across later firmware versions of the same family (inferred)"
)
XFER_BASIS = (
    "cut from 308..312 (the segmentation's field) at the register-transfer signature"
)
XFER = (
    "0 in the register-transfer words; their role (load address, offset, count, enable, store "
    "format) is given by the sequencer, not by an opcode, in the order of the transfers "
    "(inferred): in program 16 the count role followed its transfer from 73 to 74 and the roles "
    "of 75..77 followed theirs one index earlier, while a counted transfer at 78, the word "
    "before the return 79, stalls the job (verified on the camera, one run each, the stall on "
    "two boots); why 78 fails is open (hypothesis)"
)


CONDITION = (
    "the condition is no field of its own; the candidate is the register ra (f123_127), a loop "
    "or condition operand that a guard and its loop end share in about half the guarded loops, "
    "named in its own loop body less often than chance: a count held or decremented outside "
    "the op slot (hypothesis); program 16's guard 45 is taken in every job tested on the "
    "camera, also with each of src, dst, len and their sums and difference odd, so it tests no "
    "low bits of a job value (inferred)"
)


@dataclass(frozen=True)
class Op:
    """A meaning of a value of the op field f313_319."""

    name: str
    confidence: Confidence
    fact: str
    format: "Format | None" = None
    """The operand layout of a data op in the op slot; None for the other ops."""


OPS: dict[int, Op] = {
    0: Op(
        "loop",
        I,
        "loop end: back to T, at or before the word in the five stock programs (some short "
        "bodies across later firmware versions of the same family have T up to 3 words after "
        "it); loop ends nest and never cross; a counted loop's back-edge (inferred, strong)",
    ),
    1: Op(
        "brf",
        I,
        "forward conditional branch, T always after it; the guard of a loop, at the loop's T - 5 "
        "with its own T at the loop's exit (inferred); " + CONDITION,
    ),
    2: Op(
        "br",
        I,
        "conditional branch either way, the back-edge of most guarded loops (inferred); "
        + CONDITION,
    ),
    3: Op(
        "call",
        I,
        "call: when a return lies between the word and T, the word at i + 5 is almost never a "
        "branch target (11 of 950 across later firmware versions of the same family), so a "
        "return reaches it (inferred, strong); program 16's word 0 calls its job routine at 10; "
        "the op 3 words with no return before T (local, `Program.local_op3`) are open, a jump "
        "or a call: program 13's at word 210 runs in a job (verified on the camera: a halt "
        "there cuts the job) and acts as a jump (inferred: as a call its link would re-run the "
        "job's inner loop, longer than the measured job), its twin at 312 by analogy "
        "(hypothesis); `Executor(local_op3_jump=True)` steps them as jumps",
    ),
    4: Op(
        "ret",
        I,
        "return to the word after the call's delay slots (inferred, strong); it has no target",
    ),
    7: Op(
        "halt",
        I,
        "ends the job: word 5 of every stock program and no other word, where the call at word "
        "0 returns (inferred); at any word and call depth: program 16 with word 5 in place of "
        "word 11, inside the routine with the call's return pending, ended with status 0 and "
        "dst not written (verified on the camera, one run), so a halt needs no return and the "
        "return stack is not checked (inferred); whether it has delay slots is open",
    ),
    IDLE_OP: Op(
        "idle", I, "no op: the idle word's value, its immediate always 0 (inferred)"
    ),
}
CONTROL_OPS = range(5)
"""The control ops, the words with f229 clear."""
DATA_OPS = range(8, 74)
DATA_OP_CONFIDENCE = I
DATA_OP_FACT = (
    "a data op: it leaves f229 set and holds its operands in the op slot (`FAMILIES`); what the "
    "measured ones compute is `semantics.OP_SEMANTICS`, their result going to the register of "
    "the word's enabled PR port (inferred)"
)
UNSEEN_OP_FACT = (
    "never occurs in the stock programs nor across later firmware versions of the "
    "same family"
)


def op(value: int) -> Op:
    """The meaning of op value `value`; data ops and unseen values are named op<hex>."""
    if value in OPS:
        return OPS[value]
    name = f"op{value:02x}"
    if value in FORMATS:
        fmt = FORMATS[value]
        fact = f"{DATA_OP_FACT}; family {fmt.family.name} ({FAMILY_BASIS}): {fmt.family.fact}"
        if value in MEANINGS:
            fact += f"; {MEANINGS[value]}"
        return Op(name, DATA_OP_CONFIDENCE, fact, fmt)
    return Op(name, H, UNSEEN_OP_FACT)


DELAY_SLOTS = 4
DELAY_FACT = (
    "the four words after a control op are its delay slots: in the five stock programs none of "
    "them is a target or a control word (verified in the bytes), and across later firmware "
    "versions of the same family only short loop ends break it; idling program 16's slots "
    "46..49 of the branch 45 (taken in a copy) times the job out, while idling 50..57, "
    "which it skips, has no effect (verified on the camera). Reading: the slots always run and "
    "the transfer takes effect after them, so a call returns to i + 5 (inferred, strong)"
)


def _c2_fact(lo: int, hi: int) -> str:
    moved = [b for b in P16_C2_BITS if lo <= b <= hi]
    return (
        f"program 16 moves bits {', '.join(map(str, moved))}"
        if moved
        else "program 16 never moves it"
    )


# The units (`units.UNITS`) come from which strobe is active in the words where a held field
# changes, counted over every word of the programs across later firmware versions of the same
# family: a field's owner is the strobe most often active there, P the share of its changes
# in which it is. The C2 strobes 232..312 change about as often as they are active (pulses);
# the grid and register fields keep their value for runs of words (held).
OWNERSHIP = "across later firmware versions of the same family (verified in the bytes)"


def _only(where: str) -> str:
    return (
        f"active only in words where {where} active, across later firmware versions of the same "
        "family (verified in the bytes)"
    )


X0_REGS = "a register operand of X0 (hypothesis)"
X0_PATH = (
    "The residual changes of its nibble fall in data-op words with f302_307 active: the nibble is "
    "also written on the X0 path (hypothesis)"
)
X0_FACT = (
    "the strobe of X0: f1 and f2_6 are active only where it is; f69_71 and f100_104 change only "
    f"where it is active, f105_109 in 0.71 of its changes, {OWNERSHIP}"
)
X1_FACT = (
    "the strobe of X1: f7_8 and f9_14 are active only where it is; f110_114 changes where it is "
    f"active in 0.78 of its changes, {OWNERSHIP}"
)
X2_FACT = (
    f"{XFER}; in program 16 words 58..79, bit 308 clear marks the words that stall the job when "
    "idled. The strobe of X2, a value rather than an on/off strobe: xreg changes in 0.81 of the "
    f"words at 0, 0.58 at 1 and 0.61 at 2 against 0.27 overall, {OWNERSHIP} (inferred, weak)"
)
GRID_B_FACT = (
    "grid B follows grid A at +110 in which words are active, but its values equal the +110 "
    "partner's mostly only at 145, 179, 190, 194, 198 and 202, and its nibbles have one enable "
    "each where grid A's are operands of the S heads (verified in the bytes across later firmware "
    "versions of the same family): not a mirror bank (inferred); a held nibble under an idle "
    "enable is don't care at its idle value (verified on the camera at program 13 words 433 and "
    "439, 25 nibbles idled in two sets; at other values inferred)"
)
QUAD = (
    "Q, a section mode of the grid (hypothesis): f35_36 and f145_146 are set only in some "
    "programs across later firmware versions of the same family, never on the E-M10 II, equal "
    "in all words where both are set but 14, set over long runs and changed only where X0 is "
    "active; where set, f65_68, f69_71 and f179_181 are active and f80_87 / f190_197 mostly idle"
)
F76_FACT = (
    "in program 16 word 60, the one word where S0 is active, the copy needs it at 0: each of "
    "1..15 times the job out before any store (verified on the camera, one run each)"
)
PARTNER_FACT = (
    "It changes to the op slot's ra register in the same word (0.157 of its changes) or one word "
    "ahead (0.116), more often than one word behind (0.062), unlike the other register fields "
    "(verified in the bytes across later firmware versions of the same family): an operand "
    "select of the op slot (hypothesis)"
)


GRID_A = [
    (23, 30),
    (31, 34),
    (35, 36),
    *((lo, lo + 3) for lo in range(37, 68, 4)),
    (69, 71),
    *((lo, lo + 3) for lo in range(72, 99, 4)),
]
# Grid B repeats grid A's 31..95 at +110.
GRID_B = [(lo + 110, hi + 110) for lo, hi in GRID_A if 31 <= lo and hi <= 95]

HEADS = {
    "s0": (278, 284),
    "s1": (285, 291),
    "s2": (292, 295),
    "s3": (296, 301),
    "x0": (302, 307),
    "x1": (310, 312),
    "x2": (308, 309),
}
"""The strobe of each compute slot S0..S3 and transfer unit X0..X2: the C2 head or control
field whose activity the unit's operands change under."""

# Grid A: the unit of each field, and the share of its changes in words where the unit's head
# is active.
GRID_A_UNITS: dict[int, tuple[str, float]] = {
    23: ("s0", 0.74),
    31: ("s3", 0.87),
    37: ("s0", 0.91),
    41: ("s0", 0.98),
    45: ("s1", 0.98),
    49: ("s1", 0.98),
    53: ("s2", 0.78),
    57: ("s2", 0.97),
    61: ("s3", 0.96),
    65: ("s3", 0.96),
    69: ("x0", 1.00),
    72: ("s0", 0.95),
    76: ("s0", 0.78),
    80: ("s1", 0.79),
    84: ("s1", 0.69),
    88: ("s2", 0.78),
    92: ("s3", 0.85),
    96: ("s3", 0.86),
}
WEAK_OWNER = 0.9
"""Below this share the field's unit is inferred: its other changes fall in words where
another unit is active too."""

# The PB ports: (enable lo, hi, the grid B field it owns, P); the enable is active only where
# its nibble is not idle (in 99..100 % of its active words).
B_PORTS = (
    (232, 233, 141, 0.96),
    (236, 237, 147, 0.98),
    (238, 239, 151, 0.98),
    (240, 241, 155, 0.97),
    (242, 243, 159, 0.98),
    (244, 245, 163, 0.94),
    (246, 247, 167, 0.93),
    (248, 249, 171, 0.96),
    (250, 251, 175, 0.96),
    (252, 254, 179, 0.92),
    (255, 255, 182, 0.91),
    (256, 257, 186, 0.93),
    (258, 258, 190, 0.79),
    (259, 260, 194, 0.75),
    (261, 262, 198, 0.88),
    (263, 264, 202, 0.94),
)
# The PR ports: (enable lo, hi, the register group B field it owns, P).
R_PORTS = (
    (265, 266, 206, 1.00),
    (267, 268, 211, 1.00),
    (269, 269, 216, 0.96),
    (270, 271, 221, 0.96),
    (272, 273, 224, 0.99),
)


def _fid(lo: int, hi: int) -> str:
    return f"f{lo}" if lo == hi else f"f{lo}_{hi}"


def _width_id(lo: int) -> str:
    """The id of the grid or register field that starts at bit `lo`."""
    for a, b in GRID_A + GRID_B:
        if a == lo:
            return _fid(a, b)
    hi = 223 if lo == 221 else lo + 4
    return _fid(lo, hi)


PORTS: dict[str, str] = {
    _fid(lo, hi): _width_id(owned) for lo, hi, owned, _ in (*B_PORTS, *R_PORTS)
}
"""The PB and PR enables and the field each one owns: the field changes mostly in words where
its enable is active."""
_PORT_P = {_width_id(owned): p for _, _, owned, p in (*B_PORTS, *R_PORTS)}
_OWNER = {owned: enable for enable, owned in PORTS.items()}


def _enable(
    lo: int,
    hi: int,
    unit: str,
    idle: int,
    fact: str = "",
    values: Mapping[int, str] | None = None,
) -> Field:
    owned = PORTS[_fid(lo, hi)]
    base = (
        f"enable of {owned}: {owned} changes in words where it is active in "
        f"{_PORT_P[owned]:.2f} of its changes, and it is active only where {owned} is not idle, "
        f"{OWNERSHIP}; {_c2_fact(lo, hi)}"
    )
    fact = f"{base}. {fact}" if fact else base
    return Field(lo, hi, Kind.OPCODE, unit, idle, I, fact, values=values or {})


def _head(unit: str, idle: int, fact: str) -> Field:
    lo, hi = HEADS[unit]
    base = (
        f"head of compute slot {unit.upper()}: its grid A operands change mostly in words "
        f"where it is active, {OWNERSHIP}; {_c2_fact(lo, hi)}"
    )
    return Field(lo, hi, Kind.OPCODE, unit, idle, I, f"{base}. {fact}")


def _grid_a(lo: int, hi: int) -> Field:
    if (lo, hi) == (35, 36):
        return _grid(lo, hi, "q", QUAD)
    unit, p = GRID_A_UNITS[lo]
    head = _fid(*HEADS[unit])
    tag = "verified in the bytes" if p >= WEAK_OWNER else "inferred"
    fact = (
        f"operand of {unit.upper()}: it changes in words where {head} is active in {p:.2f} "
        f"of its changes ({tag}); don't care while {head} is idle (hypothesis)"
    )
    return _grid(lo, hi, unit, fact)


def _grid_b(lo: int, hi: int) -> Field:
    fid = _fid(lo, hi)
    if fid == "f145_146":
        return _grid(lo, hi, "q", QUAD)
    return _grid(lo, hi, "pb", f"the port of enable {_OWNER[fid]}; {GRID_B_FACT}")


def _port_reg(lo: int, idle: int, fact: str = "", confidence: Confidence = H) -> Field:
    owned = _width_id(lo)
    port = f"Written under enable {_OWNER[owned]} (hypothesis: a register write port)"
    return _reg(
        lo, "pr", idle, f"{port}. {fact}" if fact else port, confidence=confidence
    )


FIELDS: tuple[Field, ...] = (
    _ctl(
        0, 0, "s1", 0, f"program 16 never sets it; {_only('f285_291 and f49_52 are')}"
    ),
    _ctl(1, 1, "x0", 0, f"{_only('f302_307 is')}"),
    _ctl(2, 6, "x0", 0, f"{_only('f302_307 is')}; an immediate of X0 (hypothesis)"),
    _ctl(7, 8, "x1", 0, f"{_only('f310_312 is')}"),
    _ctl(
        9,
        14,
        "x1",
        0,
        f"program 16 never sets bits 11 and 14; {_only('f310_312 is')}; an immediate of X1 "
        "(hypothesis)",
    ),
    Field(15, 22, Kind.IMM, "op_slot", 0, V, IMM_FACT, name="imm"),
    *(_grid_a(lo, hi) for lo, hi in GRID_A),
    _reg(100, "x0", 31, f"{X0_REGS}; it changes only where f302_307 is active"),
    _reg(
        105,
        "x0",
        31,
        f"{X0_REGS}; it changes where f302_307 is active in 0.71 of its changes",
    ),
    _reg(
        110,
        "x1",
        31,
        "it changes where f310_312 is active in 0.78 of its changes, f308_309 0.67 (inferred, "
        "weak: the X1 / X2 split of f110_114 and xreg)",
    ),
    _reg(115, "x2", 31, XREG_FACT, name="xreg", confidence=I),
    Field(120, 122, Kind.RAW, "op_slot", 7, H, FLAGS_FACT),
    _reg(123, "op_slot", 31, RA_FACT, name="ra", confidence=V),
    _reg(128, "op_slot", 31, T_HIGH),
    _const(133, 140),
    *(_grid_b(lo, hi) for lo, hi in GRID_B),
    _port_reg(
        206,
        0,
        "Flipping its bits in program 16 words 67 and 68 leaves the copy unchanged (verified on "
        "the camera)",
    ),
    _port_reg(
        211,
        0,
        "Flipping its bits in program 16 words 67 and 68 leaves the copy unchanged (verified on "
        "the camera); the only difference between words 66..68. Under f267_268 = 1 the "
        "register the word's data op result goes to, in the set the X2 xreg reads (inferred, "
        "strong: on the camera, ops of families B, C, D and E inserted after program 16's word "
        "64 with f267_268 = 1 set the register it names to their result, which word 68 or 73 "
        "then read through its xreg, at pre-registered values; program 16's word 64 writes its "
        "count r7 so)",
        confidence=I,
    ),
    _port_reg(216, 31),
    Field(
        221,
        223,
        Kind.RAW,
        "pr",
        7,
        H,
        "3 bits between the register specifiers; written under enable f270_271; equal to f120_122 "
        f"in 66 % of all words, {OWNERSHIP}",
    ),
    _port_reg(
        224,
        0,
        f"{PARTNER_FACT}. Under f272_273 = 1 the register the word's data op result goes to, "
        "in the set ra and rb read (inferred, strong: on the camera, a constant written so "
        "was a later op's operand three words on, at pre-registered values, and word 68's "
        "xreg did not see it)",
        confidence=I,
    ),
    Field(
        229,
        229,
        Kind.FLAG,
        "op_slot",
        1,
        V,
        CONTROL_FACT,
        name="ctl_n",
        values={0: "control word", 1: "other word"},
    ),
    _const(230, 231),
    _enable(
        232,
        233,
        "pb",
        3,
        "f232 is never clear without f233; active together with S3 (f296_301) in 1928 of its "
        "1940 active words, so its port cannot be told apart from S3 in the bytes",
    ),
    Field(
        234,
        235,
        Kind.OPCODE,
        "pb",
        3,
        H,
        f"{RARE}; the candidate enable of f145_146: active in 18 words, all with f145_146 set, "
        "and in 10 of its 17 changes (hypothesis)",
    ),
    _enable(236, 237, "pb", 3, WORD504_NONE),
    _enable(238, 239, "pb", 3, WORD504_NONE),
    *(_enable(lo, lo + 1, "pb", 3) for lo in range(240, 252, 2)),
    _enable(252, 254, "pb", 7, f"values 0..4 and the idle 7 only; {WORD504_NONE}"),
    _enable(255, 255, "pb", 1),
    _enable(256, 257, "pb", 3),
    _enable(258, 258, "pb", 1),
    _enable(259, 260, "pb", 3, X0_PATH),
    _enable(261, 262, "pb", 3, X0_PATH),
    _enable(263, 264, "pb", 3),
    _enable(
        265,
        266,
        "pr",
        3,
        "Not the end op: idling it in program 16 word 79 (bit 229 clear) copied; idling it with "
        "bit 268 in word 60 times the job out (verified on the camera, one run each). Under 1 "
        "an inserted op's result reached neither register set as word 68 saw it (one job: "
        "hypothesis)",
    ),
    _enable(
        267,
        268,
        "pr",
        3,
        "Bit 268 is needed in program 16 words 63 and 65: setting it alone times the job out in "
        "63 and wedged the job (status -5) in 65, whose whole idle times out (verified on the "
        "camera, one run each); bit 267 is set in the stock words 60, 63, 65 and 79. Value 1 "
        "sends the word's data op result to the register f211_215 names, in the set the X2 "
        "xreg reads (inferred, strong; `semantics.RESULT_PORTS`)",
        values={1: "data op result to f211_215, xreg set (inferred)", 3: "idle"},
    ),
    _enable(
        269,
        269,
        "pr",
        1,
        "Under 0 an inserted op's result reached neither register set as word 68 saw it "
        "(one job: hypothesis)",
    ),
    _enable(270, 271, "pr", 3, SELECTOR),
    _enable(
        272,
        273,
        "pr",
        3,
        f"{SELECTOR}; in op 0 words with value 1, f224_228 names ra (307 of 336) {OWNERSHIP}: "
        "the enable of the port that writes the loop counter back (hypothesis); not needed in "
        "program 16 words 60 and 63, where idling it (272 alone in 63) copied (verified on the "
        "camera, one run each). Value 1 sends the word's data op result to the register "
        "f224_228 names, in the set ra and rb read (inferred, strong; "
        "`semantics.RESULT_PORTS`); under 2 an inserted op's result reached neither set (one "
        "job each way: hypothesis)",
        values={
            1: "data op result to f224_228, operand set (inferred)",
            2: "reached neither set in one test (hypothesis)",
            3: "idle",
        },
    ),
    Field(
        274,
        275,
        Kind.RAW,
        "m",
        0,
        I,
        "the value under the active-low strobe f276: f274_276 takes 4 (idle) and 0..3, 7 in 38 "
        f"words, never 5 or 6, {OWNERSHIP}; held, in runs of median 8 words, and no field "
        f"changes under it: a mode of the compute group (hypothesis); {_c2_fact(274, 275)}",
    ),
    Field(
        276,
        276,
        Kind.FLAG,
        "m",
        1,
        I,
        f"the active-low strobe of f274_275 (0 = active); {_c2_fact(276, 276)}",
    ),
    _const(277, 277),
    _head(
        "s0",
        0x7F,
        "Merged from 278..280 and 281..284: the low part is never active without the high part, "
        "which is active alone in 5 % of its words, and both track f41_44 (inferred: one op "
        "field, possibly an op and a modifier); f23_30 is active only where it is",
    ),
    _head(
        "s1",
        0x7F,
        "Merged from 285..287 and 288..291: the low part is never active without the high part, "
        "which is active alone in 5 % of its words, and both track f49_52 (inferred: one op "
        "field, possibly an op and a modifier)",
    ),
    _head(
        "s2",
        0xF,
        "f53_56 and f88_91 also change under S3 (hypothesis: shared operands)",
    ),
    _head(
        "s3",
        0x3F,
        "Merged from 296..297 and 298..301: the low part is never active without the high part, "
        "which is active alone in 26 % of its words, and both track f65_68 (inferred: one op "
        f"field, possibly an op and a modifier); {WORD504}",
    ),
    _ctl(302, 307, "x0", 0x3F, X0_FACT),
    _ctl(308, 309, "x2", 0x3, X2_FACT, basis=XFER_BASIS),
    _ctl(310, 312, "x1", 0x7, X1_FACT, basis=XFER_BASIS),
    Field(
        313,
        319,
        Kind.OPCODE,
        "op_slot",
        IDLE_OP,
        V,
        OP_FACT,
        name="op",
        values={value: meaning.name for value, meaning in OPS.items()},
    ),
)


_CAMERA = "verified on the camera, a source varying by line at b = 1"
_HELD_439 = (
    "program 13 word 439, its head idle there: idling it with the other six held grid A nibbles "
    f"of idle heads has no effect ({_CAMERA}, as one set at the idle value)"
)
UNIT_FACTS = {
    "f198_201": "program 13 word 433, its enable f261_262 active: idling it alone equals idling "
    f"all of grid B there, byte for byte ({_CAMERA}); what it routes is open, not the loaded "
    "tap coefficient itself (inferred: the lines it changes are not those where that tap is "
    "non-zero)",
    "f202_205": "program 13 word 433, its enable f263_264 active: idling it has no effect "
    f"({_CAMERA}), so an active enable is not sufficient for an effect",
    "f263_264": "active at program 13 word 433, where idling its port f202_205 has no effect "
    f"({_CAMERA})",
    "f163_166": "program 13 word 439, its enable f244_245 active: idling it alone equals idling "
    f"all of grid B there, the second plane's path ({_CAMERA})",
    "f53_56": "program 13 word 439, S2's head active: idling it with f57_60 and f88_91 equals "
    f"idling all of grid A there ({_CAMERA}); 1 in place of the stock 2 has no effect",
    "f57_60": "program 13 word 439, S2's head active: tap 0's source line, 5 (stock) its own "
    f"line, 4 tap 1's ({_CAMERA}); idled with f53_56 and f88_91 it equals idling all of grid A "
    "there, which replaces tap 0's product by a per-boot value (`Boot.substitute`); at word 437, "
    "4 -> 1 changes tap 0's line at the top edge only; at word 440 (stock 4), 5 does not give "
    f"tap 1 tap 0's line ({_CAMERA}, pre-registered), so a value does not name one tap's line "
    "in every word; post hoc, there tap 1 reads the second source plane's line int(a) + 1 "
    "(inferred, one run, not modelled); hypothesis: the value names an operand register that "
    "grid B's f163_166 and f167_170 write from the next word on, the register their value "
    "names",
    "f88_91": "program 13 words 439..442, S2's head active: taps 0..3's coefficients, one map "
    "for the four words, 3, 2, 4, 5 selecting c0, c1, c2, c3, each word's stock value its own "
    f"tap's ({_CAMERA}, pre-registered); that tap k takes c_j and that its product moves under "
    "tap j's coefficient give the same bytes; at word 439, 0 and 1 select none of the four: "
    "post hoc, tap 0's coefficient reads as p and 16 - p, p the phase in 16ths, the chroma "
    "planes' two linear weights (inferred, one run each, not modelled); at word 437, 4 -> 5 "
    "changes tap 0's line at the edge lines only",
    "f292_295": "its operands f57_60 and f88_91 select program 13 word 439's tap 0 line and "
    f"coefficient, and f88_91 at words 440..442 taps 1..3's coefficients ({_CAMERA}). At "
    "word 439, 15 (idle) and 8 give the same bytes: only the first plane changes, tap 0's term "
    "replaced, not dropped, by per-lane state that no lab job writes and that differs between "
    f"boots ({_CAMERA}, one run each on three boots; the state reading inferred); at word 557, "
    "15 equals f250_251 = 3 there (verified on the camera)",
    "f69_71": f"program 13 word 439, X0 active: idling it has no effect ({_CAMERA}), so an "
    "active owner is not sufficient for an effect",
    **{k: _HELD_439 for k in ("f37_40", "f45_48", "f49_52", "f65_68", "f80_83")},
    "f41_44": f"{_HELD_439}. Program 13 words 446 and 448, S0's head active: each value tried "
    "(2, 4, 5, 6 at 446; 1, 4, 5, 6 at 448) changes only the first plane, every byte 0, 255 or "
    "the low byte of the unclamped luma (verified on the camera, one run each); post hoc, the "
    "byte is 0, 255 or that low byte by the sign of the sum of the two words' selected "
    "operands, in the stock words range states of the taps 0 + 1 partial sum and of the full "
    "sum (inferred: a saturation select); what the values select is not the S2 operand "
    "registers (hypothesis)",
    "f76_79": f"{_HELD_439}. Program 13 words 446 and 448, S0's head active (stock 0): each value "
    "tried (1..4 and 15 at 446, 1..15 at 448) changes only the first plane (verified on the camera); post hoc, on lines "
    "whose kernel is one-hot a byte is 255 where bit x mod 32 of a 32-lane mask is set and the "
    "stock byte elsewhere, and on the other lines the mask enters the saturation select with "
    "the sign and carry of the taps 2 + 3 partial sum (inferred: one law fits every byte of "
    "every value). The mask is fixed per value: the same at both words for 1..4 and 15, after other "
    "values' jobs, on flat and x-varying sources for 1 and 4, and across boots for 1 and 4 "
    "(verified on the camera), but for lane 8 of value 1, fixed within a boot and different "
    "between boots; no function of the "
    "value's bits and no input of the job gives the masks (inferred): a stored per-lane vector "
    "that the value selects (hypothesis)",
    "f244_245": f"its port f163_166 carries grid B's effect at program 13 word 439 ({_CAMERA}); "
    "idling it there equals idling f163_166, byte for byte, and at word 440 changes only the third plane, to the whole word's idle's third "
    "plane (verified on the camera, a flat source, pre-registered); post hoc the register its "
    "port writes keeps its older content (inferred)",
    "f240_241": "idling it at program 13 word 440 changes only the first plane, by saturation "
    "(verified on the camera, a flat source, one run)",
    "f242_243": "idling it changes one plane only: the first at program 13 word 440, the third "
    "at 448, to the whole word's idle's third plane (verified on the camera, a flat source, one "
    "run each); post hoc, at 448 the third plane's p-weighted term becomes the second plane's "
    "(16 - p) term (inferred): its port hands S2's previous product to S1 (hypothesis)",
    "f246_247": "idling it at program 13 word 448 changes only the second plane, in every lane "
    "but the first of each 32-lane block (verified on the camera, a flat source, one run); "
    "post hoc that plane is then the rounded older content of one operand register (inferred)",
    "f250_251": "idling it at program 13 word 448 changes only the second plane's first lane "
    "of each 32-lane block on a flat source; at word 557 only the second plane, whose "
    "operands then stay at per-lane state that no lab job writes and that differs between "
    "boots, as idling 557's S2 head does, byte for byte (verified on the camera, one run each; "
    "the state reading inferred)",
    "f248_249": "the only C2 free bits off idle at program 13's store words 529, 533 and 558: "
    "idling it at 533 equals idling the word's C2 free bits and grid, byte for byte (verified "
    "on the camera, pre-registered); at 529 and 558 too but for single lanes (inferred): at 529 a "
    "lane of per-boot state, at 558 per-boot state and a real difference not separated",
    "f261_262": f"its port f198_201 carries grid B's effect at program 13 word 433 ({_CAMERA}); "
    "idling it there equals idling f198_201 but in "
    "the job's first 32-lane block, where tap 0 reads outside the source (verified on the "
    "camera on a flat source; predicted from that and met on an x-varying one, one run each)",
}
"""Camera facts of single fields on the units (`units.py`): ownership predicts use."""
CAMERA_TIERS = {"f244_245": V, "f248_249": I, "f41_44": I, "f76_79": I}
"""Fields whose tier the camera raised above the bytes': an idled enable equal to idling its
port or its word (verified), a nibble acting as one select value (inferred, post hoc)."""
FIELDS = tuple(
    replace(
        f,
        fact=f"{f.fact}. {UNIT_FACTS[f.id][0].upper()}{UNIT_FACTS[f.id][1:]}",
        confidence=CAMERA_TIERS.get(f.id, f.confidence),
    )
    if f.id in UNIT_FACTS
    else f
    for f in FIELDS
)
_unfacted = set(UNIT_FACTS) - {f.id for f in FIELDS}
if _unfacted:
    raise ValueError(f"unit facts without a field: {sorted(_unfacted)}")


def _index(fields: tuple[Field, ...]) -> dict[str, Field]:
    """Fields by id and by semantic name; raises on overlaps, gaps and name clashes."""
    owner = [""] * WORD_BITS
    by_key: dict[str, Field] = {}
    for f in fields:
        if not 0 <= f.lo <= f.hi < WORD_BITS:
            raise ValueError(f"{f.id}: bits {f.lo}..{f.hi} outside the word")
        if not 0 <= f.idle <= f.mask:
            raise ValueError(f"{f.id}: idle {f.idle:#x} does not fit")
        for bit in range(f.lo, f.hi + 1):
            if owner[bit]:
                raise ValueError(f"bit {bit} in both {owner[bit]} and {f.id}")
            owner[bit] = f.id
        for key in filter(None, (f.id, f.name)):
            if key in by_key:
                raise ValueError(f"field key {key!r} used twice")
            by_key[key] = f
    gaps = [bit for bit, o in enumerate(owner) if not o]
    if gaps:
        raise ValueError(f"bits without a field: {gaps}")
    return by_key


BY_KEY = _index(FIELDS)
IDLE_VALUE = sum(f.idle << f.lo for f in FIELDS)
"""The idle word (a no-op) composed from the fields' idle values."""


@dataclass(frozen=True)
class Composite:
    """A value whose bits lie in several positional fields, lowest part first."""

    name: str
    parts: tuple[Field, ...]
    confidence: Confidence
    fact: str

    def get(self, word: int) -> int:
        value = 0
        for f in reversed(self.parts):
            value = value << f.width | f.get(word)
        return value

    @property
    def width(self) -> int:
        return sum(f.width for f in self.parts)

    def put(self, word: int, value: int) -> int:
        if not 0 <= value < 1 << self.width:
            raise ValueError(f"{self.name}: {value:#x} does not fit {self.width} bits")
        for f in self.parts:
            word = f.put(word, value & f.mask)
            value >>= f.width
        return word


CONTROL_FLAG = BY_KEY["f229"]
"""Clear exactly in the words whose op is a control op."""
OP_FIELD = BY_KEY["op"]
TARGET_OPS = range(4)
"""The control ops that name a target T; a return has none."""

TARGET = Composite(
    "target",
    (BY_KEY["imm"], BY_KEY["f128_132"]),
    V,
    "the target of a control op 0..3: an absolute word index into its program, bits 15..22 | "
    "bits 128..132 << 8 (verified in the bytes: it follows its target word wherever code moved "
    "across later firmware versions of the same family, and lies inside its program in every "
    "op 0..3 word); what goes there is the op's meaning",
)

RA = BY_KEY["ra"]
RB = BY_KEY["f128_132"]
IMM = BY_KEY["imm"]
IMM13 = Composite(
    "imm13",
    (IMM, RB),
    V,
    "a data op's 13-bit immediate, imm | f128_132 << 8 (verified in the bytes across later firmware "
    "versions of the same family: in the immediate forms f128_132 moves with imm and the register "
    "value it held comes back after them)",
)
IMM16 = Composite(
    "imm16",
    (IMM, RB, BY_KEY["f120_122"]),
    V,
    "a data op's 16-bit immediate, imm | f128_132 << 8 | f120_122 << 13 (verified in the bytes "
    "across later firmware versions of the same family: in 0x38 f120_122 = 7 exactly when "
    "f128_132 = 31, a sign extension); family D's constant too, signed and scaled by 16 "
    "(inferred on the camera, `FAMILIES`)",
)
IMM_WIDTHS = {
    5: Composite("imm5", (IMM,), V, "imm, its values 0..31"),
    13: IMM13,
    15: IMM16,
    16: IMM16,
}
"""The fields of an immediate by its width; a 15-bit one leaves bit 122 clear."""


@dataclass(frozen=True)
class Family:
    """Data ops that share one layout of the op slot: ra (f123_127) in every family, then rb
    (f128_132) when `rb`, then an immediate `imm_bits` wide (0: none)."""

    name: str
    ops: tuple[int, ...]
    rb: bool
    imm_bits: int
    fact: str
    signed: bool = False


FAMILY_BASIS = (
    "the format verified in the bytes across later firmware versions of the same family; the "
    "meanings hypotheses unless tagged"
)
FAMILIES = (
    Family(
        "A",
        tuple(range(0x0C, 0x1B, 2)),
        False,
        13,
        "ra and a 13-bit immediate; f120_122 an operand that varies apart from it (0x0c holds "
        "every f120_122 with f128_132 = 31); loads and stores of width w (bytes 0x0c..0x10, "
        "halfwords 0x12..0x16, words 0x18, 0x1a) at the base ra plus the low 13 bits of "
        "w x imm13 as a signed byte offset (inferred); imm13's bits above them, 11..12 in the "
        "word ops and 12 in the halfword ops, do not move the address on r0: across later "
        "firmware versions of the same family every read of an r0-based slot follows a store to "
        "the same low-13-bit bytes whatever they hold, so they are a flag (a mode, port or "
        "attribute) of open meaning, not address bits (inferred); each op's register form is the "
        "op + 1",
    ),
    Family(
        "A'",
        tuple(range(0x0D, 0x1C, 2)),
        True,
        0,
        "ra and rb, imm 0 in every word; f120_122 an operand; the operation of the op - 1 with a "
        "register in place of its immediate (inferred from the pairing); on the camera, each "
        "inserted on (r2, r2) with port 267 enabled left nothing that word 68 sees and "
        "nothing damaged",
    ),
    Family(
        "B",
        (0x0A, 0x0B, 0x37, 0x38),
        False,
        16,
        "ra and a wide immediate, f120_122 its bits 13..15 (13..14 in 0x37, whose f120_122 is "
        "0..3 in every word); 0x0a passes ra and 0x37 ORs imm << 4 (on the camera)",
    ),
    Family(
        "C",
        (0x3E, 0x3F, 0x40),
        True,
        5,
        "ra, rb and an immediate 0..31 in every word; f120_122 an operand; shifts by the 5-bit "
        "count, 16 the most common count of all three: 0x3e and 0x3f right shifts (inferred "
        "from the camera), 0x40 a left shift (verified on the camera); ra and rb named one "
        "register in every word measured",
    ),
    Family(
        "D",
        (*range(0x1C, 0x22), *range(0x23, 0x26), 0x36),
        False,
        16,
        "ra and a signed 16-bit immediate imm16 = imm | f128_132 << 8 | f120_122 << 13, used "
        "as c = imm16 << 4 (inferred, strong: on the camera, pre-registered dst sha256 "
        "predictions under this rule passed for 0x1e, 0x1f, 0x24, 0x25 and 0x36 at constants "
        "of both signs); f120_122 is 0 in 99 % of the stock words. Arithmetic, min / max and "
        "compares with c (`semantics.OP_SEMANTICS`)",
        signed=True,
    ),
    Family(
        "E",
        (
            0x08,
            0x09,
            *range(0x28, 0x2D),
            *range(0x2E, 0x36),
            0x3A,
            0x3C,
            0x3D,
            0x41,
            0x42,
            0x43,
            0x47,
            0x48,
            0x49,
        ),
        True,
        0,
        "ra, rb and f120_122, imm 0 in every word; register-register ALU ops: moves, AND, OR, "
        "add, subtract, negate, NOT, min, max, compares and shifts by rb / 16 (verified on the "
        "camera at f120_122 = 7, `semantics.OP_SEMANTICS`); 0x47 adds a hidden per-boot value "
        "(inferred); 0x48 and 0x49 undecoded, and no op screened multiplies",
    ),
    Family(
        "F",
        (0x44, 0x46),
        False,
        13,
        "ra and a 13-bit immediate whose f128_132 is 4 or 8 (0x404, 0x408, 0x40c, 0x460) in the "
        "stock words; 0x44 subtracts imm13 << 4 (verified on the camera at f120_122 = 0), 0x46 "
        "adds it and a hidden per-boot value (inferred)",
    ),
)
IMM_BITS = {0x37: 15}
"""Ops whose immediate is narrower than their family's."""
SIGNED_OPS = frozenset({0x0C, 0x10})
"""Ops whose 13-bit immediates 0x1fxx read as small negative numbers, two's complement (inferred)."""
MEANINGS = {
    0x09: "the op at 294 of 381 call targets, often with ra = 31: a routine prologue (inferred "
    "from its position); it passes rb (verified on the camera), so there it sets the port's "
    "register from rb",
    0x0A: "periodic immediates such as 0x5555 and 0x3333; on the camera it passes ra, the "
    "immediate not entering at 0..0x1234 (verified)",
    0x0B: "the most varied immediates, often in a call's delay slots: sets ra from the immediate, "
    "or uses it as a base as family A does (hypothesis); it names a register just before family "
    "A uses it as a base (24 % against 7 % by chance across later firmware versions of the same "
    "family), but in program 16 base plus immediate addressing with 0x0b writing ra would move "
    "the local words that the camera shows 0x1a reading; its immediate's value is consumed on "
    "the end side: on the camera, bit 6 of program 16 word 65's immediate cleared gave a full "
    "copy, then a timeout (inferred: not the store's count, data or address); through port 267 "
    "its result was above the job length at immediate 0x123, so it is no set of the low half "
    "(inferred)",
    0x0C: "its immediates 0x1fxx read as small negative numbers (inferred); a byte store by its "
    "place before the byte loads 0x0e and 0x10 (hypothesis: no anchor; 7 of its 10 accesses of "
    "r0-based slots are their first touch, but 21 of 24 later accesses at its address are 0x0c "
    "again, as a load's would be)",
    0x0E: "every one of its 8 reads of r0-based slots follows a store to them; on the camera, 0x0e "
    "in place of the resampler's 0x16 tap-1 read (ra kept) gave tap 1 c0's byte imm "
    "sign-extended at imm 0 and 1: a byte at ra + imm bytes, sign-extended (inferred); a load "
    "from local memory or a byte select of the datum read just before it (not separated)",
    0x10: "its immediates 0x1fxx read as small negative numbers (inferred); in program 13 every "
    "non-zero immediate lies in its tap block 0x00..0x1f; on the camera, 0x10 in place of the "
    "resampler's 0x16 tap-1 read (ra kept) gave tap 1 c0's byte imm zero-extended at imm 0 and "
    "1: a byte load at ra + imm bytes, zero-extended (inferred)",
    0x12: "a halfword store (inferred: across later firmware versions of the same family, the "
    "first touch of r0-based slots in 108 of its 114 accesses of them, and entry spills at "
    "-64..-1 B)",
    0x14: "a halfword load (inferred: every one of its 31 reads of r0-based slots follows a store "
    "to the same bytes across later firmware versions of the same family; on the camera, 0x14 "
    "in place of the resampler's 0x16 tap-1 read gave tap 1 c0 at imm 0 and c1 at imm 1, imm "
    "in halfwords); its sign extension against 0x16's is open",
    0x16: "in program 13 every non-zero immediate lies in its tap block 0x00..0x1f, in runs that "
    "step 0, 1, 2, 3 with one ra per run; on the camera, imm 1 -> 0 in the resampler's tap-1 "
    "read gave tap 1 tap 0's coefficient byte for byte (verified at that word): a halfword "
    "load at ra + imm halfwords, the imm the tap index (the unit inferred)",
    0x18: "a word store (inferred: across later firmware versions of the same family, it opens "
    "88 of the 95 r0-based word slots of the E-M10 II programs, with entry spills and exit "
    "restores by 0x1a at -64..-1 B)",
    0x1A: "reads the local word at its immediate (inferred, strong): program 16's three 0x1a words "
    "name local words 0, 1 and 2 (src, dst, len), all with ra = 5; idling the one at 2 on the "
    "camera stores 16 KiB of zeros whatever len is, idling the other two leaves dst unwritten; ra "
    "a base register it reads, not a destination (inferred: across later firmware versions of "
    "the same family, in 59 % of its words the next naming of that register is this op again, "
    "with another immediate in 95 % of those pairs; as writes they would be dead), so the word "
    "is ra + imm; on the camera, 0x1a in place of the resampler's 0x16 tap-1 read (imm 1) gave "
    "tap 1 c2, the low 16 bits of the word at ra + 1 word (inferred)",
    0x1E: "at a routine's entry + 1 in 67 routines; it subtracts imm16 << 4 (inferred, strong, "
    "on the camera): a frame allocate there (hypothesis)",
    0x1F: "in 300 return delay slots, its immediate a multiple of 4 in all; it adds imm16 << 4 "
    "(inferred, strong, on the camera): the frame release there (hypothesis)",
    0x36: "imm 1, f128_132 and f120_122 0 in all 53 words; it XORs imm16 << 4 (inferred, strong, "
    "on the camera): a 16 B toggle there (hypothesis)",
    0x37: "periodic immediates such as 0x5555 and 0x2222; it ORs imm << 4 (inferred, on the "
    "camera): a lane or phase mask set-up (hypothesis)",
    0x38: "1022 of 1321 immediates are 2^k - 1 or its complement: an AND with a mask; on the camera "
    "clearing bit 4 of a copy's store-side mask 0xfff0 moved the store 16 B down at a dst 16 mod 32 "
    "only (inferred: an AND with the sign-extended imm16)",
    0x3F: "on the camera, the immediate of a copy's 0x3f word set to 1 and 2 gave a count of len / 2 "
    "and len / 4, and inserted with imm 1..5 len >> imm (inferred: a right shift by imm)",
    0x40: "a left shift by imm (verified on the camera at imm 1, 9, 10 and 11)",
    0x47: "it adds a hidden per-boot value; at f120_122 = 1, 2, 4 and 6 its result on equal "
    "operands near the job length was beyond the readout, as a product would be: the open "
    "multiply candidate (hypothesis)",
    0x48: "undecoded: on the camera it ran clean only on (r2, r2) inserted after the copy's "
    "setup, and stopped the copy with dst unwritten in three other forms",
    0x49: "undecoded: as 0x48, two other forms stopped the copy",
}


@dataclass(frozen=True)
class Operands:
    """A data op's operands; rb and imm are None where its format has none."""

    ra: int
    rb: int | None = None
    imm: int | None = None


@dataclass(frozen=True)
class Format:
    """The operand layout of one data op; decoding never fails, encoding checks the widths."""

    family: Family
    imm_bits: int
    signed: bool

    @property
    def imm(self) -> Composite | None:
        return IMM_WIDTHS.get(self.imm_bits)

    @property
    def fields(self) -> tuple[Field, ...]:
        """The fields the operands stand for."""
        regs = (RA, RB) if self.family.rb else (RA,)
        return regs + (self.imm.parts if self.imm else ())

    def decode(self, word: int) -> Operands:
        rb = RB.get(word) if self.family.rb else None
        if self.imm is None:
            return Operands(RA.get(word), rb)
        imm = self.imm.get(word)
        if self.signed and imm >> (self.imm_bits - 1):
            imm -= 1 << self.imm_bits
        return Operands(RA.get(word), rb, imm)

    def encode(self, word: int, operands: Operands) -> int:
        if (operands.rb is not None) != self.family.rb:
            raise ValueError(f"family {self.family.name}: rb {operands.rb!r}")
        if (operands.imm is not None) != (self.imm is not None):
            raise ValueError(f"family {self.family.name}: imm {operands.imm!r}")
        word = RA.put(word, operands.ra)
        if operands.rb is not None:
            word = RB.put(word, operands.rb)
        if self.imm is None:
            return word
        lo = -(1 << (self.imm_bits - 1)) if self.signed else 0
        if not lo <= operands.imm < lo + (1 << self.imm_bits):
            raise ValueError(f"imm {operands.imm} does not fit {self.imm_bits} bits")
        return self.imm.put(word, operands.imm & ((1 << self.imm_bits) - 1))


def _formats(families: tuple[Family, ...]) -> dict[int, Format]:
    """Each data op's format; raises on an op in two families or outside `DATA_OPS`."""
    formats: dict[int, Format] = {}
    for family in families:
        for value in family.ops:
            if value in formats or value not in DATA_OPS:
                raise ValueError(f"op {value:#x} in family {family.name}")
            bits = IMM_BITS.get(value, family.imm_bits)
            formats[value] = Format(family, bits, family.signed or value in SIGNED_OPS)
    return formats


FORMATS = _formats(FAMILIES)


def is_control(word: int) -> bool:
    return OP_FIELD.get(word) in CONTROL_OPS


def operands(word: int) -> Operands | None:
    """The operands of a data op; None for the other words."""
    fmt = op(OP_FIELD.get(word)).format
    return None if fmt is None else fmt.decode(word)


def target(word: int) -> int | None:
    """The target of a control op 0..3; None for other words."""
    if OP_FIELD.get(word) not in TARGET_OPS:
        return None
    return TARGET.get(word)


def lookup(key: str) -> Field:
    """The field with id or semantic name `key`."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(f"no DSP field {key!r}") from None
