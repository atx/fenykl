"""The DSP word: an immutable 320-bit value with views by field and by unit.

Decoding never fails: every 320-bit value is a Word, so bytes -> Word -> bytes is exact.
Validation of assembled words (constants, allowed values) is `Word.check`.
"""

from collections.abc import Mapping

from .fields import (
    CONTROL_FLAG,
    CONTROL_OPS,
    FIELDS,
    HEAD_VIEWS,
    IDLE_VALUE,
    OP_FIELD,
    TARGET,
    TARGET_OPS,
    WORD_BITS,
    WORD_BYTES,
    Field,
    Operands,
    is_control,
    lookup,
    op,
    operands,
    target,
)
from .units import SLOTS, UNITS, Slot


class Word:
    """One instruction. Built from slots, unset fields keep `base` (default: the idle word)::

    Word(x1=X1(xreg=3, x1op=0x1C), op_slot=OpSlot(op=0x0B))
    """

    __slots__ = ("value",)
    value: int

    def __init__(self, base: "Word | None" = None, /, **slots: Slot) -> None:
        value = IDLE_VALUE if base is None else base.value
        for unit, slot in slots.items():
            if unit not in UNITS:
                raise TypeError(f"no DSP unit {unit!r}")
            if not isinstance(slot, SLOTS[unit]):
                raise TypeError(f"{unit}= takes a {SLOTS[unit].__name__}, not {slot!r}")
            for f, v in slot.items():
                value = f.put(value, v)
        object.__setattr__(self, "value", value)

    @classmethod
    def from_int(cls, value: int) -> "Word":
        if not 0 <= value < 1 << WORD_BITS:
            raise ValueError(f"{value:#x} is not a {WORD_BITS}-bit word")
        word = object.__new__(cls)
        object.__setattr__(word, "value", value)
        return word

    @classmethod
    def from_bytes(cls, data: bytes) -> "Word":
        if len(data) != WORD_BYTES:
            raise ValueError(f"a word is {WORD_BYTES} bytes, got {len(data)}")
        return cls.from_int(int.from_bytes(data, "little"))

    @classmethod
    def from_fields(
        cls, values: Mapping[str, int], base: "Word | None" = None
    ) -> "Word":
        """A word from field values by id or name; unset fields keep `base`."""
        value = IDLE_VALUE if base is None else base.value
        for key, v in values.items():
            value = lookup(key).put(value, v)
        return cls.from_int(value)

    @classmethod
    def data(cls, code: int, args: Operands, base: "Word | None" = None) -> "Word":
        """Data op `code` with operands `args` in its format; other fields keep `base`."""
        fmt = op(code).format
        if fmt is None:
            raise ValueError(f"op {code:#x} has no operand format")
        word = OP_FIELD.put(IDLE_VALUE if base is None else base.value, code)
        return cls.from_int(fmt.encode(word, args))

    @classmethod
    def head(cls, unit: str, /, base: "Word | None" = None, **operands: int) -> "Word":
        """Unit `unit` (s0..s3, x0, x1) by its named operands (`fields.HEAD_VIEWS`), the head as
        op, immediates composed; other fields keep `base`::

            Word.head("s1", op=0x04, a=4, b=4, imm=0x20)
        """
        word = IDLE_VALUE if base is None else base.value
        return cls.from_int(HEAD_VIEWS[unit].encode(word, operands))

    def head_operands(self, unit: str) -> dict[str, int]:
        """Unit `unit`'s named operands, as `Word.head` takes them."""
        return HEAD_VIEWS[unit].decode(self.value)

    @classmethod
    def ctl(
        cls, code: int, target: int | None = None, base: "Word | None" = None
    ) -> "Word":
        """Control op `code` with f229 clear and, for ops 0..3, the absolute word index `target`
        in `TARGET`; other fields keep `base`. (`control` is the property that tests a word.)"""
        if code not in CONTROL_OPS:
            raise ValueError(f"op {code:#x} is no control op")
        if (target is not None) != (code in TARGET_OPS):
            raise ValueError(f"op {code:#x}: target {target!r}")
        value = IDLE_VALUE if base is None else base.value
        value = CONTROL_FLAG.put(OP_FIELD.put(value, code), 0)
        if target is not None:
            value = TARGET.put(value, target)
        return cls.from_int(value)

    def to_bytes(self) -> bytes:
        return self.value.to_bytes(WORD_BYTES, "little")

    def __setattr__(self, key: str, value: object) -> None:
        raise AttributeError("Word is immutable")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Word) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)

    def __getitem__(self, key: str) -> int:
        return lookup(key).get(self.value)

    @property
    def op(self) -> int:
        """The op, f313_319; `fields.op` gives its meaning."""
        return OP_FIELD.get(self.value)

    @property
    def control(self) -> bool:
        """A control word: its op is one of `fields.CONTROL_OPS`."""
        return is_control(self.value)

    @property
    def target(self) -> int | None:
        """The word index a control op 0..3 names; None for other words."""
        return target(self.value)

    @property
    def operands(self) -> Operands | None:
        """The operands of a data op in its format; None for other words."""
        return operands(self.value)

    def fields(self) -> dict[str, int]:
        """Every field's value, by id in bit order."""
        return {f.id: f.get(self.value) for f in FIELDS}

    def changed(self, base: "Word | None" = None) -> list[tuple[Field, int]]:
        """(field, value) of the fields that differ from `base` (default: idle), in bit order."""
        ref = IDLE_VALUE if base is None else base.value
        diff = self.value ^ ref
        return [(f, f.get(self.value)) for f in FIELDS if f.get(diff)]

    def slots(self, base: "Word | None" = None) -> dict[str, Slot]:
        """The fields that differ from `base` as slots, in unit order; `Word(base, **slots) ==
        self`."""
        changed = {f.id: v for f, v in self.changed(base)}
        slots = {}
        for name, unit in UNITS.items():
            values = {f.id: changed[f.id] for f in unit.fields if f.id in changed}
            if values:
                slots[name] = SLOTS[name](**values)
        return slots

    def check(self) -> None:
        """Raise ValueError if a field holds a value no assembled word may hold."""
        for f in FIELDS:
            f.check(f.get(self.value))

    def __repr__(self) -> str:
        slots = self.slots()
        if not slots:
            return "IDLE"
        return f"Word({', '.join(f'{u}={s!r}' for u, s in slots.items())})"


IDLE = Word.from_int(IDLE_VALUE)
