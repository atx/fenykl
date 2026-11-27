"""A job's time from a run of the datapath executor: the words it executed, the job's fixed cost
and the stream engine's transfers where the words do not hide them.

The figures were measured on the camera as the job's completion time (the callback after the
job's interrupt) against loop pass counts, each the minimum over repeats: single jobs jitter by
up to 1.5 ms, so the estimate is the job's minimum time, not a typical one. Every figure carries
its tier; fitted figures are inferred. A case none of them covers makes the estimate unknown
(`Estimate.unknown` says why): the estimate is never guessed.

Earlier runs agree with these figures: a branch over 1200 idle words cost nothing and running
them cost the first-run rate each, the call times of 58 straight-line jobs fit one cost per word,
and 36-word loop passes took 0.185 us, idle and compute words alike (measured on the camera;
inferred).

Three kernels that stream 4 to 16 KiB from DRAM in 4 KiB chunks, the next load handed before the
current compute (an elementwise op DRAM to DRAM, a byte sum, a dense layer of 64 inputs; 1 to 16
passes over the data; 112 to 446 us), ran on the camera within -10 to +10 us of the estimate,
minimum over three repeats, each in its pre-registered window of 15 us + 5 %; with their compute
words or their transfers idled they stayed within it too, so words with S1, S2 and S3 heads and
their ports cost as idle words there (verified on the camera). The elementwise kernel is the
worked example in `tests/test_dsp_kernel_example.py`.

Own jobs of 0.4 to 7.0 ms all ended normally (`LONGEST_JOB_US`); in them a looped word cost
5.21 ns at 64-word passes, plus about 105 us per job (fitted).

What the estimate rests on: the repeat cost (`REPEAT`) and the lane-memory byte and 16-bit
rates and the 12-bit store rate are verified; the job's fixed cost, the first-run and fetch-line
costs, the local-memory rates and how transfers overlap the words are fitted (inferred). The
tests check the arithmetic on synthetic jobs; none pins an estimate to a time measured on the
camera.
"""

from collections import deque
from dataclasses import dataclass

from .fields import CONTROL_OPS, IDLE_OP, M_UNITS, Confidence, lookup
from .job import Flow, Job, Stop
from .machine import HEAD_IDS
from .stream import LANE_MEMORY, LOAD, LOCAL_MEMORY, STORE, Engine, Event

I, V = Confidence.INFERRED, Confidence.VERIFIED

LONGEST_JOB_US = 7000.0
"""The longest own jobs run: jobs of 0.4 to 7.0 ms all ended normally (verified on the camera,
pre-registered); nothing between 7.0 and 7.5 ms was run. The firmware gives this job type 8 ticks
of 1 ms before it aborts the job, so keep a job below this."""


@dataclass(frozen=True)
class Cost:
    us: float
    tier: Confidence


JOB = Cost(97.9, I)
"""The job's fixed cost, the interrupt and callback path: the 101 us intercept of loops of 512 to
2048 passes against their pass counts (verified on the camera, in a pre-registered range of 95 to
115 us) less the first runs of those jobs' 42 words (2.7 us at `FIRST`) and their unrun line words
(0.4 us at `UNRUN`), fitted; all-idle straight runs of 152 to 1900 words on five later boots put the
intercept at 94.6 to 97.0 us (a fit of two boots held, registered, on a third), within the 3 to 5 us
scatter of a boot's minima: one constant, about 2 us high for straight runs."""
REPEAT = Cost(0.0052, V)
"""A word executed again, in a loop: 5.2 ns, registered at 5.2 +- 0.1 ns and held on the camera in
idle passes of 9, 16, 18, 36, 106, 256 and 1024 words (5.15 to 5.25 ns, the pass time 5.19 ns per
word + 1.3 ns per pass) and in 18-word passes of S-head, X0, mixed and idle words (5.15 to 5.26 ns);
a boot before gave 5.10 to 5.24 ns at the same lengths and classes, and the first figure, 5.37 ns
from 16-word idle passes on one boot, lies inside the 4.9 to 5.6 ns first registered; scalar local
loads, X1's divide, X0 lane-memory loads and taken branches with their delay slots cost the same
within 1.5 ns (verified on the camera, in loop passes of 9 to 106 words closed by ops 0..2); idle
words in passes that jump 1088 words away and back by op 2 or op 3 likewise (no loop buffer and no
cache smaller than 1024 words; passes of exactly 2000 words 5.24 ns from four totals,
pre-registered); passes of 1536 and 1800 words (4.9 and 5.7 ns) and a 900-word straight run repeated
after 900 other words (at the repeat rate within the jitter) were measured outside their registered
windows, so those distances repeat at `REPEAT_I`; in passes of 107 to 1024 words and of 2000 only
idle words were timed, so other words there repeat at `REPEAT_I`; S3 reductions likewise (fitted)."""
REPEAT_I = Cost(REPEAT.us, I)
"""A word executed again outside the measured passes (a pass under 9 words, over 106 for a word that
is not idle, other than 9 to 1024 or exactly 2000 for an idle word, or with an op 4 since the word's
last run), or with an S unit's head: S3 reductions fitted at the idle word's cost, other S heads not
timed (inferred: no measured class costs more than an idle word)."""
PASSES = range(9, 107)
"""Executed words between two runs of a word in the loops timed on the camera, any word class."""
IDLE_PASSES = frozenset(range(9, 1025)) | {2000}
"""Likewise for idle words: passes of 9 to 1024 words and of exactly 2000."""
FIRST = Cost(0.064, I)
"""A word's first execution: 64 ns, the slope over straight-line jobs of 200 to 1000 idle words
(fitted; earlier runs of 0 to 1900 words gave 62 to 70 ns, all within a pre-registered 50 to 75 ns;
runs of 152 to 1900 words on five later boots 58 to 62 ns, a fit of two of them held, registered, on
a third); words of ALU ops cost the same as idle words (verified on the camera); paid when the word
runs, not for the image's length: a word branched over costs nothing unless it shares a fetch line
with a word that runs (`LINE`)."""
LINE = 8
"""Words per fetch line, aligned to image indices that are multiples of 8: the words of a
line that runs but that the job never executes cost `UNRUN` each (inferred: chains of
8-word blocks every 16 words cost nothing extra at offsets 0 and 8 and about 6.5 words per
block at offsets 4 and 12, pre-registered as one of the line sizes named; blocks every 10
and 12 words fit the line within a word per block; every 24 and 64 words they cost 2 to 5
words per block more than the lines predict, unexplained)."""
UNRUN = Cost(0.064, I)
"""A word never executed in a fetch line some word of which runs: the first-run rate (fitted: an
extra line measured 0.82 of eight first runs)."""


