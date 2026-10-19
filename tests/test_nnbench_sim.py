"""nnbench, nnbench2 and nnbench3 (sdk/projects/nnbench, nnbench2, nnbench3): their kernels in the
GDB simulator against the C references with each plugin's plan (tests/sim/nnbench/test_nnbench.c,
bit-exact; nnbench3: trained ternary layers whole and split into blocks), a broken kernel caught,
the generated kernels current with gen_kernels.py (nnbench3's need the fenykl-nn handoff and are not
regenerated here), and the log report on synthetic logs."""

import csv
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import (
    DRAM_ALIAS,
    POWERON,
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
)

from fenykl import sdk

PROJECTS = sdk.SDK / "projects"
PROJECT = PROJECTS / "nnbench"
PLUGINS = ["nnbench", "nnbench2", "nnbench3"]
# gen_kernels.py from its own parameters alone
GENERATED = ["nnbench", "nnbench2"]
# The pure kernel and case code; bench.c, isa.c and nnbench.c call the firmware, fam_bdnet.c is
# birddetect's network (tests/test_birddetect_sim.py). nnbench2's files include nnbench's.
KERNELS = {
    "nnbench": [
        "cases.c",
        "plan.c",
        "fam_conv.c",
        "fam_dot.c",
        "fam_misc.c",
        "fam_icache.c",
        "nb_gen.c",
        "nb_gen.S",
        "nb_kern.S",
    ],
    "nnbench2": [
        "cases.c",
        "plan.c",
        "fam_conv.c",
        "fam_dot.c",
        "fam_misc.c",
        "nb_gen.c",
        "nb_gen.S",
        "nb_kern.S",
    ],
    "nnbench3": [
        "cases.c",
        "plan.c",
        "fam_conv.c",
        "fam_misc.c",
        "fam_tsplit.c",
        "nb_gen.c",
        "nb_gen.S",
        "nb_kern.S",
    ],
}


def run_kernels(
    workdir: Path, projects: Path, plugin: str
) -> subprocess.CompletedProcess:
    return run_in_simulator(
        workdir,
        f"{plugin}sim",
        [
            SIM / "nnbench" / "test_nnbench.c",
            *(projects / plugin / name for name in KERNELS[plugin]),
            SIM / "hooks" / "sim_start.S",
        ],
        [DRAM_ALIAS, region("0x98900000,0x100"), POWERON],
        timeout=600,
    )


@needs_toolchain
@pytest.mark.parametrize("plugin", PLUGINS)
def test_nnbench_kernels_in_the_simulator(tmp_path, plugin):
    result = run_kernels(tmp_path, PROJECTS, plugin)
    assert_exit(result, 0, "tests/sim/nnbench/test_nnbench.c")
    assert b"0 FAIL" in result.stdout


# (plugin whose plan runs, file, original, broken, the variant that must fail)
BREAKS = [
    (
        "nnbench",
        "nnbench/fam_conv.c",
        '"add %[x],%[s1]\\n\\t"',
        '"sub %[x],%[s1]\\n\\t"',
        "hwc_udf2",
    ),
    (
        "nnbench",
        "nnbench/fam_dot.c",
        'NB_DOT_ASM(nb_dot_udf_dn, "udf00", x, y)',
        'NB_DOT_ASM(nb_dot_udf_dn, "udf00", x, x)',
        "udf_dn",
    ),
    ("nnbench", "nnbench/nb_gen.S", "\tadd\td0,d2\n", "\tsub\td0,d2\n", "gen"),
    ("nnbench", "nnbench/nb_kern.S", "\t\\op\td1,d3\n", "\tadd\td1,d3\n", "pair_tab_asm"),
    ("nnbench", "nnbench/nb_kern.S", "and\t0x0F0F0F0F,d0", "and\t0x0F0F0F0E,d0", "swar_asm"),
    ("nnbench", "nnbench/nb_kern.S", "\tadd\td1,d3\n\t.endm", "\tsub\td1,d3\n\t.endm", "lut8_asm"),
    ("nnbench", "nnbench/nb_kern.S", "\tasr\t28,d1\n", "\tasr\t27,d1\n", "i4_asm"),
    ("nnbench", "nnbench/nb_kern.S", "\tasr\t16,d2\n", "\tasr\t15,d2\n", "i4_lut"),
    ("nnbench", "nnbench/nb_kern.S", "\tand\t0x3C,d0\n", "\tand\t0x38,d0\n", "i2_lut"),
    ("nnbench", "nnbench/nb_kern.h", "((lt << 1) - (lt >> 7))", "(lt << 1)", "cw"),
    ("nnbench2", "nnbench2/nb_gen.S", "\tM4S\td1,a3,\\w\n", "\tM4S\td1,a2,\\w\n", "gen4"),
    # one block of one split reads its input a pixel off
    (
        "nnbench3",
        "nnbench3/nb_gen.S",
        "_nb3_l5_split7_3:\n\tmovm [d2,d3],(sp)\n\tmov d0,a0\n",
        "_nb3_l5_split7_3:\n\tmovm [d2,d3],(sp)\n\tmov d0,a0\n\tadd 4,a0\n",
        "split7",
    ),
]


