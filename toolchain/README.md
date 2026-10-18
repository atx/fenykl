# mn10300-elf toolchain for C plugins

GCC + binutils for the E-PL3 main CPU (Panasonic MN1030/MN103S, manual 13250-040E), C only,
freestanding, no libc, plus the GDB simulator for tests.

| file | purpose |
|---|---|
| `build.sh <download-dir> <build-dir>` | downloads (sha256-pinned), builds and installs into the toolchain prefix (see Location) |
| `install/` | default toolchain prefix (empty in git) |
| `cflags` | the compiler flags for plugin code (one line, read by the scripts) |
| `check.sh [--isa-check] <work-dir>` | validation: compiles the corpus at three -O levels, ABI test; `--isa-check` adds the ISA check of the corpus and of all of libgcc |
| `isacheck/check_isa.py` | checks objects/archives/ELFs for non-MN103 instructions (see below) |
| `isacheck/MN103Forms.java` | Ghidra script: instruction-form histogram with the ghidra-mn103 decoder |
| `isacheck/fw-forms.tsv` | that histogram for the stock v1.6 firmware (3,150,911 instructions, 230 forms) |
| `isacheck/corpus/*.c` | C corpus exercising arithmetic, memory, control flow, ABI corners, soft float |
| `abitest/` | C <-> firmware calling-convention bridge, run in the simulator |

The calling conventions are described in the `py/fenykl/fwapi.py` module docstring.

## Location

The toolchain prefix is `$FENYKL_TOOLCHAIN` if set, else `toolchain/install/` in this repository.
`fenykl toolchain path` prints it; `build.sh`, `check.sh`, `abitest/run.sh` and `fenykl plugin build`
all use that one lookup (`py/fenykl/sdk.py`). To keep a toolchain built elsewhere, point
`FENYKL_TOOLCHAIN` at its prefix (the directory holding `bin/mn10300-elf-gcc`) or make
`toolchain/install` a symlink to it.

## Versions

| component | version | notes |
|---|---|---|
| GCC | 16.2.0 | newest release (2026-10). mn10300 is still a normal, non-obsolete target in 16.2 and on master (`config.gcc` obsolete list: ia64, epiphany, rl78). It still uses the old reload pass (`TARGET_LRA_P` false); GCC 15's release notes announced reload removal "for GCC 16" together with the non-LRA targets, which has not happened yet. Expect the port to disappear once reload is removed, so keep this version pinned. |
| binutils | 2.47 | |
| GMP/MPFR/MPC | 6.3.0/4.2.2/1.3.1 | in-tree, via GCC's `contrib/download_prerequisites` (sha512-checked) |
| GDB (simulator only) | 17.2 | `mn10300-elf-run` |

GPG signatures of the three GNU tarballs were checked against `gnu-keyring.gpg` once; `build.sh`
pins their sha256. Configure flags are commented in `build.sh`: C only, `--without-headers`
(libgcc built with `inhibit_libc`), `--disable-multilib` (the default multilib set would add
am33/am33-2/am34 libgcc variants; only the base ISA is built), no LTO/threads/libssp/NLS.
Build time about 15 min on 12 jobs; the build tree needs about 3.5 GB.

## Compiler flags

`cflags`: `-Os -ffreestanding -mno-mult-bug -fno-common -ffunction-sections -fdata-sections
-fno-unwind-tables -fno-asynchronous-unwind-tables -Wall -Wextra`.
Link: `mn10300-elf-gcc -nostdlib -T <script> ... -lgcc`, add `-Wl,--gc-sections` to drop unused code.

