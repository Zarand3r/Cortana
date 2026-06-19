#!/usr/bin/env bash
# Install (or uninstall) Cortana as a macOS LaunchAgent so it runs at login and
# restarts on crash. Usage:
#   ./install.sh            install + load
#   ./install.sh uninstall  unload + remove
#
# Prereqs (see README): a .venv with PyObjC installed, a local model (ollama pull),
# and Screen-Recording permission granted to the launching process.
set -euo pipefail

LABEL="com.cortana.tracker"
REPO="$(cd "$(dirname "$0")" && pwd)"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PY="$REPO/.venv/bin/python"

if [[ "${1:-install}" == "uninstall" ]]; then
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "uninstalled ${LABEL}"
  exit 0
fi

if [[ ! -x "$PY" ]]; then
  echo "error: $PY not found. Create the venv first (see README)." >&2
  exit 1
fi

mkdir -p "$(dirname "$PLIST_DST")"
sed -e "s#__PYTHON__#${PY}#g" -e "s#__WORKDIR__#${REPO}#g" \
  "$REPO/dist/${LABEL}.plist" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "installed + loaded ${LABEL}"
echo "  logs:   tail -f ${REPO}/cortana.log"
echo "  stop:   ./install.sh uninstall"
