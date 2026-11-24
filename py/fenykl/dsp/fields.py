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
register rb or as bits 8..12 of the immediate, f120_122 as an R register or as bits 13..15. Which,
and how wide the immediate is, is the op's family (`FAMILIES`, `FORMATS`); as with T, the
operands compose from the positional fields.

Facts tagged "by exact readout" were measured on the camera with own programs: every result was
stored into DSP local memory and read back as an exact 32-bit word. Values are plain 32-bit
integers. The scalar unit has four register files: G, which ra and rb read and the PR port
f272_273 writes (f224_228); R, 8 entries, which family A stores and ops 0x46 / 0x47 read
(f120_122) and the port f270_271 writes (f221_223); A, X0's operands, written by the port
f265_266 (f206_210); B, X1's operands, written by the ports f267_268 (f211_215) and f269
(f216_220). X0 and X1 are two copies of one ALU beside the op slot (`X_FUNCTIONS`). A data op
writes no register by itself: its result goes on the ALU result bus, and each PR port's enable
value selects its source, the bus or X0's or X1's result (`PR_SOURCES`).

The compute side has four files of 32-bit scalar registers, F0..F3 (`SCALAR_FILES`): grid B ports
write them from the same three sources (`PB_SOURCES`), and S heads read them (`SCALAR_READS`);
under the M strobe f276 = 0 an S head's scalar result goes on X0's result bus (`S_RESULTS`). Each
S unit has a lane file of 16 registers of 32 lanes of 16 bits (`LANE_FILES`), read by the unit's
two lane nibbles and written by the two grid B lane ports 110 bits above them, whose enable
names the source they take (`LANE_SOURCES`); the S heads' lane functions are
`LANE_FUNCTIONS`, their immediates `LANE_IMMEDIATES`.
"""

from collections.abc import Iterator, Mapping
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
    lo: int,
    hi: int,
    unit: str,
    idle: int,
    fact: str = "",
    basis: str = SEGMENTED,
    kind: Kind | None = None,
    confidence: Confidence = H,
    values: Mapping[int, str] | None = None,
    name: str = "",
) -> Field:
    if kind is None:
        kind = Kind.FLAG if lo == hi else Kind.RAW
    base = f"control bits; {basis}"
    return Field(
        lo,
        hi,
        kind,
        unit,
        idle,
        confidence,
        f"{base}. {fact}" if fact else base,
        name=name,
        values=values or {},
    )


GRID_FACT = (
    "grid field, a source or path select{}; 0 is its most common value in the compute "
    "sections; boundaries from segmentation and the lag-110 repeat of grid A in grid B"
)
# Program 16 sets these grid nibbles to 0 in a staircase and holds them to its end.
STAIRCASE = {194: 19, 84: 20, 198: 29, 88: 30, 182: 59, 76: 60}


GRID_PAIR = (
    "2-bit grid field, a source or path select, at lag 110 like grids A and B; all-ones in every "
    "word of the five stock programs, cleared in hundreds of words of some programs across later "
    "firmware versions of the same family"
)


def _grid(lo: int, hi: int, unit: str, extra: str) -> Field:
    confidence, scalar = NIBBLES.get(_fid(lo, hi), (H, ""))
    # Where the nibble names a register, 15 is not known to mean none.
    none = _fid(lo, hi) not in NIBBLES
    fact = (
        GRID_PAIR
        if hi - lo == 1
        else GRID_FACT.format(" (all-ones = none)" if none else "")
    )
    if lo in STAIRCASE:
        fact += (
            f"; program 16 sets it to 0 from word {STAIRCASE[lo]} on, held to the end: "
            "register 0 of a scalar file, written by a grid B port and read by the next word's "
            "S head (`SCALAR_FILES`, verified in the bytes)"
        )
    if lo == 76:
        fact += f"; {F76_FACT}"
    fact += f". {extra[0].upper()}{extra[1:]}"
    if scalar:
        fact += f". {scalar}"
    return Field(
        lo,
        hi,
        Kind.ROUTE,
        unit,
        (1 << (hi - lo + 1)) - 1,
        confidence,
        fact,
        values={0xF: "none"} if hi - lo == 3 and none else {},
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
EXACT = "verified on the camera by exact readout"
"""The tag of a fact measured with own programs whose results were read back as exact words."""
X0_IMM = f"K, X0's 11-bit immediate f105_109 << 6 | f2_6 << 1 | f1 ({EXACT})"
X1_IMM = f"K, X1's 13-bit immediate f115_119 << 8 | f9_14 << 2 | f7_8 ({EXACT})"
T_HIGH = (
    "in a control op 0..3 bits 8..12 of the target T, not a register (verified in the bytes); in a "
    "return mostly 1 (in every return of the five stock programs but three, which hold 2, 3 and "
    "7), meaning open. In a data op it is the G register rb of families A' and E (an A' op "
    f"accesses ra + w x rb, the E rules read it; {EXACT}) or bits 8..12 of the immediate "
    "(families A, B, D, F; verified in the bytes across later firmware versions of the same "
    "family: in 31878 of 32911 idle-op words it holds the last register-form word's value, "
    "which survives the immediate forms in between); in family C it is encoded as a register, "
    f"but rb had no effect on the result ({EXACT}). In family A imm13's high bits are the "
    f"offset's sign, not a flag ({EXACT}), which modulo 0x2000 gives the same address as the "
    "unsigned reading. In families A' and E a source that stays fixed "
    "across consecutive register-form ops while ra changes (same rb with another ra in 59 % of "
    "such pairs, 10 % by chance), live into the routine more often than chance (verified in the "
    "bytes across later firmware versions of the same family)"
)
RA_FACT = (
    "the op slot's register ra, in G: the field changes in 1 of 33175 idle-op words and in 19884 "
    "of 33290 data-op words across later firmware versions of the same family (verified in the "
    "bytes). A data op reads it and never writes it: its result goes to a register only through "
    f"an enabled PR port ({EXACT}). In family A the byte address base, the access at ra + w x "
    f"imm13 or ra + w x rb ({EXACT}); 0x0b does not use it ({EXACT}); in B, C, D, E and F an "
    f"operand. In ops 0..2 the condition: op 1 branches iff ra == 0, op 2 iff ra != 0, op 0 iff "
    f"ra > 0 signed, computing ra - 1 for an enabled port to write back ({EXACT}); a guard and "
    "its loop end name the same register in 47 % of the pairs against 13 % by chance (verified "
    "in the bytes across later firmware versions of the same family)"
)
XREG_FACT = (
    f"b, X1's second B register, and bits 8..12 of {X1_IMM}; f128_132 is neither ({EXACT}). "
    "Named xreg for the register transfers (X1 heads 0x1c, 0x1d): in program 16 the register-transfer words name the register they use here: as a load "
    "address (word 67), a buffer offset (68 load side, 76 store side), a store count (73), "
    "an enable (42, 66, 69, 70) or the store element format (47) (verified on the camera "
    "per word); r3 holds the job source address. B is apart from G, which ra and rb read: a "
    "data op's result written through PR port 267 (f211_215 under f267_268 = 1) reached the "
    f"xreg of words 68 and 73 and never an operand ({EXACT} through X1; in the copy inferred, "
    "strong), "
    f"and port 267 writes no G register ({EXACT}), while one written through port 272 (f224_228) "
    "reached a later op's ra and rb and not word 68's xreg (inferred, strong). Per routine the "
    "registers xreg changes to and those the op slot names overlap only slightly more than "
    "chance (Jaccard 0.51 against 0.44 across later firmware versions of the same family). The "
    "emulator keeps the two apart (`Boot.regs`, `Boot.slot`)"
)
FLAGS_FACT = (
    "3 bits between the register specifiers. In a data op: bits 13..15 of the immediate in "
    "families B and D (13..14 in 0x37) (verified in the bytes; family D's sign bits by exact "
    "readout), 0 in 99 % of family D's stock words; the R register of a family A or A' store's "
    "datum, whose low 32, 16 or 8 bits it writes, and of the third operand of 0x46 and 0x47 "
    f"({EXACT}); no effect on the loads, the E ops other than 0x47, family C, 0x44 and the "
    f"control ops 0..2 ({EXACT}, at 0 and 4), nor on the multiply 0x49 ({EXACT}, at 0, 1 and "
    "3, one operand pair). In op 0..3 words bits 120..121 are 0 and bit 122 "
    "is set in some words of each control op, mostly constant within a routine (verified in the "
    "bytes across later firmware versions of the same family); its meaning in a call open"
)
SELECTOR = (
    "the loop-end selector pair f270_271 / f272_273: in every op 0 word of the five stock programs exactly "
    "one of f270_271 and f272_273 is 1, and in 386 of 395 across later firmware versions of the "
    "same family; ops 1..3 mostly hold 3 in both. Op 0 computes ra - 1 for the enabled port to "
    f"write, back to ra under f272_273 = 1 on ra ({EXACT})"
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
XFER = (
    "In the register-transfer words (0x1c and 0x1d) the role of B[f115_119] (load address, "
    "offset, count, enable, store format) is given by the sequencer, not by an opcode, in the order of the transfers "
    "(inferred): in program 16 the count role followed its transfer from 73 to 74 and the roles "
    "of 75..77 followed theirs one index earlier, while a counted transfer at 78, the word "
    "before the return 79, stalls the job (verified on the camera, one run each, the stall on "
    "two boots); why 78 fails is open (hypothesis)"
)


CONDITION = (
    "the condition is the register ra (f123_127), no field of its own; a guard and its loop end "
    "name the same register in about half the guarded loops; program 16's guard 45 is taken in "
    "every job tested on the camera, also with each of src, dst, len and their sums and "
    "difference odd (verified on the camera)"
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
        V,
        "loop end: computes ra - 1, which an enabled port writes back (the stock loops enable "
        "f272_273 = 1 on ra), and goes back to T iff ra > 0 signed before the decrement, "
        f"do {{ body }} while (ra-- > 0) ({EXACT}); T at or before the word in the five stock "
        "programs (some short bodies across later firmware versions of the same family have T "
        "up to 3 words after it); loop ends nest and never cross (verified in the bytes)",
    ),
    1: Op(
        "brf",
        V,
        f"branch to T iff ra == 0, no result ({EXACT}); T always after it, the guard of a loop, "
        "at the loop's T - 5 with its own T at the loop's exit (inferred from the bytes); "
        + CONDITION,
    ),
    2: Op(
        "br",
        V,
        f"branch to T iff ra != 0, no result ({EXACT}); either way, the back-edge of most "
        "guarded loops (inferred from the bytes); " + CONDITION,
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
    "measured ones compute is `semantics.OP_SEMANTICS`; it writes no register by itself, its "
    "result going on the ALU result bus, which the word's enabled PR ports write to their "
    f"registers ({EXACT})"
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


X0_PATH = (
    "The residual changes of its nibble fall in data-op words with f302_307 active"
)
X_OPERANDS = (
    "a and b are the unit's two register operands, zK / sK its immediate K zero- / "
    "sign-extended from its top bit; a compare gives 1 or 0"
)
X_SHARED: dict[int, tuple[str, Confidence, str]] = {
    0x00: ("a != sK", V, ""),
    0x01: ("a == sK", V, ""),
    0x02: ("a - zK", V, ""),
    0x03: ("a + zK", V, ""),
    0x04: ("a <= sK signed", V, ""),
    0x05: ("a < sK signed", V, ""),
    0x06: ("a <= zK unsigned", I, "zero extension measured, not pre-registered"),
    0x07: ("a < zK unsigned", I, "zero extension measured, not pre-registered"),
    0x08: ("a <= b unsigned", V, ""),
    0x09: ("a < b unsigned", V, ""),
    0x0A: ("a <= b signed", V, ""),
    0x0B: ("a < b signed", V, ""),
    0x0C: ("a != b", V, ""),
    0x0D: ("a == b", V, ""),
    0x0E: ("abs(b)", V, ""),
    0x0F: ("-b", V, ""),
    0x10: ("a - b", V, ""),
    0x11: ("a + b", V, ""),
    0x12: ("a | zK << 16", V, ""),
    0x13: ("zK", V, ""),
    0x14: ("sK", V, ""),
    0x15: ("b", V, ""),
}
"""The function table X0 and X1 share, by head value h: (result, tier, note). It is the op
slot's ALU table compacted: h 0..7 are ops 0x1c..0x23, 8..0xb ops 0x28..0x2b, 0xc..0x11 ops
0x30..0x35, with the same signedness (verified on the camera by exact readout, X0 117 of 117
pre-registered values on operands that separate the compares, X1 139 of 155; the rows fitted
on those misses then matched 100 of 100 pre-registered values on fresh operands, on both units
with K's top bit set)."""
NO_RESULT = "no result on the ALU bus"
TRANSFER_HEADS = (0x1C, 0x1D, 0x1E)
"""X1's register-transfer forms: no result, no R, G or B register changed."""
X1_ONLY: dict[int, tuple[str, Confidence, str]] = {
    0x16: ("~a & b", V, ""),
    0x17: ("~a & ~b", V, ""),
    0x18: ("a ^ b", V, ""),
    0x19: ("a | b", V, ""),
    0x1A: ("a & b", V, ""),
    0x1B: ("~b", V, ""),
    **{h: (f"{NO_RESULT}: a register transfer", V, "") for h in TRANSFER_HEADS},
}
X0_ONLY: dict[int, tuple[str, Confidence, str]] = {
    0x16: (NO_RESULT, V, ""),
    0x17: (NO_RESULT, V, ""),
    0x18: (NO_RESULT, I, ""),
    0x19: (NO_RESULT, I, ""),
    **{h: ("sK + 1", V, "") for h in range(0x1A, 0x1E)},
    **{h: ("b + 1", V, "") for h in range(0x1E, 0x22)},
    0x22: (NO_RESULT, V, ""),
    **{h: ("0", V, "") for h in range(0x23, 0x3F)},
}
X_FUNCTIONS = {"x0": X_SHARED | X0_ONLY, "x1": X_SHARED | X1_ONLY}
"""Each unit's functions by head value; its idle value (all-ones) is none of them."""


TAG = {V: "verified", I: "inferred", H: "hypothesis"}


def _x_values(unit: str) -> dict[int, str]:
    values = {
        h: f"{text} ({TAG[conf]}{'; ' + note if note else ''})"
        for h, (text, conf, note) in X_FUNCTIONS[unit].items()
    }
    if unit == "x0":
        for h, (text, conf) in X0_LANE_MEMORY.items():
            values[h] = f"scalar: {values[h]}; lane memory: {text} ({TAG[conf]})"
    return values | {(1 << (HEADS[unit][1] - HEADS[unit][0] + 1)) - 1: "idle"}


X0_FACT = (
    "the head of X0: f1 and f2_6 are active only where it is; f69_71 and f100_104 change only "
    f"where it is active, f105_109 in 0.71 of its changes, {OWNERSHIP}. X0 and X1 are two "
    "copies of one ALU, the same function table by head value but for a few values "
    f"(`X_FUNCTIONS`; {EXACT}): X0 on the register file A, a = A[f100_104], b = A[f105_109], "
    "K = f105_109 << 6 | f2_6 << 1 | f1 (11 bits). Its result goes to G through f272_273 = 2, "
    "to A through f265_266 = 0, to R through f270_271 = 2 and to B through f269 = 0, all in "
    f"the head's own word ({EXACT}). {X_OPERANDS}. f69_71 at 0, 1, 2, 4 and 7 changed no scalar "
    f"result ({EXACT})"
)
X1_HEAD_FACT = (
    "the head of X1, one 5-bit value h = f310_312 << 2 | f308_309: at every value of 310..312 the "
    "four values of 308..309 give four functions, so 308..309 is no unit of its own (verified on "
    "the camera by exact readout). Its values 0x1c, 0x1d and 0x1e, once read as a separate "
    "transfer unit X2, put no result on the ALU bus and change no R, G or B register (verified "
    "on the camera by exact readout, a stock copy clean after each): they hand something to the "
    "stream engine that the scalar side does not see. xreg changes in 0.81 of the words with "
    "bits 308..309 at 0, 0.58 at 1 and 0.61 at 2 (at any value of 310..312) against 0.27 "
    f"overall, {OWNERSHIP}. {XFER}; in program 16 "
    "words 58..79, the 0x1c words (bit 308 clear) stall the job when idled. X1 is X0's copy on "
    "the register file B, a = B[f110_114], b = B[f115_119], K = f115_119 << 8 | f9_14 << 2 | "
    "f7_8 (13 bits): logic functions at 0x16..0x1b where X0 has none, the transfers at "
    f"0x1c..0x1e (`X_FUNCTIONS`; {EXACT}). Its result goes to A through f265_266 = 1, to B "
    "through f267_268 = 0, to R through f270_271 = 0 and to G through f272_273 = 0, in the "
    f"head's own word ({EXACT}). {X_OPERANDS}. f7_8 and f9_14 are active only where it is; "
    f"f110_114 changes where bits 310..312 are active in 0.78 of its changes, {OWNERSHIP}"
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
    "(verified in the bytes across later firmware versions of the same family): the G "
    "register a result is written to is often the next op's ra"
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
    "x1": (308, 312),
}
"""The strobe of each compute slot S0..S3 and ALU X0, X1: the C2 head or control
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


ALU_BUS = "the ALU bus"
PR_SOURCES: dict[str, dict[int, str | None]] = {
    "f265_266": {0: "X0's result", 1: "X1's result", 2: ALU_BUS},
    "f267_268": {0: "X1's result", 1: ALU_BUS, 2: None},
    "f269": {0: "X0's result"},
    "f270_271": {0: "X1's result", 1: ALU_BUS, 2: "X0's result"},
    "f272_273": {0: "X1's result", 1: ALU_BUS, 2: "X0's result"},
}
"""What each PR port writes to its register by enable value (verified on the camera by exact
readout: each value one exact word on a sentinel register, the other sources in the same word
leaving it; None writes nothing, with the bus, X0 and X1 all carrying results). X0's and X1's
results are those of their heads in the port's own word. The ALU bus carries the word's op
result, a load's datum three words after the load and a multiply's product one word after the
multiply (verified on the camera by exact readout)."""
PR_FILES = {
    "f265_266": "A",
    "f267_268": "B",
    "f269": "B",
    "f270_271": "R",
    "f272_273": "G",
}


def _pr_values(enable: str, idle: int) -> dict[int, str]:
    return {
        v: "writes nothing (verified)"
        if src is None
        else f"{src} to {PR_FILES[enable]} (verified)"
        for v, src in PR_SOURCES[enable].items()
    } | {idle: "idle"}


PB_FILES = {
    "f255": "F0",
    "f256_257": "F0",
    "f258": "F1",
    "f259_260": "F1",
    "f261_262": "F2",
    "f263_264": "F3",
}
"""The scalar file each of these grid B ports writes, at the register its nibble names."""
PB_SOURCES: dict[str, dict[int, str | None]] = {
    "f255": {0: "X0's result"},
    "f256_257": {0: "X1's result", 1: ALU_BUS, 2: None},
    "f258": {0: "X0's result"},
    "f259_260": {0: "X1's result", 1: ALU_BUS, 2: None},
    "f261_262": {0: "X0's result", 1: "X1's result", 2: ALU_BUS},
    "f263_264": {0: "X0's result", 1: "X1's result", 2: ALU_BUS},
}
"""What the grid B ports of the scalar files write by enable value, measured on the camera by
exact readout from a word whose ALU bus, X0 result and X1 result differ (None: the register kept
its sentinel): verified where pre-registered and met on fresh values, inferred for the cells in
`PB_MEASURED`, one value each. Under an X0 source with X0 idle the port stores a value that is
no result of the word (measured on the camera)."""
PB_MEASURED = {
    ("f256_257", 2),
    ("f258", 0),
    ("f263_264", 0),
    ("f263_264", 1),
    ("f263_264", 2),
}
"""The cells of `PB_SOURCES` measured but not pre-registered (inferred)."""


def _pb_tier(enable: str) -> Confidence:
    return I if any(e == enable for e, _ in PB_MEASURED) else V


SCALAR_FILES = (
    "F0..F3, four files of 32-bit registers on the compute side, numbered by a nibble: the grid "
    "B ports of f255 and f256_257 (F0), f258 and f259_260 (F1), f261_262 (F2) and f263_264 (F3) "
    "write them, and S heads read them from the next word on (`SCALAR_READS`). Registers 0, 3 "
    "and 5 of F1 held three values at once, a 32-bit value came back whole, and a write to F0, F1 "
    f"or F2 left the other two files' register 0 as it was ({EXACT})"
)
SCALAR_READS: dict[str, tuple[int, str, str]] = {
    "s0": (0x52, "F0", "f76_79"),
    "s1": (0x54, "F1", "f84_87"),
    "s2": (0xE, "F2", "f88_91"),
    "s3": (0x24, "F3", "f96_99"),
}
"""The S heads that return a scalar register: (head value, file, the nibble naming the register),
the result file[nibble] (on the camera by exact readout, through the M strobe and port f272_273
= 2: verified for S0..S2; S3's measured on eight values, not pre-registered, inferred)."""


def _pb_values(enable: str, idle: int) -> dict[int, str]:
    to = PB_FILES[enable]
    values = {}
    for v, src in PB_SOURCES[enable].items():
        tag = TAG[I if (enable, v) in PB_MEASURED else V]
        values[v] = (
            f"writes nothing ({tag})" if src is None else f"{src} to {to} ({tag})"
        )
    return values | {idle: "idle"}


def _pb(enable: str) -> str:
    return (
        f"the enable of the grid B port that writes {PB_FILES[enable]}[{PORTS[enable]}] "
        "(`SCALAR_FILES`), its value the source (`PB_SOURCES`)"
    )


LANE_FILES: dict[str, tuple[str, str, str]] = {
    "L0": ("s0", "f37_40", "f41_44"),
    "L1": ("s1", "f45_48", "f49_52"),
    "L2": ("s2", "f53_56", "f57_60"),
    "L3": ("s3", "f61_64", "f65_68"),
}
"""The lane files, one per S unit: (unit, the nibble of its operand a, of its operand b). Each
holds 16 registers of 32 lanes of 16 bits; a unit's lane heads read a = file[first nibble] and
b = file[second nibble], and the two grid B ports 110 bits above those nibbles write the file at
the register they name (`LANE_SOURCES`). Verified on the camera for L2 and L3 (sentinels in
both files, each port alone) and through the operands for L0 and L1: copies written by
f151_154 / f159_162 were the a and b of S0's and S1's pre-registered function tables. L3's 16
registers were dumped whole: power-up content, stable over a boot, a few bits apart between
boots (verified on the camera)."""
LANE_THIRD_FILE = (
    "U, a third kind of lane register file, 16 registers named by f31_34, whose lanes 0..n-1 fill "
    "the top lanes of S3's lane shifts by n (inferred, fitted: 16 values of f31_34 gave 16 "
    "different fills of power-up content); by position its port is f141_144 under f232_233 "
    "(inferred)"
)
S3_RESULT, S1_RESULT, S0_RESULT = "S3's result", "S1's result", "S0's result"
S2_LOW, S2_HIGH = "S2's result, a product's low half", "S2's product's high half"
S2_RESULT = "S2's result"
LOAD_LAG = 4
"""Words from an X0 load head to the one word whose port takes the loaded vector."""
LANE_LOAD = (
    f"a lane memory load's 32 lanes, {LOAD_LAG} words after its X0 head (`LANE_MEMORY`)"
)
LANE_SOURCES: dict[str, dict[int, tuple[str, Confidence]]] = {
    "f236_237": {0: (LANE_LOAD, I), 1: (S2_HIGH, I), 2: (S0_RESULT, I)},
    "f238_239": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
    "f240_241": {0: (LANE_LOAD, I), 1: (S2_HIGH, V), 2: (S0_RESULT, I)},
    "f242_243": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, I)},
    "f244_245": {0: (LANE_LOAD, V), 1: (S0_RESULT, V)},
    "f246_247": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
    "f248_249": {0: (LANE_LOAD, V), 1: (S0_RESULT, V)},
    "f250_251": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
}
"""The lane ports' enables: what each value writes, with its tier. Measured on the camera with a
sentinel in the named register, one enable value per word: f238_239 and f244_245..f250_251
pre-registered; f240_241 / 1 on all 32 lanes of a product (`S2_HIGH_HALVES`), f242_243 / 1 as
its low half; f236_237 and f240_241 / 2 on two lanes, not pre-registered (inferred); f242_243 /
2 by the pattern of the second ports (inferred). Enable 0 of the four first ports (f236_237,
f240_241, f244_245, f248_249) takes the vector an X0 load head (`LANE_MEMORY`) asked for exactly
four words earlier; at lag 3 or 5, or with no head, the port writes nothing (verified on the
camera, pre-registered sentinels, 32 lanes each); one load reaches several ports in the same
word (measured on the camera, not pre-registered: inferred); into
L2 and L3 the loaded lanes were pre-registered and met (verified), into L0 and L1 measured on
the camera, not pre-registered (inferred). In the five stock programs every word with an X0 load
head has such a port write (or O's, `STORE_SOURCES`) four words later and no such write lacks
one (verified in the bytes), so idling f244_245 = 0 in program 13 drops a load (inferred). Enable
2 of f244_245 / f248_249 wrote nothing from any S head. A port takes a move's result (S3 0x18,
0x19, 0x22, S2 0x8) in the head's own word and an arithmetic result exactly one word after the
head (`LANE_LAG`)."""
LANE_LAG: dict[str, dict[int, int]] = {
    "s0": {0x08: 1, 0x1F: 1, 0x20: 1, 0x29: 1},
    "s1": {0x0E: 1, 0x10: 1, 0x33: 1, 0x3C: 1},
    "s2": {0x01: 1, 0x05: 1, 0x08: 0},
    "s3": {0x0A: 1, 0x0C: 1, 0x18: 0, 0x19: 0, 0x22: 0},
}
"""Words from a head to the one word whose lane port takes its result (verified on the camera
for these heads, a sentinel written only at that lag, the head not held; with the head held, at
every lag from 1); that the other moves take 0 and the other arithmetic heads 1 is inferred."""

