#!/usr/bin/env bash
# Build the Arch Linux package from the staged tree. Run as root inside archlinux:base-devel
# (makepkg refuses to run as root, so a throwaway build user is created):
#
#   docker run --rm -v "$PWD:/workspace" -w /workspace archlinux:base-devel \
#     bash packaging/arch/build-arch.sh [STAGE_DIR] [OUT_DIR]
#
# STAGE_DIR defaults to build/linux/root (created by packaging/linux/build-packages.sh),
# OUT_DIR to dist/packages. Env: VERSION (default: read from pyproject.toml).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
STAGE=$(realpath "${1:-$ROOT/build/linux/root}")
OUT=${2:-$ROOT/dist/packages}
VERSION=${VERSION:-$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -n1)}
PKGVER=${VERSION//-/_}   # pacman versions may not contain '-'
BUILD=/tmp/archpkg

[ -x "$STAGE/opt/fast-file-search-pro/fast-file-search-pro" ] || {
  echo "error: staged tree not found at $STAGE" >&2; exit 1; }

id builduser >/dev/null 2>&1 || useradd -m builduser
grep -q '^builduser ' /etc/sudoers || echo 'builduser ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers

rm -rf "$BUILD"
mkdir -p "$BUILD/src"
cp -a "$STAGE" "$BUILD/src/root"
sed "s/^pkgver=.*/pkgver=$PKGVER/" "$ROOT/packaging/arch/PKGBUILD" > "$BUILD/PKGBUILD"
chown -R builduser: "$BUILD"
su builduser -c "cd '$BUILD' && PKGEXT=.pkg.tar.zst makepkg -f --nodeps --skipchecksums --noextract"

mkdir -p "$OUT"
pkg=$(ls "$BUILD"/fast-file-search-pro-"$PKGVER"-*-x86_64.pkg.tar.zst)
cp "$pkg" "$OUT/Fast-File-Search-Pro-v${VERSION}-Linux-x86_64.pkg.tar.zst"
ls -l "$OUT"/*.pkg.tar.zst
