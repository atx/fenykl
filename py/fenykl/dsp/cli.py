"""`fenykl dsp`: listings, DSL source and the field table."""

import argparse
from collections import Counter
from pathlib import Path

from ..firmware import TARGETS
from .disasm import listing, source
from .fields import (
    DATA_OP_CONFIDENCE,
    DATA_OP_FACT,
    DATA_OPS,
    DELAY_FACT,
    DELAY_SLOTS,
    FAMILIES,
    FIELDS,
    FORMATS,
    IMM13,
    IMM16,
    MEANINGS,
    OP_FIELD,
    OPS,
    TARGET,
    TARGET_OPS,
)
from .program import STOCK, Program, stock_programs


def _program(args: argparse.Namespace) -> Program:
    from ..cli import read_bin  # the top-level cli imports this module

    return stock_programs(read_bin(args.file or TARGETS["em10ii"].stock_path()))[
        args.item
    ]


def cmd_disasm(args: argparse.Namespace) -> None:
    spec = STOCK[args.item]
    title = f"item {spec.item}, command {spec.cmd:#x}: {spec.fact}"
    print(listing(_program(args), title), end="")


def cmd_source(args: argparse.Namespace) -> None:
    print(source(_program(args), f"ITEM{args.item}"), end="")


def cmd_fields(args: argparse.Namespace) -> None:
    for f in FIELDS:
        bits = f"{f.lo}" if f.lo == f.hi else f"{f.lo}..{f.hi}"
        name = f" {f.name}" if f.name else ""
        print(
            f"{f.id:<9} {bits:>8}  {f.unit:<6} {f.kind:<6} {f.confidence.name.lower():<10} "
            f"idle {f.idle:#x}{name}: {f.fact}"
        )
    shift, parts = 0, []
    for f in TARGET.parts:
        parts.append(f.id if shift == 0 else f"{f.id} << {shift}")
        shift += f.width
    print(
        f"{TARGET.name}: {' | '.join(parts)} for {OP_FIELD.id} {TARGET_OPS.start}.."
        f"{TARGET_OPS.stop - 1}, {TARGET.confidence.name.lower()}: {TARGET.fact}"
    )
    data = f"{DATA_OPS.start}..{DATA_OPS.stop - 1}"
    rows = [(v, str(v), o.name, o.confidence, o.fact) for v, o in OPS.items()]
    rows.append((DATA_OPS.start, data, "opNN", DATA_OP_CONFIDENCE, DATA_OP_FACT))
    for _, value, name, confidence, fact in sorted(rows):
        print(
            f"{OP_FIELD.label} {value:<6} {name:<5} {confidence.name.lower():<10} {fact}"
        )
    for family in FAMILIES:
        regs = "ra, rb" if family.rb else "ra"
        imm = f", imm{family.imm_bits}" if family.imm_bits else ""
        ops = " ".join(f"{v:02x}" for v in family.ops)
        print(f"family {family.name:<2} {regs}{imm}  ops {ops}: {family.fact}")
    for value, fmt in FORMATS.items():
        if fmt.imm_bits != fmt.family.imm_bits or fmt.signed:
            sign = ", two's complement" if fmt.signed else ""
            print(f"  op{value:02x}: imm{fmt.imm_bits}{sign}")
    for composite in (IMM13, IMM16):
        print(f"{composite.name}: {composite.fact}")
    for value, meaning in MEANINGS.items():
        print(f"  op{value:02x}: {meaning}")
    print(f"delay slots: {DELAY_SLOTS}; {DELAY_FACT}")
    kinds = Counter(str(f.kind) for f in FIELDS)
    confidence = Counter(f.confidence.name.lower() for f in FIELDS)
    print(f"{len(FIELDS)} fields; {dict(kinds)}; {dict(confidence)}")


def _program_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("item", type=int, choices=sorted(STOCK), help="program item")
    parser.add_argument(
        "file",
        nargs="?",
        type=Path,
        metavar="FILE.BIN",
        help="stock E-M10 II firmware (default: its image in firmware/)",
    )


def add_parser(commands: argparse._SubParsersAction) -> None:
    dsp = commands.add_parser(
        "dsp", help="DSP microcode: listings, DSL source, field table"
    )
    dsp_commands = dsp.add_subparsers(required=True, metavar="COMMAND")
    disasm = dsp_commands.add_parser(
        "disasm", help="list a stock program's words by unit and field"
    )
    _program_args(disasm)
    disasm.set_defaults(func=cmd_disasm)
    src = dsp_commands.add_parser(
        "source",
        help="print a stock program as DSL source",
        description="Print a stock program as DSL source. The output reproduces stock "
        "microcode from your firmware image: for local work only, never commit it.",
    )
    _program_args(src)
    src.set_defaults(func=cmd_source)
    fields = dsp_commands.add_parser("fields", help="print the field table")
    fields.set_defaults(func=cmd_fields)
