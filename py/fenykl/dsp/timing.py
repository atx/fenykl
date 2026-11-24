"""A job's time from a run of the datapath executor: the words it executed, the job's fixed cost
and the stream engine's transfers where the words do not hide them.

The figures were measured on the camera as the job's completion time (the callback after the
job's interrupt) against loop pass counts, each the minimum over repeats: single jobs jitter by
up to 1.5 ms, so the estimate is the job's minimum time, not a typical one. Every figure carries
its tier; fitted figures are inferred. A case none of them covers makes the estimate unknown
(`Estimate.unknown` says why): the estimate is never guessed.
"""

from collections import deque
from dataclasses import dataclass

from .emu import Flow, Job, Stop
from .fields import CONTROL_OPS, IDLE_OP, Confidence, lookup
from .machine import HEAD_IDS
from .stream import LANE_MEMORY, LOAD, LOCAL_MEMORY, STORE, Engine, Event

I, V = Confidence.INFERRED, Confidence.VERIFIED


@dataclass(frozen=True)
class Cost:
    us: float
    tier: Confidence
    fact: str


JOB = Cost(
    98.4,
    I,
    "the job's fixed cost, the interrupt and callback path: the 101 us intercept of loops of "
    "512 to 2048 passes against their pass counts (verified on the camera, in a pre-registered "
    "range of 95 to 115 us) less the first runs of those jobs' 42 words (2.6 us, fitted)",
)
REPEAT = Cost(
    0.00537,
    V,
    "a word executed again, in a loop: 5.37 ns, measured on idle words; scalar local loads, "
    "X1's divide, X0 lane-memory loads and taken branches with their delay slots cost the same "
    "within 1.5 ns (verified on the camera, in loop passes of 9 to 106 words closed by ops 0..2); "
    "idle words at 5.2 to 5.3 ns in passes of 64 to 1024 words, and in passes that jump 1088 "
    "words away and back by op 2 or op 3 (verified on the camera, pre-registered: no loop "
    "buffer and no cache smaller than 1024 words; passes of 2000 words 5.24 ns from four "
    "totals, and a 900-word straight run repeated after 900 other words at the repeat rate, "
    "pre-registered); in passes of 107 to 2000 words only idle words were "
    "timed, so other words there repeat at `REPEAT_I`; "
    "S3 reductions likewise (fitted)",
)
REPEAT_I = Cost(
    REPEAT.us,
    I,
    "a word executed again outside the measured passes (a pass under 9 words, over 106 for a "
    "word that is not idle, over 2000 for an idle word, or "
    "with an op 4 since the word's last run), or with an S unit's head: S3 reductions "
    "fitted at the idle word's cost, other S heads not timed (inferred: no measured class costs "
    "more than an idle word)",
)
PASSES = range(9, 107)
"""Executed words between two runs of a word in the loops timed on the camera, any word class."""
IDLE_PASSES = range(9, 2001)
"""Likewise for idle words."""
FIRST = Cost(
    0.064,
    I,
    "a word's first execution: 64 ns, the slope over straight-line jobs of 200 to 1000 idle "
    "words (fitted; earlier runs of 0 to 1900 words gave 62 to 70 ns, all within a "
    "pre-registered 50 to 75 ns); words of ALU ops cost the same as idle words (verified on "
    "the camera); paid when the word runs, not for the image's length: a word branched over "
    "costs nothing unless it shares a fetch line with a word that runs (`LINE`)",
)
LINE = 8
"""Words per fetch line, aligned to image indices that are multiples of 8: the words of a
line that runs but that the job never executes cost `UNRUN` each (inferred: chains of
8-word blocks every 16 words cost nothing extra at offsets 0 and 8 and about 6.5 words per
block at offsets 4 and 12, pre-registered as one of the line sizes named; blocks every 10
and 12 words fit the line within a word per block; every 24 and 64 words they cost 2 to 5
words per block more than the lines predict, unexplained)."""
UNRUN = Cost(
    0.064,
    I,
    "a word never executed in a fetch line some word of which runs: the first-run rate "
    "(fitted: an extra line measured 0.82 of eight first runs)",
)
S_UNITS = ("s0", "s1", "s2", "s3")