LANE_VECTORS = 0x400
"""The lane memory's size in vectors of 32 lanes; an address counts modulo it (`LANE_MEMORY`)."""
LANE_MEMORY = (
    f"the lane memory: {LANE_VECTORS:#x} vectors of 32 elements of 16 bits, addressed by vector "
    f"number modulo {LANE_VECTORS:#x} (verified on the camera: the wrap pre-registered at "
    f"{LANE_VECTORS:#x} and through a second operand of 0x7fff, which acts as -1; the size "
    "inferred: five different contents at 0x20, 0x40, 0x80, 0x100 and 0x200, and 0x400, "
    "0x800 .. 0x4000 read vector 0). X0's heads 0x1c, 0x1d (address A[f100_104] + sK, K X0's "
    "signed 11-bit immediate) and 0x20, 0x21 (A[f100_104] + A[f105_109]) load one vector, "
    f"delivered {LOAD_LAG} words after the head to the first lane ports under 0 "
    "(`LANE_SOURCES`) or to the store file O under 0 (`STORE_SOURCES`); 0x1a, 0x1b (A[a] + sK) "
    "and 0x1e, 0x1f (A[a] + A[b]) store the 32 lanes of O[f69_71] there (`X0_LANE_MEMORY`). "
    "It is the stock copy's buffer (program 16, cmd 0xF): the copy's load fills one element per "
    "source byte, zero-extended, source byte 32 n at vector n lane 0 at placement 0, and its "
    "store writes each element's low 8 bits (verified on the camera, pre-registered: a stock "
    "copy in, an own vector program, a stock copy out, exact on 512 + 512 + 4096 bytes). "
    "Timing: a store head three words after O's port write stores the new content (verified, "
    "pre-registered); one in the same word as the write stores the old content of O, a load one "
    "word after a store to the same vector reads the stored vector, and four loads in "
    "consecutive words land in four consecutive port words (measured on the camera, not "
    "pre-registered: inferred)"
)
STORE_FILE = (
    "O, the store file: eight lane registers of 32 lanes of 16 bits, written by the grid B port "
    "f179_181 (the register) under f252_254 (the source, `STORE_SOURCES`) and read by X0's "
    "store heads through f69_71, 110 bits below its port as at the lane files (verified on the "
    "camera, pre-registered: all eight registers written through f179_181 = 0..7 and stored "
    "through f69_71 = 0..7, 32 lanes exact)"
)
STORE_SOURCES: dict[int, tuple[str, Confidence]] = {
    0: (LANE_LOAD, V),
    1: (S3_RESULT, V),
    2: (S2_RESULT, V),
    3: (S1_RESULT, V),
    4: (S0_RESULT, V),
}
"""What f252_254 writes to O[f179_181] by value, with its tier (verified on the camera,
pre-registered, 11 groups of 32 lanes exact); 5 and 6 never occur in the stock programs and
were not run, 7 is idle. A move (S3's lane number and broadcast, S2's pass of b) is taken in the
head's own word, an arithmetic result one word after the head, as at the lane ports
(`LANE_LAG`)."""
X0_PARITY = (
    "each head and its even / odd twin (0x1a / 0x1b, 0x1c / 0x1d, 0x1e / 0x1f, 0x20 / 0x21) "
    "showed no difference in address, lanes covered, element width or scalar result, and none "
    "writes A[a] or A[b] back (measured on the camera: inferred); what the low head bit selects "
    "is open (hypothesis: the handshake with the copy engine's streams)"
)
_STORE_IMM, _STORE_REG = (
    f"stores O[f69_71] to vector A[a] + {offset}" for offset in ("sK", "A[b]")
)
_LOAD_IMM, _LOAD_REG = (
    f"loads vector A[a] + {offset}, delivered {LOAD_LAG} words later"
    for offset in ("sK", "A[b]")
)
X0_LANE_MEMORY: dict[int, tuple[str, Confidence]] = {
    0x1A: (_STORE_IMM, V),
    0x1B: (_STORE_IMM, V),
    0x1C: (_LOAD_IMM, V),
    0x1D: (_LOAD_IMM, V),
    0x1E: (_STORE_REG, V),
    0x1F: (_STORE_REG, V),
    0x20: (_LOAD_REG, V),
    0x21: (_LOAD_REG, V),
}
"""X0's lane memory heads beside their scalar result (`X_FUNCTIONS`), each with its tier
(verified on the camera, pre-registered, 32 lanes each, a negative immediate and the wrap
included); the address modulo `LANE_VECTORS` (`LANE_MEMORY`)."""
X0_LANE_LOADS = (0x1C, 0x1D, 0x20, 0x21)
"""X0's lane memory load heads; what f69_71 does on a load is open."""
X0_LANE_BY_REGISTER = range(0x1E, 0x22)
"""The lane memory heads that add A[b], not sK, to A[a]."""

