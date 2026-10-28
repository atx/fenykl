"""Firmware API for C plugins on MN10300 targets: firmware.toml -> C header, GCC->Panasonic thunks,
address symbols.

A thunk is entered with the GCC ABI (args 1-2 in D0/D1,
arg n >= 3 at (4n,SP)), builds the Panasonic call (integer args in D0/D1, pointer args in A0/A1,
the rest from (4,SP) in parameter order) in a frame of its own, and calls the firmware with the
callee's register list and frame size.

Hooks: a function table may carry a `hook` subtable. For each hook a
plugin uses, the generator emits an entry stub (Panasonic CALL-entered -> GCC wrapper), a trampoline
(the relocated entry instructions, then a jump behind them), the `fw_orig_<name>` thunk into the
trampoline and the `fenykl_hook_<name>` descriptor the runtime installs (sdk/README.md, Hooks).

Callbacks and slots: `[callbacks.<sig>]` declares a signature the firmware
calls plugin code with (`calls`, or `call` with a register list and frame). For each, the header gets
FENYKL_CALLBACK_<sig>, which emits a reverse entry stub (firmware -> GCC wrapper) for one C function,
and the thunks get `fw_call_<sig>`, which calls a firmware function pointer with that convention.
`[slots.<name>]` is a pointer word the firmware calls through, with its stock value. A plugin lists
the slots it uses in plugin.toml; their descriptors (`fenykl_slot_<name>`) go to fw_hooks.S.

Cores: a function's `cores` says which cores may call
it (default ["cpu0"]). Plugin files built for CPU1 (plugin.toml `cpu1_sources`) get -DFENYKL_CPU1,
under which the prototype of every CPU0-only function carries an `error` attribute: a call from CPU1
code fails to compile.

Data: `[data.<name>]` is a firmware address the SDK uses as a value (a global, a table, an MMIO
register, an address-range bound). The header gets it as FW_<NAME>, an unsigned constant, so shared
C names the address and each camera's firmware.toml supplies it.
"""

import dataclasses
import re
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

VARIADIC_WORDS = 8
INT_TYPES = {
    "int",
    "unsigned",
    "unsigned int",
    "long",
    "unsigned long",
    "int32_t",
    "uint32_t",
}
# Pointer-class types without a '*' that the generated header defines.
POINTER_TYPEDEFS = {"fw_task_entry"}
SAVE_REGS = ("d2", "d3", "a2", "a3")
EVIDENCE = {"high", "medium", "low"}
_ARG = re.compile(r"^([ad]) (.*?)\s*\b([A-Za-z_]\w*)$")
_FIELDS = {"address", "saves", "frame", "evidence", "returns", "args", "note"}
CPU0_ONLY = ("cpu0",)
CORE_SETS = {CPU0_ONLY, ("cpu0", "cpu1")}
_HOOK_FIELDS = {"entry", "relocate", "cpu1", "cpu1_evidence", "note"}
_CALLBACK_FIELDS = {"entry", "evidence", "returns", "args", "note"}
_SLOT_FIELDS = {"address", "callback", "expected", "cpu1", "cpu1_evidence", "note"}
_DATA_FIELDS = {"address", "type", "evidence", "note"}
_DATA_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
CPU1_SCOPES = {"cpu0-only", "unproven"}
JMP_SIZE = 5  # jmp d32: dc <displacement32>
MAX_RELOCATE = 16  # size of fenykl_hook.original (fenykl.h)
# Mnemonics that behave the same at any address, if no special register is an operand. Everything
# else (branches, calls, returns, loops, traps) is refused rather than re-encoded.
RELOCATABLE = {
    "mov", "movbu", "movhu", "movm", "add", "sub", "inc", "inc4", "clr", "extb", "extbu",
    "exth", "exthu", "and", "or", "xor", "not", "asl", "asl2", "asr", "lsr", "cmp", "nop",
}  # fmt: skip
SPECIAL_REGS = {"psw", "epsw", "mdr", "lir", "lar", "pc", "usp", "ssp", "msp"}
# Snapshot order of the argument registers in the entry stub.
STUB_REGS = ("d0", "d1", "a0", "a1")
STUB_FRAME = 28  # (0) return slot, (4, 8) GCC home slots, (12..24) D0 D1 A0 A1


class ApiError(Exception):
    pass


@dataclass(frozen=True)
class Arg:
    cls: str  # "d" or "a"
    ctype: str
    name: str


@dataclass(frozen=True)
class Hook:
    """Inline hook of a CALL-entered function: JMP_SIZE bytes at the entry become a jmp to the
    stub; `relocate` (whole instructions, at least JMP_SIZE bytes) runs in the trampoline."""

    relocate: bytes
    cpu1: str
    cpu1_evidence: str
    note: str


