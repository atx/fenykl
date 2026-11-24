"""Units: the fields grouped by the strobe they change under, and their slot classes for the DSL.

A unit is one strobe, a C2 head or control field that leaves idle on its own, with the fields
whose values change in the words where it is active: counted over every word of the programs
across later firmware versions of the same family, each held field changes almost only under
one strobe (verified in the bytes for the counts; that these groups are execution units is
inferred). The op slot is the op with its operands; PB and PR are banks of ports, each enable
owning one field (`fields.PORTS`); M is the strobe that puts an S head's result on X0's result
bus, with a held 2-bit field; Q is a mode. The constant bits form no unit. A unit's
fields need not be contiguous; re-cutting one is an edit of the `unit` of its fields.

A slot holds the values of some fields of one unit, keyed by id or semantic name::

    X1(xreg=3, x1op=0x1C)
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from .fields import FIELDS, HEADS, PORTS, Confidence, Field, lookup

H, I, V = Confidence.HYPOTHESIS, Confidence.INFERRED, Confidence.VERIFIED


@dataclass(frozen=True)
class Unit:
    name: str
    cls_name: str
    confidence: Confidence
    """That these fields group under its strobes; the role in `fact` carries its own tag."""
    fact: str
    strobes: tuple[Field, ...]
    fields: tuple[Field, ...]
    """The strobes first, then the operands in bit order."""


_HEAD = (
    "the compute slot {}: its head f{}_{} and the grid A nibbles that change under it; a "
    "unit with a lane file of its own whose two registers its lane heads read, and scalar "
    "registers of its own scalar file (`fields.LANE_FUNCTIONS`, verified on the camera)"
)
_UNITS = (
    (
        "op_slot",
        "OpSlot",
        V,
        (
            "the op (f313_319) with its immediate, ra, rb, f120_122 and the control flag f229: "
            "scalar and control ops, their formats verified in the bytes"
        ),
        ("op",),
    ),
    *(
        (
            u,
            u.upper(),
            I,
            _HEAD.format(u.upper(), *HEADS[u]),
            (f"f{HEADS[u][0]}_{HEADS[u][1]}",),
        )
        for u in ("s0", "s1", "s2", "s3")
    ),
    (
        "x0",
        "X0",
        V,
        (
            "the X0 unit: its head f302_307 and the fields that change under it; an ALU on "
            "the register file A, its result written to A, B, R or G by the PR ports, and the "
            "lane memory's load and store heads, f69_71 naming the store's register of O "
            "(verified on the camera by exact readout)"
        ),
        ("f302_307",),
    ),
    (
        "x1",
        "X1",
        V,
        (
            "the X1 unit: its head f308_312 and the fields that change under it; X0's copy on "
            "the register file B, its result written to A, B, R or G by the PR ports, and the "
            "register transfers (verified on the camera by exact readout)"
        ),
        ("f308_312",),
    ),
    (
        "pb",
        "PB",
        V,
        (
            "17 ports, one enable in 232..264 for each grid B nibble, the pairing verified in "
            "the bytes (f234_235 for f145_146 a hypothesis); the ports of f255..f264 write the "
            "scalar files F0..F3 that the S heads read, each enable value a source "
            "(`fields.SCALAR_FILES`, verified on the camera by exact readout); those of "
            "f236..f251 write the lane files L0..L3, each enable value an S result or, under 0 "
            "of the first ports, a lane memory load (`fields.LANE_SOURCES`, verified on the "
            "camera); f252_254's writes the store file O (`fields.STORE_FILE`, verified on the "
            "camera); f232_233's the third lane file U, beside an S3 head "
            "(`fields.LANE_THIRD_FILE`, verified on the camera: 1 writes, 0 and 2 do not); "
            "ports, with the S heads and M the compute group (hypothesis)"
        ),
        (*(e for e in PORTS if lookup(e).unit == "pb"), "f234_235"),
    ),
    (
        "pr",
        "PR",
        V,
        (
            "5 ports, one enable in 265..273 for each field of register group B 206..228, the "
            "pairing verified in the bytes: register write ports, f272_273 of G and f270_271 of "
            "R, f267_268 and f269 of B, f265_266 of A, each enable value a source (verified on "
            "the camera by exact readout)"
        ),
        tuple(e for e in PORTS if lookup(e).unit == "pr"),
    ),
    (
        "m",
        "M",
        I,
        (
            "the held 2-bit value f274_275 under the active-low strobe f276, which puts an S "
            "head's scalar result on X0's result bus (verified on the camera by exact readout); "
            "in the head's own word f274_275 does not select the S unit (measured on the "
            "camera), for a held result it names the unit, 0 S0, 1 S1, 3 S3 (each verified on "
            "the camera, pre-registered; 2 not shown, so the reading inferred)"
        ),
        ("f276",),
    ),
    (
        "q",
        "Q",
        H,
        "f35_36 and f145_146, never set on the E-M10 II: a section mode of the grid",
        ("f35_36", "f145_146"),
    ),
    ("const", "Const", V, "the constant bits, all-ones in every word", ()),
)


def _unit(
    name: str, cls_name: str, conf: Confidence, fact: str, strobes: tuple[str, ...]
) -> Unit:
    heads = tuple(sorted((lookup(k) for k in strobes), key=lambda f: f.lo))
    rest = tuple(f for f in FIELDS if f.unit == name and f not in heads)
    if any(f.unit != name for f in heads):
        raise ValueError(f"{name}: a strobe of another unit")
    return Unit(name, cls_name, conf, fact, heads, heads + rest)


UNITS: dict[str, Unit] = {u[0]: _unit(*u) for u in _UNITS}
_orphans = {f.id for f in FIELDS if f.unit not in UNITS}
if _orphans:
    raise ValueError(f"fields without a unit: {sorted(_orphans)}")


class Slot:
    """Values of some fields of `unit`; immutable. Unset fields keep the base word's value."""

    unit: ClassVar[Unit]
    __slots__ = ("_values",)

    def __init__(self, **values: int) -> None:
        resolved: dict[str, int] = {}
        for key, value in values.items():
            f = self.field(key)
            if f.id in resolved:
                raise ValueError(f"{f.label} given twice (as {f.id} and {f.name})")
            f.put(0, value)  # width check; allowed values are checked on assembly
            resolved[f.id] = value
        object.__setattr__(self, "_values", resolved)

    @classmethod
    def field(cls, key: str) -> Field:
        f = lookup(key)
        if f.unit != cls.unit.name:
            raise KeyError(f"{key!r} is a field of {f.unit}, not of {cls.unit.name}")
        return f

    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __getattr__(self, key: str) -> int | None:
        """The value of field `key` (id or name), None while unset."""
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            f = self.field(key)
        except KeyError as e:
            raise AttributeError(str(e)) from None
        return self._values.get(f.id)

    def items(self) -> Iterator[tuple[Field, int]]:
        """(field, value) in bit order."""
        for f in self.unit.fields:
            if f.id in self._values:
                yield f, self._values[f.id]

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and other._values == self._values

    def __hash__(self) -> int:
        return hash((self.unit.name, tuple(sorted(self._values.items()))))

    def __repr__(self) -> str:
        args = ", ".join(f"{f.label}={f.literal(value)}" for f, value in self.items())
        return f"{type(self).__name__}({args})"


def slot_class(name: str) -> type[Slot]:
    unit = UNITS[name]
    return type(unit.cls_name, (Slot,), {"unit": unit, "__slots__": ()})


OpSlot = slot_class("op_slot")
S0 = slot_class("s0")
S1 = slot_class("s1")
S2 = slot_class("s2")
S3 = slot_class("s3")
X0 = slot_class("x0")
X1 = slot_class("x1")
PB = slot_class("pb")
PR = slot_class("pr")
M = slot_class("m")
Q = slot_class("q")
Const = slot_class("const")

SLOTS: dict[str, type[Slot]] = {
    cls.unit.name: cls for cls in (OpSlot, S0, S1, S2, S3, X0, X1, PB, PR, M, Q, Const)
}
_missing = set(UNITS) - set(SLOTS)
if _missing:
    raise ValueError(f"units without a slot class: {sorted(_missing)}")