U_PORT_FACT = (
    "With each enable value 0..2, from a word with one value on all three scalar buses and no "
    "S head, it wrote nothing to lanes 0 / 1 of L3, nor under 1 beside any S3 head (verified on "
    f"the camera); by position the enable of U's port (inferred): {LANE_THIRD_FILE}"
)
LANE_OPERANDS = (
    "a and b are the unit's two lane operands (`LANE_FILES`) lane by lane, read as signed or "
    "unsigned 16-bit values as the rule says; a[k+1] is the next lane; s, t the unit's scalar "
    "(S0 F0[f72_75], S1 F1[f84_87], S2 F2[f88_91]) and imm its immediate, n a shift count; "
    "sat_s / sat_u saturate to the signed / unsigned 16-bit range, max_u / min_u compare "
    "unsigned, the others signed; a result is its low 16 bits; (a : b) is the 32-bit value "
    "with a the high half"
)
NO_LANE = "no lane result"
"""A head that wrote no lane through its unit's port into L3 in four consecutive words."""
CARRY = "a result with 1 added in some lanes: a per-lane condition bit feeds it"
SELECT = "a per-lane condition bit selects"
LaneRow = tuple[str, Confidence, str]


def _rows(
    heads: range | tuple[int, ...], text: str, tier: Confidence, note: str = ""
) -> dict[int, LaneRow]:
    return {h: (text, tier, note) for h in heads}


