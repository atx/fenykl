"""The assembly DSL: a word written as a bundle of unit instructions and ports.

    S1.xor_imm(L1[4], imm=0x20) | to(O[1], S1) | X0.store(A[1], k=0, o=O[1])

An instruction holds the fields of its own unit only, composed through the head views
(`fields.HEAD_VIEWS`) or the op slot's format (`fields.FORMATS`); a port, `to(dst, src)`, the
enable and register of the one port of dst's file that takes src. A bundle's word is the idle
word with its parts' fields set, and two parts that set one field raise. `Bundle.of` reads any
word back, the fields no instruction or port expresses in a raw part, so decoding is exact.
"""

import keyword
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from .fields import (
    ALU_BUS,
    CONTROL_FLAG,
    CONTROL_OPS,
    FIELDS,
    FORMATS,
    HEAD_VIEWS,
    IDLE_OP,
    IDLE_VALUE,
    LANE_FUNCTIONS,
    LANE_LOAD,
    LANE_SOURCES,
    OP_FIELD,
    OPS,
    PB_FILES,
    PB_SOURCES,
    PORTS,
    PR_FILES,
    PR_SOURCES,
    RA,
    RB,
    S0_RESULT,
    S1_RESULT,
    S2_HIGH,
    S2_LOW,
    S2_RESULT,
    S3_RESULT,
    S_RESULTS,
    STORE_SOURCES,
    TAG,
    TARGET,
    TARGET_OPS,
    X0_LANE_MEMORY,
    X_FUNCTIONS,
    Confidence,
    Field,
    Operands,
    lane_file,
    lookup,
    op,
)
from .semantics import OP_SEMANTICS
from .units import UNITS
from .word import Word

H, V = Confidence.HYPOTHESIS, Confidence.VERIFIED


@dataclass(frozen=True)
class RegFile:
    name: str
    size: int

    def __getitem__(self, index: int) -> "Reg":
        if not 0 <= index < self.size:
            raise IndexError(
                f"{self.name} has registers 0..{self.size - 1}, not {index}"
            )
        return Reg(self, index)

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Reg:
    file: RegFile
    index: int

    def __repr__(self) -> str:
        return f"{self.file.name}[{self.index}]"


def _file(name: str, dst: str) -> RegFile:
    """File `name`, as many registers as its port's register field `dst` names."""
    return RegFile(name, 1 << lookup(dst).width)


L0, L1, L2, L3 = (_file(f"L{n}", f"l{n}dst") for n in range(4))
F0, F1, F2, F3 = (_file(f"F{n}", f"sf{n}dst") for n in range(4))
U, O = _file("U", "udst"), _file("O", "odst")
A, B, R, G = (_file(n, f"{n.lower()}dst") for n in "ABRG")
FILES = {f.name: f for f in (L0, L1, L2, L3, F0, F1, F2, F3, U, O, A, B, R, G)}

X0_RESULT, X1_RESULT = "X0's result", "X1's result"
U_TAKES = "lanes 0..3 the S3 head beside it saves (`LANE_THIRD_FILE`)"


@dataclass(frozen=True)
class Source:
    """A port's source by name; `texts` are the table entries it stands for."""

    name: str
    texts: frozenset[str]

    def __repr__(self) -> str:
        return self.name


LOAD = Source("LOAD", frozenset({LANE_LOAD}))
S2_HI = Source("S2_HI", frozenset({S2_HIGH}))


@dataclass(frozen=True)
class PortSpec:
    file: RegFile
    enable: Field
    dst: Field
    sources: Mapping[int, tuple[str, Confidence]]
    """Enable value -> (what it writes, tier); values that write nothing are left out."""


def _port_specs() -> list[PortSpec]:
    rows: dict[str, tuple[str, Mapping[int, tuple[str, Confidence]]]] = {
        e: (lane_file(e), rows) for e, rows in LANE_SOURCES.items()
    }
    rows["f252_254"] = ("O", STORE_SOURCES)
    rows["f232_233"] = ("U", {1: (U_TAKES, V)})
    for files, sources in ((PB_FILES, PB_SOURCES), (PR_FILES, PR_SOURCES)):
        for e, by_value in sources.items():
            rows[e] = (
                files[e],
                {v: (s, V) for v, s in by_value.items() if s is not None},
            )
    return [
        PortSpec(FILES[name], lookup(e), lookup(PORTS[e]), sources)
        for e, (name, sources) in sorted(rows.items(), key=lambda r: lookup(r[0]).lo)
    ]


PORT_SPECS = _port_specs()


# Instruction parts ------------------------------------------------------------------------

Value = Reg | int | str


