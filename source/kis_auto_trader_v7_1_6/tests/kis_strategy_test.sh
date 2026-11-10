#!/usr/bin/env bash
# V7.0.0 전략 YAML 10개/시그널 테스트 실행기.
# 기본은 KIS 일봉 조회만 수행하며 주문 API를 호출하지 않습니다.
# 사용 예: ./tests/kis_strategy_test.sh --sample-size 5 --profile both
# 오프라인 검증: ./tests/kis_strategy_test.sh --offline --sample-size 10 --seed 42
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# setup.sh installs dependencies into the project-local .venv. Prefer that
# interpreter so the dependency check and the actual test use the same environment.
# PYTHON_BIN remains an explicit override for virtualenvs managed by the operator.
if [[ -n "${PYTHON_BIN:-}" ]]; then
  : # Keep the explicitly requested interpreter.
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
else
  # Compatibility fallback for installations that intentionally use system Python.
  PYTHON_BIN="python3"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[오류] Python 실행 파일을 찾을 수 없습니다: $PYTHON_BIN" >&2
  echo "       ./scripts/setup.sh를 실행하거나 PYTHON_BIN으로 올바른 Python 경로를 지정하세요." >&2
  exit 2
fi
if [[ ! -f .env && " ${*} " != *" --offline "* ]]; then
  echo "[오류] .env가 없습니다. KIS API 시세 테스트는 .env를 설정하거나 --offline으로 실행하세요." >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import yaml, requests, dotenv' >/dev/null 2>&1; then
  echo "[오류] 선택된 Python($PYTHON_BIN)에 테스트 의존성이 없습니다." >&2
  echo "       먼저 ./scripts/setup.sh 를 실행하세요." >&2
  echo "       진단: $PYTHON_BIN -c 'import yaml, requests, dotenv'" >&2
  exit 2
fi
exec "$PYTHON_BIN" tests/kis_strategy_test.py "$@"
