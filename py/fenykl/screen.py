"""`fenykl dev screen`: the E-M10 II GUI's three OSD layers, read over the dev monitor's READ and
decoded to PNG on the PC (sdk/docs/devmon.md, Screen). The layer format follows the on-camera
screenshot library (sdk/lib/shot_pix.c, sdk/arch/em10ii/fenykl_body.h)."""

import re
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path

from . import fwapi, sdk
from .assets import Palette, encode_png_indexed, encode_png_rgb

LAYERS = 3
PITCH = 896  # every OSD buffer is 896 x 600 bytes, 8 bpp, for all three layers
# OSD source size per disp_device: 1 rear LCD (shown 1:1), 6 built-in EVF (scaled 1.6x to 1024 x 768)
OSD_SIZE = {1: (720, 480), 6: (640, 480)}
DISP_LIVE = 2  # disp_state while display 0's registers are live
LAYER_BPP = bytes([4, 8, 4])  # gpi_layer_bpp, the colour class this layout rests on
PAL_BYTES = 320 * 4
PAL_ENTRIES = (32, 256, 32)
PAL_FIRST = (256, 0, 288)  # entry of each layer's palette in osd_palette
SHADOW_SLOT = (1, 0, 2)  # osd_shadow slot of each layer
# bottom to top: the mix code 3 seen in every live-view and menu screen
STACK = (2, 0, 1)
ALPHA_MAX = 32
UNCACHED_LO = 0xA0021000  # READ's RAM floor 0x60021000 in the uncached alias
UNCACHED_END = 0xC0000000
DRAM_SEGMENTS = (1, 3, 5)  # addr >> 29: as the controller holds it, cached, uncached
# Rows per READ: 28 KiB, 55-105 ms at 505-265 KiB/s, under the ~115 ms between GUI flips in live
# view, so that the flip state sampled after each READ cannot flip twice unseen.
CHUNK_ROWS = 32
TRIES = 2

type Read = Callable[[int, int], bytes]


class ScreenError(Exception):
    pass


@dataclass(frozen=True)
class Symbols:
    """The firmware.toml data addresses a grab reads."""

    osd_shadow: int
    osd_palette: int
    disp_device: int
    disp_state: int
    gpi_draw_buf: int
    gpi_hide_mask: int
    gpi_layer_bpp: int

    @classmethod
    def load(cls, target: str = "em10ii") -> "Symbols":
        data = fwapi.load(sdk.target(target).dir / "firmware.toml").data
        return cls(**{f.name: data[f.name].address for f in fields(cls)})


@dataclass(frozen=True)
class Layer:
    pixels: bytes
    """w * h palette indices, row-major."""
    palette: Palette
    """256 entries {R, G, B, alpha 0..ALPHA_MAX}; indices past the layer's own are transparent."""
    shown: int
    """The buffer address the display shadow held."""


@dataclass(frozen=True)
class Screen:
    w: int
    h: int
    device: int
    layers: dict[int, Layer]
    """The layers read: shown and not hidden."""
    hidden: int
    """gpi_hide_mask: bit 4 * l set = layer l hidden."""
    stable: bool
    """No flip seen: the flip state read the same before the layers and after every READ of them."""
    tries: int
    bytes_read: int
    max_gap: float
    """Longest time between two flip state samples, seconds."""


def uncached(addr: int) -> int:
    """The uncached DRAM alias: CPU1's READ then sees DRAM, as the display does."""
    return addr & 0x1FFFFFFF | 0xA0000000


def visible(raw: bytes, w: int, h: int, pitch: int) -> bytes:
    return b"".join(raw[y * pitch : y * pitch + w] for y in range(h))


def layer_palette(raw: bytes, layer: int) -> Palette:
    first, n = PAL_FIRST[layer], PAL_ENTRIES[layer]
    entries = struct.iter_unpack("4B", raw[4 * first : 4 * (first + n)])
    pal = [(r, g, b, min(a, ALPHA_MAX)) for r, g, b, a in entries]
    return tuple(pal + [(0, 0, 0, 0)] * (256 - n))


def compose(w: int, h: int, layers: dict[int, Layer]) -> bytearray:
    """RGB over black, the layers blended bottom to top with their palette alpha."""
    out = bytearray(3 * w * h)
    for index in STACK:
        layer = layers.get(index)
        if layer is None:
            continue
        pal = layer.palette
        alpha = layer.pixels.translate(bytes(e[3] for e in pal))
        for run in re.finditer(rb"[^\x00]+", alpha):
            for i in range(run.start(), run.end()):
                r, g, b, a = pal[layer.pixels[i]]
                o = 3 * i
                if a == ALPHA_MAX:
                    out[o : o + 3] = bytes((r, g, b))
                    continue
                k = ALPHA_MAX - a
                out[o] = (r * a + out[o] * k) // ALPHA_MAX
                out[o + 1] = (g * a + out[o + 1] * k) // ALPHA_MAX
                out[o + 2] = (b * a + out[o + 2] * k) // ALPHA_MAX
    return out