class Part:
    """One part of a bundle; `a | b` bundles parts."""

    fields: Mapping[str, int]
    tier: Confidence
    label: str | None = None

    def __or__(self, other: "Part | Bundle") -> "Bundle":
        return Bundle((self,)) | other

    def word(self, labels: Mapping[str, int] | None = None) -> Word:
        return Bundle((self,)).word(labels)


def _text(value: Value) -> str:
    if isinstance(value, int):
        return f"{value}" if -10 < value < 10 else f"{value:#x}"
    return repr(value)


@dataclass(frozen=True, eq=False)
class Instr(Part):
    """An instruction of one unit: its code, the operands as given, its fields and tier."""

    unit: "Unit"
    code: int
    args: tuple[tuple[str, Value], ...]
    fields: Mapping[str, int] = field(repr=False)
    tier: Confidence
    label: str | None = None

    @property
    def name(self) -> str | None:
        return self.unit.names.get(self.code)

    def __repr__(self) -> str:
        if self.name is None:
            args = [f"{self.code:#04x}", *(f"{k}={_text(v)}" for k, v in self.args)]
            return f"{self.unit.key}({', '.join(args)})"
        reads, args, positional = self.unit.reads[self.code], [], True
        for i, (k, v) in enumerate(self.args):
            positional = positional and i < len(reads) and reads[i] == k
            args.append(_text(v) if positional else f"{k}={_text(v)}")
        return f"{self.unit.key}.{self.name}({', '.join(args)})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Instr) and repr(other) == repr(self)

    def __hash__(self) -> int:
        return hash(repr(self))


@dataclass(frozen=True, eq=False)
class Port(Part):
    """`to(dst, src)`: the one port of dst's file that writes src."""

    dst: Reg
    src: Source
    spec: PortSpec = field(repr=False)
    value: int = field(repr=False)

    @property
    def fields(self) -> Mapping[str, int]:
        return {self.spec.enable.id: self.value, self.spec.dst.id: self.dst.index}

    @property
    def tier(self) -> Confidence:
        spec = self.spec
        return min(
            spec.sources[self.value][1], spec.enable.confidence, spec.dst.confidence
        )

    def __repr__(self) -> str:
        return f"to({self.dst!r}, {self.src!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Port) and repr(other) == repr(self)

    def __hash__(self) -> int:
        return hash(repr(self))


def _sources(spec: PortSpec, src: Source) -> list[int]:
    return [v for v, (text, _) in spec.sources.items() if text in src.texts]


def to(dst: Reg, src: Source) -> Port:
    """The port of dst's file that writes `src` to dst; raises if no port of the file does."""
    if not isinstance(dst, Reg) or not isinstance(src, Source):
        raise TypeError(f"to(register, source), not to({dst!r}, {src!r})")
    for spec in PORT_SPECS:
        if spec.file is dst.file and (values := _sources(spec, src)):
            return Port(dst, src, spec, values[0])
    raise ValueError(f"no port of {dst.file.name} writes {src.name}")


@dataclass(frozen=True)
class Raw(Part):
    """Field values by id that no instruction or port expresses."""

    fields: Mapping[str, int]

    @property
    def tier(self) -> Confidence:
        return min(lookup(k).confidence for k in self.fields)

    def __repr__(self) -> str:
        args = (
            f"{lookup(k).label}={lookup(k).literal(v)}" for k, v in self.fields.items()
        )
        return f"raw({', '.join(args)})"

    def __hash__(self) -> int:
        return hash(tuple(self.fields.items()))


def raw(**values: int) -> Raw:
    """Fields by id or name, each checked against its width."""
    fields = {}
    for key, value in values.items():
        f = lookup(key)
        f.put(0, value)
        fields[f.id] = value
    return Raw(fields)


@dataclass(frozen=True)
class Bundle:
    """The parts of one word; any field set by two parts raises."""

    parts: tuple[Part, ...]

    def __post_init__(self) -> None:
        seen: dict[str, Part] = {}
        labels = [p.label for p in self.parts if p.label is not None]
        if len(labels) > 1:
            raise ValueError(f"two targets in one bundle: {labels}")
        for p in self.parts:
            for f in p.fields:
                if f in seen:
                    raise ValueError(f"{lookup(f).label} set by {seen[f]!r} and {p!r}")
                seen[f] = p

    @classmethod
    def of(cls, word: Word, labels: Mapping[int, str] | None = None) -> "Bundle":
        """The bundle of `word`; a control op whose target `labels` names gets the label."""
        return cls(tuple(_decode(word.value, labels or {})))

    def __or__(self, other: "Part | Bundle") -> "Bundle":
        more = other.parts if isinstance(other, Bundle) else (other,)
        return Bundle((*self.parts, *more))

    @property
    def label(self) -> str | None:
        return next((p.label for p in self.parts if p.label is not None), None)

    @property
    def tier(self) -> Confidence:
        return min((p.tier for p in self.parts), default=V)

    def word(self, labels: Mapping[str, int] | None = None) -> Word:
        """The idle word with the parts' fields; a label target is looked up in `labels`."""
        value = IDLE_VALUE
        for p in self.parts:
            for f, v in p.fields.items():
                value = lookup(f).put(value, v)
        if self.label is not None:
            if labels is None or self.label not in labels:
                raise KeyError(f"label {self.label!r} is not defined")
            value = TARGET.put(value, labels[self.label])
        return Word.from_int(value)

    def __repr__(self) -> str:
        return " | ".join(map(repr, self.parts)) if self.parts else "Bundle(())"


