"""The field table of the 320-bit DSP word: the single source of truth for encoding.

Bit k of a word is bit (k % 8) of byte k // 8 of its 40 bytes. Every bit belongs to exactly one
field. A field has a permanent positional id that names both ends of its bits (`f115_119`, `f229`),
a semantic name where its role is measured (`NAMES`, its role `ROLES`), a kind, its value in the
idle word, a confidence and a fact.

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

import keyword
import re
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

    @property
    def span(self) -> int:
        """Its bits in the word."""
        return self.mask << self.lo

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
"""Where the segmentation alone does not decide a cut: the boundaries between the enables
f252_254 and f255, f256_257 and f258 (PB) and f265_266 and f267_268 (PR) lie where the two bits
across the boundary share little information and each side owns another field (between 241 and
242 mainly the ownership); the one between f274_275 and f276 by their values."""


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
    "emulator keeps the two apart (`machine.Machine`'s files G and B)"
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
    "0x2d, 0x39, 0x3b and 0x45 never occur (those seven run on the camera as data ops: "
    "`UNSEEN_FORMATS`). Bits 17, 18 set with 128, "
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
        V,
        "a jump to T after four delay slots that writes no link (verified on the camera: r1 "
        "kept its value across an op 3 without a link word). A call is an op 3 whose delay "
        "slots hold 0x0b #(i + 5) with f272_273 = 1 on r1: 24 of the 42 op 3 words of the five "
        "stock programs (verified in the bytes), program 16's word 0 among them, which calls "
        "its job routine at 10; calls two deep with the link saved in another register ran as "
        "written (verified on the camera). The other 18 (forward, after word 0, no return "
        "before T) are jumps: program 13's at word 210 runs in a job (verified on the camera: "
        "a halt there cuts the job). In the stock words f120_122 is 0 or 4 (verified in the "
        "bytes); an op 3 built with f120_122 = 7 left its program (measured on the camera), so "
        "its bits 0..1 may extend T (hypothesis)",
    ),
    4: Op(
        "ret",
        V,
        "a jump, after four delay slots, to the word G[f128_132] names (verified on the "
        "camera at f128_132 = 1 and 21, the register set just before); f128_132 is 1 in 15 of "
        "the 18 returns of the five stock programs and 3, 7 and 2 in the others (verified in "
        "the bytes): r1 is the link by convention, written in the call's delay slots, and a "
        "routine that calls reloads it from its frame before its op 4",
    ),
    7: Op(
        "halt",
        I,
        "ends the job: word 5 of every stock program and no other word, where the call at word "
        "0 returns (inferred); at any word and call depth: program 16 with word 5 in place of "
        "word 11, inside the routine with the call's return pending, ended with status 0 and "
        "dst not written (verified on the camera, one run), so a halt needs no return and the "
        "return stack is not checked (inferred); three words behind a halt still run: of five "
        "stores there the first three wrote, and the job ended after them also with an op 3 or op 4 "
        "second or a halt first among them (verified on the camera, pre-registered); they are "
        "the next three words fetched, so a taken jump due among them puts the target's words "
        "there (verified on the camera with the halt in slot 2 or 3 of an op 4; fitted with "
        "the halt in slot 2, 3 or 4 of an op 3) and one due after them is not taken (halt in "
        "slot 1 of an op 3, verified); a halt third among them, a target's own, ended the job "
        "(fitted)",
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
        basis = UNSEEN_FORMAT_FACTS.get(value, FAMILY_BASIS)
        fact = f"{DATA_OP_FACT}; family {fmt.family.name} ({basis}): {fmt.family.fact}"
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
    0x06: ("a <= zK unsigned", V, "the zero extension pre-registered at K's top value"),
    0x07: ("a < zK unsigned", V, "the zero extension pre-registered at K's top value"),
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
TRANSFER_HEADS = (0x1C, 0x1D)
"""X1's transfers to the stream engine (`stream.Engine`) with register a (f110_114) =
`STREAM_SELECT`: 0x1c hands B[f115_119], 0x1d waits for one command; no result, no R, G or B
register changed."""
WAIT_HEAD = 0x1D
STREAM_SELECT = 0
"""The register a of X1 0x1c and 0x1d that meet the stream engine: a 0x1c hands its value, a 0x1d
waits (verified on the camera: a surplus wait hung, the engine's depth, loads landed only behind
their wait). A 0x1c with another a is not measured (one lone 0x1c with a = 8 returned, whether
its value was handed not known), nor a 0x1d with an a outside `WAIT_NOTHING` (31 included, the
idle word's)."""
WAIT_NOTHING = frozenset({1, 2, 8})
"""The registers a with which X1 0x1d hands nothing to the stream engine and returns (inferred:
a = 2 in an op 3 word and in an idle word, a = 1 in an op 3 word, one job each, the load behind
them not landed; a = 8 a lone 0x1d in one job). The resampler's 0x1d in words 210 (a = 2) and
312 (a = 1) are such. Reading (hypothesis): a selects what a 0x1d waits for, and only
`STREAM_SELECT` is the stream engine's queue."""
DIVIDE_HEAD, DIVIDE_LAG = 0x1E, 4
"""X1 0x1e divides a by b, signed, truncated toward zero, and puts the 32-bit quotient on X1's
result bus exactly `DIVIDE_LAG` executed words after the head, nothing in its own word (verified
on the camera, pre-registered, for a >= 0 and b > 0 at lags 0..8; a negative dividend inferred
from one pair; b <= 0 not measured)."""
X1_ONLY: dict[int, tuple[str, Confidence, str]] = {
    0x16: ("~a & b", V, ""),
    0x17: ("~a & ~b", V, ""),
    0x18: ("a ^ b", V, ""),
    0x19: ("a | b", V, ""),
    0x1A: ("a & b", V, ""),
    0x1B: ("~b", V, ""),
    **{
        h: (f"{NO_RESULT}: a transfer to the stream engine", V, "")
        for h in TRANSFER_HEADS
    },
    DIVIDE_HEAD: (
        f"{NO_RESULT} in its own word: a / b signed on X1's result bus {DIVIDE_LAG} words later",
        V,
        "a negative dividend inferred, b <= 0 not measured",
    ),
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
    "the camera by exact readout). Its values 0x1c and 0x1d, once read as a separate transfer "
    "unit X2 with 0x1e, put no result on any bus and change no R, G or B register (verified on "
    "the camera by exact readout, a stock copy clean after each): 0x1c hands b to the stream "
    "engine, 0x1d waits for one of its commands (`stream`). 0x1e is no transfer but a signed "
    "divide a / b whose quotient is on X1's result bus four words after the head "
    "(`DIVIDE_HEAD`; verified on the camera, pre-registered). xreg changes in 0.81 of the words with "
    "bits 308..309 at 0, 0.58 at 1 and 0.61 at 2 (at any value of 310..312) against 0.27 "
    f"overall, {OWNERSHIP}. {XFER}; in program 16 "
    "words 58..79, the 0x1c words (bit 308 clear) stall the job when idled. X1 is X0's copy on "
    "the register file B, a = B[f110_114], b = B[f115_119], K = f115_119 << 8 | f9_14 << 2 | "
    "f7_8 (13 bits): logic functions at 0x16..0x1b where X0 has none, the transfers at "
    f"0x1c, 0x1d and the divide at 0x1e (`X_FUNCTIONS`; {EXACT}). Its result goes to A through f265_266 = 1, to B "
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
Q_NULL = (
    "In own programs f35_36 = 0..2 on a load head's word, on its port word, beside S3 0x19, "
    "beside S0 0x20 and on a write of O with its store head, and f145_146 = 0..2 under f234_235 "
    "= 0..2 beside S3 0x19, changed nothing (measured on the camera, 24 cells x 8 lanes, each "
    "expected unchanged before the run): no use of Q is known"
)
QUAD = (
    "Q, a section mode of the grid (hypothesis): f35_36 and f145_146 are set only in some "
    "programs across later firmware versions of the same family, never on the E-M10 II, equal "
    "in all words where both are set but 14, set over long runs and changed only where X0 is "
    "active; where set, f65_68, f69_71 and f179_181 are active and f80_87 / f190_197 mostly idle. "
    f"{Q_NULL}"
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
its sentinel): every cell verified, pre-registered and met on fresh values (f256_257 / 2 at one
register, f258 / 0 at three of F1, f263_264's three read back both by S3 0x18 and by S3 0x24).
Under an X0 source
with X0 idle the port stores a value that is no result of the word (measured on the camera).
X0's result bus is also M's (`S_RESULTS`): f255 = 0 takes S0's compare mask into F0 and f258 = 0
S1's into F1, and f255 = 0 under f274_275 = 3 S3's held reduction into F0 (each verified on the
camera, pre-registered; S3 0x0's maximum read back by S0 0x19 against that F0 register)."""


SCALAR_FILES = (
    "F0..F3, four files of 32-bit registers on the compute side, numbered by a nibble: the grid "
    "B ports of f255 and f256_257 (F0), f258 and f259_260 (F1), f261_262 (F2) and f263_264 (F3) "
    "write them, and S heads read them from the next word on (`SCALAR_READS`). Registers 0, 3 "
    "and 5 of F1 held three values at once, a 32-bit value came back whole from each of the "
    f"four files, and a write to F0, F1 or F2 left the other two files' register 0 as it was ({EXACT})"
)
SCALAR_READS: dict[str, tuple[int, str, str]] = {
    "s0": (0x52, "F0", "f76_79"),
    "s1": (0x54, "F1", "f84_87"),
    "s2": (0xE, "F2", "f88_91"),
    "s3": (0x24, "F3", "f96_99"),
}
"""The S heads that return a scalar register: (head value, file, the nibble naming the register),
the result file[nibble] (on the camera by exact readout, through the M strobe and port f272_273
= 2, and S3's also against f263_264's three sources; verified, pre-registered)."""


def _pb_values(enable: str, idle: int) -> dict[int, str]:
    to = PB_FILES[enable]
    return {
        v: "writes nothing (verified)" if src is None else f"{src} to {to} (verified)"
        for v, src in PB_SOURCES[enable].items()
    } | {idle: "idle"}


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
boots (verified on the camera). A register a port writes in word t is read by a head in word
t + 1 (verified on the camera, pre-registered against a sentinel in the register, five pairs:
L3 through f250_251 = 0 (S3 0x18's result) then S3 0x19, L2 through f244_245 = 0 (a load) then
S2 0x2, L3 through f250_251 = 1 (S2's product) then the reduction S3 0x2; L0 written by S0's
port then read by S0, L1 by S1's then S1); a head in the port's own word reads the old
content, in every lane file and in U (verified on the camera, pre-registered)."""
LANE_THIRD_FILE = (
    "U, a third kind of lane register file, 16 registers named by f31_34 (verified on the "
    "camera, pre-registered: sixteen values written through f141_144 = 0..15 read back by "
    "number), whose lanes 0..3 are the "
    "carry lanes of S3's lane shifts: a shift up by n = 1..4 fills its top n lanes from U's lanes "
    "0..n-1, a shift down by m = 1..4 its bottom m lanes from U's lanes 4-m..3 (verified on "
    "the camera, pre-registered, 0x1b). Its port is f141_144 under f232_233 = 1, "
    "beside an S3 head, which decides what U takes, not its lane result: after 0x1c, 0x1f and "
    "0x21 U's lanes 0..3 held b's lanes 0..3, after 0x1b, 0x1e and 0x20 b's lanes 28..31, after "
    "0x22 nothing (verified on the camera, pre-registered, lanes 0..3 read back as the top lanes "
    "of S3 0x1b by 4): the next vector's first lanes for a shift up, the previous vector's last "
    "lanes for a shift down (inferred). So the lane shifts 0x1b, 0x1c and 0x22, equal in their "
    "lanes, differ in what they save: 0x1b the lanes a shift down of the next vector needs, 0x1c "
    "those a shift up of the previous one needs, 0x22 none (the reading inferred); in the stock programs 0x22 never "
    "stands beside U's port and 0x1b and 0x1c always do (verified in the bytes). f141_144 names the register written and f31_34 the one the shifts read: U[3] "
    "and U[9], written in consecutive heads with values apart in every one of lanes 0..3, read "
    "back as written through S3 0x1b by 4 with f31_34 = 3 and 9, also after later writes to "
    "U[5] (verified on the camera, pre-registered); under 0 and 2 beside 0x1f U[5] kept a known "
    "value and under 1 in the same word took the new one (verified on the camera, "
    "pre-registered, lanes 0..3), so 1 is the write enable among 0..2; 0x18, 0x19 and 0x14 "
    "under 1 wrote nothing and 0x1a and 0x1d a content not identified (inferred, one "
    "run); U's lanes 4..31 were not read"
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
    "f236_237": {0: (LANE_LOAD, V), 1: (S2_HIGH, V), 2: (S0_RESULT, V)},
    "f238_239": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
    "f240_241": {0: (LANE_LOAD, V), 1: (S2_HIGH, V), 2: (S0_RESULT, V)},
    "f242_243": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
    "f244_245": {0: (LANE_LOAD, V), 1: (S0_RESULT, V)},
    "f246_247": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
    "f248_249": {0: (LANE_LOAD, V), 1: (S0_RESULT, V)},
    "f250_251": {0: (S3_RESULT, V), 1: (S2_LOW, V), 2: (S1_RESULT, V)},
}
"""The lane ports' enables: what each value writes, with its tier. Measured on the camera with a
sentinel in the named register, one enable value per word, every cell pre-registered (verified):
f238_239 and f244_245..f250_251 first; f240_241 / 1 on all 32 lanes of a product
(`S2_HIGH_HALVES`), f242_243 / 1 as its low half; then f236_237 / 1 (S2 0x2's high half) and
f236_237 / 2 and f240_241 / 2 (S0's result), each at one register, and f242_243 / 2 (S1's
result) at two, one word after the head. Enable 0 of the four first ports (f236_237, f240_241,
f244_245, f248_249) takes the vector an X0 load head (`LANE_MEMORY`) asked for exactly four
words earlier, into all four files (L0 at three registers, L1 at five), 32 lanes each; at lag 3
or 5, or with no head, the port writes nothing (verified on the camera, pre-registered
sentinels); one load reaches all four first ports in the same word (verified on the camera,
pre-registered, 16 lanes each). In the five stock programs every word with an X0 load head has
such a port write (or O's, `STORE_SOURCES`) four words later and no such write lacks one
(verified in the bytes, 379 words), so idling f244_245 = 0 in program 13 drops a load
(inferred). Enable 2 of f244_245 / f248_249 wrote nothing from any S head. A port takes a head's
result at the lag `LANE_LAG` gives for the heads timed."""


def _lags(
    lag: int, tier: Confidence, *heads: int | range
) -> dict[int, tuple[int, Confidence]]:
    out = {}
    for h in heads:
        out |= dict.fromkeys(h if isinstance(h, range) else (h,), (lag, tier))
    return out


LANE_LAG: dict[str, dict[int, tuple[int, Confidence]]] = {
    "s0": _lags(1, V, range(0x8), range(0xE, 0x12), range(0x14, 0x18), 0x1A, 0x1B, 0x1D)
    | _lags(1, V, range(0x08, 0x0E), 0x1E, 0x1F, range(0x20, 0x24), range(0x26, 0x2E))
    | _lags(1, V, range(0x34, 0x40), 0x1C)
    | _lags(0, V, range(0x41, 0x47), 0x4B, 0x4D)
    | _lags(1, V, range(0x60, 0x7F)),
    "s1": _lags(0, V, range(0xA), 0x53, range(0x60, 0x7F))
    | _lags(1, V, range(0xA, 0x41), range(0x47, 0x53)),
    "s2": _lags(1, V, range(0x7)) | _lags(0, V, 0x7, 0x8, 0x9, 0xA, 0xB, 0xD),
    "s3": _lags(1, V, range(0xA, 0xE))
    | _lags(0, V, range(0x10, 0x16), 0x18, 0x19, 0x1B, 0x1C, range(0x20, 0x24))
    | _lags(1, V, 0x9)
    | _lags(1, V, 0x6, 0x7, 0x8),
}
"""Words from a head to the one word whose lane port takes its result, with the tier, by head (verified on the camera,
pre-registered with the rule's lanes at that lag and a sentinel at the other: per head one cell
with the unit's port in the head's word only and one in the next word only, 32 lanes each). A
port at the other lag writes nothing: a result is on its unit's bus for exactly one word. At 0:
S0's selects 0x41..0x46, 0x4b and 0x4d, S1's logic heads 0x00..0x09 and 0x53, S2's passes 0x8
and 0xd, sign masks 0x9 / 0xb and lane number 0xa, S3's moves, inserts, broadcast, lane number and lane shifts; at 1: every other S0 and
S1 head timed, S2's multiplies and S3's shuffles 0xa..0xd. No head class predicts the lag across
units. S2 0x9, 0xa and 0xb at 0 were measured against a registration at 1, then pre-registered at
0 and met (verified). S0's clamps 0x9..0xd and carry-in adds 0x1e, 0x2a..0x2d and S1's floor
shifts 0x16..0x19, 0x1f..0x22 and carry-in heads 0x32, 0x3d..0x40 at 1: pre-registered on four
lanes each (verified). S2 0x7 at 0: pre-registered with the port in the head's word (verified); a port
one word later wrote nothing (measured). S3 0x7, 0x8 at 1 and S1 0x60..0x7e at 0, S0's at 1:
pre-registered (verified); S3 0x6 at 1: 0 in every lane there and nothing in its own word,
pre-registered on two fresh operand sets in two jobs and the earlier lag runs (verified). The other heads' lags
are open (hypothesis: as their nearest timed relative)."""

ALONE_ZEROS: dict[str, tuple[range, int, int]] = {
    "s0": (range(0x54, 0x60), 1, 1),
    "s1": (range(0x57, 0x60), 0, 0),
}
"""S heads that give 0 at one lag and nothing at the other in the form they ran on the camera, by
unit: (heads, lag, earlier runs). The form: in a job with no other S0, S1 or S2 head (the
operands' setup before them ran S3, X0 and X1 heads), the head value run twice, each run with
no other S head in its word, with the register selects of `ALONE_SELECTS`, and its result taken
by the port of `ALONE_PORTS` alone, in the head's word only on the first run and one word later
only on the second; lanes 0..15 read at each: 0 at the lag given, the sentinel at the other, on
three operand sets (edge values, a == b lanes, and a != b with both nonzero in every lane), the
last pre-registered (verified). So the zeros were read on the run after the earlier runs given
(S0's second run, S1's first) and the other lag's nothing on the other run: each verified on
the run where it was read and inferred on the other; from a third run on, after a run in
another form, with the port in both words or with another reader of the unit's result, not
predicted. In jobs with other heads
before them S1 0x55..0x5f gave b, or left the sentinel at either single lag (`LANE_FUNCTIONS`).
Lanes 16..31 were not read. S0's M results beside these heads and its latched operand after them
stay unknown; S1's M follows the rules of its heads without a scalar result."""
ALONE_ZERO_LANES = range(16)
"""The lanes read in `ALONE_ZEROS`' runs."""
ALONE_SELECTS = {
    "s0": {"f37_40": 1, "f41_44": 2, "f72_75": 4, "f76_79": 5, "f23_30": 0x5A},
    "s1": {"f45_48": 1, "f49_52": 2, "f80_83": 4, "f84_87": 5, "f0": 0},
}
"""The fields of the head's word that select its operands, scalars and immediate in every
`ALONE_ZEROS` run."""
ALONE_PORTS = {"s0": ("f248_249", 1), "s1": ("f250_251", 2)}
"""The lane port (an L3 port) and its enable that took the result in every `ALONE_ZEROS` run."""

LANE_VECTORS = 0x400
"""The lane memory's size in vectors of 32 lanes; an address counts modulo it (`LANE_MEMORY`)."""
VECTOR_BYTES = 64
"""A lane-memory offset counts bytes of 16-bit elements: 64 per vector."""
STREAM_CHANNELS = 4
"""The stream engine's channels (`stream`)."""
STREAM_LOAD, STREAM_NOTHING_MOVES, STREAM_STORE = 0, 1, 6
"""The stream command kinds measured (`stream`)."""
STREAM_LANE_MEMORY, STREAM_LOCAL_MEMORY = 1, 2
"""A stream channel's r0: the DSP-side memory (`stream`)."""
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
    "Timing: a store head in the word of O's port write stores the old content of O, one, two "
    "or three words later the new content; a load one or two words after a store to the same "
    "vector reads the stored vector, and a store one, two or three words after a load of its "
    "vector does not change what the load delivers (verified on the camera, pre-registered); "
    "four loads in consecutive words land in four consecutive port words (measured on the "
    "camera, not pre-registered: inferred). It keeps its content across jobs within a boot (verified on the "
    "camera, pre-registered: vectors 0x40..0x7f written by one job gave a later job's exact "
    "output), so "
    "a job can leave data, such as a layer's weights, for the next"
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
pre-registered, 11 groups of 32 lanes exact); 5 and 6 (`STORE_NOTHING`) never occur in the
stock programs, 7 is idle. The heads tried into O, S3's lane number and broadcast, S2's pass of b
and S1 0x04, are taken in the head's own word, the arithmetic heads tried one word after it, as
at the lane ports (`LANE_LAG`)."""
STORE_NOTHING = (5, 6)
"""Values of f252_254 under which O keeps its content, beside a result of every S unit and with
none (verified on the camera, pre-registered on a second register and other heads)."""

M_UNITS = ("s0", "s1", "s2", "s3")
"""f274_275's values: the unit whose result M puts out."""
U_TAKES = {
    0x1F: range(4),
    0x21: range(4),
    0x1C: range(4),
    0x1B: range(28, 32),
    0x1E: range(28, 32),
    0x20: range(28, 32),
}
"""What U's port takes beside an S3 head under f232_233 = 1: b's lanes into U's lanes 0..3
(verified on the camera, pre-registered); U's lanes 4..31 are not known after a write."""
U_NOTHING = (0x18, 0x19, 0x22, 0x14)
"""S3 heads beside which U's port wrote nothing (measured on the camera)."""
U_UNIDENTIFIED: dict[int, Confidence] = {0x1A: I, 0x1D: I}
"""S3 heads beside which U's port wrote U's lanes 0..3 with a content not identified, with its
tier (inferred, one run on the camera); U's lanes 4..31 were not read in that run. Beside these and every head in neither
`U_TAKES` nor `U_NOTHING` what U's port writes is open."""
U_SAVED = "lanes 0..3 the S3 head beside it saves (`U_TAKES`)"
U_SOURCES: dict[int, tuple[str, Confidence]] = {1: (U_SAVED, V)}
"""What f232_233 writes to U[f141_144] by value: 1 what the S3 head beside it saves; 0 and 2
write nothing (`LANE_THIRD_FILE`, verified on the camera)."""
VECTOR_PORTS = {"f252_254": ("O", STORE_SOURCES), "f232_233": ("U", U_SOURCES)}
"""The ports of O and U: the file and its sources by enable value."""
X0_PARITY = (
    "each head and its even / odd twin (0x1a / 0x1b, 0x1c / 0x1d, 0x1e / 0x1f, 0x20 / 0x21) "
    "showed no difference in address, lanes covered, element width or scalar result, and none "
    "writes A[a] or A[b] back (measured on the camera: inferred); what the low head bit selects "
    "is open (it is no wait on the stream engine: a load head beside a load in flight read the old "
    "content either way, measured on the camera)"
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
    "S head, it wrote nothing to lanes 0 / 1 of L3, nor to L3 under 1 beside any S3 head "
    "(verified on the camera). The enable of U's port U[f141_144] beside an S3 head: 1 writes, "
    "0 and 2 write nothing, and with no S3 head in the word 1 writes nothing (verified on the "
    "camera, pre-registered against a known value in U); U is `LANE_THIRD_FILE`"
)
LANE_OPERANDS = (
    "a and b are the unit's two lane operands (`LANE_FILES`) lane by lane, read as signed or "
    "unsigned 16-bit values as the rule says; a[k+1] is the next lane; s, t the unit's scalar "
    "(S0 F0[f72_75], S1 F1[f84_87], S2 F2[f88_91]) and imm its immediate, n a shift count; "
    "bit k of s is bit k of S0's whole 32-bit scalar, for lane k (`LANE_MASKS`); c is the "
    "carry-in, bit k of the 32-bit register F0[f76_79] (S0) or F1[f80_83] (S1) for lane k; "
    "sat_s / sat_u saturate to the signed / unsigned 16-bit range, max_u / min_u compare "
    "unsigned, the others signed; clamp(b, lo, hi) = min_s(max_s(b, lo), hi), ~x the bitwise "
    "complement; a result is its low 16 bits; (a : b) is the 32-bit value with a the high half"
)
NO_LANE = "no lane result"
"""A head that wrote no lane through its unit's port into L3 in four consecutive words."""
CARRY_IN = (
    "pre-registered on 32 lanes; c's register through 0x1f and 0x33 at four register numbers "
    "each, every mask distinct (`LANE_MASKS`)"
)
CLAMP_BOUNDS = (
    "pre-registered at non-negative bounds only, a negative bound not predicted"
)
CLAMP_A = (
    "for a < 0 the compare is the sign bit of a wrapped 16-bit sum: b >= 0 gives b where b - a "
    "has its sign bit set, else a; b < 0 gives 0 under 0xa and under 0xd ~a where b + a + 1 has "
    "its sign bit set, else b (pre-registered on 32 constructed lanes of both signs with "
    "overflow cases); for a >= 0 the clamp"
)
NO_SELECT = "pre-registered and met under one mask in s: no select seen"
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
FLOOR_CARRY = (
    "c the carry-in, bit k of F1[f80_83] for lane k; the low 16 bits (fitted on three masks, "
    "then pre-registered on other operands, counts and mask and met on all 32 lanes); with c = "
    "0 the plain floor shift; what separates the four heads of the group is open"
)
NO_LANE_LAGS = (
    "pre-registered: a sentinel kept with the unit's port in the head's word only and in the "
    "next word only, 32 lanes each"
)
LANE_MASKS = (
    "S0's per-lane condition is a 32-bit mask in a scalar register: its select heads 0x44, 0x45, "
    "0x46 and 0x4b read bit k of F0[f72_75] for lane k (verified on the camera, pre-registered, "
    "0x46 under two masks and the others under one, 32 lanes exact per group, a = L0[f37_40], b = L0[f41_44]); a lane port, "
    "or O under 4, takes their result in the head's own word, one and two words later nothing "
    "(`LANE_LAG`). S0's and S1's compare heads make such a mask, bit k from lane k, which M puts "
    "on X0's result bus one word after the head under f274_275 = 0 for S0 and 1 for S1 "
    "(`S_RESULTS`), where f255 = 0 takes it into F0 and f258 = 0 into F1 (verified on the camera, "
    "pre-registered). The whole chain ran as one program: S0 0x32, in the next word M under "
    "f274_275 = 0 with f255 = 0 into F0[k], then S0 0x46 with f72_75 = k gave max_u(a, b) "
    "(verified on the camera, pre-registered, two masks and 2 x 32 lanes). In the stock programs "
    "every S0 0x13 (b == imm16) is followed by a word with M under f274_275 = 0 and F0's port "
    "f182_185 under f255 = 0 (verified in the bytes, 18 of 18 words), so they keep its mask in "
    "F0 (inferred). Both units' order compare heads are unsigned; a signed order compare is the "
    "unsigned one on both operands XOR-ed with 0x8000 (S1 0x01 against a broadcast 0x8000): so "
    "S0 0x32 / 0x33 and S1 0x45 / 0x46 gave the masks of a < b and a >= b signed (verified on "
    "the camera, pre-registered, four masks on edge and random lanes). The signed max / min "
    "heads hand over a signed order compare: a < b under S0 0x22 / S1 0x2e and a >= b under "
    "0x23 / 0x2f; against a scalar or an immediate b > x under the max heads (S0 0x16, 0xe, S1 "
    "0x2c, 0x2a) and b <= x under the min heads (S0 0x17, 0xf, S1 0x2d, 0x2b) (verified on the "
    "camera, pre-registered with ties in the lanes; `S_RESULTS`), so a signed order compare is "
    "one head and one M word. An add's "
    "mask is its carry: S0 0x20 under f274_275 = 0 and S1 0x34 under 1 put the carry mask of "
    "a + b on M's bus one word after the head (verified on the camera, pre-registered), and "
    "two words after S0 0x20 no longer (verified on the camera); the carry-in heads (S0 0x1e, 0x1f, 0x2a..0x2d, S1 0x32, 0x33, 0x3d..0x40) "
    "add bit k of F0[f76_79] / F1[f80_83] in lane k (verified on the camera, pre-registered, "
    "0x1f and 0x33 with the register at four numbers, the others at one). With M into F0[0] / F1[0] one word after S0 0x20 / "
    "S1 0x34 and the carry-in head with that register in the next word, the high halves' add "
    "takes the low halves' carry: the stock programs' 32-bit add per lane (verified on the "
    "camera, pre-registered, 32 lanes in each unit); without the M word the carry-in head adds "
    "the register's old bits, nothing is held in the unit. S3's held reduction reaches F0 the same way under f274_275 = 3: S3 0x0's "
    "maximum, then S0 0x19 against it, gave the one-hot mask of the maximum's lane (verified on "
    "the camera, pre-registered). S1 0x56, "
    "0x5b, 0x60, 0x61, 0x62, 0x6f, 0x70 and 0x7e gave b in every lane with a mask in F1 "
    "(measured on the camera): no select of S1's is known"
)
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
        0x08: ("clamp(b, 0, imm)", V, CLAMP_BOUNDS),
        0x09: ("clamp(b, 0, s)", V, CLAMP_BOUNDS),
        0x0A: ("clamp(b, 0, a), a of either sign by the wrapped rule", V, CLAMP_A),
        0x0B: ("clamp(b, ~imm, imm)", V, CLAMP_BOUNDS),
        0x0C: ("clamp(b, ~s, s)", V, CLAMP_BOUNDS),
        0x0D: ("clamp(b, ~a, a), a of either sign by the wrapped rule", V, CLAMP_A),
        0x0E: ("max_s(b, imm)", V, ""),
        0x0F: ("min_s(b, imm)", V, ""),
        0x10: ("sat_s(b - imm)", V, ""),
        0x11: ("sat_s(b + imm)", V, ""),
        **_rows((0x12, 0x13, 0x18, 0x19, 0x24, 0x25), NO_LANE, V, NO_LANE_LAGS),
        0x14: (f"(b - s) / 2, {TIES}", V, ""),
        0x15: (f"(b + s) / 2, {TIES}", V, ""),
        0x16: ("max_s(b, s)", V, ""),
        0x17: ("min_s(b, s)", V, ""),
        0x1A: ("sat_s(b - s)", V, ""),
        0x1B: ("sat_s(b + s)", V, ""),
        0x1C: (f"(b - a) / 2, {TIES}", V, ""),
        0x1D: (f"(a + b) / 2, {TIES}", V, ""),
        **_rows((0x1E, 0x2A), "sat_s(a + b + c)", V, CARRY_IN),
        **_rows((0x1F, 0x2B), "a + b + c, wrapping", V, CARRY_IN),
        **_rows((0x2C, 0x2D), "sat_u(a + b + c)", V, CARRY_IN),
        0x20: ("a + b, wrapping", V, ""),
        0x22: ("max_s(a, b)", V, ""),
        0x23: ("min_s(a, b)", V, ""),
        0x26: ("sat_s(|a|)", V, ""),
        0x27: ("-a, wrapping", V, ""),
        **_rows(range(0x2E, 0x34), NO_LANE, V, NO_LANE_LAGS),
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
        **_rows((0x41, 0x4D), "b", V, NO_SELECT),
        0x42: ("imm", V, NO_SELECT),
        0x43: ("F0[f76_79]", V, NO_SELECT),
        0x44: ("b where bit k of s is set, else imm", V, ""),
        0x45: ("b where bit k of s is set, else F0[f76_79]", V, ""),
        0x46: ("b where bit k of s is set, else a", V, ""),
        0x4B: ("bit k of s: 1 or 0", V, ""),
        **_rows(
            range(0x60, 0x7F),
            "a of the unit's last head in the job, the value it read then (latched), 0 with "
            "none",
            V,
            "pre-registered for every head at lag 1 and 0, the latch against a rewrite of the "
            "register and at a job's start; the head's own nibbles are ignored",
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
        **_rows(range(0x16, 0x1A), "((a : b) >> n) + c, n = imm & 15", V, FLOOR_CARRY),
        **_rows((0x1A, 0x1C), "sat_u(b << t)", V),
        **_rows((0x1B, 0x23), "sat_s(b << t)", V),
        **_rows((0x1D, 0x1E), f"(a : b) / 2^t, {TIES}", V),
        **_rows(range(0x1F, 0x23), "((a : b) >> n) + c, n = t & 15", V, FLOOR_CARRY),
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
        **_rows((0x32, 0x3D), "sat_s(a + b + c)", V, CARRY_IN),
        **_rows((0x33, 0x3E), "a + b + c, wrapping", V, CARRY_IN),
        **_rows((0x3F, 0x40), "sat_u(a + b + c)", V, CARRY_IN),
        0x34: ("a + b, wrapping", V, ""),
        0x35: (f"(b + t) / 2, {TIES}", V, ""),
        0x36: ("sat_s(b - t)", V, ""),
        0x37: ("sat_s(b + t)", V, ""),
        0x38: (f"(a + b) / 2, {TIES}", V, ""),
        0x39: ("sat_s(|a|)", V, ""),
        0x3A: ("-a, wrapping", V, ""),
        0x3B: ("sat_s(b - a)", V, ""),
        0x3C: ("sat_s(a + b)", V, ""),
        **_rows((*range(0x41, 0x47), 0x54), NO_LANE, V, NO_LANE_LAGS),
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
        0x53: ("b", V, ""),
        **_rows(
            range(0x55, 0x60),
            "b or nothing, on a state not identified",
            H,
            "b with the port in the head's word and the next in an earlier run (0x55, 0x5a, "
            "0x5f), nothing with the port in a single word at either lag nor, in a later run, "
            "in both; 0x55 once b in its own word right after an 0x53; 0x57..0x5f 0 at lag 0 "
            "in the form of `ALONE_ZEROS`",
        ),
        **_rows(
            range(0x60, 0x7F),
            "b of the unit's last head in the job that read b, the value it read then "
            "(latched), 0 with none",
            V,
            "pre-registered for every head at lag 0 and 1, the latch against a rewrite of the "
            "register, at a job's start and after a head that reads a only (which keeps it); "
            "the head's own nibbles are ignored",
        ),
    },
    "s2": {
        **_rows(
            (0x00, 0x01),
            "b x imm, low 16 bits",
            V,
            "imm by field `LANE_IMMEDIATES`; 0x0's high half that of the unsigned b",
        ),
        0x02: (
            "a x b, low 16 bits",
            V,
            "its high half that of the unsigned a x the signed b",
        ),
        **_rows((0x04, 0x06), "a x b, low 16 bits", V),
        0x03: ("b x t, low 16 bits", V, ""),
        0x05: ("b x t, low 16 bits", V, "its high half that of the signed b"),
        0x07: (
            "b where bit 1 of the nibble f88_91 is set, else a",
            V,
            (
                "pre-registered at all sixteen values of f88_91 (seven on all 32 lanes, nine on "
                "four lanes), the port in the head's word"
            ),
        ),
        **_rows((0x08, 0x0D), "b", V),
        0x09: ("0xffff where b < 0, else 0", V, ""),
        0x0A: ("the lane number 0..31", V, ""),
        0x0B: ("0xffff where b >= 0, else 0", V, ""),
        **_rows((0x0C, 0x0E), NO_LANE, V, NO_LANE_LAGS + " and in both"),
    },
    "s3": {
        **_rows(
            (*range(0x06), 0x0E, 0x0F, 0x16, 0x17, 0x1A, 0x1D, 0x1E, 0x1F, 0x24),
            NO_LANE,
            V,
            NO_LANE_LAGS,
        ),
        **_rows(range(0x25, 0x3F), NO_LANE, V, NO_LANE_LAGS),
        0x06: (
            "0 in every lane",
            V,
            "pre-registered at lag 1 on fresh and extreme a (all 0xffff, all 0x8000, -k)",
        ),
        0x07: (
            "lanes 8j..8j + 7 := b's 8-lane block a[j] where a[j] < 4, else 0, for j = 0..3",
            V,
            "a block select, pre-registered on fresh a against a[j] & 3",
        ),
        0x08: (
            "lanes 16j..16j + 15 := b's 16-lane half a[j] where a[j] < 2, else 0, for j = 0, 1",
            V,
            "a block select, pre-registered on fresh a against a[j] & 1",
        ),
        0x09: (
            "b[(k & 16) + a[k]] where a[k] < 16, else 0",
            V,
            "a table lookup in each 16-lane half",
        ),
        0x0A: ("lanes 2k, 2k + 1 := b[k], a[k] for k = 0..15", V, ""),
        0x0B: ("lanes 2k, 2k + 1 := b[16 + k], a[16 + k] for k = 0..15", V, ""),
        0x0C: ("b's odd lanes, then a's odd lanes", V, ""),
        0x0D: ("b's even lanes, then a's even lanes", V, ""),
        0x10: ("b with lane f92_95 + 16 x (f31_34 bit 2) := F3[f96_99]", V, ""),
        0x11: (
            "an immediate in every lane",
            V,
            "the immediate imm14 by field (`LANE_IMMEDIATES`)",
        ),
        0x12: ("0xffff where b < 0, else 0", V, ""),
        0x13: ("0xffff where b >= 0, else 0", V, ""),
        0x14: ("the lane number 0..31", V, ""),
        0x15: (
            "b with lane F3[f92_95] & 31 := F3[f96_99]",
            V,
            "an index of 32 and up wraps",
        ),
        0x18: ("F3[f96_99] in every lane: a broadcast", V, ""),
        **_rows((0x19, 0x20, 0x21, 0x23), "b", V),
        0x1B: (
            (
                "b moved up by n = f92_95 = 1..4: lane k := b[k + n], the top n lanes U's "
                "lanes 0..n-1; down by m = 16 - f92_95 = 1..4: lane k := b[k - m], the bottom "
                "m lanes U's lanes 4-m..3"
            ),
            V,
            (
                "down by 1..4 (f92_95 = 15..12) pre-registered on all 32 lanes; up by 1..4 "
                "pre-registered on lanes 0..4, 15 and 24..31 with U's lanes moved in, up by 2 "
                "also on 30 lanes; U's register named by f31_34 (verified: all sixteen read "
                "back by number); f92_95 = 5..11 b, no move (pre-registered at each count)"
            ),
        ),
        **_rows(
            (0x1C, 0x22),
            "as 0x1b at f92_95 = 1..5, 8 and 11..15; 6, 7, 9 and 10 not predicted",
            V,
            "pre-registered at 3 and 13 on all 32 lanes, at 4 on lanes 0..27, (0x1c) at 2 on "
            "lanes 0..29, and at 1, 5, 8, 11, 12, 14 and 15 on six edge lanes with U's lanes "
            "moved in; an earlier run fitted all three heads alike at every count (inferred), "
            "6, 7, 9 and 10 not pre-registered for these two heads; they differ from 0x1b in "
            "what U's port takes "
            "beside them "
            "(`LANE_THIRD_FILE`); 0x22 wrote nothing while F3 held power-up content",
        ),
    },
}
"""Each S unit's lane functions by head value: (result, tier, note), the result written to a lane
file by a lane port (`LANE_SOURCES`). Measured on the camera: every head beside the unit's port
into L3 in four consecutive words on one random operand pair (fitted, inferred), then the rows
pre-registered lane by lane on two constructed 32-lane vectors of edge and random values
(verified where all lanes matched), and the refits of the misses pre-registered again on other
scalars (verified where they matched; inferred where a refit was not run again); every row with a
verified lag (`LANE_LAG`) was pre-registered again on new operands, at that lag, and met. Heads not
listed are open; `LANE_OPERANDS` names the operands."""
S2_HIGH_HALVES: dict[int, LaneRow] = {
    0x02: ("bits 16..31 of the unsigned a x the signed b", V, ""),
    0x05: ("bits 16..31 of the signed b x t", V, ""),
    0x00: (
        "bits 16..31 of the unsigned b x imm's 16 bits (sext7, then unsigned)",
        V,
        "",
    ),
    0x01: ("bits 16..31 of the signed b x the signed imm", V, ""),
    0x03: ("bits 16..31 of the unsigned b x t", V, ""),
    0x04: ("bits 16..31 of the unsigned a x the unsigned b", V, ""),
    0x06: ("bits 16..31 of the signed a x the signed b", V, ""),
}
"""Where S2's multiplies are known whole: the high half of the 32-bit product, which the first
ports of L0 and L1 take under enable 1 one word after the head (`LANE_SOURCES`). With the low
half taken by f159_162 / 1 in the same word, S1's double-width shifts of a : b narrow the
product (verified on the camera for 0x5 on all 32 lanes, pre-registered: a fixed-point multiply
by a scalar, rounded and saturated, in three words). 0x2's signedness was pre-registered on 16-bit
edge operands and met on all 32 lanes, and through 0x2's low and high halves and S3 0x5 a signed
dot product came out exact (verified on the camera). The pairs differ in the operands'
signedness: 0x0 / 0x1 and 0x3 / 0x5 unsigned / signed b, 0x4 / 0x2 / 0x6 unsigned a x unsigned
b, unsigned a x signed b, signed a x signed b; under a negative immediate 0x0 multiplies by its
16 bits taken unsigned, 0x1 by the signed value (fitted on all 32 lanes of 16-bit edge
operands, then pre-registered on swapped operands and immediates -1 / -30 and met, both halves
of all seven multiplies: verified on the camera). 0x3 multiplies by t's low 16 bits unsigned,
0x5 by them signed, t's bits 16..31 not read (verified on the camera, pre-registered on all 32
lanes with t's bit 15 set and its bits 16..31 both set and clear)."""
LANE_IMMEDIATES: dict[str, tuple[str, Confidence]] = {
    "s0": ("imm16 = f76_79 << 12 | f37_40 << 8 | f23_30", V),
    "s1": ("imm7 = sext7(f80_83 bits 3..2 << 5 | f84_87 << 1 | f0)", V),
    "s2": ("imm7 = sext7(f53_56 << 3 | f88_91 >> 1), the multiplier of 0x0 / 0x1", V),
    "s3": (
        "imm14 = sext14(f96_99 << 10 | f65_68 << 6 | f31_34 bits 3..2 << 4 | f92_95), 0x11's",
        V,
    ),
}
"""Each S unit's immediate by field. Fitted on the camera with one field varied per cell, read
through max_s(0x8000, imm), 1 x imm or S3 0x11, then pre-registered on fresh values of every
field and met (verified on the camera, 13 cells x 2 lanes); S0's bit 5, 0 in those cells, was
pre-registered set later through the clamp S0 0x8 and met; f80_83's bits 1..0 do not enter
S1's. Register nibbles double as immediate bits: S0's f37_40 (a) and f76_79, S1's f84_87 (t),
S2's f53_56 (a) and f88_91 (t)."""

S_NONE = {
    "s0": "the others of 0..0x3f and 0x60..0x7f none in the head's own word, and of 0..0x3f none "
    "one word after it under f274_275 = 0 (the unsigned max / min among them)",
    "s1": "the others none in the head's own word; one word after the head under f274_275 = 1 "
    "0x13, 0x1a and 0x1b a per-lane word not fitted, 0x14, 0x15, 0x1d and 0x1e one bit, 0x16, "
    "0x17, 0x1f and 0x20 the word 0, the others of 0..0x5f none (the unsigned max / min among "
    "them)",
}
"""Where not every other head value was read."""
REDUCTION = (
    "of the 32 lanes of L3[f65_68], on M's bus in the third word after the head and in no other (verified, "
    "pre-registered)"
)


@dataclass(frozen=True)
class ScalarRow:
    """An S head's scalar result on M: its rule, its tier, the word after the head it is on M
    from (`lag`) and whether it stays there (`held`) or is there for that word only."""

    text: str
    tier: Confidence
    lag: int
    held: bool = False


def _own(text: str, tier: Confidence = V) -> ScalarRow:
    return ScalarRow(text, tier, 0)


def _next(text: str, tier: Confidence = V) -> ScalarRow:
    return ScalarRow(text, tier, 1)


def _held(text: str, tier: Confidence = V) -> ScalarRow:
    return ScalarRow(text, tier, 3, held=True)


def _mask(held: int, tried: int) -> str:
    """A compare head's scalar result, which M puts out under f274_275 = `held`; `tried` the
    head read under the other values."""
    return (
        f"a 32-bit mask, bit k from lane k, on M's bus one word after the head under f274_275 = "
        f"{held} (verified on the camera, pre-registered); under its other values none (verified "
        f"for {tried:#x}, inferred for the other heads); in the head's own word none (inferred) "
        "and two words after it none (verified for S0 0x20; `LANE_MASKS`)"
    )


S0_MASK, S1_MASK = _mask(0, 0x32), _mask(1, 0x45)
S_RESULTS: dict[str, dict[int, ScalarRow]] = {
    "s0": {
        0x52: _own(f"F0[f76_79] ({EXACT})"),
        0x24: _next(f"a != b, {S0_MASK}"),
        0x25: _next(f"a == b, {S0_MASK}"),
        0x32: _next(f"a < b unsigned, {S0_MASK}"),
        0x33: _next(f"a >= b unsigned, {S0_MASK}"),
        0x2E: _next(f"b > imm unsigned, {S0_MASK}"),
        0x2F: _next(f"b <= imm unsigned, {S0_MASK}"),
        0x30: _next(f"b > s unsigned, {S0_MASK}"),
        0x31: _next(f"b <= s unsigned, {S0_MASK}"),
        0x12: _next(f"b != imm, {S0_MASK}"),
        0x13: _next(f"b == imm, {S0_MASK}"),
        0x18: _next(f"b != s, {S0_MASK}"),
        0x19: _next(f"b == s, {S0_MASK}"),
        0x20: _next(f"the carry of a + b, {S0_MASK}"),
        0x1F: _next(f"the carry of a + b + c, {S0_MASK}"),
        0x2C: _next(f"the carry of a + b + c, {S0_MASK}"),
        0x2A: _next(f"the signed overflow of a + b + c, {S0_MASK}"),
        0x22: _next(f"a < b signed, {S0_MASK}"),
        0x23: _next(f"a >= b signed, {S0_MASK}"),
        0x16: _next(f"b > s signed, {S0_MASK}"),
        0x17: _next(f"b <= s signed, {S0_MASK}"),
        0x0E: _next(f"b > imm signed, {S0_MASK}"),
        0x0F: _next(f"b <= imm signed, {S0_MASK}"),
    },
    "s1": {
        0x54: _own(f"F1[f84_87] ({EXACT})"),
        0x34: _next(f"the carry of a + b, {S1_MASK}"),
        0x33: _next(f"the carry of a + b + c, {S1_MASK}"),
        0x3F: _next(f"the carry of a + b + c, {S1_MASK}"),
        0x3D: _next(f"the signed overflow of a + b + c, {S1_MASK}"),
        0x2E: _next(f"a < b signed, {S1_MASK}"),
        0x2F: _next(f"a >= b signed, {S1_MASK}"),
        0x2C: _next(f"b > t signed, {S1_MASK}"),
        0x2D: _next(f"b <= t signed, {S1_MASK}"),
        0x2A: _next(f"b > imm signed, {S1_MASK}"),
        0x2B: _next(f"b <= imm signed, {S1_MASK}"),
        0x12: _next(
            "a 32-bit word that fits a mask of the lanes the shift saturated, on M's bus one "
            "word after the head under f274_275 = 1 (measured on the camera, fitted: inferred)",
            I,
        ),
        0x41: _next(f"b > imm unsigned, {S1_MASK}"),
        0x42: _next(f"b <= imm unsigned, {S1_MASK}"),
        0x43: _next(f"b > t unsigned, {S1_MASK}"),
        0x44: _next(f"b <= t unsigned, {S1_MASK}"),
        0x45: _next(f"a < b unsigned, {S1_MASK}"),
        0x46: _next(f"a >= b unsigned, {S1_MASK}"),
    },
    "s2": {
        0xC: _own(
            "lane (f53_56 & 3) x 8 + (f88_91 >> 1) of L2[f57_60], sign-extended (verified on the "
            "camera, pre-registered, 38 cells; the other bits of the two nibbles without effect)"
        ),
        0xE: _own(
            f"F2[f88_91] ({EXACT}); not held: one word after the head none under any f274_275"
        ),
    },
    "s3": {
        0x0: _held(f"the unsigned maximum {REDUCTION}"),
        0x1: _held(f"the unsigned minimum {REDUCTION}"),
        0x2: _held(f"the sum, the lanes zero-extended, {REDUCTION}"),
        0x3: _held(f"the signed maximum, sign-extended, {REDUCTION}"),
        0x4: _held(f"the signed minimum, sign-extended, {REDUCTION}"),
        0x5: _held(f"the sum, the lanes sign-extended, {REDUCTION}"),
        0xE: _own(
            "lane f92_95 + 16 x (f31_34 bit 2) of L3[f65_68], zero-extended (verified on the "
            "camera, pre-registered)"
        ),
        0xF: _own(
            "lane f92_95 + 16 x (f31_34 bit 2) of L3[f65_68], sign-extended (verified: built "
            "vectors read back on all 32 lanes)"
        ),
        0x16: _own(
            "lane F3[f92_95] & 31 of L3[f65_68], zero-extended (verified on the camera, "
            "pre-registered, indices of 32 and up included)"
        ),
        0x17: _own(
            "lane F3[f92_95] & 31 of L3[f65_68], sign-extended (verified on the camera, "
            "pre-registered, indices of 32 and up included)"
        ),
        0x24: _own(
            "F3[f96_99] (verified on the camera, pre-registered on three sources)"
        ),
    },
}
"""The S head values with a scalar result under the M strobe (which values have one: measured on
the camera on every head value but S0's 0x40..0x5f, of which only 0x52 was read, in the head's
own word, and one word after it on S0's 0..0x3f and S1's 0..0x5f; the rules' tiers in the text). A
reduction's register and an extract's lane in the same word reach the bus OR-ed (measured on the
camera). M takes a unit's result of the previous word while that unit has a new head in the M
word (verified on the camera: S0 0x20 in consecutive words of a packed loop). None of S0's select
heads (`LANE_MASKS`) hangs the engine (verified on the camera)."""


S_OWN_OPEN = {"s0": frozenset(range(0x40, 0x60)) - {0x52}}
"""S heads whose own-word result under the M strobe was not read (measured: every other head)."""
M_OTHER_OPEN = {
    "s0": frozenset({0x40, *range(0x47, 0x60)}) - {0x4B, 0x4D},
    "s1": frozenset(range(0x55, 0x60)),
}
"""S heads beside which M under another unit's f274_275, in the head's word and the next, was
not read; beside every other head it was, and nothing was on M there (verified on the camera,
pre-registered)."""
S_NEXT_VALUES = {
    "s1": frozenset({0x13, 0x1A, 0x1B, 0x14, 0x15, 0x1D, 0x1E, 0x16, 0x17, 0x1F, 0x20})
}
"""S heads that put a value on M one word after the head that is not fitted (measured on the
camera under their unit's f274_275)."""
S_NEXT_NONE = {
    "s0": frozenset(range(0x40)),
    "s1": frozenset(range(0x60)) - S_NEXT_VALUES["s1"],
    "s2": frozenset({0x0E}),
    "s3": frozenset(range(0x06)),
}
"""S heads without a held row in `S_RESULTS` that put nothing on M one word after the head
under their unit's f274_275 (measured on the camera; S3's reductions: their register from the
third word on). One word after any other head, M under its unit is not known: S1 0x13, 0x1a,
0x1b, 0x14, 0x15, 0x1d, 0x1e, 0x16, 0x17, 0x1f and 0x20 put a value there that is not fitted,
and S0 0x40.., S1 0x60.., S2 and S3 were not read there. Under another unit's f274_275 a held
result is not on M (verified for S0 0x32 and S1 0x45, inferred for the others)."""


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
        V,
        _written("F1", "f258", "the file f194_197's port writes too"),
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
    "f141_144": (
        V,
        (
            "The register of U its port writes under f232_233 = 1 (verified on the camera, "
            "pre-registered: U[3] and U[9] written in one job and read back by number); U is "
            "`LANE_THIRD_FILE`"
        ),
    ),
    "f23_30": (
        V,
        (
            "Bits 7..0 of S0's 16-bit lane immediate (`LANE_IMMEDIATES`; verified on the camera, "
            "pre-registered on fresh values, bit 5 set in a later one)"
        ),
    ),
    "f31_34": (
        V,
        (
            "S3's: bit 2 is bit 4 of the lane 0x10 inserts (verified on the camera: inserts at "
            "lanes 0..31 read back as built) and 0xe / 0xf extract; bits 3..2 are bits 5..4 of "
            "S3 0x11's immediate (verified on the camera, pre-registered on fresh values); "
            "0x16 and 0x17 ignore it (verified on the camera, pre-registered at two values "
            "each); as a whole the register of U that the lane "
            "shifts read (verified on the camera, pre-registered: U[3] and U[9] read back by "
            f"number through 0x1b): {LANE_THIRD_FILE}"
        ),
    ),
    "f37_40": (
        V,
        (
            "a, the register of the lane file L0 that S0's lane heads read first (`LANE_FILES`; "
            "pre-registered at registers 1, 5, 6, 9 and 12); also bits 11..8 of S0's immediate "
            "(`LANE_IMMEDIATES`, verified on the camera, pre-registered); S0 0x60 did not return "
            "its register at other numbers than 1"
        ),
    ),
    "f41_44": (
        V,
        (
            "b, the register of L0 that S0's lane heads read second (`LANE_FILES`; "
            "pre-registered at registers 2, 5, 6, 9 and 12)"
        ),
    ),
    "f45_48": (
        V,
        (
            "a, the register of the lane file L1 that S1's lane heads read first (`LANE_FILES`; "
            "pre-registered at registers 1, 5, 6 and 8)"
        ),
    ),
    "f49_52": (
        V,
        (
            "b, the register of L1 that S1's lane heads read second (`LANE_FILES`; "
            "pre-registered at registers 2, 9 and 12)"
        ),
    ),
    "f53_56": (
        V,
        (
            "a, the register of the lane file L2 that S2's lane heads read first (`LANE_FILES`; "
            "pre-registered at registers 1, 5, 6, 9 and 12); bits 6..3 of S2's multiply "
            "immediate (`LANE_IMMEDIATES`, verified on the camera, pre-registered); under S2 head "
            "0xc its low two bits are bits 4..3 of the lane read and its high two bits without "
            "effect (verified on the camera, pre-registered)"
        ),
    ),
    "f57_60": (
        V,
        (
            "b, the register of L2 that S2's lane heads read second (`LANE_FILES`; pre-registered "
            "at registers 2, 4, 6 and 13), and the register S2 head 0xc reads a lane of "
            "(verified on the camera, pre-registered at two registers)"
        ),
    ),
    "f61_64": (
        V,
        (
            "a, the register of the lane file L3 that S3's lane heads read first (`LANE_FILES`; "
            "pre-registered at registers 1, 5, 6, 9 and 12)"
        ),
    ),
    "f65_68": (
        V,
        (
            "b, the register of L3 that S3's lane heads read second (`LANE_FILES`): the register S3 "
            "0xe, 0xf, 0x16 and 0x17 extract a lane of and 0x0..0x5 reduce (verified on the "
            "camera: built vectors and pre-registered writers read back at several registers, and "
            "S3 0xa at registers 5, 6, 9 and 12); bits 9..6 of S3 0x11's immediate (verified on "
            "the camera, pre-registered)"
        ),
    ),
    "f72_75": (
        V,
        (
            "s, the register of F0 that S0's lane heads read as their scalar (`LANE_OPERANDS`; "
            "the bound of the clamps 0x9 and 0xc, verified on the camera, pre-registered against "
            "F0[f76_79]) and whose 32 bits the select heads read as a lane mask (verified on the camera, "
            f"pre-registered at two registers holding two masks): {LANE_MASKS}"
        ),
    ),
    "f76_79": (
        V,
        (
            f"Under S0 head 0x52 the register of F0 it returns ({EXACT}); the register of F0 "
            "whose 32 bits are the carry-in mask of S0's carry-in heads, bit k for lane k "
            "(`LANE_MASKS`; verified on the camera, pre-registered at registers 0, 5, 6 and 15); "
            "bits 15..12 of S0's lane immediate (`LANE_IMMEDIATES`; verified on the camera, "
            "pre-registered)"
        ),
    ),
    "f80_83": (
        V,
        (
            "The register of F1 whose 32 bits are the carry-in mask of S1's carry-in heads, bit "
            "k for lane k (`LANE_MASKS`; verified on the camera, pre-registered at registers 0, "
            "5, 6 and 15); its bits 3..2 are also bits 6..5 of S1's lane immediate and its bits "
            "1..0 do not enter it (`LANE_IMMEDIATES`; verified on the camera, pre-registered on "
            "fresh values); S1's floor shifts 0x16..0x19 and 0x1f..0x22 add the same bit k "
            "(verified on the camera, pre-registered)"
        ),
    ),
    "f84_87": (
        V,
        (
            f"Under S1 head 0x54 the register of F1 it returns ({EXACT}); t, the scalar S1's lane "
            "heads read (verified on the camera by S1's pre-registered function table); bits 4..1 "
            "of S1's lane immediate (`LANE_IMMEDIATES`; verified on the camera, pre-registered)"
        ),
    ),
    "f88_91": (
        V,
        (
            f"Under S2 head 0xe the register of F2 it returns ({EXACT}); t, the scalar S2's lane "
            "heads read (verified on the camera by S2's pre-registered function table). Its bits "
            "3..1 are bits 2..0 of the immediate S2 0x0 / 0x1 multiply by (`LANE_IMMEDIATES`; "
            "verified on the camera, pre-registered) and under S2 0xc bits 2..0 of the lane read, "
            "its bit 0 without effect (verified on the camera, pre-registered). Under S2 0x7 its "
            "bit 1 chooses b over a (`LANE_FUNCTIONS`; verified on the camera, pre-registered "
            "at all sixteen values)"
        ),
    ),
    "f92_95": (
        V,
        (
            "S3's lane select: the lane itself under 0xe, 0xf and 0x10 (with f31_34 bit 2 as bit "
            "4), the register of F3 whose value & 31 is the lane under 0x15, 0x16 and 0x17 "
            "(verified on the camera: inserts at lanes 0..31 read back as built, the writers "
            "pre-registered by nibble, the & 31 pre-registered); bits 3..0 of 0x11's immediate "
            "(verified on the camera, pre-registered); the signed shift count of 0x1b, 0x1c and "
            "0x22, used at -4..+4 (`LANE_FUNCTIONS`: for 0x1b down by 1..4 verified on all 32 "
            "lanes and up by 1..4 on lanes 0..4, 15 and 24..31, pre-registered; 0x1c and 0x22 "
            "at eleven counts) and 5..11 no shift (verified on the camera, pre-registered at "
            "each count under 0x1b)"
        ),
    ),
    "f96_99": (
        V,
        (
            "The register of F3 whose value S3 0x18 broadcasts and 0x10 and 0x15 insert (verified "
            "on the camera, pre-registered by nibble), the one 0x24 returns and bits 13..10 of "
            "0x11's immediate (verified on the camera, pre-registered)"
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
LANE_FILE_OF = {n: file for file, (_, *nibs) in LANE_FILES.items() for n in nibs}
"""The lane file each lane operand nibble names a register of."""


def lane_file(enable: str) -> str:
    """The lane file a lane port writes: the file of the nibble 110 bits below its own."""
    lo = int(PORTS[enable][1:4]) - 110
    return LANE_FILE_OF[_fid(lo, lo + 3)]


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
    file = lane_file(enable)
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
    "s1": "logic, shifts and double-width shifts, min / max, add and compares with a scalar or an "
    "immediate",
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
            parts.append(f"scalar: {scalars[h].text}")
        values[h] = "; ".join(parts)
    return values


def _head_tier(unit: str, idle: int) -> Confidence:
    """Verified when every value but the idle one has a verified lane row and, where it writes
    a lane, a verified lag; inferred otherwise."""
    lo, hi = HEADS[unit]
    rows, lags = LANE_FUNCTIONS[unit], LANE_LAG[unit]
    for h in range(1 << (hi - lo + 1)):
        if h == idle:
            continue
        row = rows.get(h)
        if row is None or row[1] != V:
            return I
        if row[0] != NO_LANE and (h not in lags or lags[h][1] != V):
            return I
    return V


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
    tier = _head_tier(unit, idle)
    lanes = (
        "Its values select lane functions, each tagged by value (`LANE_FUNCTIONS`); the unit: "
        f"{LANE_ROLES[unit]} ({'verified: every value has a verified row and lag' if tier == V else 'inferred'})"
    )
    return Field(
        lo,
        hi,
        Kind.OPCODE,
        unit,
        idle,
        tier,
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
        "immediate (`LANE_IMMEDIATES`; verified on the camera, pre-registered on fresh values)",
        kind=Kind.IMM,
        confidence=V,
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
        {
            0: "writes nothing (verified, beside S3 0x1f)",
            1: "U[f141_144] takes what the S3 head beside it hands (verified, lanes 0..3 read)",
            2: "writes nothing (verified, beside S3 0x1f)",
            3: "idle",
        },
        V,
    ),
    Field(
        234,
        235,
        Kind.OPCODE,
        "pb",
        3,
        H,
        f"{RARE}; the candidate enable of f145_146: active in 18 words, all with f145_146 set, "
        f"and in 10 of its 17 changes (hypothesis). {Q_NULL}",
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
    _enable(256, 257, "pb", 3, _pb("f256_257"), _pb_values("f256_257", 3), V),
    _enable(258, 258, "pb", 1, _pb("f258"), _pb_values("f258", 1), V),
    _enable(
        259, 260, "pb", 3, f"{X0_PATH}; {_pb('f259_260')}", _pb_values("f259_260", 3), V
    ),
    _enable(
        261, 262, "pb", 3, f"{X0_PATH}; {_pb('f261_262')}", _pb_values("f261_262", 3), V
    ),
    _enable(263, 264, "pb", 3, _pb("f263_264"), _pb_values("f263_264", 3), V),
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
        Kind.ROUTE,
        "m",
        0,
        V,
        "the value under the active-low strobe f276: f274_276 takes 4 (idle) and 0..3, 7 in 38 "
        f"words, never 5 or 6, {OWNERSHIP}; held, in runs of median 8 words, and no field "
        f"changes under it; {_c2_fact(274, 275)}. "
        "In the stock words it is 0 beside S0 head 0x52, 1 beside S1 0x54, 2 beside S2 0xe and "
        "3 beside S3 0x17 (verified in the bytes). It names the S unit whose result M puts "
        "out: with S0 0x52, S1 0x54, S2 0xe and S3 0x24 in one word, four distinct 32-bit "
        "values, 0..3 put out S0's, S1's, S2's and S3's value; one word after S0 0x32 only 0 "
        "put its mask out, one word after an S1 compare head only 1, three words "
        "after S3 0x2 only 3 its sum, the other values leaving the sentinel (verified on the "
        "camera, pre-registered). A single head's result in its own word passed under each of "
        "0..3 (measured on the camera, one head per word). S2 holds no result: one word after "
        "S2 0xe every value left the sentinel (`LANE_MASKS`)",
        values={
            0: "S0's result, and its held mask one word after the head (verified)",
            1: "S1's result, and its held mask one word after the head (verified)",
            2: "S2's result in the head's own word (verified)",
            3: "S3's result, and a reduction in the third word after the head (verified)",
        },
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
        "bus in the third word after the head only (verified on the camera, "
        "pre-registered), OR-ed with an extract's lane in the same word (measured); and S0's and "
        "S1's compare and carry masks, on the bus one word after the head under f274_275 = 0 "
        "and 1 (verified on the camera, pre-registered), two words after S0 0x20 no longer "
        "(verified on the camera; `LANE_MASKS`)",
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
        "which value stalls is not separated (measured on the camera); 0x41..0x46, 0x4b and "
        "0x4d run in own programs without hanging the engine (measured on the camera). "
        f"{LANE_MASKS}",
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
    "(inferred, one run, not modelled); the value names S2's b, a register of L2 "
    "(`LANE_FILES`)",
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
    "value's bits and no input of the job gives the masks (inferred). The mask is F0[value], "
    "which S0's carry-in heads read as their carry-in, bit k for lane k (verified, `LANE_MASKS`); "
    "that this explains these edits is inferred. The value is also bits 15..12 of S0's lane "
    "immediate (`LANE_IMMEDIATES`, verified)",
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
"""Camera facts of single fields on the units (`units.py`): ownership predicts use, in one
direction. At program 13 words 433 and 439, idling the nibbles whose owner is active in the word
gives the effect of idling the whole grid, byte for byte, and idling the held nibbles of idle
owners has none (32 nibbles in three sets, at their idle values). An active owner is not
sufficient: port f202_205 and X0's f69_71 are inert there. f57_60 = 5 at words 441 and 442
reads plane 1's line a + 1 (verified on the camera)."""
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

NAMES = {
    "f0": "s1imm_lo",
    "f1": "x0k_lo",
    "f2_6": "x0k_mid",
    "f7_8": "x1k_lo",
    "f9_14": "x1k_mid",
    "f23_30": "s0imm_lo",
    "f31_34": "s3u",
    "f37_40": "s0a",
    "f41_44": "s0b",
    "f45_48": "s1a",
    "f49_52": "s1b",
    "f53_56": "s2a",
    "f57_60": "s2b",
    "f61_64": "s3a",
    "f65_68": "s3b",
    "f69_71": "x0o",
    "f72_75": "s0s",
    "f76_79": "s0r",
    "f80_83": "s1r",
    "f84_87": "s1t",
    "f88_91": "s2t",
    "f92_95": "s3idx",
    "f96_99": "s3val",
    "f120_122": "rr",
    "f128_132": "rb",
    "f141_144": "udst",
    "f147_150": "l0dst",
    "f151_154": "l0dst2",
    "f155_158": "l1dst",
    "f159_162": "l1dst2",
    "f163_166": "l2dst",
    "f167_170": "l2dst2",
    "f171_174": "l3dst",
    "f175_178": "l3dst2",
    "f179_181": "odst",
    "f182_185": "sf0dst",
    "f186_189": "sf0dst2",
    "f190_193": "sf1dst",
    "f194_197": "sf1dst2",
    "f198_201": "sf2dst",
    "f202_205": "sf3dst",
    "f232_233": "usrc",
    "f236_237": "l0src",
    "f238_239": "l0src2",
    "f240_241": "l1src",
    "f242_243": "l1src2",
    "f244_245": "l2src",
    "f246_247": "l2src2",
    "f248_249": "l3src",
    "f250_251": "l3src2",
    "f252_254": "osrc",
    "f255": "sf0src",
    "f256_257": "sf0src2",
    "f258": "sf1src",
    "f259_260": "sf1src2",
    "f261_262": "sf2src",
    "f263_264": "sf3src",
    "f274_275": "msel",
    "f276": "m_n",
    "f278_284": "s0op",
    "f285_291": "s1op",
    "f292_295": "s2op",
    "f296_301": "s3op",
}
"""The semantic names of the fields named after their measured role, by id (`fenykl dsp
fields` prints them)."""
_IMM_BITS = {
    "f37_40": "; bits 11..8 of S0's lane immediate",
    "f53_56": "; bits 6..3 of S2's lane immediate",
    "f65_68": "; bits 9..6 of S3's lane immediate",
}


_HEAD_TIERS = {
    V: "verified: every value has a verified row and lag",
    I: "inferred: not every value has a verified row and lag",
}


def _table_roles() -> Iterator[tuple[str, str]]:
    """The roles of the lane operand registers, the PB ports and the S heads, from their
    tables."""
    names = {f.id: f.name for f in FIELDS} | NAMES
    for file, (unit, *nibbles) in LANE_FILES.items():
        for operand, nibble in zip("ab", nibbles, strict=True):
            bits = _IMM_BITS.get(nibble, "")
            yield (
                nibble,
                (
                    f"{unit.upper()}'s lane operand {operand}: the register of {file} its lane "
                    f"heads read as {operand}{bits} (`LANE_FILES`, verified)"
                ),
            )
    lane_ports = {enable: lane_file(enable) for enable in LANE_SOURCES}
    for table, files in (("LANE_SOURCES", lane_ports), ("PB_SOURCES", PB_FILES)):
        for enable, file in files.items():
            port = PORTS[enable]
            yield (
                port,
                f"the register of {file} its port writes under {names[enable]} (verified)",
            )
            yield (
                enable,
                (
                    f"the enable of a port of {file}, its value the source it writes to "
                    f"{file}[{names[port]}] (`{table}`, verified)"
                ),
            )
    for enable, file in PR_FILES.items():
        port = PORTS[enable]
        yield (
            port,
            f"the register of {file} its port writes under {names[enable]} (verified)",
        )
        yield (
            enable,
            (
                f"the enable of a port of {file}, its value the source it writes to "
                f"{file}[{names[port]}] (`PR_SOURCES`, verified)"
            ),
        )
    heads = {_fid(*HEADS[u]): u for u in ("s0", "s1", "s2", "s3")}
    for f in FIELDS:
        if f.id in heads:
            yield (
                f.id,
                (
                    f"the head of {heads[f.id].upper()}: its value names the lane function and "
                    f"the scalar result (`LANE_FUNCTIONS`, `S_RESULTS`; {_HEAD_TIERS[f.confidence]})"
                ),
            )


_K = "(`X_IMMEDIATES`, verified)"
_S_IMM = "(`LANE_IMMEDIATES`, verified)"
ROLES = dict(_table_roles()) | {
    "f0": f"bit 0 of S1's lane immediate {_S_IMM}",
    "f1": f"bit 0 of X0's immediate K {_K}",
    "f2_6": f"bits 5..1 of X0's immediate K {_K}",
    "f7_8": f"bits 1..0 of X1's immediate K {_K}",
    "f9_14": f"bits 7..2 of X1's immediate K {_K}",
    "f23_30": f"bits 7..0 of S0's lane immediate {_S_IMM}",
    "f31_34": "the register of U whose lanes 0..3 S3's lane shifts move in (`LANE_THIRD_FILE`); "
    "its bit 2 is bit 4 of the lane of 0xe, 0xf and 0x10, its bits 3..2 are bits 5..4 of S3's "
    "lane immediate (verified)",
    "f69_71": "the register of O that X0's store heads store (`STORE_FILE`, verified)",
    "f72_75": "S0's scalar s: the register of F0 read as the bound of its clamps and as its "
    "select heads' lane mask (`LANE_OPERANDS`, `LANE_MASKS`, verified)",
    "f76_79": "S0's scalar r: the register of F0 whose bit k is lane k's carry-in, the lane "
    "value of 0x43 and 0x45, and head 0x52's result; bits 15..12 of S0's lane immediate "
    "(`LANE_OPERANDS`, `LANE_FUNCTIONS`, `SCALAR_READS`, verified)",
    "f80_83": "S1's scalar r: the register of F1 whose bit k is lane k's carry-in in the "
    "carry-in heads and the floor shifts; its bits 3..2 are bits 6..5 of S1's lane immediate "
    "(`LANE_OPERANDS`, verified)",
    "f84_87": "S1's scalar t: the register of F1 its lane heads read, and head 0x54's result; "
    "bits 4..1 of S1's lane immediate (`LANE_OPERANDS`, `SCALAR_READS`, verified)",
    "f88_91": "S2's scalar t: the register of F2 the multiplies 0x3 / 0x5 read, and head 0xe's "
    "result; its bits 3..1 are bits 2..0 of S2's lane immediate, its bit 1 head 0x7's choice "
    "(`LANE_OPERANDS`, `SCALAR_READS`, verified)",
    "f92_95": "S3's lane select and shift count, and the register of F3 read as the lane index; "
    "bits 3..0 of S3's lane immediate (verified)",
    "f96_99": "S3's value: the register of F3 head 0x24 returns and 0x10 inserts; bits 13..10 "
    "of S3's lane immediate (`SCALAR_READS`, `LANE_FUNCTIONS`, verified)",
    "f120_122": "the op slot's R register: a family A or A' store's datum and the third operand "
    "of 0x46 / 0x47; bits 15..13 of a family B or D immediate (`FAMILIES`, verified)",
    "f128_132": "the op slot's register rb in families A' and E, and op 4's link register "
    "G[rb]; bits 12..8 of an immediate and of a control op's target (`FAMILIES`, `TARGET`, "
    "verified)",
    "f141_144": "the register of U its port writes under usrc (`LANE_THIRD_FILE`, verified)",
    "f179_181": "the register of O its port writes under osrc (`STORE_FILE`, verified)",
    "f232_233": "the enable of U's port: 1 writes U[udst] beside S3 0x1b, 0x1c and 0x1e..0x21, "
    "0 and 2 write nothing (verified); under 1 nothing beside 0x14, 0x18, 0x19 and 0x22 "
    "(measured), a content not identified beside 0x1a and 0x1d (inferred) "
    "(`LANE_THIRD_FILE`)",
    "f252_254": "the enable of O's port, its value the source it writes to O[odst] "
    "(`STORE_SOURCES`, verified)",
    "f15_22": "the op slot's 8-bit immediate: the low byte of a data op's immediate and of a "
    "control op's target (`FAMILIES`, `TARGET`, verified)",
    "f100_104": "X0's register a, A[x0a] (`X_FUNCTIONS`, verified)",
    "f105_109": "X0's register b, A[x0b]; bits 10..6 of X0's immediate K (`X_IMMEDIATES`, "
    "verified)",
    "f110_114": "X1's register a, B[x1a] (`X_FUNCTIONS`, verified); in X1 0x1c and 0x1d its "
    "index, 0 the stream engine (`STREAM_SELECT`, `WAIT_NOTHING`)",
    "f115_119": "X1's register b, B[xreg], also the register X1 0x1c hands to the stream "
    "engine; bits 12..8 of X1's immediate K (`X_IMMEDIATES`, verified)",
    "f123_127": "the op slot's register ra, G[ra], in every data op (`FAMILIES`, verified)",
    "f229": "clear in exactly the words whose op is a control op (verified)",
    "f302_307": "the head of X0: its value names the function, a lane memory load or store "
    "among them (`X_FUNCTIONS`, `X0_LANE_MEMORY`, verified)",
    "f308_312": "the head of X1: its value names the function or a register transfer "
    "(`X_FUNCTIONS`, verified)",
    "f313_319": "the op: control ops 0..4, data ops 8..73 in their families, idle 0x7f (`OPS`, "
    "`FAMILIES`, verified)",
    "f274_275": "the S unit whose scalar result M puts out, 0..3 for S0..S3 (verified)",
    "f276": "the active-low strobe M: under 0 an S head's scalar result goes on X0's result "
    "bus (`S_RESULTS`, verified)",
}
"""What each named field does, with its tier: its fact, with one sentence of evidence after."""
FIELDS = tuple(replace(f, name=NAMES[f.id]) if f.id in NAMES else f for f in FIELDS)
_roleless = {f.id for f in FIELDS if f.name} ^ set(ROLES)
if _roleless:
    raise ValueError(f"named fields and roles differ: {sorted(_roleless)}")

_RESTATED = {"f15_22": "; ", "f221_223": ": ", "f229": "; ", "f313_319": ": "}
"""Fields whose first sentence goes on to restate the role after this separator."""


def _evidence(f: Field) -> str:
    """The one sentence of a named field's boundary and ownership evidence: for a grid nibble
    its owner's share of its changes, else the first sentence of the fact it was built with.
    The rest of that fact is in the tables its role names (`NIBBLES`, `UNIT_FACTS`, ...)."""
    if f.lo in GRID_A_UNITS:
        unit, p = GRID_A_UNITS[f.lo]
        tag = "verified in the bytes" if p >= WEAK_OWNER else "inferred"
        return (
            f"A grid A nibble, its boundaries from segmentation and the lag-110 repeat in grid "
            f"B; it changes where {_fid(*HEADS[unit])} is active in {p:.2f} of its changes ({tag})"
        )
    if f.kind is Kind.ROUTE and f.id in _OWNER:
        p = _PORT_P[f.id]
        tag = "verified in the bytes" if p >= WEAK_OWNER else "inferred"
        return (
            f"A grid B nibble, its boundaries from segmentation and the lag-110 repeat of grid "
            f"A; it changes where its enable {_OWNER[f.id]} is active in {p:.2f} of its "
            f"changes, across later firmware versions of the same family ({tag})"
        )
    first = f.fact.split(". ", 1)[0]
    if f.id in _RESTATED:
        first = first.split(_RESTATED[f.id], 1)[0]
    return f"{first[0].upper()}{first[1:]}"


FIELDS = tuple(
    replace(f, fact=f"{ROLES[f.id]}. {_evidence(f)}") if f.name else f for f in FIELDS
)
ID_PATTERN = re.compile(r"f\d")
"""The start of a positional id; no name starts like one."""


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
        if f.name and (ID_PATTERN.match(f.name) or keyword.iskeyword(f.name)):
            raise ValueError(f"{f.id}: name {f.name!r} reads as an id or is a keyword")
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
class Bits:
    """Bits lo..hi of `field`, counted from the field's own bit 0."""

    field: Field
    lo: int
    hi: int

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1

    @property
    def span(self) -> int:
        return ((1 << self.width) - 1) << (self.field.lo + self.lo)

    def get(self, word: int) -> int:
        return self.field.get(word) >> self.lo & ((1 << self.width) - 1)

    def put(self, word: int, value: int) -> int:
        mask = (1 << self.width) - 1
        if not 0 <= value <= mask:
            raise ValueError(
                f"{self.field.label}: {value:#x} does not fit {self.width} bits"
            )
        return self.field.put(
            word, self.field.get(word) & ~(mask << self.lo) | value << self.lo
        )


@dataclass(frozen=True)
class Composite:
    """A value whose bits lie in several fields or parts of fields, lowest part first; two's
    complement when `signed`."""

    name: str
    parts: tuple[Field | Bits, ...]
    confidence: Confidence
    fact: str
    signed: bool = False

    @property
    def width(self) -> int:
        return sum(p.width for p in self.parts)

    @property
    def span(self) -> int:
        """Its bits in the word."""
        span = 0
        for p in self.parts:
            span |= p.span
        return span

    def get(self, word: int) -> int:
        value = 0
        for p in reversed(self.parts):
            value = value << p.width | p.get(word)
        if self.signed and value >> (self.width - 1):
            value -= 1 << self.width
        return value

    def compose(self, values: Mapping[str, int]) -> int:
        """The value from the values of its fields by id."""
        word = 0
        for p in self.parts:
            f = p.field if isinstance(p, Bits) else p
            word = f.put(word, values[f.id])
        return self.get(word)

    def put(self, word: int, value: int) -> int:
        lo = -(1 << (self.width - 1)) if self.signed else 0
        if not lo <= value < lo + (1 << self.width):
            sign = " signed" if self.signed else ""
            raise ValueError(
                f"{self.name}: {value:#x} does not fit {self.width}{sign} bits"
            )
        value &= (1 << self.width) - 1
        for p in self.parts:
            word = p.put(word, value & ((1 << p.width) - 1))
            value >>= p.width
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
"""Each X unit's immediate; its high bits are the register field of its operand b. K is raw: each
head reads it zero- or sign-extended (`semantics.X_RULES`)."""
_S_IMM_PARTS = {
    "s0": (BY_KEY["f23_30"], BY_KEY["f37_40"], BY_KEY["f76_79"]),
    "s1": (BY_KEY["f0"], BY_KEY["f84_87"], Bits(BY_KEY["f80_83"], 2, 3)),
    "s2": (Bits(BY_KEY["f88_91"], 1, 3), BY_KEY["f53_56"]),
    "s3": (
        BY_KEY["f92_95"],
        Bits(BY_KEY["f31_34"], 2, 3),
        BY_KEY["f65_68"],
        BY_KEY["f96_99"],
    ),
}
S_IMMEDIATES = {
    unit: Composite(
        f"{unit}imm",
        parts,
        LANE_IMMEDIATES[unit][1],
        LANE_IMMEDIATES[unit][0],
        unit != "s0",
    )
    for unit, parts in _S_IMM_PARTS.items()
}
"""Each S unit's lane immediate as the machine reads it (`LANE_IMMEDIATES`)."""


@dataclass(frozen=True)
class HeadView:
    """A head's unit by named operands: the head as op, its registers and its immediate composed
    from its pieces, which share bits with register operands."""

    unit: str
    operands: Mapping[str, Field | Composite]

    def decode(self, word: int) -> dict[str, int]:
        return {name: o.get(word) for name, o in self.operands.items()}

    def encode(self, word: int, values: Mapping[str, int]) -> int:
        """`word` with the operands `values`; raises on a name not in the view, a value out of
        range and two operands that disagree on a shared bit."""
        given = 0
        for name, value in values.items():
            if name not in self.operands:
                raise KeyError(f"{self.unit} has no operand {name!r}")
            o = self.operands[name]
            new = o.put(word, value)
            if (new ^ word) & given:
                raise ValueError(
                    f"{self.unit}: {name}={value:#x} disagrees with another operand"
                )
            given |= o.span
            word = new
        return word


def _view(unit: str, **operands: str | Composite) -> HeadView:
    head = BY_KEY[_fid(*HEADS[unit])]
    named = {k: BY_KEY[o] if isinstance(o, str) else o for k, o in operands.items()}
    return HeadView(unit, {"op": head, **named})


HEAD_VIEWS = {
    "s0": _view("s0", a="s0a", b="s0b", s="s0s", r="s0r", imm=S_IMMEDIATES["s0"]),
    "s1": _view("s1", a="s1a", b="s1b", t="s1t", r="s1r", imm=S_IMMEDIATES["s1"]),
    "s2": _view("s2", a="s2a", b="s2b", t="s2t", imm=S_IMMEDIATES["s2"]),
    "s3": _view(
        "s3",
        a="s3a",
        b="s3b",
        u="s3u",
        idx="s3idx",
        val="s3val",
        imm=S_IMMEDIATES["s3"],
    ),
    "x0": _view("x0", a="x0a", b="x0b", o="x0o", k=X_IMMEDIATES["x0"]),
    "x1": _view("x1", a="x1a", b="xreg", k=X_IMMEDIATES["x1"]),
}
"""The operand view of each head's unit, every field of the unit in some operand: S0 a, b
(L0[a], L0[b]), s (F0[s]), r (F0[r], the carry-in mask), imm16; S1 a, b, t (F1[t]), r, imm7;
S2 a, b, t (F2[t]), imm7; S3 a, b, u (U[u]), idx (the lane, the shift count or F3[idx]), val
(F3[val]), imm14; X0 a, b (A[a], A[b]), o (O[o], the store register), K; X1 a, b (B[a], B[b]), K
(`LANE_OPERANDS`, `LANE_IMMEDIATES`, `X_IMMEDIATES`)."""

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
    "three words after the load, where a word with f272_273 = 1 or f270_271 = 1 takes it, also "
    "a word with a control op, a store, a load or a multiply; the bus ORs it with the result of a "
    "data op or of op 0 in that word and with a product arriving there (verified on the camera, "
    "pre-registered on values whose bits are not nested); consecutive loads deliver in "
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
        f"ra & the sign-extended imm16 ({EXACT}); 0x0a ra | (imm16 << 16) (verified on the camera, pre-registered)",
    ),
    Family(
        "C",
        (0x3E, 0x3F, 0x40),
        True,
        5,
        "ra, rb and an immediate 0..31 in every word: 0x3e ra >> imm5 arithmetic, 0x3f ra >> "
        "imm5 logical, 0x40 ra << imm5, ra shifted, not rb (verified on the camera by exact readout, pre-registered); rb and "
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
        "(verified on the camera, pre-registered); f120_122 is 0 in 99 % of the stock words (`semantics.OP_SEMANTICS`)",
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
        f"({EXACT} unless tagged): 0x08 / 0x09 rb, 0x32 abs(rb), 0x3c ra & rb, 0x3a "
        "ra | rb, 0x34 ra - rb, 0x35 ra + rb, 0x33 -rb, 0x3d ~ra, 0x2f min signed, 0x2e max "
        "signed, 0x2c max unsigned, 0x41 ra >> (rb & 31) arithmetic, 0x42 logical, "
        "0x43 ra << (rb & 31), compares to 1 or 0: 0x28 / 0x29 ra <= rb, ra < rb "
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
    0x0A: "periodic immediates such as 0x5555 and 0x3333: ra | (imm16 << 16) (verified on the camera, pre-registered), a "
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
    0x1E: "at a routine's entry + 1 in 67 routines: ra - imm16 (verified on the camera, pre-registered), a frame allocate "
    "there (hypothesis)",
    0x1F: "in 300 return delay slots, its immediate a multiple of 4 in all: ra + imm16 "
    "(verified on the camera, pre-registered), the frame release there (hypothesis)",
    0x36: f"imm 1, f128_132 and f120_122 0 in all 53 words: ra ^ 1 there ({EXACT} for the "
    "rule), a toggle (hypothesis)",
    0x37: f"periodic immediates such as 0x5555 and 0x2222: ra | imm15 ({EXACT}), a lane or "
    "phase mask set-up (hypothesis)",
    0x38: "1022 of 1321 immediates are 2^k - 1 or its complement: ra & the sign-extended imm16 "
    f"({EXACT})",
    0x3F: "ra >> imm5, logical (verified on the camera by exact readout, pre-registered)",
    0x40: "ra << imm5 (verified on the camera by exact readout, pre-registered)",
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
_FAMILY = {family.name: family for family in FAMILIES}
UNSEEN_FORMATS = {
    0x22: Format(_FAMILY["D"], 16, False),
    0x26: Format(_FAMILY["D"], 16, False),
    0x27: Format(_FAMILY["D"], 16, False),
    **{value: Format(_FAMILY["E"], 0, False) for value in (0x2D, 0x39, 0x3B, 0x45)},
}
"""The data op values that never occur in the stock programs, with the operand format their
results on eight operand sets read through (`UNSEEN_FORMAT_FACTS`: both readings were given to
each op)."""
_E_UNSEEN = (
    "never occurs in the stock programs; ra and rb as family E reads them, f120_122 without "
    "effect (verified on the camera by exact readout, eight operand sets)"
)
_D_UNSEEN = (
    "never occurs in the stock programs; ra and imm16 = imm | f128_132 << 8 | f120_122 << 13 "
    "zero-extended (verified on the camera by exact readout: two jobs with ra below, equal to "
    "and above imm16, imm16 0x8000 and 0xffff against ra 0x8000, 0xffff8000 and 0x10000)"
)
UNSEEN_FORMAT_FACTS = {
    **{value: _D_UNSEEN for value in (0x22, 0x26, 0x27)},
    **{value: _E_UNSEEN for value in (0x2D, 0x39, 0x3B, 0x45)},
}
FORMATS |= UNSEEN_FORMATS


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
