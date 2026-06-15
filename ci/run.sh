#!/usr/bin/env bash
# Canonical CI gate for Cortana. Runs the full hermetic test suite with branch
# coverage and fails if coverage drops below the threshold. No PyObjC / Ollama /
# network required — everything runs against fakes + temp SQLite.
#
# Run this after every significant change:  ./ci/run.sh
# Pass extra pytest args through, e.g.:      ./ci/run.sh -k memory
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

COV_MIN="${COV_MIN:-95}"

exec "$PY" -m pytest \
  --cov=cortana \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under="$COV_MIN" \
  "$@"