def bundle(statement: "Part | Bundle") -> Bundle:
    return statement if isinstance(statement, Bundle) else Bundle((statement,))


# Units ------------------------------------------------------------------------------------

_OPERAND_FILES: dict[str, dict[str, RegFile | None]] = {
    "s0": {"a": L0, "b": L0, "s": F0, "r": F0, "imm": None},
    "s1": {"a": L1, "b": L1, "t": F1, "r": F1, "imm": None},
    "s2": {"a": L2, "b": L2, "t": F2, "imm": None},
    "s3": {"a": L3, "b": L3, "u": U, "idx": F3, "val": F3, "imm": None},
    "x0": {"a": A, "b": A, "o": O, "k": None},
    "x1": {"a": B, "b": B, "k": None},
    "op": {"ra": G, "rb": G, "rr": R, "imm": None, "to": None, "ctl_n": None},
}
"""Each operand's register file; None an integer. S3's idx is a register of F3 or an integer
(a lane or a shift count)."""
_INT_OR_REG = {("s3", "idx")}


class Unit(Source):
    """A unit's instructions: `S1(0x04, b=L1[4], imm=0x20)` by code, `S1.xor_imm(L1[4],
    imm=0x20)` by name; as a port's source, the unit's result."""

    def __init__(self, key: str, texts: Iterable[str], table: str) -> None:
        super().__init__(key.upper(), frozenset(texts))
        object.__setattr__(self, "key", key.upper())
        object.__setattr__(self, "unit", key)
        names, reads = _parse(table)
        object.__setattr__(self, "names", names)
        object.__setattr__(self, "codes", {n: c for c, n in names.items()})
        object.__setattr__(self, "reads", reads)

    def __getattr__(self, name: str):
        if name.startswith("_") or name not in self.__dict__.get("codes", {}):
            raise AttributeError(f"{self.key} has no instruction {name!r}")
        code = self.codes[name]

        def build(*args: Value, **operands: Value) -> Instr:
            reads = self.reads[code]
            if len(args) > len(reads):
                raise TypeError(
                    f"{self.key}.{name} takes {len(reads)} positional operands"
                )
            given = dict(zip(reads, args))
            for k, v in operands.items():
                if k in given:
                    raise TypeError(f"{self.key}.{name}: {k} given twice")
                given[k] = v
            return self(code, **given)

        return build

    def __call__(self, code: int, **operands: Value) -> Instr:
        return _instr(self, code, operands)

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return other is self

    def __repr__(self) -> str:
        return self.key


def _codes(text: str) -> list[int]:
    out: list[int] = []
    for item in text.split(","):
        lo, _, hi = item.partition("-")
        out += range(int(lo, 16), int(hi or lo, 16) + 1)
    return out


def _parse(table: str) -> tuple[dict[int, str], dict[int, tuple[str, ...]]]:
    """Rows "codes name operands": the first code takes the name, the others name_<code>."""
    names: dict[int, str] = {}
    reads: dict[int, tuple[str, ...]] = {}
    for line in filter(None, (s.strip() for s in table.splitlines())):
        codes, name, *operands = line.split()
        for i, code in enumerate(_codes(codes)):
            if code in names:
                raise ValueError(f"{code:#x} named twice")
            names[code] = name if i == 0 else f"{name}_{code:02x}"
            reads[code] = tuple(operands)
    return names, reads


def _number(unit: Unit, name: str, value: Value) -> int:
    """The field value of operand `name`, checked against its type."""
    files = _OPERAND_FILES[unit.unit]
    if name not in files:
        raise TypeError(f"{unit.key} has no operand {name!r}")
    want = files[name]
    if isinstance(value, Reg) and want is not None:
        if value.file is not want:
            raise TypeError(
                f"{unit.key} {name}= takes a register of {want.name}, not {value!r}"
            )
        return value.index
    is_int = isinstance(value, int) and not isinstance(value, bool)
    if is_int and (want is None or (unit.unit, name) in _INT_OR_REG):
        return value
    kind = "an integer" if want is None else f"a register of {want.name}"
    raise TypeError(f"{unit.key} {name}= takes {kind}, not {value!r}")


