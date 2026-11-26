"""The CPU setups of the DSP commands, as jobs: what the driver writes to local memory.

cmd 0xF (the copy, program 16): the setup copies the request's src, dst and length (caller block
+8, +0xC, +0x10) to local memory 0x9D000000, 0x9D000004 and 0x9D000008 and points the program
register 0x9D200010 at the program. Callers pass uncached addresses. The setup passes src, dst and
len unchanged; after posting the job, the command's wrapper copies the (-dst) & 0xF head bytes and
the (dst + len) & 0xF tail bytes of dst on the CPU [verified listing], so the DSP job writes only
the 16-byte-aligned middle of dst [inferred]. `copy` is the DSP job alone, without those bytes.

cmd 0xC (the resampler, program 13): the setup copies the 32 tap words from DRAM 0xA324BC00, the
program item's constant block, to local words 0x00..0x1F on every job, then the request's src,
dst, table pointer and six geometry halfwords (caller block +8..+0x1E) to local words
0x742..0x747, and nothing else [verified listing]. The table stays in DRAM, read by the DSP.
"""

import struct

from ..job import Job
from ..program import Program


def copy(program: Program, src: int, dst: int, length: int) -> Job:
    """The cmd 0xF job that runs `program` on (src, dst, length)."""
    for name, v in (("src", src), ("dst", dst), ("length", length)):
        if not 0 <= v <= 0xFFFFFFFF:
            raise ValueError(f"{name} {v:#x} is not a u32")
    return Job(program, ((0, struct.pack("<III", src, dst, length)),))


TAPS_ADDRESS = 0xA324BC00
"""Where the setup reads the taps: the constant block of program 13's item."""
TAP_WORDS = 32
RESAMPLE_TAPS, RESAMPLE_REQUEST = 0x00, 0x742 * 4
"""Local offsets of the tap words and of the six request words."""


def resample(
    program: Program,
    src: int,
    dst: int,
    table: int,
    stride: int,
    width: int,
    height: int,
) -> Job:
    """The cmd 0xC job that runs `program` on (src, dst, table) with equal src and dst geometry.

    The taps are the program's constants, the DRAM words the setup copies: a program with other
    constants is a job with other taps."""
    if len(program.constants) != 4 * TAP_WORDS:
        raise ValueError(f"{len(program.constants)} tap bytes, the setup copies 128")
    for name, v in (("src", src), ("dst", dst), ("table", table)):
        if not 0 <= v <= 0xFFFFFFFF:
            raise ValueError(f"{name} {v:#x} is not a u32")
    for name, v in (("stride", stride), ("width", width), ("height", height)):
        if not 0 <= v <= 0xFFFF:
            raise ValueError(f"{name} {v:#x} is not a u16")
    request = struct.pack(
        "<IIIHHHHHH", src, dst, table, stride, width, height, stride, width, height
    )
    return Job(
        program, ((RESAMPLE_TAPS, program.constants), (RESAMPLE_REQUEST, request))
    )
