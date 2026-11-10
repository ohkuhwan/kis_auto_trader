#!/usr/bin/env python3
"""KIS 주문 상태를 사람이 확인한 뒤 불명확 주문 잠금을 해제하는 도구.

사용법:
  python scripts/resolve_uncertain_order.py 005930 --confirmed

--confirmed는 사용자가 KIS HTS/주문체결/미체결/잔고를 직접 확인했다는 확인 표시입니다.
이 스크립트는 KIS API를 호출하지 않으며, 상태를 스스로 판정하지 않습니다.
"""
import argparse
import json
import os
import time
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.paths import runtime_path
from dotenv import load_dotenv

load_dotenv()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", help="잠금을 해제할 6자리 종목코드")
    parser.add_argument("--confirmed", action="store_true", help="KIS 실제 주문/체결/잔고를 확인했음을 명시")
    args = parser.parse_args()
    if not args.confirmed:
        parser.error("KIS HTS의 당일 주문/체결/미체결/잔고를 확인한 후 --confirmed를 지정해야 합니다.")
    symbol = args.symbol.strip().zfill(6)
    path = runtime_path("uncertain_orders")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"잠금 파일을 읽을 수 없습니다: {path}: {exc}")
        return 2
    if not isinstance(raw, dict):
        print(f"잠금 파일 형식이 올바르지 않습니다(최상위 JSON 객체 필요): {path}")
        return 2
    symbols = {str(x).strip().zfill(6) for x in raw.get("symbols", []) if str(x).strip()}
    order_records = raw.get("orders", [])
    if not isinstance(order_records, list):
        print(f"잠금 파일의 orders 필드가 배열이 아닙니다: {path}")
        return 2
    order_symbols = {str(item.get("symbol", "")).strip().zfill(6)
                     for item in order_records if isinstance(item, dict) and str(item.get("symbol", "")).strip()}
    if symbol not in symbols and symbol not in order_symbols:
        print(f"잠금 종목이 아닙니다: {symbol}")
        return 1
    symbols.discard(symbol)
    # 현재 저장 형식은 orders 배열이 symbols와 별도로 잠금을 복구할 수 있으므로 두 필드를 함께 갱신합니다.
    # symbols만 지우면 재시작 시 orders 항목으로 잠금이 다시 살아나므로 두 곳을 함께 갱신합니다.
    updated = dict(raw)  # 알 수 없는 향후 필드와 감사 정보를 보존합니다.
    updated["symbols"] = sorted(symbols)
    updated["orders"] = [item for item in order_records
                          if not (isinstance(item, dict) and str(item.get("symbol", "")).strip().zfill(6) == symbol)]
    updated["updated_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"잠금 파일 저장에 실패했습니다: {path}: {exc}")
        return 2
    print(f"주문 결과 확인 완료로 표시했습니다: {symbol}. 다음 실행에서 잠금 해제가 반영됩니다.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