@dataclass(frozen=True)
class Rate:
    """A transfer's time: `per_command` plus `per_kib` per KiB of DRAM it moves, for commands
    of one row of `kib` KiB of DRAM (the sizes measured, inclusive). Measured as the pass time
    of loops whose pass hands one command and its 0x1d, fitted over the sizes; `per_command` is
    the fit's intercept less the pass's own words (about 0.06 us)."""

    per_kib: float
    per_command: float
    kib: tuple[float, float]
    fact: str
    tier: Confidence = I


LANE_LOAD = Rate(
    0.442,
    0.2,
    (1, 16),
    "byte loads into the lane memory: 0.91, 1.72 and 7.38 us at 1, 4 and 16 KiB, fitted "
    "(about 2.2 KiB per us)",
)
LANE_STORE = Rate(
    0.427,
    0.3,
    (1, 16),
    "byte stores from the lane memory: 0.74, 2.14 and 7.19 us for 1, 4 and 16 KiB, fitted",
)
PACKED_STORE = Rate(
    LANE_STORE.per_kib,
    LANE_STORE.per_command,
    (4, 4),
    "16-bit and 12-bit stores from the lane memory: 1.73 to 1.94 and 1.56 us per 4 KiB of DRAM, "
    "the byte store's rate within the jitter (fitted, at 4 KiB only)",
)
LOCAL_LOAD = Rate(
    1.44,
    0.0,
    (4, 4),
    "byte loads into local memory at r2 = 4: 5.77 us per 4 KiB, one size (about 0.7 KiB per "
    "us, a third of the lane memory's rate)",
)
RATES: dict[tuple[int, int, int, int], Rate] = {
    (LOAD, LANE_MEMORY, 0x20, 0x10): LANE_LOAD,
    (STORE, LANE_MEMORY, 0x20, 0x10): LANE_STORE,
    (STORE, LANE_MEMORY, 0x20, 0x08): PACKED_STORE,
    (STORE, LANE_MEMORY, 0x20, 0x0A): PACKED_STORE,
    (LOAD, LOCAL_MEMORY, 4, 0x10): LOCAL_LOAD,
}
"""(kind, r0, r2, r5): the transfers timed on the camera, each a command of one row (r7 = 1)
handed and joined by a 0x1d, with r3 the units its r5 r6 elements fill. Other shapes have no
time."""
QUEUE = 16
"""Commands handed and not yet waited for whose times are known: 16 ran with no 0x1d (verified);
past 19 the engine holds commands until a 0x1d frees room, and its depth is 44..47 (44
completed, 48 wedged the job: `stream.DEPTH`); how long held commands take was not timed."""
OVERLAP = (
    "the stream engine: one for all channels, its commands run one after another from their "
    "hand-off (two 8 KiB loads on two channels took about as long as one 16 KiB load: fitted); "
    "a 0x1d waits for what the words since its command did not hide (idle words between an "
    "8 KiB load and its 0x1d hid it: fitted)"
)


class Unmeasured(Exception):
    """A case without a measured time."""


@dataclass(frozen=True)
class Part:
    name: str
    us: float
    tier: Confidence


@dataclass(frozen=True)
class Estimate:
    """A job's minimum time as parts, each with its tier; `us` is None when it is not known."""

    parts: tuple[Part, ...] = ()
    unknown: str = ""

    @property
    def us(self) -> float | None:
        return None if self.unknown else sum(p.us for p in self.parts)

    @property
    def tier(self) -> Confidence | None:
        """The weakest part's tier; None when the time is not known."""
        return None if self.unknown else min(p.tier for p in self.parts)

    def __str__(self) -> str:
        if self.unknown:
            return f"unknown: {self.unknown}"
        parts = ", ".join(f"{p.name} {p.us:.2f}" for p in self.parts)
        return f"{self.us:.1f} us [{self.tier.name.lower()}] ({parts})"


