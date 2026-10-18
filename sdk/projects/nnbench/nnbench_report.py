#!/usr/bin/env python3
"""Turns the FENYKL.LOGs of nnbench and nnbench2 into one markdown cost table and a CSV (stdlib
only).

Usage: python3 nnbench_report.py LOG [LOG ...] [--out PREFIX]   (writes PREFIX.md and PREFIX.csv;
default: nnbench next to the first log)

Reads the last run of each plugin in the logs (the R, I and clock lines, nnbench.c). Cycles per
call are the camera's (the CPU1 clock measured by that run's calib stage); per-op costs divide them
by the row's op counts. The tables are per kernel family, the rows of a case from both runs next to
each other (column run). Per family, variant and mode with several sizes, a least-squares fit
cycles = a + b * ops gives the fixed cost a and the cost per op b.
"""

import argparse
import csv
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

LINE_RE = re.compile(r"^\[\s*([\d.]+)\] (\w) nnb\s*: (.*)$")
RUN_START = re.compile(r"^(nnbench\d*), load")
FIXED = (
    "st",
    "k",
    "v",
    "m",
    "it",
    "reps",
    "us",
    "cyc",
    "ops",
    "ops2",
    "code",
    "chk",
    "bad",
    "ci",
    "err",
    "frame",
    "hash",
    "tries",
)
ISA_PAIR = 2  # enum nb_isa_kind
ISA_EMPTY = 0
ISA_PROBE = 3


@dataclass
class Row:
    stage: str
    kernel: str
    variant: str
    params: dict[str, int]
    mode: str = ""
    iters: int = 0
    reps: int = 0
    us_min: int = 0
    us_med: int = 0
    cyc_min: int = 0
    cyc_med: int = 0
    ops: int = 0
    ops_unit: str = ""
    ops2: int = 0
    ops2_unit: str = ""
    code: int = 0
    check: str = ""
    bad: int = 0
    err: str = ""
    run: str = ""
    live: str = ""  # live modes: frame slot, hash, checks tried

    def per_op(self, n: int) -> float | None:
        return self.cyc_min / n if n and not self.err else None


@dataclass
class IsaRow:
    core: int
    stage: str
    kind: int
    us_min: int
    ops: int
    got: str
    want: str
    mdr: str
    hi: str
    check: str
    name: str


@dataclass
class Run:
    plugin: str = ""
    rows: list[Row] = field(default_factory=list)
    isa: list[IsaRow] = field(default_factory=list)
    khz: dict[int, int] = field(default_factory=dict)
    done: str = ""
    errors: list[str] = field(default_factory=list)


def parse_r(text: str) -> Row:
    head, _, err = text.partition(" err=")
    tokens = head.split()[1:]
    kv: dict[str, str] = {}
    units: dict[str, str] = {}
    last = ""
    for tok in tokens:
        if "=" in tok:
            last, _, val = tok.partition("=")
            kv[last] = val
        else:
            units[last] = tok
    us = kv.get("us", "0/0").split("/")
    cyc = kv.get("cyc", "0/0").split("/")
    return Row(
        stage=kv["st"],
        kernel=kv["k"],
        variant=kv["v"],
        params={k: int(v) for k, v in kv.items() if k not in FIXED},
        mode=kv.get("m", ""),
        iters=int(kv.get("it", 0)),
        reps=int(kv.get("reps", 0)),
        us_min=int(us[0]),
        us_med=int(us[1]),
        cyc_min=int(cyc[0]),
        cyc_med=int(cyc[1]),
        ops=int(kv.get("ops", 0)),
        ops_unit=units.get("ops", ""),
        ops2=int(kv.get("ops2", 0)),
        ops2_unit=units.get("ops2", ""),
        code=int(kv.get("code", 0)),
        check=kv.get("chk", ""),
        bad=int(kv.get("bad", 0)),
        err=err,
        live=" ".join(f"{k}={kv[k]}" for k in ("frame", "hash", "tries") if k in kv),
    )


