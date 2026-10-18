#!/usr/bin/env python3
"""Converts the lvrsz probe dumps (LR<stage><job><sample>.BIN) into PNG files.

Jobs: F = FD frame 640x480 Y8, A/B = IPU Y8 160x120 / 320x240, C/D = IPU YUYV 160x120 / 320x240.
For A/B it also writes the CPU references of the same sample: a k x k box filter and point sampling
of F, and prints the mean absolute difference of each against the IPU output.

    python3 lvrsz_view.py <dump dir> <output dir>
"""

import argparse
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Job:
    width: int
    height: int
    yuyv: bool


JOBS = {
    "F": Job(640, 480, False),
    "A": Job(160, 120, False),
    "B": Job(320, 240, False),
    "C": Job(160, 120, True),
    "D": Job(320, 240, True),
}


def write_png(path: Path, width: int, height: int, rows: list[bytes], colour: bool) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2 if colour else 0, 0, 0, 0)
    raw = b"".join(b"\0" + r for r in rows)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw))
                     + chunk(b"IEND", b""))


def clamp(v: float) -> int:
    return 0 if v < 0 else 255 if v > 255 else int(v + 0.5)


def yuyv_rows(data: bytes, width: int, height: int) -> list[bytes]:
    """BT.601 full-range YUYV (Y0 U Y1 V) to RGB."""
    rows = []
    for y in range(height):
        line = data[y * width * 2:(y + 1) * width * 2]
        out = bytearray()
        for x in range(0, width, 2):
            y0, u, y1, v = line[2 * x:2 * x + 4]
            for yy in (y0, y1):
                out += bytes((clamp(yy + 1.402 * (v - 128)),
                              clamp(yy - 0.344 * (u - 128) - 0.714 * (v - 128)),
                              clamp(yy + 1.772 * (u - 128))))
        rows.append(bytes(out))
    return rows


def y8_rows(data: bytes, width: int, height: int) -> list[bytes]:
    return [data[y * width:(y + 1) * width] for y in range(height)]


def reference(fd: bytes, job: Job, box: bool) -> bytes:
    k = 640 // job.width
    out = bytearray()
    for y in range(job.height):
        for x in range(job.width):
            if not box:
                out.append(fd[y * k * 640 + x * k])
                continue
            acc = sum(fd[(y * k + dy) * 640 + x * k + dx] for dy in range(k) for dx in range(k))
            out.append((acc + k * k // 2) // (k * k))
    return bytes(out)


def mad(a: bytes, b: bytes, width: int, rows: int) -> float:
    n = width * rows
    return sum(abs(a[i] - b[i]) for i in range(n)) / n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dumps", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in args.dumps.iterdir() if p.name.upper().startswith("LR") and p.suffix.upper() == ".BIN")
    for path in files:
        tag = path.stem.upper()[2:]
        job = JOBS[tag[1]]
        data = path.read_bytes()
        need = job.width * job.height * (2 if job.yuyv else 1)
        if len(data) != need:
            print(f"{path.name}: {len(data)} bytes, expected {need}; skipped")
            continue
        rows = yuyv_rows(data, job.width, job.height) if job.yuyv else y8_rows(data, job.width, job.height)
        write_png(args.out / f"LR{tag}.png", job.width, job.height, rows, job.yuyv)
        if tag[1] not in "AB":
            continue
        fd_path = path.with_name(f"LR{tag[0]}F{tag[2]}{path.suffix}")
        if not fd_path.exists():
            continue
        fd = fd_path.read_bytes()
        valid_rows = 476 // (640 // job.width)  # FD row 479 is garbage
        for name, box in (("box", True), ("point", False)):
            ref = reference(fd, job, box)
            write_png(args.out / f"LR{tag}_{name}.png", job.width, job.height,
                      y8_rows(ref, job.width, job.height), False)
            print(f"{path.name}: MAD vs CPU {name} {mad(data, ref, job.width, valid_rows):.2f}")
    print(f"PNG files in {args.out}")


if __name__ == "__main__":
    main()