def _function_tier(unit: str, code: int) -> Confidence:
    """The tier of the function the unit's table gives code; hypothesis without a row."""
    if unit in LANE_FUNCTIONS:
        rows = [r[1] for r in (LANE_FUNCTIONS[unit].get(code),) if r]
        rows += [r.tier for r in (S_RESULTS[unit].get(code),) if r]
        return min(rows, default=H)
    if unit in X_FUNCTIONS:
        rows = [r[1] for r in (X_FUNCTIONS[unit].get(code),) if r]
        if unit == "x0" and code in X0_LANE_MEMORY:
            rows.append(X0_LANE_MEMORY[code][1])
        return min(rows, default=H)
    if code in OP_SEMANTICS:
        return OP_SEMANTICS[code].confidence
    return op(code).confidence


def _instr(unit: Unit, code: int, operands: Mapping[str, Value]) -> Instr:
    numbers = {k: _number(unit, k, v) for k, v in operands.items() if k != "to"}
    if unit.unit == "op":
        value, span, label = _op_encode(code, numbers, operands.get("to"))
    else:
        view = HEAD_VIEWS[unit.unit]
        value = view.encode(IDLE_VALUE, {"op": code, **numbers})
        span = 0
        for k in ("op", *numbers):
            span |= view.operands[k].span
        label = None
    owned = [f for f in UNITS[_UNIT_OF[unit.unit]].fields if f.span & span]
    fields = {f.id: f.get(value) for f in owned}
    tier = min(_function_tier(unit.unit, code), *(f.confidence for f in owned))
    return Instr(unit, code, tuple(operands.items()), fields, tier, label)


_UNIT_OF = {"op": "op_slot"} | {u: u for u in HEAD_VIEWS}
_OP_EXTRAS = {"ra": RA, "rb": RB, "rr": lookup("rr"), "imm": lookup("imm")}
"""The op slot's fields by operand name where no format or target claims them."""


def _op_encode(
    code: int, numbers: Mapping[str, int], to: Value | None
) -> tuple[int, int, str | None]:
    """The op slot's value, the span of what it sets and its label target."""
    value, span, label = OP_FIELD.put(IDLE_VALUE, code), OP_FIELD.span, None
    numbers = dict(numbers)
    if code in CONTROL_OPS:
        value = CONTROL_FLAG.put(value, numbers.pop("ctl_n", 0))
        span |= CONTROL_FLAG.span
    if (to is not None) != (code in TARGET_OPS):
        raise ValueError(f"op {code:#x}: target {to!r}")
    if isinstance(to, str):
        label, span = to, span | TARGET.span
    elif to is not None:
        if not isinstance(to, int):
            raise TypeError(f"a target is a label or a word index, not {to!r}")
        value, span = TARGET.put(value, to), span | TARGET.span
    fmt = FORMATS.get(code)
    if fmt is not None:
        args = Operands(
            numbers.pop("ra", RA.idle),
            numbers.pop("rb", RB.idle) if fmt.family.rb else None,
            numbers.pop("imm") if fmt.imm is not None else None,
        )
        value = fmt.encode(value, args)
        for f in fmt.fields:
            span |= f.span
    for name, v in numbers.items():
        f = _OP_EXTRAS.get(name)
        if f is None or f.span & span:
            raise ValueError(f"op {code:#x}: {name} is part of its format or target")
        value, span = f.put(value, v), span | f.span
    return value, span, label


M_UNITS = ("s0", "s1", "s2", "s3")


@dataclass(frozen=True)
class MPart(Part):
    """`M(S1)`: the strobe M puts S1's scalar result on X0's result bus."""

    unit: Unit

    @property
    def fields(self) -> Mapping[str, int]:
        return {"f274_275": M_UNITS.index(self.unit.unit), "f276": 0}

    @property
    def tier(self) -> Confidence:
        return min(lookup(f).confidence for f in self.fields)

    def __repr__(self) -> str:
        return f"M({self.unit!r})"


def M(unit: Unit) -> MPart:
    if not isinstance(unit, Unit) or unit.unit not in M_UNITS:
        raise ValueError(f"M puts out S0..S3, not {unit!r}")
    return MPart(unit)


# Mnemonics: "codes name operands"; of codes with one formula, the one the stock programs use
# most (the lowest on a tie) takes the name, the others name_<code>. Operands are those the
# formula reads, in the order they are given positionally.