def parse_i(text: str) -> IsaRow:
    head, _, name = text.partition(" name=")
    kv = dict(tok.partition("=")[::2] for tok in head.split()[1:])
    return IsaRow(
        core=int(kv["c"]),
        stage=kv["st"],
        kind=int(kv["kind"]),
        us_min=int(kv["us"].split("/")[0]),
        ops=int(kv["ops"]),
        got=kv["got"],
        want=kv["want"],
        mdr=kv["mdr"],
        hi=kv["hi"],
        check=kv["chk"],
        name=name,
    )


def parse(path: Path) -> dict[str, Run]:
    """The last run of each plugin in the log."""
    runs: dict[str, Run] = {}
    run = Run()
    for line in path.read_text(errors="replace").splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        level, text = m.group(2), m.group(3)
        start = RUN_START.match(text)
        if start:
            run = runs[start.group(1)] = Run(plugin=start.group(1))
        elif text.startswith("R "):
            run.rows.append(parse_r(text))
            run.rows[-1].run = run.plugin
        elif text.startswith("I "):
            run.isa.append(parse_i(text))
        elif text.startswith("clock "):
            kv = dict(tok.partition("=")[::2] for tok in text.split()[1:3])
            run.khz[int(kv["c"])] = int(kv["khz"])
        elif text.startswith("nnbench done"):
            run.done = text
        elif level == "E":
            run.errors.append(text)
    return runs


def fit(points: list[tuple[int, int]]) -> tuple[float, float] | None:
    """Least squares cycles = a + b * ops; None without two distinct sizes."""
    xs = [p[0] for p in points]
    if len(set(xs)) < 2:
        return None
    b, a = statistics.linear_regression(xs, [p[1] for p in points])
    return a, b


def fmt(v: float | None, digits: int = 3) -> str:
    return "" if v is None else f"{v:.{digits}f}"


def param_text(r: Row) -> str:
    return " ".join(f"{k}={v}" for k, v in r.params.items())


def isa_cycles(run: Run, r: IsaRow, empty: dict[int, tuple[int, int]]) -> str:
    """macbench's cycles per op: the empty block loop subtracted, the mov of a pair too."""
    if r.kind == ISA_PROBE or r.core not in empty or not r.ops or r.core not in run.khz:
        return ""
    e_us, e_ops = empty[r.core]
    net = r.us_min - e_us * r.ops / e_ops
    cyc = net * run.khz[r.core] / 1000 / r.ops
    return fmt(cyc - 1 if r.kind == ISA_PAIR else cyc, 2)


def case_key(r: Row) -> tuple:
    return (tuple(r.params.items()), r.mode)


