#!/usr/bin/env bash
# Run with the project virtual environment or set PYTHON explicitly.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
PYTHON="${PYTHON:-$SCRIPT_DIR/venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
exec "$PYTHON" "$SCRIPT_DIR/smoke_test.py" "${1:-http://localhost:8892}"
