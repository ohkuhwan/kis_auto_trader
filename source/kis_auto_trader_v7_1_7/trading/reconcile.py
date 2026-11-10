"""trading/reconcile.py - broker 잔고/미체결 주문과 로컬 상태를 동기화합니다.

잔고와 미체결 주문은 서로 다른 축으로 대사합니다.
- 잔고: 현재 실제 보유 포지션의 source of truth
- 미체결 주문: 신규 진입/중복 주문을 막는 source of truth
"""

from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import nullcontext
import logging
import os
from trading.symbols import register_symbol, symbol_label

log = logging.getLogger(__name__)


def _first(row: dict, *keys: str, default=""):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _num(value, default=0.0):
    """KIS 문자열 숫자를 float으로 변환합니다."""
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default



def _sync_open_orders_with_history(api, orders, today: str, open_rows: list[dict]) -> dict:
    """미체결 조회에서 사라진 주문은 주문번호별 이력으로 최종 상태를 확인합니다."""
    remote_numbers = {str(_first(r, "odno", "ODNO", "order_no", default="")).strip() for r in open_rows or []}
    before = {o.order_no: o for o in orders.snapshot()}
    preserve_missing = set()
    now = __import__("time").time()
    for order_no, pending in before.items():
        if order_no in remote_numbers or not (pending.cancel_attempted or now - pending.created_ts >= 20.0):
            continue
        try:
            history = api.daily_orders(today, today, order_no=order_no, ccld_dvsn="00")
            matched = [r for r in history or [] if str(_first(r, "odno", "ODNO", "order_no", default="")).strip() == order_no]
            if not matched:
                preserve_missing.add(order_no)
                log.warning("주문번호별 최종 이력 미확인: order=%s symbol=%s; pending 유지", order_no, symbol_label(pending.symbol))
                continue
            orders.sync_order_history(matched)
            row = matched[0]
            filled = int(_num(_first(row, "tot_ccld_qty", "TOT_CCLD_QTY", default=0), 0))
            qty = int(_num(_first(row, "ord_qty", "ORD_QTY", default=pending.qty), pending.qty))
            reported_remaining = _first(row, "rmn_qty", "RMN_QTY", "nccs_qty", "NCCS_QTY", default=None)
            status = str(_first(row, "cncl_yn", "CNCL_YN", "ccld_dvsn_name", "CCLD_DVSN_NAME", default="")).strip().upper()
            cancelled = status in {"Y", "YES", "1"} or "취소" in status or "CANCEL" in status
            remaining = int(_num(reported_remaining, max(0, qty - filled))) if reported_remaining not in (None, "") else max(0, qty - filled)
            # 전체 주문 이력에 잔량이 남아 있고 취소도 확인되지 않으면,
            # 미체결 조회 지연/불일치일 수 있으므로 pending을 제거하지 않습니다.
            if not cancelled and remaining > 0:
                preserve_missing.add(order_no)
                log.warning("주문이력에 잔량 존재, 미체결 pending 유지: order=%s symbol=%s requested=%d cumulative=%d remaining=%d status=%s",
                            order_no, symbol_label(pending.symbol), qty, filled, remaining, status or "unknown")
        except Exception:
            preserve_missing.add(order_no)
            log.exception("주문번호별 최종 이력 조회 실패: order=%s symbol=%s; pending 유지", order_no, symbol_label(pending.symbol))
    result = orders.sync_open_orders(open_rows, preserve_missing_order_nos=preserve_missing)
    log.info(
        "미체결 주문 reconcile: remote_open=%d added=%d updated=%d removed=%d preserved_unconfirmed=%d",
        result["remote"], result["added"], result["updated"], result["removed"], len(preserve_missing),
    )
    return result