@dataclass(frozen=True)
class Function:
    name: str
    address: int
    saves: tuple[str, ...]
    frame: int
    evidence: str
    returns: str
    args: tuple[Arg, ...]
    variadic: bool
    note: str
    hook: Hook | None = None
    cores: tuple[str, ...] = CPU0_ONLY
    cores_evidence: str = ""


@dataclass(frozen=True)
class Callback:
    """Signature the firmware enters plugin code with: `calls` is `call` with saves [] and frame 0."""

    name: str
    entry: str
    saves: tuple[str, ...]
    frame: int
    evidence: str
    returns: str
    args: tuple[Arg, ...]
    note: str
    variadic: bool = False


@dataclass(frozen=True)
class Slot:
    """Aligned pointer word the firmware calls through; `expected` is its stock value (0 = empty)."""

    name: str
    address: int
    callback: Callback
    expected: int
    cpu1: str
    cpu1_evidence: str
    note: str


@dataclass(frozen=True)
class Data:
    """Firmware address used as a value; `type` says what lives there."""

    name: str
    address: int
    type: str
    evidence: str
    note: str

    @property
    def macro(self) -> str:
        return f"FW_{self.name.upper()}"


@dataclass(frozen=True)
class Api:
    functions: list[Function]
    callbacks: dict[str, Callback]
    slots: dict[str, Slot]
    data: dict[str, Data]


def _parse_arg(func: str, text: str) -> Arg:
    m = _ARG.match(text)
    if not m:
        raise ApiError(f"{func}: cannot parse argument {text!r}")
    cls, ctype, name = m.groups()
    if cls == "d" and ctype not in INT_TYPES:
        raise ApiError(
            f"{func}: {name}: integer class needs a 32-bit integer type, not {ctype!r}"
        )
    # Address registers also carry plain integers (appevent_post's flag in A0).
    if cls == "a" and not _is_pointer(ctype) and ctype not in INT_TYPES:
        raise ApiError(
            f"{func}: {name}: pointer class needs a pointer or 32-bit integer type, not {ctype!r}"
        )
    return Arg(cls, ctype, name)


def _is_pointer(ctype: str) -> bool:
    return "*" in ctype or ctype in POINTER_TYPEDEFS


def _check_scalar_return(where: str, returns: str) -> None:
    if returns != "void" and returns not in INT_TYPES and not _is_pointer(returns):
        raise ApiError(f"{where}: return type {returns!r} not supported")


def _check_cpu1(where: str, d: dict) -> None:
    if d["cpu1"] not in CPU1_SCOPES:
        raise ApiError(f"{where}: cpu1 must be one of {sorted(CPU1_SCOPES)}")
    if not d["cpu1_evidence"].strip():
        raise ApiError(f"{where}: cpu1_evidence must say what the cpu1 scope rests on")


def _parse_hook(func: Function, d: dict) -> Hook:
    name = func.name
    if set(d) != _HOOK_FIELDS:
        raise ApiError(
            f"{name}.hook: fields must be exactly {sorted(_HOOK_FIELDS)}, got {sorted(d)}"
        )
    if d["entry"] != "call":
        raise ApiError(
            f"{name}.hook: entry {d['entry']!r} not supported (only 'call': CALL-entered)"
        )
    _check_cpu1(f"{name}.hook", d)
    relocate = bytes.fromhex(d["relocate"])
    if not JMP_SIZE <= len(relocate) <= MAX_RELOCATE:
        raise ApiError(
            f"{name}.hook: relocate must be {JMP_SIZE}..{MAX_RELOCATE} bytes, got {len(relocate)}"
        )
    if func.variadic:
        raise ApiError(f"{name}.hook: variadic functions cannot be hooked")
    _check_scalar_return(f"{name}.hook", func.returns)
    plan(func)
    return Hook(relocate, d["cpu1"], d["cpu1_evidence"], d["note"])


def _check_fields(name: str, d: dict, fields: set[str]) -> None:
    if set(d) != fields:
        raise ApiError(
            f"{name}: fields must be exactly {sorted(fields)}, got {sorted(d)}"
        )


def _parse_call(name: str, d: dict) -> tuple[str, ...]:
    """Validated CALL operands; returns the register list in CALL order."""
    if not set(d["saves"]) <= set(SAVE_REGS):
        raise ApiError(f"{name}: saves must be a subset of {SAVE_REGS}")
    if not 0 <= d["frame"] <= 0xFF:
        raise ApiError(f"{name}: frame 0x{d['frame']:x} does not fit the CALL imm8")
    return tuple(r for r in SAVE_REGS if r in d["saves"])


