"""Programs: word sequences with an optional constant block, and the five stock programs.

The boot loader copies five items of the main-b106 resource slot 5 (resource class 0x8007) to
DRAM; the driver points the engine's program register 0x9D200010 at one of them per command.
An item is a run of 64-byte slots, one word in the first 40 bytes of each (bytes 40..63 zero),
zero up to an optional constant block, then zero to its end. The constants are read only by the
CPU setup of the command, which copies them to DSP local memory 0x9D000000.

`assemble` takes statements: words, bundles and parts of `asm` (whose control op may name a
label), `Label`s and `Ctl`s, control ops whose target is a label.
"""

import struct
import warnings
from collections.abc import Iterable
from dataclasses import dataclass

from ..firmware import HEAD_TAIL_SIZE, identify, unpack_firmware
from .asm import Bundle, Part, bundle
from .fields import DELAY_SLOTS, FIELDS, WORD_BYTES, Confidence
from .word import IDLE, Word

SLOT_BYTES = 64
RESOURCE_BLOCK = "main-b106"
RESOURCE_SLOT = 5  # class 0x8007
DIR_ENTRY = struct.Struct("<IIII")  # flash address, size, 0, 0


class ProgramFormatError(ValueError):
    pass


class AssemblyError(ValueError):
    """A label defined twice or never."""


class HypothesisFieldWarning(UserWarning):
    """An assembled program sets fields whose decoding is only a hypothesis."""


@dataclass(frozen=True)
class StockProgram:
    item: int
    cmd: int
    dram: int
    """Where the boot loader copies the item."""
    size: int
    const_offset: int | None
    const_words: int
    fact: str


STOCK: dict[int, StockProgram] = {
    s.item: s
    for s in (
        StockProgram(
            7,
            0x6,
            0xA3222C00,
            0x18E00,
            0x18D40,
            38,
            "constants: 32 one-bit masks 0xFFFFFFFE << k (k = 0..31), then 6 bytes",
        ),
        StockProgram(
            13,
            0xC,
            0xA3240C00,
            0xB200,
            0xB000,
            32,
            "the resampler; constants: 16 phases x 4 s16 taps, each phase sums to 256",
        ),
        StockProgram(
            15,
            0xE,
            0xA324C400,
            0xA800,
            0xA680,
            32,
            "peaking; constants: 32 words of 2-bit lane masks",
        ),
        StockProgram(
            16,
            0xF,
            0xA3260C00,
            0x1600,
            None,
            0,
            "the copy: src, dst and len in local memory words 0..2",
        ),
        StockProgram(17, 0x10, 0xA3263C00, 0x12000, None, 0, "no constants"),
    )
}


@dataclass(frozen=True)
class Program:
    words: tuple[Word, ...]
    constants: bytes = b""

    def __len__(self) -> int:
        return len(self.words)

    def delay_slots(self) -> set[int]:
        """The indices of the words in the delay slots of a control word (`DELAY_SLOTS`)."""
        return {
            j
            for i, w in enumerate(self.words)
            if w.control
            for j in range(i + 1, min(i + DELAY_SLOTS + 1, len(self.words)))
        }

    def local_op3(self) -> set[int]:
        """The indices of the local op 3 words: forward, after word 0, with no return (op 4)
        between the word and T. Word 0 calls the job routine although no return lies before its
        T; whether a local op 3 is a call or a jump is open."""
        local = set()
        for i, w in enumerate(self.words):
            if w.op != 3 or i == 0 or w.target <= i:
                continue
            if all(v.op != 4 for v in self.words[i + 1 : w.target]):
                local.add(i)
        return local


def decode_item(data: bytes, spec: StockProgram) -> Program:
    """The program in item bytes `data`; raises ProgramFormatError if the layout is off."""
    if len(data) != spec.size:
        raise ProgramFormatError(
            f"item {spec.item}: {len(data):#x} bytes, expected {spec.size:#x}"
        )
    end = len(data) if spec.const_offset is None else spec.const_offset
    const_end = end + 4 * spec.const_words
    if any(data[const_end:]):
        raise ProgramFormatError(
            f"item {spec.item}: non-zero bytes after {const_end:#x}"
        )
    slots = [data[o : o + SLOT_BYTES] for o in range(0, end, SLOT_BYTES)]
    while slots and not any(slots[-1]):
        slots.pop()
    for index, slot in enumerate(slots):
        if any(slot[WORD_BYTES:]):
            raise ProgramFormatError(
                f"item {spec.item}: slot {index} has bytes past the word"
            )
    words = tuple(Word.from_bytes(slot[:WORD_BYTES]) for slot in slots)
    return Program(words, data[end:const_end])