@needs_toolchain
@pytest.mark.parametrize(
    "plugin, name, original, broken, variant", BREAKS, ids=[b[4] for b in BREAKS]
)
def test_a_broken_kernel_fails(tmp_path, plugin, name, original, broken, variant):
    projects = tmp_path / "projects"
    for p in PLUGINS:
        shutil.copytree(
            PROJECTS / p, projects / p, ignore=shutil.ignore_patterns("build")
        )
    path = projects / name
    text = path.read_text()
    assert original in text
    path.write_text(text.replace(original, broken, 1))
    result = run_kernels(tmp_path, projects, plugin)
    out = result.stdout.decode("latin-1")
    assert result.returncode != 0, out[-2000:]
    assert f" v={variant} " in out, out[-2000:]


@pytest.mark.parametrize("plugin", GENERATED)
def test_generated_kernels_are_current(tmp_path, plugin):
    subprocess.run(
        [sys.executable, str(PROJECT / "gen_kernels.py"), plugin, str(tmp_path)],
        check=True,
    )
    for name in ("nb_gen.S", "nb_gen.c"):
        assert (tmp_path / name).read_text() == (
            PROJECTS / plugin / name
        ).read_text(), name


SYNTHETIC_LOG = """\
==================================================
[     0.054823] I nnb    : nnbench, load 6ea70000 size 4b340, log init 0
[     3.187768] I nnb    : I c=1 st=calib kind=0 us=40/41 ops=256000 got=0 want=0 mdr=0 hi=0 chk=- name=empty loop
[     3.187770] I nnb    : I c=1 st=calib kind=1 us=1651/1652 ops=256000 got=0 want=0 mdr=0 hi=0 chk=- name=add (clock)
[     3.187774] I nnb    : clock c=1 khz=158900 (add = 1 cycle)
[     4.000000] I nnb    : R st=conv3 k=conv v=c cin=8 cout=8 h=16 w=16 kh=3 kw=3 s=1 d=1 fl=0 nz=0 m=warm it=1 reps=5 us=9000/9100 cyc=1430100/1446000 ops=147456 mac ops2=2048 out code=0 chk=OK bad=0
[     4.100000] I nnb    : R st=conv3 k=conv v=gen cin=8 cout=8 h=16 w=16 kh=3 kw=3 s=1 d=1 fl=0 nz=0 m=warm it=8 reps=5 us=4000/4040 cyc=79450/80245 ops=147456 mac ops2=2048 out code=9300 chk=OK bad=0
[     4.200000] I nnb    : R st=conv3 k=conv v=gen cin=4 cout=8 h=16 w=16 kh=3 kw=3 s=1 d=1 fl=0 nz=0 m=warm it=16 reps=5 us=4000/4100 cyc=39725/40718 ops=73728 mac ops2=2048 out code=4700 chk=OK bad=0
[     4.250000] I nnb    : R st=conv3 k=conv v=hwc_udf cin=4 cout=8 h=16 w=16 kh=3 kw=3 s=1 d=1 fl=0 nz=0 m=warm it=2 reps=5 us=4000/4100 cyc=317800/325745 ops=73728 mac ops2=2048 out code=0 chk=BAD bad=12
[     4.300000] E nnb    : R st=dwpw k=conv v=hwc_udf ci=3 chk=BAD err=no memory for the variant
[     9.000000] I nnb    : nnbench done 0: 5 rows, 2 BAD, 6000 ms
"""


SYNTHETIC_LOG2 = """\
[     0.050000] I nnb    : nnbench2, load 6ea70000 size 12040, log init 0
[     3.187774] I nnb    : clock c=1 khz=159000 (add = 1 cycle)
[     4.000000] I nnb    : R st=gen4 k=conv v=gen4 cin=8 cout=8 h=16 w=16 kh=3 kw=3 s=1 d=1 fl=0 nz=0 m=warm it=8 reps=5 us=4000/4040 cyc=70000/71000 ops=147456 mac ops2=2048 out code=18000 chk=OK bad=0
[     5.000000] I nnb    : R st=live k=prep v=c op=0 ow=96 oh=72 sh=480 m=live it=50 reps=5 us=4000/4100 cyc=12720/13038 ops=6912 out ops2=307200 srcbyte code=0 chk=OK bad=0 frame=6dc56300 hash=1a2b3c4d tries=1
[     5.100000] I nnb    : R st=live k=prep v=c ci=1 chk=SKIP err=no live FD frame (not in live view?)
[     9.000000] I nnb    : nnbench done 0: 3 rows, 0 BAD, 5000 ms
"""