def _parse_cores(name: str, d: dict) -> tuple[tuple[str, ...], str]:
    """`cores` and `cores_evidence` (both or neither) -> (cores in CORE_SETS order, evidence)."""
    if "cores" not in d and "cores_evidence" not in d:
        return CPU0_ONLY, ""
    if "cores" not in d or "cores_evidence" not in d:
        raise ApiError(f"{name}: cores and cores_evidence go together")
    cores = tuple(sorted(d["cores"]))
    if cores not in CORE_SETS:
        raise ApiError(
            f"{name}: cores must be {sorted(map(list, CORE_SETS))}, got {d['cores']}"
        )
    if not d["cores_evidence"].strip():
        raise ApiError(f"{name}: cores_evidence must say what the cores rest on")
    return cores, d["cores_evidence"]


def _parse_function(name: str, d: dict) -> Function:
    d = dict(d)
    hook = d.pop("hook", None)
    cores, cores_evidence = _parse_cores(name, d)
    d.pop("cores", None)
    d.pop("cores_evidence", None)
    _check_fields(name, d, _FIELDS)
    saves = _parse_call(name, d)
    if d["evidence"] not in EVIDENCE:
        raise ApiError(f"{name}: evidence must be one of {sorted(EVIDENCE)}")
    texts = list(d["args"])
    variadic = bool(texts) and texts[-1] == "..."
    if variadic:
        texts.pop()
        if not texts:
            raise ApiError(f"{name}: a variadic function needs a named argument")
    func = Function(
        name,
        d["address"],
        saves,
        d["frame"],
        d["evidence"],
        d["returns"],
        tuple(_parse_arg(name, t) for t in texts),
        variadic,
        d["note"],
        cores=cores,
        cores_evidence=cores_evidence,
    )
    if hook is None:
        return func
    return dataclasses.replace(func, hook=_parse_hook(func, hook))


def _parse_callback(name: str, d: dict) -> Callback:
    where = f"callbacks.{name}"
    if d.get("entry") == "call":
        _check_fields(where, d, _CALLBACK_FIELDS | {"saves", "frame"})
        saves, frame = _parse_call(where, d), d["frame"]
    elif d.get("entry") == "calls":
        _check_fields(where, d, _CALLBACK_FIELDS)
        saves, frame = (), 0
    else:
        raise ApiError(f"{where}: entry must be 'calls' or 'call' (with saves, frame)")
    if d["evidence"] not in EVIDENCE:
        raise ApiError(f"{where}: evidence must be one of {sorted(EVIDENCE)}")
    if "..." in d["args"]:
        raise ApiError(f"{where}: variadic callbacks are not supported")
    _check_scalar_return(where, d["returns"])
    cb = Callback(
        name,
        d["entry"],
        saves,
        frame,
        d["evidence"],
        d["returns"],
        tuple(_parse_arg(where, t) for t in d["args"]),
        d["note"],
    )
    plan(cb)
    return cb


def _parse_slot(name: str, d: dict, callbacks: dict[str, Callback]) -> Slot:
    where = f"slots.{name}"
    _check_fields(where, d, _SLOT_FIELDS)
    if d["callback"] not in callbacks:
        raise ApiError(f"{where}: no [callbacks.{d['callback']}] declared")
    if d["address"] % 4:
        raise ApiError(f"{where}: address 0x{d['address']:08X} is not word-aligned")
    _check_cpu1(where, d)
    return Slot(
        name,
        d["address"],
        callbacks[d["callback"]],
        d["expected"],
        d["cpu1"],
        d["cpu1_evidence"],
        d["note"],
    )


def _parse_data(name: str, d: dict) -> Data:
    where = f"data.{name}"
    _check_fields(where, d, _DATA_FIELDS)
    if not _DATA_NAME.match(name):
        raise ApiError(
            f"{where}: name must be lower case [a-z0-9_] (it becomes FW_<NAME>)"
        )
    if not 0 <= d["address"] <= 0xFFFFFFFF:
        raise ApiError(f"{where}: address 0x{d['address']:X} is not 32-bit")
    if d["evidence"] not in EVIDENCE:
        raise ApiError(f"{where}: evidence must be one of {sorted(EVIDENCE)}")
    if not d["type"].strip():
        raise ApiError(f"{where}: type must say what lives at the address")
    return Data(name, d["address"], d["type"], d["evidence"], d["note"])


def load(path: Path) -> Api:
    with path.open("rb") as f:
        doc = tomllib.load(f)
    callbacks = {n: _parse_callback(n, d) for n, d in doc.pop("callbacks", {}).items()}
    slots = {n: _parse_slot(n, d, callbacks) for n, d in doc.pop("slots", {}).items()}
    data = {n: _parse_data(n, d) for n, d in doc.pop("data", {}).items()}
    functions = [_parse_function(name, d) for name, d in doc.items()]
    # Slots share fw_orig_<name> with hooks; cpu1_unproven names functions and slots.
    names = [f.name for f in functions] + list(slots)
    clash = sorted({n for n in names if names.count(n) > 1})
    if clash:
        raise ApiError(f"names used twice in functions and slots: {clash}")
    return Api(functions, callbacks, slots, data)