def _flip_state(read: Read, sym: Symbols) -> tuple[int, ...]:
    """(gpi_draw_buf, disp_state, disp_device, the three osd_shadow words): one READ of the
    display driver's words, which lie together, and one of gpi_draw_buf."""
    words = {sym.disp_device: "<I", sym.disp_state: "<B", sym.osd_shadow: "<3I"}
    lo = min(words)
    hi = max(a + struct.calcsize(f) for a, f in words.items())
    block = read(uncached(lo), hi - lo)
    (device,) = struct.unpack_from("<I", block, sym.disp_device - lo)
    shadow = struct.unpack_from("<3I", block, sym.osd_shadow - lo)
    draw = read(uncached(sym.gpi_draw_buf), 1)[0]
    return (draw, block[sym.disp_state - lo], device, *shadow)


def _geometry(state: tuple[int, ...]) -> tuple[int, int]:
    _, disp_state, device, *_ = state
    if disp_state != DISP_LIVE:
        raise ScreenError(f"display not live (disp_state {disp_state})")
    if device not in OSD_SIZE:
        raise ScreenError(f"output device {device} not supported (1 LCD, 6 EVF)")
    return OSD_SIZE[device]


def _source(addr: int, span: int, layer: int) -> int:
    src = uncached(addr)
    if (
        addr & 3
        or addr >> 29 not in DRAM_SEGMENTS
        or src < UNCACHED_LO
        or src + span > UNCACHED_END
    ):
        raise ScreenError(f"L{layer}: shown buffer 0x{addr:08x} is no OSD plane")
    return src


def grab(read: Read, sym: Symbols) -> Screen:
    """Read the buffers the display shadow holds, sampling the flip state after every READ; once
    more when it changed."""
    if read(uncached(sym.gpi_layer_bpp), LAYERS) != LAYER_BPP:
        raise ScreenError("gpi_layer_bpp is not 04 08 04: unknown OSD layout")
    total, max_gap = 0, 0.0
    for attempt in range(1, TRIES + 1):
        first = _flip_state(read, sym)
        w, h = _geometry(first)
        span = (h - 1) * PITCH + w
        (hidden,) = struct.unpack("<H", read(uncached(sym.gpi_hide_mask), 2))
        raw: dict[int, tuple[int, bytes]] = {}
        stable, moved = True, False
        last = time.monotonic()
        for layer in range(LAYERS):
            shown = first[3 + SHADOW_SLOT[layer]]
            if hidden & 1 << 4 * layer or not shown:
                continue
            src = _source(shown, span, layer)
            data = bytearray()
            while len(data) < span:
                data += read(src + len(data), min(CHUNK_ROWS * PITCH, span - len(data)))
                state = _flip_state(read, sym)
                stable &= state == first
                moved |= state[1:3] != first[1:3]
                now = time.monotonic()
                max_gap, last = max(max_gap, now - last), now
            raw[layer] = (shown, bytes(data))
            total += span
        pal = read(uncached(sym.osd_palette), PAL_BYTES)
        total += PAL_BYTES
        if stable:
            break
    if moved:
        raise ScreenError("the display state or output changed during every try")
    layers = {
        index: Layer(visible(data, w, h, PITCH), layer_palette(pal, index), shown)
        for index, (shown, data) in raw.items()
    }
    return Screen(w, h, first[2], layers, hidden, stable, attempt, total, max_gap)


def _png_palette(pal: Palette) -> Palette:
    return tuple(
        (r, g, b, (a * 255 + ALPHA_MAX // 2) // ALPHA_MAX) for r, g, b, a in pal
    )


def save(screen: Screen, out: Path) -> list[Path]:
    """`out`: the composite; `<stem>-L<n>.png`: each layer read, indexed with its RGBA palette."""
    out.write_bytes(
        encode_png_rgb(screen.w, screen.h, compose(screen.w, screen.h, screen.layers))
    )
    paths = [out]
    for index, layer in screen.layers.items():
        path = out.with_name(f"{out.stem}-L{index}{out.suffix}")
        png = encode_png_indexed(
            screen.w, screen.h, layer.pixels, _png_palette(layer.palette)
        )
        path.write_bytes(png)
        paths.append(path)
    return paths
