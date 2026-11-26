"""The copy model's transfer registers (xreg): register intervals, the register facts and the
boot parameters.

When a job starts, `REGISTER_FACTS` set the registers they cover from the job's request and the
boot's parameters (unknown where a fact has no value for the job). They are kept apart from the
op slot's registers (ra, rb): nothing shows the two to be one file.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from ..fields import Confidence

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


@dataclass(frozen=True)
class Reg:
    """A register's value as a closed interval [lo, hi], exact when lo == hi."""

    lo: int
    hi: int
    taint: Confidence
    fact: str

    @property
    def exact(self) -> int | None:
        return self.lo if self.lo == self.hi else None

    def image(self, f: Callable[[int], int]) -> int | None:
        """f of the value when f is the same at both ends (f monotonic), else None."""
        a, b = f(self.lo), f(self.hi)
        return a if a == b else None


@dataclass(frozen=True)
class JobView:
    """What the register facts may read: the request in local memory and the boot parameters."""

    src: int | None
    dst: int | None
    length: int | None
    boot: "BootParams"


@dataclass(frozen=True)
class RegisterFact:
    reg: int
    taint: Confidence
    fact: str
    value: Callable[[JobView], tuple[int, int] | None]


def _exact(
    f: Callable[[JobView], int | None],
) -> Callable[[JobView], tuple[int, int] | None]:
    def value(job: JobView) -> tuple[int, int] | None:
        v = f(job)
        return None if v is None else (v, v)

    return value


def _const(v: int) -> Callable[[JobView], tuple[int, int] | None]:
    return lambda job: (v, v)


def _half(job: JobView) -> int | None:
    return None if job.length is None else job.length // 2


# Values seen through program 16's register-transfer words on the camera (copy jobs of
# 0.5..4 KiB): word 73 counts a register's value in bytes, word 68 offsets the load by the
# value / 32 (rounded to 32 bytes) and word 67 reads at it as an address. The program's data ops
# write some of them through PR port 267 (word 64 writes r7, inferred); the model sets them when
# the job starts with the values its transfers read.
REGISTER_FACTS: tuple[RegisterFact, ...] = (
    RegisterFact(
        0, I, "0x10: word 73 counts 0x10 bytes, word 68 offsets by 0", _const(0x10)
    ),
    RegisterFact(
        1,
        V,
        "the job length: word 73 counts len bytes, word 68 offsets by len / 32",
        _exact(lambda job: job.length),
    ),
    RegisterFact(
        3,
        V,
        "the job's src: the only register that makes word 67 load the job's src",
        _exact(lambda job: job.src),
    ),
    *(
        RegisterFact(
            r,
            I,
            "0: word 73 counts nothing (dst not written), word 68 offsets by 0",
            _const(0),
        )
        for r in (4, 5, 10, 12, 13)
    ),
    RegisterFact(
        6,
        H,
        "0x400..0x41F: word 68 offsets by a fixed +0x20 at 0.5..4 KiB; word 73 at r6 times out, "
        "so the low bits are not all zero or the register is no plain count",
        lambda job: (0x400, 0x41F),
    ),
    RegisterFact(
        7, V, "half the job length: word 73 counts len / 2 bytes", _exact(_half)
    ),
    RegisterFact(
        9, I, "0x200: word 73 counts 0x200 bytes, word 68 offsets by 0", _const(0x200)
    ),
    RegisterFact(
        11,
        H,
        "as r6 in words 68 and 76 at 1 KiB (+0x20), seen at no other length",
        lambda job: (0x400, 0x41F) if job.length == 0x400 else None,
    ),
    RegisterFact(
        14,
        H,
        "negative, a boot parameter: word 68 offsets by -0xE0 on every boot, word 76 by -0xE0 or "
        "-0x100 depending on the boot",
        lambda job: job.boot.r14,
    ),
    RegisterFact(
        21,
        V,
        "0x606CC000 on every boot read: word 67 loads the firmware text there",
        _const(0x606CC000),
    ),
    RegisterFact(
        29,
        V,
        "0x60E58B00 or 0x60E58B40, a boot parameter: word 67 loads the firmware text there",
        lambda job: None if job.boot.r29 is None else (job.boot.r29, job.boot.r29),
    ),
)


@dataclass(frozen=True)
class BootParams:
    """State that differs between boots and that no job is known to set; None = not known.

    - r14: the interval of r14. Word 68 truncates r14 / 32 towards zero and word 76 rounds it
      down (hypothesis), so -0x1C00 gives both -0xE0, while (-0x2000, -0x1C00) gives 68 -0xE0
      and 76 -0x100; both were seen, on different boots.
    - r29: word 67's load address at r29.
    - r21_format: word 47's store format at r21, "pack10" or "zero"; it differs between boots
      with nothing else seen to differ.
    """

    r14: tuple[int, int] | None = (-0x1FFF, -0x1C00)
    r29: int | None = None
    r21_format: str | None = None


R14_EXACT = (-0x1C00, -0x1C00)
"""r14 on the boots where word 76 at r14 offsets by -0xE0."""
R14_BELOW = (-0x1FFF, -0x1C01)
"""r14 on the boots where word 76 at r14 offsets by -0x100."""


@dataclass
class RegisterFile:
    """The registers of one boot; they persist across its jobs."""

    regs: list[Reg | None] = field(default_factory=lambda: [None] * 32)

    def start_job(self, job: JobView) -> None:
        """Set the registers the facts give when a job starts; a fact without a value for this
        job leaves its register unknown."""
        for f in REGISTER_FACTS:
            v = f.value(job)
            self.regs[f.reg] = None if v is None else Reg(v[0], v[1], f.taint, f.fact)

    def __getitem__(self, r: int) -> Reg | None:
        return self.regs[r]

    def __setitem__(self, r: int, value: Reg | None) -> None:
        self.regs[r] = value
