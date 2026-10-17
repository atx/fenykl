#!/bin/sh
# Validates the installed toolchain: compiles the corpus with toolchain/cflags (plus -O0 and -O2)
# and runs the ABI bridge test. With --isa-check it also checks the corpus and all of libgcc with
# isacheck/check_isa.py, which needs GHIDRA_INSTALL_DIR and JAVA_HOME and the
# ghidra-mn103 extension installed.
#   toolchain/check.sh [--isa-check] <work-dir>
set -eu
usage="usage: check.sh [--isa-check] <work-dir>"
isa_check=false
if [ "${1-}" = --isa-check ]; then
	isa_check=true
	shift
fi
work=${1:?$usage}
here=$(cd "$(dirname "$0")" && pwd)
MN103_BINUTILS=$(uv run --project "$here/.." fenykl toolchain path)/bin/mn10300-elf-
export MN103_BINUTILS
cflags=$(cat "$here/cflags")

for opt in "" -O0 -O2; do
	obj="$work/corpus$opt"
	mkdir -p "$obj"
	for c in "$here"/isacheck/corpus/*.c; do
		# shellcheck disable=SC2086
		"${MN103_BINUTILS}gcc" $cflags $opt -c "$c" -o "$obj/$(basename "$c" .c).o"
	done
	if $isa_check; then
		"$here/isacheck/check_isa.py" --workdir "$obj/check" "$obj"/*.o
	fi
done
if $isa_check; then
	"$here/isacheck/check_isa.py" --workdir "$work/libgcc-check" "$("${MN103_BINUTILS}gcc" -print-libgcc-file-name)"
fi
for opt in "" -O0 -O2; do
	"$here/abitest/run.sh" "$work/abitest$opt" $opt
done
