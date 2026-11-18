"""`fenykl dev sample`: the memory sampler test program (sdk/tp/sampler/) run from the PC. The
config TOML, the control block of sampler.h, the ring fetch over READ and the CSV
(sdk/docs/devmon.md, Memory sampler)."""

import argparse
import csv
import re
import signal
import struct
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import sdk, tp
from .dev import TIMEOUT, Client, DevmonError, DevmonStatus, parse_keys

SOURCE = sdk.SDK / "tp" / "sampler" / "sampler.c"
SLOT = 0
MAGIC = 0x4D534B46
WORDS_MAX = 32
CTL_SIZE = 0x280
WORDS_OFF = 0x50
WORD = struct.Struct("<3I2H")  # addr, offset, mask, width, flags
DEREF = 1
TRIGGER = 2
REC_HEAD = 4  # seq, t (us since t_start), lost, bad
DURATION_MAX_S = 3600
MODES = {"periodic": 0, "change": 1}
WIDTHS = (8, 16, 32)
# magic, mode, period_us, duration_ms, nwords, cap
CONFIG = struct.Struct("<6I")
# stop, tail, state, head, drops, overruns, loop_us, loop_max_us, t_start, t_end, invalid
STATUS = struct.Struct("<2Ii8I")
STOP_OFF = 0x18
TAIL_OFF = 0x1C
RUNNING = 1
STATES = {
    -1: "no control block at arg",
    -2: "mode, word count, width, flags, ring or duration",
    -3: "address not aligned to its width",
    -4: "address in DSP local memory",
    -5: "period below the cost of one pass",
    -6: "control block or ring outside the slots after the program",
}
TIMER_MASK = 0x3FFFFFFF
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
# Polling the control block while nothing is new; the grace past the duration before stop.
POLL_S = 0.02
GRACE_S = 5.0
START_S = 2.0
# Status reads in a row that give no record: READ copies the uncached ring as it changes, so one
# read can be torn; more in a row are not.
STALE_MAX = 3


class ConfigError(DevmonError):
    pass


@dataclass(frozen=True)
class Word:
    """A sampled word: the value at addr, or with ptr at (u32 at ptr) + offset. A trigger word's
    (value & mask) changing makes a record in change mode."""

    name: str
    addr: int
    width: int = 32
    ptr: bool = False
    offset: int = 0
    trigger: bool = False
    mask: int = 0


@dataclass(frozen=True)
class Config:
    mode: str
    period_us: int
    words: tuple[Word, ...]
    duration_s: float | None = None


@dataclass(frozen=True)
class Status:
    stop: int
    tail: int
    state: int
    head: int
    drops: int
    overruns: int
    loop_us: int
    loop_max_us: int
    t_start: int
    t_end: int
    invalid: int


@dataclass(frozen=True)
class Record:
    seq: int
    t: int
    lost: int
    bad: int
    values: tuple[int, ...]


def uncached(addr: int) -> int:
    """The uncached alias of a DRAM address (FENYKL_UNCACHED)."""
    return addr & 0x3FFFFFFF | 0x80000000


def check_word(addr: int, width: int, what: str) -> None:
    """The refusals the sampler also makes: width, alignment and DSP local memory."""
    if width not in WIDTHS:
        raise ConfigError(f"{what}: width {width}, not one of 8, 16, 32")
    if not 0 <= addr <= 0xFFFFFFFF:
        raise ConfigError(f"{what}: address 0x{addr:x} is not 32-bit")
    if addr % (width // 8):
        raise ConfigError(f"{what}: 0x{addr:08x} is not aligned to {width} bits")
    # 0x9D000000..0x9DFFFFFF and the bases that differ only in the top two address bits.
    if addr & 0x3F000000 == 0x1D000000:
        raise ConfigError(
            f"{what}: 0x{addr:08x} is DSP local memory: a CPU0 read outside a DSP job hangs "
            "the bus"
        )


def _keys(table: dict, allowed: set[str], what: str) -> None:
    extra = set(table) - allowed
    if extra:
        raise ConfigError(f"{what}: unknown keys {', '.join(sorted(extra))}")


def _int(table: dict, key: str, what: str, default: int | None = None) -> int:
    v = table.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool):
        raise ConfigError(f"{what}: {key} must be an integer")
    return v


