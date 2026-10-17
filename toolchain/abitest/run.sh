#!/bin/sh
# Builds the ABI bridge test and runs it in the GDB simulator; exit status 0 = pass.
#   abitest/run.sh <build-dir> [extra CFLAGS...]
# Uses the toolchain from toolchain/build.sh at the prefix `fenykl toolchain path` prints.
set -eu
out=${1:?usage: run.sh <build-dir> [CFLAGS...]}
shift
here=$(cd "$(dirname "$0")" && pwd)
bin=$(uv run --project "$here/../.." fenykl toolchain path)/bin/mn10300-elf-
mkdir -p "$out"
cflags=$(cat "$here/../cflags")
# shellcheck disable=SC2086
"${bin}gcc" $cflags "$@" -nostdlib -Wl,--no-warn-rwx-segments -T "$here/link.ld" -o "$out/abitest.elf" \
	"$here/crt0.S" "$here/pana_mock.S" "$here/thunks.S" "$here/test.c" -lgcc
"${bin}run" --memory-region 0x6ea70000,0x80000 "$out/abitest.elf" && status=0 || status=$?
echo "abitest $*: exit status $status"
exit "$status"