S0 = Unit(
    "s0",
    {S0_RESULT},
    """
    00 abd_next_ss a b
    01 abd_bnext_ss a b
    02 sub_next_ss a b
    03 sub_bnext_ss a b
    04 add_next_ss a b
    21,05 abd_ss a b
    28,06 sub_ss a b
    29,07 add_ss a b
    08 clamp0_imm b imm
    09 clamp0_s b s
    0a clamp0 a b
    0b clamp_imm b imm
    0c clamp_s b s
    0d clamp a b
    0e max_imm b imm
    0f min_imm b imm
    10 sub_imm_ss b imm
    11 add_imm_ss b imm
    12 cmpne_imm b imm
    13 cmpeq_imm b imm
    14 hsub_s b s
    15 hadd_s b s
    16 max_s b s
    17 min_s b s
    18 cmpne_s b s
    19 cmpeq_s b s
    1a sub_s_ss b s
    1b add_s_ss b s
    1c hsub a b
    1d hadd a b
    1e,2a addc_ss a b r
    1f,2b addc a b r
    20 add a b
    22 max a b
    23 min a b
    24 cmpne a b
    25 cmpeq a b
    26 abs_ss a
    27 neg a
    2c,2d addc_us a b r
    2e cmpgt_imm_u b imm
    2f cmple_imm_u b imm
    30 cmpgt_s_u b s
    31 cmple_s_u b s
    32 cmplt_u a b
    33 cmpge_u a b
    34 max_imm_u b imm
    35 min_imm_u b imm
    36 max_s_u b s
    37 min_s_u b s
    38 max_u a b
    39 min_u a b
    3a sub_imm_us b imm
    3b add_imm_us b imm
    3c sub_s_us b s
    3d add_s_us b s
    3e sub_us a b
    3f add_us a b
    4d,41 mov b
    42 mov_imm imm
    43 mov_r r
    44 sel_imm b s imm
    45 sel_r b s r
    46 sel a b s
    4b bits_s s
    52 get_r r
    60-7e latch
    """,
)
S1 = Unit(
    "s1",
    {S1_RESULT},
    """
    00 not_ b
    01 xor a b
    02 or_ a b
    03 and_ a b
    04 xor_imm b imm
    05 or_imm b imm
    06 and_imm b imm
    07 xor_t b t
    08 or_t b t
    09 and_t b t
    0a,13 shl_imm_ss b imm
    0b shl_imm b imm
    0c shr_imm b imm
    0d sar_imm b imm
    0e rnd_imm b imm
    0f fsh_imm_ss a b imm
    10 frnd_imm_ss a b imm
    11,12 shl_imm_us b imm
    14,15 frnd_imm a b imm
    16-19 fshc_imm a b r imm
    1a,1c shl_t_us b t
    1b,23 shl_t_ss b t
    1d,1e frnd_t a b t
    1f-22 fshc_t a b r t
    24 shl_t b t
    25 shr_t b t
    26 sar_t b t
    27 rnd_t b t
    28 fsh_t_ss a b t
    29 frnd_t_ss a b t
    2a max_imm b imm
    2b min_imm b imm
    2c max_t b t
    2d min_t b t
    2e max a b
    2f min a b
    30 sub_imm_ss b imm
    31 add_imm_ss b imm
    32,3d addc_ss a b r
    33,3e addc a b r
    34 add a b
    35 hadd_t b t
    36 sub_t_ss b t
    37 add_t_ss b t
    38 hadd a b
    39 abs_ss a
    3a neg a
    3b sub_ss a b
    3c add_ss a b
    3f,40 addc_us a b r
    41 cmpgt_imm_u b imm
    42 cmple_imm_u b imm
    43 cmpgt_t_u b t
    44 cmple_t_u b t
    45 cmplt_u a b
    46 cmpge_u a b
    47 max_imm_u b imm
    48 min_imm_u b imm
    49 max_t_u b t
    4a min_t_u b t
    4b max_u a b
    4c min_u a b
    4d sub_imm_us b imm
    4e add_imm_us b imm
    4f sub_t_us b t
    50 add_t_us b t
    51 sub_us a b
    52 add_us a b
    53 mov b
    54 get_t t
    55-5f movx b
    60-7e latch
    """,
)
S2 = Unit(
    "s2",
    {S2_LOW, S2_RESULT},
    """
    01,00 mul_imm b imm
    06,02,04 mul a b
    05,03 mul_t b t
    07 pick a b t
    0d,08 mov b
    09 ltz b
    0a lane
    0b gez b
    0c ext b imm
    0e get_t t
    """,
)
S3 = Unit(
    "s3",
    {S3_RESULT, U_TAKES},
    """
    00 rmax_u b
    01 rmin_u b
    02 rsum_u b
    03 rmax b
    04 rmin b
    05 rsum b
    06 zero
    07 blk8 a b
    08 blk16 a b
    09 perm a b
    0a zip_lo a b
    0b zip_hi a b
    0c unzip_odd a b
    0d unzip_even a b
    0e ext_u b idx u
    0f ext b idx u
    10 ins b val idx u
    11 mov_imm imm
    12 ltz b
    13 gez b
    14 lane
    15 ins_reg b idx val
    16 ext_reg_u b idx
    17 ext_reg b idx
    18 bcast val
    19,20,21,23 mov b
    22,1b,1c shift b u idx
    24 get_val val
    1a,1d-1f,25-3e none
    """,
)
X0 = Unit(
    "x0",
    {X0_RESULT},
    """
    00 cmpne_imm a k
    01 cmpeq_imm a k
    02 sub_imm a k
    03 add_imm a k
    04 cmple_imm a k
    05 cmplt_imm a k
    06 cmple_imm_u a k
    07 cmplt_imm_u a k
    08 cmple_u a b
    09 cmplt_u a b
    0a cmple a b
    0b cmplt a b
    0c cmpne a b
    0d cmpeq a b
    0e abs b
    0f neg b
    10 sub a b
    11 add a b
    12 orhi_imm a k
    13 mov_imm_u k
    14 mov_imm k
    15 mov b
    16-19,22 none
    1b,1a store a k o
    1d,1c load a k
    1f,1e store_reg a b o
    21,20 load_reg a b
    23-3e zero
    """,
)
X1 = Unit(
    "x1",
    {X1_RESULT},
    """
    00 cmpne_imm a k
    01 cmpeq_imm a k
    02 sub_imm a k
    03 add_imm a k
    04 cmple_imm a k
    05 cmplt_imm a k
    06 cmple_imm_u a k
    07 cmplt_imm_u a k
    08 cmple_u a b
    09 cmplt_u a b
    0a cmple a b
    0b cmplt a b
    0c cmpne a b
    0d cmpeq a b
    0e abs b
    0f neg b
    10 sub a b
    11 add a b
    12 orhi_imm a k
    13 mov_imm_u k
    14 mov_imm k
    15 mov b
    16 andn a b
    17 nor a b
    18 xor a b
    19 or_ a b
    1a and_ a b
    1b not_ b
    1c hand b
    1d wait
    1e div a b
    """,
)
OP = Unit(
    "op",
    {ALU_BUS},
    """
    00 loop ra to
    01 brf ra to
    02 br ra to
    03 call to
    04 ret rb
    07 halt
    09,08 mov rb
    0a orhi ra imm
    0b li imm
    0c stb ra imm rr
    0d stb_reg ra rb rr
    0e ldb ra imm
    0f ldb_reg ra rb
    10 ldbu ra imm
    11 ldbu_reg ra rb
    12 sth ra imm rr
    13 sth_reg ra rb rr
    14 ldhu ra imm
    15 ldhu_reg ra rb
    16 ldh ra imm
    17 ldh_reg ra rb
    18 stw ra imm rr
    19 stw_reg ra rb rr
    1a ldw ra imm
    1b ldw_reg ra rb
    1c cmpne_imm ra imm
    1d cmpeq_imm ra imm
    1e sub_imm ra imm
    1f add_imm ra imm
    20 cmple_imm ra imm
    21 cmplt_imm ra imm
    22 cmple_imm_u ra imm
    23 cmplt_imm_u ra imm
    24 max_imm ra imm
    25 min_imm ra imm
    26 max_imm_u ra imm
    27 min_imm_u ra imm
    28 cmple_u ra rb
    29 cmplt_u ra rb
    2a cmple ra rb
    2b cmplt ra rb
    2c max_u ra rb
    2d min_u ra rb
    2e max ra rb
    2f min ra rb
    30 cmpne ra rb
    31 cmpeq ra rb
    32 abs rb
    33 neg rb
    34 sub ra rb
    35 add ra rb
    36 xor_imm ra imm
    37 or_imm ra imm
    38 and_imm ra imm
    39 xor ra rb
    3a or_ ra rb
    3b andn ra rb
    3c and_ ra rb
    3d not_ ra
    3e sar_imm ra imm
    3f shr_imm ra imm
    40 shl_imm ra imm
    41 sar ra rb
    42 shr ra rb
    43 shl ra rb
    44 ring_inc ra imm
    45 ring_inc_reg ra rb
    46 ring ra imm rr
    47 ring_reg ra rb rr
    49,48 mul ra rb
    """,
)
UNITS_BY_KEY = {u.unit: u for u in (S0, S1, S2, S3, X0, X1, OP)}