def _reconcile_cancel_requests(api, orders, today: str, open_rows: list[dict]) -> dict[str, int]:
    """취소번호/원주문번호를 당일 주문이력에서 대조해 최종 상태를 기록합니다.

    취소번호마다 API를 반복 호출하지 않고 당일 전체 주문이력을 한 번 조회한 뒤
    취소 주문번호와 원주문번호로 각각 매칭합니다. 호출 제한을 불필요하게 늘리지 않으며,
    취소 API 응답만으로 완료 처리하지 않고 미체결 목록까지 교차 확인합니다.
    """
    if not hasattr(orders, "pending_cancel_records"):
        return {"checked": 0, "confirmed": 0, "unconfirmed": 0}
    records = orders.pending_cancel_records(min_interval_sec=60.0)
    if not records:
        return {"checked": 0, "confirmed": 0, "unconfirmed": 0}

    open_numbers = {str(_first(r, "odno", "ODNO", "order_no", default="")).strip()
                    for r in (open_rows or [])}
    checked = confirmed = unconfirmed = 0
    with_cancel_numbers = []
    for record in records:
        original_no = str(record.get("original_order_no", "")).strip()
        cancel_numbers = record.get("cancel_order_numbers", [])
        if not isinstance(cancel_numbers, list):
            cancel_numbers = []
        latest_cancel_no = str(record.get("cancel_order_no", "")).strip()
        cancel_numbers = list(dict.fromkeys([str(x).strip() for x in cancel_numbers if str(x).strip()] + ([latest_cancel_no] if latest_cancel_no else [])))
        if not original_no or not cancel_numbers:
            orders.update_cancel_record(original_no, {"status": "UNCONFIRMED_NO_CANCEL_ORDER_NO"})
            log.warning("취소 최종 확인 보류: 원주문=%s 취소주문번호 없음; 원주문 이력/미체결 조회로 확인", original_no or "unknown")
            unconfirmed += 1
            continue
        record["_cancel_numbers_for_check"] = cancel_numbers
        with_cancel_numbers.append(record)

    # 한 번의 전체 이력 조회로 취소번호와 원주문번호를 함께 찾습니다.
    # 조회 실패 시 미확정 상태를 유지하며 취소 완료로 오판하지 않습니다.
    if not with_cancel_numbers:
        return {"checked": 0, "confirmed": 0, "unconfirmed": unconfirmed}
    try:
        history_rows = api.daily_orders(today, today, ccld_dvsn="00") or []
    except Exception as exc:
        for record in with_cancel_numbers:
            original_no = record["original_order_no"]
            orders.update_cancel_record(original_no, {
                "status": "CANCEL_HISTORY_QUERY_ERROR",
                "last_query_error": f"{type(exc).__name__}: {exc}"[:300],
            })
        log.exception("취소 주문 최종 대사용 당일 전체 주문이력 조회 실패; 미확정 상태 유지")
        return {"checked": 0, "confirmed": 0, "unconfirmed": unconfirmed + len(with_cancel_numbers)}

    rows_by_no = {}
    for row in history_rows:
        number = str(_first(row, "odno", "ODNO", "order_no", default="")).strip()
        if number:
            rows_by_no[number] = row

    for record in with_cancel_numbers:
        original_no = record["original_order_no"]
        cancel_numbers = record["_cancel_numbers_for_check"]
        checked += 1
        cancel_no = next((number for number in reversed(cancel_numbers) if number in rows_by_no), "")
        cancel_row = rows_by_no.get(cancel_no) if cancel_no else None
        original_row = rows_by_no.get(original_no)
        if cancel_row is None:
            orders.update_cancel_record(original_no, {"status": "CANCEL_HISTORY_PENDING"})
            log.info("취소 주문번호 기준 이력 아직 미확인: original_order=%s cancel_orders=%s", original_no, cancel_numbers)
            unconfirmed += 1
            continue
        if original_row is None:
            orders.update_cancel_record(original_no, {"status": "CANCEL_HISTORY_FOUND_ORIGINAL_PENDING",
                                                       "matched_cancel_order_no": cancel_no})
            log.warning("취소 이력은 확인했으나 원주문 이력 미확인: original_order=%s cancel_order=%s", original_no, cancel_no)
            unconfirmed += 1
            continue

        original_qty = int(_num(_first(original_row, "ord_qty", "ORD_QTY", default=record.get("requested_qty", 0)), 0))
        original_filled = int(_num(_first(original_row, "tot_ccld_qty", "TOT_CCLD_QTY", default=0), 0))
        original_remaining_raw = _first(original_row, "rmn_qty", "RMN_QTY", "nccs_qty", "NCCS_QTY", default=None)
        original_remaining = int(_num(original_remaining_raw, max(0, original_qty - original_filled))) if original_remaining_raw not in (None, "") else max(0, original_qty - original_filled)
        original_status = str(_first(original_row, "cncl_yn", "CNCL_YN", "ccld_dvsn_name", "CCLD_DVSN_NAME", default="")).strip()
        cancel_qty = int(_num(_first(cancel_row, "ord_qty", "ORD_QTY", default=0), 0))
        cancel_filled = int(_num(_first(cancel_row, "tot_ccld_qty", "TOT_CCLD_QTY", default=0), 0))
        cancel_remaining_raw = _first(cancel_row, "rmn_qty", "RMN_QTY", "nccs_qty", "NCCS_QTY", default=None)
        cancel_remaining = int(_num(cancel_remaining_raw, max(0, cancel_qty - cancel_filled))) if cancel_remaining_raw not in (None, "") else max(0, cancel_qty - cancel_filled)
        cancel_status = str(_first(cancel_row, "cncl_yn", "CNCL_YN", "ccld_dvsn_name", "CCLD_DVSN_NAME", default="")).strip()
        original_cancelled_flag = original_status.upper() in {"Y", "YES", "1"} or "취소" in original_status or "CANCEL" in original_status.upper()
        cancel_row_flag = cancel_status.upper() in {"Y", "YES", "1"} or "취소" in cancel_status or "CANCEL" in cancel_status.upper()
        terminal = original_no not in open_numbers and original_remaining <= 0
        cancelled_qty = min(max(0, original_qty - original_filled), cancel_qty) if cancel_qty > 0 else (max(0, original_qty - original_filled) if original_cancelled_flag or cancel_row_flag else 0)

        if terminal:
            if cancelled_qty > 0 or original_cancelled_flag or cancel_row_flag:
                final_status = "CONFIRMED_CANCELLED"
            elif original_filled >= original_qty > 0:
                final_status = "CONFIRMED_CLOSED_NO_CANCEL_QTY"
            else:
                final_status = "CONFIRMED_CLOSED_UNCLASSIFIED"
            orders.update_cancel_record(original_no, {
                "status": final_status,
                "matched_cancel_order_no": cancel_no,
                "cancel_history_found": True,
                "cancel_history_status": cancel_status,
                "cancel_history_qty": cancel_qty,
                "cancel_history_filled_qty": cancel_filled,
                "cancel_history_remaining_qty": cancel_remaining,
                "original_final_status": original_status,
                "original_final_qty": original_qty,
                "original_final_filled_qty": original_filled,
                "original_final_remaining_qty": original_remaining,
                "final_cancelled_qty": cancelled_qty,
                "final_checked_ts": __import__("time").time(),
            })
            log.info(
                "취소 주문 최종 대사: result=%s original_order=%s cancel_order=%s symbol=%s side=%s "
                "original_qty=%d original_filled=%d original_remaining=%d original_status=%s "
                "cancel_order_qty=%d cancel_order_filled=%d cancel_order_remaining=%d cancel_status=%s cancelled_qty=%d",
                final_status, original_no, cancel_no, symbol_label(record.get("symbol", "")),
                record.get("side", "UNKNOWN"), original_qty, original_filled, original_remaining,
                original_status or "unknown", cancel_qty, cancel_filled, cancel_remaining,
                cancel_status or "unknown", cancelled_qty,
            )
            confirmed += 1
        else:
            orders.update_cancel_record(original_no, {
                "status": "CANCEL_HISTORY_FOUND_ORIGINAL_STILL_OPEN",
                "matched_cancel_order_no": cancel_no,
                "cancel_history_found": True,
                "cancel_history_status": cancel_status,
                "cancel_history_qty": cancel_qty,
                "cancel_history_filled_qty": cancel_filled,
                "cancel_history_remaining_qty": cancel_remaining,
                "original_final_status": original_status,
                "original_final_qty": original_qty,
                "original_final_filled_qty": original_filled,
                "original_final_remaining_qty": original_remaining,
            })
            log.warning("취소 주문 이력은 있으나 원주문 잔량 종료 미확인: original_order=%s cancel_order=%s original_filled=%d original_remaining=%d open=%s",
                        original_no, cancel_no, original_filled, original_remaining, original_no in open_numbers)
            unconfirmed += 1
    return {"checked": checked, "confirmed": confirmed, "unconfirmed": unconfirmed}