def test_report_merges_both_runs(tmp_path):
    logs = [tmp_path / "a.log", tmp_path / "b.log"]
    logs[0].write_text(SYNTHETIC_LOG)
    logs[1].write_text(SYNTHETIC_LOG2)
    subprocess.run(
        [sys.executable, str(PROJECT / "nnbench_report.py"), *map(str, logs)]
        + ["--out", str(tmp_path / "r")],
        check=True,
    )
    md = (tmp_path / "r.md").read_text()
    rows = list(csv.DictReader((tmp_path / "r.csv").open()))
    assert "### run nnbench\n" in md and "### run nnbench2\n" in md
    # the same case from both runs: gen (nnbench) and gen4 (nnbench2) next to each other
    case = [
        (r["run"], r["variant"]) for r in rows if r["cin"] == "8" and r["kernel"] == "conv"
    ]
    assert ("nnbench", "gen") in case and ("nnbench2", "gen4") in case
    live = next(r for r in rows if r["mode"] == "live")
    assert live["live"] == "frame=6dc56300 hash=1a2b3c4d tries=1" and live["check"] == "OK"
    assert "SKIP (no live FD frame" in md
    # the fit of gen: nnbench's two rows
    assert "| conv | gen | warm | 2 | 0 | 0.539 |" in md


def test_report_on_a_synthetic_log(tmp_path):
    log = tmp_path / "FENYKL.LOG"
    log.write_text(SYNTHETIC_LOG)
    subprocess.run(
        [
            sys.executable,
            str(PROJECT / "nnbench_report.py"),
            str(log),
            "--out",
            str(tmp_path / "r"),
        ],
        check=True,
    )
    md = (tmp_path / "r.md").read_text()
    rows = list(csv.DictReader((tmp_path / "r.csv").open()))
    assert "158.9 MHz" in md
    gen = next(r for r in rows if r["variant"] == "gen" and r["cin"] == "8")
    # 79450 cycles per call over 147456 MACs
    assert (
        gen["cyc_per_op"] == "0.539"
        and gen["cyc_min"] == "79450"
        and gen["check"] == "OK"
    )
    assert [r["check"] for r in rows if r["cin"] == "4"] == ["OK", "BAD"]
    assert "no memory for the variant" in md
    assert "| conv | gen |" in md
    # cycles = a + b * ops over the two gen rows: a = 0, b = 79450 / 147456
    assert "| conv | gen | warm | 2 | 0 | 0.539 |" in md
    assert "(12 wrong)" in md


SYNTHETIC_LOG3 = """\
[     0.050000] I nnb    : nnbench3, load 6ea70000 size 41980, log init 0
[     3.187774] I nnb    : clock c=1 khz=159000 (add = 1 cycle)
[     4.000000] I nnb    : R st=l5 k=tsplit v=pair_tab_asm cin=28 cout=28 h=9 w=12 kh=3 kw=3 s=1 d=1 fl=2 layer=5 m=warm it=1 reps=5 us=23000/23100 cyc=3657000/3672900 ops=762048 mac ops2=284148 nzw code=0 chk=OK bad=0
[     4.100000] I nnb    : R st=l5 k=tsplit v=split5 cin=28 cout=28 h=9 w=12 kh=3 kw=3 s=1 d=1 fl=2 layer=5 m=warm it=1 reps=5 us=5000/5100 cyc=795000/810900 ops=762048 mac ops2=284148 nzw code=32831 chk=OK bad=0 blocks=5 sizes=6972,6893,7026,5936,6004
[     9.000000] I nnb    : nnbench done 0: 2 rows, 0 BAD, 5000 ms
"""


def test_report_on_split_layers(tmp_path):
    log = tmp_path / "nnbench3.log"
    log.write_text(SYNTHETIC_LOG3)
    subprocess.run(
        [sys.executable, str(PROJECT / "nnbench_report.py"), str(log), "--out", str(tmp_path / "r")],
        check=True,
    )
    md = (tmp_path / "r.md").read_text()
    rows = list(csv.DictReader((tmp_path / "r.csv").open()))
    split = next(r for r in rows if r["variant"] == "split5")
    assert split["blocks"] == "5" and split["sizes"] == "6972,6893,7026,5936,6004"
    assert split["layer"] == "5"
    # 3657000 / 795000 cycles per layer
    assert "| warm | split5 | 5 | 32831 | 0.795 | 0.811 | 4.60 | OK |" in md
