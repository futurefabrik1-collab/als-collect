#!/bin/bash
# ALS Collect macOS release: build -> Developer ID sign (hardened runtime) -> self-test
# -> notarize + staple the app -> DMG -> sign, notarize + staple the DMG.
#
#   app/release-macos.sh [--no-notarize] [--selftest "<Project folder or .als>"]
#
# Adapted from Tunefield's packaging/release-macos.sh (same identity + notary profile):
#   * nested Mach-O files are signed one by one first (runtime + timestamp, no
#     entitlements), then Python.framework, then the main executable and the bundle
#     with app/entitlements.plist on top;
#   * the app is stapled BEFORE it is copied into the DMG, so the copy carries a ticket;
#   * notarytool credentials: keychain profile $NOTARY_PROFILE (default tunefield-notary).
# Never notarize unless Mark said so for this release (--no-notarize stops before Apple).
set -euo pipefail
cd "$(dirname "$0")/.."

NOTARIZE=1
SELFTEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-notarize) NOTARIZE=0 ;;
    --selftest) SELFTEST="$2"; shift ;;
    *) echo "usage: app/release-macos.sh [--no-notarize] [--selftest <Project folder or .als>]"; exit 2 ;;
  esac
  shift
done

ID="${SIGN_IDENTITY:-Developer ID Application: Mark Burnett (6R865P6UAX)}"
PROFILE="${NOTARY_PROFILE:-tunefield-notary}"
ENT=app/entitlements.plist
VER=$(python3 -c "import als_collect; print(als_collect.VERSION)")
grep -q "'CFBundleShortVersionString': '$VER'" app/als_collect.spec || {
  echo "app/als_collect.spec CFBundleShortVersionString is not $VER"; exit 1; }
APP="app/dist/ALS Collect.app"
DMG="app/dist/ALS-Collect-$VER-arm64.dmg"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

echo "== build ($VER) =="
app/build_app.sh

echo "== sign =="
# every nested Mach-O first (dylibs, extension modules, framework binaries)
find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -print0 |
  xargs -0 codesign --force --options runtime --timestamp --sign "$ID"
if [ -d "$APP/Contents/Frameworks/Python.framework" ]; then
  codesign --force --options runtime --timestamp --sign "$ID" "$APP/Contents/Frameworks/Python.framework"
fi
codesign --force --options runtime --timestamp --entitlements "$ENT" --sign "$ID" "$APP/Contents/MacOS/ALS Collect"
codesign --force --options runtime --timestamp --entitlements "$ENT" --sign "$ID" "$APP"
codesign --verify --deep --strict --verbose=1 "$APP"
echo "signature verifies"

if [ -n "$SELFTEST" ]; then
  echo "== self-test of the SIGNED bundle (scan, preview, media server, GM player) =="
  OUT="$STAGE/selftest.json"
  if ! "$APP/Contents/MacOS/ALS Collect" --selftest "$SELFTEST" "$OUT"; then
    cat "$OUT" 2>/dev/null || true; echo "self-test FAILED — not shipping this build"; exit 1
  fi
  cat "$OUT"
fi

if [ "$NOTARIZE" = "1" ]; then
  echo "== notarize app (waits for Apple) =="
  ditto -c -k --keepParent "$APP" "$STAGE/app.zip"
  xcrun notarytool submit "$STAGE/app.zip" --keychain-profile "$PROFILE" --wait
  xcrun stapler staple "$APP"
fi

echo "== dmg =="
mkdir -p "$STAGE/dmg"
ditto "$APP" "$STAGE/dmg/ALS Collect.app"
ln -s /Applications "$STAGE/dmg/Applications"
rm -f "$DMG"
hdiutil create -volname "ALS Collect $VER" -srcfolder "$STAGE/dmg" -ov -format UDZO "$DMG" -quiet
codesign --force --timestamp --sign "$ID" "$DMG"

if [ "$NOTARIZE" = "1" ]; then
  echo "== notarize dmg (waits for Apple) =="
  xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  spctl -a -t exec -vv "$APP"
  spctl -a -t open --context context:primary-signature -vv "$DMG"
else
  echo "== notarize SKIPPED (--no-notarize): signed but not notarized; Gatekeeper on other Macs will refuse it =="
fi
echo "built: $DMG"