def table_rows(unit: str) -> set[int]:
    """The codes the tables give a meaning: the rows of the unit's function tables."""
    if unit in LANE_FUNCTIONS:
        return set(LANE_FUNCTIONS[unit]) | set(S_RESULTS[unit])
    if unit in X_FUNCTIONS:
        return set(X_FUNCTIONS[unit])
    return (set(OPS) | set(FORMATS)) - {IDLE_OP}


def _check_units() -> None:
    """Every row named once, every name an identifier, every operand in the unit's view."""
    for key, unit in UNITS_BY_KEY.items():
        if set(unit.names) != table_rows(key):
            raise ValueError(f"{key}: names for {set(unit.names) ^ table_rows(key)}")
        if len(unit.codes) != len(unit.names):
            raise ValueError(f"{key}: a name used twice")
        for code, name in unit.names.items():
            if (
                not name.isidentifier()
                or keyword.iskeyword(name)
                or hasattr(Unit, name)
                or name in vars(unit)
            ):
                raise ValueError(f"{key}: {name!r} is no usable name")
            bad = set(unit.reads[code]) - set(_OPERAND_FILES[key])
            if bad:
                raise ValueError(f"{key} {name}: no operands {bad}")


def _check_ports() -> None:
    """Every table source has one Source, and each (file, source) one port."""
    sources = [LOAD, S2_HI, *UNITS_BY_KEY.values()]
    for spec in PORT_SPECS:
        for text, _ in spec.sources.values():
            if sum(text in s.texts for s in sources) != 1:
                raise ValueError(f"{spec.enable.id}: no one source for {text!r}")
    for file in FILES.values():
        for src in sources:
            n = sum(len(_sources(s, src)) for s in PORT_SPECS if s.file is file)
            if n > 1:
                raise ValueError(f"{file.name}: {n} ports write {src.name}")