@dataclass(frozen=True)
class Rate:
    """A transfer's time: `per_command` plus `per_kib` per KiB of DRAM it moves, for commands
    of one row of `kib` KiB of DRAM (the sizes measured, inclusive). Measured as the pass time
    of loops whose pass hands one command and its 0x1d, fitted over the sizes; `per_command` is
    the fit's intercept less the pass's own words (about 0.06 us). `r1` and `r4`, the row pitches
    on the memory and the DRAM side, are the values every timed command ran with (0 in all of
    them); a command with other values has no time."""

    per_kib: float
    per_command: float
    kib: tuple[float, float]
    tier: Confidence = I
    r1: int = 0
    r4: int = 0


LANE_LOAD = Rate(0.442, 0.2, (1, 16), V)
"""Byte loads into the lane memory: 0.91, 1.72 and 7.38 us at 1, 4 and 16 KiB, fitted (about 2.2 KiB
per us); the line's times registered at 1 to 16 KiB on a later boot held within 10 % + 0.15 us at 1,
2, 8 and 16 KiB (0.72, 1.13, 3.82, 6.75 us) but not at 4 KiB (1.49 us, below, its shorter loop 33 us
long; 1.75 and 1.89 us on two more boots, inside); registered again at 1, 2, 4, 8 and 16 KiB on one
further boot, all five held (0.72, 1.18, 1.79, 3.38, 6.44 us)."""
LANE_STORE = Rate(0.4872, 0.119, (0.5, 16), V)
"""Byte stores from the lane memory: a line fitted, weighted by relative error, over ten earlier
times (0.74, 2.14, 7.19 us at 1, 4, 16 KiB on one boot; 0.54, 1.04, 2.13, 4.19, 7.94 us at 1, 2, 4,
8, 16 KiB on a second; 4.07, 8.25 us at 8, 16 KiB on a third), then registered at 0.5 to 16 KiB on a
fourth boot, where all six sizes held within 10 % + 0.15 us (0.30, 0.59, 1.11, 2.02, 3.79, 8.07 us;
three repeats per pass count); the earlier line, 0.427 us per KiB + 0.3, missed there at 0.5 and 16
KiB."""
LANE_STORE12 = Rate(0.4208, 0.084, (1, 16), V)
"""12-bit stores from the lane memory (r3 = ceil(10 r6 / 32) vectors): 0.50, 1.77 and 6.82 us at 1,
4 and 16 KiB of DRAM, each within its registered window around the byte store's rate, fitted; the
fit's times registered at 2 and 8 KiB held within 10 % + 0.15 us (0.97, 3.29 us)."""
LANE_LOAD16 = Rate(0.4665, 0.139, (1, 16), V)
"""16-bit loads into the lane memory (r3 = r6 / 4 vectors): 0.71, 1.88 and 7.63 us at 1, 4 and 16
KiB of DRAM, each within its registered window around the byte load's rate, fitted; the fit's times
registered at 2 and 8 KiB held within 10 % + 0.15 us (1.13, 3.63 us)."""
LANE_STORE16 = Rate(0.4326, 0.055, (0.0625, 16), V)
"""16-bit stores from the lane memory (r3 = r6 / 4 vectors): 0.11, 0.20, 0.48, 1.68 and 7.00 us at
64 B, 192 B, 1, 4 and 16 KiB of DRAM, each within its registered window around the byte store's
rate, fitted; the fit's times registered at 512 B and 8 KiB held within 10 % + 0.15 us (0.26, 3.07
us)."""
LOCAL_LOAD32 = Rate(1.3293, 0.377, (0.25, 4))
"""32-bit loads into local memory at r2 = 1 (one element per word): 0.69, 1.39, 1.71 and 5.69 us at
0.25, 0.75, 1 and 4 KiB of DRAM, fitted; the 0.25 KiB time lies above its registered window (the
byte local load's rate per KiB, which holds at the other sizes); the fit's times registered at 0.5
and 2 KiB held within 10 % + 0.15 us (1.02, 3.04 us; inferred: the 0.25 KiB time missed its window)."""
LOCAL_LOAD = Rate(1.3499, 0.312, (0.25, 4))
"""Byte loads into local memory at r2 = 4: 0.67, 1.65, 2.99 and 5.72 us at 0.25, 1, 2 and 4 KiB of
DRAM, fitted (about 0.75 KiB per us, a third of the lane memory's rate, plus a per-command part that
puts 0.25 KiB above the per-KiB rate's window); the fit's times registered at 0.5 and 3 KiB held
within 10 % + 0.15 us (0.99, 4.24 us; inferred: the 0.25 KiB time missed its window)."""
RATES: dict[tuple[int, int, int, int], Rate] = {
    (LOAD, LANE_MEMORY, 0x20, 0x10): LANE_LOAD,
    (STORE, LANE_MEMORY, 0x20, 0x10): LANE_STORE,
    (LOAD, LANE_MEMORY, 0x20, 0x08): LANE_LOAD16,
    (STORE, LANE_MEMORY, 0x20, 0x08): LANE_STORE16,
    (STORE, LANE_MEMORY, 0x20, 0x0A): LANE_STORE12,
    (LOAD, LOCAL_MEMORY, 4, 0x10): LOCAL_LOAD,
    (LOAD, LOCAL_MEMORY, 1, 0x04): LOCAL_LOAD32,
}
"""(kind, r0, r2, r5): the transfers timed on the camera, each a command of one row (r7 = 1)
handed and joined by a 0x1d, with r3 the units its r5 r6 elements fill. Other shapes have no
time."""
QUEUE = 16
"""Commands handed and not yet waited for whose times are known: 16 ran with no 0x1d (verified);
past 19 the engine holds commands until a 0x1d frees room, up to its depth (`stream.DEPTH`);
how long held commands take was not timed."""
OVERLAP_TIER = I
"""The stream engine is one for all channels, its commands run one after another from their
hand-off (two 8 KiB loads on two channels took about as long as one 16 KiB load: fitted), and a
0x1d waits for what the words since its command did not hide (idle words between an 8 KiB load
and its 0x1d hid it: fitted). The waits are at most this tier and their commands' rates'
(`Rate.tier`)."""


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