def transfer_us(event: Event) -> float:
    """The time of the command `event` hands, from its registers; raises `Unmeasured`."""
    if None in event.regs:
        raise Unmeasured("a register not handed: the command moves nothing, not timed")
    r0, _, r2, r3, _, r5, r6, r7 = event.regs
    rate = RATES.get((event.kind, r0, r2, r5))
    if rate is None:
        raise Unmeasured(
            f"a command of kind {event.kind} at r0 {r0}, r2 {r2}, r5 {r5}: not timed"
        )
    if r3 != -(-r5 * r6 // r2):
        raise Unmeasured(f"r3 {r3:#x} short of or past what r6 fills: not timed")
    if r7 != 1:
        raise Unmeasured(f"a command of {r7} rows: only one row was timed")
    kib = 16 * r6 / 1024
    if not rate.kib[0] <= kib <= rate.kib[1]:
        raise Unmeasured(
            f"a transfer of {kib:g} KiB: timed at {rate.kib[0]:g}..{rate.kib[1]:g} KiB"
        )
    return rate.per_command + rate.per_kib * kib


def estimate(job: Job, flow: Flow, engine: Engine) -> Estimate:
    """The time of `job` as `flow` executed it (`Executor.step` on a `Machine`), with the stream
    `engine` the machine drove (a fresh one per job, as the machine needs). The word parts and
    the waits are capped at the weakest control step's tier: the flow decides which words run.
    Raises ValueError for a flow that did not halt."""
    if flow.stop is not Stop.HALT:
        raise ValueError(f"the job did not halt: {flow.why}")
    if engine.off:
        return Estimate(unknown=f"the stream engine went off script: {engine.off}")
    events: dict[int, list[Event]] = {}
    for e in engine.events:
        events.setdefault(e.t, []).append(e)
    words = job.program.words
    # Each index's last executed word and the count of op 4 returns run up to it.
    last: dict[int, tuple[int, int]] = {}
    links = 0
    flow_tier = V
    counts = {FIRST: 0, REPEAT: 0, REPEAT_I: 0}
    now, free, waited, joined = 0.0, 0.0, 0.0, False
    ends: deque[float] = deque()
    for t, step in enumerate(flow.steps):
        if step.op in CONTROL_OPS and step.confidence is not None:
            flow_tier = min(flow_tier, step.confidence)
        before = last.get(step.index)
        if before is None:
            cost = FIRST
        elif (
            t - before[0] not in (IDLE_PASSES if step.op == IDLE_OP else PASSES)
            or links != before[1]
            or any(words[step.index][HEAD_IDS[u]] != _idle(u) for u in S_UNITS)
        ):
            cost = REPEAT_I
        else:
            cost = REPEAT
        links += step.op == 4
        last[step.index] = (t, links)
        counts[cost] += 1
        now += cost.us
        for e in events.get(t, []):
            if e.kind is not None:
                try:
                    start = max(now, free)
                    free = start + transfer_us(e)
                except Unmeasured as x:
                    return Estimate(unknown=f"word {step.index}: {x}")
                ends.append(free)
                if len(ends) > QUEUE:
                    return Estimate(
                        unknown=f"word {step.index}: more than {QUEUE} commands handed "
                        "before their 0x1d: the later ones may not run"
                    )
                continue
            joined = True
            if ends:
                end = ends.popleft()
                waited += max(end - now, 0.0)
                now = max(now, end)
    if ends:
        return Estimate(
            unknown=f"{len(ends)} commands outstanding at the halt: whether the job waits "
            "for them was not timed"
        )
    run = {i for i in last}
    unrun = sum(
        1
        for line in {i // LINE for i in run}
        for i in range(line * LINE, min(line * LINE + LINE, len(words)))
        if i not in run
    )
    parts = [Part("job", JOB.us, JOB.tier)]
    parts += [
        Part(name, n * c.us, min(c.tier, flow_tier))
        for name, c in (
            ("first runs", FIRST),
            ("repeats", REPEAT),
            ("inferred repeats", REPEAT_I),
        )
        if (n := counts[c])
    ]
    if unrun:
        parts.append(
            Part("unrun line words", unrun * UNRUN.us, min(UNRUN.tier, flow_tier))
        )
    if joined:
        parts.append(Part("stream waits", waited, min(I, flow_tier)))
    return Estimate(tuple(parts))


def _idle(unit: str) -> int:
    return lookup(HEAD_IDS[unit]).idle
