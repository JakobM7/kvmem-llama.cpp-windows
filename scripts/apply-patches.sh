#!/usr/bin/env bash
# Keep Linux and Windows patch replay on the same implementation.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "${PYTHON:-python3}" "$ROOT/scripts/apply-patches.py"
