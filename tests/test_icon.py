import re

from fenykl import icon
from fenykl.assets import decode_bitmap


def _tables(text: str) -> dict[str, bytes]:
    """The u32 tables of a generated header, by name, as little-endian bytes."""
    return {
        name: b"".join(
            int(w, 16).to_bytes(4, "little")
            for w in re.findall(r"0x([0-9A-F]{8})u", body)
        )
        for name, body in re.findall(
            r"fui_icon_(\w+)\[\d+\] = \{(.*?)\};", text, re.DOTALL
        )
    }


def test_committed_header_is_generated_from_the_grid():
    assert icon.HEADER.read_text() == icon.header(icon.read_grid())


def test_header_tables_decode_to_the_grid_and_its_dim_version():
    bright = icon.read_grid()
    tables = _tables(icon.HEADER.read_text())
    assert decode_bitmap(tables["bright"], 0) == bright
    assert decode_bitmap(tables["dim"], 0) == icon.dim(bright)
    assert set(icon.dim(bright).pixels).isdisjoint(icon.DIM)
