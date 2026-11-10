"""trading/orders.py - 주문 상태 머신, 체결통보, 취소/재조회 동기화.

주문 상태 관리 원칙
1. 주문 API의 성공은 '접수 성공'일 뿐 실제 체결이 아니다.
2. 실제 체결은 WebSocket 체결통보(CNTG_YN=2) 또는 broker 주문조회로 확정한다.
3. timeout 취소 요청만으로 pending 주문을 삭제하지 않는다.
4. broker의 '미체결 주문' 조회에서 사라진 주문은 로컬 pending에서도 제거한다.
5. 프로그램 재시작 후에도 broker 미체결 주문을 다시 읽어 중복 주문을 방지한다.
"""

from __future__ import annotations

import threading
import time
import logging
from trading.symbols import symbol_label
import json
import os
from pathlib import Path
from dataclasses import dataclass

from brokers.kis.client import AmbiguousOrderError, KisApiError
from config.paths import runtime_path

order_log = logging.getLogger("trading.order")


_ACCOUNT_ORDER_ERROR_CODES = {"40910000", "IGW00002", "90070000"}


class AccountOrderLockedError(RuntimeError):
    """계좌/인증 오류로 BUY·SELL 신규 주문이 잠긴 상태를 나타냅니다."""
    account_order_locked = True

    def __init__(self, lock: dict | None = None):
        self.lock = dict(lock or {})
        self.msg_cd = str(self.lock.get("msg_cd", "") or "")
        self.reason = str(self.lock.get("msg1", self.lock.get("reason", "")) or "")
        super().__init__(f"계좌 전체 주문 잠금 상태: msg_cd={self.msg_cd or 'UNKNOWN'}; {self.reason}")


def _int(value, default: int = 0) -> int:
    """KIS 문자열 숫자를 안전하게 int로 변환합니다."""
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    """KIS 문자열 숫자를 안전하게 float으로 변환합니다."""
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _first(row: dict, *keys: str, default=""):
    """KIS 응답 버전/필드명 차이를 흡수하기 위한 첫 값 선택 함수입니다."""
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


@dataclass
class PendingOrder:
    """broker에서 아직 최종 상태가 확인되지 않은 주문입니다."""
    order_no: str
    branch_no: str
    symbol: str
    side: str
    qty: int
    filled_qty: int = 0
    created_ts: float = 0.0
    cancel_attempted: bool = False
    last_notice_key: str = ""
    seen_notice_keys: list[str] | None = None
    strategy: str = "unified"
    reservation_price: float = 0.0
    rest_filled_qty: int = 0
    rest_filled_amount: float = 0.0
    notice_filled_qty: int = 0
    rest_sync_ts: float = 0.0
    rest_unmatched_qty: int = 0
    position_qty_before_submit: int = 0
    # 취소 응답으로 발급된 별도 취소 주문번호(원주문번호와 구분)
    cancel_order_no: str = ""
    cancel_order_branch_no: str = ""
    cancel_requested_ts: float = 0.0

    @property
    def remaining_qty(self) -> int:
        """현재 미체결 잔량입니다."""
        return max(0, self.qty - self.filled_qty)


@dataclass
class FillEvent:
    """체결통보 한 건을 main/state가 처리할 수 있는 형태로 정규화합니다."""
    order_no: str
    symbol: str
    side: str
    qty: int
    price: float
    event_ts: float
    first_fill: bool = False
    strategy: str = "unified"
    cumulative_filled_qty: int = 0
    remaining_qty: int = 0
    position_qty_before_submit: int = 0


