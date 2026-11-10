#!/usr/bin/env python3
"""계좌/API 신청정보 검증 완료 후 계좌 전체 주문 잠금을 운영자가 해제합니다.

사용 예:
  python3 scripts/unlock_account_order_lock.py --confirm-account-verified --reason "새 모의계좌 API 신청 및 조회 검증 완료"

잠금은 자동 해제되지 않습니다. 실행자는 KIS Developers 신청 계좌, HTS ID,
App Key/Secret, CANO/상품코드, REST 잔고 조회와 WebSocket 연결을 먼저 확인해야 합니다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from config.paths import runtime_path


def main() -> int:
    parser = argparse.ArgumentParser(description="KIS 계좌 전체 주문 잠금 수동 해제")
    parser.add_argument("--confirm-account-verified", action="store_true",
                        help="계좌/API 신청정보 및 REST·WebSocket 연결 검증을 완료했음을 확인")
    parser.add_argument("--reason", required=True, help="해제 사유(최소 10자)")
    args = parser.parse_args()
    if not args.confirm_account_verified:
        parser.error("--confirm-account-verified 확인 옵션이 필요합니다.")
    reason = str(args.reason or "").strip()
    if len(reason) < 10:
        parser.error("--reason은 10자 이상으로 입력해야 합니다.")

    path = runtime_path("account_order_lock")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"활성 계좌 주문 잠금 파일이 없습니다: {path}")
        return 0
    except (OSError, json.JSONDecodeError) as exc:
        print(f"잠금 파일을 읽을 수 없어 해제하지 않습니다: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict) or not isinstance(data.get("locked"), bool):
        print("잠금 파일 구조가 잘못되어 해제하지 않습니다.", file=sys.stderr)
        return 2
    if not data["locked"]:
        print("이미 해제된 상태입니다.")
        return 0

    data.update({
        "locked": False,
        "released_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
        "released_by": os.getenv("USER", "operator"),
        "release_reason": reason,
        "manual_release_confirmed": True,
    })
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        os.chmod(path, 0o600)
    except OSError as exc:
        print(f"잠금 해제 기록 저장 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    print(f"계좌 전체 주문 잠금을 수동 해제했습니다: {path}")
    print("프로그램을 재시작한 뒤 잔고 조회와 WebSocket 연결을 다시 확인하십시오.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
