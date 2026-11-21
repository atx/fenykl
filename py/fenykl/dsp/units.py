"""Units: provisional groups of fields, and their slot classes for the DSL.

The units are contiguous bit blocks of the word, named by position rather than by function: no
field's function is known well enough yet to call a block an ALU or a load/store unit. They are
cut from the table, so re-cutting one is an edit of the `unit` of its fields.

A slot holds the values of some fields of one unit, keyed by id or semantic name::

    RegsA(xreg=3, f123_127=2)
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from .fields import FIELDS, Field, lookup


@dataclass(frozen=True)
class Unit:
    name: str
    cls_name: str
    fact: str
    fields: tuple[Field, ...]

    @property
    def lo(self) -> int:
        return self.fields[0].lo

    @property
    def hi(self) -> int:
        return self.fields[-1].hi


_UNITS = (
    ("ctl_lo", "CtlLo", "control bits 0..22, clear when idle"),
    ("grid_a", "GridA", "the first grid of route nibbles, all-ones when idle"),
    (
        "regs_a",
        "RegsA",
        "the first register group, holding the register-transfer operand",
    ),
    ("grid_b", "GridB", "the second grid, grid A's layout at +110"),
    ("regs_b", "RegsB", "the second register group"),
    ("c2", "C2", "the op-code candidates of 229..301"),
    ("ctl_hi", "CtlHi", "control bits 302..319, set when idle"),
)

UNITS: dict[str, Unit] = {
    name: Unit(name, cls_name, fact, tuple(f for f in FIELDS if f.unit == name))
    for name, cls_name, fact in _UNITS
}
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


CtlLo = slot_class("ctl_lo")
GridA = slot_class("grid_a")
RegsA = slot_class("regs_a")
GridB = slot_class("grid_b")
RegsB = slot_class("regs_b")
C2 = slot_class("c2")
CtlHi = slot_class("ctl_hi")

SLOTS: dict[str, type[Slot]] = {
    cls.unit.name: cls for cls in (CtlLo, GridA, RegsA, GridB, RegsB, C2, CtlHi)
}
_missing = set(UNITS) - set(SLOTS)
if _missing:
    raise ValueError(f"units without a slot class: {sorted(_missing)}")