_check_units()
_check_ports()
SOURCES = {t: s for s in (LOAD, S2_HI, *UNITS_BY_KEY.values()) for t in s.texts}


# Decoding ---------------------------------------------------------------------------------

_IDX_REGS = {c for c, row in LANE_FUNCTIONS["s3"].items() if "F3[f92_95]" in row[0]} | {
    c for c, row in S_RESULTS["s3"].items() if "F3[f92_95]" in row.text
}
"""S3 heads that read idx as a register of F3."""


def _typed(unit: Unit, code: int, name: str, value: int) -> Value:
    file = _OPERAND_FILES[unit.unit][name]
    if file is None or (unit.unit == "s3" and name == "idx" and code not in _IDX_REGS):
        return value
    return file[value]


def _head(unit: Unit, value: int) -> Instr | None:
    view = HEAD_VIEWS[unit.unit]
    code = view.operands["op"].get(value)
    if code == view.operands["op"].idle:
        return None
    given = {k: view.operands[k].get(value) for k in unit.reads.get(code, ())}
    word = view.encode(IDLE_VALUE, {"op": code, **given})
    for k, o in view.operands.items():
        if k != "op" and k not in given and o.get(word) != o.get(value):
            given[k] = o.get(value)
            word = view.encode(word, {k: given[k]})
    typed = {k: _typed(unit, code, k, v) for k, v in given.items()}
    return unit(code, **_in_order(unit, code, typed))


def _op(value: int, labels: Mapping[int, str]) -> Instr | None:
    code = OP_FIELD.get(value)
    if code == IDLE_OP:
        return None
    given: dict[str, Value] = {}
    claimed = OP_FIELD.span
    reads = OP.reads.get(code, ())
    if "to" in reads:
        target = TARGET.get(value)
        given["to"], claimed = labels.get(target, target), claimed | TARGET.span
    fmt = FORMATS.get(code)
    if fmt is not None and fmt.imm is not None and fmt.imm.get(value) >> fmt.imm_bits:
        return None  # an immediate wider than the op's: the slot stays raw
    if fmt is not None:
        args = fmt.decode(value)
        if "ra" in reads or args.ra != RA.idle:
            given["ra"] = G[args.ra]
        if args.rb is not None and ("rb" in reads or args.rb != RB.idle):
            given["rb"] = G[args.rb]
        if args.imm is not None:
            given["imm"] = args.imm
        for f in fmt.fields:
            claimed |= f.span
    reads = OP.reads.get(code, ())
    for name, f in _OP_EXTRAS.items():
        if name in given or f.span & claimed:
            continue
        if name in reads or f.get(value) != f.idle:
            given[name], claimed = (
                _typed(OP, code, name, f.get(value)),
                claimed | f.span,
            )
    if code in CONTROL_OPS and CONTROL_FLAG.get(value) != 0:
        given["ctl_n"] = CONTROL_FLAG.get(value)
    return OP(code, **_in_order(OP, code, given))


def _in_order(unit: Unit, code: int, given: dict[str, Value]) -> dict[str, Value]:
    """The operands the code reads first, in their order, then the others."""
    return {k: given[k] for k in unit.reads.get(code, ()) if k in given} | given