TIES = "ties to even"
NEXT_BLOCK = (
    "lane 31's neighbour lies in the next 32-lane block (hypothesis): not predicted"
)
TIES_AT_10 = (
    "ties to even met at counts 1, 2 and 3; no operand lane was a tie at count 10, where half "
    "up is not excluded"
)
WIDE_FLOOR = "1 more in some lanes on the camera, as the carry forms: the lanes are not predicted"
LANE_FUNCTIONS: dict[str, dict[int, LaneRow]] = {
    "s0": {
        0x00: ("sat_s(|a[k+1] - b|) in lanes 0..30", V, NEXT_BLOCK),
        0x01: ("sat_s(|a - b[k+1]|) in lanes 0..30", V, NEXT_BLOCK),
        0x02: ("sat_s(b - a[k+1]) in lanes 0..30", V, NEXT_BLOCK),
        0x03: ("sat_s(b[k+1] - a) in lanes 0..30", V, NEXT_BLOCK),
        0x04: ("sat_s(a[k+1] + b) in lanes 0..30", V, NEXT_BLOCK),
        **_rows((0x05, 0x21), "sat_s(|a - b|)", V),
        **_rows((0x06, 0x28), "sat_s(b - a)", V),
        **_rows((0x07, 0x29), "sat_s(a + b)", V),
        0x08: (f"0 or imm by lane; {SELECT}", H, ""),
        0x09: (f"0 or s by lane; {SELECT}", H, ""),
        0x0A: (f"0, a or b by lane; {SELECT}", H, ""),
        **_rows(range(0x0B, 0x0E), f"one of 0, imm, s, a and b by lane; {SELECT}", H),
        0x0E: ("max_s(b, imm)", V, ""),
        0x0F: ("min_s(b, imm)", V, ""),
        0x10: ("sat_s(b - imm)", V, ""),
        0x11: ("sat_s(b + imm)", V, ""),
        **_rows((0x12, 0x13, 0x18, 0x19, 0x24, 0x25), NO_LANE, I),
        0x14: (f"(b - s) / 2, {TIES}", V, ""),
        0x15: (f"(b + s) / 2, {TIES}", V, ""),
        0x16: ("max_s(b, s)", V, ""),
        0x17: ("min_s(b, s)", V, ""),
        0x1A: ("sat_s(b - s)", V, ""),
        0x1B: ("sat_s(b + s)", V, ""),
        0x1D: (f"(a + b) / 2, {TIES}", V, ""),
        **_rows((0x1E, 0x1F, 0x2A, 0x2B, 0x2C, 0x2D), f"a + b, {CARRY}", H),
        0x20: ("a + b, wrapping", V, ""),
        0x22: ("max_s(a, b)", V, ""),
        0x23: ("min_s(a, b)", V, ""),
        0x26: ("sat_s(|a|)", V, ""),
        0x27: ("-a, wrapping", V, ""),
        **_rows(range(0x2E, 0x34), NO_LANE, I),
        0x34: ("max_u(b, imm)", V, ""),
        0x35: ("min_u(b, imm)", V, ""),
        0x36: ("max_u(b, s)", V, ""),
        0x37: ("min_u(b, s)", V, ""),
        0x38: ("max_u(a, b)", V, ""),
        0x39: ("min_u(a, b)", V, ""),
        0x3A: ("sat_u(b - imm)", V, ""),
        0x3B: ("sat_u(b + imm)", V, ""),
        0x3C: ("sat_u(b - s)", V, ""),
        0x3D: ("sat_u(b + s)", V, ""),
        0x3E: ("sat_u(b - a)", V, ""),
        0x3F: ("sat_u(a + b)", V, ""),
        **_rows(
            range(0x60, 0x7F),
            "a at f37_40 = 1; A or 0 at other register numbers, never that register",
            H,
            "the rule open",
        ),
    },
    "s1": {
        0x00: ("~b", V, ""),
        0x01: ("a ^ b", V, ""),
        0x02: ("a | b", V, ""),
        0x03: ("a & b", V, ""),
        0x04: ("b ^ imm", V, ""),
        0x05: ("b | imm", V, ""),
        0x06: ("b & imm", V, ""),
        0x07: ("b ^ t", V, ""),
        0x08: ("b | t", V, ""),
        0x09: ("b & t", V, ""),
        **_rows((0x0A, 0x13), "sat_s(b << n), n = imm & 15", V),
        0x0B: ("b << n, n = imm & 15, wrapping", V, ""),
        0x0C: ("b >> n logical, n = imm & 15", V, ""),
        0x0D: ("b >> n arithmetic, n = imm & 15", V, ""),
        0x0E: ("b / 2^n rounded, n = imm & 15", V, TIES_AT_10),
        0x0F: ("sat_s((a : b) >> n), n = imm & 15", V, ""),
        0x10: ("sat_s((a : b) / 2^n rounded), n = imm & 15", V, TIES_AT_10),
        **_rows((0x11, 0x12), "sat_u(b << n), n = imm & 15", V),
        **_rows((0x14, 0x15), "(a : b) / 2^n rounded, n = imm & 15", V, TIES_AT_10),
        **_rows(range(0x16, 0x1A), "(a : b) >> n, n = imm & 15", I, WIDE_FLOOR),
        **_rows((0x1A, 0x1C), "sat_u(b << t)", V),
        **_rows((0x1B, 0x23), "sat_s(b << t)", V),
        **_rows((0x1D, 0x1E), f"(a : b) / 2^t, {TIES}", V),
        **_rows(range(0x1F, 0x23), "(a : b) >> t", I, WIDE_FLOOR),
        0x24: ("b << t, wrapping", V, ""),
        0x25: ("b >> t logical", V, ""),
        0x26: ("b >> t arithmetic", V, ""),
        0x27: (f"b / 2^t, {TIES}", V, ""),
        0x28: ("sat_s((a : b) >> t)", V, ""),
        0x29: (f"sat_s((a : b) / 2^t), {TIES}", V, ""),
        0x2A: ("max_s(b, imm)", V, ""),
        0x2B: ("min_s(b, imm)", V, ""),
        0x2C: ("max_s(b, t)", V, ""),
        0x2D: ("min_s(b, t)", V, ""),
        0x2E: ("max_s(a, b)", V, ""),
        0x2F: ("min_s(a, b)", V, ""),
        0x30: ("sat_s(b - imm)", V, ""),
        0x31: ("sat_s(b + imm)", V, ""),
        **_rows((0x32, 0x33, 0x3D, 0x3E, 0x3F, 0x40), CARRY, H),
        0x34: ("a + b, wrapping", V, ""),
        0x35: (f"(b + t) / 2, {TIES}", V, ""),
        0x36: ("sat_s(b - t)", V, ""),
        0x37: ("sat_s(b + t)", V, ""),
        0x38: (f"(a + b) / 2, {TIES}", V, ""),
        0x39: ("sat_s(|a|)", V, ""),
        0x3A: ("-a, wrapping", V, ""),
        0x3B: ("sat_s(b - a)", V, ""),
        0x3C: ("sat_s(a + b)", V, ""),
        **_rows((*range(0x41, 0x47), 0x54), NO_LANE, I),
        0x47: ("max_u(b, imm)", V, ""),
        0x48: ("min_u(b, imm)", V, ""),
        0x49: ("max_u(b, t)", V, ""),
        0x4A: ("min_u(b, t)", V, ""),
        0x4B: ("max_u(a, b)", V, ""),
        0x4C: ("min_u(a, b)", V, ""),
        0x4D: ("sat_u(b - imm)", V, ""),
        0x4E: ("sat_u(b + imm)", V, ""),
        0x4F: ("sat_u(b - t)", V, ""),
        0x50: ("sat_u(b + t)", V, ""),
        0x51: ("sat_u(b - a)", V, ""),
        0x52: ("sat_u(a + b)", V, ""),
        **_rows((*range(0x55, 0x60), 0x53), "b", I),
        **_rows((0x53, 0x55, 0x5A, 0x5F), "b", V),
        **_rows(
            range(0x61, 0x7E),
            "0 in one run",
            H,
            "0x60 and 0x7e of this range gave 0 in that run and b in later ones: open",
        ),
        **_rows((0x60, 0x7E), "0 or b, on a state not identified", H),
    },
    "s2": {
        **_rows(
            (0x00, 0x01),
            "b x imm, low 16 bits",
            V,
            "imm by field `LANE_IMMEDIATES` (inferred); 0x0's high half that of the unsigned b",
        ),
        0x02: (
            "a x b, low 16 bits",
            V,
            "its high half that of the unsigned a x the signed b",
        ),
        **_rows((0x04, 0x06), "a x b, low 16 bits", V),
        0x03: ("b x t, low 16 bits", V, ""),
        0x05: ("b x t, low 16 bits", V, "its high half that of the signed b"),
        0x07: ("a", V, ""),
        **_rows((0x08, 0x0D), "b", V),
        0x09: ("0xffff where b < 0, else 0", V, ""),
        0x0A: ("the lane number 0..31", V, ""),
        0x0B: ("0xffff where b >= 0, else 0", V, ""),
    },
    "s3": {
        **_rows(
            (*range(0x06), 0x0E, 0x0F, 0x16, 0x17, 0x1A, 0x1D, 0x1E, 0x1F),
            NO_LANE,
            I,
        ),
        **_rows(range(0x24, 0x3F), NO_LANE, I),
        0x06: ("0", I, ""),
        0x07: (
            "b in lanes 0..15, 0 in lanes 16..31",
            I,
            "0 in all lanes in an earlier run",
        ),
        0x08: ("b", V, "0 in an earlier run on other operands"),
        0x09: ("b's lanes 0..2, then 0", H, "0 in an earlier run; the rule open"),
        0x0A: ("lanes 2k, 2k + 1 := b[k], a[k] for k = 0..15", V, ""),
        0x0B: ("lanes 2k, 2k + 1 := b[16 + k], a[16 + k] for k = 0..15", V, ""),
        0x0C: ("b's odd lanes, then a's odd lanes", V, ""),
        0x0D: ("b's even lanes, then a's even lanes", V, ""),
        0x10: ("b with lane f92_95 + 16 x (f31_34 bit 2) := F3[f96_99]", V, ""),
        0x11: (
            "an immediate in every lane",
            V,
            "the immediate imm14 by field inferred (`LANE_IMMEDIATES`)",
        ),
        0x12: ("0xffff where b < 0, else 0", V, ""),
        0x13: ("0xffff where b >= 0, else 0", V, ""),
        0x14: ("the lane number 0..31", V, ""),
        0x15: (
            "b with lane F3[f92_95] := F3[f96_99]",
            V,
            "the lane taken & 31 as by 0x16 / 0x17 (inferred)",
        ),
        0x18: ("F3[f96_99] in every lane: a broadcast", V, ""),
        **_rows((0x19, 0x20, 0x21, 0x23), "b", V),
        **_rows(
            (0x1B, 0x1C, 0x22),
            "at f92_95 = 4, lane k := b[k + 4] for k = 0..27",
            V,
            "inferred: b moved by n = f92_95 as a signed nibble, n = 1..4 lane k := b[k + n] "
            "with the top n lanes from U[f31_34] lanes 0..n-1, n = -4..-1 the other way with "
            "the bottom lanes from a source not identified, n = 5..11 b; 0x22 wrote nothing "
            "while F3 held power-up content: it depends on an F3 register",
        ),
    },
}
"""Each S unit's lane functions by head value: (result, tier, note), the result written to a lane
file by a lane port (`LANE_SOURCES`). Measured on the camera: every head beside the unit's port
into L3 in four consecutive words on one random operand pair (fitted, inferred), then the rows
pre-registered lane by lane on two constructed 32-lane vectors of edge and random values
(verified where all lanes matched), and the refits of the misses pre-registered again on other
scalars (verified where they matched; inferred where a refit was not run again). Heads not
listed are open; `LANE_OPERANDS` names the operands. The 1 added by the carry forms is not
predicted."""
S2_HIGH_HALVES: dict[int, LaneRow] = {
    0x02: ("bits 16..31 of the unsigned a x the signed b", I, ""),
    0x05: ("bits 16..31 of the signed b x t", V, ""),
    0x00: ("bits 16..31 of the unsigned b x imm", I, ""),
}
"""Where S2's multiplies are known whole: the high half of the 32-bit product, which the first
ports of L0 and L1 take under enable 1 one word after the head (`LANE_SOURCES`). With the low
half taken by f159_162 / 1 in the same word, S1's double-width shifts of a : b narrow the
product (verified on the camera for 0x5 on all 32 lanes, pre-registered: a fixed-point multiply
by a scalar, rounded and saturated, in three words). 0x2's and 0x0's signedness was refitted on
all 32 lanes where an operand is negative (inferred); what separates 0x2 / 0x4 / 0x6, 0x3 / 0x5
and 0x0 / 0x1 is open (hypothesis: the operands' signedness)."""
LANE_IMMEDIATES: dict[str, tuple[str, Confidence]] = {
    "s0": ("imm16 = f76_79 << 12 | f37_40 << 8 | f23_30", I),
    "s1": ("imm7 = sext7(f80_83 bits 3..2 << 5 | f84_87 << 1 | f0)", I),
    "s2": ("imm7 = sext7(f53_56 << 3 | f88_91 >> 1), the multiplier of 0x0 / 0x1", I),
    "s3": (
        "imm14 = sext14(f96_99 << 10 | f65_68 << 6 | f31_34 bits 3..2 << 4 | f92_95), 0x11's",
        I,
    ),
}
"""Each S unit's immediate by field. S0's, S1's and S2's were fitted on the camera with one
field varied per cell, read through max_s(0x8000, imm) or 1 x imm (inferred); S3's were fitted
by nibble, then met at one nibble setting pre-registered, f31_34 bit 3 not pre-registered
(inferred). Register nibbles double as immediate bits: S0's f37_40 (a)
and f76_79, S1's f84_87 (t), S2's f53_56 (a) and f88_91 (t)."""

