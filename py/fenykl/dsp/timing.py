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
from .fields import Confidence, lookup
from .machine import HEAD_IDS
from .stream import LANE_MEMORY, LOAD, LOCAL_MEMORY, STORE, Engine, Event

I, V = Confidence.INFERRED, Confidence.VERIFIED


@dataclass(frozen=True)
class Cost:
    us: float
    tier: Confidence
    fact: str


JOB = Cost(
    101.0,
    V,
    "the job's fixed cost, the interrupt and callback path: the intercept of loops of 512 to "
    "2048 passes against their pass counts (verified on the camera, in a pre-registered range "
    "of 95 to 115 us; it holds the first runs of those jobs' few dozen words, about 3 us)",
)
REPEAT = Cost(
    0.00537,
    V,
    "a word executed again, in a loop: 5.37 ns, measured on idle words; scalar local loads, "
    "X1's divide, X0 lane-memory loads and taken branches with their delay slots cost the same "
    "within 1.5 ns (verified on the camera); S3 reductions likewise (fitted)",
)
REPEAT_S = Cost(
    REPEAT.us,
    I,
    "a word executed again with an S unit's head: S3 reductions fitted at the idle word's cost, "
    "other S heads not timed (inferred: no measured class costs more than an idle word)",
)
FIRST = Cost(
    0.062,
    I,
    "a word's first execution: 62 ns, the slope over straight-line jobs of 190 to 2000 words "
    "(fitted); paid when the word runs, not for the image's length: a word branched over costs "
    "nothing (verified on the camera)",
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
    "stores from the lane memory: 0.74, 2.14 and 7.19 us for 1, 4 and 16 KiB of bytes, fitted; "
    "16-bit and 12-bit stores ran at the byte store's rate per KiB of DRAM within the jitter "
    "(1.73 to 2.36 us per 4 KiB, fitted)",
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
    (STORE, LANE_MEMORY, 0x20, 0x08): LANE_STORE,
    (STORE, LANE_MEMORY, 0x20, 0x0A): LANE_STORE,
    (LOAD, LOCAL_MEMORY, 4, 0x10): LOCAL_LOAD,
}
"""(kind, r0, r2, r5): the transfers timed on the camera, each a command of one row (r7 = 1)
handed and joined by a 0x1d. Other shapes have no time."""
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
    def tier(self) -> Confidence:
        """The weakest part's tier."""
        return min(p.tier for p in self.parts)

    def __str__(self) -> str:
        if self.unknown:
            return f"unknown: {self.unknown}"
        parts = ", ".join(f"{p.name} {p.us:.2f}" for p in self.parts)
        return f"{self.us:.1f} us [{self.tier.name.lower()}] ({parts})"


def transfer_us(event: Event) -> float:
    """The time of the command `event` hands, from its registers; raises `Unmeasured`."""
    r0, _, r2, _, _, r5, r6, r7 = event.regs
    rate = RATES.get((event.kind, r0, r2, r5))
    if rate is None or None in (r6, r7):
        raise Unmeasured(
            f"a command of kind {event.kind} at r0 {r0}, r2 {r2}, r5 {r5}: not timed"
        )
    if r7 != 1:
        raise Unmeasured(f"a command of {r7} rows: only one row was timed")
    kib = 16 * r6 / 1024
    if not rate.kib[0] <= kib <= rate.kib[1]:
        raise Unmeasured(
            f"a transfer of {kib:g} KiB: timed at {rate.kib[0]:g}..{rate.kib[1]:g} KiB"
        )
    return rate.per_command + rate.per_kib * kib


def estimate(job: Job, flow: Flow, engine: Engine | None = None) -> Estimate:
    """The time of `job` as `flow` executed it (`Executor.step` on a `Machine`), with the stream
    `engine` the machine drove. Raises ValueError for a flow that did not halt."""
    if flow.stop is not Stop.HALT:
        raise ValueError(f"the job did not halt: {flow.why}")
    if engine is not None and engine.off:
        return Estimate(unknown=f"the stream engine went off script: {engine.off}")
    events: dict[int, list[Event]] = {}
    for e in engine.events if engine is not None else []:
        events.setdefault(e.t, []).append(e)
    words = job.program.words
    seen: set[int] = set()
    counts = {FIRST: 0, REPEAT: 0, REPEAT_S: 0}
    now, free, waited, joined = 0.0, 0.0, 0.0, False
    ends: deque[float] = deque()
    for t, step in enumerate(flow.steps):
        if step.index not in seen:
            cost = FIRST
        elif any(words[step.index][HEAD_IDS[u]] != _idle(u) for u in S_UNITS):
            cost = REPEAT_S
        else:
            cost = REPEAT
        seen.add(step.index)
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
    parts = [Part("job", JOB.us, JOB.tier)]
    parts += [
        Part(name, n * c.us, c.tier)
        for name, c in (
            ("first runs", FIRST),
            ("repeats", REPEAT),
            ("S repeats", REPEAT_S),
        )
        if (n := counts[c])
    ]
    if joined:
        parts.append(Part("stream waits", waited, I))
    return Estimate(tuple(parts))


def _idle(unit: str) -> int:
    return lookup(HEAD_IDS[unit]).idle
