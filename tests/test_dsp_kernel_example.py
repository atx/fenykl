"""A worked example of an own kernel: y = x ^ 0x20 over a DRAM buffer, DRAM to DRAM.

The buffer streams through the lane memory in 4 KiB chunks over two banks (vectors 0x000 and
0x080): the next chunk's load is handed before the current chunk's compute, so the words hide
it; each chunk is stored back from its bank once its compute is done. Per pass of the inner loop
four vectors are loaded (X0 0x1d), moved into S1's lane file through its first port, XORed by S1
0x04 (b ^ imm7) into the store file O in the head's own word, and stored back to their vectors
(X0 0x1b). A kernel of this shape ran on the camera at 4 and 16 KiB, its dst exact and its
minimum time within 10 us of `timing.estimate` (the jobs' 112 to 446 us).

The registers the job's setup would provide (B3 = src, B2 = dst) are preset on the machine.
"""

import pytest

from fenykl.dsp.emu import Boot, Executor, Job, Stop
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Machine, Sv
from fenykl.dsp.program import Program
from fenykl.dsp.stream import Engine
from fenykl.dsp.timing import estimate
from fenykl.dsp.word import IDLE, Word

SRC, DST = 0xA0000000, 0xA1000000
B_SRC, B_DST, B_LOAD, B_STORE = 3, 2, 13, 12
CHUNK = 0x1000
BANKS = (0x000, 0x080)
HALT = Word.from_fields({"op": 7}, base=IDLE)


def w(**fields: int) -> Word:
    return Word.from_fields(fields, base=IDLE)


def li(v: int, **port: int) -> Word:
    """Op 0x0b: the 16-bit immediate v (sign-extended) to the ports given."""
    return w(
        f313_319=0x0B, f15_22=v & 0xFF, f128_132=v >> 8 & 0x1F, f120_122=v >> 13, **port
    )


def to_b(v: int, r: int) -> Word:
    return li(v, f267_268=1, f211_215=r)


def to_g(v: int, r: int) -> Word:
    return li(v, f272_273=1, f224_228=r)


def b_add(r: int, src: int, k: int) -> Word:
    """X1 0x03: B[r] := B[src] + k (13-bit k) through f267_268 = 0."""
    return w(
        f308_312=0x03,
        f110_114=src,
        f115_119=k >> 8,
        f9_14=k >> 2 & 63,
        f7_8=k & 3,
        f267_268=0,
        f211_215=r,
    )


def hand(*items: int | tuple[str, int]) -> list[Word]:
    """Values to the stream engine through X1 0x1c: an int through B14 / B15, ("b", r) from
    B[r], ("wait",) a 0x1d."""
    out, scratch = [], 14
    for it in items:
        if isinstance(it, int):
            out += [to_b(it, scratch), w(f308_312=0x1C, f115_119=scratch)]
            scratch ^= 1
        elif it[0] == "b":
            out.append(w(f308_312=0x1C, f115_119=it[1]))
        else:
            out.append(w(f308_312=0x1D, f115_119=1))
    return out