class OrderManager:
    """주문 제출/체결/timeout/취소/브로커 재조회를 하나의 상태로 관리합니다."""

    def __init__(self, api, timeout_sec: int, buy_timeout_sec: int | None = None, sell_timeout_sec: int | None = None):
        self.api = api
        # timeout_sec는 구버전 호출부 호환용이며, side별 값이 없으면 기존 공통값을 사용합니다.
        self.timeout_sec = max(1, int(timeout_sec))
        self.buy_timeout_sec = max(1, int(buy_timeout_sec if buy_timeout_sec is not None else timeout_sec))
        self.sell_timeout_sec = max(1, int(sell_timeout_sec if sell_timeout_sec is not None else timeout_sec))
        self.pending: dict[str, PendingOrder] = {}
        self.uncertain_file = runtime_path("uncertain_orders")
        # 취소 요청/취소 주문번호/최종 대사 결과는 원주문 pending이 제거된 뒤에도 감사 추적을 위해 보존합니다.
        self.cancel_records_file = runtime_path("cancel_orders")
        self.cancel_records: dict[str, dict] = {}
        self.cancel_records_integrity_error: str | None = None
        self.uncertain_orders: dict[str, dict] = {}
        self.uncertain_symbols: set[str] = set()
        # 손상/접근 불가한 기존 파일은 빈 상태로 대체하지 않습니다.
        # 이 플래그가 참이면 신규 매수는 전역 차단하고 원본 파일 쓰기도 금지합니다.
        self.integrity_error: str | None = None
        self.last_balance_reconcile_ts = 0.0
        self.lock = threading.RLock()
        self.account_order_lock_file = runtime_path("account_order_lock")
        self.account_order_lock: dict = {}
        self.account_order_lock_integrity_error: str | None = None
        # WebSocket 체결통보가 REST 주문 응답/로컬 pending 등록보다 먼저 도착하는
        # 짧은 경합 구간을 흡수합니다. 보류 통보는 주문번호가 확인된 뒤 재처리합니다.
        try:
            orphan_ttl = float(os.getenv("ORPHAN_NOTICE_TTL_SEC", "180"))
        except (TypeError, ValueError):
            orphan_ttl = 180.0
        self.orphan_notice_ttl_sec = max(30.0, orphan_ttl)
        self.orphan_notices: dict[str, list[dict]] = {}
        self.deferred_fill_events: list[FillEvent] = []
        self._load_uncertain_orders()
        self._load_cancel_records()
        self._load_account_order_lock()
        # 모든 REST 경로에서 계좌/인증 오류를 감지해 계좌 잠금으로 전파합니다.
        try:
            self.api.account_error_handler = self.latch_account_order_lock_from_error
        except Exception:
            order_log.warning("KIS API account_error_handler 등록을 건너뜀; 주문 POST 오류에서 직접 판별합니다.")

    @property
    def account_order_locked(self) -> bool:
        """잠금 마커가 활성화됐거나 마커 무결성을 확인할 수 없으면 True입니다."""
        return bool(self.account_order_lock.get("locked")) or bool(self.account_order_lock_integrity_error)

    def _load_account_order_lock(self) -> None:
        """계좌 주문 잠금을 복구합니다. 손상된 파일은 잠금으로 간주하고 원본을 보존합니다."""
        try:
            raw_text = self.account_order_lock_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self.account_order_lock_integrity_error = f"{type(exc).__name__}: {exc}"
            self.account_order_lock = {"locked": True, "msg_cd": "LOCK_FILE_UNREADABLE",
                                       "reason": self.account_order_lock_integrity_error,
                                       "requires_manual_release": True}
            order_log.critical("계좌 주문 잠금 파일을 읽을 수 없어 fail-closed: %s", self.account_order_lock_file)
            return
        try:
            raw = json.loads(raw_text)
            if not isinstance(raw, dict) or not isinstance(raw.get("locked"), bool):
                raise ValueError("잠금 파일은 locked boolean 필드를 포함한 JSON 객체여야 합니다")
            self.account_order_lock = dict(raw)
            if raw["locked"]:
                order_log.critical("이전 실행의 계좌 전체 주문 잠금 복구: msg_cd=%s reason=%s file=%s",
                                   raw.get("msg_cd", "UNKNOWN"), raw.get("msg1", raw.get("reason", "")),
                                   self.account_order_lock_file)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self.account_order_lock_integrity_error = f"{type(exc).__name__}: {exc}"
            self.account_order_lock = {"locked": True, "msg_cd": "LOCK_FILE_INVALID",
                                       "reason": self.account_order_lock_integrity_error,
                                       "requires_manual_release": True}
            order_log.critical("계좌 주문 잠금 파일 손상/형식 오류; fail-closed, 원본 보존: %s",
                               self.account_order_lock_file)

    def _save_account_order_lock(self) -> None:
        """잠금 상태를 원자 저장하고 소유자 전용 권한을 적용합니다."""
        try:
            path = self.account_order_lock_file
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.account_order_lock, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            self.account_order_lock_integrity_error = f"잠금 파일 저장 실패: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 메모리상 잠금은 유지하지만 재시작 전 파일 쓰기 권한을 복구해야 합니다.",
                               self.account_order_lock_integrity_error)

    def latch_account_order_lock_from_error(self, *, msg_cd: str, msg1: str = "",
                                            method: str = "", path: str = "",
                                            side: str = "", symbol: str = "") -> bool:
        """계좌 단위 KIS 오류를 영구 잠금으로 기록합니다. 최초 1회만 새 잠금으로 처리합니다."""
        code = str(msg_cd or "").strip().upper()
        if code not in _ACCOUNT_ORDER_ERROR_CODES:
            return False
        with self.lock:
            if self.account_order_locked:
                return False
            self.account_order_lock = {
                "schema_version": 1, "locked": True, "msg_cd": code,
                "msg1": str(msg1 or "")[:500], "method": str(method or ""),
                "path": str(path or ""), "side": str(side or ""),
                "symbol": str(symbol or ""), "locked_at_epoch": time.time(),
                "requires_manual_release": True, "alert_sent": False,
            }
            self._save_account_order_lock()
        order_log.critical("계좌 전체 주문 잠금 발동: msg_cd=%s method=%s path=%s side=%s symbol=%s; BUY/SELL 신규 주문 차단, 재시작 후에도 유지",
                           code, method or "UNKNOWN", path or "UNKNOWN", side or "UNKNOWN", symbol or "UNKNOWN")
        return True

    def mark_account_order_lock_alerted(self) -> bool:
        """활성 잠금의 운영자 알림을 최초 1회만 허용합니다."""
        with self.lock:
            if not self.account_order_locked or self.account_order_lock.get("alert_sent"):
                return False
            self.account_order_lock["alert_sent"] = True
            self.account_order_lock["alert_sent_at_epoch"] = time.time()
            self._save_account_order_lock()
            return True

    def _load_cancel_records(self) -> None:
        """취소 감사 기록을 복구합니다. 손상된 파일은 빈 기록으로 덮어쓰지 않습니다."""
        try:
            raw_text = self.cancel_records_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 새 설치/최초 실행은 정상입니다. json/ 아래에 빈 감사 저장소를 만들어
            # 실제 취소 이력이 아직 없어도 canonical 경로와 쓰기 권한을 확인합니다.
            self._save_cancel_records()
            return
        except OSError as exc:
            self.cancel_records_integrity_error = f"취소 기록 파일 읽기 실패: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 원본 보존: %s", self.cancel_records_integrity_error, self.cancel_records_file)
            return
        try:
            raw = json.loads(raw_text)
            records = raw.get("records", {}) if isinstance(raw, dict) else None
            if not isinstance(records, dict) or any(not isinstance(v, dict) for v in records.values()):
                raise ValueError("records는 원주문번호별 객체여야 합니다")
            self.cancel_records = {str(k).strip(): dict(v) for k, v in records.items() if str(k).strip()}
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            self.cancel_records_integrity_error = f"취소 기록 파일 손상/형식 오류: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 원본을 수동 확인하기 전 덮어쓰지 않습니다: %s",
                               self.cancel_records_integrity_error, self.cancel_records_file)

    def _save_cancel_records(self) -> None:
        """취소 기록을 원자 저장합니다. 손상/접근 오류가 있는 원본은 덮어쓰지 않습니다."""
        if self.cancel_records_integrity_error:
            order_log.critical("취소 기록 무결성 오류로 저장 거부; 원본 보존: %s (%s)",
                               self.cancel_records_file, self.cancel_records_integrity_error)
            return
        try:
            self.cancel_records_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cancel_records_file.with_suffix(self.cancel_records_file.suffix + ".tmp")
            tmp.write_text(json.dumps({"updated_at": time.time(), "records": self.cancel_records},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.cancel_records_file)
        except OSError as exc:
            self.cancel_records_integrity_error = f"취소 기록 파일 저장 실패: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 취소 기록 추적이 불완전할 수 있습니다: %s",
                               self.cancel_records_integrity_error, self.cancel_records_file)

    @staticmethod
    def _cancel_response_output(response: dict) -> dict:
        """KIS 취소 응답에서 output/output1 구조 차이를 흡수합니다."""
        if not isinstance(response, dict):
            return {}
        output = response.get("output") or response.get("output1") or {}
        if isinstance(output, list):
            return output[0] if output and isinstance(output[0], dict) else {}
        return output if isinstance(output, dict) else {}

    def request_cancel(self, pending: PendingOrder) -> dict:
        """원주문 취소를 요청하고, 응답 주문번호와 원문 응답의 핵심 필드를 기록합니다."""
        response = self.api.cancel_order(pending.order_no, pending.branch_no)
        output = self._cancel_response_output(response)
        cancel_order_no = str(_first(output, "ODNO", "odno", "CANCEL_ODNO", "cancel_odno", default="")).strip()
        cancel_branch = str(_first(output, "KRX_FWDG_ORD_ORGNO", "ORD_GNO_BRNO", "ord_gno_brno", default="")).strip()
        now = time.time()
        with self.lock:
            previous = self.cancel_records.get(pending.order_no, {})
            record = dict(previous)
            attempts = list(record.get("cancel_attempts", [])) if isinstance(record.get("cancel_attempts", []), list) else []
            attempt = {
                "requested_ts": now,
                "cancel_order_no": cancel_order_no,
                "cancel_order_branch_no": cancel_branch,
                "api_rt_cd": str(response.get("rt_cd", "") if isinstance(response, dict) else ""),
                "api_msg_cd": str(response.get("msg_cd", "") if isinstance(response, dict) else ""),
                "response_fields": sorted(str(k) for k in output.keys()),
            }
            attempts.append(attempt)
            record.update({
                "original_order_no": pending.order_no,
                "original_branch_no": pending.branch_no,
                "symbol": pending.symbol,
                "side": pending.side,
                "requested_qty": pending.qty,
                "filled_qty_at_cancel_request": pending.filled_qty,
                "remaining_qty_at_cancel_request": pending.remaining_qty,
                "cancel_requested_ts": now,
                "cancel_order_no": cancel_order_no or str(previous.get("cancel_order_no", "")),
                "cancel_order_branch_no": cancel_branch or str(previous.get("cancel_order_branch_no", "")),
                "cancel_order_numbers": list(dict.fromkeys([str(x).strip() for x in (previous.get("cancel_order_numbers", []) or []) if str(x).strip()] + ([cancel_order_no] if cancel_order_no else []))),
                "cancel_attempts": attempts,
                "api_rt_cd": str(response.get("rt_cd", "") if isinstance(response, dict) else ""),
                "api_msg_cd": str(response.get("msg_cd", "") if isinstance(response, dict) else ""),
                "api_msg1": str(response.get("msg1", "") if isinstance(response, dict) else "")[:300],
                "response_fields": sorted(str(k) for k in output.keys()),
                "status": "RESPONSE_RECEIVED" if cancel_order_no else "RESPONSE_NO_CANCEL_ORDER_NO",
                "last_check_ts": 0.0,
            })
            self.cancel_records[pending.order_no] = record
            pending.cancel_order_no = record["cancel_order_no"]
            pending.cancel_order_branch_no = record["cancel_order_branch_no"]
            pending.cancel_requested_ts = now
            pending.cancel_attempted = True
            self._save_cancel_records()
        order_log.warning(
            "취소 API 응답 수신: original_order=%s cancel_order=%s symbol=%s side=%s requested=%d filled=%d remaining=%d rt_cd=%s msg_cd=%s fields=%s",
            pending.order_no, cancel_order_no or "미발급/응답필드없음", symbol_label(pending.symbol), pending.side,
            pending.qty, pending.filled_qty, pending.remaining_qty, record["api_rt_cd"], record["api_msg_cd"],
            record["response_fields"],
        )
        if not cancel_order_no:
            order_log.error("취소 응답에서 취소 주문번호를 찾지 못함: original_order=%s response_keys=%s; 원주문 이력으로 상태 대사",
                            pending.order_no, sorted(str(k) for k in response.keys()) if isinstance(response, dict) else type(response).__name__)
        return response

    def record_cancel_error(self, pending: PendingOrder, error: object) -> None:
        """취소 POST 예외를 기록합니다. 접수 여부 불명확이면 자동 재전송하지 않습니다."""
        with self.lock:
            record = dict(self.cancel_records.get(pending.order_no, {}))
            record.update({"original_order_no": pending.order_no, "original_branch_no": pending.branch_no,
                           "symbol": pending.symbol, "side": pending.side, "requested_qty": pending.qty,
                           "filled_qty_at_cancel_request": pending.filled_qty,
                           "remaining_qty_at_cancel_request": pending.remaining_qty,
                           "last_error": f"{type(error).__name__}: {error}"[:500],
                           "error_ts": time.time(), "status": "REQUEST_ERROR"})
            self.cancel_records[pending.order_no] = record
            self._save_cancel_records()
        order_log.error("취소 요청 예외 기록: original_order=%s cancel_order=%s error=%s",
                        pending.order_no, record.get("cancel_order_no", "") or "unknown", error)

    def pending_cancel_records(self, min_interval_sec: float = 30.0) -> list[dict]:
        """최종 대사가 안 끝난 취소 기록을 조회 간격 제한과 함께 반환합니다."""
        now = time.time()
        with self.lock:
            records = []
            for original_no, record in self.cancel_records.items():
                if str(record.get("status", "")).startswith("CONFIRMED"):
                    continue
                if now - float(record.get("last_check_ts", 0) or 0) < max(0.0, min_interval_sec):
                    continue
                item = dict(record)
                item["original_order_no"] = item.get("original_order_no") or original_no
                records.append(item)
            return records

    def update_cancel_record(self, original_order_no: str, updates: dict) -> None:
        """취소 주문 이력 조회 결과를 저장합니다."""
        with self.lock:
            record = self.cancel_records.get(str(original_order_no))
            if not record:
                return
            record.update(updates)
            record["last_check_ts"] = time.time()
            self._save_cancel_records()

    def note_balance_reconcile(self, reconciled_at: float | None = None) -> None:
        """성공한 잔고 스냅샷 시각을 기록해 지연 WebSocket 통보의 이중 반영을 막습니다."""
        stamp = time.time() if reconciled_at is None else float(reconciled_at)
        with self.lock:
            self.last_balance_reconcile_ts = max(self.last_balance_reconcile_ts, stamp)

    @staticmethod
    def _notice_epoch(notice: dict) -> float:
        """체결통보의 체결시각을 KST epoch으로 변환합니다. 식별 불가 시 0."""
        import datetime as _dt
        from zoneinfo import ZoneInfo
        raw = str(_first(notice, "CCLD_DTIME", "CNTG_DTIME", "STCK_CNTG_HOUR", default="")).strip()
        tz = ZoneInfo("Asia/Seoul")
        for fmt in ("%Y%m%d%H%M%S", "%H%M%S"):
            try:
                parsed = _dt.datetime.strptime(raw[:14] if fmt == "%Y%m%d%H%M%S" else raw[:6], fmt)
                if fmt == "%H%M%S":
                    now = _dt.datetime.now(tz)
                    parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
                return parsed.replace(tzinfo=tz).timestamp()
            except ValueError:
                continue
        return 0.0

    def _load_uncertain_orders(self) -> None:
        """불명확 주문 기록을 복구합니다. 손상된 기존 파일은 덮어쓰지 않고 fail-closed 합니다."""
        try:
            raw_text = self.uncertain_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 최초 실행에서 파일이 없는 것은 정상입니다.
            return
        except OSError as exc:
            self.integrity_error = f"불명확 주문 파일을 읽을 수 없음: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 신규 매수 차단, 파일 보존: %s", self.integrity_error, self.uncertain_file)
            return

        try:
            raw = json.loads(raw_text)
            if not isinstance(raw, dict):
                raise ValueError("최상위 JSON 값은 객체여야 합니다")
            orders = raw.get("orders", [])
            symbols = raw.get("symbols", [])
            if not isinstance(orders, list) or not isinstance(symbols, list):
                raise ValueError("orders와 symbols는 배열이어야 합니다")
            if any(not isinstance(item, dict) for item in orders):
                raise ValueError("orders 배열 항목은 객체여야 합니다")
            for item in orders:
                symbol = str(item.get("symbol", "")).strip().zfill(6)
                if not symbol or symbol == "000000":
                    raise ValueError("orders 항목에 유효하지 않은 symbol이 있습니다")
                side = str(item.get("side", "UNKNOWN")).strip().upper()
                if side not in {"BUY", "SELL", "UNKNOWN"}:
                    raise ValueError(f"{symbol} 주문 side 값이 유효하지 않습니다")
                try:
                    qty = int(item.get("qty", 0) or 0)
                    created_ts = float(item.get("created_ts", time.time()) or time.time())
                    checks = int(item.get("checks", 0) or 0)
                    reservation_price = float(item.get("reservation_price", 0.0) or 0.0)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"{symbol} 주문 수량/시각/조회횟수/예약가격 형식이 유효하지 않습니다") from exc
                if qty < 0 or checks < 0 or created_ts <= 0 or reservation_price < 0:
                    raise ValueError(f"{symbol} 주문 수량/시각/조회횟수/예약가격 범위가 유효하지 않습니다")
                self.uncertain_orders[symbol] = dict(item, symbol=symbol, side=side, qty=qty,
                                                     created_ts=created_ts, checks=checks,
                                                     reservation_price=reservation_price)
            for value in symbols:
                symbol = str(value).strip().zfill(6)
                if not symbol or symbol == "000000":
                    raise ValueError("symbols 배열에 유효하지 않은 종목코드가 있습니다")
                if symbol not in self.uncertain_orders:
                    # 구버전 파일은 side/qty를 모르므로 UNKNOWN으로 보수적으로 취급합니다.
                    self.uncertain_orders[symbol] = {"symbol": symbol, "side": "UNKNOWN", "qty": 0,
                                                     "created_ts": time.time(), "checks": 0,
                                                     "last_error": "legacy symbols-only record"}
            self.uncertain_symbols = set(self.uncertain_orders)
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            self.uncertain_orders.clear()
            self.uncertain_symbols.clear()
            self.integrity_error = f"불명확 주문 파일 손상/형식 오류: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 신규 매수 차단, 원본을 수동 복구하기 전 파일을 수정하지 않습니다: %s",
                               self.integrity_error, self.uncertain_file)

    def _save_uncertain_symbols(self) -> None:
        """불명확 주문 상태를 원자 저장합니다. 파일 무결성 오류가 있으면 원본 덮어쓰기를 금지합니다."""
        if self.integrity_error:
            order_log.critical("불명확 주문 파일 무결성 오류로 저장 거부; 원본 보존: %s (%s)",
                               self.uncertain_file, self.integrity_error)
            return
        try:
            self.uncertain_symbols = set(self.uncertain_orders)
            self.uncertain_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.uncertain_file.with_suffix(self.uncertain_file.suffix + ".tmp")
            payload = {"updated_at": time.time(), "symbols": sorted(self.uncertain_symbols),
                       "orders": [self.uncertain_orders[k] for k in sorted(self.uncertain_orders)]}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.uncertain_file)
        except OSError as exc:
            self.integrity_error = f"불명확 주문 파일 저장 실패: {type(exc).__name__}: {exc}"
            order_log.critical("%s; 신규 매수 차단: %s", self.integrity_error, self.uncertain_file)

    def _record_uncertain(self, symbol: str, side: str, qty: int, error: object, reservation_price: float = 0.0) -> None:
        """불명확 POST는 재전송하지 않고 진단 정보와 자동 조회 상태를 저장합니다."""
        symbol = str(symbol).strip().zfill(6)
        with self.lock:
            previous = self.uncertain_orders.get(symbol, {})
            self.uncertain_orders[symbol] = {
                "symbol": symbol, "side": side, "qty": qty,
                "created_ts": previous.get("created_ts", time.time()),
                "checks": 0, "last_check_ts": 0,
                "reservation_price": max(0.0, float(reservation_price or previous.get("reservation_price", 0.0) or 0.0)),
                "last_error": f"{type(error).__name__}: {error}"[:1000],
            }
            self._save_uncertain_symbols()

    def confirm_rejected_order_absent(
        self, symbol: str, side: str, qty: int, request_started_ts: float, error: object,
    ) -> bool:
        """명시 거절 후 당일 주문이력에서 동일 주문이 없음을 확인할 때만 재주문을 허용합니다.

        주문이력 조회 실패, 시간 필드 누락, 일치 후보 존재 시에는 불명확 주문으로 잠그고
        False를 반환합니다. 이 메서드는 주문 POST를 절대 재전송하지 않습니다.
        """
        import datetime as _dt
        from zoneinfo import ZoneInfo
        normalized = str(symbol).strip().zfill(6)
        normalized_side = str(side).strip().upper()
        error_code = str(getattr(error, "msg_cd", "") or "unknown")
        try:
            request_day = _dt.datetime.fromtimestamp(float(request_started_ts), ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
            rows = self.api.daily_orders(request_day, request_day, ccld_dvsn="00") or []
        except Exception as exc:
            self._record_uncertain(normalized, normalized_side, int(qty),
                                   f"명시 거절 후 주문이력 조회 실패 msg_cd={error_code}: {type(exc).__name__}: {exc}")
            order_log.critical("매수 거절 후 주문 미접수 확인 실패; 종목 잠금 및 재주문 금지 symbol=%s", symbol_label(normalized))
            return False

        def first(row, *keys):
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
            return ""

        def number(value):
            try:
                return int(float(str(value).replace(",", "")))
            except (TypeError, ValueError):
                return 0

        candidates = []
        for row in rows:
            row_symbol = str(first(row, "pdno", "PDNO", "mksc_shrn_iscd", "MKSC_SHRN_ISCD")).strip().zfill(6)
            side_code = str(first(row, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")).strip()
            side_name = str(first(row, "sll_buy_dvsn_cd_name", "SLL_BUY_DVSN_CD_NAME")).strip()
            row_side = "SELL" if side_code == "01" or "매도" in side_name else "BUY" if side_code == "02" or "매수" in side_name else "UNKNOWN"
            row_qty = number(first(row, "ord_qty", "ORD_QTY"))
            if row_symbol != normalized or row_side != normalized_side or row_qty != int(qty):
                continue
            raw_date = str(first(row, "ord_dt", "ORD_DT", "ord_date", "ORD_DATE")).strip()
            raw_time = str(first(row, "ord_dtl_dtime", "ORD_DTL_DTIME", "ord_tmd", "ORD_TMD")).strip()
            epoch = 0.0
            try:
                if len(raw_time) >= 14 and raw_time[:14].isdigit():
                    parsed = _dt.datetime.strptime(raw_time[:14], "%Y%m%d%H%M%S")
                elif len(raw_date) == 8 and raw_date.isdigit() and len(raw_time) >= 6 and raw_time[:6].isdigit():
                    parsed = _dt.datetime.strptime(raw_date + raw_time[:6], "%Y%m%d%H%M%S")
                else:
                    parsed = None
                if parsed is not None:
                    epoch = parsed.replace(tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
            except ValueError:
                epoch = 0.0
            # 시각이 없으면 과거 주문인지 배제할 수 없으므로 후보로 간주해 재주문을 막습니다.
            # 서버/클라이언트 시각 오차와 주문 접수 직전 시각 기록 차이를 고려해 120초 여유를 둡니다.
            if epoch <= 0 or epoch >= float(request_started_ts) - 120.0:
                candidates.append(row)

        if candidates:
            self._record_uncertain(
                normalized, normalized_side, int(qty),
                f"매수 거절 응답(msg_cd={error_code}) 후 동일 종목/방향/수량 주문이력 후보 {len(candidates)}건; 운영자/REST 대사 필요",
            )
            order_log.critical(
                "매수 명시 거절 후 주문이력 후보 존재; 접수 여부 단일 확정 전 재주문 금지 symbol=%s candidates=%d",
                symbol_label(normalized), len(candidates),
            )
            return False

        order_log.info(
            "매수 명시 거절 후 주문이력 미접수 확인 완료: symbol=%s side=%s qty=%d msg_cd=%s; 예수금 fallback 재계산 허용",
            symbol_label(normalized), normalized_side, int(qty), error_code,
        )
        return True

    def reconcile_uncertain_orders(self, rows: list[dict], filled_rows: list[dict] | None = None,
                                   min_age_sec: int = 180, required_checks: int = 3) -> dict[str, int]:
        """당일 전체 주문과 체결내역으로 불명확 요청을 대사합니다.

        주문번호가 있는 일치 주문을 찾으면 해당 주문을 로컬 pending으로 동기화하고
        불명확 잠금을 해제합니다. 체결내역도 주문번호 기준으로 확인해 로그에 남깁니다.
        일치 주문이 없더라도 자동 잠금 해제하지 않습니다. 반복 조회 기준은 진단/알림에만
        사용하며, API 조회 실패 시 호출하지 않아 조회 횟수가 증가하지 않습니다.
        """
        now = time.time()
        rows = rows or []
        filled_rows = filled_rows or []
        filled_order_numbers = {str(_first(r, "odno", "ODNO", "order_no", default="")).strip()
                                for r in filled_rows}
        def row_symbol(row):
            return str(_first(row, "pdno", "PDNO", "mksc_shrn_iscd", "MKSC_SHRN_ISCD", default="")).strip().zfill(6)
        def row_side(row):
            code = str(_first(row, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD", default="")).strip()
            text = str(_first(row, "sll_buy_dvsn_cd_name", "SLL_BUY_DVSN_CD_NAME", default="")).upper()
            return "SELL" if code == "01" or "매도" in text or text == "SELL" else "BUY" if code == "02" or "매수" in text or text == "BUY" else "UNKNOWN"
        def row_qty(row):
            return _int(_first(row, "ord_qty", "ORD_QTY", default=0))
        def row_epoch(row):
            # 당일 주문시간이 있는 응답만 요청시각과 대조합니다.
            import datetime as _dt
            raw_date = str(_first(row, "ord_dt", "ORD_DT", "ord_date", "ORD_DATE", default="")).strip()
            raw_time = str(_first(row, "ord_tmd", "ORD_TMD", "ord_dtl_dtime", "ORD_DTL_DTIME", default="")).strip()
            if len(raw_time) >= 14 and raw_time[:14].isdigit():
                try:
                    return _dt.datetime.strptime(raw_time[:14], "%Y%m%d%H%M%S").replace(tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Seoul")).timestamp()
                except ValueError:
                    pass
            if len(raw_time) >= 6 and raw_time[:6].isdigit() and len(raw_date) == 8 and raw_date.isdigit():
                try:
                    return _dt.datetime.strptime(raw_date + raw_time[:6], "%Y%m%d%H%M%S").replace(tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Seoul")).timestamp()
                except ValueError:
                    pass
            return 0.0
        resolved = 0
        waiting = 0
        with self.lock:
            for symbol, record in list(self.uncertain_orders.items()):
                age = now - float(record.get("created_ts", now) or now)
                side, qty = record.get("side", "UNKNOWN"), int(record.get("qty", 0) or 0)
                possible = [r for r in rows if row_symbol(r) == symbol
                            and (side == "UNKNOWN" or row_side(r) == side)
                            and (qty <= 0 or row_qty(r) == qty)]
                # timestamp가 있으면 요청 발생 직전의 과거 주문을 잘못 매칭하지 않습니다.
                # 응답에 주문시각이 없거나 같은 조건의 주문이 여러 건이면 안전하게 자동 매칭하지 않습니다.
                # 오래된 동일 종목/수량 매도를 잘못 매칭하면 실제 미확정 매도 재주문으로 이어질 수 있습니다.
                recent = [r for r in possible if row_epoch(r) > 0
                          and row_epoch(r) >= float(record.get("created_ts", now) or now) - 120]
                match = recent[0] if len(recent) == 1 else None
                if possible and not match:
                    order_log.warning("불명확 주문 대사 후보가 없거나 시간/방향/수량으로 단일 확정 불가: symbol=%s side=%s qty=%s candidates=%d; 잠금 유지",
                                      symbol_label(symbol), side, qty, len(possible))
                if match:
                    order_no = str(_first(match, "odno", "ODNO", "order_no", default="")).strip()
                    # 주문번호가 없으면 안전하게 매칭 완료로 처리하지 않습니다.
                    if not order_no:
                        record["checks"] = int(record.get("checks", 0) or 0) + 1
                        record["last_check_ts"] = now
                        waiting += 1
                        order_log.warning("불명확 주문 후보는 있으나 주문번호가 없어 확인 지속: symbol=%s", symbol_label(symbol))
                        continue
                    branch = str(_first(match, "ord_gno_brno", "ORD_GNO_BRNO", "krx_fwdg_ord_orgno", "KRX_FWDG_ORD_ORGNO", default="")).strip()
                    matched_qty = _int(_first(match, "ord_qty", "ORD_QTY", default=qty)) or qty
                    matched_filled = _int(_first(match, "tot_ccld_qty", "TOT_CCLD_QTY", "tot_ccld_qty", default=0))
                    # 먼저 로컬 pending으로 예약해, 다음 잔고/미체결 대사가 끝나기 전
                    # 같은 종목/보유 슬롯에 중복 주문이 들어가지 않게 합니다.
                    if order_no and order_no not in self.pending and side in {"BUY", "SELL"} and matched_qty > 0:
                        self.pending[order_no] = PendingOrder(
                            order_no=order_no, branch_no=branch, symbol=symbol, side=side,
                            qty=matched_qty, filled_qty=min(matched_filled, matched_qty),
                            rest_filled_qty=min(matched_filled, matched_qty), rest_sync_ts=now,
                            rest_unmatched_qty=min(matched_filled, matched_qty),
                            created_ts=now, strategy="unified", reservation_price=_float(_first(match, "ord_unpr", "ORD_UNPR", "ord_prc", "ORD_PRC", default=0)))
                    fill_status = "체결내역 확인" if order_no in filled_order_numbers else "주문내역 확인(미체결/부분체결 포함)"
                    order_log.warning("불명확 주문 자동 대사: %s symbol=%s side=%s qty=%s order=%s",
                                      fill_status, symbol_label(symbol), side, qty, order_no)
                    self.uncertain_orders.pop(symbol, None)
                    resolved += 1
                    continue
                record["checks"] = int(record.get("checks", 0) or 0) + 1
                record["last_check_ts"] = now
                record["last_no_match_age_sec"] = int(age)
                if age >= min_age_sec and record["checks"] >= required_checks:
                    # 조회 결과에 없다는 이유만으로 잠금을 해제하지 않습니다. KIS 조회 지연,
                    # 페이지 누락 등의 가능성이 있으므로 운영자 확인까지 계속 보존합니다.
                    record["manual_review_required"] = True
                    order_log.error("불명확 주문 장기 미해결: %s; %d회 성공 조회/%.0f초 경과, 자동 해제하지 않음 (오류=%s)",
                                    symbol_label(symbol), record["checks"], age, record.get("last_error", "unknown"))
                else:
                    order_log.info("불명확 주문 상태 확인 대기: symbol=%s side=%s qty=%s checks=%d age=%.0fs error=%s",
                                   symbol_label(symbol), side, qty, record["checks"], age, record.get("last_error", "unknown"))
                waiting += 1
            self._save_uncertain_symbols()
        return {"resolved": resolved, "waiting": waiting}

    def archive_and_clear_uncertain_orders(self, reason: str = "end_of_day_confirmed") -> str | None:
        """종료 시 KIS 미체결 0건 및 최종 조회 성공 후 감사용 백업을 남기고 잠금 파일을 초기화합니다."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with self.lock:
            self.uncertain_file.parent.mkdir(parents=True, exist_ok=True)
            archive_path = None
            if self.uncertain_orders:
                # 테스트 실행 시 KIS_LOG_DIR이 임시 경로를 가리키므로 운영 logs/를 오염시키지 않습니다.
                archive_dir = runtime_path("log_dir")
                archive_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d_%H%M%S")
                archive_path = archive_dir / f"uncertain_orders_archive_{stamp}.json"
                payload = {"reason": reason, "archived_at": time.time(),
                           "orders": [self.uncertain_orders[k] for k in sorted(self.uncertain_orders)]}
                archive_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self.uncertain_orders.clear()
            self.uncertain_symbols.clear()
            self._save_uncertain_symbols()
            order_log.warning("종료 처리 후 uncertain_orders.json 초기화 완료; archive=%s", archive_path or "no uncertain records")
            return str(archive_path) if archive_path else None

    def pending_buy_symbols(self) -> set[str]:
        """보유 한도 계산을 위한 미완료 매수 종목 예약 목록입니다."""
        with self.lock:
            result = {o.symbol for o in self.pending.values() if o.side == "BUY"}
            result.update(symbol for symbol, record in self.uncertain_orders.items()
                          if record.get("side") in {"BUY", "UNKNOWN"})
            return result

    def pending_buy_reservation(self) -> tuple[float, int]:
        """미체결 매수 잔량의 예상 예약액과 가격을 모르는 주문 수를 반환합니다.

        예약액은 잔량 x 주문 제출 시점의 기준가격으로 추정합니다. 주문가격을
        복구하지 못한 재시작/불명확 주문은 금액을 임의로 0원 처리하지 않고
        unknown_count로 보고하여 신규 매수 차단 판단에 사용합니다.
        """
        total = 0.0
        unknown_count = 0
        with self.lock:
            for order in self.pending.values():
                if order.side != "BUY" or order.remaining_qty <= 0:
                    continue
                if order.reservation_price <= 0:
                    unknown_count += 1
                else:
                    total += order.remaining_qty * order.reservation_price
            for record in self.uncertain_orders.values():
                if record.get("side") not in {"BUY", "UNKNOWN"}:
                    continue
                qty = max(0, _int(record.get("qty", 0)))
                filled = max(0, _int(record.get("filled_qty", 0)))
                remaining = max(0, qty - filled)
                price = _float(record.get("reservation_price", 0))
                if remaining <= 0:
                    continue
                if price <= 0:
                    unknown_count += 1
                else:
                    total += remaining * price
        return total, unknown_count

    def has_pending_buy_orders(self) -> bool:
        """미완료/불명확 매수 주문 또는 복구 불가한 주문 파일이 있는지 확인합니다."""
        return bool(self.integrity_error or self.pending_buy_symbols())

    def submit(self, symbol: str, side: str, qty: int, strategy: str = "unified", market: bool = True, price: int = 0, order_mode: str | None = None, reservation_price: float = 0.0, position_qty_before_submit: int = 0, stop_event=None) -> PendingOrder:
        """주문을 제출합니다. 종료 신호가 설정되었으면 REST POST 전에 차단합니다.

        결과가 불명확하면 재전송하지 않고 종목을 안전 차단합니다.
        """
        symbol = str(symbol).strip().zfill(6)
        side = str(side).upper().strip()
        qty = int(qty)
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if qty <= 0:
            raise ValueError("qty must be positive")
        if self.account_order_locked:
            raise AccountOrderLockedError(self.account_order_lock)
        if stop_event is not None and stop_event.is_set():
            order_log.warning("종료 신호로 신규 주문 POST 차단: symbol=%s side=%s qty=%d", symbol_label(symbol), side, qty)
            raise RuntimeError("종료 신호 수신으로 신규 주문 제출 차단")

        mode = (order_mode or ("MARKET" if market else "LIMIT")).strip().upper()
        if mode not in {"MARKET", "BEST_LIMIT", "LIMIT"}:
            raise ValueError(f"지원하지 않는 주문 방식: {mode}")
        try:
            if mode == "BEST_LIMIT":
                data = self.api.order_cash(symbol, side, qty, price=0, market=False, best_limit=True)
            elif mode == "MARKET":
                data = self.api.order_cash(symbol, side, qty, price=0, market=True)
            else:
                data = self.api.order_cash(symbol, side, qty, price=price, market=False)
        except AmbiguousOrderError as exc:
            self._record_uncertain(symbol, side, qty, exc, reservation_price=reservation_price)
            order_log.critical("주문 결과 불명확: symbol=%s side=%s qty=%d; 해당 종목만 대사 대상으로 기록, POST 재전송 금지", symbol_label(symbol), side, qty)
            raise
        except KisApiError as exc:
            # 계좌/인증 오류는 종목 단위가 아니므로 모든 BUY·SELL 신규 주문을 잠급니다.
            code = str(getattr(exc, "msg_cd", "") or "").strip().upper()
            if code in _ACCOUNT_ORDER_ERROR_CODES:
                self.latch_account_order_lock_from_error(
                    msg_cd=code, msg1=getattr(exc, "msg1", str(exc)),
                    method="POST", path="/uapi/domestic-stock/v1/trading/order-cash",
                    side=side, symbol=symbol,
                )
                raise AccountOrderLockedError(self.account_order_lock) from exc
            # 명시적인 일반 종목 업무오류는 기존처럼 상위 호출부로 전달합니다.
            order_log.error("KIS 주문 API 오류(명시 응답): symbol=%s side=%s qty=%d error=%s", symbol_label(symbol), side, qty, exc)
            raise
        except Exception as exc:
            # 예상하지 못한 POST 예외는 접수됐을 가능성을 배제할 수 없어 기록 후 재전송하지 않습니다.
            self._record_uncertain(symbol, side, qty, exc, reservation_price=reservation_price)
            order_log.exception("주문 요청 예외로 접수 여부 불명확: symbol=%s side=%s qty=%d; 자동 재전송 금지", symbol_label(symbol), side, qty)
            raise AmbiguousOrderError(f"주문 결과 불명확: {symbol_label(symbol)} {side} {qty}; 오류={type(exc).__name__}: {exc}") from exc
        out = data.get("output", {}) or {}
        order_no = str(_first(out, "ODNO", "odno", default="")).strip()
        branch = str(_first(out, "KRX_FWDG_ORD_ORGNO", "ORD_GNO_BRNO", "ord_gno_brno", default="")).strip()
        if not order_no:
            self._record_uncertain(symbol, side, qty, "주문 응답에 주문번호 없음", reservation_price=reservation_price)
            order_log.critical("주문 응답에 주문번호가 없음: symbol=%s side=%s qty=%d; 대사 대상으로 기록", symbol_label(symbol), side, qty)
            raise AmbiguousOrderError(f"주문번호가 없는 응답으로 접수 여부 불명확: {symbol_label(symbol)} {side} {qty}; KIS 주문내역 확인 필요")

        order = PendingOrder(
            order_no=order_no,
            branch_no=branch,
            symbol=symbol,
            side=side,
            qty=qty,
            filled_qty=0,
            created_ts=time.time(),
            cancel_attempted=False,
            last_notice_key="",
            seen_notice_keys=[],
            strategy="unified",
            reservation_price=max(0.0, float(reservation_price or 0.0)),
            position_qty_before_submit=max(0, int(position_qty_before_submit or 0)),
        )
        with self.lock:
            self.pending[order_no] = order
            # 주문번호가 로컬 pending에 등록되기 전에 도착한 체결통보를 같은 락 안에서
            # 재생해, 새 통보가 먼저 처리되는 순서 역전을 방지합니다.
            buffered_notices = self.orphan_notices.pop(order_no, [])
            cutoff = time.time() - self.orphan_notice_ttl_sec
            early_notices = [entry for entry in buffered_notices
                             if float(entry.get("received_ts", 0) or 0) >= cutoff]
            expired_count = len(buffered_notices) - len(early_notices)
            if expired_count:
                order_log.error("선행 체결통보 TTL 만료로 재처리 생략: order=%s expired=%d ttl=%.1f초; REST 대사 필요",
                                order_no, expired_count, self.orphan_notice_ttl_sec)
            for entry in early_notices:
                event = self.on_notice(entry["notice"])
                if event is not None:
                    self.deferred_fill_events.append(event)
            if early_notices:
                order_log.warning("주문 접수 후 선행 체결통보 재처리: order=%s buffered=%d deferred_fills=%d",
                                  order_no, len(early_notices), len(self.deferred_fill_events))
        return order

    def drain_deferred_fill_events(self) -> list[FillEvent]:
        """submit()에서 복구한 선행 체결 이벤트를 main thread가 한 번씩 처리하도록 꺼냅니다."""
        with self.lock:
            events = list(self.deferred_fill_events)
            self.deferred_fill_events.clear()
            return events

    def _buffer_orphan_notice_locked(self, order_no: str, notice: dict) -> None:
        """pending 등록 전/재시작 직후의 체결통보를 제한된 시간 동안 보류합니다."""
        now = time.time()
        cutoff = now - self.orphan_notice_ttl_sec
        for known_order_no in list(self.orphan_notices):
            retained = [item for item in self.orphan_notices[known_order_no]
                        if float(item.get("received_ts", 0) or 0) >= cutoff]
            if retained:
                self.orphan_notices[known_order_no] = retained
            else:
                self.orphan_notices.pop(known_order_no, None)
        # 무제한 메모리 증가 방지. 오래된/매칭되지 않는 알림은 경고 로그 후 폐기합니다.
        if order_no not in self.orphan_notices and len(self.orphan_notices) >= 500:
            oldest = min(self.orphan_notices, key=lambda key: min(
                float(item.get("received_ts", now) or now) for item in self.orphan_notices[key]))
            self.orphan_notices.pop(oldest, None)
            order_log.error("선행 체결통보 버퍼 상한 도달: 가장 오래된 미매칭 주문번호 폐기 order=%s", oldest)
        entries = self.orphan_notices.setdefault(order_no, [])
        signature = "|".join(str(_first(notice, key, default="")) for key in (
            "CNTG_NO", "CCLD_NO", "CCLD_SEQ", "STCK_CNTG_HOUR", "CCLD_DTIME",
            "TOT_CCLD_QTY", "CNTG_QTY", "CNTG_UNPR"))
        if any(item.get("signature") == signature for item in entries):
            return
        if len(entries) >= 100:
            entries.pop(0)
        entries.append({"notice": dict(notice), "received_ts": now, "signature": signature})
        order_log.warning("pending 등록 전 체결통보 보류: order=%s buffered=%d", order_no, len(entries))

    def on_notice(self, notice: dict) -> FillEvent | None:
        """KIS 체결통보에서 실제 체결(CNTG_YN=2)만 처리합니다.

        부분체결은 pending을 유지하고, 누적 체결수량이 주문수량에 도달하면
        해당 주문을 pending에서 제거합니다.
        """
        if str(_first(notice, "CNTG_YN", default="")).strip() != "2":
            return None

        order_no = str(_first(notice, "ODER_NO", "ODNO", "odno", default="")).strip()
        if not order_no:
            return None

        qty = _int(_first(notice, "CNTG_QTY", "CCLD_QTY", "ccld_qty", default=0))
        cumulative_qty = _int(_first(notice, "TOT_CCLD_QTY", "tot_ccld_qty", "ACML_CCLD_QTY", "acml_ccld_qty", default=0))
        price = _float(_first(notice, "CNTG_UNPR", "CCLD_UNPR", "ccld_unpr", default=0))
        if qty <= 0 or price <= 0:
            order_log.warning("FILL_NOTICE_INVALID order=%s qty=%d price=%.2f fields=%s",
                              order_no, qty, price, sorted(str(k) for k in notice.keys()))
            return None
        order_log.info("FILL_NOTICE_RECEIVED order=%s cntg_no=%s trade_time=%s qty=%d price=%.2f "
                       "notice_cumulative_qty=%d notice_amount=%.0f notice_amount_fields=%s",
                       order_no, _first(notice, "CNTG_NO", "CCLD_NO", "CCLD_SEQ", default=""),
                       _first(notice, "STCK_CNTG_HOUR", "CCLD_DTIME", default=""), qty, price,
                       cumulative_qty, qty * price,
                       {key: notice.get(key) for key in ("CNTG_AMT", "CCLD_AMT", "TOT_CCLD_AMT", "ACML_CCLD_AMT") if key in notice})

        with self.lock:
            order = self.pending.get(order_no)
            if order is None:
                # REST 응답과 로컬 pending 등록 사이에 먼저 도착한 체결통보는 버퍼링합니다.
                # 주문번호가 끝내 확인되지 않는 알림은 TTL 만료 후 폐기하며 포지션을 임의 생성하지 않습니다.
                self._buffer_orphan_notice_locked(order_no, notice)
                return None

            # KIS 체결통보는 통상 CNTG_QTY(개별 체결량)만 제공하고,
            # REST 미체결 조회의 TOT_CCLD_QTY는 주문 누적량입니다. REST가 먼저
            # 누적 체결을 반영한 뒤 지연 통보가 도착할 수 있으므로 두 수치를 혼합하지 않습니다.
            if cumulative_qty > 0:
                known_cumulative = max(order.filled_qty, order.rest_filled_qty)
                if cumulative_qty <= known_cumulative:
                    order_log.info("FILL_NOTICE_DUPLICATE_SUPPRESSED order=%s symbol=%s notice_cumulative=%d known_cumulative=%d rest_cumulative=%d notice_cumulative_known=%d",
                                   order_no, symbol_label(order.symbol), cumulative_qty, known_cumulative,
                                   order.rest_filled_qty, order.notice_filled_qty)
                    return None
                qty = min(cumulative_qty - known_cumulative, order.remaining_qty)
            notice_key = "|".join([
                order_no,
                str(_first(notice, "CNTG_NO", "CCLD_NO", "CCLD_SEQ", default="")),
                str(_first(notice, "STCK_CNTG_HOUR", "CCLD_DTIME", default="")),
                str(cumulative_qty or qty),
                str(qty),
                str(price),
                str(_first(notice, "OODER_NO", "ORGN_ODNO", default="")),
            ])
            if order.seen_notice_keys is None:
                order.seen_notice_keys = []
            if notice_key in order.seen_notice_keys:
                return None
            # 같은 주문의 잔량보다 큰 체결 이벤트는 중복/오염 통보로 보고 반영하지 않습니다.
            if qty <= 0 or qty > order.remaining_qty:
                order_log.error("체결통보 수량 초과/오류로 무시: order=%s qty=%d remaining=%d", order_no, qty, order.remaining_qty)
                return None

            order.last_notice_key = notice_key
            order.seen_notice_keys.append(notice_key)
            order.seen_notice_keys = order.seen_notice_keys[-200:]
            if cumulative_qty > 0:
                order.notice_filled_qty = max(order.notice_filled_qty, cumulative_qty)
                order.rest_unmatched_qty = max(0, order.rest_filled_qty - order.notice_filled_qty)
            else:
                # REST 누적량 중 아직 개별 웹소켓 통보로 확인되지 않은 수량은
                # 지연 통보가 뒤늦게 도착할 때 중복 적용하지 않습니다. 억제된 체결은
                # 다음 잔고 대사에서 실제 포지션 수량으로 보정됩니다.
                order.notice_filled_qty += qty
                if order.rest_unmatched_qty > 0:
                    suppressed = min(qty, order.rest_unmatched_qty)
                    order.rest_unmatched_qty -= suppressed
                    qty -= suppressed
                    if qty <= 0:
                        order.filled_qty = max(order.filled_qty, order.rest_filled_qty)
                        if order.remaining_qty <= 0:
                            self.pending.pop(order_no, None)
                        order_log.info("REST 대사 후 지연 체결통보 중복 반영 방지: order=%s suppressed=%d rest_filled=%d",
                                       order_no, suppressed, order.rest_filled_qty)
                        return None
            first_fill = order.filled_qty == 0 and order.rest_filled_qty == 0
            notice_epoch = self._notice_epoch(notice)
            last_balance_ts = float(getattr(self, "last_balance_reconcile_ts", 0.0) or 0.0)
            # 잔고 스냅샷 이후에 도착한 지연 체결통보가 스냅샷에 이미 반영된 체결이면
            # 포지션을 다시 더하지 않습니다. 체결시각이 없으면 안전 우선으로 억제하고
            # 다음 REST 잔고 대사에서 수량을 확정합니다.
            balance_may_already_include_fill = (
                last_balance_ts >= order.created_ts > 0 and
                (notice_epoch <= 0 or notice_epoch <= last_balance_ts)
            )
            order.filled_qty += qty
            order.filled_qty = min(order.qty, max(order.filled_qty, order.rest_filled_qty))
            if balance_may_already_include_fill:
                order_log.info("잔고 대사 후 지연 체결통보 포지션 중복 반영 방지: order=%s symbol=%s qty=%d trade_ts=%s balance_ts=%.3f; REST 잔고를 기준으로 보정",
                               order_no, symbol_label(order.symbol), qty, f"{notice_epoch:.3f}" if notice_epoch else "unknown", last_balance_ts)
                # 이벤트를 반환해 체결 로그/Telegram 알림은 보내되, main loop는 event_ts와
                # 잔고 대사 시각을 비교해 로컬 포지션에 중복 반영하지 않습니다.
                # 체결 시각이 없는 경우에도 main loop가 이 이벤트를 스냅샷 이전으로 판정하게 합니다.
            event = FillEvent(
                order_no=order.order_no,
                symbol=order.symbol,
                side=order.side,
                qty=qty,
                price=price,
                event_ts=(notice_epoch or (last_balance_ts if balance_may_already_include_fill else time.time())),
                first_fill=first_fill,
                strategy=order.strategy,
                cumulative_filled_qty=order.filled_qty,
                remaining_qty=order.remaining_qty,
                position_qty_before_submit=order.position_qty_before_submit,
            )
            order_log.info(
                "체결통보 대사 order=%s symbol=%s side=%s delta=%d cumulative=%d/%d remaining=%d rest_cumulative=%d notice_cumulative=%d",
                order.order_no, symbol_label(order.symbol), order.side, qty, order.filled_qty, order.qty,
                order.remaining_qty, order.rest_filled_qty, order.notice_filled_qty,
            )
            # WebSocket만으로 pending을 제거하지 않습니다. REST 미체결 대사와 잔고 동기화가
            # 성공한 후 terminal order가 정리되어야 포지션/주문 상태가 일치합니다.
            return event

    def timed_out(self, now: float | None = None) -> list[PendingOrder]:
        """timeout을 넘었고 아직 취소 요청하지 않은 주문을 반환합니다."""
        now = time.time() if now is None else now
        with self.lock:
            return [
                o for o in self.pending.values()
                if o.remaining_qty > 0
                and not o.cancel_attempted
                and now - o.created_ts >= (self.buy_timeout_sec if o.side == "BUY" else self.sell_timeout_sec)
            ]

    def mark_cancel_attempted(self, order_no: str) -> None:
        """취소 요청을 보낸 주문을 CANCEL_REQUESTED 상태로 표시합니다."""
        with self.lock:
            order = self.pending.get(str(order_no))
            if order:
                order.cancel_attempted = True

    def pending_for_symbol(self, symbol: str) -> bool:
        """미완료 주문 또는 결과 불명확 주문이 있는 종목인지 확인합니다."""
        symbol = str(symbol).strip().zfill(6)
        with self.lock:
            return symbol in self.uncertain_symbols or any(o.symbol == symbol for o in self.pending.values())

    def has_pending(self) -> bool:
        """호환성용: 미완료/불명확 주문이 있는지 반환합니다. 신규 진입 전체 차단에 쓰지 마세요."""
        with self.lock:
            return bool(self.pending or self.uncertain_symbols)

    def snapshot(self) -> list[PendingOrder]:
        """현재 pending을 안전하게 복사합니다."""
        with self.lock:
            return [PendingOrder(**vars(o)) for o in self.pending.values()]

    def sync_open_orders(self, rows: list[dict], preserve_missing_order_nos: set[str] | None = None) -> dict[str, int]:
        """KIS '미체결 주문' 조회 결과와 로컬 pending을 동기화합니다.

        중요한 주문 상태 보호 로직입니다.
        - broker 미체결 목록에 존재: pending 유지/수량 보정
        - broker 미체결 목록에서 사라짐: 로컬 pending 제거
        - 로컬에 없지만 broker에 미체결 주문이 존재: 재시작/외부주문 복구를 위해 등록

        이 함수는 API 호출 자체를 하지 않습니다. API 호출은 main/reconcile에서
        성공적으로 완료한 뒤 이 함수에 결과를 전달해야 합니다.
        """
        preserve_missing_order_nos = {str(x).strip() for x in (preserve_missing_order_nos or set())}
        remote: dict[str, dict] = {}
        for row in rows or []:
            order_no = str(_first(row, "odno", "ODNO", "order_no", default="")).strip()
            if not order_no:
                continue
            remote[order_no] = row

        added = 0
        updated = 0
        removed = 0
        now = time.time()

        with self.lock:
            # 1) 로컬 pending을 broker 미체결 상태에 맞춥니다.
            for order_no in list(self.pending.keys()):
                order = self.pending[order_no]
                row = remote.get(order_no)
                if row is None:
                    # broker의 미체결 조회가 성공했고 이 주문이 목록에서 사라졌다면
                    # 취소 완료뿐 아니라 전량 체결/거부 등 최종 상태일 수 있습니다.
                    # 다만 새 주문은 조회 결과에 잠시 늦게 나타날 수 있으므로,
                    # 주문 생성 직후에는 유예시간을 두고 그 이후에는 상태를 정리합니다.
                    # 취소 요청을 했거나, 최초 조회 유예시간이 지난 주문만 제거합니다.
                    grace_sec = 20.0
                    if order_no in preserve_missing_order_nos:
                        order_log.warning("주문 최종상태 미확인으로 pending 유지: order=%s symbol=%s side=%s qty=%d filled=%d remaining=%d",
                                          order.order_no, symbol_label(order.symbol), order.side, order.qty, order.filled_qty, order.remaining_qty)
                        continue
                    if order.cancel_attempted or (now - order.created_ts >= grace_sec):
                        order_log.info("미체결 목록에서 주문 종료 감지: order=%s symbol=%s side=%s requested=%d filled=%d remaining=%d cancel_attempted=%s",
                                       order.order_no, symbol_label(order.symbol), order.side, order.qty, order.filled_qty,
                                       order.remaining_qty, order.cancel_attempted)
                        self.pending.pop(order_no, None)
                        removed += 1
                    continue

                before_filled, before_remaining = order.filled_qty, order.remaining_qty
                before_filled, before_remaining = order.filled_qty, order.remaining_qty
                self._update_from_remote(order, row)
                updated += 1
                order_log.info(
                    "미체결 주문 대사 order=%s symbol=%s side=%s requested=%d filled=%d remaining=%d prior_filled=%d prior_remaining=%d rest_filled=%d notice_filled=%d",
                    order.order_no, symbol_label(order.symbol), order.side, order.qty, order.filled_qty,
                    order.remaining_qty, before_filled, before_remaining,
                    order.rest_filled_qty, order.notice_filled_qty,
                )
                order_log.info(
                    "미체결 주문 대사 order=%s symbol=%s side=%s requested=%d filled=%d remaining=%d prior_filled=%d prior_remaining=%d rest_filled=%d notice_filled=%d",
                    order.order_no, symbol_label(order.symbol), order.side, order.qty, order.filled_qty,
                    order.remaining_qty, before_filled, before_remaining,
                    order.rest_filled_qty, order.notice_filled_qty,
                )

            # 2) 재시작/외부 주문 등 로컬에 없던 미체결 주문도 등록합니다.
            for order_no, row in remote.items():
                if order_no in self.pending:
                    continue
                symbol = str(_first(row, "pdno", "PDNO", "mksc_shrn_iscd", default="")).strip().zfill(6)
                side_code = str(_first(row, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD", default="")).strip()
                side = "SELL" if side_code == "01" else "BUY" if side_code == "02" else ""
                qty = _int(_first(row, "ord_qty", "ORD_QTY", default=0))
                filled = _int(_first(row, "tot_ccld_qty", "TOT_CCLD_QTY", "tot_ccld_qty", default=0))
                if not symbol or not side or qty <= 0:
                    continue
                branch = str(_first(row, "ord_gno_brno", "ORD_GNO_BRNO", "krx_fwdg_ord_orgno", "KRX_FWDG_ORD_ORGNO", default="")).strip()
                created = self._parse_created_ts(row) or now
                self.pending[order_no] = PendingOrder(
                    order_no=order_no,
                    branch_no=branch,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    filled_qty=min(filled, qty),
                    rest_filled_qty=min(filled, qty),
                    rest_sync_ts=now,
                    rest_unmatched_qty=min(filled, qty),
                    created_ts=created,
                    cancel_attempted=False,
                    strategy="unified",
                    reservation_price=_float(_first(row, "ord_unpr", "ORD_UNPR", "ord_prc", "ORD_PRC", default=0)),
                )
                added += 1

        return {"added": added, "updated": updated, "removed": removed, "remote": len(remote)}


    def sync_order_history(self, rows: list[dict]) -> int:
        """당일 주문조회 응답으로 로컬 pending의 누적체결을 대사하고 주문번호별로 기록합니다.

        완료 주문은 다음 open-order 동기화에서 제거될 수 있으므로, 이 메서드는
        수량 갱신/감사 로그만 담당하고 주문 상태를 임의로 삭제하지 않습니다.
        """
        updated = 0
        with self.lock:
            for row in rows or []:
                order_no = str(_first(row, "odno", "ODNO", "order_no", default="")).strip()
                order = self.pending.get(order_no)
                if not order_no or order is None:
                    continue
                self._update_from_remote(order, row)
                reported_remaining = _first(row, "rmn_qty", "RMN_QTY", "nccs_qty", "NCCS_QTY", default=None)
                if reported_remaining not in (None, ""):
                    final_remaining = max(0, _int(reported_remaining))
                else:
                    final_remaining = order.remaining_qty
                status = str(_first(row, "cncl_yn", "CNCL_YN", "ccld_dvsn_name", "CCLD_DVSN_NAME", default="")).strip()
                order_log.info(
                    "당일 주문이력 대사 order=%s symbol=%s side=%s status=%s requested=%d rest_cumulative=%d rest_cumulative_amount=%.0f notice_cumulative=%d effective_filled=%d calculated_remaining=%d broker_remaining=%d",
                    order.order_no, symbol_label(order.symbol), order.side, status or "unknown", order.qty,
                    order.rest_filled_qty, order.rest_filled_amount, order.notice_filled_qty, order.filled_qty,
                    order.remaining_qty, final_remaining,
                )
                updated += 1
        return updated


    @staticmethod
    def _parse_created_ts(row: dict) -> float:
        """주문일시 필드가 있으면 epoch으로 변환합니다. 실패하면 0을 반환합니다."""
        raw = str(_first(row, "ord_dtl_dtime", "ORD_DTL_DTIME", "ord_tmd", "ORD_TMD", default="")).strip()
        if not raw:
            return 0.0
        # KIS는 YYYYMMDDHHMMSS 또는 HHMMSS 계열을 사용할 수 있습니다.
        import datetime as _dt
        for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M%S.%f"):
            try:
                return _dt.datetime.strptime(raw, fmt).timestamp()
            except ValueError:
                pass
        return 0.0

    @staticmethod
    def _update_from_remote(order: PendingOrder, row: dict) -> None:
        """broker 조회 한 건으로 기존 pending의 수량/지점/체결량을 보정합니다."""
        branch = str(_first(row, "ord_gno_brno", "ORD_GNO_BRNO", "krx_fwdg_ord_orgno", "KRX_FWDG_ORD_ORGNO", default="")).strip()
        if branch:
            order.branch_no = branch
        qty = _int(_first(row, "ord_qty", "ORD_QTY", default=order.qty))
        filled = _int(_first(row, "tot_ccld_qty", "TOT_CCLD_QTY", default=order.rest_filled_qty or order.filled_qty))
        if qty > 0:
            order.qty = max(order.qty, qty)
        order.rest_filled_qty = min(order.qty, max(order.rest_filled_qty, filled))
        # 누적 체결금액 필드만 사용합니다. 단일 체결금액(ccld_amt)을 누적값으로 오인하지 않습니다.
        filled_amount = _float(_first(row, "tot_ccld_amt", "TOT_CCLD_AMT", default=0))
        if filled_amount > 0:
            order.rest_filled_amount = max(order.rest_filled_amount, filled_amount)
        order.rest_sync_ts = time.time()
        order.rest_unmatched_qty = max(0, order.rest_filled_qty - order.notice_filled_qty)
        order.filled_qty = min(order.qty, max(order.filled_qty, order.rest_filled_qty))