def markdown(runs: list[Run]) -> str:
    out = ["# nnbench cost table", ""]
    for run in runs:
        out.append(f"### run {run.plugin}")
        mhz = run.khz.get(1)
        out.append(
            f"- CPU1 clock: {mhz / 1000:.1f} MHz (calib stage, one-cycle add)"
            if mhz
            else "- CPU1 clock: not in the log"
        )
        if 0 in run.khz:
            out.append(f"- CPU0 clock: {run.khz[0] / 1000:.1f} MHz")
        bad = [r for r in run.rows if r.check == "BAD"]
        out.append(f"- {len(run.rows)} measurements, {len(bad)} BAD")
        out.append(
            f"- end: {run.done}"
            if run.done
            else "- end: no `nnbench done` line (the run stopped early)"
        )
        for e in run.errors:
            out.append(f"- error: {e}")
        out.append("")
    out += [
        (
            "Cycles per call: the fastest (min) and the median rep, divided by the calls per rep. "
            "cyc/op: min cycles over ops (MACs for convolutions and dot products, outputs or bytes "
            "elsewhere: the unit column); cyc/op2 likewise for the second count (ternary: non-zero "
            "weight applications). Live rows (prep, modes live / liveu) time the live FD frame and "
            "check on a frame whose hash stayed the same around the reference and the variant."
        ),
    ]
    rows_all = [r for run in runs for r in run.rows]
    multi = len(runs) > 1
    for kernel in dict.fromkeys(r.kernel for r in rows_all):
        rows = [r for r in rows_all if r.kernel == kernel]
        order = list(dict.fromkeys(case_key(r) for r in rows))
        rows.sort(key=lambda r: order.index(case_key(r)))
        out += [
            "",
            f"## {kernel}",
            "",
            "| run | stage | variant | params | mode | ops | unit | cyc min | cyc med | cyc/op | ops2 | unit2 | cyc/op2 | code B | check |",
            "|---|---|---|---|---|---:|---|---:|---:|---:|---:|---|---:|---:|---|",
        ]
        for r in rows:
            check = r.check if not r.err else f"{r.check} ({r.err})"
            if r.bad:
                check += f" ({r.bad} wrong)"
            if r.live:
                check += f" ({r.live})"
            out.append(
                f"| {r.run} | {r.stage} | {r.variant} | {param_text(r)} | {r.mode} | {r.ops} | {r.ops_unit} | "
                f"{r.cyc_min} | {r.cyc_med} | {fmt(r.per_op(r.ops))} | {r.ops2} | {r.ops2_unit} | "
                f"{fmt(r.per_op(r.ops2))} | {r.code or ''} | {check} |"
            )
        fits = []
        groups: dict[tuple[str, str], list[Row]] = {}
        for r in rows:
            if r.check == "OK":
                groups.setdefault((r.variant, r.mode), []).append(r)
        for (v, m), g in groups.items():
            ab = fit([(r.ops, r.cyc_min) for r in g])
            if ab:
                fits.append(f"| {kernel} | {v} | {m} | {len(g)} | {ab[0]:.0f} | {ab[1]:.3f} |")
        if fits:
            out += [
                "",
                "Pooled fit cycles = a + b * ops over the rows above (all shapes of the group"
                + (", both runs" if multi else "")
                + "):",
                "",
                "| kernel | variant | mode | rows | a (cycles) | b (cyc/op) |",
                "|---|---|---|---:|---:|---:|",
                *fits,
            ]
    for run in runs:
        if not run.isa:
            continue
        empty = {r.core: (r.us_min, r.ops) for r in run.isa if r.kind == ISA_EMPTY}
        out += [
            "",
            f"## instruction timings (macbench's stages, run {run.plugin})",
            "",
            "| core | stage | name | us | ops | cycles/op | got | want | MDR | 64-bit high | check |",
            "|---:|---|---|---:|---:|---:|---|---|---|---|---|",
        ]
        for r in run.isa:
            out.append(
                f"| {r.core} | {r.stage} | {r.name} | {r.us_min} | {r.ops} | {isa_cycles(run, r, empty)} | "
                f"{r.got} | {r.want} | {r.mdr} | {r.hi} | {r.check} |"
            )
    return "\n".join(out) + "\n"


def write_csv(rows: list[Row], path: Path) -> None:
    pnames = list(dict.fromkeys(k for r in rows for k in r.params))
    cols = ["run", "stage", "kernel", "variant", *pnames, "mode", "iters", "reps"]
    cols += ["us_min", "us_med", "cyc_min", "cyc_med", "ops", "ops_unit", "ops2", "ops2_unit"]
    cols += ["cyc_per_op", "cyc_per_op2", "code", "check", "bad", "err", "live"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(
                [r.run, r.stage, r.kernel, r.variant]
                + [r.params.get(p, "") for p in pnames]
                + [r.mode, r.iters, r.reps, r.us_min, r.us_med, r.cyc_min, r.cyc_med]
                + [r.ops, r.ops_unit, r.ops2, r.ops2_unit]
                + [fmt(r.per_op(r.ops)), fmt(r.per_op(r.ops2)), r.code]
                + [r.check, r.bad, r.err, r.live]
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", type=Path, nargs="+")
    ap.add_argument(
        "--out", type=Path, help="output prefix (default: nnbench next to the first log)"
    )
    args = ap.parse_args()
    prefix = args.out or args.logs[0].with_name("nnbench")
    runs: dict[str, Run] = {}
    for log in args.logs:
        runs.update(parse(log))
    ordered = [runs[k] for k in sorted(runs)]
    prefix.with_suffix(".md").write_text(markdown(ordered))
    rows = [r for run in ordered for r in run.rows]
    write_csv(rows, prefix.with_suffix(".csv"))
    print(f"{len(rows)} rows -> {prefix.with_suffix('.md')}, {prefix.with_suffix('.csv')}")


if __name__ == "__main__":
    main()
