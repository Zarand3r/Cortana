#!/usr/bin/env bash
# Convenience alias for the canonical CI gate. See ci/run.sh.
set -euo pipefail
exec "$(dirname "$0")/ci/run.sh" "$@"
