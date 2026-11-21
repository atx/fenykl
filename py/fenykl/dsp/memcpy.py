"""Program 16, the stock copy (command 0xF), rebuilt from slot objects.

The driver passes src, dst and len in DSP local memory words 0..2. The comments give each word's
role, read from camera runs that replaced it by the idle word ("idled") or swept its register
field; what a run showed is stated as such, the rest is inferred from it. `program()` equals the
stock item's program word for word.

The register values the camera showed through the register-transfer words are data in
`state.REGISTER_FACTS`; what each word does, or does without, is in `semantics`.

Hazards: idling word 62 lost the camera's link and idling 64 or 72 failed the job with status
-5; these register values lost the link too: word 44 at 0, 1, 4, 8, 10; word 47 at 16, 18, 19,
20, 24, 26, 27, 28; word 70 at 26; word 71 at 0.
"""

from fenykl.dsp.fields import FIELDS, STAIRCASE, Kind
from fenykl.dsp.program import Program, assemble
from fenykl.dsp.units import C2, SLOTS, CtlHi, CtlLo, RegsA, RegsB, Slot
from fenykl.dsp.word import IDLE, Word


def route(index: int) -> dict[str, Slot]:
    """The grid slots of word `index`: the nibbles the staircase has switched to 0 by then."""
    on: dict[str, dict[str, int]] = {}
    for f in FIELDS:
        if f.kind is Kind.ROUTE and index >= STAIRCASE.get(f.lo, index + 1):
            on.setdefault(f.unit, {})[f.id] = 0
    return {unit: SLOTS[unit](**values) for unit, values in on.items()}


def xfer(
    index: int, context: tuple[RegsA, RegsB], xreg: int, f308_309: int = 0
) -> Word:
    """A register-transfer word: its register context with `xreg`; the role is its position."""
    regs_a, regs_b = context
    values = {f.id: v for f, v in regs_a.items()}
    return Word(
        regs_a=RegsA(**values, xreg=xreg),
        regs_b=regs_b,
        ctl_hi=CtlHi(f308_309=f308_309),
        **route(index),
    )


# The register contexts of the register-transfer words, named after their first word.
CTX42 = (
    RegsA(f100_104=5, f105_109=4, f110_114=0, f123_127=2, f128_132=3),
    RegsB(f206_210=4, f211_215=13, f216_220=11, f224_228=4),
)
CTX46 = (
    RegsA(f100_104=5, f105_109=4, f110_114=0, f123_127=5, f128_132=3),
    RegsB(f206_210=4, f211_215=13, f216_220=11, f224_228=4),
)
CTX54 = (
    RegsA(f100_104=1, f105_109=2, f110_114=0, f123_127=2, f128_132=3),
    RegsB(f206_210=1, f211_215=3, f216_220=11, f224_228=6),
)
CTX57 = (
    RegsA(f100_104=1, f105_109=2, f110_114=0, f123_127=4, f128_132=3),
    RegsB(f206_210=1, f211_215=2, f216_220=11, f224_228=4),
)
CTX66 = (
    RegsA(f100_104=1, f105_109=1, f110_114=0, f123_127=2, f128_132=2),
    RegsB(f206_210=1, f211_215=8, f216_220=6, f224_228=2),
)
CTX70 = (
    RegsA(f100_104=1, f105_109=1, f110_114=0, f123_127=2, f128_132=2),
    RegsB(f206_210=1, f211_215=3, f216_220=6, f224_228=2),
)