@dataclass(frozen=True)
class Plan:
    """Panasonic placement of the GCC arguments, by GCC argument index."""

    regs: dict[str, int]  # firmware register -> GCC argument index
    stack: tuple[int, ...]  # firmware stack slot k = (4+4k,SP) -> GCC argument index


def plan(func: Function | Callback) -> Plan:
    free = {"d": ["d0", "d1"], "a": ["a0", "a1"]}
    regs: dict[str, int] = {}
    stack: list[int] = []
    last = len(func.args) - 1
    for i, arg in enumerate(func.args):
        if func.variadic and i == last:
            stack.append(i)
            continue
        if stack and free[arg.cls]:
            # Seen only with the class exhausted; a register argument after a stack argument has
            # not been characterised.
            raise ApiError(
                f"{func.name}: {arg.name} would follow a stack argument in a register"
            )
        if free[arg.cls]:
            regs[free[arg.cls].pop(0)] = i
        else:
            stack.append(i)
    if func.variadic:
        stack += range(len(func.args), len(func.args) + VARIADIC_WORDS)
    return Plan(regs, tuple(stack))


def _gcc_source(index: int, frame: int) -> str:
    """Location of GCC argument `index` after the thunk's `add -frame,sp`."""
    if index < 2:
        return f"d{index}"
    return f"({4 * (index + 1) + frame},sp)"


def thunk_asm(func: Function) -> str:
    return (
        "\n".join(
            [
                f"/* {func.name} 0x{func.address:08X} */",
                f'\t.section .text.fw_{func.name},"ax"',
                *_thunk_body(func, f"fw_{func.name}", f"fw_addr_{func.name}"),
            ]
        )
        + "\n"
    )


def _marshal(func: Function | Callback, frame: int, shift: int) -> list[str]:
    """Moves func's arguments, GCC argument `index + shift`, to their Panasonic places below the
    caller's `add -frame,sp`."""
    p = plan(func)
    lines = []
    # Stack slots first (A0 is free as scratch), then A0/A1 (D0/D1 still hold GCC args 1-2), then
    # D0 before D1: D0's source index is always lower than D1's, so D0 never overwrites D1's source.
    for slot, index in enumerate(p.stack):
        src = _gcc_source(index + shift, frame)
        if src.startswith("("):
            lines += [f"\tmov {src},a0", f"\tmov a0,({4 + 4 * slot},sp)"]
        else:
            lines.append(f"\tmov {src},({4 + 4 * slot},sp)")
    for reg in ("a0", "a1", "d0", "d1"):
        if reg in p.regs and _gcc_source(p.regs[reg] + shift, frame) != reg:
            lines.append(f"\tmov {_gcc_source(p.regs[reg] + shift, frame)},{reg}")
    return lines


def _thunk_body(func: Function, symbol: str, callee: str) -> list[str]:
    """GCC-entered `symbol` that calls `callee` with the Panasonic placement of func's arguments."""
    frame = 4 + 4 * len(plan(func).stack)
    return [
        f"\t.global _{symbol}",
        f"_{symbol}:",
        f"\tadd -{frame},sp",
        *_marshal(func, frame, 0),
        f"\tcall {callee},[{','.join(func.saves)}],{func.frame}",
        f"\tret [],{frame}",
    ]


def call_thunk_asm(cb: Callback) -> str:
    """`fw_call_<sig>(fn, args...)`: calls the firmware function pointer fn (GCC argument 1) with
    cb's convention. The CALL to a local `jmp (a2)` gives the target the frame its own callers'
    CALL (or `calls`, the same as `call [],0`) would; A2 holds fn and is saved in the thunk's frame."""
    frame = 8 + 4 * len(plan(cb).stack)  # (0) return slot, stack args, saved A2
    go = f".Lfw_call_{cb.name}_jmp"
    lines = [
        f"/* call through a pointer with the {cb.name} convention */",
        f'\t.section .text.fw_call_{cb.name},"ax"',
        f"\t.global _fw_call_{cb.name}",
        f"_fw_call_{cb.name}:",
        f"\tadd -{frame},sp",
        f"\tmov a2,({frame - 4},sp)",
        "\tmov d0,a2",
        *_marshal(cb, frame, 1),
        f"\tcall {go},[{','.join(cb.saves)}],{cb.frame}",
        f"\tmov ({frame - 4},sp),a2",
        f"\tret [],{frame}",
        f"{go}:",
        "\tjmp (a2)",
    ]
    return "\n".join(lines) + "\n"


def _returns(func: Function | Callback) -> str:
    return func.returns if func.returns.endswith("*") else func.returns + " "


