#!/usr/bin/env python3
"""Summarises a trklog FENYKL.LOG for the open C-AF+TR tracking questions (stdlib only).

Usage: python3 trklog_report.py FENYKL.LOG

Prints: whether the live AF window moved during the C-AF+TR FtAF loop (com 0x1202), the 0x1200
packets per FtAF peak (phase 0x1500), the tracker update interval and the AF position -> FCS window
lag, and the lost episodes. Times are the log stamps (10 ms poll).
"""

import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

LINE_RE = re.compile(r"^\[\s*([\d.]+)\] \w trklog\s*: (.*)$")
FTAF_COM = 0x1202
EVAL_COMS = (0x1100, 0x1200)
PEAK_PHASE = 0x1500


@dataclass
class Report:
    com: int = 0
    phase: int = 0
    active: bool = False
    af: int | None = None
    af_t: float = 0.0
    win: int | None = None
    lost: bool = False
    lost_t: float = 0.0
    lost_idle: float = 0.0
    pending_lag: list[tuple[float, int]] = field(default_factory=list)
    live_moves: list[str] = field(default_factory=list)
    live_ftaf: int = 0
    live_eval: int = 0
    live_other: int = 0
    win_ftaf: int = 0
    peaks: list[list[int]] = field(default_factory=list)
    intervals: list[float] = field(default_factory=list)
    lags: list[float] = field(default_factory=list)
    lost_runs: list[tuple[float, float, float]] = field(default_factory=list)
    live: tuple[int, int] | None = None

    def boot(self) -> None:
        self.com = self.phase = 0
        self.active = self.lost = False
        self.af = self.win = self.live = None
        self.pending_lag.clear()

    def packet(self, com: int, cont: int) -> None:
        if self.active and com == 0x1200 and self.com != 0x1200 and self.peaks:
            self.peaks[-1].append(cont)
        self.com = com

    def ftaf(self, phase: int) -> None:
        if self.active and phase == PEAK_PHASE and self.phase != PEAK_PHASE:
            self.peaks.append([])
        self.phase = phase

    def pos(self, t: float, af: int, win: int) -> None:
        if af != self.af:
            if self.af is not None and self.active:
                self.intervals.append(t - self.af_t)
            self.af, self.af_t = af, t
            self.pending_lag.append((t, af))
        if win != self.win and self.win is not None and self.com == FTAF_COM:
            self.win_ftaf += 1
        self.win = win
        matched = [p for p in self.pending_lag if p[1] == win]
        if matched:
            self.lags.append(t - matched[0][0])
            self.pending_lag = [p for p in self.pending_lag if p[0] > matched[0][0]]
        self.pending_lag = [p for p in self.pending_lag if t - p[0] < 1.0]

    def trk(self, t: float, lost: bool) -> None:
        if lost and not self.lost:
            self.lost_t, self.lost_idle = t, t - self.af_t
        if not lost and self.lost:
            self.lost_runs.append((self.lost_t, t - self.lost_t, self.lost_idle))
        self.lost = lost

    def live_window(self, t: float, pos: int, size: int) -> None:
        live = (pos, size)
        if self.live is None or live == self.live:
            self.live = live
            return
        self.live = live
        if self.com == FTAF_COM:
            self.live_ftaf += 1
        elif self.com in EVAL_COMS:
            self.live_eval += 1
        else:
            self.live_other += 1
        win = f"{self.win:08x}" if self.win is not None else "-"
        self.live_moves.append(
            f"  {t:11.6f} com {self.com:04x} phase {self.phase:04x} fcs win {win} "
            f"live hs|vs {pos:08x} hw|vw {size:08x}")

    def line(self, t: float, text: str) -> None:
        w = text.split()
        if text.startswith("trklog:"):
            self.boot()
        elif w[0] == "act":
            self.active = w[1] == "1"
            self.af = self.win = None
            self.pending_lag.clear()
        elif w[0] == "k":
            self.packet(int(w[2], 16), int(w[6]))
        elif w[0] == "f":
            self.ftaf(int(w[1], 16) >> 16)
        elif w[0] == "p" and self.active:
            self.pos(t, int(w[2], 16), int(w[4], 16))
        elif w[0] == "t" and self.active:
            self.trk(t, int(w[3], 16) >> 8 != 0)
        elif w[0] == "al" and self.active:
            self.live_window(t, int(w[2], 16), int(w[3], 16))


def ms(values: list[float]) -> str:
    if not values:
        return "no data"
    v = sorted(values)
    return (f"median {statistics.median(v) * 1000:.0f} ms, p90 {v[int(0.9 * (len(v) - 1))] * 1000:.0f} ms, "
            f"n {len(v)}")


def verdict(r: Report) -> str:
    if r.live_ftaf and r.win_ftaf:
        return "YES: the live AF window moved during the FtAF loop"
    if r.win_ftaf >= 10 and not r.live_ftaf:
        if r.live_eval:
            return "NO per frame: the window moves only at evaluation starts (0x1100/0x1200)"
        return "NO: the FCS window moved in FtAF, the live AF window never did"
    return "inconclusive: too few FCS window moves during 0x1202"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    r = Report()
    for raw in Path(sys.argv[1]).read_text(errors="replace").splitlines():
        m = LINE_RE.match(raw)
        if m:
            r.line(float(m.group(1)), m.group(2))

    print("1. AF window follows the tracker in FtAF (com 0x1202)?")
    print(f"  FCS window moves in 0x1202: {r.win_ftaf}; live window moves: in 0x1202 {r.live_ftaf}, "
          f"at 0x1100/0x1200 {r.live_eval}, other {r.live_other}")
    print(f"  -> {verdict(r)}")
    print("\n".join(r.live_moves[:40]))
    if len(r.live_moves) > 40:
        print(f"  ... {len(r.live_moves) - 40} more")

    print("2. 0x1200 packets per FtAF peak (phase 0x1500)")
    counts: dict[int, int] = {}
    for p in r.peaks:
        counts[len(p)] = counts.get(len(p), 0) + 1
    cont = [c for p in r.peaks for c in p]
    print(f"  peaks {len(r.peaks)}; reissues per peak {dict(sorted(counts.items()))}; "
          f"with area_continue {sum(1 for c in cont if c)} of {len(cont)}")

    print("3. Tracker timing")
    print(f"  AF position update interval: {ms(r.intervals)} (upper bound on the frame period)")
    print(f"  AF position -> FCS window: {ms(r.lags)}")

    print("4. Lost episodes (start s, duration ms, AF position frozen before lost ms)")
    for start, dur, idle in r.lost_runs:
        print(f"  {start:11.6f} {dur * 1000:6.0f} {idle * 1000:6.0f}")
    if r.intervals:
        period = statistics.median(r.intervals)
        frames = [idle / period for _, _, idle in r.lost_runs]
        if frames:
            print(f"  frozen before lost: median {statistics.median(frames):.1f} frames "
                  f"(frame = {period * 1000:.0f} ms)")


if __name__ == "__main__":
    main()
