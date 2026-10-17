#!/usr/bin/env python3
"""Checks that mn10300-elf code only uses MN1030/MN103S instructions (no AM33/AM33-2/AM34).

  check_isa.py --workdir DIR [--fw-forms fw-forms.tsv] FILE...      (.o, .a or linked ELF)

1. Every ELF (archive member) must carry the base E_MN10300_MACH_MN10300 machine flag.
2. Every instruction objdump finds in the code sections is re-decoded by the ghidra-mn103 SLEIGH
   module (written from manual 13250-040E, no AM33 opcodes) and must decode with the same length
   and not as a UDF instruction (several AM33 encodings land in the MN103 UDF20-35 space). The
   firmware's multiply forms of udf00/udf01/udfu01 are allowed (inline asm, e.g. macbench).
3. Every instruction form is looked up in the stock firmware's form histogram (MN103Forms.java run
   over the E-PL3 Ghidra project). Forms the firmware never uses are listed for review; they are
   valid MN103 instructions but have no evidence of running on the E-PL3 silicon.

Exit status 1 if 1. or 2. fail. Needs GHIDRA_INSTALL_DIR, JAVA_HOME, the installed ghidra-mn103
extension and mn10300-elf-objdump/readelf on PATH (or MN103_BINUTILS=<prefix>/bin/mn10300-elf-).
"""

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANGUAGE = "MN103:LE:32:default"
FIRMWARE_UDF = {"udf00", "udf01", "udfu01"}
EF_MN10300_MACH = 0x00FF0000
E_MN10300_MACH_MN10300 = 0x00810000

OBJ_FILE = re.compile(r"^(\S+):\s+file format (\S+)$")
OBJ_SECTION = re.compile(r"^Disassembly of section (\S+):$")
OBJ_INSN = re.compile(r"^\s*([0-9a-f]+):\t([0-9a-f ]+?)\s*\t(\S+)(?:\t(.*))?$")
READELF_FILE = re.compile(r"^File: (\S+)$")
READELF_FLAGS = re.compile(r"^\s*Flags:\s+(0x[0-9a-f]+)")


@dataclass
class Insn:
    where: str  # file(member):section+offset
    raw: bytes
    text: str  # objdump text


@dataclass
class Form:
    count: int = 0
    example: str = ""


def tool(name: str) -> str:
    return os.environ.get("MN103_BINUTILS", "mn10300-elf-") + name


def check_machine(files: list[Path]) -> list[str]:
    out = subprocess.run([tool("readelf"), "-h", *map(str, files)], check=True, capture_output=True,
                         text=True).stdout
    errors = []
    current = str(files[0])
    for line in out.splitlines():
        if m := READELF_FILE.match(line):
            current = m.group(1)
        elif m := READELF_FLAGS.match(line):
            flags = int(m.group(1), 16)
            if flags & EF_MN10300_MACH != E_MN10300_MACH_MN10300:
                errors.append(f"{current}: e_flags {flags:#x} is not the base MN10300 machine")
    return errors


def disassemble(files: list[Path]) -> list[Insn]:
    out = subprocess.run([tool("objdump"), "-d", "-z", "--insn-width=8", *map(str, files)], check=True,
                         capture_output=True, text=True).stdout
    insns = []
    obj, section = "", ""
    for line in out.splitlines():
        if m := OBJ_FILE.match(line):
            obj = m.group(1)
        elif m := OBJ_SECTION.match(line):
            section = m.group(1)
        elif m := OBJ_INSN.match(line):
            raw = bytes.fromhex(m.group(2).replace(" ", ""))
            text = f"{m.group(3)} {m.group(4) or ''}".strip()
            insns.append(Insn(f"{obj}:{section}+{int(m.group(1), 16):#x}", raw, text))
    return insns


