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
#   * create-dmg (optional):  brew install create-dmg
#
# Configure via env:
#   SIGN_ID="Developer ID Application: Your Name (TEAMID)"   (required to sign)
#   NOTARY_PROFILE="cortana-notary"                          (required to notarize)
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Cortana.app"
DMG="dist/Cortana.dmg"
ENTITLEMENTS="bundle/entitlements.plist"
CONSTRAINTS="bundle/constraints.txt"
VENV=".venv-build"

echo "==> [1/8] Clean build venv with desktop + MLX + build deps (pinned)"
rm -rf build dist
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
# Constraints pin the full transitive runtime so a rebuild months later ships the
# SAME inference stack that was tested (regenerate deliberately with
# `pip freeze > bundle/constraints.txt` after re-testing).
if [[ -f "$CONSTRAINTS" ]]; then
  "$VENV/bin/pip" install -q -c "$CONSTRAINTS" '.[desktop,mlx,build]'
else
  echo "!! $CONSTRAINTS missing — resolving deps UNPINNED (do not ship this build)"
  "$VENV/bin/pip" install -q '.[desktop,mlx,build]'
fi

echo "==> [2/8] py2app -> $APP"
"$VENV/bin/python" bundle/build_app.py py2app

PYVER="$("$VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
LIBDST="$APP/Contents/Resources/lib/python$PYVER"

echo "==> [3/8] Copy the full mlx package (PEP 420 namespace pkg — py2app can only"
echo "          trace named submodules, which missed mlx._reprlib_fix and mlx/lib dylibs)"
MLX_SRC="$VENV/lib/python$PYVER/site-packages/mlx"
MLX_DST="$LIBDST/lib-dynload/mlx"
if [[ -d "$MLX_SRC/lib" ]]; then
  mkdir -p "$MLX_DST"
  # Whole package: every .py submodule + native lib/ (libmlx.dylib, mlx.metallib).
  # Exclude C++ headers (dead weight in an app bundle).
  rsync -a --exclude 'include' "$MLX_SRC/" "$MLX_DST/"
  echo "   copied: $(ls "$MLX_DST" | tr '\n' ' ')"
else
  echo "!! mlx/lib not found at $MLX_SRC — MLX inference cannot work; aborting"
  exit 1
fi

echo "==> [4/8] Copy dist-info metadata (transformers checks versions via importlib.metadata)"
# Only runtime packages — build tooling metadata must not ship.
for d in "$VENV/lib/python$PYVER/site-packages/"*.dist-info; do
  name="$(basename "$d")"
  case "$name" in
    pip-*|setuptools-*|wheel-*|py2app-*|modulegraph-*|macholib-*|altgraph-*) continue ;;
  esac
  cp -R "$d" "$LIBDST/"
done
echo "   copied $(ls -d "$LIBDST"/*.dist-info | wc -l | tr -d ' ') dist-info dirs"

echo "==> [5/8] Verify the freeze (headless self-check through the app boot path)"
# perl alarm = timeout (macOS has no coreutils timeout); a py2app boot failure pops
# a blocking GUI alert, which without a timeout would hang this script forever.
set +e
CHECK_OUT="$(perl -e 'alarm 180; exec @ARGV' env CORTANA_CHILD=selfcheck \
  "$APP/Contents/MacOS/Cortana" 2>&1)"
CHECK_RC=$?
set -e
if ! grep -q "SELFCHECK OK" <<<"$CHECK_OUT"; then
  echo "!! frozen bundle failed its self-check (rc=$CHECK_RC):"
  tail -20 <<<"$CHECK_OUT"
  exit 1
fi
grep "SELFCHECK OK" <<<"$CHECK_OUT"

if [[ -n "${SIGN_ID:-}" ]]; then
  echo "==> [6/8] codesign INSIDE-OUT with: $SIGN_ID"
  # --deep is deprecated AND skips Mach-O files under Resources/ (where py2app puts
  # the entire Python runtime) — notarization rejects any unsigned Mach-O. So: sign
  # every nested .so/.dylib first, then the frameworks, then the executables, then
  # the bundle. Entitlements go on the executables only.
  find "$APP" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 |
    while IFS= read -r -d '' lib; do
      codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$lib"
    done
  if [[ -d "$APP/Contents/Frameworks" ]]; then
    find "$APP/Contents/Frameworks" -mindepth 1 -maxdepth 1 -print0 |
      while IFS= read -r -d '' fw; do
        codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$fw"
      done
  fi
  codesign --force --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$APP/Contents/MacOS/python"
  codesign --force --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
else
  echo "==> [6/8] SIGN_ID not set — keeping py2app's ad-hoc signature."
  echo "    Downloaders must right-click -> Open once (Gatekeeper); the Screen"
  echo "    Recording grant resets on each rebuild. Set SIGN_ID to fix both."
fi

if [[ -n "${SIGN_ID:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
  echo "==> [7/8] Notarize + staple the APP (so a dragged-out copy verifies offline)"
  ditto -c -k --keepParent "$APP" dist/Cortana-app.zip
  xcrun notarytool submit dist/Cortana-app.zip --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  rm -f dist/Cortana-app.zip
else
  echo "==> [7/8] Skipping notarization (needs SIGN_ID with a Developer ID cert"
  echo "    + NOTARY_PROFILE)."
fi

echo "==> [8/8] Package DMG"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg --volname "Cortana" --app-drop-link 480 170 \
    --icon "Cortana.app" 140 170 --window-size 640 360 "$DMG" "$APP"
else
  echo "   (create-dmg not found; using hdiutil)"
  hdiutil create -volname "Cortana" -srcfolder "$APP" -ov -format UDZO "$DMG"
fi
if [[ -n "${SIGN_ID:-}" && -n "${NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
  xcrun stapler validate "$DMG"
  echo "==> Done: $DMG (notarized — installs cleanly on any Apple-Silicon Mac)"
else
  echo "==> Done: $DMG (un-notarized — downloaders right-click -> Open once)"
fi
