#!/usr/bin/env python3
"""Per-shot table from an lvlock probe log (C:\\FENYKL.LOG).

Takes the last boot in the file that ran lvlock. A shot starts at its FULL press; its frames are the
take handles that turned busy (ps 0xFF) before the next FULL press; it ends when the last of them has
its file result (ps != 0xFF). Columns: mode of the resize task, frames, press -> last developed (fr 1)
and press -> last file result in ms, stock engine-1 jobs (count, IPU ms), stock jobs that found the
lock taken (count, ms waited behind our job), and from the probe's capture window (FULL press or busy
handle .. no busy handle) our resize jobs run / skipped (busy, gate), the longest one and how many
were logged as slow (> 3 ms or lock taken). Shots that share a window show its counts once. fps: burst
rate from the times the handles turned busy.

    python3 lvlock_log.py FENYKL.LOG [--timeline]
"""

import argparse
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("OFF", "WAIT", "SKIP", "GATED")
MODE_LAG_S = 0.5

LINE = re.compile(r"^\[\s*(\d+)\.(\d+)\] (\w) lvlock\s*: (.*)$")


@dataclass
class Job:
    end: float  # s since log init
    dt_us: int
    mode: int
    eng2: bool
    task: int
    status: int
    ra: int
    flag: int
    lock: int | None = None
    wait_us: int = 0

    @property
    def start(self) -> float:
        return self.end - self.dt_us / 1e6


@dataclass
class Ours:
    end: float
    dt_us: int
    lock: int
    status: int


@dataclass
class Handle:
    busy: float
    developed: float | None = None
    filed: float | None = None
    result: int | None = None


@dataclass
class Shot:
    index: int
    press: float
    mode: int
    handles: list[Handle] = field(default_factory=list)

    @property
    def block(self) -> str:
        return "burst" if len(self.handles) > 1 else "single"

    @property
    def end(self) -> float | None:
        if not self.handles or any(h.filed is None for h in self.handles):
            return None
        return max(h.filed for h in self.handles)


@dataclass
class Window:
    start: float
    end: float | None = None
    counts: tuple[int, int, int, int] | None = (
        None  # run, skip busy, skip gate, longest us
    )


@dataclass
class Log:
    shots: list[Shot] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    ours: list[Ours] = field(default_factory=list)
    windows: list[Window] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    modes: list[tuple[float, int]] = field(default_factory=list)


def last_boot(text: str) -> list[str]:
    boots = re.split(r"^.*fenykl log boot:.*$", text, flags=re.MULTILINE)
    runs = [b for b in boots if "lvlock start" in b]
    if not runs:
        raise SystemExit("no lvlock run in this log")
    return runs[-1].splitlines()


def parse(lines: list[str]) -> Log:
    log = Log()
    open_handles: dict[int, Handle] = {}
    for ln in lines:
        m = LINE.match(ln.strip())
        if not m:
            continue
        t = int(m.group(1)) + int(m.group(2)) / 1e6
        f = m.group(4).split()
        tag = f[0]
        if tag == "FULL":
            log.shots.append(Shot(int(f[1]), t, int(f[3])))
        elif tag == "MODE":
            log.modes.append((t, MODES.index(f[1])))
        elif tag == "J":
            packed = int(f[2], 16)
            log.jobs.append(
                Job(
                    t,
                    int(f[1]),
                    packed >> 24,
                    bool(packed >> 23 & 1),
                    packed >> 16 & 0x7F,
                    packed & 0xFFFF,
                    int(f[3], 16),
                    int(f[4], 16),
                )
            )
        elif tag == "W" and log.jobs:
            log.jobs[-1].lock = int(f[2], 16)
            log.jobs[-1].wait_us = int(f[6])
        elif tag == "R":
            log.ours.append(Ours(t, int(f[1]), int(f[3], 16), int(f[5], 16)))
        elif tag == "CAP" and f[1] == "1":
            log.windows.append(Window(t))
        elif tag == "CAP" and log.windows:
            log.windows[-1].end = t
        elif tag == "SHOT" and log.windows:
            log.windows[-1].counts = (int(f[2]), int(f[4]), int(f[6]), int(f[8]))
        elif tag == "TH":
            th, fr, ps = int(f[1]), int(f[3], 16), int(f[5], 16)
            h = open_handles.get(th)
            if ps == 0xFF and h is None:
                h = Handle(t)
                open_handles[th] = h
                if log.shots:
                    log.shots[-1].handles.append(h)
            if h is None:
                continue
            if fr == 1 and h.developed is None:
                h.developed = t
            if ps != 0xFF:
                h.filed, h.result = t, ps
                del open_handles[th]
        if tag in ("SUM", "MODE", "E", "lvlock") or m.group(3) in "EW":
            log.other.append(ln.strip())
    # The mode the resize task applied: its MODE line follows the press within a tick or two.
    for s in log.shots:
        applied = [mode for t, mode in log.modes if t <= s.press + MODE_LAG_S]
        if applied:
            s.mode = applied[-1]
    return log


