#!/bin/sh
# Build a double-clickable "ALS Collect.app" (macOS) with PyInstaller.
# Result: app/dist/ALS Collect.app  — drag it to /Applications if you like.
# The bundle is ad-hoc signed: fine on this Mac; a copy on another Mac needs
# right-click → Open once (or Developer ID signing + notarization).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
VENV="${ALS_COLLECT_VENV:-$HOME/.venvs/als-collect}"
[ -x "$VENV/bin/pyinstaller" ] || "$HERE/run.sh" --version >/dev/null 2>&1 || true
cd "$HERE"
rm -rf build dist
"$VENV/bin/pyinstaller" --noconfirm --clean als_collect.spec
codesign --force --deep --sign - "dist/ALS Collect.app" 2>/dev/null || true
echo "built: $HERE/dist/ALS Collect.app"
