from functools import cache

from conftest import stock_image

from fenykl.dsp.emu import Boot, back_firmware
from fenykl.dsp.fields import Confidence
from fenykl.dsp.machine import Carry, Sv
from fenykl.dsp.program import Program, stock_programs
from fenykl.dsp.stock import commands
from fenykl.dsp.stock.registers import (
    R14_BELOW,
    R14_EXACT,
    BootParams,
    JobView,
    machine_start,
)
from fenykl.dsp.word import Word

SRC, DST, LEN = 0x10000000, 0x10100000, 0x400
DATA = bytes(range(256)) * 4
EXACT = BootParams(R14_EXACT, 0x60E58B00)


@cache
def p16() -> Program:
    return stock_programs(stock_image("em10ii"))[16]


def with_reg(word: int, reg: int) -> Program:
    words = list(p16().words)
    words[word] = Word.from_fields({"xreg": reg}, base=words[word])
    return Program(tuple(words))


def run(program: Program, params: BootParams | None):
    """The job on a fresh boot backed by the firmware, its start registers preset from `params`
    (none without); returns the boot, the run and dst."""
    boot = Boot()
    back_firmware(boot, stock_image("em10ii"))
    boot.put(SRC, DATA, "src")
    regs = machine_start(JobView(SRC, DST, LEN, params), p16()) if params else {}
    r = boot.run(
        program, regs=regs, request=commands.copy(program, SRC, DST, LEN).local
    )
    return boot, r, boot.read(DST, LEN)


def test_the_presets_are_the_facts_no_stock_word_writes():
    regs = machine_start(JobView(SRC, DST, LEN, EXACT), p16())["B"]
    # r14 -0x1C00 in 16-byte units; the firmware addresses as they are.
    assert regs == {
        14: Sv(-0x1C0 & 0xFFFFFFFF, Confidence.HYPOTHESIS),
        21: Sv(0x606CC000, Confidence.HYPOTHESIS),
        29: Sv(0x60E58B00, Confidence.HYPOTHESIS),
    }
    # An interval gives no value, nor does an r29 not known.
    below = machine_start(JobView(SRC, DST, LEN, BootParams(R14_BELOW)), p16())["B"]
    assert set(below) == {21}
    # A register the carry knows keeps its value.
    known = [Sv(None)] * 32
    known[21] = Sv(5)
    carry = Carry(registers={"B": known})
    kept = machine_start(JobView(SRC, DST, LEN, EXACT), p16(), carry)["B"]
    assert set(kept) == {14, 29}


def test_word_68_at_r14_places_the_load_from_the_boot_parameter():
    program = with_reg(68, 14)
    _, r, dst = run(program, None)
    assert "a memory offset unknown" in r.machine.stream.off
    assert not any(dst.known)
    # Placed at -0xE0, as the copy model has it.
    _, r, dst = run(program, EXACT)
    assert not r.machine.stream.off
    assert bytes(dst.data[: LEN - 0xE0]) == DATA[0xE0:]
    assert dst.weakest() is Confidence.HYPOTHESIS


def test_word_67_at_r21_loads_the_firmware_text_at_a_hypothesis():
    boot, _, dst = run(with_reg(67, 21), EXACT)
    assert all(dst.known)
    assert bytes(dst.data) == bytes(boot.read(0x606CC000, LEN).data)
    assert dst.weakest() is Confidence.HYPOTHESIS