def reconcile(api, store, orders=None, *, state_lock=None) -> dict:
    """broker 잔고와 미체결 주문을 조회해 로컬 상태를 보정합니다.

    API 조회가 성공한 경우에만 해당 결과를 로컬 상태에 반영합니다.
    state_lock이 주어지면 balance snapshot 취득과 포지션 반영을 체결 worker와 직렬화합니다.
    주문이력 조회는 lock 밖에서 수행해 체결 이벤트가 불필요하게 대기하지 않게 합니다.
    """
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")

    with (state_lock if state_lock is not None else nullcontext()):
        # ------------------------------------------------------------
        # 1. 잔고 동기화
        # ------------------------------------------------------------
        holdings, _summary = api.balance()
        # 성공한 잔고 스냅샷 시각을 주문 관리자에 알려, 그 뒤 도착한 지연 체결통보가
        # 이미 잔고에 반영된 체결을 로컬 포지션에 다시 더하지 않도록 합니다.
        if orders is not None and hasattr(orders, "note_balance_reconcile"):
            orders.note_balance_reconcile()
        asset_keys = ('nass_amt', 'NASS_AMT', 'tot_asst_amt', 'TOT_ASST_AMT', 'tot_evlu_amt', 'TOT_EVLU_AMT', 'scts_evlu_amt')
        asset_key = next((k for k in asset_keys if _num(_summary.get(k)) > 0), "")
        assets = _num(_summary.get(asset_key)) if asset_key else 0.0
        log.info("RECONCILE_BALANCE_AUDIT summary_keys=%s asset_source_key=%s asset_value=%.0f rows=%d",
                 sorted(str(k) for k in _summary.keys()) if isinstance(_summary, dict) else type(_summary).__name__,
                 asset_key or "NONE", assets, len(holdings or []))
        if assets > 0:
            had_start_assets = store.state.daily_start_assets_krw > 0
            store.set_daily_start_assets(assets)
            if not had_start_assets and store.state.daily_start_assets_krw > 0:
                log.info("일일 손실 제한 기준자산 설정: %.0f원", store.state.daily_start_assets_krw)
        elif store.state.daily_start_assets_krw <= 0:
            log.warning(
                "일일 손실 제한 기준자산을 잔고 응답에서 확인하지 못했습니다: "
                "summary_keys=%s; 기준자산 확인 전 신규 매수는 차단됩니다.",
                sorted(str(k) for k in _summary.keys()) if isinstance(_summary, dict) else type(_summary).__name__,
            )
        broker_symbols = set()

        for row in holdings:
            symbol = str(row.get("pdno", row.get("PDNO", ""))).strip().zfill(6)
            qty = int(_num(row.get("hldg_qty", row.get("HLDG_QTY", 0)), 0))
            if not symbol or symbol == "000000" or qty <= 0:
                continue

            name = str(row.get("prdt_name", row.get("PRDT_NAME", "")) or "").strip()
            register_symbol(symbol, name)
            old = store.get_position(symbol) or {}
            old_qty = int(old.get("qty", 0) or 0)
            if old_qty != qty:
                log.warning(
                    "잔고 수량 대사 차이: symbol=%s local_before=%d broker_qty=%d delta=%+d; broker 잔고를 기준으로 보정",
                    symbol_label(symbol, name), old_qty, qty, qty - old_qty,
                )
            current = _num(row.get("prpr", row.get("STCK_PRPR", 0)))
            avg = _num(row.get("pchs_avg_pric", row.get("PCHS_AVG_PRIC", 0)))
            high = max(_num(old.get("high_price", 0)), current, avg)

            store.set_position(symbol, {
                "symbol": symbol,
                "name": name or str(old.get("name", "")),
                "qty": qty,
                "avg_price": avg,
                "high_price": high,
                "pending_order_no": old.get("pending_order_no", ""),
                "pending_side": old.get("pending_side", ""),
                "strategy": old.get("strategy", "unified"),
                "last_exit_ts": float(old.get("last_exit_ts", 0) or 0),
                "exit_retry_after_ts": float(old.get("exit_retry_after_ts", 0) or 0),
                "sell_retry_count": int(old.get("sell_retry_count", 0) or 0),
                "yaml_exit_eval_ts": float(old.get("yaml_exit_eval_ts", 0) or 0),
            })
            broker_symbols.add(symbol)

        # broker에서 사라진 종목은 실제 보유가 0이므로 local position에서도 제거합니다.
        # 단, 이 함수의 balance() 자체가 성공했다는 것이 전제입니다.
        removed_symbols = []
        for symbol in list(store.symbols()):
            if symbol not in broker_symbols:
                # WebSocket 체결통보 누락/외부 수동매도도 잔고 대사에서 전량 청산으로
                # 확인되면 재매수 제한을 시작합니다. 로컬 포지션 삭제만 하면 cooldown이
                # 누락되어 재시작 직후 동일 종목을 다시 살 수 있습니다.
                old_position = store.get_position(symbol) or {}
                if int(old_position.get("qty", 0) or 0) > 0:
                    try:
                        configured_cooldown = int(float(os.getenv("REENTRY_COOLDOWN_SEC", "30000") or 30000))
                    except (TypeError, ValueError, OverflowError):
                        configured_cooldown = 30000
                    cooldown_sec = max(30000, configured_cooldown)
                    store.mark_exit_cooldown(symbol, cooldown_sec)
                    log.warning("잔고 대사 전량 청산 확인: symbol=%s reentry_cooldown_sec=%d source=broker_balance",
                                symbol_label(symbol, old_position.get("name", "")), cooldown_sec)
                store.remove_position(symbol)
                removed_symbols.append(symbol)

        # 잔고 API가 성공했다면 미체결 주문 조회가 실패하더라도 잔고 변경은 보존합니다.
        # 앱에서 수동 매도한 뒤 주문조회 API에서 일시 오류가 발생해도 state.json이
        # 이전 보유 수량으로 남는 상황을 방지합니다.
        store.save()

        holdings_desc = []
        for symbol in sorted(broker_symbols):
            pos = store.get_position(symbol) or {}
            holdings_desc.append(
                f"{symbol_label(symbol, pos.get('name', ''))} {int(pos.get('qty', 0) or 0)}주"
            )
        log.info(
            "브로커 잔고 동기화: 보유=%d 제거=%d 목록=[%s]",
            len(broker_symbols), len(removed_symbols),
            ", ".join(holdings_desc) if holdings_desc else "없음",
        )
        log.info("RECONCILE_POSITION_AUDIT broker_symbols=%s removed_symbols=%s local_after=%s",
                 sorted(broker_symbols), sorted(removed_symbols),
                 {symbol: int((store.get_position(symbol) or {}).get("qty", 0) or 0) for symbol in sorted(store.symbols())})
        if removed_symbols:
            log.info("잔고 0으로 로컬 보유 상태 제거: %s", ", ".join(symbol_label(item) for item in sorted(removed_symbols)))

    # ------------------------------------------------------------
    # 2. 미체결 주문 동기화
    # ------------------------------------------------------------
    order_result = {"added": 0, "updated": 0, "removed": 0, "remote": 0}
    if orders is not None:
        # CCLD_DVSN=02: 현재 미체결 주문만 조회합니다.
        # API 호출이 성공하면, broker 목록에서 사라진 local pending은 정리됩니다.
        open_rows = api.daily_orders(today, today, ccld_dvsn="02")
        order_result = _sync_open_orders_with_history(api, orders, today, open_rows)
        cancel_result = _reconcile_cancel_requests(api, orders, today, open_rows)
        if cancel_result["checked"] or cancel_result["unconfirmed"]:
            log.info("취소 요청 대사 요약: checked=%d confirmed=%d unconfirmed=%d",
                     cancel_result["checked"], cancel_result["confirmed"], cancel_result["unconfirmed"])
    else:
        # 호환용: 외부에서 orders를 전달하지 않는 경우에도 API 조회는 유지합니다.
        api.daily_orders(today, today)

    store.save()
    return {
        "positions": len(broker_symbols), "orders": order_result,
        # 시작/종료 계좌 스냅샷에 재사용해 불필요한 추가 잔고 API 호출을 줄입니다.
        "holdings_rows": list(holdings), "summary": dict(_summary or {}),
    }