S_NONE = {"s0": "0..0x3f and 0x60..0x7f none"}
"""Where not every other head value was read."""
REDUCTION = (
    "of the 32 lanes of L3[f65_68], on M's bus from the third word after the head on, where it stays: a result "
    "register (verified, pre-registered)"
)
S_RESULTS: dict[str, dict[int, str]] = {
    "s0": {0x52: f"F0[f76_79] ({EXACT})"},
    "s1": {0x54: f"F1[f84_87] ({EXACT})"},
    "s2": {
        0xC: "lane (f53_56 & 3) x 8 + (f88_91 >> 1) of L2[f57_60], sign-extended (inferred, "
        "fitted)",
        0xE: f"F2[f88_91] ({EXACT})",
    },
    "s3": {
        0x0: f"the unsigned maximum {REDUCTION}",
        0x1: f"the unsigned minimum {REDUCTION}",
        0x2: f"the sum, the lanes zero-extended, {REDUCTION}",
        0x3: f"the signed maximum, sign-extended, {REDUCTION}",
        0x4: f"the signed minimum, sign-extended, {REDUCTION}",
        0x5: f"the sum, the lanes sign-extended, {REDUCTION}",
        0xE: "lane f92_95 + 16 x (f31_34 bit 2) of L3[f65_68], zero-extended (inferred, fitted)",
        0xF: "lane f92_95 + 16 x (f31_34 bit 2) of L3[f65_68], sign-extended (verified: built "
        "vectors read back on all 32 lanes)",
        0x16: "lane F3[f92_95] & 31 of L3[f65_68], zero-extended (inferred, fitted)",
        0x17: "lane F3[f92_95] & 31 of L3[f65_68], sign-extended (inferred, fitted)",
        0x24: "F3[f96_99] (inferred: measured on eight values, not pre-registered)",
    },
}
"""The S head values with a scalar result under the M strobe (which values have one: measured on
the camera on every head value but S0's 0x40..0x5f, of which only 0x52 was read; the rules'
tiers in the text). A reduction's register and an extract's lane in the same word reach the bus
OR-ed (measured on the camera)."""


def _written(file: str, enable: str, reader: str) -> str:
    return f"The register of {file} that {enable} writes, {reader}; {SCALAR_FILES}"


def _lane_port(file: str, nibble: str, unit: str) -> str:
    return (
        f"The register of the lane file {file} that its port writes, the file S{unit[1]}'s "
        f"{nibble} reads 110 bits below (`LANE_FILES`, `LANE_SOURCES`)"
    )


def _lane_ports() -> Iterator[tuple[str, tuple[Confidence, str]]]:
    """The lane ports' nibbles, each at the lowest tier of its enable's sources."""
    for lo, hi, owned, _ in B_PORTS:
        enable = _fid(lo, hi)
        if enable not in LANE_SOURCES:
            continue
        nibble = _fid(owned - 110, owned - 107)
        file = next(f for f, (_, *nibs) in LANE_FILES.items() if nibble in nibs)
        tier = min(t for _, t in LANE_SOURCES[enable].values())
        yield (
            _fid(owned, owned + 3),
            (tier, _lane_port(file, nibble, LANE_FILES[file][0])),
        )


