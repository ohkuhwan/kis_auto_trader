#!/usr/bin/env python3
"""현재 KIS 계정에서 조회되는 HTS 서버 저장 조건명과 .env 설정을 비교합니다.

API Key/Secret, 계좌번호, 토큰은 출력하지 않습니다. 실제 주문은 제출하지 않습니다.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings  # noqa: E402
from brokers.kis.client import KisApi, KisApiError  # noqa: E402


def main() -> int:
    """HTS 조건 목록과 통합 KIS_CONDITION_NAME 설정을 비교합니다."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    print(f"KIS 환경: {'모의투자' if settings.paper else '실전'}")
    print(f"HTS ID 설정 여부: {'설정됨' if settings.hts_id else '비어 있음'}")
    if not settings.hts_id:
        print("오류: .env의 KIS_HTS_ID가 비어 있습니다.", file=sys.stderr)
        return 2
    api = KisApi(settings)
    try:
        rows = api.condition_titles(force_refresh=True)
    except Exception as exc:
        # 예외에 포함될 수 있는 URL/응답 전문은 출력하지 않고 오류 종류와 짧은 설명만 출력합니다.
        message = str(exc).replace(settings.app_key, "[REDACTED]").replace(settings.app_secret, "[REDACTED]")
        print(f"HTS 조건 목록 조회 실패: {type(exc).__name__}: {message[:240]}", file=sys.stderr)
        return 1
    available = [str(row.get("condition_nm", "")).strip() for row in rows if str(row.get("condition_nm", "")).strip()]
    print(f"조회된 HTS 조건 수: {len(available)}")
    if available:
        for name in available:
            print(f"  - {name}")
    else:
        print("  (조건 없음)")
    checks = (("통합 HTS 조건", settings.condition_name),)
    all_ok = True
    for label, configured in checks:
        if not configured:
            print(f"{label}: 조건명이 비어 있어 HTS 조건검색이 비활성화됩니다.")
        elif configured in available:
            print(f"{label}: OK - '{configured}'")
        else:
            all_ok = False
            print(f"{label}: 불일치 - .env='{configured}' / HTS 조회 목록에 없음")
    if not all_ok:
        print("조치: HTS에서 조건식을 서버 저장하고 KIS_HTS_ID 및 KIS_CONDITION_NAME 철자를 확인하세요.")
        return 3
    print("조건명 진단 완료: 설정된 조건은 조회 목록에 존재합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
