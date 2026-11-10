#!/usr/bin/env bash
# Analyze strategy signal JSONL. Usage: scripts/analyze_strategy_logs.sh [path ...]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
if [[ "$#" -eq 0 ]]; then
  exec python scripts/analyze_strategy_logs.py
fi
exec python scripts/analyze_strategy_logs.py "$@"
