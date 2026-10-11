#!/usr/bin/env bash
# Wrap the app bundle produced by build.py into a compressed, drag-to-install DMG.
#
# Usage: packaging/macos/make-dmg.sh [APP] [OUT_DMG]
#   APP      default: "dist/Fast File Search Pro.app"
#   OUT_DMG  default: dist/Fast-File-Search-Pro-v<VERSION>-macOS-<arch>.dmg
# The app is ad-hoc signed (no Apple Developer ID), so Gatekeeper asks users to
# right-click -> Open on first launch.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
APP_NAME="Fast File Search Pro"
APP=${1:-$ROOT/dist/$APP_NAME.app}
VERSION=${VERSION:-$(python3 "$ROOT/build.py" --print-version)}
ARCH=$(uname -m)   # arm64 or x86_64
OUT=${2:-$ROOT/dist/Fast-File-Search-Pro-v${VERSION}-macOS-${ARCH}.dmg}

[ -d "$APP" ] || { echo "error: $APP not found; run build.py first" >&2; exit 1; }

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
ditto "$APP" "$stage/$APP_NAME.app"
# Re-sign ad hoc (also covers bundles copied from elsewhere) and verify.
codesign --force --deep -s - "$stage/$APP_NAME.app"
codesign --verify --deep --strict "$stage/$APP_NAME.app"
ln -s /Applications "$stage/Applications"

rm -f "$OUT"
# hdiutil occasionally fails with "Resource busy" on CI runners; retry a few times.
for attempt in 1 2 3 4 5; do
  if hdiutil create -volname "$APP_NAME" -srcfolder "$stage" -ov -format UDZO "$OUT"; then
    break
  fi
  [ "$attempt" = 5 ] && { echo "error: hdiutil failed" >&2; exit 1; }
  echo "hdiutil failed (attempt $attempt), retrying..."
  sleep $((attempt * 5))
done
hdiutil verify "$OUT"
ls -l "$OUT"
