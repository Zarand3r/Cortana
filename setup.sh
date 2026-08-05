#!/usr/bin/env bash
#
# Cortana installer — creates a virtualenv, installs the app, and (optionally)
# pulls a local model. Safe to re-run.
#
#   ./setup.sh                       # full macOS install (desktop app + Ollama model)
#   ./setup.sh --core-only           # CLI only, no native/GUI deps (works anywhere)
#   ./setup.sh --mlx                 # also install the MLX backend
#   ./setup.sh --model llama3.1:8b   # pull a different model
#   ./setup.sh --no-model            # skip the model download
#
set -euo pipefail
cd "$(dirname "$0")"

EXTRAS="desktop"
DESKTOP=1
MODEL="qwen2.5:7b-instruct"
PULL=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --core-only) EXTRAS=""; DESKTOP=0; shift ;;
    --mlx)       EXTRAS="${EXTRAS:+$EXTRAS,}mlx"; shift ;;
    --model)     MODEL="${2:?--model needs a value}"; shift 2 ;;
    --no-model)  PULL=0; shift ;;
    -h|--help)   sed -n '3,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# 1) platform + Python 3.11+
[[ "$(uname)" == "Darwin" ]] || \
  echo "  ! Not macOS — perception + the desktop app need macOS; the core CLI still works."
command -v python3 >/dev/null || { echo "  ✗ python3 not found. Install Python 3.11+."; exit 1; }
python3 - <<'PY' || { echo "  ✗ Cortana needs Python 3.11+"; exit 1; }
import sys
raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)
PY
say "Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') OK"

# 2) venv + install (core is dependency-free; extras add native/GUI/MLX bits)
say "Creating .venv and installing (editable — so updates take effect without reinstall)"
python3 -m venv .venv
./.venv/bin/pip -q install --upgrade pip
if [[ -n "$EXTRAS" ]]; then
  ./.venv/bin/pip -q install -e ".[$EXTRAS]"
  say "Installed cortana[$EXTRAS] (editable)"
else
  ./.venv/bin/pip -q install -e .
  say "Installed cortana (core CLI, editable)"
fi

# 3) local model
if [[ "$PULL" == 1 ]]; then
  if command -v ollama >/dev/null; then
    say "Pulling model: $MODEL"
    ollama pull "$MODEL" || echo "  ! Pull failed — run 'ollama serve' and retry, or use --mlx."
  else
    echo "  ! Ollama not found. Install it from https://ollama.com then: ollama pull $MODEL"
    echo "    (or re-run with --mlx to use mlx-lm instead.)"
  fi
fi

# 4) next steps
cat <<EOF

$(say "Done.")
Activate the environment:   source .venv/bin/activate

Smoke test (no model / no screen needed):
    cortana run --backend fake --demo --ticks 20 --db /tmp/cortana.db
    cortana ask "what was I working on" --backend fake --db /tmp/cortana.db
EOF
if [[ "$DESKTOP" == 1 ]]; then
cat <<EOF
Run the real menu-bar app:
    cortana desktop        # or just: cortana

First launch: System Settings → Privacy & Security → Screen Recording → enable
your terminal (or Cortana.app), then relaunch. Nothing leaves your machine.
Build the distributable app:  ./scripts/build_release.sh
EOF
fi
