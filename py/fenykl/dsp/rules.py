"""The shapes `machine.Machine` runs at verified because they ran on the camera: the timing and bus
rules of the datapath beyond the encoding (`fields`). A shape outside them runs at inferred or
lower."""

from itertools import combinations

from .fields import M_UNITS

LOAD_CHAIN_HEAD = 0x1D
"""The X0 lane load head of the measured load chains (`LOAD_CHAINS`)."""
LOAD_CHAINS = ((1,) * 15, (2,) * 31, (3,), (4,), (1, 3, 1, 3, 1))
"""X0 `LOAD_CHAIN_HEAD` loads in flight together (each issued at most `fields.LANE_LOAD_LAG` words
after the last) that land as each would alone, as the words between their heads, from the chain's
first load on: each vector at its first lane port `fields.LANE_LOAD_LAG` words after its head
(verified on the camera, pre-registered): sixteen in consecutive words (into L3 and L2, and a
matrix product's rows); up to 32 two words apart (one vector per two words, beside and between
lane stores); two three or four words apart (a head in the word the first load lands); pairs in
consecutive words three words apart, six loads (a two-row convolution). A chain that begins as one
of these counts (a load's tier is fixed as its head runs). Other loads in flight together (longer
or mixed chains, the other load heads) were not measured: inferred."""
DIVIDE_CHAINS = ((1, 1, 1), (2,), (3,), (4,))
"""X1 divides in flight together (each issued at most `fields.DIVIDE_LAG` words after the last)
whose quotients land as each would alone, as the words between their heads, from the chain's
first divide on (verified on the camera, pre-registered): four in consecutive words, and a second
one to four words after the first. A chain that begins as one of these counts; other divides in
flight together were not measured: inferred."""
FIRST_PORT_WINS = (("s0", 0x41), ("s3", 0x19))
"""Both ports of a lane file naming one register in one word: the first port's value (measured on
the camera in L0, L1, L2 and L3, the first port taking S0 0x41's result, the second S3 0x19's,
both in the head's own word, each head alone in its unit). Other sources and heads, and either
port taking nothing, inferred."""

WAIT_HEAD = 0x1D
WAIT_NOTHING = frozenset({1, 2, 8})
"""The registers a with which X1 0x1d hands nothing to the stream engine and returns (inferred:
a = 2 in an op 3 word and in an idle word, a = 1 in an op 3 word, one job each, the load behind
them not landed; a = 8 a lone 0x1d in one job). The resampler's 0x1d in words 210 (a = 2) and
312 (a = 1) are such. Reading (hypothesis): a selects what a 0x1d waits for, and only
`fields.STREAM_SELECT` is the stream engine's queue."""

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
before them S1 0x55..0x5f gave b, or left the sentinel at either single lag (`fields.LANE_FUNCTIONS`).
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

STORE_NOTHING = (5, 6)
"""Values of f252_254 under which O keeps its content, beside a result of every S unit and with
none (verified on the camera, pre-registered on a second register and other heads)."""

S_OWN_OPEN = {"s0": frozenset(range(0x40, 0x60)) - {0x52}}
"""S heads whose own-word result under the M strobe was not read (measured: every other head)."""
M_OTHER_OPEN = {
    "s0": frozenset({0x40, *range(0x47, 0x60)}) - {0x4B, 0x4D},
    "s1": frozenset(range(0x55, 0x60)),
}
"""S heads beside which M under another unit's f274_275, in the head's word and the next, was
not read; beside every other head it was, and nothing was on M there (verified on the camera,
pre-registered)."""
_LANE_ONLY = (("s0", 0x0), ("s1", 0x0), ("s2", 0x8), ("s3", 0x19))
_OWN_WORD = (("s1", 0x54), ("s2", 0xE), ("s3", 0x24))
_HELD_PAIR = (("s0", 0x32), ("s1", 0x45))


def _lane_only_cells(heads: tuple[tuple[str, int], ...]) -> frozenset[tuple[int, str]]:
    """Every unit in the heads' word and the next, but S2 / S3 one word after their own head."""
    later = {u for u, _ in heads} & {"s2", "s3"}
    return frozenset(
        (at, u) for at in (0, 1) for u in M_UNITS if not (at == 1 and u in later)
    )


