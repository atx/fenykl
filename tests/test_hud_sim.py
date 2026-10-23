"""HUD library (sdk/lib/hud.c) in the GDB simulator (tests/sim/hud/): text rendered with
the stock English font must match the Python font decoder (fenykl.assets) placed with the firmware's
spacing rules, pixel for pixel, for every alignment, background and clipping case. In the LV style
the characters of the GUI's image fonts 12 and 16 (read from the firmware's own char -> image
tables) must be exactly the decoded stock images, and the other characters the font glyph scaled to
2/3, made bold and given the stock outline rule of sdk/lib/hud.c. test_hud.c checks the hook side (erase counting, hidden
states, the one-layer override, hide clears) in its exit status and dumps each rendered rectangle;
this file builds the font and image tables it serves and compares the dumps. test_hud_draw.c
decodes the exposure-row cells 1095-1098 with fenykl_hud_image and draws the focus meter's
10-cell row with fenykl_hud_draw; both dumps are compared with the Python decoder. The image table
holds the firmware's own encoded image bytes, read from the stock firmware at test time.
The text cases run for both cameras, each with its own stock font and live-view images (E-M10 II:
image fonts 14 and 25); test_hud_em10ii.c checks the E-M10 II visibility predicate (GUI state,
display request, output device, INFO screen property), the output device and the palette pick."""

import math
import struct
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest
from conftest import (
    DRAM_ALIAS,
    POWERON,
    SIM,
    assert_exit,
    needs_toolchain,
    region,
    run_in_simulator,
    stock_image,
)

from fenykl import sdk
from fenykl.assets import ASSET_LAYOUTS, Bitmap, common_resource, read_assets
from fenykl.firmware import identify, unpack_firmware

pytestmark = needs_toolchain

HUD = SIM / "hud"
FONT = "enu_id15_k24"
MARGIN = 2
LEFT, CENTER, RIGHT = range(3)
PLAIN, LV = range(2)
LV_MARGIN = 1
LV_SPACE = 11
LV_IMAGE_TOP = 1  # line row of the images' row 0
LV_IMAGE_WHITE = (0x06, 0x05, 0x04)  # the images' white ramp, drawn in ink[0..2]
LV_DROP = 3  # scaled baseline row 17 onto line row 20, the images' row 19
LV_SCALE = Fraction(
    2, 3
)  # letters: the font scaled down, row 25 (baseline) shifted to end row 17
PITCH, LV_PITCH = 32, 21  # line to line after a newline (dumped as "|")
BLACK, TRANSLUCENT, FAINT = 0x10, 0x11, 0x12
CLEAR = 0xFF
# Exposure-row cells by test_hud_draw.c's pattern letter; 'f' is the unlit cell,
# 1095 with its white fill 0x04 translucent
CELLS = {"W": 1095, "Y": 1096, "O": 1097, "G": 1098, "f": 1095}
CELL_PITCH, CELL_TOP = 8, 1
# images test_hud_draw.c refuses: a live-view digit (too large), the AF dot (fmt 0x02)
DIGIT, DOT = 1136, 969
# GUI image fonts of the LV style: u16 {char, image id} pairs, the digits' font and the one of '.'
# (E-PL3 gui_imgfont_tab 0x60351B9C [12], [16]; E-M10 II 0x615F3D30 [14], [25])
IMAGE_FONTS = {"epl3": (0x6F1B7908, 0x6F1B8908), "em10ii": (0x60D11CDC, 0x60D148DC)}
MEMORY = {
    "epl3": [
        DRAM_ALIAS,
        POWERON,
        region("0x6034d000,0x1000"),  # draw index, one-layer override, hide mask
        region(
            "0x6066e000,0x1000"
        ),  # layer vram table, font resource, the fake osd_vram records
        region("0x60702000,0x1000"),  # GUI super state and state
        region("0x6f123000,0x1000"),  # layer bpp
        region("0xa0800000,0x200000"),  # the two layer-1 buffers
        region("0xaf966000,0x1000"),  # common resource header: image table pointer
    ],
    # DRAM up to the GUI, display and property words and the two layer-1 buffers (0xa1e00000,
    # 0xa1f00000), with its uncached alias; the mock bodies
    "em10ii": [
        ("--memory-alias", "0x60000000,0x2000000,0xa0000000"),
        region("0x6eb00000,0x10000"),
    ],
}
SIM_START = {
    "epl3": SIM / "hooks" / "sim_start.S",
    "em10ii": SIM / "hooks_em10ii" / "sim_start.S",
}


