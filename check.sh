#!/usr/bin/env bash
# Verification gate: run the hermetic test suite. Exits 0 on green.
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

exec "$PY" -m pytest "$@"