M_LANE_ONLY = frozenset(_LANE_ONLY)
M_TOGETHER: dict[frozenset[tuple[str, int]], frozenset[tuple[int, str]]] = {
    **{frozenset(p): _lane_only_cells(p) for p in combinations(_LANE_ONLY, 2)},
    frozenset(_LANE_ONLY): _lane_only_cells(_LANE_ONLY),
    **{
        frozenset({o, q}): frozenset({(0, o[0]), (0, q[0])})
        for o in _OWN_WORD
        for q in _LANE_ONLY
        if q[0] != o[0]
    },
    frozenset({("s2", 0xE), ("s3", 0x24)}): frozenset({(0, "s0"), (0, "s1")}),
    frozenset({("s1", 0x54), ("s3", 0x24)}): frozenset({(0, "s0"), (0, "s2")}),
    frozenset(_HELD_PAIR): frozenset((1, u) for u in M_UNITS),
}
"""Several S units' heads in one word, and the M reads measured beside them on the camera
(pre-registered, a fresh sentinel per read) as (words after the head, unit under f274_275):
under a unit with no result there nothing was on M; under a unit with an own-word or held result,
that result alone. Lane-only heads (S0 0x0 lag 1, S1 0x0, S2 0x8, S3 0x19) in pairs and all four,
in their word and the next under every unit, but S2 / S3 one word after their own head there
(nothing was on M, not registered: inferred); an own-word head beside another unit's lane-only
head, in its word under the two units; two own-word heads, in their word under the other units;
S0 0x32 and S1 0x45 (both held), one word later under every unit."""
M_HELD_BESIDE_OWN = frozenset(
    (frozenset({h}), frozenset({o}))
    for h in _HELD_PAIR
    for o in (("s2", 0xE), ("s3", 0x24))
)
"""(the heads of a word, those of the next) where M under the first word's unit in the next word
was measured on the camera (pre-registered): its held result alone, not the other unit's
own-word result. Beside other heads a held result is on M alone at inferred."""
S2_NEXT = frozenset(
    {
        (frozenset({("s2", 0xC)}), frozenset()),
        (frozenset({("s2", 0x1)}), frozenset()),
        (frozenset({("s2", 0xC)}), frozenset({("s2", 0xC)})),
    }
)
"""(the heads of a word, those of the next) where M under S2 in the next word was measured on the
camera (pre-registered): after S2 0xc or S2 0x1 and a word with no S head nothing, after S2 0xc
and S2 0xc the second extract alone."""
X0_BESIDE_KEPT = (0x13, frozenset({("s0", 0x0)}))
"""An X0 head and the S heads of its word beside which nothing on M kept the register M's port
names (measured on the camera, pre-registered, under every f274_275). Beside other X0 heads the
kept register is inferred."""
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
"""S heads without a held row in `fields.S_RESULTS` that put nothing on M one word after the head
under their unit's f274_275 (measured on the camera; S3's reductions: their register from the
third word on). One word after any other head, M under its unit is not known: S1 0x13, 0x1a,
0x1b, 0x14, 0x15, 0x1d, 0x1e, 0x16, 0x17, 0x1f and 0x20 put a value there that is not fitted,
and S0 0x40.., S1 0x60.., S2 and S3 were not read there. Under another unit's f274_275 a held
result is not on M (verified for S0 0x32 and S1 0x45, inferred for the others)."""

WEDGING_S0 = 0x4F
WEDGING_S0_AT = 3
"""The count of `WEDGING_S0` words under M's strobe and port 272 / 2 at which a job lapses."""
WEDGING_S0_WHY = (
    "a third S0 0x4f word under M's strobe with port 272 / 2 in the job: three or four such "
    "words (nibbles 1..3, G12 written) wedged the camera's job, each as a fresh boot's first "
    "job after a stock copy, so the boot's earlier jobs are not needed; one alone (four "
    "settings, either port form) and two (four pairs, adjacent or not) ran; each of three "
    "triples wedged (verified in one frame; other frames and settings inferred)"
)
"""The S0 head whose words, under M's strobe and with port 272 / 2, never let a job end on the
camera from the third such word in a job; one or two ran, each writing nothing through the port."""
LATCH_MEASURED = {
    "s0": frozenset({0x05, 0x20, 0x41}),
    "s1": frozenset({0x00, 0x01, 0x34, 0x53}),
}
"""Heads whose read was latched on the camera; other heads latch at inferred. S1 0x39, which
reads a only, kept S1's latch (measured)."""

SELECT_MEASURED = frozenset({0, 3, 5, 6, 7})
"""The f120_122 values at which op 0x08's select on R[f120_122] was measured with R both 0 and
not (verified on the camera by exact readout); at the others it is inferred."""

TIES_MET = range(1, 4)
"""The counts at which S1's rounding shifts by imm & 15 met ties to even; at the others a tie
lane is inferred (`fields.TIES_AT_10`)."""