def _signature(
    func: Function | Callback, cname: str, first: tuple[str, ...] = ()
) -> str:
    params = list(first) + [
        f"{a.ctype} {a.name}" if not a.ctype.endswith("*") else f"{a.ctype}{a.name}"
        for a in func.args
    ]
    if func.variadic:
        params.append("...")
    return f"{_returns(func)}{cname}({', '.join(params) or 'void'})"


def prototype(func: Function) -> str:
    if "cpu1" in func.cores:
        return _signature(func, f"fw_{func.name}") + ";"
    return _signature(func, f"fw_{func.name}") + f' FENYKL_CPU0_ONLY("fw_{func.name}");'


# Arguments counted by the guard of a variadic call; more is not a plausible call.
NARGS_MAX = 32
NARGS = [
    f"/* The number of arguments, 1 to {NARGS_MAX}: the variadic call guards. */",
    "#define __FENYKL_FW_NARGS_PICK("
    + ", ".join(f"_{i}" for i in range(1, NARGS_MAX + 1))
    + ", n, ...) n",
    "#define __FENYKL_FW_NARGS(...) \\",
    "\t__FENYKL_FW_NARGS_PICK(__VA_ARGS__, "
    + ", ".join(str(i) for i in range(NARGS_MAX, 0, -1))
    + ")",
    "",
]


def variadic_guard(func: Function) -> list[str]:
    """A macro of the function's name that refuses a call with more than VARIADIC_WORDS variadic
    words at compile time; inside it the name is not expanded again and calls the thunk."""
    n = f"fw_{func.name}"
    return [
        f"#define {n}(...) ({{ \\",
        f"\t_Static_assert(__FENYKL_FW_NARGS(__VA_ARGS__) - {len(func.args)} <= {VARIADIC_WORDS}, \\",
        f'\t\t       "{n}: at most {VARIADIC_WORDS} variadic words (the thunk forwards no more)"); \\',
        f"\t{n}(__VA_ARGS__); \\",
        "})",
    ]


_INSN = re.compile(r"^\s*[0-9a-f]+:\t[0-9a-f ]+\t(\S+)\s*(.*)$")