def block(channel: int, nbytes: int) -> list[int]:
    """The channel's eight registers: the lane memory, byte elements, one row of nbytes."""
    return [0x20 | channel << 6, 1, 0, 0x20, nbytes // 32, 0, 0x10, nbytes // 16, 1]


def chunk(bank: int, start: int) -> list[Word]:
    """y = x ^ 0x20 over the bank's 128 vectors, four per pass; G8 counts, G9 / A[1] walk."""
    body = [
        w(f302_307=0x1D, f100_104=1, f105_109=0, f2_6=i >> 1, f1=i & 1)
        for i in range(4)
    ]
    body += [w(f240_241=0, f155_158=4 + i) for i in range(4)] + [IDLE] * 5
    for i in range(4):
        body[6 + i] = Word.from_fields(
            {
                "f285_291": 0x04,
                "f45_48": 4 + i,
                "f49_52": 4 + i,
                "f80_83": 4,
                "f84_87": 0,
                "f252_254": 3,
                "f179_181": 1 + i,
            },
            base=body[6 + i],
        )
        body[7 + i] = Word.from_fields(
            {
                "f302_307": 0x1B,
                "f100_104": 1,
                "f105_109": 0,
                "f2_6": i >> 1,
                "f1": i & 1,
                "f69_71": 1 + i,
            },
            base=body[7 + i],
        )
    body[10] = Word.from_fields(
        {
            "f313_319": 0x1F,
            "f123_127": 9,
            "f15_22": 4,
            "f128_132": 0,
            "f120_122": 0,
            "f272_273": 1,
            "f224_228": 9,
            "f265_266": 2,
            "f206_210": 1,
        },
        base=body[10],
    )
    top = start + 5
    loop = Word.from_fields(
        {"f123_127": 8, "f120_122": 0, "f272_273": 1, "f224_228": 8},
        base=Word.ctl(0, top, IDLE),
    )
    return [
        to_g(bank, 9),
        li(bank, f265_266=2, f206_210=1),
        to_g(128 // 4 - 1, 8),
        IDLE,
        IDLE,
        *body,
        loop,
        *[IDLE] * 4,
    ]


def xor_kernel(nbytes: int) -> Program:
    """The kernel over nbytes (a multiple of 4 KiB), every transfer waited for before the halt."""
    words = hand(*block(0, CHUNK), *block(1, CHUNK))
    words += [b_add(B_LOAD, B_SRC, 0), b_add(B_STORE, B_DST, 0)]
    words += hand(0, ("b", B_LOAD), BANKS[0] * 64)
    out = ["L0"]
    n = nbytes // CHUNK
    for c in range(n):
        if c:
            words += hand(0x46, BANKS[(c - 1) % 2] * 64, ("b", B_STORE))
            words.append(b_add(B_STORE, B_STORE, CHUNK))
            out.append(f"S{c - 1}")
        while (
            out
        ):  # the engine runs its commands in order: wait until chunk c has landed
            words += hand(("wait",))
            if out.pop(0) == f"L{c}":
                break
        if c + 1 < n:
            words.append(b_add(B_LOAD, B_LOAD, CHUNK))
            words += hand(0, ("b", B_LOAD), BANKS[(c + 1) % 2] * 64)
            out.append(f"L{c + 1}")
        words += chunk(BANKS[c % 2], len(words))
    words += hand(0x46, BANKS[(n - 1) % 2] * 64, ("b", B_STORE))
    words += hand(*[("wait",)] * (len(out) + 1))
    return Program((*words, HALT, *[IDLE] * 4))


def run(nbytes: int, data: bytes):
    m = Machine(Boot(), stream=Engine())
    m.boot.put(SRC, data, "src")
    m.b[B_SRC], m.b[B_DST], m.b[1] = Sv(SRC), Sv(DST), Sv(1)
    job = Job(xor_kernel(nbytes), ())
    flow = Executor(m.boot, strict=True, datapath=m).step(job)
    assert flow.stop is Stop.HALT
    m.between_jobs()
    return m.boot.read(DST, nbytes), estimate(job, flow, m.stream)


@pytest.mark.parametrize("nbytes", [0x1000, 0x4000])
def test_the_xor_kernel_streams_dram_to_dram(nbytes):
    data = bytes((7 * i + i // 251) & 0xFF for i in range(nbytes))
    got, e = run(nbytes, data)
    assert all(got.known)
    assert bytes(got.data) == bytes(b ^ 0x20 for b in data)
    # A store's source counts as rewritten once a later load is handed into its bank, though the
    # engine runs the two in order: with more than two chunks those are inferred.
    assert got.weakest() is (
        Confidence.VERIFIED if nbytes <= 2 * CHUNK else Confidence.INFERRED
    )
    assert e.us is not None and e.tier is Confidence.INFERRED
    assert any(p.name == "stream waits" for p in e.parts)


def test_the_kernel_hides_its_loads():
    """The waits are what the chunk's compute does not cover: under a microsecond per chunk."""
    _, e = run(0x4000, bytes(0x4000))
    waits = next(p for p in e.parts if p.name == "stream waits")
    assert waits.us < 4 * 1.5