F2_TAPS = (
    "Program 13's tap loads at words 432..435 reach F2 so: their datum words 435..438 have "
    "f261_262 = 2 (the ALU bus) on registers 3, 2, 4, 5, the ones f88_91 names at 439..442 "
    "(verified in the bytes)"
)
NIBBLES: dict[str, tuple[Confidence, str]] = {
    "f182_185": (V, _written("F0", "f255", "which S0 head 0x52 reads through f76_79")),
    "f186_189": (V, _written("F0", "f256_257", "the file f182_185's port writes too")),
    "f190_193": (
        I,
        _written(
            "F1", "f258", "the file f194_197's port writes too (inferred: one value)"
        ),
    ),
    "f194_197": (
        V,
        _written("F1", "f259_260", "which S1 head 0x54 reads through f84_87"),
    ),
    "f198_201": (
        V,
        f"{_written('F2', 'f261_262', 'which S2 head 0xe reads through f88_91')}. {F2_TAPS}",
    ),
    "f202_205": (
        V,
        _written(
            "F3",
            "f263_264",
            "which S3 reads through f92_95 as a lane number and through f96_99 as a value",
        ),
    ),
    **dict(_lane_ports()),
    "f179_181": (V, f"The register of O that its port writes; {STORE_FILE}"),
    "f69_71": (
        V,
        (
            "The register of O whose 32 lanes X0's store heads 0x1a, 0x1b, 0x1e and 0x1f write "
            f"to the lane memory (`X0_LANE_MEMORY`); {STORE_FILE}"
        ),
    ),
    "f141_144": (I, f"The register of U its port writes; {LANE_THIRD_FILE}"),
    "f23_30": (
        I,
        (
            "Bits 7..0 of S0's 16-bit lane immediate (`LANE_IMMEDIATES`; inferred, fitted: three "
            "values moved the low byte)"
        ),
    ),
    "f31_34": (
        I,
        (
            "S3's: bit 2 is bit 4 of the lane 0x10 inserts (verified on the camera: inserts at "
            "lanes 0..31 read back as built) and 0xe / 0xf extract; bits 3..2 are bits 5..4 of "
            "S3 0x11's immediate, and 0x16 / 0x17 ignore it (inferred, fitted); as a whole the "
            f"register of U (inferred): {LANE_THIRD_FILE}"
        ),
    ),
    "f37_40": (
        I,
        (
            "a, the register of the lane file L0 that S0's lane heads read first (`LANE_FILES`); "
            "also bits 11..8 of S0's immediate (`LANE_IMMEDIATES`) (inferred: pre-registered at "
            "register 1 only; S0 0x60 did not return its register at other numbers)"
        ),
    ),
    "f41_44": (
        I,
        (
            "b, the register of L0 that S0's lane heads read second (`LANE_FILES`; inferred: "
            "pre-registered at register 2 only)"
        ),
    ),
    "f45_48": (
        V,
        (
            "a, the register of the lane file L1 that S1's lane heads read first (`LANE_FILES`; "
            "pre-registered at registers 1 and 8)"
        ),
    ),
    "f49_52": (
        V,
        (
            "b, the register of L1 that S1's lane heads read second (`LANE_FILES`; "
            "pre-registered at registers 2 and 9)"
        ),
    ),
    "f53_56": (
        I,
        (
            "a, the register of the lane file L2 that S2's lane heads read first (`LANE_FILES`); "
            "under S2 head 0xc its low two bits are bits 4..3 of the lane read; bits 6..3 of S2's "
            "multiply immediate (`LANE_IMMEDIATES`) (inferred: pre-registered at register 1 only; "
            "the rest fitted)"
        ),
    ),
    "f57_60": (
        V,
        (
            "b, the register of L2 that S2's lane heads read second (`LANE_FILES`), and the register "
            "S2 head 0xc reads a lane of (inferred, fitted)"
        ),
    ),
    "f61_64": (
        I,
        (
            "a, the register of the lane file L3 that S3's lane heads read first (`LANE_FILES`; "
            "inferred: pre-registered at register 1 only)"
        ),
    ),
    "f65_68": (
        V,
        (
            "b, the register of L3 that S3's lane heads read second (`LANE_FILES`): the register S3 "
            "0xe, 0xf, 0x16 and 0x17 extract a lane of and 0x0..0x5 reduce (verified on the "
            "camera: built vectors and pre-registered writers read back at several registers); "
            "bits 9..6 of S3 0x11's immediate (inferred)"
        ),
    ),
    "f72_75": (
        I,
        (
            "s, the register of F0 that S0's lane heads read as their scalar (`LANE_OPERANDS`; "
            "inferred: S0's pre-registered function table at one register)"
        ),
    ),
    "f76_79": (
        V,
        (
            f"Under S0 head 0x52 the register of F0 it returns ({EXACT}); bits 15..12 of S0's "
            "lane immediate (`LANE_IMMEDIATES`; inferred, fitted)"
        ),
    ),
    "f80_83": (
        H,
        (
            "Its bits 3..2 are bits 6..5 of S1's lane immediate (`LANE_IMMEDIATES`; inferred, "
            "fitted: 0, 1 and 2 gave one immediate, 8 and 15 two others)"
        ),
    ),
    "f84_87": (
        V,
        (
            f"Under S1 head 0x54 the register of F1 it returns ({EXACT}); t, the scalar S1's lane "
            "heads read (verified on the camera by S1's pre-registered function table); bits 4..1 "
            "of S1's lane immediate (`LANE_IMMEDIATES`; inferred, fitted)"
        ),
    ),
    "f88_91": (
        V,
        (
            f"Under S2 head 0xe the register of F2 it returns ({EXACT}); t, the scalar S2's lane "
            "heads read (verified on the camera by S2's pre-registered function table). Under S2 "
            "0xc its bits 3..1 are bits 2..0 of the lane read, and bits 2..0 of the immediate S2 "
            "0x0 / 0x1 multiply by (`LANE_IMMEDIATES`) (inferred, fitted)"
        ),
    ),
    "f92_95": (
        V,
        (
            "S3's lane select: the lane itself under 0xe, 0xf and 0x10 (with f31_34 bit 2 as bit "
            "4), the register of F3 whose value is the lane under 0x15, 0x16 and 0x17 (& 31 "
            "under 0x16 / 0x17) (verified on the camera: inserts at lanes 0..31 read back as "
            "built, the writers pre-registered by nibble); bits 3..0 of 0x11's immediate and the "
            "signed shift count of 0x1b, 0x1c and 0x22 (inferred)"
        ),
    ),
    "f96_99": (
        V,
        (
            "The register of F3 whose value S3 0x18 broadcasts and 0x10 and 0x15 insert (verified "
            "on the camera, pre-registered by nibble); the one 0x24 returns and bits 13..10 of "
            "0x11's immediate (inferred)"
        ),
    ),
}
"""The grid nibbles the S heads' results tell about: their tier and what they name."""
IMM_NIBBLES = ("f23_30",)
"""Grid A fields that hold an immediate's bits rather than name a register."""


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
    confidence: Confidence = I,
    name: str = "",
) -> Field:
    owned = PORTS[_fid(lo, hi)]
    base = (
        f"enable of {owned}: {owned} changes in words where it is active in "
        f"{_PORT_P[owned]:.2f} of its changes, and it is active only where {owned} is not idle, "
        f"{OWNERSHIP}; {_c2_fact(lo, hi)}"
    )
    fact = f"{base}. {fact}" if fact else base
    return Field(
        lo,
        hi,
        Kind.OPCODE,
        unit,
        idle,
        confidence,
        fact,
        name=name,
        values=values or {},
    )


def _lane_enable(lo: int, fact: str = "") -> Field:
    enable = _fid(lo, lo + 1)
    port = PORTS[enable]
    nibble = _fid(int(port[1:4]) - 110, int(port[1:4]) - 107)
    file = next(f for f, (_, *nibs) in LANE_FILES.items() if nibble in nibs)
    rows = LANE_SOURCES[enable]
    lane = (
        f"The enable of the lane port that writes {file}[{port}], the file {nibble} reads: its "
        "value names the source it writes (`LANE_SOURCES`)"
    )
    values = {v: f"{source} to {file} ({TAG[t]})" for v, (source, t) in rows.items()}
    return _enable(
        lo,
        lo + 1,
        "pb",
        3,
        f"{fact}. {lane}" if fact else lane,
        values | {3: "idle"},
        min(t for _, t in rows.values()),
    )


LANE_ROLES = {
    "s0": "an adder / comparator with neighbour-lane forms and a 16-bit immediate",
    "s1": "logic, shifts and double-width shifts, min / max and add with a scalar or an immediate",
    "s2": "the multiplier",
    "s3": "moves, shuffles, lane insert, broadcast and the lane number",
}
"""What each S unit's lane functions amount to (inferred, from `LANE_FUNCTIONS`)."""


def _s_values(unit: str) -> dict[int, str]:
    lanes = LANE_FUNCTIONS[unit]
    scalars = S_RESULTS[unit]
    values = {}
    for h in sorted(set(lanes) | set(scalars)):
        parts = []
        if h in lanes:
            text, tier, note = lanes[h]
            parts.append(f"lanes: {text} ({TAG[tier]}{'; ' + note if note else ''})")
        if h in scalars:
            parts.append(f"scalar: {scalars[h]}")
        values[h] = "; ".join(parts)
    return values


def _head(unit: str, idle: int, fact: str) -> Field:
    lo, hi = HEADS[unit]
    base = (
        f"head of compute slot {unit.upper()}: its grid A operands change mostly in words "
        f"where it is active, {OWNERSHIP}; {_c2_fact(lo, hi)}"
    )
    results = ", ".join(f"{v:#x}" for v in S_RESULTS[unit])
    scalar = (
        f"Under the M strobe of its {1 << (hi - lo + 1)} values {results} "
        f"{'has' if len(S_RESULTS[unit]) == 1 else 'have'} a scalar result (`S_RESULTS`), "
        f"{S_NONE.get(unit, 'the others none')} (measured on the camera)"
    )
    lanes = (
        "Its values select lane functions, each tagged by value (`LANE_FUNCTIONS`); the unit: "
        f"{LANE_ROLES[unit]} (inferred)"
    )
    return Field(
        lo,
        hi,
        Kind.OPCODE,
        unit,
        idle,
        I,
        f"{base}. {fact}. {lanes}. {scalar}",
        values=_s_values(unit),
    )


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
    f = _grid(lo, hi, unit, fact)
    return replace(f, kind=Kind.IMM) if f.id in IMM_NIBBLES else f


def _grid_b(lo: int, hi: int) -> Field:
    fid = _fid(lo, hi)
    if fid == "f145_146":
        return _grid(lo, hi, "q", QUAD)
    return _grid(lo, hi, "pb", f"the port of enable {_OWNER[fid]}; {GRID_B_FACT}")


def _port_reg(
    lo: int, idle: int, fact: str = "", confidence: Confidence = H, name: str = ""
) -> Field:
    owned = _width_id(lo)
    port = f"Written under enable {_OWNER[owned]}"
    if confidence is H:
        port += " (hypothesis: a register write port)"
    return _reg(
        lo,
        "pr",
        idle,
        f"{port}. {fact}" if fact else port,
        name=name,
        confidence=confidence,
    )


