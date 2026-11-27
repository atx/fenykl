"""Units: the fields grouped by the strobe they change under.

A unit is one strobe, a C2 head or control field that leaves idle on its own, with the fields
whose values change in the words where it is active: counted over every word of the programs
across later firmware versions of the same family, each held field changes almost only under
one strobe (verified in the bytes for the counts; that these groups are execution units is
inferred). The op slot is the op with its operands; PB and PR are banks of ports, each enable
owning one field (`fields.PORTS`); M is the strobe that puts an S head's result on X0's result
bus, with a held 2-bit field; Q is a mode. The constant bits form no unit. A unit's
fields need not be contiguous; re-cutting one is an edit of the `unit` of its fields.

In the stock programs S0..S3, PB and M (the compute group) act mostly in words with an idle op,
and X0, X1 and PR (the transfer group) in words with the data ops, early in a routine.
"""

from dataclasses import dataclass

from .fields import FIELDS, HEADS, M_UNITS, PORTS, Field, lookup


@dataclass(frozen=True)
class Unit:
    name: str
    strobes: tuple[Field, ...]
    fields: tuple[Field, ...]
    """The strobes first, then the operands in bit order."""


_STROBES: dict[str, tuple[str, ...]] = {
    "op_slot": ("op",),
    **{u: (f"f{HEADS[u][0]}_{HEADS[u][1]}",) for u in M_UNITS},
    "x0": ("f302_307",),
    "x1": ("f308_312",),
    "pb": (*(e for e in PORTS if lookup(e).unit == "pb"), "f234_235"),
    "pr": tuple(e for e in PORTS if lookup(e).unit == "pr"),
    "m": ("f276",),
    "q": ("f35_36", "f145_146"),
    "const": (),
}
"""Each unit's strobes by unit name."""


def _unit(name: str, strobes: tuple[str, ...]) -> Unit:
    heads = tuple(sorted((lookup(k) for k in strobes), key=lambda f: f.lo))
    rest = tuple(f for f in FIELDS if f.unit == name and f not in heads)
    if any(f.unit != name for f in heads):
        raise ValueError(f"{name}: a strobe of another unit")
    return Unit(name, heads, heads + rest)


UNITS: dict[str, Unit] = {n: _unit(n, s) for n, s in _STROBES.items()}
_orphans = {f.id for f in FIELDS if f.unit not in UNITS}
if _orphans:
    raise ValueError(f"fields without a unit: {sorted(_orphans)}")
