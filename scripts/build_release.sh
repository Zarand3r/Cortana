#!/usr/bin/env bash
# Build the single distributable artifact: a signed, notarized Cortana.dmg.
#
#   ./scripts/build_release.sh
#
# End state: a user double-clicks Cortana.dmg, drags Cortana.app to Applications,
# launches it, and it lives in the menu bar (top of screen) — no Terminal, no venv.
# First launch downloads the model once, then it runs fully offline.
#
# Prerequisites (Apple Developer ID path — chosen for clean install + TCC persistence):
#   * Apple Silicon Mac, Xcode command line tools.
#   * A "Developer ID Application" cert in your keychain.
#   * A notarytool keychain profile:
#       xcrun notarytool store-credentials cortana-notary \
#         --apple-id "<you@apple>" --team-id "<TEAMID>" --password "<app-specific-pw>"
#   * create-dmg:  brew install create-dmg
#
# Configure via env:
#   SIGN_ID="Developer ID Application: Your Name (TEAMID)"   (required to sign)
#   NOTARY_PROFILE="cortana-notary"                          (required to notarize)
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Cortana.app"
DMG="dist/Cortana.dmg"
ENTITLEMENTS="packaging/entitlements.plist"
VENV=".venv-build"

echo "==> [1/6] Clean build venv with desktop + MLX + build deps"
rm -rf build dist "$APP" "$DMG"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q '.[desktop,mlx,build]'

echo "==> [2/6] py2app -> $APP"
"$VENV/bin/python" packaging/build_app.py py2app

if [[ -z "${SIGN_ID:-}" ]]; then
  echo "!! SIGN_ID not set — built an UNSIGNED $APP (fine for local use; others will"
  echo "   hit Gatekeeper and the Screen Recording grant may reset on rebuild)."
  echo "   Set SIGN_ID + NOTARY_PROFILE to produce a distributable notarized DMG."
  exit 0
fi

echo "==> [3/6] codesign (hardened runtime, deep) with: $SIGN_ID"
codesign --deep --force --options runtime --timestamp \
  --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$APP"
codesign --verify --strict --verbose=2 "$APP"

echo "==> [4/6] Package DMG -> $DMG"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg --volname "Cortana" --app-drop-link 480 170 \
    --icon "Cortana.app" 140 170 --window-size 640 360 "$DMG" "$APP"
else
  echo "   (create-dmg not found; falling back to hdiutil)"
  hdiutil create -volname "Cortana" -srcfolder "$APP" -ov -format UDZO "$DMG"
fi

if [[ -z "${NOTARY_PROFILE:-}" ]]; then
  echo "!! NOTARY_PROFILE not set — DMG is signed but NOT notarized. Set it to finish."
  exit 0
fi

echo "==> [5/6] Notarize (submit + wait) via profile: $NOTARY_PROFILE"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait

echo "==> [6/6] Staple the notarization ticket"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

echo "==> Done: $DMG  (notarized — installs cleanly on any Apple-Silicon Mac)"