def _decode(value: int, labels: Mapping[int, str]) -> list[Part]:
    parts: list[Part] = []
    candidates: list[Part | None] = [_op(value, labels)]
    candidates += [_head(UNITS_BY_KEY[u], value) for u in HEAD_VIEWS]
    target = {p.id for p in TARGET.parts}
    for instr in candidates:
        if instr is None:
            continue
        # A label target's fields are reserved, not set; any other field must match the word.
        fields = {
            f: v for f, v in instr.fields.items() if not instr.label or f not in target
        }
        if all(lookup(f).get(value) == v for f, v in fields.items()):
            parts.append(instr)
    if lookup("m_n").get(value) == 0:
        parts.append(M(UNITS_BY_KEY[M_UNITS[lookup("msel").get(value)]]))
    for spec in PORT_SPECS:
        v = spec.enable.get(value)
        if v != spec.enable.idle and v in spec.sources:
            src = SOURCES[spec.sources[v][0]]
            parts.append(Port(spec.file[spec.dst.get(value)], src, spec, v))
    taken = {f for p in parts for f in p.fields}
    rest = {
        f.id: f.get(value)
        for f in FIELDS
        if f.id not in taken and f.get(value) != f.idle
    }
    if rest:
        parts.append(Raw(rest))
    return parts


# The review list --------------------------------------------------------------------------


def _short(text: str) -> str:
    """A scalar result's rule without its evidence."""
    cut = min(
        (
            i
            for m in (", a 32-bit mask", ", on M's bus", " (verified", " (measured")
            if (i := text.find(m)) >= 0
        ),
        default=len(text),
    )
    mask = " mask" if text[cut:].startswith(", a 32-bit mask") else ""
    return f"M{mask}: {text[:cut]}"


def formula(unit: str, code: int) -> str:
    """What the tables say code computes, without the evidence."""
    if unit in LANE_FUNCTIONS:
        parts = [LANE_FUNCTIONS[unit][code][0]] if code in LANE_FUNCTIONS[unit] else []
        if code in S_RESULTS[unit]:
            parts.append(_short(S_RESULTS[unit][code].text))
        return "; ".join(parts)
    if unit in X_FUNCTIONS:
        text = X_FUNCTIONS[unit][code][0]
        if unit == "x0" and code in X0_LANE_MEMORY:
            return f"{X0_LANE_MEMORY[code][0]}; scalar: {text}"
        return text
    fact = OP_SEMANTICS[code].fact if code in OP_SEMANTICS else op(code).fact
    cut = min(
        (
            i
            for m in (
                " (verified",
                " (a data op",
                " (fitted",
                " (inferred",
                " (measured",
            )
            if (i := fact.find(m)) >= 0
        ),
        default=len(fact),
    )
    return fact[:cut]


def mnemonics_markdown() -> str:
    """MNEMONICS.md: every unit's names with their code, operands, formula and tier."""
    lines = [
        "# DSP mnemonics",
        "",
        "Generated by `fenykl.dsp.asm.mnemonics_markdown()`; a test keeps this file equal to it.",
        "",
        "Scheme: the operation, then the operand it takes besides the lane operands (`_imm`,",
        "`_s` / `_t` / `_r` a scalar register, `_reg` a register in place of an immediate,",
        "`_next` / `_bnext` a's / b's next lane), then the arithmetic (`_ss` / `_us` saturated",
        "signed / unsigned, `_u` unsigned compare or extension; none: wrapping or signed).",
        "Codes with one formula share a name: the one the stock programs use most (the lowest",
        "on a tie) takes it, the others add their code (`abd_ss_05`). `and_`, `or_`, `not_` avoid",
        "Python keywords. Operands are those the formula reads, in positional order; every",
        "other operand of the unit's view is a keyword. The tier is the table's for the",
        "function; an instruction also carries its fields' (`Instr.tier`).",
    ]
    for key, unit in UNITS_BY_KEY.items():
        lines += [
            "",
            f"## {unit.key}",
            "",
            "| code | name | operands | formula | tier |",
        ]
        lines.append("|---|---|---|---|---|")
        for code in sorted(unit.names):
            text = formula(key, code).replace("|", "\\|")
            reads = ", ".join(unit.reads[code])
            tier = TAG[_function_tier(key, code)]
            lines.append(
                f"| {code:#04x} | `{unit.names[code]}` | {reads} | {text} | {tier} |"
            )
    return "\n".join(lines) + "\n"


EXPORTS = (
    *FILES,
    *(u.key for u in UNITS_BY_KEY.values()),
    "M",
    "LOAD",
    "S2_HI",
    "to",
    "raw",
)
"""The names DSL source imports."""