def rate_of(event: Event) -> Rate:
    """The rate of the command `event` hands, by its shape (`RATES`); raises `Unmeasured`."""
    if None in event.regs:
        raise Unmeasured("a register not handed: the command moves nothing, not timed")
    r0, _, r2, _, _, r5, _, _ = event.regs
    found = RATES.get((event.kind, r0, r2, r5))
    if found is None:
        raise Unmeasured(
            f"a command of kind {event.kind} at r0 {r0}, r2 {r2}, r5 {r5}: not timed"
        )
    return found


def transfer_us(event: Event) -> float:
    """The time of the command `event` hands, from its registers; raises `Unmeasured`."""
    rate = rate_of(event)
    _, r1, r2, r3, r4, r5, r6, r7 = event.regs
    if r3 != -(-r5 * r6 // r2):
        raise Unmeasured(f"r3 {r3:#x} short of or past what r6 fills: not timed")
    if r7 != 1:
        raise Unmeasured(f"a command of {r7} rows: only one row was timed")
    if (r1, r4) != (rate.r1, rate.r4):
        raise Unmeasured(
            f"r1 {r1:#x}, r4 {r4:#x}: timed at r1 {rate.r1:#x}, r4 {rate.r4:#x} only"
        )
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
    The waits are also capped at the overlap model's tier and their commands' rates'.
    A flow with an unknown step (a lapse) or an engine off script has no estimate. Raises
    ValueError for a flow that did not halt."""
    if flow.stop not in (Stop.HALT, Stop.OPEN_END):
        raise ValueError(f"the job did not halt: {flow.why}")
    lapsed = next((s for s in flow.steps if s.unknown), None)
    if lapsed is not None:
        return Estimate(unknown=f"word {lapsed.index} is not covered: {lapsed.note}")
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
    wait_tier = OVERLAP_TIER
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
            or any(words[step.index][HEAD_IDS[u]] != _idle(u) for u in M_UNITS)
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
                    wait_tier = min(wait_tier, rate_of(e).tier)
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
        parts.append(Part("stream waits", waited, min(wait_tier, flow_tier)))
    return Estimate(tuple(parts))


def _idle(unit: str) -> int:
    return lookup(HEAD_IDS[unit]).idle