FIELDS: tuple[Field, ...] = (
    _ctl(
        0,
        0,
        "s1",
        0,
        f"program 16 never sets it; {_only('f285_291 and f49_52 are')}; bit 0 of S1's lane "
        "immediate (`LANE_IMMEDIATES`; inferred, fitted: 1 added 1)",
        kind=Kind.IMM,
        confidence=I,
    ),
    _ctl(
        1,
        1,
        "x0",
        0,
        f"{_only('f302_307 is')}; bit 0 of {X0_IMM}",
        kind=Kind.IMM,
        confidence=V,
    ),
    _ctl(
        2,
        6,
        "x0",
        0,
        f"{_only('f302_307 is')}; bits 1..5 of {X0_IMM}",
        kind=Kind.IMM,
        confidence=V,
    ),
    _ctl(
        7,
        8,
        "x1",
        0,
        f"{_only('f308_312 is')}; bits 0..1 of {X1_IMM}",
        kind=Kind.IMM,
        confidence=V,
    ),
    _ctl(
        9,
        14,
        "x1",
        0,
        f"program 16 never sets bits 11 and 14; {_only('f308_312 is')}; bits 2..7 of "
        f"{X1_IMM}",
        kind=Kind.IMM,
        confidence=V,
    ),
    Field(15, 22, Kind.IMM, "op_slot", 0, V, IMM_FACT, name="imm"),
    *(_grid_a(lo, hi) for lo, hi in GRID_A),
    _reg(
        100,
        "x0",
        31,
        f"a, the A register X0 reads first ({EXACT}); it changes only where f302_307 is active",
        name="x0a",
        confidence=V,
    ),
    _reg(
        105,
        "x0",
        31,
        f"b, X0's second A register, and bits 6..10 of {X0_IMM}; it changes where f302_307 "
        "is active in 0.71 of its changes",
        name="x0b",
        confidence=V,
    ),
    _reg(
        110,
        "x1",
        31,
        f"a, the B register X1 reads first ({EXACT}); it changes where bits 310..312 of "
        "f308_312 are active in 0.78 of its changes",
        name="x1a",
        confidence=V,
    ),
    _reg(115, "x1", 31, XREG_FACT, name="xreg", confidence=V),
    Field(120, 122, Kind.REG, "op_slot", 7, V, FLAGS_FACT),
    _reg(123, "op_slot", 31, RA_FACT, name="ra", confidence=V),
    _reg(128, "op_slot", 31, T_HIGH, confidence=V),
    _const(133, 140),
    *(_grid_b(lo, hi) for lo, hi in GRID_B),
    _port_reg(
        206,
        0,
        "The register of A, X0's operand file, that f265_266 writes, its source by value "
        f"(`PR_SOURCES`; {EXACT}). X0 reads a write from the next word on; in the port's own "
        f"word it reads the old value ({EXACT}). Flipping its bits in program 16 words 67 and 68 "
        "leaves the copy unchanged (verified on the camera)",
        confidence=V,
        name="adst",
    ),
    _port_reg(
        211,
        0,
        "Flipping its bits in program 16 words 67 and 68 leaves the copy unchanged (verified on "
        "the camera); the only difference between words 66..68. The register of B, X1's "
        f"operand file, that f267_268 writes, its source by value (`PR_SOURCES`; {EXACT}); in "
        "the copy, ops of families B, C, D and E inserted after program 16's word 64 with "
        "f267_268 = 1 set the register it names to their result, which word 68 or 73 then read "
        "through its xreg, at pre-registered values (inferred, strong); program 16's word 64 "
        "writes its count r7 so",
        confidence=V,
        name="bdst",
    ),
    _port_reg(
        216,
        31,
        f"The register of B that f269 = 0 writes with X0's result ({EXACT})",
        confidence=V,
        name="bdst2",
    ),
    Field(
        221,
        223,
        Kind.REG,
        "pr",
        7,
        V,
        "3 bits between the register specifiers: the register of R (8 entries) that f270_271 = 1 "
        f"writes with the op's result, which a store in the very next word reads ({EXACT}); "
        f"equal to f120_122 in 66 % of all words, {OWNERSHIP}",
        name="rdst",
    ),
    _port_reg(
        224,
        0,
        f"{PARTNER_FACT}. The register of G that f272_273 writes: under 1 with the word's ALU "
        f"result, which the next word's ra and rb read ({EXACT})",
        confidence=V,
        name="gdst",
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
        f"1940 active words, so its port cannot be told apart from S3 in the bytes. {U_PORT_FACT}",
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
    _lane_enable(236, WORD504_NONE),
    _lane_enable(238, WORD504_NONE),
    *(_lane_enable(lo) for lo in range(240, 252, 2)),
    _enable(
        252,
        254,
        "pb",
        7,
        f"values 0..4 and the idle 7 only; {WORD504_NONE}. The enable of the store file's port, "
        f"its value the source (`STORE_SOURCES`): {STORE_FILE}",
        {v: f"{src} to O ({TAG[t]})" for v, (src, t) in STORE_SOURCES.items()}
        | {7: "idle"},
        min(t for _, t in STORE_SOURCES.values()),
    ),
    _enable(255, 255, "pb", 1, _pb("f255"), _pb_values("f255", 1), V),
    _enable(
        256,
        257,
        "pb",
        3,
        _pb("f256_257"),
        _pb_values("f256_257", 3),
        _pb_tier("f256_257"),
    ),
    _enable(258, 258, "pb", 1, _pb("f258"), _pb_values("f258", 1), _pb_tier("f258")),
    _enable(
        259, 260, "pb", 3, f"{X0_PATH}; {_pb('f259_260')}", _pb_values("f259_260", 3), V
    ),
    _enable(
        261, 262, "pb", 3, f"{X0_PATH}; {_pb('f261_262')}", _pb_values("f261_262", 3), V
    ),
    _enable(
        263,
        264,
        "pb",
        3,
        _pb("f263_264"),
        _pb_values("f263_264", 3),
        _pb_tier("f263_264"),
    ),
    _enable(
        265,
        266,
        "pr",
        3,
        f"The enable of A's write port on f206_210, its value the source ({EXACT}). Not the end "
        "op: idling it in program 16 word 79 (bit 229 clear) "
        "copied; idling it with bit 268 in word 60 times the job out (verified on the camera, "
        "one run each)",
        values=_pr_values("f265_266", 3),
        confidence=V,
        name="asrc",
    ),
    _enable(
        267,
        268,
        "pr",
        3,
        "Bit 268 is needed in program 16 words 63 and 65: setting it alone times the job out in "
        "63 and wedged the job (status -5) in 65, whose whole idle times out (verified on the "
        "camera, one run each); bit 267 is set in the stock words 60, 63, 65 and 79. The enable "
        f"of B's write port on f211_215, its value the source ({EXACT}); 2, which no stock word "
        "uses, writes nothing with the bus, X0 and X1 all carrying results. It writes no G "
        f"register ({EXACT}) and no A register (inferred)",
        values=_pr_values("f267_268", 3),
        confidence=V,
        name="bsrc",
    ),
    _enable(
        269,
        269,
        "pr",
        1,
        f"The enable of B's second write port on f216_220: 0 takes X0's result ({EXACT}), not "
        "the ALU bus (in the copy an inserted op's result under 0 did not reach the xreg set "
        "as word 68 saw it, verified on the camera, one job)",
        values=_pr_values("f269", 1),
        confidence=V,
        name="bsrc2",
    ),
    _enable(
        270,
        271,
        "pr",
        3,
        f"{SELECTOR}. The enable of R's write port on f221_223, its value the source ({EXACT}); "
        "a store in the very next word reads the register written; in a word with an idle op "
        "1 takes a load's datum three words after the load, or a multiply's product one word "
        f"after it ({EXACT})",
        values=_pr_values("f270_271", 3),
        confidence=V,
        name="rsrc",
    ),
    _enable(
        272,
        273,
        "pr",
        3,
        f"{SELECTOR}; in op 0 words with value 1, f224_228 names ra (307 of 336) {OWNERSHIP}, "
        "the loop counter's write-back; not needed in program 16 words 60 and 63, where idling "
        "it (272 alone in 63) copied (verified on the camera, one run each). The enable of G's "
        f"write port on f224_228, its value the source ({EXACT}); the next word's ra and rb "
        f"read the register written, no latency ({EXACT}). Under 1 in a word with an idle op it "
        "takes a load's datum three words after the load, or a multiply's product one word "
        f"after it, which the multiply's own word does not write ({EXACT}). Without an enabled "
        f"port a data op writes no register ({EXACT})",
        values=_pr_values("f272_273", 3),
        confidence=V,
        name="gsrc",
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
        f"changes under it: a mode of the compute group (hypothesis); {_c2_fact(274, 275)}. "
        "In the stock words it is 0 beside S0 head 0x52, 1 beside S1 0x54, 2 beside S2 0xe and "
        "3 beside S3 0x17 (verified in the bytes), but it does not select the S unit: a head's "
        "result passed under M at each of 0..3 (measured on the camera, one head per word; the "
        "selection reading pre-registered and refuted). What it selects is open",
    ),
    Field(
        276,
        276,
        Kind.FLAG,
        "m",
        1,
        V,
        f"the active-low strobe M of f274_275 (0 = active); {_c2_fact(276, 276)}. Under 0 an S "
        "head's scalar result goes on X0's result bus, where port f272_273 = 2 takes it, in the "
        "head's own word only; under 1 that port wrote nothing, and M or the port one or two "
        "words after the head took nothing (verified on the camera by exact readout, "
        "pre-registered). S3's reductions 0x0..0x5 are the exception: their result is on the "
        "bus from the third word after the head on and stays (verified on the camera, "
        "pre-registered), OR-ed with an extract's lane in the same word (measured)",
        values={
            0: "an S head's scalar result on X0's result bus (verified)",
            1: "idle",
        },
    ),
    _const(277, 277),
    _head(
        "s0",
        0x7F,
        "Merged from 278..280 and 281..284: the low part is never active without the high part, "
        "which is active alone in 5 % of its words, and both track f41_44 (inferred: one op "
        "field, possibly an op and a modifier); f23_30 is active only where it is. Of "
        "0x40..0x5f only 0x52 was read: a job of all 32 values under M timed out in the driver, "
        "which value stalls is not separated (measured on the camera)",
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
        f"field, possibly an op and a modifier); {WORD504}. 0xe, 0xf, 0x16 and 0x17 extract one "
        "16-bit lane of L3 and 0x0..0x5 reduce its 32 lanes to a scalar (`S_RESULTS`)",
    ),
    _ctl(
        302,
        307,
        "x0",
        0x3F,
        f"{X0_FACT}. Its heads 0x1a..0x21 load and store vectors of the lane memory beside "
        f"their scalar result (`X0_LANE_MEMORY`): {LANE_MEMORY}; {X0_PARITY}",
        kind=Kind.OPCODE,
        confidence=V,
        values=_x_values("x0"),
        name="x0op",
    ),
    _ctl(
        308,
        312,
        "x1",
        0x1F,
        X1_HEAD_FACT,
        kind=Kind.OPCODE,
        confidence=V,
        values=_x_values("x1"),
        name="x1op",
    ),
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
    f"all of grid B there, byte for byte ({_CAMERA}); what that word writes to F2 is open, not "
    "the loaded tap coefficient itself (inferred: the lines it changes are not those where that "
    "tap is non-zero)",
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
    "active owner is not sufficient for an effect; X0's head there is 0x3, no store head "
    "(verified in the bytes)",
    **{k: _HELD_439 for k in ("f37_40", "f45_48", "f49_52", "f65_68", "f80_83")},
    "f41_44": f"{_HELD_439}. Program 13 words 446 and 448, S0's head active: each value tried "
    "(2, 4, 5, 6 at 446; 1, 4, 5, 6 at 448) changes only the first plane, every byte 0, 255 or "
    "the low byte of the unclamped luma (verified on the camera, one run each); post hoc, the "
    "byte is 0, 255 or that low byte by the sign of the sum of the two words' selected "
    "operands, in the stock words range states of the taps 0 + 1 partial sum and of the full "
    "sum (inferred: a saturation select); the values name S0's b, a register of L0 "
    "(`LANE_FILES`), not an S2 operand register",
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
    "that the value selects (hypothesis). The value is also bits 15..12 of S0's lane immediate "
    "(`LANE_IMMEDIATES`, inferred); how one immediate gives a per-lane mask is open",
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
CAMERA_TIERS = {"f244_245": V}
"""Fields whose tier the camera raised above the bytes': an idled enable equal to idling its
port or its word (verified)."""
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

X_IMMEDIATES = {
    "x0": Composite(
        "x0k",
        (BY_KEY["f1"], BY_KEY["f2_6"], BY_KEY["x0b"]),
        V,
        f"X0's 11-bit immediate K, f1 | f2_6 << 1 | f105_109 << 6 ({EXACT})",
    ),
    "x1": Composite(
        "x1k",
        (BY_KEY["f7_8"], BY_KEY["f9_14"], BY_KEY["xreg"]),
        V,
        f"X1's 13-bit immediate K, f7_8 | f9_14 << 2 | f115_119 << 8 ({EXACT})",
    ),
}
"""Each X unit's immediate; its high bits are the register field of its operand b."""

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
    "f128_132 = 31, a sign extension); used as it is, unscaled, sign- or zero-extended by op "
    f"({EXACT}, `FAMILIES`)",
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
RING_STEP = (
    "a ring pointer step: s = zext(ra) + sext(R), the result s + L if s < 0, s - L if s >= L, "
    f"else s ({EXACT} at 216 points)"
)
LOAD_DATUM = (
    "a load's datum goes to no register by itself: it appears on the ALU result bus exactly "
    "three words after the load, where a word with an idle op and f272_273 = 1 or f270_271 = 1 "
    "takes it; a data op in that word wins and the datum is lost; consecutive loads deliver in "
    f"consecutive words ({EXACT})"
)
FAMILIES = (
    Family(
        "A",
        tuple(range(0x0C, 0x1B, 2)),
        False,
        13,
        "ra and a signed 13-bit immediate: loads and stores of width w (bytes 0x0c..0x10, "
        "halfwords 0x12..0x16, words 0x18, 0x1a) in local memory, 0x2000 bytes with a stack at "
        f"its top, r0 the stack pointer, at the byte address ra + w x imm13 ({EXACT}), taken "
        f"modulo 0x2000: no DRAM, register or lane state is reachable ({EXACT}); imm13's "
        "high bits, once read as a flag, are the offset's sign. Stores 0x18 / 0x12 / 0x0c write "
        "the low 32 / 16 / 8 bits of R[f120_122]; loads 0x1a 32 bits, 0x16 signed and 0x14 "
        f"unsigned 16, 0x10 unsigned and 0x0e signed 8 ({EXACT}); {LOAD_DATUM}; each op's "
        "register form is the op + 1",
        signed=True,
    ),
    Family(
        "A'",
        tuple(range(0x0D, 0x1C, 2)),
        True,
        0,
        "ra and rb, imm 0 in every word: family A's accesses at ra + w x rb, stores 0x19 / 0x13 "
        f"/ 0x0d, loads 0x1b, 0x17 (s16), 0x15 (u16), 0x11 (u8), 0x0f (s8) ({EXACT})",
    ),
    Family(
        "B",
        (0x0A, 0x0B, 0x37, 0x38),
        False,
        16,
        "ra and a wide immediate, f120_122 its bits 13..15 (13..14 in 0x37, whose f120_122 is "
        "0..3 in every word): 0x0b the sign-extended imm16, ra not used; 0x37 ra | imm15; 0x38 "
        f"ra & the sign-extended imm16 ({EXACT}); 0x0a ra | (imm16 << 16) (inferred)",
    ),
    Family(
        "C",
        (0x3E, 0x3F, 0x40),
        True,
        5,
        "ra, rb and an immediate 0..31 in every word: 0x3e ra >> imm5 arithmetic, 0x3f ra >> "
        "imm5 logical, 0x40 ra << imm5, ra shifted, not rb (inferred, by exact readout); rb and "
        f"f120_122 are encoded but had no effect on the result ({EXACT}); 16 the most common "
        "count of all three in the stock words",
    ),
    Family(
        "D",
        (*range(0x1C, 0x22), *range(0x23, 0x26), 0x36),
        False,
        16,
        "ra and a 16-bit immediate imm16 = imm | f128_132 << 8 | f120_122 << 13, unscaled; "
        "sign-extended to c in 0x1c / 0x1d / 0x20 / 0x21 (ra != c, ra == c, ra <= c, ra < c "
        f"signed, 1 or 0), 0x24 / 0x25 (max / min signed) and 0x36 (ra ^ c) ({EXACT}); "
        "zero-extended in 0x1e / 0x1f (ra - imm16, ra + imm16) and 0x23 (ra < imm16 unsigned) "
        "(inferred); f120_122 is 0 in 99 % of the stock words (`semantics.OP_SEMANTICS`)",
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
        "ra, rb and f120_122, imm 0 in every word; register-register ALU ops, 32-bit wrap "
        f"({EXACT} unless tagged): 0x08 / 0x09 rb, 0x32 abs(rb) (inferred), 0x3c ra & rb, 0x3a "
        "ra | rb, 0x34 ra - rb, 0x35 ra + rb, 0x33 -rb, 0x3d ~ra, 0x2f min signed, 0x2e max "
        "signed, 0x2c max unsigned (inferred), 0x41 ra >> (rb & 31) arithmetic, 0x42 logical "
        "(inferred), 0x43 ra << (rb & 31), compares to 1 or 0: 0x28 / 0x29 ra <= rb, ra < rb "
        "unsigned, 0x2a / 0x2b signed, 0x30 / 0x31 ra != rb, ra == rb; f120_122 has no effect "
        "but in 0x47, family F's ring pointer step with R[f120_122] and L = rb; 0x48 and 0x49 "
        "give no ALU result, change no operand and do not stall, their function open; no op "
        "screened multiplies",
    ),
    Family(
        "F",
        (0x44, 0x46),
        False,
        13,
        "ra and a 13-bit immediate whose f128_132 is 4 or 8 (0x404, 0x408, 0x40c, 0x460) in the "
        f"stock words: {RING_STEP}; R = 1 in 0x44 and R[f120_122] in 0x46, L the low 8 bits of "
        "imm13",
    ),
)
IMM_BITS = {0x37: 15}
"""Ops whose immediate is narrower than their family's."""
SIGNED = {0x0B: True, 0x38: True, 0x1E: False, 0x1F: False, 0x23: False}
"""Ops whose immediate's extension differs from their family's: True sign-extended."""
MEANINGS = {
    0x09: "the op at 294 of 381 call targets, often with ra = 31: a routine prologue (inferred "
    f"from its position); it passes rb ({EXACT})",
    0x0A: "periodic immediates such as 0x5555 and 0x3333: ra | (imm16 << 16) (inferred), a "
    "high-half set-up (hypothesis)",
    0x0B: "the most varied immediates, often in a call's delay slots: the sign-extended imm16, "
    f"ra not used ({EXACT})",
    0x0C: f"a byte store ({EXACT}); its immediates 0x1fxx are small negative offsets",
    0x0E: f"a signed byte load ({EXACT}); every one of its 8 reads of r0-based slots follows a "
    "store to them across later firmware versions of the same family",
    0x10: f"an unsigned byte load ({EXACT}); in program 13 every non-zero immediate lies in its "
    "tap block 0x00..0x1f",
    0x12: f"a halfword store ({EXACT}); across later firmware versions of the same family, the "
    "first touch of r0-based slots in 108 of its 114 accesses of them, and entry spills at "
    "-64..-1 B",
    0x14: f"an unsigned halfword load ({EXACT})",
    0x16: f"a signed halfword load ({EXACT}); in program 13 every non-zero immediate lies in its "
    "tap block 0x00..0x1f, in runs that step 0, 1, 2, 3 with one ra per run; on the camera, "
    "imm 1 -> 0 in the resampler's tap-1 read gave tap 1 tap 0's coefficient byte for byte "
    "(verified at that word): the imm the tap index",
    0x18: f"a word store ({EXACT}); across later firmware versions of the same family, it opens "
    "88 of the 95 r0-based word slots of the E-M10 II programs, with entry spills and exit "
    "restores by 0x1a at -64..-1 B",
    0x1A: f"a word load ({EXACT}); program 16's three 0x1a words read src, dst and len at one "
    "ra and immediates 0, 1 and 2; idling the one at 2 on the camera stores 16 KiB of zeros "
    "whatever len is, idling the other two leaves dst unwritten",
    0x1E: "at a routine's entry + 1 in 67 routines: ra - imm16 (inferred), a frame allocate "
    "there (hypothesis)",
    0x1F: "in 300 return delay slots, its immediate a multiple of 4 in all: ra + imm16 "
    "(inferred), the frame release there (hypothesis)",
    0x36: f"imm 1, f128_132 and f120_122 0 in all 53 words: ra ^ 1 there ({EXACT} for the "
    "rule), a toggle (hypothesis)",
    0x37: f"periodic immediates such as 0x5555 and 0x2222: ra | imm15 ({EXACT}), a lane or "
    "phase mask set-up (hypothesis)",
    0x38: "1022 of 1321 immediates are 2^k - 1 or its complement: ra & the sign-extended imm16 "
    f"({EXACT})",
    0x3F: "ra >> imm5, logical (inferred, by exact readout)",
    0x40: "ra << imm5 (inferred, by exact readout)",
    0x47: f"{RING_STEP}; R = R[f120_122], L = rb",
    0x48: "the multiply as 0x49, the same 32 bits on every operand pair; no stock word uses it "
    f"({EXACT}); what sets it apart from 0x49 is open",
    0x49: "the multiply: (ra x rb) & 0xffffffff on the ALU bus one word after the op, nothing in "
    "the op's own word and nothing two or three words on; a port of the next word takes it, "
    f"also when that word holds the next multiply ({EXACT}, 48 of 48 values on eight operand "
    "pairs of both signs); the high half was not seen",
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
            formats[value] = Format(family, bits, SIGNED.get(value, family.signed))
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
