#!/usr/bin/env bash
# Evaluate all enabled strategy YAMLs via the internal strategy engine, then summarize JSONL logs.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
python scripts/run_strategy_review.py "$@"
python scripts/analyze_strategy_logs.py --profile all