def relocated_instructions(func: Function, objdump: str, workdir: Path) -> list[str]:
    """Disassembles the hook's relocate bytes; refuses a span that does not end on an instruction
    boundary and every instruction outside RELOCATABLE or with a special register operand."""
    assert func.hook is not None
    path = workdir / f"hook_{func.name}.bin"
    path.write_bytes(func.hook.relocate)
    out = subprocess.run(
        [objdump, "-D", "-b", "binary", "-m", "mn10300", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "out of bounds" in out:
        raise ApiError(f"{func.name}.hook: relocate ends inside an instruction")
    insns = []
    for line in out.splitlines():
        m = _INSN.match(line)
        if not m:
            continue
        mnemonic, operands = m.groups()
        if (
            mnemonic not in RELOCATABLE
            or set(re.findall(r"[a-z]+", operands)) & SPECIAL_REGS
        ):
            raise ApiError(
                f"{func.name}.hook: cannot relocate '{mnemonic} {operands}' (only "
                "instructions without control flow, PC or special registers are relocated)"
            )
        insns.append(f"{mnemonic} {operands}".strip())
    return insns


@dataclass(frozen=True)
class Selection:
    hooks: list[Function]
    slots: list[Slot]


def _check_cpu1_scope(
    declared: dict[str, dict],
    used: dict[str, Sequence[str]],
    acked: Sequence[str],
    where: str,
) -> None:
    """Every name in `used` whose CPU1 scope is unproven must be in `acked` (`where`'s
    acknowledgement list), and `acked` may name nothing else."""
    stray = [n for n in acked if not any(n in u for u in used.values())]
    if stray:
        raise ApiError(
            f"{where} lists {', '.join(stray)}, which hooks and slots do not"
        )
    for kind, names in used.items():
        for name in names:
            item = declared[kind][name]
            if item.cpu1 == "unproven" and name not in acked:
                raise ApiError(
                    f"{name}: it is not proven that CPU1 never runs it ({item.cpu1_evidence}); "
                    f'add "{name}" to {where} to build it anyway'
                )


def select(
    api: Api,
    hooks: Sequence[str] = (),
    slots: Sequence[str] = (),
    cpu1_unproven: Sequence[str] = (),
    sdk_hooks: Sequence[str] = (),
    sdk_slots: Sequence[str] = (),
    sdk_cpu1_unproven: Sequence[str] = (),
) -> Selection:
    """What a plugin uses (plugin.toml `hooks`, `slots`) plus the hooks and slots the
    SDK binds itself (target.toml `sdk_hooks`, `sdk_slots`), which the plugin may not list. One
    whose CPU1 scope is unproven needs an explicit acknowledgement: plugin.toml `cpu1_unproven` for
    the plugin's own, target.toml `sdk_cpu1_unproven` for the SDK's."""
    hints = {
        "hook": "; appevent_post: the event layer, sdk/docs/event.md",
        "slot": "; the E-M10 II settings page and pre-capture, sdk/docs/ui.md, precap.md",
    }
    for kind, names, sdk_names in (
        ("hook", hooks, sdk_hooks),
        ("slot", slots, sdk_slots),
    ):
        bound = [n for n in names if n in sdk_names]
        if bound:
            raise ApiError(
                f"{', '.join(bound)}: the SDK binds it itself (target.toml sdk_{kind}s"
                f"{hints[kind]}); remove it from {kind}s"
            )
    declared = {
        "hook": {f.name: f.hook for f in api.functions if f.hook is not None},
        "slot": api.slots,
    }
    own = {"hook": hooks, "slot": slots}
    sdk = {"hook": sdk_hooks, "slot": sdk_slots}
    for kind, names in own.items():
        unknown = [n for n in [*sdk[kind], *names] if n not in declared[kind]]
        if unknown:
            raise ApiError(
                f"no {kind} declared in firmware.toml for {', '.join(unknown)}"
            )
    _check_cpu1_scope(declared, own, cpu1_unproven, "plugin.toml cpu1_unproven")
    _check_cpu1_scope(declared, sdk, sdk_cpu1_unproven, "target.toml sdk_cpu1_unproven")
    funcs = {f.name: f for f in api.functions}
    return Selection(
        [funcs[n] for n in [*sdk_hooks, *hooks]],
        [api.slots[n] for n in [*sdk_slots, *slots]],
    )


def _wrapper_body(func: Function | Callback, fn: str) -> str:
    """Body of the C wrapper the entry stub calls with r = the D0 D1 A0 A1 snapshot and s = the
    firmware stack arguments: calls `fn` with func's arguments."""
    p = plan(func)
    where = {i: f"r[{STUB_REGS.index(reg)}]" for reg, i in p.regs.items()}
    where |= {i: f"s[{k}]" for k, i in enumerate(p.stack)}
    args = ", ".join(f"({a.ctype}){where[i]}" for i, a in enumerate(func.args))
    result = "" if func.returns == "void" else "return "
    return f"{{ (void)r; (void)s; {result}{fn}({args}); }}"


def _hook_header(func: Function) -> list[str]:
    n, h = func.name, func.hook
    assert h is not None
    wrap = f"{_returns(func)}fenykl_hookwrap_{n}(const uint32_t *r, const uint32_t *s)"
    return [
        f"/* Hook {n} 0x{func.address:08X}, CPU1 scope {h.cpu1}: {h.cpu1_evidence}",
        f"   {h.note} */",
        f"typedef {_signature(func, f'fw_hook_{n}_fn')};",
        f"{_signature(func, f'fw_orig_{n}')};",
        f"extern struct fenykl_hook fenykl_hook_{n};",
        f"#define FENYKL_HOOK_{n}(fn)" + " \\",
        f"\t{wrap};" + " \\",
        f"\t{wrap} {_wrapper_body(func, 'fn')}" + " \\",
        f"\t_Static_assert(__builtin_types_compatible_p(__typeof__(fn), fw_hook_{n}_fn), \\",
        f'\t\t       #fn ": not the signature of hook {n} (fw_hook_{n}_fn)")',
        "",
    ]


def header(api: Api, sel: Selection) -> str:
    out = [
        "/* Generated by fenykl from firmware.toml; do not edit. */",
        "#ifndef FENYKL_FW_H",
        "#define FENYKL_FW_H",
        "",
        "#include <stdint.h>",
        "",
        "/* Task entry: define it with FENYKL_TASK() from fenykl.h. */",
        "typedef void (*fw_task_entry)(void);",
        "",
        "/* Files in plugin.toml cpu1_sources are built with FENYKL_CPU1: calling a CPU0-only function",
        "   from them is a compile error (firmware.toml cores). */",
        "#ifdef FENYKL_CPU1",
        "#define FENYKL_CPU0_ONLY(name) \\",
        '\t__attribute__((error(name " is CPU0-only (firmware.toml cores), not callable from cpu1_sources")))',
        "#else",
        "#define FENYKL_CPU0_ONLY(name)",
        "#endif",
        "",
    ]
    if api.data:
        out += [
            "/* Firmware data (firmware.toml [data]): addresses used as values. */",
            "",
        ]
    for d in api.data.values():
        out += [
            f"/* {d.name}, {d.type}, evidence {d.evidence}: {d.note} */",
            f"#define {d.macro} 0x{d.address:08X}u",
            "",
        ]
    if any(f.variadic for f in api.functions):
        out += NARGS
    for f in api.functions:
        out.append(f"/* {f.name} 0x{f.address:08X}, evidence {f.evidence}: {f.note}")
        if f.cores_evidence:
            out.append(f"   Cores {', '.join(f.cores)}: {f.cores_evidence}")
        if f.variadic:
            out.append(
                f"   Forwards at most {VARIADIC_WORDS} 32-bit variadic words; the build refuses more."
            )
        out[-1] += " */"
        out.append(prototype(f))
        if f.variadic:
            out += variadic_guard(f)
        out.append("")
    if sel.hooks:
        out += [
            "/* Hooks of this plugin (plugin.toml); see FENYKL_HOOK in fenykl.h. */"
        ]
        out += ["struct fenykl_hook;", ""]
    for f in sel.hooks:
        out += _hook_header(f)
    if api.callbacks:
        out += [
            "/* Callback signatures (firmware -> plugin); see FENYKL_CALLBACK in fenykl.h. */",
            "",
        ]
    for cb in api.callbacks.values():
        out += _callback_header(cb)
    if sel.slots:
        out += [
            "/* Slots of this plugin (plugin.toml); see FENYKL_SLOT_REPLACE in fenykl.h. */",
            "struct fenykl_slot;",
            "",
        ]
    for slot in sel.slots:
        out += _slot_header(slot)
    out.append("#endif")
    return "\n".join(out) + "\n"


def thunks(api: Api) -> str:
    return "/* Generated by fenykl from firmware.toml; do not edit. */\n\n" + "\n".join(
        [
            *(thunk_asm(f) for f in api.functions),
            *map(call_thunk_asm, api.callbacks.values()),
        ]
    )


def _entry_stub(wrapper: str, saves: tuple[str, ...], frame: int) -> list[str]:
    """Body of a stub entered by a firmware CALL with operands [saves],frame (`calls`: [],0) that
    calls the GCC `wrapper(r, s)` and returns its result with the matching RET. STUB_FRAME bytes
    below the CALL frame: return slot, GCC home slots, then the D0 D1 A0 A1 snapshot `r`."""
    return [
        f"\tadd -{STUB_FRAME},sp",
        *(f"\tmov {r},({12 + 4 * i},sp)" for i, r in enumerate(STUB_REGS)),
        "\tmov sp,a0",
        "\tadd 12,a0",
        "\tmov a0,d0",
        "\tmov sp,a0",
        f"\tadd {STUB_FRAME + frame + 4},a0",  # the caller's (4,SP): stack arguments
        "\tmov a0,d1",
        f"\tcall {wrapper},[],0",
        f"\tadd {STUB_FRAME},sp",
        f"\tret [{','.join(saves)}],{frame}",
    ]


def _callback_header(cb: Callback) -> list[str]:
    n = cb.name
    entry = (
        "calls" if cb.entry == "calls" else f"call [{','.join(cb.saves)}],{cb.frame}"
    )
    wrap = f"{_returns(cb)}fenykl_cbwrap_{n}_##fn(const uint32_t *r, const uint32_t *s)"
    # Assembly of the entry stub as C string literals; @FN@ becomes the stringified fn.
    stub = [
        f'\t.section .text.fenykl_cb_{n}_@FN@,"ax"',
        f"\t.global _fenykl_cb_{n}_@FN@",
        f"_fenykl_cb_{n}_@FN@:",
        *_entry_stub(f"_fenykl_cbwrap_{n}_@FN@", cb.saves, cb.frame),
        "\t.previous",
    ]
    asm = [
        '\t\t"'
        + line.replace('"', '\\"').replace("\t", "\\t").replace("@FN@", '" #fn "')
        + '\\n"'
        for line in stub
    ]
    asm[0] = "\t__asm__(" + asm[0].lstrip("\t")
    asm[-1] += ")"
    return [
        f"/* Callback {n}, entered by {entry}, evidence {cb.evidence}: {cb.note} */",
        f"typedef {_signature(cb, f'fw_cb_{n}_fn')};",
        f"{_signature(cb, f'fw_call_{n}', ('uint32_t fn',))};",
        f"#define FENYKL_CALLBACK_{n}(fn)" + " \\",
        f"\t{wrap};" + " \\",
        f"\t{wrap} {_wrapper_body(cb, 'fn')}" + " \\",
        f"\t_Static_assert(__builtin_types_compatible_p(__typeof__(fn), fw_cb_{n}_fn), \\",
        f'\t\t       #fn ": not the signature of callback {n} (fw_cb_{n}_fn)");'
        + " \\",
        f"\tvoid fenykl_cb_{n}_##fn(void);" + " \\",
        *(line + " \\" for line in asm[:-1]),
        asm[-1],
        "",
    ]


def _slot_header(slot: Slot) -> list[str]:
    n, cb = slot.name, slot.callback
    out = [
        (
            f"/* Slot {n} 0x{slot.address:08X} ({cb.name}, stock value "
            f"0x{slot.expected:08X}), CPU1 scope {slot.cpu1}: {slot.cpu1_evidence}"
        ),
        f"   {slot.note} */",
        f"extern struct fenykl_slot fenykl_slot_{n};",
        (
            f"#define FENYKL_SLOT_REPLACE_{n}(fn) "
            f"fenykl_slot_replace(&fenykl_slot_{n}, FENYKL_CB({cb.name}, fn))"
        ),
    ]
    if slot.expected:
        names = ", ".join(a.name for a in cb.args)
        result = "" if cb.returns == "void" else "return "
        call = (
            f"fw_call_{cb.name}(0x{slot.expected:08X}u{', ' if names else ''}{names})"
        )
        out.append(
            f"static inline {_signature(cb, f'fw_orig_{n}')} {{ {result}{call}; }}"
        )
    return [*out, ""]


def slot_asm(slot: Slot) -> str:
    """The fenykl_slot_<name> descriptor (struct fenykl_slot in fenykl_target.h)."""
    n = slot.name
    lines = [
        f"/* slot {n} 0x{slot.address:08X} */",
        f'\t.section .data.fenykl_slot_{n},"aw"',
        "\t.balign 4",
        f"\t.global _fenykl_slot_{n}",
        f"_fenykl_slot_{n}:",
        f"\t.long 0x{slot.address:08X}",
        f"\t.long 0x{slot.expected:08X}",
        "\t.long 0",
    ]
    return "\n".join(lines) + "\n"


def hook_asm(func: Function) -> str:
    """Entry stub, trampoline, fw_orig_<name> and the fenykl_hook_<name> descriptor (layout:
    struct fenykl_hook in fenykl.h)."""
    n, h = func.name, func.hook
    assert h is not None
    saves = f"[{','.join(func.saves)}],{func.frame}"
    original = h.relocate.ljust(MAX_RELOCATE, b"\0")
    lines = [
        f"/* hook {n} 0x{func.address:08X}, CALL {saves} */",
        f'\t.section .text.fenykl_hook_{n},"ax"',
        # Entered by the jmp at the entry: the caller's CALL already saved the registers and
        # allocated the frame, which the original RET operands undo at the end.
        f"_fenykl_hookstub_{n}:",
        *_entry_stub(f"_fenykl_hookwrap_{n}", func.saves, func.frame),
        # Entered by CALL from fw_orig_<name> with the original operands: a fresh frame.
        f"_fenykl_hooktramp_{n}:",
        "\t.byte " + ",".join(f"0x{b:02x}" for b in h.relocate),
        f"\tjmp fw_addr_{n}+{len(h.relocate)}",
        *_thunk_body(func, f"fw_orig_{n}", f"_fenykl_hooktramp_{n}"),
        f'\t.section .data.fenykl_hook_{n},"aw"',
        "\t.balign 4",
        f"\t.global _fenykl_hook_{n}",
        f"_fenykl_hook_{n}:",
        f"\t.long fw_addr_{n}",
        f"\t.long _fenykl_hookstub_{n}",
        "\t.long 0",
        f"\t.byte {len(h.relocate)}",
        "\t.byte " + ",".join(f"0x{b:02x}" for b in original),
        "\t.balign 4",
    ]
    return "\n".join(lines) + "\n"


def symbols(funcs: list[Function]) -> str:
    """Linker script fragment; the firmware addresses must be link-time symbols."""
    return "/* Generated by fenykl from firmware.toml; do not edit. */\n" + "".join(
        f"fw_addr_{f.name} = 0x{f.address:08X};\n" for f in funcs
    )


def generate(
    toml: Path,
    outdir: Path,
    objdump: str,
    hooks: Sequence[str] = (),
    cpu1_unproven: Sequence[str] = (),
    slots: Sequence[str] = (),
    sdk_hooks: Sequence[str] = (),
    sdk_slots: Sequence[str] = (),
    sdk_cpu1_unproven: Sequence[str] = (),
) -> None:
    api = load(toml)
    sel = select(
        api, hooks, slots, cpu1_unproven, sdk_hooks, sdk_slots, sdk_cpu1_unproven
    )
    outdir.mkdir(parents=True, exist_ok=True)
    for f in sel.hooks:
        relocated_instructions(f, objdump, outdir)
    (outdir / "fenykl_fw.h").write_text(header(api, sel))
    (outdir / "fw_thunks.S").write_text(thunks(api))
    (outdir / "fw_hooks.S").write_text(
        "/* Generated by fenykl from firmware.toml and plugin.toml; do not edit. */\n\n"
        + "\n".join(
            [
                *map(hook_asm, sel.hooks),
                *map(slot_asm, sel.slots),
            ]
        )
    )
    (outdir / "fw_symbols.ld").write_text(symbols(api.functions))