WORDS = [
    # 0: prologue; idled, nothing is stored
    Word(
        ctl_lo=CtlLo(f7_8=0x1, f15_18=0xA),
        regs_a=RegsA(f105_109=16, xreg=0, f120_122=0x4, f128_132=0),
        regs_b=RegsB(f224_228=31),
        c2=C2(f229=0x0, f265_268=0x0),
        ctl_hi=CtlHi(f302_307=0x13, f308_309=0x0, f310_312=0x5, f313_319=0x3),
    ),
    # 1: prologue
    Word(
        ctl_lo=CtlLo(f15_18=0x5),
        regs_a=RegsA(f120_122=0x0, f128_132=0),
        regs_b=RegsB(f224_228=1),
        c2=C2(f272_273=0x1),
        ctl_hi=CtlHi(f313_319=0xB),
    ),
    # 2: prologue
    Word(
        regs_a=RegsA(f120_122=0x1, f128_132=0),
        c2=C2(f272_273=0x1),
        ctl_hi=CtlHi(f313_319=0xB),
    ),
    # 3: no-op
    IDLE,
    # 4: no-op
    IDLE,
    # 5: prologue; idled, the job never completes
    Word(ctl_hi=CtlHi(f313_319=0x7)),
    # 6: no-op
    IDLE,
    # 7: no-op
    IDLE,
    # 8: no-op
    IDLE,
    # 9: no-op
    IDLE,
    # 10: no effect seen when idled
    Word(
        regs_a=RegsA(f105_109=0, f128_132=0),
        regs_b=RegsB(f206_210=1, f211_215=31, f224_228=2),
        c2=C2(f265_268=0xC, f272_273=0x1),
        ctl_hi=CtlHi(f302_307=0x15, f313_319=0x9),
    ),
    # 11: no effect seen when idled; control signature
    Word(
        ctl_lo=CtlLo(f15_18=0xC),
        regs_a=RegsA(f100_104=1, f105_109=0, f120_122=0x0, f123_127=2, f128_132=0),
        regs_b=RegsB(f211_215=31),
        c2=C2(f265_268=0xC, f272_273=0x1),
        ctl_hi=CtlHi(f302_307=0x2, f313_319=0x1E),
    ),
    # 12: store path
    Word(
        ctl_lo=CtlLo(f19_22=0x6),
        regs_a=RegsA(
            f100_104=1, f105_109=0, xreg=0, f120_122=0x0, f123_127=2, f128_132=0
        ),
        regs_b=RegsB(f206_210=4, f211_215=13, f216_220=10, f224_228=5),
        c2=C2(f265_268=0x4, f269=0x0, f272_273=0x0),
        ctl_hi=CtlHi(f302_307=0x14, f310_312=0x4, f313_319=0xB),
    ),
    # 13: store path
    Word(
        ctl_lo=CtlLo(f2_6=0x10, f7_8=0x1, f15_18=0x1),
        regs_a=RegsA(f100_104=1, f105_109=0, xreg=0, f123_127=5, f128_132=0),
        regs_b=RegsB(f206_210=4, f211_215=8, f216_220=9, f224_228=5),
        c2=C2(f265_268=0x3, f269=0x0),
        ctl_hi=CtlHi(f302_307=0x14, f308_309=0x0, f310_312=0x5, f313_319=0x1A),
    ),
    # 14: store path
    Word(
        ctl_lo=CtlLo(f15_18=0x1),
        regs_a=RegsA(
            f100_104=1, f105_109=8, xreg=0, f120_122=0x0, f123_127=5, f128_132=0
        ),
        regs_b=RegsB(f206_210=1, f211_215=4, f216_220=7, f224_228=2),
        c2=C2(f265_268=0x6, f269=0x0, f272_273=0x0),
        ctl_hi=CtlHi(f302_307=0x14, f308_309=0x0, f310_312=0x5, f313_319=0xB),
    ),
    # 15: store count and data: idled, the job stores 0x4000 zero bytes
    Word(
        ctl_lo=CtlLo(f2_6=0x8, f15_18=0x2),
        regs_a=RegsA(f100_104=1, f105_109=0, xreg=4, f123_127=5, f128_132=0),
        regs_b=RegsB(f206_210=1, f211_215=5, f216_220=6, f224_228=2),
        c2=C2(f265_268=0x3, f269=0x0),
        ctl_hi=CtlHi(f302_307=0x14, f310_312=0x4, f313_319=0x1A),
    ),
    # 16: store path
    Word(
        regs_a=RegsA(f100_104=1, f105_109=0, f123_127=5, f128_132=0),
        regs_b=RegsB(f206_210=2, f211_215=5, f216_220=6, f224_228=3),
        c2=C2(f265_268=0xE, f272_273=0x1),
    ),
    # 17: a buffer address, load or store side (hypothesis)
    Word(
        ctl_lo=CtlLo(f1=0x1, f2_6=0x7, f15_18=0xF),
        regs_a=RegsA(f100_104=2, f105_109=0, f120_122=0x0, f123_127=3, f128_132=0),
        regs_b=RegsB(f206_210=2, f211_215=5, f216_220=6, f224_228=4),
        c2=C2(f265_268=0xE, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x3, f313_319=0x38),
    ),
    # 18: no effect seen when idled
    Word(
        regs_a=RegsA(f100_104=2, f105_109=2, f123_127=3, f128_132=0),
        regs_b=RegsB(f206_210=5, f211_215=5, f216_220=6, f224_228=3),
        c2=C2(f265_268=0xE, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0xF),
    ),
    # 19: no effect seen when idled
    Word(
        ctl_lo=CtlLo(f15_18=0xF),
        regs_a=RegsA(f100_104=2, f105_109=2, f120_122=0x0, f123_127=3, f128_132=0),
        regs_b=RegsB(f206_210=3, f211_215=5, f216_220=6, f224_228=3),
        c2=C2(f259_260=0x1, f265_268=0xE),
        ctl_hi=CtlHi(f313_319=0x38),
        **route(19),
    ),
    # 20: no effect seen when idled
    Word(
        regs_a=RegsA(f100_104=2, f105_109=2, f123_127=3, f128_132=0),
        regs_b=RegsB(f206_210=2, f211_215=5, f216_220=6, f224_228=3),
        c2=C2(f265_268=0xC, f274_276=0x1, f285_287=0x4, f288_291=0xA),
        **route(20),
    ),
    # 21: store path
    Word(
        regs_a=RegsA(f100_104=5, f105_109=2, f123_127=3, f128_132=0),
        regs_b=RegsB(f206_210=2, f211_215=5, f216_220=6, f224_228=3),
        c2=C2(f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x10),
        **route(21),
    ),
    # 22: store path
    Word(
        ctl_lo=CtlLo(f15_18=0x4),
        regs_a=RegsA(f100_104=5, f105_109=2, f123_127=3, f128_132=0),
        regs_b=RegsB(f206_210=2, f211_215=1, f216_220=6, f224_228=3),
        c2=C2(f265_268=0x7, f272_273=0x1),
        ctl_hi=CtlHi(f313_319=0x3F),
        **route(22),
    ),
    # 23: store path
    Word(
        regs_a=RegsA(f100_104=5, f105_109=2, f123_127=3, f128_132=3),
        regs_b=RegsB(f206_210=2, f211_215=1, f216_220=6, f224_228=3),
        c2=C2(f265_268=0xE),
        ctl_hi=CtlHi(f313_319=0x9),
        **route(23),
    ),
    # 24: store path
    Word(
        ctl_lo=CtlLo(f1=0x1, f2_6=0x1F),
        regs_a=RegsA(f100_104=2, f105_109=15, f123_127=5, f128_132=0),
        regs_b=RegsB(f206_210=2, f211_215=1, f216_220=6, f224_228=5),
        c2=C2(f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x3, f313_319=0x1A),
        **route(24),
    ),
    # 25: no effect seen when idled
    Word(
        ctl_lo=CtlLo(f15_18=0xA),
        regs_a=RegsA(f100_104=2, f105_109=2, f123_127=5, f128_132=3),
        regs_b=RegsB(f206_210=2, f211_215=2, f216_220=6, f224_228=5),
        c2=C2(f265_268=0x6),
        ctl_hi=CtlHi(f313_319=0x3F),
        **route(25),
    ),
    # 26: no effect seen when idled
    Word(
        regs_a=RegsA(f100_104=4, f105_109=2, f123_127=5, f128_132=3),
        regs_b=RegsB(f206_210=2, f211_215=2, f216_220=6, f224_228=7),
        c2=C2(f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0xB),
        **route(26),
    ),
    # 27: a load address (hypothesis)
    Word(
        ctl_lo=CtlLo(f15_18=0xF, f19_22=0x4),
        regs_a=RegsA(f100_104=4, f105_109=2, f120_122=0x4, f123_127=7, f128_132=0),
        regs_b=RegsB(f206_210=4, f211_215=2, f216_220=6, f224_228=5),
        c2=C2(f229=0x0, f265_268=0xE, f272_273=0x1),
        ctl_hi=CtlHi(f313_319=0x1),
        **route(27),
    ),
    # 28: needed for completion
    Word(
        ctl_lo=CtlLo(f15_18=0xF),
        regs_a=RegsA(
            f100_104=4,
            f105_109=2,
            f110_114=4,
            xreg=2,
            f120_122=0x0,
            f123_127=5,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=5, f211_215=2, f216_220=6, f224_228=5),
        c2=C2(f265_268=0xE, f272_273=0x0),
        ctl_hi=CtlHi(f310_312=0x2, f313_319=0x38),
        **route(28),
    ),
    # 29: no effect seen when idled
    Word(
        regs_a=RegsA(
            f100_104=5, f105_109=3, f110_114=4, xreg=2, f123_127=5, f128_132=3
        ),
        regs_b=RegsB(f206_210=5, f211_215=2, f216_220=6, f224_228=6),
        c2=C2(f261_262=0x0, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x11),
        **route(29),
    ),
    # 30: no effect seen when idled
    Word(
        ctl_lo=CtlLo(f19_22=0x1),
        regs_a=RegsA(
            f100_104=5,
            f105_109=3,
            f110_114=4,
            xreg=2,
            f120_122=0x0,
            f123_127=6,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=3, f211_215=2, f216_220=6, f224_228=8),
        c2=C2(f265_268=0xE, f272_273=0x2, f274_276=0x2, f292_295=0xE),
        ctl_hi=CtlHi(f313_319=0x38),
        **route(30),
    ),
    # 31: store-side buffer address, paired with 17 (hypothesis)
    Word(
        regs_a=RegsA(
            f100_104=4, f105_109=3, f110_114=4, xreg=2, f123_127=6, f128_132=3
        ),
        regs_b=RegsB(f206_210=3, f211_215=2, f216_220=6, f224_228=6),
        c2=C2(f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x11),
        **route(31),
    ),
    # 32: store path
    Word(
        ctl_lo=CtlLo(f15_18=0xF),
        regs_a=RegsA(
            f100_104=4,
            f105_109=0,
            f110_114=0,
            xreg=9,
            f120_122=0x0,
            f123_127=8,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=3, f211_215=2, f216_220=12, f224_228=7),
        c2=C2(f265_268=0xE, f269=0x0, f272_273=0x1),
        ctl_hi=CtlHi(f302_307=0x14, f308_309=0x0, f313_319=0x38),
        **route(32),
    ),
    # 33: store path; control signature
    Word(
        ctl_lo=CtlLo(f2_6=0x3, f15_18=0xF),
        regs_a=RegsA(
            f100_104=4,
            f105_109=1,
            f110_114=0,
            xreg=8,
            f120_122=0x2,
            f123_127=7,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=5, f211_215=2, f216_220=11, f224_228=8),
        c2=C2(f265_268=0xE, f269=0x0, f272_273=0x1),
        ctl_hi=CtlHi(f302_307=0x14, f308_309=0x0, f313_319=0x1F),
        **route(33),
    ),
    # 34: store path
    Word(
        ctl_lo=CtlLo(f15_18=0xF, f19_22=0x1),
        regs_a=RegsA(
            f100_104=4, f105_109=3, f110_114=0, xreg=10, f123_127=8, f128_132=3
        ),
        regs_b=RegsB(f206_210=5, f211_215=2, f216_220=11, f224_228=8),
        c2=C2(f272_273=0x1),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x3E),
        **route(34),
    ),
    # 35: store path
    Word(
        ctl_lo=CtlLo(f15_18=0xC, f19_22=0x1),
        regs_a=RegsA(
            f100_104=4, f105_109=3, f110_114=0, xreg=9, f123_127=8, f128_132=3
        ),
        regs_b=RegsB(f206_210=4, f211_215=2, f216_220=11, f224_228=8),
        c2=C2(f265_268=0xE),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x3F),
        **route(35),
    ),
    # 36: store path
    Word(
        ctl_lo=CtlLo(f15_18=0x8),
        regs_a=RegsA(
            f100_104=4, f105_109=3, f110_114=0, xreg=7, f123_127=7, f128_132=3
        ),
        regs_b=RegsB(f206_210=4, f211_215=2, f216_220=11, f224_228=7),
        c2=C2(f272_273=0x1),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x40),
        **route(36),
    ),
    # 37: store path
    Word(
        ctl_lo=CtlLo(f19_22=0x1),
        regs_a=RegsA(
            f100_104=5,
            f105_109=4,
            f110_114=0,
            xreg=10,
            f120_122=0x0,
            f123_127=7,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=4, f211_215=2, f216_220=11, f224_228=7),
        c2=C2(f265_268=0x7, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x11, f308_309=0x0, f313_319=0x37),
        **route(37),
    ),
    # 38: store path
    Word(
        ctl_lo=CtlLo(f15_18=0x4),
        regs_a=RegsA(
            f100_104=5, f105_109=4, f110_114=0, xreg=2, f123_127=7, f128_132=3
        ),
        regs_b=RegsB(f206_210=4, f211_215=2, f216_220=11, f224_228=7),
        c2=C2(f265_268=0x7),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x3E),
        **route(38),
    ),
    # 39: store path
    Word(
        ctl_lo=CtlLo(f19_22=0xF),
        regs_a=RegsA(f100_104=5, f105_109=4, f110_114=0, xreg=2, f123_127=6),
        regs_b=RegsB(f206_210=4, f211_215=3, f216_220=11, f224_228=6),
        c2=C2(f265_268=0x7, f272_273=0x1),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x38),
        **route(39),
    ),
    # 40: store path
    Word(
        ctl_lo=CtlLo(f19_22=0xF),
        regs_a=RegsA(f100_104=5, f105_109=4, f110_114=0, xreg=4, f123_127=4),
        regs_b=RegsB(f206_210=4, f211_215=2, f216_220=11, f224_228=4),
        c2=C2(f265_268=0x7, f272_273=0x1),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x38),
        **route(40),
    ),
    # 41: store path
    Word(
        ctl_lo=CtlLo(f15_18=0x6),
        regs_a=RegsA(
            f100_104=5, f105_109=4, f110_114=0, xreg=13, f123_127=2, f128_132=3
        ),
        regs_b=RegsB(f206_210=4, f211_215=13, f216_220=11, f224_228=4),
        c2=C2(f265_268=0x7),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x40),
        **route(41),
    ),
    # 42: completion; its register enables the store
    xfer(42, CTX42, xreg=8),
    # 43: completion
    xfer(43, CTX42, xreg=10),
    # 44: completion
    xfer(44, CTX42, xreg=9),
    # 45: completion
    Word(
        ctl_lo=CtlLo(f15_18=0xA, f19_22=0x3),
        regs_a=RegsA(
            f100_104=5,
            f105_109=4,
            f110_114=0,
            xreg=7,
            f120_122=0x4,
            f123_127=5,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=4, f211_215=13, f216_220=11, f224_228=4),
        c2=C2(f229=0x0),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x1),
        **route(45),
    ),
    # 46: completion
    xfer(46, CTX46, xreg=10),
    # 47: completion; the store's element format
    xfer(47, CTX46, xreg=6),
    # 48: completion
    xfer(48, CTX46, xreg=5),
    # 49: completion
    xfer(49, CTX46, xreg=4),
    # 50: no effect seen when idled
    Word(
        ctl_lo=CtlLo(f1=0x1),
        regs_a=RegsA(
            f100_104=1,
            f105_109=0,
            f110_114=0,
            xreg=12,
            f120_122=0x0,
            f123_127=3,
            f128_132=4,
        ),
        regs_b=RegsB(f206_210=1, f211_215=1, f216_220=11, f224_228=3),
        c2=C2(f265_268=0x4, f272_273=0x1),
        ctl_hi=CtlHi(f302_307=0x3, f308_309=0x0, f313_319=0x1E),
        **route(50),
    ),
    # 51: no effect seen when idled
    Word(
        regs_a=RegsA(
            f100_104=1, f105_109=2, f110_114=0, xreg=3, f123_127=3, f128_132=3
        ),
        regs_b=RegsB(f206_210=1, f211_215=1, f216_220=11, f224_228=2),
        c2=C2(f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0xB, f308_309=0x0),
        **route(51),
    ),
    # 52: no effect seen when idled
    Word(
        regs_a=RegsA(
            f100_104=1,
            f105_109=2,
            f110_114=0,
            xreg=13,
            f120_122=0x2,
            f123_127=6,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=1, f211_215=3, f216_220=11, f224_228=6),
        c2=C2(f265_268=0x7, f272_273=0x1),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x1F),
        **route(52),
    ),
    # 53: no effect seen when idled
    Word(
        ctl_lo=CtlLo(f15_18=0x2, f19_22=0x3),
        regs_a=RegsA(
            f100_104=1,
            f105_109=2,
            f110_114=0,
            xreg=13,
            f120_122=0x4,
            f123_127=2,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=1, f211_215=3, f216_220=11, f224_228=6),
        c2=C2(f229=0x0),
        ctl_hi=CtlHi(f308_309=0x1, f313_319=0x2),
        **route(53),
    ),
    # 54: no effect seen when idled
    xfer(54, CTX54, xreg=11),
    # 55: no effect seen when idled
    xfer(55, CTX54, xreg=13),
    # 56: no effect seen when idled
    Word(
        regs_a=RegsA(
            f100_104=1,
            f105_109=2,
            f110_114=0,
            xreg=2,
            f120_122=0x2,
            f123_127=4,
            f128_132=0,
        ),
        regs_b=RegsB(f206_210=1, f211_215=2, f216_220=11, f224_228=4),
        c2=C2(f265_268=0x7, f272_273=0x1),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x1F),
        **route(56),
    ),
    # 57: no effect seen when idled
    xfer(57, CTX57, xreg=2, f308_309=1),
    # 58: a load address with fixed content
    Word(
        ctl_lo=CtlLo(f7_8=0x1, f9_14=0x8, f15_18=0x4),
        regs_a=RegsA(
            f100_104=1, f105_109=0, f110_114=0, xreg=24, f123_127=3, f128_132=3
        ),
        regs_b=RegsB(f206_210=1, f211_215=4, f216_220=5, f224_228=4),
        c2=C2(f265_268=0x2, f269=0x0),
        ctl_hi=CtlHi(f302_307=0x14, f310_312=0x4, f313_319=0x40),
        **route(58),
    ),
    # 59: no effect seen when idled
    Word(
        ctl_lo=CtlLo(f15_18=0x1, f19_22=0x2),
        regs_a=RegsA(
            f100_104=3,
            f105_109=1,
            f110_114=0,
            xreg=4,
            f120_122=0x1,
            f123_127=3,
            f128_132=16,
        ),
        regs_b=RegsB(f206_210=1, f211_215=7, f216_220=5, f224_228=2),
        c2=C2(f252_255=0x7, f265_268=0x7, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x11, f308_309=0x0, f313_319=0xB),
        **route(59),
    ),
    # 60: stream or completion
    Word(
        regs_a=RegsA(
            f100_104=3, f105_109=1, f110_114=0, xreg=0, f123_127=3, f128_132=2
        ),
        regs_b=RegsB(f206_210=1, f211_215=6, f216_220=5, f224_228=2),
        c2=C2(f265_268=0x4, f272_273=0x0, f274_276=0x0, f278_280=0x2, f281_284=0xA),
        ctl_hi=CtlHi(f308_309=0x0, f310_312=0x5, f313_319=0x9),
        **route(60),
    ),
    # 61: source load: without it the job loads no new source
    Word(
        ctl_lo=CtlLo(f1=0x1, f2_6=0xF, f15_18=0x6),
        regs_a=RegsA(
            f100_104=1, f105_109=0, f110_114=0, xreg=1, f123_127=2, f128_132=2
        ),
        regs_b=RegsB(f206_210=1, f211_215=4, f216_220=5, f224_228=2),
        c2=C2(f265_268=0x5, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x3, f308_309=0x1, f310_312=0x5, f313_319=0x40),
        **route(61),
    ),
    # 62: must not be idled
    Word(
        ctl_lo=CtlLo(f2_6=0x3, f7_8=0x3, f9_14=0x3, f15_18=0x5),
        regs_a=RegsA(
            f100_104=1, f105_109=1, f110_114=6, xreg=0, f123_127=2, f128_132=2
        ),
        regs_b=RegsB(f206_210=1, f211_215=8, f216_220=6, f224_228=2),
        c2=C2(f265_268=0x7, f269=0x0, f272_273=0x0),
        ctl_hi=CtlHi(f302_307=0x14, f310_312=0x0, f313_319=0x3F),
        **route(62),
    ),
    # 63: stream or completion
    Word(
        ctl_lo=CtlLo(f1=0x1, f15_18=0x4),
        regs_a=RegsA(
            f100_104=1, f105_109=0, f110_114=0, xreg=8, f123_127=2, f128_132=2
        ),
        regs_b=RegsB(f206_210=1, f211_215=8, f216_220=6, f224_228=2),
        c2=C2(f265_268=0x7, f272_273=0x2),
        ctl_hi=CtlHi(f302_307=0x3, f308_309=0x0, f313_319=0x3F),
        **route(63),
    ),
    # 64: must not be idled
    Word(
        ctl_lo=CtlLo(f15_18=0x1),
        regs_a=RegsA(
            f100_104=1, f105_109=1, f110_114=0, xreg=7, f123_127=2, f128_132=2
        ),
        regs_b=RegsB(f206_210=1, f211_215=7, f216_220=6, f224_228=2),
        c2=C2(f265_268=0x7),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0x3F),
        **route(64),
    ),
    # 65: stream or completion
    Word(
        ctl_lo=CtlLo(f15_18=0x1, f19_22=0x6),
        regs_a=RegsA(
            f100_104=1,
            f105_109=1,
            f110_114=0,
            xreg=8,
            f120_122=0x1,
            f123_127=2,
            f128_132=16,
        ),
        regs_b=RegsB(f206_210=1, f211_215=8, f216_220=6, f224_228=2),
        c2=C2(f265_268=0x7),
        ctl_hi=CtlHi(f308_309=0x0, f313_319=0xB),
        **route(65),
    ),
    # 66: load enable: its register decides whether the load runs
    xfer(66, CTX66, xreg=5),
    # 67: load address: r3 = the job's src
    xfer(67, CTX66, xreg=3),
    # 68: load-side buffer offset
    xfer(68, CTX66, xreg=4),
    # 69: store side
    Word(
        ctl_lo=CtlLo(f7_8=0x1, f9_14=0x18),
        regs_a=RegsA(
            f100_104=1, f105_109=1, f110_114=0, xreg=24, f123_127=2, f128_132=2
        ),
        regs_b=RegsB(f206_210=1, f211_215=3, f216_220=6, f224_228=2),
        c2=C2(f265_268=0x3),
        ctl_hi=CtlHi(f310_312=0x4),
        **route(69),
    ),
    # 70: stream setup
    xfer(70, CTX70, xreg=3),
    # 71: stream
    xfer(71, CTX70, xreg=7),
    # 72: must not be idled
    xfer(72, CTX70, xreg=8),
    # 73: store count: r1 = len
    xfer(73, CTX70, xreg=1),
    # 74: no effect seen when idled
    xfer(74, CTX70, xreg=1, f308_309=1),
    # 75: stream
    xfer(75, CTX70, xreg=6),
    # 76: store-side buffer offset, the pair of 68
    xfer(76, CTX70, xreg=4),
    # 77: needs its register r2
    xfer(77, CTX70, xreg=2),
    # 78: no effect seen when idled
    xfer(78, CTX70, xreg=2, f308_309=1),
    # 79: stream end
    Word(
        regs_a=RegsA(
            f100_104=0, f105_109=0, f110_114=0, xreg=2, f123_127=2, f128_132=1
        ),
        regs_b=RegsB(f211_215=3, f216_220=6, f224_228=2),
        c2=C2(f229=0x0, f265_268=0xC),
        ctl_hi=CtlHi(f302_307=0x3, f313_319=0x4),
        **route(79),
    ),
    # 80: control signature
    Word(
        ctl_lo=CtlLo(f15_18=0xC),
        regs_a=RegsA(
            f100_104=0,
            f105_109=1,
            f110_114=0,
            xreg=2,
            f120_122=0x0,
            f123_127=0,
            f128_132=0,
        ),
        regs_b=RegsB(f211_215=3, f216_220=6),
        c2=C2(f272_273=0x1),
        ctl_hi=CtlHi(f313_319=0x1F),
        **route(80),
    ),
    # 81: drain
    Word(
        regs_a=RegsA(
            f100_104=0, f105_109=1, f110_114=0, xreg=2, f123_127=0, f128_132=1
        ),
        regs_b=RegsB(f211_215=3, f216_220=6),
        **route(81),
    ),
    # 82: drain
    Word(
        regs_a=RegsA(
            f100_104=0, f105_109=1, f110_114=0, xreg=2, f123_127=0, f128_132=1
        ),
        regs_b=RegsB(f211_215=3, f216_220=6),
        **route(82),
    ),
    # 83: drain
    Word(
        regs_a=RegsA(
            f100_104=0, f105_109=1, f110_114=0, xreg=2, f123_127=0, f128_132=1
        ),
        regs_b=RegsB(f211_215=3, f216_220=6),
        **route(83),
    ),
]


def program() -> Program:
    return assemble(WORDS)
