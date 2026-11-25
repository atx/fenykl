"""A worked example of an own kernel: y = x ^ 0x20 over a DRAM buffer, DRAM to DRAM.

The buffer streams through the lane memory in 4 KiB chunks over two banks (vectors 0x000 and
0x080): the next chunk's load is handed before the current chunk's compute, so the words hide
it; each chunk is stored back from its bank once its compute is done. Per pass of the inner loop
four vectors are loaded (X0 0x1d), moved into S1's lane file through its first port, XORed by S1
0x04 (b ^ imm7) into the store file O in the head's own word, and stored back to their vectors
(X0 0x1b). A kernel of this shape ran on the camera at 4 and 16 KiB, its dst exact and its
minimum time within 10 us of `timing.estimate` (the jobs took 117 to 415 us, 1 to 16 passes).

The registers the job's setup would provide (B3 = src, B2 = dst) are preset on the machine. Each
word is a bundle of unit instructions and ports (`fenykl.dsp.asm`): `X0.load(A[1], k=i)` loads
vector A[1] + i, `to(L1[4], LOAD)` takes it into L1[4] four words later. `Label("top")` names
the word it is bundled with, `NOP` is the idle word.
"""

import pytest

from fenykl.dsp.asm import (
    F1,
    L1,
    LOAD,
    NOP,
    OP,
    S1,
    X0,
    X1,
    A,
    B,
    Bundle,
    G,
    Label,
    O,
    Part,
    R,
    Reg,
    to,
)
from fenykl.dsp.emu import Boot, Executor, Job, Stop
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Machine, Sv
from fenykl.dsp.program import Program, assemble
from fenykl.dsp.stream import Engine
from fenykl.dsp.timing import estimate

SRC, DST = 0xA0000000, 0xA1000000
B_SRC, B_DST, B_LOAD, B_STORE = 3, 2, 13, 12
CHUNK = 0x1000
BANKS = (0x000, 0x080)


def li(dst: Reg, v: int):
    """Op 0x0b: the 16-bit immediate v (sign-extended) through the ALU bus to dst."""
    return OP.li(v) | to(dst, OP)


def b_add(r: int, src: int, k: int):
    """X1 0x03: B[r] := B[src] + k (13-bit k)."""
    return X1.add_imm(B[src], k) | to(B[r], X1)


def hand(*items: int | tuple[str, int]) -> list[Bundle | Part]:
    """Values to the stream engine through X1 0x1c: an int through B14 / B15, ("b", r) from
    B[r], ("wait",) a 0x1d."""
    out, scratch = [], 14
    for it in items:
        if isinstance(it, int):
            out += [li(B[scratch], it), X1.hand(B[scratch])]
            scratch ^= 1
        elif it[0] == "b":
            out.append(X1.hand(B[it[1]]))
        else:
            out.append(X1.wait(b=B[1]))
    return out


def block(channel: int, nbytes: int) -> list[int]:
    """The channel's eight registers: the lane memory, byte elements, one row of nbytes."""
    return [0x20 | channel << 6, 1, 0, 0x20, nbytes // 32, 0, 0x10, nbytes // 16, 1]


def chunk(bank: int, top: str) -> list[Bundle | Part]:
    """y = x ^ 0x20 over the bank's 128 vectors, four per pass; G8 counts, G9 / A[1] walk."""
    # S1 0x04, b ^ imm7, into O[j - 3] in the head's word; imm7 holds only bits 3..2 of r,
    # r = F1[4] sets all four.
    xor = [
        S1.xor_imm(L1[j], imm=0x20, a=L1[j], r=F1[4]) | to(O[j - 3], S1)
        for j in range(4, 8)
    ]
    store = [X0.store(A[1], k=i, o=O[1 + i]) for i in range(4)]
    return [
        li(G[9], bank),
        li(A[1], bank),
        li(G[8], 128 // 4 - 1),
        NOP,
        NOP,
        Label(top) | X0.load(A[1], k=0),
        *[X0.load(A[1], k=i) for i in range(1, 4)],
        to(L1[4], LOAD),
        to(L1[5], LOAD),
        to(L1[6], LOAD) | xor[0],
        to(L1[7], LOAD) | xor[1] | store[0],
        xor[2] | store[1],
        xor[3] | store[2],
        store[3] | OP.add_imm(G[9], 4) | to(G[9], OP) | to(A[1], OP),
        NOP,
        NOP,
        OP.loop(G[8], top, rr=R[0]) | to(G[8], OP),
        *[NOP] * 4,
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
        words += chunk(BANKS[c % 2], f"top{c}")
    words += hand(0x46, BANKS[(n - 1) % 2] * 64, ("b", B_STORE))
    words += hand(*[("wait",)] * (len(out) + 1))
    return assemble([*words, OP.halt(), *[NOP] * 4])


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
