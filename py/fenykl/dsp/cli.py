"""`fenykl dsp`: listings, DSL source and the field table."""

import argparse
from collections import Counter
from pathlib import Path

from ..firmware import TARGETS
from .disasm import listing, source
from .fields import FIELDS
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
