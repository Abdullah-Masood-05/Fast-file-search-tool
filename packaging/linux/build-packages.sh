#!/usr/bin/env bash
# Stage the Nuitka standalone build into an FHS tree and produce the Linux packages:
#   <OUT>/Fast-File-Search-Pro-v<VERSION>-Linux-x86_64.tar.gz   (portable)
#   <OUT>/Fast-File-Search-Pro-v<VERSION>-Linux-x86_64.deb      (nfpm)
#   <OUT>/Fast-File-Search-Pro-v<VERSION>-Linux-x86_64.rpm      (nfpm)
# The staged tree build/linux/root is also what packaging/arch/build-arch.sh consumes.
#
# Usage: packaging/linux/build-packages.sh [DIST_DIR] [OUT_DIR]
#   DIST_DIR  Nuitka standalone folder (default: dist/FastFileSearchPro)
#   OUT_DIR   where packages are written (default: dist/packages)
# Env: VERSION (default: from pyproject.toml), NFPM (path to nfpm; downloaded if absent)
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
NAME=fast-file-search-pro
DIST=$(realpath "${1:-$ROOT/dist/FastFileSearchPro}")
OUT=${2:-$ROOT/dist/packages}
mkdir -p "$OUT"
OUT=$(realpath "$OUT")
VERSION=${VERSION:-$(python3 "$ROOT/build.py" --print-version)}
export VERSION
BASE="Fast-File-Search-Pro-v${VERSION}-Linux-x86_64"
STAGE="$ROOT/build/linux/root"
NFPM_VERSION=2.47.0

[ -x "$DIST/$NAME" ] || { echo "error: $DIST/$NAME not found; run build.py first" >&2; exit 1; }

echo "==> Staging $DIST -> $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/opt/$NAME" "$STAGE/usr/bin"
cp -a "$DIST/." "$STAGE/opt/$NAME/"
chmod -R u=rwX,go=rX "$STAGE/opt/$NAME"
chmod 755 "$STAGE/opt/$NAME/$NAME"
ln -sfn "/opt/$NAME/$NAME" "$STAGE/usr/bin/$NAME"
install -Dm644 "$ROOT/packaging/linux/$NAME.desktop" "$STAGE/usr/share/applications/$NAME.desktop"
for size in 16 32 48 64 128 256; do
  png="$ROOT/packaging/icons/$NAME-$size.png"
  [ -f "$png" ] && install -Dm644 "$png" "$STAGE/usr/share/icons/hicolor/${size}x${size}/apps/$NAME.png"
done

echo "==> Portable tarball"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/$NAME-$VERSION"
cp -a "$DIST/." "$tmp/$NAME-$VERSION/"
cp "$ROOT/packaging/linux/$NAME.desktop" "$tmp/$NAME-$VERSION/"
cp "$ROOT/packaging/icons/$NAME-256.png" "$tmp/$NAME-$VERSION/$NAME.png"
tar -C "$tmp" -czf "$OUT/$BASE.tar.gz" "$NAME-$VERSION"

NFPM_BIN=${NFPM:-$(command -v nfpm || true)}
if [ -z "$NFPM_BIN" ]; then
  echo "==> Downloading nfpm $NFPM_VERSION"
  dl="$tmp/nfpm"
  mkdir -p "$dl"
  url="https://github.com/goreleaser/nfpm/releases/download/v${NFPM_VERSION}"
  tarball="nfpm_${NFPM_VERSION}_Linux_x86_64.tar.gz"
  curl -fsSL -o "$dl/$tarball" "$url/$tarball"
  curl -fsSL -o "$dl/checksums.txt" "$url/checksums.txt"
  (cd "$dl" && grep " $tarball\$" checksums.txt | sha256sum -c -)
  tar -C "$dl" -xzf "$dl/$tarball" nfpm
  NFPM_BIN="$dl/nfpm"
fi

echo "==> deb + rpm via $NFPM_BIN"
cd "$ROOT"
"$NFPM_BIN" package --config packaging/linux/nfpm.yaml --packager deb --target "$OUT/$BASE.deb"
"$NFPM_BIN" package --config packaging/linux/nfpm.yaml --packager rpm --target "$OUT/$BASE.rpm"

ls -l "$OUT"
