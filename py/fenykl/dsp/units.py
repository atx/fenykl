"""Units: the fields grouped by the strobe they change under, and their slot classes for the DSL.

A unit is one strobe, a C2 head or control field that leaves idle on its own, with the fields
whose values change in the words where it is active: counted over every word of the programs
across later firmware versions of the same family, each held field changes almost only under
one strobe (verified in the bytes for the counts; that these groups are execution units is
inferred). The op slot is the op with its operands; PB and PR are banks of ports, each enable
owning one field (`fields.PORTS`); M and Q are modes. The constant bits form no unit. A unit's
fields need not be contiguous; re-cutting one is an edit of the `unit` of its fields.

A slot holds the values of some fields of one unit, keyed by id or semantic name::

    X2(xreg=3, f308_309=0)
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
    """That these fields act as one unit under its strobes."""
    fact: str
    strobes: tuple[Field, ...]
    fields: tuple[Field, ...]
    """The strobes first, then the operands in bit order."""


_HEAD = (
    "the compute slot {}: its head f{}_{} and the grid A nibbles that change under it"
)
_XFER = "the transfer unit {}: its strobe f{}_{} and the fields that change under it"
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
    *(
        (
            u,
            u.upper(),
            I,
            _XFER.format(u.upper(), *HEADS[u]),
            (f"f{HEADS[u][0]}_{HEADS[u][1]}",),
        )
        for u in ("x0", "x1", "x2")
    ),
    (
        "pb",
        "PB",
        I,
        (
            "17 ports, one enable in 232..264 for each grid B nibble (f234_235 for f145_146 a "
            "hypothesis); with the S heads and M the compute group (hypothesis)"
        ),
        (*(e for e in PORTS if lookup(e).unit == "pb"), "f234_235"),
    ),
    (
        "pr",
        "PR",
        I,
        (
            "5 ports, one enable in 265..273 for each field of register group B 206..228: "
            "register write ports (hypothesis); with X0..X2 the transfer group"
        ),
        tuple(e for e in PORTS if lookup(e).unit == "pr"),
    ),
    (
        "m",
        "M",
        H,
        "the held 2-bit value f274_275 under the active-low strobe f276: a compute-group mode",
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
X2 = slot_class("x2")
PB = slot_class("pb")
PR = slot_class("pr")
M = slot_class("m")
Q = slot_class("q")
Const = slot_class("const")

SLOTS: dict[str, type[Slot]] = {
    cls.unit.name: cls
    for cls in (OpSlot, S0, S1, S2, S3, X0, X1, X2, PB, PR, M, Q, Const)
}
_missing = set(UNITS) - set(SLOTS)
if _missing:
    raise ValueError(f"units without a slot class: {sorted(_missing)}")