def ms(a: float | None, b: float) -> str:
    return "-" if a is None else f"{(a - b) * 1e3:.0f}"


def fps(s: Shot) -> str:
    span = s.handles[-1].busy - s.handles[0].busy if len(s.handles) > 1 else 0
    return f"{(len(s.handles) - 1) / span:4.1f}" if span > 0 else "   -"


def window_of(log: Log, s: Shot) -> Window | None:
    for w in log.windows:
        if w.start - 0.05 <= s.press and (w.end is None or s.press <= w.end):
            return w
    return None


def row(
    log: Log, s: Shot, nxt: Shot | None, seen: set[int]
) -> tuple[str, float | None]:
    end = s.end
    stop = end if end is not None else (nxt.press if nxt else float("inf"))
    e1 = [j for j in log.jobs if not j.eng2 and s.press <= j.start <= stop]
    waited = [j for j in e1 if j.lock is not None]
    slow = [o for o in log.ours if s.press <= o.end <= stop]
    w = window_of(log, s)
    if w is None or w.counts is None:
        counts = "   -    -    -      -"
    elif id(w) in seen:
        counts = "   ^    ^    ^      ^"
    else:
        seen.add(id(w))
        run, skb, skg, rmax = w.counts
        counts = f"{run:4d} {skb:4d} {skg:4d} {rmax / 1e3:6.1f}"
    dev = max((h.developed for h in s.handles if h.developed is not None), default=None)
    block = s.block
    t_file = None if end is None else (end - s.press) * 1e3
    text = (
        f"{s.index:3d} {block:6s} {MODES[s.mode]:5s} {len(s.handles):3d} {ms(dev, s.press):>6s} "
        f"{ms(end, s.press):>6s} {len(e1):4d} {sum(j.dt_us for j in e1) / 1e3:7.1f} "
        f"{len(waited):3d} {sum(j.wait_us for j in waited) / 1e3:6.1f} {counts} {len(slow):4d} "
        f"{fps(s)}"
    )
    return text, t_file


def timeline(log: Log, s: Shot, stop: float) -> None:
    for j in log.jobs:
        if not s.press <= j.start <= stop:
            continue
        wait = f" lock {j.lock:x} wait {j.wait_us}" if j.lock is not None else ""
        print(
            f"      +{(j.start - s.press) * 1e3:7.1f} ms {j.dt_us:6d} us mode {j.mode:02x} "
            f"e{2 if j.eng2 else 1} task {j.task:02x} ra {j.ra:08x} flag {j.flag:08x} "
            f"st {j.status:x}{wait}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("log", type=Path)
    ap.add_argument("--timeline", action="store_true", help="list each shot's IPU jobs")
    args = ap.parse_args()
    log = parse(last_boot(args.log.read_text(errors="replace")))

    print(
        "  # block  mode  frm  dev_ms file_ms  e1j  e1_ms  wj wait_ms  run skpb skpg rmax_ms slow  fps"
    )
    per_mode: dict[tuple[str, int], list[float]] = {}
    seen: set[int] = set()
    for i, s in enumerate(log.shots):
        nxt = log.shots[i + 1] if i + 1 < len(log.shots) else None
        text, t_file = row(log, s, nxt, seen)
        print(text)
        if args.timeline:
            stop = s.end if s.end is not None else (nxt.press if nxt else float("inf"))
            timeline(log, s, stop)
        if t_file is not None:
            per_mode.setdefault((s.block, s.mode), []).append(t_file)
    print()
    print("block  mode  shots  file_ms mean / max")
    for (block, mode), v in sorted(per_mode.items()):
        print(
            f"{block:6s} {MODES[mode]:5s} {len(v):5d}  {statistics.mean(v):8.0f} / {max(v):.0f}"
        )
    print()
    for ln in log.other:
        print(ln)


if __name__ == "__main__":
    main()