| option | choice | why |
|---|---|---|
| CPU | default (`mn10300`); never `-mam33`, `-mam33-2`, `-mam34` | the manual has no AM33 instructions; the default emits the base ISA and sets `e_flags` E_MN10300_MACH_MN10300 (0x810000). `-mam33` output fails the ISA check (negative control: 70 errors, e.g. `mov_mov` LIW, `f9..fd` forms that decode as UDF20-35) |
| `-mno-mult-bug` | use | default `-mmult-bug` puts `nop; nop` before every `mul`/`mulu` (`mn10300.md`) to dodge a bug in early MN10300 silicon. The manual lists no such erratum and the vendor compiler on this chip never pads: 66 `mul`/`mulu` in the firmware, none preceded by NOP, some directly after a load of the operand. Low-risk fallback: drop the flag (costs 2 bytes per multiply). libgcc is built with the default and pads, which is harmless |
| `-mreturn-pointer-on-d0` | default (on) | pointer results go to A0 and D0; the firmware reads pointers from A0, so this is compatible either way |
| `-mliw`, `-msetlb` | irrelevant | both default on, but `mn10300_reorg` only runs them for AM33 targets; base code never gets LIW or SETLB/Lcc |
| `-mrelax` | do not use | passed to ld as `--relax`; ld then shortens calls and can move callee prologues (`movm`/`add`) into the callers' CALL operands, i.e. turn GCC functions into Panasonic-style ones. That changes the calling contract the bridge relies on and is not covered by any check here |
| `-mno-crt0` | not needed | only affects linking `crt0.o`, which `-nostdlib` drops anyway (and there is no crt0 without newlib) |
| `-mtune=` | not needed | scheduling is disabled for the base ISA anyway (`mn10300_option_override`) |
| `-ffreestanding` | use | implies `-fno-builtin`; no hosted assumptions. GCC still emits calls to `memcpy`/`memset` for struct copies and clears, so the payload must define them (in C; the firmware's versions use the Panasonic ABI) |
| `-nostdlib` + `-lgcc` | use | no libc/crt0; libgcc supplies 64-bit shifts/divisions, bit builtins and soft float. 32-bit multiply/divide and 64-bit multiply are inline |
| PIC | default `-fno-pic` | the loader places the payload at the fixed address 0x6EA70000; `-fpic` is supported by the port but not needed or tested |
| float | avoid in hot code | no FPU: `float`/`double` work through libgcc soft-float (`fp-bit`), checked by the ISA check |
| `-Os` | default | payload limit is 488 KiB; `-O0` and `-O2` pass the same checks |
| `-fno-common`, `-ffunction-sections`, `-fdata-sections` | use | duplicate-definition errors and section GC |
| unwind tables off | use | C, no exceptions; saves `.eh_frame` |

## ISA validation (`check_isa.py`, optional)

GCC's mn10300 port also targets AM33/AM33-2/AM34, supersets of the MN1030/MN103S core in the
E-PL3. Their extra instructions are not in the CPU's manual and may not execute on the camera:
several of their encodings are undefined (UDF) opcodes on MN103, which trap or do something else.
The check guards against such code reaching a plugin, e.g. from `-mam33`, inline asm, a changed
`cflags` or a different GCC/libgcc, and lists valid MN103 instruction forms the stock firmware never
uses (`fw-forms.tsv`), which have no evidence of running on this silicon. With the pinned toolchain and `cflags` it has never found
anything (results below), so it is opt-in: `fenykl plugin build --isa-check` runs it on every
`plugin.elf`, `check.sh --isa-check` on the corpus and libgcc. It needs Ghidra
(`GHIDRA_INSTALL_DIR`, `JAVA_HOME`) with the ghidra-mn103 extension installed, which is not part of
this repository, and fails with an error if they are missing. No other build step needs it. Run it after changing the toolchain, `cflags` or inline asm.

For each input it checks:

1. `e_flags` machine is the base MN10300 for every object/archive member;
2. every instruction objdump finds in the code is re-decoded by the ghidra-mn103 SLEIGH module
   (built from 13250-040E only) with the same length and not as UDF (several AM33 encodings decode
   as UDF20-35 on MN103, so this is checked explicitly). Exception: `udf00`, `udf01`, `udfu01` in the
   forms the firmware uses (its multiply unit, ~12,000 uses, also in CPU1 code; inline asm only,
   e.g. `sdk/projects/nnbench`);
3. every instruction form (mnemonic, operand kinds, length) is looked up in `fw-forms.tsv`, the
   histogram of what the stock firmware's code uses; forms the firmware never uses are listed.

Results (2026-10-13, `check.sh --isa-check`): corpus with `cflags` 851 instructions / 110 forms, at `-O0`
1,972 / 94, at `-O2` 1,264 / 114, all of libgcc 13,189 / 124; **0 errors and 0 forms that the
firmware does not use**. All 145 distinct forms are in the firmware; the rarest one there is
`mov (d32,An),An` (2 uses), then `movbu (abs16)` / `movbu (d32,An)` forms (3-6 uses).
The linked ABI test image fails only on the simulator's `syscall` (`f0c0`, sim-only start-up code),
as expected.

`fw-forms.tsv` is regenerated from a Ghidra project of the v1.6 firmware with the ghidra-mn103 decoder:

```sh
$GHIDRA_INSTALL_DIR/support/analyzeHeadless <project-dir> epl3_v16 -process -noanalysis -readOnly \
  -scriptPath fenykl/toolchain/isacheck -postScript MN103Forms.java fenykl/toolchain/isacheck/fw-forms.tsv
```

## ABI test (`abitest/`)

`pana_mock.S` contains functions written like the firmware's (no prologue, CALL-supplied frame,
pointer args in A0/A1, stack args, sprintf-style variadic, a callback through `calls`), each
clobbering everything its CALL saved plus MDR. `thunks.S` bridges C to them and back, `test.c`
checks the results and that callee-saved registers survive in both directions. `run.sh` links at
0x6EA70000 and runs it in `mn10300-elf-run`; exit status 0 = pass. Passes at `-Os`, `-O0`, `-O2`.
Negative controls (wrong CALL frame, wrong stack-arg offset, wrong register mapping, wrong
variadic slot) fail with a crash or the expected check bit.

Limits: the simulator implements CALL/RET from the same manual, and the mocks encode the ABI as
read from the firmware; the test proves the thunks and GCC's side, not the
firmware model itself. Nothing here has run on the camera yet.