def ghidra_decode(insns: list[Insn], workdir: Path, headless: Path) -> dict[int, tuple[int, str, str]]:
    """Returns offset -> (length, ghidra text, form) for the blob of all instructions."""
    blob = workdir / "insns.bin"
    offsets = workdir / "offsets.txt"
    data = bytearray()
    starts = []
    for i in insns:
        starts.append(len(data))
        data += i.raw
    blob.write_bytes(bytes(data))
    offsets.write_text("".join(f"{s}\n" for s in starts))
    decoded = workdir / "ghidra-insns.tsv"
    proj = workdir / "proj"
    proj.mkdir(exist_ok=True)
    log = workdir / "headless.log"
    with log.open("w") as fh:
        subprocess.run([
            str(headless), str(proj), "isacheck",
            "-import", str(blob), "-overwrite", "-loader", "BinaryLoader", "-loader-baseAddr", "0",
            "-processor", LANGUAGE, "-noanalysis", "-scriptPath", str(HERE),
            "-postScript", "MN103Forms.java", str(workdir / "ghidra-forms.tsv"), str(offsets), str(decoded),
            "-deleteProject",
        ], check=True, stdout=fh, stderr=subprocess.STDOUT)
    if "SCRIPT ERROR" in log.read_text():
        sys.exit(f"error: MN103Forms.java failed, see {log}")
    result = {}
    for line in decoded.read_text().splitlines():
        off, length, text, form = line.split("\t")
        result[int(off)] = (int(length), text, form)
    return result


def load_fw_forms(path: Path) -> Counter:
    forms: Counter = Counter()
    for line in path.read_text().splitlines():
        form, length, count = line.split("\t")
        forms[f"{form}\t{length}"] = int(count)
    return forms


def firmware_udf(got: tuple, fw: Counter) -> bool:
    """udf00/udf01/udfu01 are the firmware's multiply unit (~12,000 uses, also on CPU1): allowed from
    inline asm in exactly the forms the firmware uses."""
    return got[2].split()[0] in FIRMWARE_UDF and fw[f"{got[2]}\t{got[0]}"] > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--fw-forms", type=Path, default=HERE / "fw-forms.tsv")
    ap.add_argument("files", type=Path, nargs="+")
    args = ap.parse_args()
    missing = [v for v in ("GHIDRA_INSTALL_DIR", "JAVA_HOME") if not os.environ.get(v)]
    if missing:
        sys.exit(f"error: the ISA check runs Ghidra and needs {', '.join(missing)}")
    headless = Path(os.environ["GHIDRA_INSTALL_DIR"]) / "support" / "analyzeHeadless"
    if not headless.is_file():
        sys.exit(f"error: {headless} missing, GHIDRA_INSTALL_DIR is not a Ghidra installation")
    args.workdir.mkdir(parents=True, exist_ok=True)

    errors = check_machine(args.files)
    insns = disassemble(args.files)
    if not insns:
        sys.exit("error: objdump found no instructions")
    decoded = ghidra_decode(insns, args.workdir, headless)
    fw = load_fw_forms(args.fw_forms)

    forms: dict[str, Form] = {}
    offset = 0
    for i in insns:
        got = decoded.get(offset)
        if got is None:
            errors.append(f"{i.where}: {i.raw.hex()} ({i.text}) is not an MN103 instruction")
        elif got[0] != len(i.raw):
            errors.append(f"{i.where}: {i.raw.hex()} ({i.text}) decodes as {got[1]} with length {got[0]}")
        elif got[2].startswith("udf") and not firmware_udf(got, fw):
            # GCC never emits UDF; AM33 encodings in the F9/FB/FD space decode as UDF20-35 on MN103.
            errors.append(f"{i.where}: {i.raw.hex()} ({i.text}) decodes as user-defined {got[1]}")
        else:
            key = f"{got[2]}\t{got[0]}"
            f = forms.setdefault(key, Form(example=f"{i.where} {i.raw.hex()} {got[1]}"))
            f.count += 1
        offset += len(i.raw)

    report = args.workdir / "forms.tsv"
    with report.open("w") as fh:
        fh.write("form\tlength\tcount\tfirmware_count\texample\n")
        for key, f in sorted(forms.items(), key=lambda kv: -kv[1].count):
            fh.write(f"{key}\t{f.count}\t{fw[key]}\t{f.example}\n")

    unseen = {k: f for k, f in forms.items() if fw[k] == 0}
    print(f"{len(insns)} instructions, {len(forms)} forms, {len(errors)} errors, "
          f"{len(unseen)} forms not used by the stock firmware (full table: {report})")
    for key, f in sorted(unseen.items()):
        print(f"  not in firmware: {key.replace(chr(9), ' /')} x{f.count}  e.g. {f.example}")
    for e in errors:
        print(f"ERROR {e}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
