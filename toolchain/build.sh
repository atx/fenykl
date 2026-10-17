#!/bin/sh
# Builds the mn10300-elf cross toolchain for fenykl C payloads: GNU binutils + GCC (C only,
# freestanding, libgcc for the base MN10300 ISA only), no libc; plus the GDB instruction-set
# simulator (mn10300-elf-run) used by the ABI tests in toolchain/abitest.
#
#   toolchain/build.sh <download-dir> <build-dir>
#
# Installs into the prefix `fenykl toolchain path` prints: $FENYKL_TOOLCHAIN if set, else
# toolchain/install/ in this repo. JOBS is the make parallelism.
# Tarballs are verified against the sha256 sums below; GMP/MPFR/MPC come from GCC's own
# contrib/download_prerequisites, which pins their versions and checks sha512 sums.
# Needs about 3.5 GB in <build-dir> and network access to ftp.gnu.org and gcc.gnu.org.
#
# Why GCC 16.2.0: the newest release (2026-10). mn10300 is still a supported, non-obsolete target
# there, but it still uses the old reload pass (TARGET_LRA_P false), so it is at risk of removal
# once reload is deleted from GCC. Keep this version pinned unless there is a reason to move.
set -eu

dl=${1:?usage: build.sh <download-dir> <build-dir>}
work=${2:?usage: build.sh <download-dir> <build-dir>}
here=$(cd "$(dirname "$0")" && pwd)
prefix=$(uv run --project "$here/.." fenykl toolchain path)
jobs=${JOBS:-$(nproc)}
target=mn10300-elf

binutils=binutils-2.47
binutils_sha256=154ab23b60070e8f27013c22977f1129425d67d1e8acd6e13010e617811e4cff
gcc=gcc-16.2.0
gcc_sha256=e6738e29597f733270731aa90600f37ffdc045079dfc27ec7e8192cc81085c3e
gdb=gdb-17.2
gdb_sha256=1c036c0d72e4b3d1fb5c94c88632add6f9d76f4d7c4d2ea793c12a9f19a3228c

mkdir -p "$dl" "$work/src"
dl=$(cd "$dl" && pwd)
work=$(cd "$work" && pwd)

fetch() { # <url> <file> <sha256>
	if [ ! -f "$dl/$2" ]; then
		curl -sSfL "$1" -o "$dl/$2.part"
		mv "$dl/$2.part" "$dl/$2"
	fi
	echo "$3  $dl/$2" | sha256sum -c -
}

fetch "https://ftp.gnu.org/gnu/binutils/$binutils.tar.xz" "$binutils.tar.xz" "$binutils_sha256"
fetch "https://ftp.gnu.org/gnu/gcc/$gcc/$gcc.tar.xz" "$gcc.tar.xz" "$gcc_sha256"
fetch "https://ftp.gnu.org/gnu/gdb/$gdb.tar.xz" "$gdb.tar.xz" "$gdb_sha256"

[ -d "$work/src/$binutils" ] || tar -C "$work/src" -xf "$dl/$binutils.tar.xz"
[ -d "$work/src/$gdb" ] || tar -C "$work/src" -xf "$dl/$gdb.tar.xz"
if [ ! -d "$work/src/$gcc" ]; then
	tar -C "$work/src" -xf "$dl/$gcc.tar.xz"
	# GMP/MPFR/MPC are built in-tree; ISL (graphite) is not needed.
	(cd "$work/src/$gcc" && ./contrib/download_prerequisites --no-graphite)
fi

# binutils: only gas/ld/objdump & co. are needed.
mkdir -p "$work/build-binutils"
cd "$work/build-binutils"
"$work/src/$binutils/configure" --target=$target --prefix="$prefix" \
	--disable-nls --disable-werror --disable-gdb --disable-gdbserver --disable-sim \
	--disable-gprof --disable-gprofng --disable-readline --disable-libdecnumber
make -j"$jobs"
make install

# GCC: C only, no libc headers (inhibit_libc), single multilib = base MN10300 ISA
# (the default multilib set would also build am33/am33-2/am34 variants of libgcc).
# --disable-lto/--disable-libssp/--disable-threads: nothing of that exists on the camera.
PATH="$prefix/bin:$PATH"
export PATH
mkdir -p "$work/build-gcc"
cd "$work/build-gcc"
"$work/src/$gcc/configure" --target=$target --prefix="$prefix" \
	--enable-languages=c --without-headers --disable-multilib \
	--disable-nls --disable-shared --disable-threads --disable-lto \
	--disable-libssp --disable-libquadmath --disable-libgomp --disable-libatomic \
	--disable-decimal-float --disable-bootstrap
make -j"$jobs" all-gcc all-target-libgcc
make install-gcc install-target-libgcc

# Simulator only (no gdb): sim/mn10300 implements the base MN10300 ISA plus AM33, used for tests.
mkdir -p "$work/build-sim"
cd "$work/build-sim"
"$work/src/$gdb/configure" --target=$target --prefix="$prefix" \
	--disable-nls --disable-werror --disable-gdb --disable-gdbserver --enable-sim \
	--disable-libdecnumber --disable-gprofng
make -j"$jobs" all-sim
make install-sim

"$prefix/bin/$target-gcc" --version | head -1
"$prefix/bin/$target-as" --version | head -1