def _word(w: dict, what: str, mode: str) -> Word:
    _keys(w, {"name", "addr", "ptr", "offset", "width", "trigger", "mask"}, what)
    name = w.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConfigError(f"{what}: name {name!r} must match {NAME.pattern}")
    what = f"{what} {name}"
    if ("addr" in w) == ("ptr" in w):
        raise ConfigError(f"{what}: give addr or ptr (one level of pointer)")
    if "offset" in w and "ptr" not in w:
        raise ConfigError(f"{what}: offset needs ptr")
    width = _int(w, "width", what, 32)
    ptr = "ptr" in w
    addr = _int(w, "ptr" if ptr else "addr", what)
    # the pointer itself is a u32; the value's alignment is checked on the camera, per pass
    check_word(addr, 32 if ptr else width, what)
    if width not in WIDTHS:
        raise ConfigError(f"{what}: width {width}, not one of 8, 16, 32")
    offset = _int(w, "offset", what, 0)
    if not -0x80000000 <= offset <= 0xFFFFFFFF:
        raise ConfigError(f"{what}: offset 0x{offset:x} is not 32-bit")
    trigger = w.get("trigger", False)
    if not isinstance(trigger, bool):
        raise ConfigError(f"{what}: trigger must be true or false")
    if trigger and mode != "change":
        raise ConfigError(f"{what}: trigger words need mode change")
    full = (1 << width) - 1
    mask = _int(w, "mask", what, full) if trigger else 0
    if "mask" in w and not trigger:
        raise ConfigError(f"{what}: mask needs trigger = true")
    if trigger and not 0 < mask <= full:
        raise ConfigError(f"{what}: mask must be 1..0x{full:x}")
    return Word(name, addr, width, ptr, offset & 0xFFFFFFFF, trigger, mask)


def parse_config(data: dict) -> Config:
    _keys(data, {"mode", "period_us", "duration_s", "trigger", "word"}, "config")
    mode = data.get("mode", "periodic")
    if mode not in MODES:
        raise ConfigError(f"mode {mode!r}: periodic or change")
    period = _int(data, "period_us", "config")
    if not 1 <= period < 0x20000000:
        raise ConfigError(f"period_us {period} outside 1..0x1fffffff")
    duration = data.get("duration_s")
    if duration is not None and (
        not isinstance(duration, int | float) or isinstance(duration, bool)
    ):
        raise ConfigError("duration_s must be a number")
    words = tuple(
        _word(w, f"word {i + 1}", mode) for i, w in enumerate(data.get("word", []))
    )
    trig = data.get("trigger")
    if trig is not None:
        # [trigger] {addr or ptr, offset, width, mask}: one more word, a trigger, column "trigger"
        if not isinstance(trig, dict) or mode != "change":
            raise ConfigError("[trigger] is a table and needs mode change")
        words += (_word({"name": "trigger", **trig, "trigger": True}, "trigger", mode),)
    if not 1 <= len(words) <= WORDS_MAX:
        raise ConfigError(f"{len(words)} [[word]] entries: 1..{WORDS_MAX}")
    names = [w.name for w in words]
    if len(set(names)) != len(names) or {"timestamp_us", "lost"} & set(names):
        raise ConfigError(
            "word names must be unique and not timestamp_us or lost (CSV columns)"
        )
    if mode == "change" and not any(w.trigger for w in words):
        raise ConfigError("mode change needs at least one word with trigger = true")
    return Config(mode, period, words, duration)


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_config(data)


def rec_bytes(nwords: int) -> int:
    return 4 * (REC_HEAD + nwords)


def ring_layout(target: sdk.SdkTarget, slot: int, nwords: int) -> tuple[int, int]:
    """The control block's cached address and the ring's capacity in records: the slots after the
    sampler's, up to the job buffer that follows the last slot."""
    region = target.tp
    if region is None:
        raise ConfigError(f"target {target.name} has no exec region")
    if slot + 1 >= region.slots:
        raise ConfigError(f"slot {slot}: no slot after it for the ring")
    ctl = region.slot(slot + 1)
    end = region.base + region.slots * region.slot_size
    return ctl, (end - ctl - CTL_SIZE) // rec_bytes(nwords)


def ctl_bytes(cfg: Config, duration_ms: int, cap: int) -> bytes:
    """The control block as the host writes it before the run: config, the rest zero."""
    block = bytearray(CTL_SIZE)
    CONFIG.pack_into(
        block,
        0,
        MAGIC,
        MODES[cfg.mode],
        cfg.period_us,
        duration_ms,
        len(cfg.words),
        cap,
    )
    for i, w in enumerate(cfg.words):
        flags = (DEREF if w.ptr else 0) | (TRIGGER if w.trigger else 0)
        WORD.pack_into(
            block, WORDS_OFF + WORD.size * i, w.addr, w.offset, w.mask, w.width, flags
        )
    return bytes(block)