def reconcile_open_orders(api, orders) -> dict:
    """미체결 주문만 broker와 동기화합니다.

    취소 요청 후 pending이 영구적으로 남는 문제를 해결하기 위해
    별도 주기로 호출합니다.
    """
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    rows = api.daily_orders(today, today, ccld_dvsn="02")
    result = _sync_open_orders_with_history(api, orders, today, rows)
    cancel_result = _reconcile_cancel_requests(api, orders, today, rows)
    if cancel_result["checked"] or cancel_result["unconfirmed"]:
        log.info("취소 요청 대사 요약: checked=%d confirmed=%d unconfirmed=%d",
                 cancel_result["checked"], cancel_result["confirmed"], cancel_result["unconfirmed"])
    sell_orders = [o for o in orders.snapshot() if o.side == "SELL" and o.remaining_qty > 0]
    if sell_orders:
        details = ", ".join(
            f"{symbol_label(o.symbol)} 주문={o.order_no} 잔량={o.remaining_qty}/{o.qty}"
            for o in sell_orders
        )
        log.info("매도 미체결 확인: %s", details)
    return result


def cancel_all_open_orders(api, orders, confirm_timeout_sec: int = 60, poll_sec: int = 5) -> dict:
    """장 종료 시 미체결 주문을 모두 취소 요청하고 KIS 조회로 결과를 확인합니다.

    취소 응답만으로 종료 처리하지 않습니다. 조회가 실패하거나 종료 시점까지
    미체결이 남으면 unresolved로 반환해 로그/알림 대상이 되게 합니다.
    """
    import time
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    rows = api.daily_orders(today, today, ccld_dvsn="02")
    orders.sync_open_orders(rows)
    attempts: dict[str, int] = {}
    for pending in orders.snapshot():
        if getattr(orders, "account_order_locked", False):
            break
        if pending.remaining_qty <= 0:
            continue
        try:
            orders.request_cancel(pending)
            attempts[pending.order_no] = 1
            cancel_record = orders.cancel_records.get(pending.order_no, {})
            log.warning("장 종료 취소 요청: original_order=%s cancel_order=%s symbol=%s side=%s remaining=%d",
                        pending.order_no, cancel_record.get("cancel_order_no", "") or "미발급/확인중",
                        symbol_label(pending.symbol), pending.side, pending.remaining_qty)
        except Exception as exc:
            # 취소 요청 응답도 불명확할 수 있습니다. 중복 취소 요청보다 조회를 우선합니다.
            orders.record_cancel_error(pending, exc)
            attempts[pending.order_no] = 1
            orders.mark_cancel_attempted(pending.order_no)
            log.exception("장 종료 취소 요청 오류; 자동 재전송 대신 KIS 주문상태 재조회: order=%s symbol=%s",
                          pending.order_no, symbol_label(pending.symbol))

    deadline = time.monotonic() + max(0, confirm_timeout_sec)
    while True:
        try:
            rows = api.daily_orders(today, today, ccld_dvsn="02")
            result = orders.sync_open_orders(rows)
            _reconcile_cancel_requests(api, orders, today, rows)
            if not rows:
                log.info("장 종료 미체결 주문 취소 확인 완료: 미체결 0건")
                return {"cancelled_or_closed": True, "open_count": 0, "unresolved": []}
            # 재조회에서도 미체결이면 KIS 상태가 아직 열려 있는 것이므로 취소를 한 번 더 시도합니다.
            open_numbers = {str(_first(r, "odno", "ODNO", "order_no", default="")).strip() for r in rows}
            for pending in orders.snapshot():
                if getattr(orders, "account_order_locked", False):
                    break
                if pending.order_no in open_numbers and attempts.get(pending.order_no, 0) < 2:
                    try:
                        orders.request_cancel(pending)
                        attempts[pending.order_no] = attempts.get(pending.order_no, 0) + 1
                        cancel_record = orders.cancel_records.get(pending.order_no, {})
                        log.warning("장 종료 잔여 미체결 재취소 요청: original_order=%s cancel_order=%s symbol=%s",
                                    pending.order_no, cancel_record.get("cancel_order_no", "") or "미발급/확인중", symbol_label(pending.symbol))
                    except Exception as exc:
                        orders.record_cancel_error(pending, exc)
                        attempts[pending.order_no] = attempts.get(pending.order_no, 0) + 1
                        log.exception("장 종료 재취소 요청 오류: order=%s", pending.order_no)
            if time.monotonic() >= deadline:
                unresolved = [{"order_no": o.order_no, "symbol": o.symbol, "side": o.side,
                               "remaining_qty": o.remaining_qty} for o in orders.snapshot() if o.remaining_qty > 0]
                log.critical("장 종료 후에도 미체결 주문이 남음: %s", unresolved)
                return {"cancelled_or_closed": False, "open_count": len(rows), "unresolved": unresolved}
        except Exception:
            log.exception("장 종료 미체결 주문 최종 확인 실패; 안전상 미확인으로 기록")
            if time.monotonic() >= deadline:
                unresolved = [{"order_no": o.order_no, "symbol": o.symbol, "side": o.side,
                               "remaining_qty": o.remaining_qty} for o in orders.snapshot() if o.remaining_qty > 0]
                return {"cancelled_or_closed": False, "open_count": len(unresolved), "unresolved": unresolved, "query_failed": True}
        time.sleep(max(1, poll_sec))