def encode_item(program: Program, spec: StockProgram) -> bytes:
    """The item bytes of `program` in the layout of `spec`."""
    code = b"".join(
        w.to_bytes() + bytes(SLOT_BYTES - WORD_BYTES) for w in program.words
    )
    end = spec.size if spec.const_offset is None else spec.const_offset
    if len(code) > end or len(program.constants) != 4 * spec.const_words:
        raise ProgramFormatError(f"program does not fit the layout of item {spec.item}")
    data = code.ljust(end, b"\0") + program.constants
    return data.ljust(spec.size, b"\0")


def stock_items(image: bytes) -> dict[int, bytes]:
    """The raw bytes of the five program items of a stock E-M10 II `.BIN`."""
    target = identify(image)
    if target is None or target.name != "em10ii":
        raise ProgramFormatError("not the stock E-M10 II v1.4 image")
    firmware = unpack_firmware(image)
    block = firmware.blocks[firmware.find_block(RESOURCE_BLOCK)]
    body, base = block.body, block.header.load_address + HEAD_TAIL_SIZE
    nested, _, _, _ = DIR_ENTRY.unpack_from(body, RESOURCE_SLOT * DIR_ENTRY.size)
    directory = nested - base + HEAD_TAIL_SIZE
    items = {}
    for item in STOCK:
        addr, size, _, _ = DIR_ENTRY.unpack_from(
            body, directory + item * DIR_ENTRY.size
        )
        items[item] = body[addr - base : addr - base + size]
    return items


def stock_programs(image: bytes) -> dict[int, Program]:
    """The five stock programs by item number."""
    return {
        item: decode_item(data, STOCK[item])
        for item, data in stock_items(image).items()
    }


@dataclass(frozen=True)
class Label:
    """Marks the statement after it; a `Ctl` names it as its target."""

    name: str


@dataclass(frozen=True)
class Ctl:
    """Control op 0..3 `op` to label `to`: `Word.ctl(op, index of to, base)` once assembled."""

    op: int
    to: str
    base: Word = IDLE


Statement = Word | Bundle | Part | Label | Ctl


def labels(stmts: Iterable[Statement], origin: int = 0) -> dict[str, int]:
    """Each label's word index: `origin` plus the number of words before it."""
    names: dict[str, int] = {}
    index = origin
    for s in stmts:
        if not isinstance(s, Label):
            index += 1
            continue
        if s.name in names:
            raise AssemblyError(f"label {s.name!r} defined twice")
        names[s.name] = index
    return names


def assemble(
    stmts: Iterable[Statement], constants: bytes = b"", *, origin: int = 0
) -> Program:
    """A program from DSL statements placed at word index `origin`, in two passes: the labels'
    indices, then the words, each `Ctl` and bundle with a label targeting its label's index (a
    target over 13 bits raises ValueError). A word's own target stays as it is, absolute. Checks every word and warns once
    about hypothesis fields."""
    stmts = tuple(stmts)
    names = labels(stmts, origin)
    words = []
    for s in stmts:
        if isinstance(s, Label):
            continue
        if isinstance(s, Ctl):
            if s.to not in names:
                raise AssemblyError(f"label {s.to!r} is not defined")
            s = Word.ctl(s.op, names[s.to], s.base)
        elif isinstance(s, (Bundle, Part)):
            s = bundle(s)
            if s.label is not None and s.label not in names:
                raise AssemblyError(f"label {s.label!r} is not defined")
            s = s.word(names)
        words.append(s)
    weak = set()
    for word in words:
        word.check()
        weak.update(
            f.id for f, _ in word.changed(IDLE) if f.confidence is Confidence.HYPOTHESIS
        )
    if weak:
        ids = [f.id for f in FIELDS if f.id in weak]
        warnings.warn(
            f"the program sets {len(ids)} hypothesis fields: {', '.join(ids)}",
            HypothesisFieldWarning,
            stacklevel=2,
        )
    return Program(tuple(words), constants)


def call_return_errors(program: Program) -> list[str]:
    """Report each op 3 whose return point, i + 5 after its delay slots (the call reading), is
    no word of `program`; the return is implicit, so no label can move it."""
    return [
        f"op 3 at word {i} returns to {i + DELAY_SLOTS + 1}, past the end ({len(program)} words)"
        for i, w in enumerate(program.words)
        if w.op == 3 and i + DELAY_SLOTS + 1 >= len(program)
    ]