def decode(data: bytes, nwords: int) -> list[Record]:
    size = rec_bytes(nwords)
    if len(data) % size:
        raise DevmonError(f"{len(data)} ring bytes: not whole records of {size}")
    fmt = struct.Struct(f"<{REC_HEAD + nwords}I")
    return [
        Record(r[0], r[1], r[2], r[3], r[REC_HEAD:])
        for r in (fmt.unpack_from(data, off) for off in range(0, len(data), size))
    ]


def csv_row(r: Record, words: tuple[Word, ...]) -> list:
    """timestamp_us (since the run's start), lost, then each value in hex; a value whose pointer
    led outside RAM is "bad:" and the pointer."""
    cells = [
        f"bad:0x{v:08x}" if r.bad >> i & 1 else f"0x{v:0{w.width // 4}x}"
        for i, (v, w) in enumerate(zip(r.values, words))
    ]
    return [r.t, r.lost, *cells]


@dataclass(frozen=True)
class Result:
    records: int
    status: Status


def _status(client: Client, ctl: int) -> Status:
    return Status(*STATUS.unpack(client.read(ctl + STOP_OFF, STATUS.size)))


def _fetch(
    client: Client, ring: int, cap: int, nwords: int, first: int, n: int
) -> bytes:
    """Records first..first+n (counted from the start of the run) of the ring, in order."""
    size = rec_bytes(nwords)
    pos = first % cap
    k = min(n, cap - pos)
    data = client.read(ring + pos * size, k * size)
    if n > k:
        data += client.read(ring, (n - k) * size)
    return data


def _stop(client: Client, ctl: int, say) -> None:
    """Set the stop word, best effort: on an error path the connection may be gone."""
    try:
        client.write(ctl + STOP_OFF, struct.pack("<I", 1))
    except (DevmonError, OSError) as e:
        say(
            f"could not set the sampler's stop word ({e}): it runs to the end of its duration"
        )


def _started(client: Client, ctl: int) -> Status:
    """The status once the program has left state 0, or after START_S."""
    st = _status(client, ctl)
    t0 = time.monotonic()
    while st.state == 0 and time.monotonic() - t0 < START_S:
        time.sleep(POLL_S)
        st = _status(client, ctl)
    return st