@pytest.fixture(scope="module", params=["epl3", "em10ii"])
def camera(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def font(camera) -> dict[str, Bitmap]:
    """ASCII glyphs of the English font (the C table has 128 entries)."""
    glyphs = {
        chr(int(a.name.removeprefix("U+"), 16)): a.bitmap
        for a in read_assets(stock_image(camera))
        if a.kind == "glyph" and a.font == FONT
    }
    return {c: bitmap for c, bitmap in glyphs.items() if c < "\x80"}


@pytest.fixture(scope="module")
def lv_images(camera) -> dict[str, tuple[int, Bitmap]]:
    """The LV style's image ids and images by character: the digits' image font, and its '.'."""
    data = stock_image(camera)
    code = unpack_firmware(data).blocks[0].body
    base = identify(data).load_addresses["main-code-01"]

    def image_font(addr: int) -> dict[str, int]:
        pairs = {}
        for off in range(addr - base, addr - base + 0x400, 4):
            ch, image = struct.unpack_from("<2H", code, off)
            if not ch:
                break
            pairs[chr(ch)] = image
        return pairs

    digits, dot = IMAGE_FONTS[camera]
    ids = {c: i for c, i in image_font(digits).items() if c in "0123456789+-:"}
    ids["."] = image_font(dot)["."]
    assert sorted(ids) == sorted("0123456789+-:."), ids
    images = {
        int(a.name): a.bitmap
        for a in read_assets(data)
        if a.kind == "image" and int(a.name) in ids.values()
    }
    return {c: (i, images[i]) for c, i in ids.items()}


def encode(bitmap: Bitmap) -> bytes:
    """fmt 0x09: header {h-1:12, w-1:12, fmt:8}, then (colour << 6 | run-1) bytes, runs within a row."""
    out = bytearray(
        ((bitmap.height - 1) | (bitmap.width - 1) << 12 | 0x09 << 24).to_bytes(
            4, "little"
        )
    )
    for y in range(bitmap.height):
        row = bitmap.pixels[y * bitmap.width : (y + 1) * bitmap.width]
        x = 0
        while x < len(row):
            run = 1
            while x + run < len(row) and row[x + run] == row[x] and run < 64:
                run += 1
            out.append(row[x] << 6 | (run - 1))
            x += run
    return bytes(out)


def image_table(ids: set[int], path: Path, camera: str = "epl3") -> Path:
    """The firmware's struct gui_image records {u16 type, w, h, bpp, u32 0, size, bitmap, key} of
    `ids` with their encoded bitmaps, and the id table."""
    common = common_resource(stock_image(camera))
    layout = ASSET_LAYOUTS[camera]
    table = common.u32(layout.common_addr + layout.image_table)
    lines = ["#include <stdint.h>"]
    entries = []
    for image in sorted(ids):
        record = common.u32(table + 4 * image)
        kind, w, h, bpp = (common.u16(record + k) for k in range(0, 8, 2))
        size = common.u32(record + 0x0C)
        start = common.off(common.u32(record + 0x10))
        # the decoder reads whole u64 code words; the firmware's last one may end after `size`
        raw = common.data[start : start + size] + bytes(8)
        data = ", ".join(str(b) for b in raw)
        lines += [
            f"static const uint8_t b{image}[] __attribute__((aligned(4))) = {{{data}}};",
            (
                f"static const struct {{ uint16_t t, w, h, bpp; uint32_t z, size; const void *bmp;"
                f" uint32_t key; }} r{image} = {{ {kind}, {w}, {h}, {bpp}, 0, {size}, b{image}, 0 }};"
            ),
        ]
        entries.append(f"[{image}] = &r{image}")
    lines.append(
        f"const void *const sim_images[{max(ids) + 1}] = {{{', '.join(entries)}}};"
    )
    path.write_text("\n".join(lines) + "\n")
    return path


def glyph_table(font: dict[str, Bitmap], path: Path) -> Path:
    lines = [
        "#include <stdint.h>",
        "struct sim_glyph { uint16_t w, h; const uint8_t *bmp; };",
    ]
    entries = []
    for c, bitmap in sorted(font.items()):
        data = ", ".join(str(b) for b in encode(bitmap))
        lines.append(
            f"static const uint8_t g{ord(c):02x}[] __attribute__((aligned(4))) = {{{data}}};"
        )
        entries.append(
            f"[{ord(c)}] = {{{bitmap.width}, {bitmap.height}, g{ord(c):02x}}}"
        )
    lines.append(f"const struct sim_glyph sim_font[128] = {{{', '.join(entries)}}};")
    path.write_text("\n".join(lines) + "\n")
    return path


def spacing(c: str, following: str) -> int:
    """Pen advance beyond the glyph cell (the firmware subtracts the font's
    adjustment {-3 default, -1 digit-digit, -2 digit before .:-/" and + before digit, space +3})."""
    if not following:
        return 0
    if c.isdigit():
        return 2 if following in '.:-/"' else 1
    if c == "+" and following.isdigit():
        return 2
    return 0 if c == " " else 3


@dataclass(frozen=True)
class Case:
    x: int
    y: int
    w: int
    h: int
    bg: int
    align: int
    style: int
    ink: tuple[int, int, int]
    text: str
    pixels: bytes


def scaled(glyph: Bitmap) -> dict[tuple[int, int], int]:
    """Codes 1..3 of the glyph scaled by LV_SCALE with an area average, its rows shifted one down
    first; pixels of mean code 1 or less (1/3 coverage) stay empty."""
    w = math.ceil(glyph.width * LV_SCALE)
    h = math.ceil((glyph.height + 1) * LV_SCALE)
    out = {}
    for y in range(h):
        for x in range(w):
            total = Fraction(0)
            top, left = y / LV_SCALE - 1, x / LV_SCALE
            for gy in range(math.floor(top), math.ceil(top + 1 / LV_SCALE)):
                for gx in range(math.floor(left), math.ceil(left + 1 / LV_SCALE)):
                    if not (0 <= gx < glyph.width and 0 <= gy < glyph.height):
                        continue
                    dy = min(gy + 1, top + 1 / LV_SCALE) - max(gy, top)
                    dx = min(gx + 1, left + 1 / LV_SCALE) - max(gx, left)
                    total += glyph.pixels[gy * glyph.width + gx] * dx * dy
            mean = total * LV_SCALE * LV_SCALE
            if mean > 1:
                out[(x, y)] = round(mean)
    return out


def outlined(glyph: Bitmap) -> dict[tuple[int, int], int]:
    """A font glyph in the LV style, by scaled coordinates: codes 1..3 for ink (bold: each pixel
    also inks the pixel right of it), 0x10 beside ink, 0x11 diagonal to it, 0x12 two pixels right
    of and/or below it."""
    small = scaled(glyph)
    w = math.ceil(glyph.width * LV_SCALE) + 1
    h = math.ceil((glyph.height + 1) * LV_SCALE)
    bold = {
        (x, y): max(small.get((x, y), 0), small.get((x - 1, y), 0))
        for y in range(h)
        for x in range(w)
    }

    def ink(x: int, y: int) -> bool:
        return bold.get((x, y), 0) > 0

    out = {}
    for y in range(-2, h + 2):
        for x in range(-1, w + 2):
            if ink(x, y):
                out[(x, y)] = bold[(x, y)]
            elif any(
                ink(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            ):
                out[(x, y)] = BLACK
            elif any(ink(x + dx, y + dy) for dx in (1, -1) for dy in (1, -1)):
                out[(x, y)] = TRANSLUCENT
            elif any(ink(x - dx, y - dy) for dx in (1, 2) for dy in (1, 2)):
                out[(x, y)] = FAINT
    return out


def _pen(case: Case, width: int, margin: int) -> int:
    return {
        LEFT: margin,
        CENTER: int((case.w - width) / 2),
        RIGHT: case.w - margin - width,
    }[case.align]


def _lv_line(
    case: Case,
    text: str,
    top: int,
    out: bytearray,
    font: dict[str, Bitmap],
    lv_images: dict[str, tuple[int, Bitmap]],
) -> None:
    advances = [
        lv_images[c][1].width
        if c in lv_images
        else LV_SPACE
        if c == " "
        else math.ceil(font[c].width * LV_SCALE) + 3
        for c in text
    ]
    pen = _pen(case, sum(advances), LV_MARGIN)
    lut = (case.bg, *case.ink)
    image_ink = dict(zip(LV_IMAGE_WHITE, case.ink, strict=True))

    def put(x: int, y: int, v: int) -> None:
        if 0 <= x < case.w and 0 <= y < case.h:
            out[y * case.w + x] = v

    for c, adv in zip(text, advances, strict=True):
        if c in lv_images:
            image = lv_images[c][1]
            for y in range(image.height):
                for x in range(image.width):
                    if v := image.pixels[y * image.width + x]:
                        put(pen + x, top + LV_IMAGE_TOP + y, image_ink.get(v, v))
        elif c != " ":
            for (x, y), v in outlined(font[c]).items():
                put(pen + 1 + x, top + y + LV_DROP, lut[v] if v < 4 else v)
        pen += adv


def _plain_line(
    case: Case, text: str, top: int, out: bytearray, font: dict[str, Bitmap]
) -> None:
    glyphs = [font[c] for c in text]
    advances = [
        g.width + spacing(c, text[i + 1 : i + 2])
        for i, (c, g) in enumerate(zip(text, glyphs, strict=True))
    ]
    pen = _pen(case, sum(advances), MARGIN)
    lut = (case.bg, *case.ink)
    for g, adv in zip(glyphs, advances, strict=True):
        for y in range(g.height):
            for x in range(g.width):
                code = g.pixels[y * g.width + x]
                px, py = pen + x, top + MARGIN + y
                if code and 0 <= px < case.w and py < case.h:
                    out[py * case.w + px] = lut[code]
        pen += adv


def expected(
    case: Case, font: dict[str, Bitmap], lv_images: dict[str, tuple[int, Bitmap]]
) -> bytes:
    """The rectangle: each line of the text placed by its own width, the next PITCH lower."""
    out = bytearray([case.bg]) * (case.w * case.h)
    pitch = LV_PITCH if case.style == LV else PITCH
    for n, line in enumerate(case.text.split("\n")):
        if case.style == LV:
            _lv_line(case, line, n * pitch, out, font, lv_images)
        else:
            _plain_line(case, line, n * pitch, out, font)
    return bytes(out)


@pytest.fixture(scope="module")
def cases(camera, font, lv_images, tmp_path_factory) -> list[Case]:
    workdir = tmp_path_factory.mktemp(f"hudsim_{camera}")
    result = run_in_simulator(
        workdir,
        "hudsim",
        [
            HUD / "test_hud.c",
            HUD / "fw.S",
            SIM_START[camera],
            glyph_table(font, workdir / "glyphs.c"),
            image_table(
                {i for i, _ in lv_images.values()}, workdir / "images.c", camera
            ),
        ],
        MEMORY[camera],
        mocked=["gpi_get_glyph", "malloc", "free"],
        target=sdk.target(camera),
    )
    assert_exit(result, 0, "tests/sim/hud/test_hud.c")
    out, found = result.stdout, []
    while out.startswith(b"#### "):
        header, out = out[5:].split(b"\n", 1)
        *fields, text = header.decode("latin-1").split(" ", 10)
        x, y, w, h, bg, align, style, *ink = map(int, fields)
        text = text.replace("|", "\n")
        found.append(Case(x, y, w, h, bg, align, style, tuple(ink), text, out[: w * h]))
        out = out[w * h :]
    assert len(found) == 8, result.stdout[-500:]
    return found


def test_text_matches_the_font_decoder(cases, font, lv_images):
    for case in cases:
        assert case.pixels == expected(case, font, lv_images), case.text


@dataclass(frozen=True)
class DrawDump:
    images: dict[int, Bitmap]
    """fenykl_hud_image's output by id (fmt 0x03 is assumed)."""
    row: Case
    """The drawn element; `text` is the cell pattern."""


@pytest.fixture(scope="module")
def stock_images() -> dict[int, Bitmap]:
    wanted = {*CELLS.values(), DIGIT, DOT}
    return {
        int(a.name): a.bitmap
        for a in read_assets(stock_image())
        if a.kind == "image" and int(a.name) in wanted
    }


@pytest.fixture(scope="module")
def draw_dump(stock_images, tmp_path_factory) -> DrawDump:
    workdir = tmp_path_factory.mktemp("huddraw")
    result = run_in_simulator(
        workdir,
        "huddraw",
        [
            HUD / "test_hud_draw.c",
            HUD / "fw.S",
            SIM / "hooks" / "sim_start.S",
            image_table(set(stock_images), workdir / "images.c"),
        ],
        MEMORY["epl3"],
        mocked=["malloc", "free"],
    )
    assert_exit(result, 0, "tests/sim/hud/test_hud_draw.c")
    out, images = result.stdout, {}
    while out.startswith(b"#img "):
        header, out = out[5:].split(b"\n", 1)
        image, w, h = map(int, header.split())
        images[image] = Bitmap(w, h, 0x03, out[: w * h])
        out = out[w * h :]
    assert out.startswith(b"#row "), out[:100]
    header, out = out[5:].split(b"\n", 1)
    *fields, pattern = header.decode("latin-1").split(" ")
    x, y, w, h = map(int, fields)
    row = Case(x, y, w, h, CLEAR, LEFT, PLAIN, (0, 0, 0), pattern, out[: w * h])
    assert len(out) == w * h, result.stdout[-200:]
    return DrawDump(images, row)


def test_image_matches_the_asset_decoder(draw_dump, stock_images):
    assert sorted(draw_dump.images) == [1095, 1096, 1097, 1098]
    for image, bitmap in draw_dump.images.items():
        assert bitmap.pixels == stock_images[image].pixels, image
        assert (bitmap.width, bitmap.height) == (8, 9)


def test_cell_row_places_the_stock_cells(draw_dump, stock_images):
    row = draw_dump.row
    assert (row.x, row.y, row.w, row.h, len(row.text)) == (620, 42, 80, 12, 10)
    want = bytearray([CLEAR]) * (row.w * row.h)
    for k, kind in enumerate(row.text):
        cell = stock_images[CELLS[kind]]
        for y in range(cell.height):
            for x in range(cell.width):
                v = cell.pixels[y * cell.width + x]
                if kind == "f" and v == 0x04:
                    v = TRANSLUCENT
                if v:
                    want[(CELL_TOP + y) * row.w + k * CELL_PITCH + x] = v
    assert row.pixels == bytes(want)

    def column(x: int) -> bytes:
        return row.pixels[x :: row.w]

    for k in range(10):
        # the cells' 1-px transparent ring: the stock 2-px gap between neighbours
        assert column(k * CELL_PITCH) == bytes([CLEAR]) * row.h
        assert column(k * CELL_PITCH + 7) == bytes([CLEAR]) * row.h
        inside = row.pixels[
            2 * row.w + k * CELL_PITCH + 1 : 2 * row.w + k * CELL_PITCH + 7
        ]
        assert CLEAR not in inside, k
    for y in (0, 1, 9, 10, 11):
        assert row.pixels[y * row.w : (y + 1) * row.w] == bytes([CLEAR]) * row.w


def test_em10ii_visibility_output_and_palette(tmp_path):
    result = run_in_simulator(
        tmp_path,
        "hudsim_em10ii",
        [HUD / "test_hud_em10ii.c", HUD / "fw.S", SIM_START["em10ii"]],
        MEMORY["em10ii"],
        mocked=["malloc", "free"],
        target=sdk.target("em10ii"),
    )
    assert_exit(result, 0, "tests/sim/hud/test_hud_em10ii.c")
