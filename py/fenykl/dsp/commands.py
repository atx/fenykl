"""The CPU setups of the DSP commands, as jobs: what the driver writes to local memory.

cmd 0xF (the copy, program 16): the setup copies the request's src, dst and length (caller block
+8, +0xC, +0x10) to local memory 0x9D000000, 0x9D000004 and 0x9D000008 and points the program
register 0x9D200010 at the program. Callers pass uncached addresses; the command's wrapper copies
the bytes outside the 16-byte-aligned middle on the CPU, so the job sees aligned src, dst and len.
"""

import struct

from .emu import Job
from .program import Program


def copy(program: Program, src: int, dst: int, length: int) -> Job:
    """The cmd 0xF job that runs `program` on (src, dst, length)."""
    for name, v in (("src", src), ("dst", dst), ("length", length)):
        if not 0 <= v <= 0xFFFFFFFF:
            raise ValueError(f"{name} {v:#x} is not a u32")
    return Job(program, ((0, struct.pack("<III", src, dst, length)),))