def sample(
    client: Client,
    cfg: Config,
    image: bytes,
    target: sdk.SdkTarget,
    duration_s: float,
    out: Path,
    keys: str = "",
    say=print,
) -> Result:
    """Load the sampler, write its control block, start it detached (TPRUN with a 1 ms timeout:
    the timeout reply frees the server while the program keeps running), then fetch the ring
    into `out` until the program ends. Keys of `keys` (dev.parse_keys) are pressed meanwhile on
    the same connection; Ctrl-C sets the stop word, and so does any error once TPRUN went out."""
    if not 0 < duration_s <= DURATION_MAX_S:
        raise ConfigError(f"duration {duration_s:g} s outside (0, {DURATION_MAX_S}]")
    duration_ms = round(duration_s * 1000)
    if duration_ms < 1:
        raise ConfigError(f"duration {duration_s:g} s is under 1 ms")
    steps = parse_keys(keys)
    slot = tp.slot_of(target, tp.check(image))
    base, cap = ring_layout(target, slot, len(cfg.words))
    ctl = uncached(base)
    ring = ctl + CTL_SIZE
    nwords = len(cfg.words)
    interrupted = False

    def on_sigint(signum, frame) -> None:
        nonlocal interrupted
        interrupted = True

    old = signal.signal(signal.SIGINT, on_sigint)
    launched = False
    st: Status | None = None
    try:
        client.load(image, slot)
        # Through the cached alias: CPU0 writes it and writes its D-cache back, so no stale CPU0
        # line can later overwrite it; the program then uses only the uncached alias.
        client.write(base, ctl_bytes(cfg, duration_ms, cap))
        launched = True
        try:
            result, _ = client.run(slot, ctl, 1)
            say(f"sampler returned {result} at once")
        except DevmonStatus as e:
            if e.status != TIMEOUT:
                raise
        st = _started(client, ctl)
        if st.state < 0:
            detail = f" (one pass takes {st.loop_us} us)" if st.loop_us else ""
            raise DevmonError(
                f"sampler refused the config: {STATES.get(st.state, st.state)}{detail}"
            )
        if st.state == 0:
            raise DevmonError("the sampler did not start: see fenykl dev log")
        say(f"sampling, one pass {st.loop_us} us, ring of {cap} records")
        started = time.monotonic()
        stop_at = started + duration_s + GRACE_S
        stopping = False
        next_step = started
        taken = 0
        stale = 0
        with out.open("w", newline="") as f:
            rows = csv.writer(f)
            rows.writerow(["timestamp_us", "lost", *(w.name for w in cfg.words)])
            while True:
                st = _status(client, ctl)
                n = st.head - taken
                good = 0
                if 0 < n <= cap:
                    data = _fetch(client, ring, cap, nwords, taken, n)
                    # a record is whole when its seq is the index we expect (written last)
                    for r in decode(data, nwords):
                        if r.seq != taken + good:
                            break
                        rows.writerow(csv_row(r, cfg.words))
                        good += 1
                if good:
                    taken += good
                    client.write(ctl + TAIL_OFF, struct.pack("<I", taken))
                if n and not good:
                    stale += 1
                    if stale >= STALE_MAX:
                        raise DevmonError(
                            f"ring state: head {st.head}, {taken} taken, capacity {cap}"
                        )
                else:
                    stale = 0
                if st.state != RUNNING and taken == st.head:
                    break
                now = time.monotonic()
                if steps and not stopping and now >= next_step:
                    step = steps.pop(0)
                    if isinstance(step, int):
                        next_step = now + step / 1000
                    else:
                        say(f"+{now - started:.3f} s key {step[0]} {step[1]} ms")
                        client.key(*step)
                if not stopping and (interrupted or now >= stop_at):
                    say("stopping the sampler")
                    client.write(ctl + STOP_OFF, struct.pack("<I", 1))
                    stopping = True
                    stop_at = now + GRACE_S
                elif stopping and now >= stop_at:
                    raise DevmonError(
                        "the sampler did not stop: it keeps running until it returns "
                        "(PING exec_busy), power-cycle the camera"
                    )
                if not good:
                    time.sleep(POLL_S)
    finally:
        if launched and (st is None or st.state in (0, RUNNING)):
            _stop(client, ctl, say)
        signal.signal(signal.SIGINT, old)
    if st.state < 0:
        raise DevmonError(f"sampler ended with state {st.state}")
    if steps:
        say(f"the run ended before {len(steps)} key steps")
    return Result(taken, st)


def cmd_sample(args: argparse.Namespace, c: Client) -> None:
    cfg = load_config(args.config)
    duration = args.duration if args.duration is not None else cfg.duration_s
    if duration is None:
        raise ConfigError("no duration: give --duration or duration_s in the config")
    target = sdk.target(args.target)
    if args.image:
        image = args.image.read_bytes()
    else:
        with tempfile.TemporaryDirectory() as tmp:
            image = tp.build([SOURCE], Path(tmp), target, SLOT).read_bytes()
    r = sample(c, cfg, image, target, duration, args.output, args.keys)
    st = r.status
    print(
        f"{r.records} records to {args.output}: drops {st.drops}, overruns {st.overruns}, "
        f"invalid pointers {st.invalid}, pass {st.loop_us} us (longest {st.loop_max_us} us), "
        f"{((st.t_end - st.t_start) & TIMER_MASK) / 1e6:.3f} s on the camera"
    )
    if st.drops or st.overruns:
        print(
            f"warning: {st.drops} records dropped (ring full), {st.overruns} periods missed: "
            "see the lost column",
            file=sys.stderr,
        )


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sample",
        help="sample memory words on the camera into a CSV (sdk/tp/sampler)",
    )
    p.set_defaults(dev_func=cmd_sample)
    p.add_argument("config", type=Path, help="sampler config TOML")
    p.add_argument(
        "--duration", type=float, help="seconds (overrides duration_s of the config)"
    )
    p.add_argument("-o", "--output", type=Path, required=True, help="CSV file")
    p.add_argument(
        "--keys", default="", help='key sequence during the run, as for "dev keys"'
    )
    p.add_argument(
        "--image",
        type=Path,
        help="a prebuilt sampler (fenykl tp build); default: build it now",
    )
    p.add_argument("-t", "--target", default="em10ii")
