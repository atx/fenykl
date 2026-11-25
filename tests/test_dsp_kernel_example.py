"""A worked example of an own kernel: y = x ^ 0x20 over a DRAM buffer, DRAM to DRAM.

The buffer streams through the lane memory in 4 KiB chunks over two banks (vectors 0x000 and
0x080): the next chunk's load is handed before the current chunk's compute, so the words hide
it; each chunk is stored back from its bank once its compute is done. Per pass of the inner loop
four vectors are loaded (X0 0x1d), moved into S1's lane file through its first port, XORed by S1
0x04 (b ^ imm7) into the store file O in the head's own word, and stored back to their vectors
(X0 0x1b). A kernel of this shape ran on the camera at 4 and 16 KiB, its dst exact and its
minimum time within 10 us of `timing.estimate` (the jobs took 117 to 415 us, 1 to 16 passes).

The registers the job's setup would provide (B3 = src, B2 = dst) are preset on the machine. The
words are written by field name (`fields.NAMES`), data op (`Word.data`) and head view
(`Word.head`), whose immediates compose from their split fields.
"""

import pytest

from fenykl.dsp.emu import Boot, Executor, Job, Stop
from fenykl.dsp.fields import Confidence, Operands
from fenykl.dsp.machine import Machine, Sv
from fenykl.dsp.program import Program
from fenykl.dsp.stream import Engine
from fenykl.dsp.timing import estimate
from fenykl.dsp.word import IDLE, Word

SRC, DST = 0xA0000000, 0xA1000000
B_SRC, B_DST, B_LOAD, B_STORE = 3, 2, 13, 12
CHUNK = 0x1000
BANKS = (0x000, 0x080)
HALT = Word.from_fields({"op": 7})


def w(**fields: int) -> Word:
    return Word.from_fields(fields)


def li(v: int, **port: int) -> Word:
    """Op 0x0b: the 16-bit immediate v (sign-extended) to the ports given."""
    return Word.data(0x0B, Operands(ra=31, imm=v), base=w(**port))


def to_b(v: int, r: int) -> Word:
    return li(v, bsrc=1, bdst=r)


def to_g(v: int, r: int) -> Word:
    return li(v, gsrc=1, gdst=r)


def b_add(r: int, src: int, k: int) -> Word:
    """X1 0x03: B[r] := B[src] + k (13-bit k) through bsrc = 0."""
    return Word.head("x1", w(bsrc=0, bdst=r), op=0x03, a=src, k=k)


def hand(*items: int | tuple[str, int]) -> list[Word]:
    """Values to the stream engine through X1 0x1c: an int through B14 / B15, ("b", r) from
    B[r], ("wait",) a 0x1d."""
    out, scratch = [], 14
    for it in items:
        if isinstance(it, int):
            out += [to_b(it, scratch), Word.head("x1", op=0x1C, b=scratch)]
            scratch ^= 1
        elif it[0] == "b":
            out.append(Word.head("x1", op=0x1C, b=it[1]))
        else:
            out.append(Word.head("x1", op=0x1D, b=1))
    return out


def block(channel: int, nbytes: int) -> list[int]:
    """The channel's eight registers: the lane memory, byte elements, one row of nbytes."""
    return [0x20 | channel << 6, 1, 0, 0x20, nbytes // 32, 0, 0x10, nbytes // 16, 1]


def chunk(bank: int, start: int) -> list[Word]:
    """y = x ^ 0x20 over the bank's 128 vectors, four per pass; G8 counts, G9 / A[1] walk."""
    body = [Word.head("x0", op=0x1D, a=1, b=0, k=i) for i in range(4)]
    body += [w(l1src=0, l1dst=4 + i) for i in range(4)] + [IDLE] * 5
    for i in range(4):
        # S1 0x04, b ^ imm7, into O[1 + i] in the head's word; imm7 holds only bits 3..2 of
        # s1c, c = 4 sets all four.
        into_o = Word.from_fields({"osrc": 3, "odst": 1 + i}, base=body[6 + i])
        body[6 + i] = Word.head("s1", into_o, op=0x04, a=4 + i, b=4 + i, c=4, imm=0x20)
        body[7 + i] = Word.head("x0", body[7 + i], op=0x1B, a=1, b=0, k=i, o=1 + i)
    ports = Word.from_fields(
        {"gsrc": 1, "gdst": 9, "asrc": 2, "adst": 1}, base=body[10]
    )
    body[10] = Word.data(0x1F, Operands(ra=9, imm=4), base=ports)
    top = start + 5
    loop = Word.from_fields(
        {"ra": 8, "rr": 0, "gsrc": 1, "gdst": 8}, base=Word.ctl(0, top, IDLE)
    )
    return [
        to_g(bank, 9),
        li(bank, asrc=2, adst=1),
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
