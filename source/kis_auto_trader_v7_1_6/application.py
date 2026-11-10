"""application.py - KIS 자동매매 애플리케이션 조립 및 실행 루프.

주문 접수 상태와 브로커 실제 상태를 분리해 관리합니다.

주문 생명주기:
    BUY/SELL 접수
        -> local pending 등록
        -> WebSocket 실제 체결통보(CNTG_YN=2)
        -> 부분체결이면 pending 유지
        -> 전량체결이면 pending 제거
        -> timeout이면 취소 요청
        -> 다음 주문 reconcile에서 KIS 미체결 목록 재조회
        -> broker 미체결 목록에서 사라지면 local pending 제거

이 구조 때문에 '취소 요청은 보냈는데 pending이 계속 남아 신규 매수가
영구 정지되는 문제'를 방지합니다.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
import os
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from config import load_settings
from config.paths import runtime_path, resolve_path
from brokers.kis.client import KisApi, RateLimitError
from trading.orders import OrderManager, FillEvent
from trading.fill_handler import FillHandler
from trading.sell_process import SellProcess
from trading.buy_process import BuyProcess
from trading.submission_guard import SymbolSubmissionGuard
from trading.subscriptions import subscribe_held_positions as sync_held_position_subscriptions
from trading.reconcile import reconcile, reconcile_open_orders, cancel_all_open_orders
from brokers.kis.realtime import KisRealtime
from storage.state import StateStore, StateRecoveryError
from storage.daily_logging import KSTDailyFileHandler, KSTFormatter
from strategies.engine import Candidate, StrategyEngine, num
from strategies.builder_engine.yaml_engine import YamlStrategyEngine
from strategies.builder_engine.official_adapter import OfficialBuilderAdapter
from strategies.builder_engine.signal_strength import score_signal
from notifications.telegram import TelegramNotifier
from storage.holiday import HolidayChecker
from risk.position_sizer import calculate_buy_qty
from trading.symbols import register_symbol, symbol_label, symbol_name

log = logging.getLogger(__name__)
order_log = logging.getLogger("trading.order")
fill_log = logging.getLogger("trading.fill")


def _configure_logging() -> None:
    """실행 프로세스에서만 운영 로그 핸들러를 설치합니다. 테스트 import는 운영 로그를 변경하지 않습니다."""
    log_dir = runtime_path("log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    # 모든 파일 로그는 각 이벤트가 기록되는 시점의 KST 날짜를 기준으로 분리합니다.
    # 프로세스가 자정을 넘어 실행되어도 다음 로그부터 새 날짜 파일로 자동 전환됩니다.
    _LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    _LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
    _root = logging.getLogger()
    _root.setLevel(_LOG_LEVEL)
    # requests/urllib3 DEBUG 로그는 Telegram bot token이 포함된 요청 경로를 기록할 수 있습니다.
    # 애플리케이션 로그 수준과 무관하게 HTTP 전송 라이브러리의 상세 URL 로그를 차단합니다.
    for _third_party_logger in ("urllib3", "urllib3.connectionpool", "requests", "httpx", "httpcore"):
        _http_logger = logging.getLogger(_third_party_logger)
        _http_logger.setLevel(logging.WARNING)
        _http_logger.disabled = True  # URL에 token/chat_id가 포함될 수 있는 외부 로그 차단
    if not any(isinstance(_handler, KSTDailyFileHandler) and _handler.prefix == "trader" for _handler in _root.handlers):
        _trader_file = KSTDailyFileHandler(log_dir, "trader")
        _trader_file.setFormatter(KSTFormatter(_LOG_FORMAT))
        _root.addHandler(_trader_file)
    if not any(isinstance(_handler, logging.StreamHandler) for _handler in _root.handlers):
        _root.addHandler(logging.StreamHandler())
    _error_handler = KSTDailyFileHandler(log_dir, "error", level=logging.ERROR)
    _error_handler.setFormatter(KSTFormatter(_LOG_FORMAT))
    _root.addHandler(_error_handler)
    for _handler in _root.handlers:
        if _handler.formatter is None:
            _handler.setFormatter(KSTFormatter(_LOG_FORMAT))
    order_log = logging.getLogger("trading.order")
    order_log.setLevel(_LOG_LEVEL)
    order_log.propagate = False
    if not order_log.handlers:
        _order_file = KSTDailyFileHandler(log_dir, "orders")
        _order_file.setFormatter(KSTFormatter(_LOG_FORMAT))
        order_log.addHandler(_order_file)
        order_log.addHandler(_error_handler)
        _order_console = logging.StreamHandler()
        _order_console.setFormatter(KSTFormatter(_LOG_FORMAT))
        order_log.addHandler(_order_console)
    # 시작/종료 포트폴리오 스냅샷과 실제 체결 이벤트도 날짜별 전용 파일에 기록합니다.
    fill_log = logging.getLogger("trading.fill")
    fill_log.setLevel(_LOG_LEVEL)
    fill_log.propagate = False
    if not fill_log.handlers:
        _fill_file = KSTDailyFileHandler(log_dir, "fills")
        _fill_file.setFormatter(KSTFormatter(_LOG_FORMAT))
        fill_log.addHandler(_fill_file)
    
    log = logging.getLogger(__name__)


def normalize_symbol(row: dict) -> str:
    """조건검색 결과에서 6자리 종목코드를 추출합니다."""
    for key in ("mksc_shrn_iscd", "stck_shrn_iscd", "MKSC_SHRN_ISCD", "STCK_SHRN_ISCD", "pdno", "PDNO", "code"):
        value = row.get(key)
        if value:
            return str(value).strip().zfill(6)
    return ""


def price_value(data: dict) -> float:
    """WebSocket/REST 현재가 응답에서 현재가를 추출합니다."""
    return num(data or {}, "STCK_PRPR", "stck_prpr", "prpr", default=0.0)

def _count_filled_buy_orders(rows: list[dict] | None) -> int:
    """당일 체결내역에서 실제 체결이 1주 이상 발생한 고유 BUY 주문 수를 셉니다.

    부분체결은 여러 행/통보로 나뉘어도 주문번호당 1회만 계산합니다.
    """
    order_numbers: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        side_code = str(row.get("sll_buy_dvsn_cd", row.get("SLL_BUY_DVSN_CD", ""))).strip()
        side_name = str(row.get("sll_buy_dvsn_cd_name", row.get("SLL_BUY_DVSN_CD_NAME", ""))).strip().upper()
        if not (side_code == "02" or "매수" in side_name or side_name == "BUY"):
            continue
        raw_filled = row.get("tot_ccld_qty", row.get("TOT_CCLD_QTY", row.get("ccld_qty", row.get("CCLD_QTY", 0))))
        try:
            filled_qty = int(float(str(raw_filled or 0).replace(",", "")))
        except (TypeError, ValueError):
            filled_qty = 0
        if filled_qty <= 0:
            continue
        order_no = str(row.get("odno", row.get("ODNO", row.get("order_no", "")))).strip()
        if not order_no:
            # 누락된 주문번호는 행을 합치지 않고 각각 보수적으로 집계합니다.
            order_no = f"missing:{len(order_numbers)}:{row.get('pdno', row.get('PDNO', ''))}:{filled_qty}"
        order_numbers.add(order_no)
    return len(order_numbers)

def _should_latch_daily_buy_limit(history_ok: bool, trades_today: int, limit: int) -> bool:
    """이력 복구 실패 또는 일일 한도 도달이면 당일 신규 매수 잠금을 유지합니다."""
    return (not history_ok) or max(0, int(trades_today or 0)) >= max(1, int(limit or 1))


def _recover_daily_buy_trade_count(api, store: StateStore, settings) -> bool:
    """재시작 후 KIS 당일 체결이력을 기준으로 trades_today가 과소 집계되지 않게 복구합니다.

    이력 조회에 실패하면 일일 거래한도만큼 사용한 것으로 보수 처리해 신규 매수를 차단합니다.
    기존 카운터보다 낮게 덮어쓰지 않아 과다 카운트도 안전하게 보존합니다.
    """
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    try:
        filled_rows = api.daily_orders(today, today, ccld_dvsn="00")
        broker_count = _count_filled_buy_orders(filled_rows)
        previous_count = max(0, int(store.state.trades_today or 0))
        recovered_count = max(previous_count, broker_count)
        if recovered_count != previous_count:
            store.state.trades_today = recovered_count
            store.save()
        log.info("당일 매수 거래횟수 복구 완료: date=%s local_before=%d broker_filled_buy_orders=%d effective=%d",
                 today, previous_count, broker_count, recovered_count)
        return True
    except Exception as exc:
        previous_count = max(0, int(store.state.trades_today or 0))
        store.state.trades_today = max(previous_count, int(settings.max_trades_per_day))
        store.save()
        log.exception("당일 매수 체결이력 조회 실패: 신규 매수 안전 차단 trades_today=%d limit=%d error=%s",
                      store.state.trades_today, settings.max_trades_per_day, type(exc).__name__)
        return False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

def _show_order_calculation() -> bool:
    return os.getenv("KIS_SHOW_ORDER_CALCULATION", "true").strip().lower() in {"1", "true", "yes", "on"}


def _meets_min_stock_price(current_price: float, minimum_price: float) -> bool:
    """HTS 후보 1주 가격 하한. 0 이하는 필터를 비활성화합니다."""
    minimum_price = max(0.0, float(minimum_price or 0.0))
    return current_price > 0 and (minimum_price <= 0 or current_price >= minimum_price)

def _resolve_order_mode(configured: str, side: str, current: float, reference: float,
                        high_price: float = 0.0, is_stop_loss: bool = False,
                        quote: dict | None = None, order_qty: int = 0) -> tuple[str, str]:
    """환경변수 주문 모드를 실제 KIS 주문 방식으로 결정합니다.

    손절 AUTO는 가격 손실/고점 대비 급락, 호가 스프레드, 매수1호가 잔량,
    시세 수신 시각을 함께 봅니다. 주문 방식 선택은 체결을 보장하지 않으며,
    유효한 호가가 없으면 시장가로 맹목 주문하지 않고 HOLD 합니다.
    """
    mode = (configured or "BEST_LIMIT").strip().upper()
    if mode not in {"MARKET", "BEST_LIMIT", "AUTO"}:
        raise ValueError(f"잘못된 주문 방식 {configured!r}; MARKET/BEST_LIMIT/AUTO만 허용")
    if mode != "AUTO":
        return mode, "환경변수 고정 주문 방식"

    selected = os.getenv("KIS_AUTO_SELECTED_MODE", "BEST_LIMIT").strip().upper()
    if selected not in {"MARKET", "BEST_LIMIT"}:
        raise ValueError("KIS_AUTO_SELECTED_MODE는 MARKET 또는 BEST_LIMIT이어야 합니다.")
    threshold = max(0.0, _env_float("KIS_AUTO_PRICE_MOVE_THRESHOLD_PCT", 1.0)) / 100.0
    q = quote or {}

    if side == "BUY":
        if reference <= 0 or current <= 0:
            return "HOLD", "기준가격/현재가 확인 불가"
        change = (current / reference) - 1.0
        if abs(change) >= threshold and threshold > 0:
            return "HOLD", f"기준가격 대비 변동 {change * 100:.2f}%가 임계치 {threshold * 100:.2f}% 이상; 재검색 대기"
        return selected, f"AUTO 매수: 기준가격 대비 변동 {change * 100:.2f}%, 선택={selected}"

    if side != "SELL":
        raise ValueError(f"지원하지 않는 side: {side}")

    if is_stop_loss:
        # stop loss는 일반 매도보다 체결 우선순위를 높이되, 오래되거나
        # 가격/호가가 불완전한 데이터로 주문하지 않습니다.
        if current <= 0:
            return "HOLD", "손절 AUTO: 현재가가 유효하지 않아 주문 보류"
        max_age = max(1.0, _env_float("KIS_STOP_AUTO_QUOTE_MAX_AGE_SEC", 5.0))
        received_ts = _safe_float(q.get("_received_ts", 0), 0.0)
        if received_ts <= 0 or (time.time() - received_ts) > max_age:
            return "HOLD", f"손절 AUTO: 시세 수신시각 없음/오래됨(허용 {max_age:.1f}초), 재조회 대기"

        bid = num(q, "BIDP1", "bidp1", default=0.0)
        ask = num(q, "ASKP1", "askp1", default=0.0)
        bid_qty = num(q, "BIDP_RSQN1", "bidp_rsqn1", "BIDP_RSQN", "bidp_rsqn", default=0.0)
        spread_limit = max(0.0, _env_float("KIS_STOP_AUTO_MAX_SPREAD_PCT", 0.5))
        market_drop = max(0.0, _env_float("KIS_STOP_AUTO_MARKET_DROP_PCT", 1.0)) / 100.0
        min_bid_qty_ratio = max(0.0, _env_float("KIS_STOP_AUTO_MIN_BID_QTY_RATIO", 1.0))

        if bid <= 0 or ask <= 0 or ask < bid:
            # 손절 신호가 이미 발생했고 현재가는 유효하지만 호가 필드만 누락된 경우,
            # AUTO가 HOLD를 반복하면 손절이 무기한 실행되지 않을 수 있습니다.
            # 이때는 호가 기반 지정가 판단을 포기하고 시장가로 청산 우선 처리합니다.
            return "MARKET", "손절 AUTO: 유효한 1호가 누락; 손절 지연 방지를 위해 시장가 청산 선택"
        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 else 999.0
        avg_loss_pct = ((current / reference) - 1.0) * 100.0 if reference > 0 else 0.0
        high_drawdown_pct = ((current / high_price) - 1.0) * 100.0 if high_price > 0 else 0.0
        urgent = ((reference > 0 and current <= reference * (1.0 - market_drop)) or
                  (high_price > 0 and current <= high_price * (1.0 - market_drop)))
        depth_fields = ("BIDP_RSQN1", "bidp_rsqn1", "BIDP_RSQN", "bidp_rsqn")
        depth_known = any(key in q and q.get(key) not in (None, "") for key in depth_fields)
        required_bid_qty = max(1.0, float(order_qty) * min_bid_qty_ratio) if order_qty > 0 else min_bid_qty_ratio
        depth_weak = depth_known and bid_qty < required_bid_qty
        if urgent:
            return "MARKET", (f"손절 AUTO: 긴급 청산(평단대비 {avg_loss_pct:.2f}%, 고점대비 {high_drawdown_pct:.2f}%, "
                               f"스프레드 {spread_pct:.3f}%, 매수1호가잔량={bid_qty:g}); 체결 우선")
        if spread_pct > spread_limit:
            # 아직 급락 임계치 전이고 스프레드가 넓다면 시장가 슬리피지를 피합니다.
            return "BEST_LIMIT", f"손절 AUTO: 스프레드 {spread_pct:.3f}% > {spread_limit:.3f}%; 가격 보호 우선"
        if depth_weak:
            return "MARKET", f"손절 AUTO: 매수1호가 잔량 {bid_qty:g} < 기준 {required_bid_qty:g}; 미체결 위험 우선 대응"
        return selected, (f"손절 AUTO: 급락 아님(평단대비 {avg_loss_pct:.2f}%, 고점대비 {high_drawdown_pct:.2f}%), "
                          f"스프레드 {spread_pct:.3f}%, 매수1호가잔량={bid_qty:g}, 선택={selected}")

    ref = high_price if high_price > 0 else reference
    if ref > 0 and current > 0 and current <= ref * (1.0 - threshold):
        return "MARKET", f"AUTO 매도: 고점/기준가 대비 {((current/ref)-1)*100:.2f}% 하락, 시장가 선택"
    return selected, f"AUTO 매도: 급락 임계치 미도달, 선택={selected}"


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _quote_price_for_order(quote: dict, mode: str, fallback: float) -> float:
    """최유리지정가 매수 수량 산정에는 최우선 매도호가를 사용합니다."""
    if mode == "BEST_LIMIT":
        ask = num(quote or {}, "ASKP1", "askp1", default=0.0)
        if ask > 0:
            return ask
    return fallback

def _quote_sell_price_for_order(quote: dict, mode: str, fallback: float) -> float:
    """최유리지정가 매도 정산 추정에는 최우선 매수호가를 사용합니다."""
    if mode == "BEST_LIMIT":
        bid = num(quote or {}, "BIDP1", "bidp1", default=0.0)
        if bid > 0:
            return bid
    return fallback

def _is_stop_reason(reason: str) -> bool:
    text = str(reason or "").upper()
    return any(key in text for key in ("STOP_LOSS", "STOP LOSS", "STOPLOSS", "TRAILING_STOP", "손절", "트레일링"))


def _fill_may_already_be_in_balance(fill_event_ts: float, balance_reconcile_ts: float) -> bool:
    """체결 이벤트가 마지막 성공 잔고 대사보다 오래되면 REST 잔고가 이미 반영했을 수 있습니다."""
    try:
        event_ts = float(fill_event_ts or 0.0)
        balance_ts = float(balance_reconcile_ts or 0.0)
    except (TypeError, ValueError):
        return False
    return event_ts > 0 and balance_ts >= event_ts


def _is_no_broker_position_error(error: object) -> bool:
    """KIS가 잔고 없음/수량 부족을 명시적으로 거절한 응답인지 판별합니다."""
    message = str(error or "")
    return "40240000" in message or "잔고내역이 없습니다" in message or "주문가능수량" in message and "없" in message


def _sell_retry_delay(error: object, default_sec: float) -> float:
    """호출 제한·잔고 불일치·일반 오류를 구분해 매도 재시도 대기시간을 정합니다."""
    message = str(error or "")
    if isinstance(error, RateLimitError) or any(code in message for code in ("EGW00201", "EGW00215", "HTTP 429")):
        return max(1.0, _env_float("KIS_RATE_LIMIT_COOLDOWN_SEC", 10.0))
    if _is_no_broker_position_error(error):
        return max(30.0, _env_float("SELL_NO_BALANCE_RETRY_SEC", 30.0))
    return max(1.0, float(default_sec))

def _evaluate_exit_priority(pnl_pct: float, strategy_sell: bool,
                           stop_loss_hard_pct: float = 2.0,
                           stop_loss_first_pct: float = 1.5,
                           take_profit_first_pct: float = 1.5,
                           take_profit_hard_pct: float = 3.0) -> tuple[bool, str]:
    """손익 구간과 YAML SELL 신호를 결합해 청산 우선순위를 적용합니다.

    우선순위: 2차 손절 -> 전략 SELL+1차 손절 -> 전략 SELL+1차 익절 -> 2차 익절.
    실제 퍼센트는 인자로 전달되며 기본값은 .env.example과 일치합니다.
    """
    stop_hard = abs(float(stop_loss_hard_pct))
    stop_first = abs(float(stop_loss_first_pct))
    take_first = abs(float(take_profit_first_pct))
    take_hard = abs(float(take_profit_hard_pct))
    if pnl_pct <= -stop_hard:
        return True, f"STOP_LOSS_HARD {pnl_pct:.2f}% <= -{stop_hard:.2f}% (전략 신호 무관)"
    if strategy_sell and pnl_pct <= -stop_first:
        return True, f"STOP_LOSS_FIRST {pnl_pct:.2f}% <= -{stop_first:.2f}% + STRATEGY SELL"
    if strategy_sell and pnl_pct >= take_first:
        return True, f"TAKE_PROFIT_FIRST {pnl_pct:.2f}% >= {take_first:.2f}% + STRATEGY SELL"
    if pnl_pct >= take_hard:
        return True, f"TAKE_PROFIT_HARD {pnl_pct:.2f}% >= {take_hard:.2f}% (전략 신호 무관)"
    return False, ""


def _sell_order_window() -> bool:
    """매도 가능 시간은 신규 진입 시간과 분리합니다. 정규장 주문 가능 구간을 보수적으로 적용."""
    now = datetime.now(ZoneInfo("Asia/Seoul")).time()
    start = dtime.fromisoformat(os.getenv("SELL_START_TIME", "09:00"))
    end = dtime.fromisoformat(os.getenv("SELL_END_TIME", "15:20"))
    return start <= now < end


def run_cycle(
    api: KisApi,
    store: StateStore,
    engine: StrategyEngine,
    orders: OrderManager,
    realtime: KisRealtime,
    price_cache: dict,
    price_cache_lock: threading.RLock,
    last_rest_quote: dict,
    style: str,
    condition_name: str,
    max_candidates: int,
    quote_stale_sec: int,
    quote_rest_min_interval_sec: float,
    candidate_sink: list[Candidate] | None = None,
):
    """HTS 조건검색 결과를 정규화하고 가격을 확인해 평가용 후보 풀을 구성합니다.

    candidate_sink가 주어지면 후보를 전달하고 여기서는 점수 판정을 하지 않습니다.
    최종 BUY 신호는 STRATEGY_EVALUATION_MODE가 선택한 평가 엔진이 결정합니다.

    조건명이 비어 있으면 설정 누락으로 간주하고 KIS 조건검색 API를 호출하지 않습니다.
    """
    if not str(condition_name or "").strip():
        log.info("[%s] HTS 조건검색 건너뜀: KIS_CONDITION_NAME이 비어 있습니다.", style)
        return None
    log.info("[HTS_CONDITION] lookup_start style=%s condition_name=%r entry_mode=%s",
             style, condition_name, os.getenv("ENTRY_MODE", "HTS").strip().upper())
    try:
        seq = api.find_condition_seq(condition_name)
        log.info("[HTS_CONDITION] lookup_success style=%s condition_name=%r seq=%s", style, condition_name, seq)
    except Exception as exc:
        log.error("[%s] HTS 조건검색 설정/조회 실패: condition=%r error=%s: %s",
                  style, condition_name, type(exc).__name__, exc)
        return None
    try:
        rows = api.condition_result(seq)
    except Exception as exc:
        log.exception("[%s] HTS 조건검색 결과 조회 실패: condition=%r seq=%s", style, condition_name, seq)
        return None
    if not rows:
        log.error("[%s] HTS 조건검색 결과 0건: condition=%r seq=%s; 신규 매수 후보 없음",
                  style, condition_name, seq)
        return None
    log.info("[HTS_CONDITION] result_received style=%s condition_name=%r seq=%s rows=%d sample_keys=%s",
             style, condition_name, seq, len(rows), sorted(str(k) for k in rows[0].keys()) if rows and isinstance(rows[0], dict) else [])

    symbols: list[str] = []
    candidates_meta: list[dict] = []
    for row in rows:
        symbol = normalize_symbol(row)
        if not symbol:
            continue
        if store.cooldown_active(symbol):
            log.info("[%s] HTS 후보 제외 symbol=%s reason=cooldown_active", style, symbol_label(symbol, str(row.get("hts_kor_isnm", ""))))
            continue
        if orders.pending_for_symbol(symbol):
            log.info("[%s] HTS 후보 제외 symbol=%s reason=pending_or_uncertain_order", style, symbol_label(symbol, str(row.get("hts_kor_isnm", ""))))
            continue
        if symbol in symbols:
            log.debug("[%s] HTS 후보 중복 제거 symbol=%s", style, symbol)
            continue
        symbols.append(symbol)
        candidates_meta.append(row)

    if not symbols:
        log.info("[%s] 조건검색 후보 없음: 검색결과=%d 유효코드=%d cooldown_or_pending_or_duplicate 제외 포함", style, len(rows), len(symbols))
        return None
    log.info("[HTS_CONDITION] normalized_candidates style=%s condition_name=%r seq=%s valid_unique=%d symbols=%s",
             style, condition_name, seq, len(symbols), ",".join(symbols))

    # 후보 구독은 이번 회차 목록으로 교체합니다. 구독 대상 누적에 따른 KIS 제한을 방지합니다.
    if hasattr(realtime, "set_candidate_symbols"):
        realtime.set_candidate_symbols(symbols)
    else:
        realtime.subscribe_symbols(symbols)

    candidates: list[Candidate] = []
    now = time.time()
    min_stock_price = max(0.0, _env_float("MIN_STOCK_PRICE_KRW", 0.0))
    for row, symbol in zip(candidates_meta, symbols):
        if len(candidates) >= max_candidates:
            break
        with price_cache_lock:
            cached = dict(price_cache.get(symbol, {}))
        ts = float(cached.get("_received_ts", 0) or 0)
        price = cached if cached and now - ts <= quote_stale_sec else {}

        if not price:
            last = float(last_rest_quote.get(symbol, 0) or 0)
            if now - last >= quote_rest_min_interval_sec:
                try:
                    price = api.current_price(symbol)
                    price = dict(price or {})
                    price["_received_ts"] = time.time()
                    with price_cache_lock:
                        price_cache[symbol] = dict(price)
                    last_rest_quote[symbol] = time.time()
                except Exception:
                    log.exception("[%s] 현재가 fallback 실패: %s", style, symbol_label(symbol, str(row.get("hts_kor_isnm", row.get("prdt_name", "")))))
                    continue

        current_price = price_value(price)
        if current_price <= 0:
            log.warning("[%s] 현재가 없음으로 후보 제외: %s", style, symbol_label(symbol, str(row.get("hts_kor_isnm", row.get("prdt_name", "")))))
            continue
        if not _meets_min_stock_price(current_price, min_stock_price):
            log.info("[%s] 1주 현재가 기준 미달 제외: %s price=%.0f minimum=%.0f", style, symbol_label(symbol, str(row.get("hts_kor_isnm", row.get("prdt_name", "")))),
                     current_price, min_stock_price)
            continue

        name = str(row.get("hts_kor_isnm", row.get("prdt_name", row.get("PRDT_NAME", ""))))
        candidates.append(Candidate(symbol=symbol, name=name, hts=row, price=price))

    if candidate_sink is not None:
        candidate_sink.extend(candidates)
        # V7.1.0: HTS는 후보 출처일 뿐이며 BUY 판정은 선택된 평가 엔진이 수행합니다.
        return None
    pick = engine.pick_one(candidates, style)
    threshold = engine.s.strategy.min_score
    if not pick:
        log.info("[%s] 기준점수 이상 후보 없음: min_score=%.2f 후보수=%d",
                 style, threshold, len(candidates))
        return None

    log.info(
        "[%s] PICK %s score=%.2f min_score=%.2f reason=%s",
        style, symbol_label(pick.symbol, pick.name), pick.score, threshold, pick.reason,
    )
    return pick


def _market_open_window() -> bool:
    """KST 신규 진입 시간과 AUTO_SHUTDOWN_TIME 중 더 이른 종료선을 검사합니다."""
    now = datetime.now(ZoneInfo("Asia/Seoul")).time()
    start = dtime.fromisoformat(os.getenv("ENTRY_START_TIME", "09:10"))
    end = dtime.fromisoformat(os.getenv("ENTRY_END_TIME", "15:10"))
    shutdown = dtime.fromisoformat(os.getenv("AUTO_SHUTDOWN_TIME", "15:10"))
    effective_end = min(end, shutdown)
    return start <= now < effective_end


def _configured_builder_symbols() -> list[str]:
    """Builder의 유일한 수동 종목 목록(BUILDER_SYMBOLS_FILE)을 읽습니다."""
    from config.builder_symbols import load_builder_symbols
    return load_builder_symbols(logger=log)


_ENTRY_CANDIDATE_CURSOR = 0
_ENTRY_CANDIDATE_CURSOR_LOCK = threading.Lock()

_DAILY_BAR_CACHE_LOCK = threading.RLock()
_DAILY_BAR_CACHE: dict[tuple[str, int, str], tuple[float, list[dict]]] = {}
_DAILY_BAR_FAILURE_CACHE: dict[tuple[str, int, str], tuple[float, str]] = {}
_DAILY_BAR_KEY_LOCKS: dict[tuple[str, int, str], threading.Lock] = {}


def required_daily_bars(evaluation_mode: str, *strategy_paths: str) -> int:
    """평가 엔진/YAML 지표의 최대 lookback을 기준으로 필요한 일봉 수를 계산합니다.

    KIS10/ALL은 strategy_profiles의 BUY/SELL 참여 목록에 있는 실제 활성 전략의
    최대 lookback(+5봉)을 사용합니다. observe_all이면 모든 전략을 포함합니다.
    CUSTOM은 활성 YAML indicator period를 읽어 필요한 범위(+5봉)만 조회합니다.
    YAML을 읽지 못하면 데이터 부족을 숨기지 않도록 260일 안전 기본값을 사용합니다.
    """
    mode = str(evaluation_mode or os.getenv("STRATEGY_EVALUATION_MODE", "ALL")).strip().upper()
    try:
        import yaml
        max_period = 20
        found = False
        if mode in {"KIS10", "ALL"}:
            profile_path = resolve_path(os.getenv("STRATEGY_PROFILES_FILE", "config/strategy_profiles.yaml"))
            with profile_path.open("r", encoding="utf-8") as handle:
                profile = yaml.safe_load(handle) or {}
            strategy_rows = profile.get("strategies") or []
            if not isinstance(strategy_rows, list):
                raise ValueError("strategy_profiles.strategies must be a list")
            by_id = {str(row.get("id")): row for row in strategy_rows if isinstance(row, dict) and row.get("id")}
            if os.getenv("BUILDER_OBSERVE_ALL_STRATEGIES", "false").strip().lower() in {"1", "true", "yes", "on"}:
                active_ids = set(by_id)
            else:
                decision = profile.get("decision") or {}
                active_ids = set()
                for side in ("buy", "sell"):
                    rows = ((decision.get(side) or {}).get("strategies") or [])
                    if not isinstance(rows, list):
                        raise ValueError(f"decision.{side}.strategies must be a list")
                    active_ids.update(str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id"))
            for strategy_id in active_ids:
                row = by_id.get(strategy_id)
                if not row:
                    raise ValueError(f"strategy profile missing: {strategy_id}")
                strategy_path = resolve_path(row.get("file", ""))
                with strategy_path.open("r", encoding="utf-8") as handle:
                    document = yaml.safe_load(handle) or {}
                strategy = document.get("strategy") or {}
                strategy_params = strategy.get("params") or {}
                if not isinstance(strategy_params, dict):
                    raise ValueError(f"strategy params invalid: {strategy_id}")
                for key, raw_value in strategy_params.items():
                    if any(token in str(key).lower() for token in ("period", "days", "lookback", "window")):
                        try:
                            value = int(raw_value)
                        except (TypeError, ValueError):
                            continue
                        max_period = max(max_period, value)
                        found = found or value > 0
                indicators = strategy.get("indicators") or []
                if not isinstance(indicators, list):
                    raise ValueError(f"strategy indicators invalid: {strategy_id}")
                for indicator in indicators:
                    if not isinstance(indicator, dict):
                        continue
                    params = indicator.get("params") or {}
                    if not isinstance(params, dict):
                        continue
                    for key in ("period", "length", "window", "lookback"):
                        raw_value = params.get(key, 0)
                        if isinstance(raw_value, str) and raw_value.startswith("$"):
                            raw_value = strategy_params.get(raw_value[1:], 0)
                        try:
                            value = int(raw_value or 0)
                        except (TypeError, ValueError):
                            value = 0
                        max_period = max(max_period, value)
                        found = found or value > 0
        else:
            for raw_path in strategy_paths:
                if not raw_path:
                    continue
                path = resolve_path(raw_path)
                if not path.is_file():
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    document = yaml.safe_load(handle) or {}
                strategy = document.get("strategy") or {}
                strategy_params = strategy.get("params") or {}
                if not isinstance(strategy_params, dict):
                    raise ValueError("strategy.params must be an object")
                for key, raw_value in strategy_params.items():
                    if any(token in str(key).lower() for token in ("period", "days", "lookback", "window")):
                        try:
                            value = int(raw_value)
                        except (TypeError, ValueError):
                            continue
                        max_period = max(max_period, value)
                        found = found or value > 0
                indicators = strategy.get("indicators") or []
                if not isinstance(indicators, list):
                    raise ValueError("indicators must be a list")
                for indicator in indicators:
                    if not isinstance(indicator, dict):
                        continue
                    params = indicator.get("params") or {}
                    if isinstance(params, dict):
                        for key in ("period", "length", "window", "lookback"):
                            raw_value = params.get(key, 0)
                            if isinstance(raw_value, str) and raw_value.startswith("$"):
                                raw_value = strategy_params.get(raw_value[1:], 0)
                            try:
                                value = int(raw_value or 0)
                            except (TypeError, ValueError):
                                value = 0
                            max_period = max(max_period, value)
                            found = found or value > 0
        return max(25, max_period + 5) if found else 60
    except Exception as exc:
        log.warning("전략 YAML/profile lookback 확인 실패; 안전 기본 일봉 260개 사용: error_type=%s", type(exc).__name__)
        return 260


def get_cached_daily_prices(api: KisApi, symbol: str, days: int = 260) -> list[dict]:
    """동일 종목/기간 일봉을 5분 캐시하고 실패 종목은 재시도 간격을 둡니다.

    일봉은 실시간 시세와 달리 초단위 갱신이 필요하지 않습니다. 장중 전략은
    별도 현재가/WebSocket을 사용하므로 300초 캐시로 일봉 REST 중복을 줄입니다.
    API 실패는 60초 음성 캐시로 반복 호출을 억제하되, 성공 데이터는 정상 재사용합니다.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    normalized = str(symbol).strip().zfill(6)
    requested = max(25, int(days))
    date_key = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    key = (normalized, requested, date_key)
    ttl = max(300.0, _env_float("DAILY_BAR_CACHE_TTL_SEC", 300.0))
    failure_ttl = max(15.0, _env_float("DAILY_BAR_ERROR_RETRY_SEC", 60.0))
    now = time.monotonic()
    with _DAILY_BAR_CACHE_LOCK:
        cached = _DAILY_BAR_CACHE.get(key)
        if cached and now - cached[0] <= ttl:
            return [dict(row) for row in cached[1]]
        failed = _DAILY_BAR_FAILURE_CACHE.get(key)
        if failed and now - failed[0] <= failure_ttl:
            raise RuntimeError(f"일봉 API 실패 재시도 대기({failure_ttl:.0f}초): {failed[1]}")
        key_lock = _DAILY_BAR_KEY_LOCKS.setdefault(key, threading.Lock())
    with key_lock:
        now = time.monotonic()
        with _DAILY_BAR_CACHE_LOCK:
            cached = _DAILY_BAR_CACHE.get(key)
            if cached and now - cached[0] <= ttl:
                return [dict(row) for row in cached[1]]
            failed = _DAILY_BAR_FAILURE_CACHE.get(key)
            if failed and now - failed[0] <= failure_ttl:
                raise RuntimeError(f"일봉 API 실패 재시도 대기({failure_ttl:.0f}초): {failed[1]}")
        try:
            bars = api.daily_prices(normalized, days=requested)
            if not bars:
                raise RuntimeError("KIS 일봉 API가 빈 데이터를 반환했습니다.")
        except Exception as exc:
            with _DAILY_BAR_CACHE_LOCK:
                _DAILY_BAR_FAILURE_CACHE[key] = (time.monotonic(), f"{type(exc).__name__}: {exc}"[:240])
            raise
        snapshot = [dict(row) for row in bars]
        with _DAILY_BAR_CACHE_LOCK:
            _DAILY_BAR_CACHE[key] = (time.monotonic(), snapshot)
            _DAILY_BAR_FAILURE_CACHE.pop(key, None)
            # 오래된 날짜/기간 캐시는 메모리에서 정리합니다.
            for old_key, old_value in list(_DAILY_BAR_CACHE.items()):
                if old_key[2] != date_key or time.monotonic() - old_value[0] > ttl * 4:
                    _DAILY_BAR_CACHE.pop(old_key, None)
                    _DAILY_BAR_FAILURE_CACHE.pop(old_key, None)
                    _DAILY_BAR_KEY_LOCKS.pop(old_key, None)
            for old_key, old_value in list(_DAILY_BAR_FAILURE_CACHE.items()):
                if old_key[2] != date_key or time.monotonic() - old_value[0] > failure_ttl * 4:
                    _DAILY_BAR_FAILURE_CACHE.pop(old_key, None)
        return [dict(row) for row in snapshot]

def sell_retry_backoff_seconds(retry_count: int, base_retry_sec: float,
                               max_backoff_sec: float, settle_sec: float) -> float:
    """반복 취소 SELL 재평가 대기시간을 지수형으로 계산합니다."""
    base = max(1.0, float(base_retry_sec))
    ceiling = max(base, float(max_backoff_sec))
    count = max(1, int(retry_count))
    exponential = min(ceiling, base * (2 ** min(count - 1, 10)))
    return max(exponential, max(0.0, float(settle_sec)))


def _daily_change_pct(quote: dict) -> float | None:
    """KIS 현재가 응답의 전일 대비 등락률(%)을 안전하게 읽습니다."""
    for key in ("prdy_ctrt", "PRDY_CTRT", "change_pct", "change_rate", "chgrate"):
        value = quote.get(key) if isinstance(quote, dict) else None
        if value not in (None, ""):
            try:
                result = float(str(value).replace(",", "").replace("%", "").strip())
                if result == result and abs(result) != float("inf"):
                    return result
            except (TypeError, ValueError):
                continue
    return None


def run_builder_cycle(api: KisApi, store: StateStore,
                      price_cache: dict, price_cache_lock: threading.RLock,
                      quote_stale_sec: int, profile_name: str = "unified",
                      extra_symbols: list[str] | None = None,
                      include_manual: bool = True,
                      evaluation_mode: str | None = None) -> Candidate | None:
    """후보 출처와 평가 엔진을 분리해 선정된 종목을 평가합니다.

    ENTRY_MODE가 결정한 HTS 후보(extra_symbols)와 수동 목록을 입력으로 받습니다.
    STRATEGY_EVALUATION_MODE=KIS10/CUSTOM/ALL에 따라 한투 10개 전략, 자체 YAML,
    또는 두 엔진을 독립 평가합니다. ALL 매수는 두 엔진 모두 BUY여야 승인합니다.
    전략 계산/시세 오류는 신규 매수를 허용하지 않는 fail-closed 방식입니다.
    """
    from config.builder_symbols import combine_candidate_symbols
    mode = (evaluation_mode or os.getenv("STRATEGY_EVALUATION_MODE", "ALL")).strip().upper()
    if mode not in {"KIS10", "CUSTOM", "ALL"}:
        raise ValueError("STRATEGY_EVALUATION_MODE는 KIS10, CUSTOM, ALL 중 하나여야 합니다.")
    max_daily_change = _env_float("ENTRY_MAX_DAILY_CHANGE_PCT", 25.0)
    min_daily_change = _env_float("ENTRY_MIN_DAILY_CHANGE_PCT", -15.0)
    if min_daily_change >= max_daily_change:
        log.error("[ENTRY_DAILY_CHANGE_FILTER] invalid_thresholds lower=%.3f upper=%.3f; 신규 매수 평가 중단",
                  min_daily_change, max_daily_change)
        return None
    hts_symbols = extra_symbols or []
    manual_symbols = _configured_builder_symbols() if include_manual else []
    combined_symbols = combine_candidate_symbols(hts_symbols, manual_symbols)
    hts_unique = set(combine_candidate_symbols(hts_symbols, []))
    manual_unique = set(combine_candidate_symbols([], manual_symbols))
    log.info("[ENTRY_CANDIDATE_POOL] hts=%d manual=%d union=%d evaluation_mode=%s",
             len(hts_unique), len(manual_unique), len(combined_symbols), mode)
    for candidate_symbol in combined_symbols:
        sources = []
        if candidate_symbol in hts_unique: sources.append("HTS")
        if candidate_symbol in manual_unique: sources.append("BUILDER_SYMBOLS_FILE")
        log.info("[CANDIDATE_SOURCE] symbol=%s source=%s HTS_SCORED_CANDIDATE=%s", candidate_symbol, "+".join(sources), candidate_symbol in hts_unique)
    symbols = [sym for sym in combined_symbols if not store.cooldown_active(sym)]
    cooled = [sym for sym in combined_symbols if store.cooldown_active(sym)]
    if cooled:
        log.info("[CANDIDATE_FILTER] reason=cooldown_active symbols=%s", ",".join(cooled))
    candidate_limit = max(1, min(20, int(_env_float("STRATEGY_BUILDER_MAX_SYMBOLS", 20))))
    if len(symbols) > candidate_limit:
        # HTS 응답 순서를 우선순위로 간주하지 않습니다. 정규화된 종목코드 목록을
        # 회차마다 순환 평가해 긴 후보 목록의 뒤쪽 종목도 반복적으로 평가 기회를 얻습니다.
        global _ENTRY_CANDIDATE_CURSOR
        ordered_symbols = sorted(symbols)
        with _ENTRY_CANDIDATE_CURSOR_LOCK:
            start_at = _ENTRY_CANDIDATE_CURSOR % len(ordered_symbols)
            rotated = ordered_symbols[start_at:] + ordered_symbols[:start_at]
            symbols = rotated[:candidate_limit]
            _ENTRY_CANDIDATE_CURSOR = (start_at + candidate_limit) % len(ordered_symbols)
        log.info("[ENTRY_CANDIDATE_LIMIT] total=%d evaluated=%d next_cursor=%d order=rotating_symbol_set",
                 len(ordered_symbols), len(symbols), _ENTRY_CANDIDATE_CURSOR)
    if not symbols:
        log.warning("[ENTRY_CANDIDATE_POOL] 평가할 종목 없음; HTS 조건 또는 BUILDER_SYMBOLS_FILE 확인")
        return None

    # 두 엔진이 같은 시점의 동일한 일봉 데이터를 사용하도록 한 번만 수집합니다.
    entry_path = str(resolve_path(os.getenv("ENTRY_STRATEGY_FILE", "config/my_entry_strategy.kis.yaml")))
    exit_path = str(resolve_path(os.getenv("EXIT_STRATEGY_FILE", "config/my_exit_strategy.kis.yaml")))
    required_bars = required_daily_bars(mode, entry_path, exit_path)
    log.info("[ENTRY_DAILY_DATA_POLICY] mode=%s required_bars=%d cache_ttl_sec=%.0f error_retry_sec=%.0f",
             mode, required_bars, max(300.0, _env_float("DAILY_BAR_CACHE_TTL_SEC", 300.0)),
             max(15.0, _env_float("DAILY_BAR_ERROR_RETRY_SEC", 60.0)))
    bars_by_symbol: dict[str, list[dict] | None] = {}
    bar_status_by_symbol: dict[str, str] = {}
    for symbol in symbols:
        try:
            bars = get_cached_daily_prices(api, symbol, days=required_bars)
            if len(bars) < required_bars:
                log.warning("[ENTRY_EVALUATION_DATA] symbol=%s status=INSUFFICIENT_HISTORY returned_bars=%d required_bars=%d; 신규 매수 평가 제외",
                            symbol, len(bars), required_bars)
                bars_by_symbol[symbol] = None
                bar_status_by_symbol[symbol] = "INSUFFICIENT_HISTORY"
                continue
            bars_by_symbol[symbol] = bars
            bar_status_by_symbol[symbol] = "OK"
        except Exception as exc:
            log.warning("[ENTRY_EVALUATION_DATA] symbol=%s status=API_ERROR reason=%s: %s", symbol, type(exc).__name__, exc)
            bars_by_symbol[symbol] = None
            bar_status_by_symbol[symbol] = "API_ERROR"

    kis_results: dict[str, dict] = {}
    if mode in {"KIS10", "ALL"}:
        try:
            evaluator = OfficialBuilderAdapter(str(resolve_path(os.getenv("STRATEGY_PROFILES_FILE", "config/strategy_profiles.yaml"))))
            observe_all = os.getenv("BUILDER_OBSERVE_ALL_STRATEGIES", "false").strip().lower() in {"1", "true", "yes", "on"}
            kis_results = evaluator.evaluate(
                "unified", symbols, observe_all=observe_all, api=api,
                bars_by_symbol=bars_by_symbol, data_status_by_symbol=bar_status_by_symbol,
            )
        except Exception:
            log.exception("[KIS10_EVALUATION] 평가 실패; 해당 엔진 매수 신호는 거부")
            kis_results = {sym: {"action": "ERROR", "score": 0, "reason": "KIS10 evaluator error"} for sym in symbols}

    custom_results: dict[str, dict] = {}
    if mode in {"CUSTOM", "ALL"}:
        custom_engine = YamlStrategyEngine(float(os.getenv("MIN_BUY_SIGNAL_SCORE", "70")))
        for symbol in symbols:
            bars = bars_by_symbol.get(symbol)
            if bars is None:
                custom_results[symbol] = {
                    "action": "ERROR", "strength": 0,
                    "reason": f"daily bars {bar_status_by_symbol.get(symbol, 'UNAVAILABLE').lower()}",
                    "data_ready": False,
                }
            else:
                custom_results[symbol] = custom_engine.evaluate(entry_path, bars, symbol=symbol, purpose="entry")

    picks: list[Candidate] = []
    for symbol in symbols:
        kr = kis_results.get(symbol, {}) if mode in {"KIS10", "ALL"} else {}
        cr = custom_results.get(symbol, {}) if mode in {"CUSTOM", "ALL"} else {}
        kis_action = kr.get("action", "ERROR") if mode in {"KIS10", "ALL"} else "NOT_USED"
        custom_action = cr.get("action", "ERROR") if mode in {"CUSTOM", "ALL"} else "NOT_USED"
        from trading.strategy_decision import combine_buy
        if mode == "KIS10":
            accepted = combine_buy(mode, kis_action, custom_action)
            score = float(kr.get("score", 0) or 0)
        elif mode == "CUSTOM":
            accepted = combine_buy(mode, kis_action, custom_action) and bool(cr.get("data_ready", False)) and bool(cr.get("buy_allowed", False))
            score = float(cr.get("strength", 0) or 0)
        else:
            # 명시된 ALL 매수 규칙: 두 엔진 모두 BUY여야 승인합니다.
            accepted = combine_buy(mode, kis_action, custom_action) and bool(cr.get("data_ready", False)) and bool(cr.get("buy_allowed", False))
            score = min(float(kr.get("score", 0) or 0), float(cr.get("strength", 0) or 0))
        log.info("[STRATEGY_COMBINED_DECISION] symbol=%s mode=%s kis10=%s custom=%s accepted_buy=%s score=%.2f kis_reason=%s custom_reason=%s",
                 symbol, mode, kis_action, custom_action, accepted, score,
                 kr.get("reason", "not evaluated"), cr.get("reason", "not evaluated"))
        if not accepted:
            continue
        with price_cache_lock:
            quote = dict(price_cache.get(symbol, {}))
        if not quote or time.time() - float(quote.get("_received_ts", 0) or 0) > quote_stale_sec:
            try:
                quote = dict(api.current_price(symbol) or {})
                quote["_received_ts"] = time.time()
                with price_cache_lock: price_cache[symbol] = dict(quote)
            except Exception:
                log.exception("[ENTRY_EVALUATION] 최종 후보 현재가 조회 실패: %s", symbol_label(symbol))
                continue
        if price_value(quote) <= 0:
            log.warning("[ENTRY_EVALUATION] 현재가가 없어 후보 제외: %s", symbol_label(symbol))
            continue
        change_pct = _daily_change_pct(quote)
        if change_pct is None:
            # WebSocket payload may not yet contain the previous-day change field.
            # Fetch one REST quote only for an otherwise valid BUY candidate.
            try:
                fresh_quote = dict(api.current_price(symbol) or {})
                if price_value(fresh_quote) > 0:
                    fresh_quote["_received_ts"] = time.time()
                    quote = fresh_quote
                    with price_cache_lock:
                        price_cache[symbol] = dict(quote)
                change_pct = _daily_change_pct(quote)
            except Exception as exc:
                log.warning("[ENTRY_DAILY_CHANGE_FILTER] symbol=%s status=BLOCKED reason=quote_error error_type=%s", symbol, type(exc).__name__)
                continue
        if change_pct is None:
            log.warning("[ENTRY_DAILY_CHANGE_FILTER] symbol=%s status=BLOCKED reason=change_pct_missing", symbol)
            continue
        max_change = max_daily_change
        min_change = min_daily_change
        if change_pct >= max_change or change_pct <= min_change:
            log.info("[ENTRY_DAILY_CHANGE_FILTER] symbol=%s status=EXCLUDED change_pct=%.3f upper=%.3f lower=%.3f",
                     symbol, change_pct, max_change, min_change)
            continue
        log.info("[ENTRY_DAILY_CHANGE_FILTER] symbol=%s status=PASSED change_pct=%.3f upper=%.3f lower=%.3f",
                 symbol, change_pct, max_change, min_change)
        name = str(quote.get("hts_kor_isnm", quote.get("prdt_name", symbol)))
        register_symbol(symbol, name)
        reason = f"mode={mode}; KIS10={kis_action}; CUSTOM={custom_action}; KIS10_reason={kr.get('reason','')}; CUSTOM_reason={cr.get('reason','')}"
        picks.append(Candidate(symbol=symbol, name=name, hts={}, price=quote, score=score, reason=reason))
    picks.sort(key=lambda item: item.score, reverse=True)
    if picks:
        log.info("[ENTRY_EVALUATION_PICK] mode=%s symbol=%s score=%.2f reason=%s", mode, symbol_label(picks[0].symbol, picks[0].name), picks[0].score, picks[0].reason)
        return picks[0]
    log.info("[ENTRY_EVALUATION_PICK] BUY 후보 없음 mode=%s symbols=%d", mode, len(symbols))
    return None


def _position_strategy_risk_exit(pos: dict, current_price: float) -> tuple[bool, str]:
    """레거시 포지션별 위험값은 더 이상 별도 청산을 발생시키지 않습니다.

    V7.1.0 청산은 1차 손익률+선택 엔진 SELL과 2차 강제 손익률로 통일합니다.
    이전 버전 상태의 strategy_risk는 호환을 위해 보존하지만 판정에는 사용하지 않습니다.
    """
    return False, ""


def _load_profile_risk(profile_name: str) -> dict:
    """내부 전략 프로필 매수에 해당 전략 YAML 위험 설정을 저장합니다."""
    try:
        return OfficialBuilderAdapter(str(resolve_path(os.getenv("STRATEGY_PROFILES_FILE", "config/strategy_profiles.yaml")))).get_profile_risk(profile_name)
    except Exception as exc:
        log.error("프로필 위험 설정을 읽지 못했습니다: profile=%s error=%s: %s", profile_name, type(exc).__name__, exc)
        return {}


def _apply_fill(store: StateStore, engine: StrategyEngine, fill: FillEvent) -> None:
    """호환 래퍼: 실제 체결 반영 로직은 trading.fill_handler에 위임합니다."""
    from trading.fill_handler import apply_fill_state

    apply_fill_state(
        store, engine, fill,
        load_profile_risk=_load_profile_risk,
        env_float=_env_float,
        symbol_name=symbol_name,
        symbol_label=symbol_label,
    )


def _summary_value(summary: dict, *keys: str) -> float:
    """KIS 잔고 요약의 여러 필드명을 숫자로 정규화합니다."""
    for key in keys:
        try:
            value = summary.get(key)
            if value not in (None, ""):
                return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return 0.0


def log_portfolio_snapshot(stage: str, holdings: list[dict], summary: dict) -> None:
    """프로그램 시작/종료 시 총자산과 보유종목별 평가 정보를 남깁니다."""
    total_eval = _summary_value(summary, "tot_evlu_amt", "TOT_EVLU_AMT", "nass_amt", "NASS_AMT")
    purchase_total = _summary_value(summary, "pchs_amt_smtl_amt", "PCHS_AMT_SMTL_AMT", "pchs_amt", "PCHS_AMT")
    deposit = _summary_value(summary, "dnca_tot_amt", "DNCA_TOT_AMT")
    orderable = _summary_value(summary, "ord_psbl_cash", "ORD_PSBL_CASH")
    pnl_total = _summary_value(summary, "evlu_pfls_smtl_amt", "EVLU_PFLS_SMTL_AMT")
    summary_line = (f"[{stage} 보유주식 요약] 총 평가금액={total_eval:,.0f}원 매입금액={purchase_total:,.0f}원 "
                    f"평가손익={pnl_total:,.0f}원 예수금={deposit:,.0f}원 주문가능금액={orderable:,.0f}원")
    raw_summary_fields = {key: summary.get(key) for key in (
        "tot_evlu_amt", "TOT_EVLU_AMT", "nass_amt", "NASS_AMT", "tot_asst_amt", "TOT_ASST_AMT",
        "dnca_tot_amt", "DNCA_TOT_AMT", "ord_psbl_cash", "ORD_PSBL_CASH",
        "pchs_amt_smtl_amt", "PCHS_AMT_SMTL_AMT", "evlu_pfls_smtl_amt", "EVLU_PFLS_SMTL_AMT") if key in summary}
    log.info("PORTFOLIO_AMOUNT_AUDIT stage=%s raw_summary=%s computed_total_eval=%.0f purchase_total=%.0f pnl_total=%.0f deposit=%.0f orderable=%.0f",
             stage, raw_summary_fields, total_eval, purchase_total, pnl_total, deposit, orderable)
    log.info(summary_line)
    order_log.info(summary_line)
    fill_log.info(summary_line)
    valid = []
    for row in holdings or []:
        symbol = str(row.get("pdno", row.get("PDNO", row.get("mksc_shrn_iscd", ""))) or "").strip().zfill(6)
        qty = int(_summary_value(row, "hldg_qty", "HLDG_QTY"))
        if not symbol or symbol == "000000" or qty <= 0:
            continue
        name = str(row.get("prdt_name", row.get("PRDT_NAME", "")) or "").strip()
        register_symbol(symbol, name)
        pnl = _summary_value(row, "evlu_pfls_amt", "EVLU_PFLS_AMT")
        pnl_pct = _summary_value(row, "evlu_pfls_rt", "EVLU_PFLS_RT")
        eval_amt = _summary_value(row, "evlu_amt", "EVLU_AMT")
        current = _summary_value(row, "prpr", "STCK_PRPR")
        avg = _summary_value(row, "pchs_avg_pric", "PCHS_AVG_PRIC")
        valid.append(f"{symbol_label(symbol, name)} 수량={qty}주 평가손익={pnl:,.0f}원 수익률={pnl_pct:.2f}% 평가금액={eval_amt:,.0f}원 현재가={current:,.0f}원 매입단가={avg:,.0f}원")
    # 종목별로 별도 로그 레코드를 남겨 로그 뷰어에서 한 종목씩 확인할 수 있게 합니다.
    if not valid:
        valid = ["없음"]
    for position_line in valid:
        message = f"[{stage} 보유종목] {position_line}"
        log.info(message)
        order_log.info(message)
        fill_log.info(message)


def main() -> None:
    """자동매매 프로그램을 실행합니다."""
    _configure_logging()
    settings = load_settings()
    missing = [
        name for name, value in {
            "KIS_APP_KEY": settings.app_key,
            "KIS_APP_SECRET": settings.app_secret,
            "KIS_HTS_ID": settings.hts_id,
            "KIS_CANO": settings.cano,
        }.items() if not value
    ]
    if missing:
        raise SystemExit(".env 필수값 누락: " + ", ".join(missing))

    api = KisApi(settings)
    try:
        store = StateStore(settings.state_file)
    except StateRecoveryError as exc:
        # 상태 파일이 손상된 채 빈 상태로 매매를 시작하면 재진입/일일 한도 보호가 유실될 수 있습니다.
        # 실제 운영 파일을 자동 덮어쓰지 않고, 운영자가 원본을 확인·복구할 때까지 시작을 중단합니다.
        log.critical("상태 파일 복구 필요로 자동매매 시작 중단: %s", exc)
        raise SystemExit(2) from exc
    engine = StrategyEngine(settings)
    # 매수는 지정가 체결 대기 여유를 주기 위해 기본 120초, 매도는 기본 60초입니다.
    # 기존 ORDER_TIMEOUT_SEC는 새 BUY/SELL 값이 없을 때의 하위 호환 기준값입니다.
    orders = OrderManager(
        api,
        settings.order_timeout_sec,
        buy_timeout_sec=settings.buy_order_timeout_sec,
        sell_timeout_sec=settings.sell_order_timeout_sec,
    )
    log.info("미체결 타임아웃 설정: BUY=%d초 SELL=%d초 legacy=%d초",
             orders.buy_timeout_sec, orders.sell_timeout_sec, orders.timeout_sec)
    if orders.uncertain_symbols:
        log.critical("이전 실행의 주문 접수 결과 불명확 종목이 잠겨 있습니다: %s; KIS HTS 주문/체결/잔고를 확인한 뒤에만 잠금을 해제하십시오.",
                     [symbol_label(item) for item in sorted(orders.uncertain_symbols)])
    yaml_engine = YamlStrategyEngine(float(os.getenv("MIN_BUY_SIGNAL_SCORE", "70")))
    telegram = TelegramNotifier()
    holiday_checker = HolidayChecker(api, str(runtime_path("holiday_cache")))

    price_cache: dict = {}
    price_cache_lock = threading.RLock()
    last_rest_quote: dict = {}
    stop_flag = threading.Event()
    # 실시간 청산 이벤트는 손절(0)을 익절(1)보다 먼저 처리합니다.
    exit_queue: queue.PriorityQueue = queue.PriorityQueue()
    exit_queue_lock = threading.RLock()
    exit_queued_symbols: set[str] = set()
    exit_submission_guard = SymbolSubmissionGuard()
    fill_apply_lock = threading.RLock()
    # WebSocket callback에서는 포지션 상태 변경/구독 API 호출을 하지 않고 이벤트만 전달합니다.
    fill_event_queue: queue.Queue[FillEvent | None] = queue.Queue()

    def log_post_fill_positions(fallback_symbol: str = "", fallback_price: float = 0.0) -> None:
        """체결 상태 반영 직후 로컬 보유수량과 캐시 시세 기준 보유종목을 한 줄씩 기록합니다.

        체결 콜백에서 REST 잔고를 추가 호출하지 않습니다. 현재가 캐시가 없으면 해당
        체결 종목에 한해 체결단가를 사용하며, 그 외 종목은 현재가를 0으로 표시합니다.
        """
        symbols = sorted(store.symbols())
        if not symbols:
            message = "[실제체결 후 보유종목] 없음"
            log.info(message)
            order_log.info(message)
            fill_log.info(message)
            return
        with price_cache_lock:
            quotes = {symbol: dict(price_cache.get(symbol, {})) for symbol in symbols}
        for symbol in symbols:
            pos = store.get_position(symbol) or {}
            qty = int(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue
            name = str(pos.get("name", "") or "").strip()
            label = symbol_label(symbol, name)
            avg = float(pos.get("avg_price", 0) or 0)
            current = price_value(quotes.get(symbol, {})) if quotes.get(symbol) else 0.0
            if current <= 0 and symbol == fallback_symbol and fallback_price > 0:
                current = float(fallback_price)
            pnl = (current - avg) * qty if current > 0 and avg > 0 else 0.0
            pnl_pct = (current / avg - 1.0) * 100.0 if current > 0 and avg > 0 else 0.0
            eval_amt = current * qty if current > 0 else 0.0
            message = (f"[실제체결 후 보유종목] {label} 수량={qty}주 평가손익={pnl:,.0f}원 "
                       f"수익률={pnl_pct:.2f}% 평가금액={eval_amt:,.0f}원 현재가={current:,.0f}원 "
                       f"매입단가={avg:,.0f}원")
            log.info(message)
            order_log.info(message)
            fill_log.info(message)

    def on_price(data: dict):
        """WebSocket 콜백은 캐시 갱신과 가벼운 위험 판정/큐 등록만 수행합니다.

        콜백 안에서 REST를 호출하거나 주문을 직접 제출하지 않아 수신 루프를 막지 않습니다.
        """
        symbol = str(data.get("MKSC_SHRN_ISCD", "")).strip().zfill(6)
        if not symbol or symbol == "000000":
            return
        value = dict(data)
        value["_received_ts"] = time.time()
        with price_cache_lock:
            price_cache[symbol] = value
        pos = store.get_position(symbol)
        if not pos or int(pos.get("qty", 0) or 0) <= 0 or orders.pending_for_symbol(symbol):
            return
        current = price_value(value)
        avg_price = float(pos.get("avg_price", 0) or 0)
        if current <= 0 or avg_price <= 0:
            return
        if current > float(pos.get("high_price", 0) or 0):
            pos["high_price"] = current
            store.set_position(symbol, pos)
        pnl_pct = (current / avg_price - 1.0) * 100.0
        should_exit, reason = _position_strategy_risk_exit(pos, current)
        if not should_exit:
            should_exit, reason = _evaluate_exit_priority(
                pnl_pct, bool(pos.get("yaml_exit_signal", False)),
                stop_loss_hard_pct=_env_float("EXIT_STOP_LOSS_HARD_PCT", 2.0),
                stop_loss_first_pct=_env_float("EXIT_STOP_LOSS_FIRST_PCT", _env_float("EXIT_STOP_LOSS_YAML_PCT", 1.5)),
                take_profit_first_pct=_env_float("EXIT_TAKE_PROFIT_FIRST_PCT", _env_float("EXIT_TAKE_PROFIT_YAML_PCT", 1.5)),
                take_profit_hard_pct=_env_float("EXIT_TAKE_PROFIT_HARD_PCT", 3.0),
            )
        if not should_exit:
            return
        priority = 0 if _is_stop_reason(reason) else 1
        with exit_queue_lock:
            if symbol in exit_queued_symbols:
                return
            exit_queued_symbols.add(symbol)
            exit_queue.put((priority, time.monotonic(), symbol))


    def on_notice(data: dict):
        """WebSocket 콜백에서는 이벤트만 큐에 넣고 전용 worker가 즉시 상태를 반영합니다."""
        fill = orders.on_notice(data)
        if fill:
            fill_event_queue.put(fill)

    realtime = KisRealtime(api, on_price, on_notice)
    fill_handler = FillHandler(
        store=store, engine=engine, orders=orders, realtime=realtime,
        telegram=telegram, settings=settings, fill_apply_lock=fill_apply_lock,
        log_post_fill_positions=log_post_fill_positions,
        fill_may_already_be_in_balance=_fill_may_already_be_in_balance,
        apply_fill_callback=_apply_fill, register_symbol=register_symbol,
        symbol_label=symbol_label,
    )

    def fill_event_worker() -> None:
        """WebSocket 체결통보를 main loop의 REST 대기와 분리해 즉시 반영합니다."""
        while True:
            event = fill_event_queue.get()
            try:
                if event is None:
                    return
                fill_handler.apply_fill_event(event)
            except Exception:
                log.exception("전용 체결 처리 worker 오류; 주문/잔고 REST 대사로 후속 확인합니다.")
            finally:
                fill_event_queue.task_done()

    fill_worker_thread = threading.Thread(target=fill_event_worker, name="fill-event-worker", daemon=True)
    fill_worker_thread.start()

    # 신규 매수는 계좌 잔고와 미체결 주문을 모두 최근 정상 조회한 경우에만 허용합니다.
    # 매수 차단은 매도 신호 평가/매도 주문 경로와 분리되어, 보유 종목의 청산 관리는 계속됩니다.
    buy_safety = {
        "balance_ok": False,
        "orders_ok": False,
        "reason": "시작 시 계좌/미체결 대사 대기",
    }
    # 같은 보유 종목/매도 유형의 반복 DRY-RUN 신호는 60초에 한 번만 기록합니다.
    # 신호 평가 자체는 매 루프 계속 수행하므로 로그 억제가 매도 판단을 늦추지 않습니다.
    _sell_signal_log_times: dict[tuple[str, str, bool], float] = {}
    _sell_signal_log_lock = threading.Lock()

    def log_dry_run_sell_signal(symbol: str, reason: str, *, realtime: bool = False) -> None:
        """반복되는 동일 유형의 DRY-RUN 매도 로그만 제한하고 매도 조건 평가는 유지합니다."""
        category = str(reason).split()[0] if reason else "EXIT"
        key = (str(symbol), category, realtime)
        now_mono = time.monotonic()
        with _sell_signal_log_lock:
            previous = _sell_signal_log_times.get(key, 0.0)
            if now_mono - previous < 60.0:
                return
            _sell_signal_log_times[key] = now_mono
        label = symbol_label(symbol, (store.get_position(symbol) or {}).get("name", ""))
        if realtime:
            log.warning("[DRY-RUN] 실시간 SELL signal %s reason=%s (동일 유형은 60초 간격으로 기록)", label, reason)
        else:
            log.warning("[DRY-RUN] SELL signal %s reason=%s (동일 유형은 60초 간격으로 기록)", label, reason)

    def update_buy_safety(*, balance_ok=None, orders_ok=None, reason=None) -> None:
        """조회 성공/실패 상태를 갱신하고 신규 매수 허용 여부를 fail-closed로 관리합니다."""
        if balance_ok is not None:
            buy_safety["balance_ok"] = bool(balance_ok)
        if orders_ok is not None:
            buy_safety["orders_ok"] = bool(orders_ok)
        if reason:
            buy_safety["reason"] = str(reason)
        elif buy_safety["balance_ok"] and buy_safety["orders_ok"]:
            buy_safety["reason"] = ""
        if not (buy_safety["balance_ok"] and buy_safety["orders_ok"]):
            log.warning(
                "신규 매수 안전 게이트 차단: balance_ok=%s orders_ok=%s reason=%s",
                buy_safety["balance_ok"], buy_safety["orders_ok"],
                buy_safety["reason"] or "계좌/미체결 대사 미완료",
            )

    def subscribe_held_positions() -> None:
        """로컬/브로커 대사로 복구된 모든 보유 종목을 실시간 구독 목록에 유지합니다."""
        sync_held_position_subscriptions(store, realtime, log)

    def reconcile_and_subscribe():
        """잔고 대사 후 보유 종목을 구독합니다. 실패 시 신규 매수를 차단하고 기존 보유 구독은 유지합니다."""
        try:
            result = reconcile(api, store, orders, state_lock=fill_apply_lock)
            orderable_cash = _summary_value(result.get("summary", {}), "ord_psbl_cash", "ORD_PSBL_CASH")
            if orderable_cash > 0:
                update_buy_safety(balance_ok=True)
            else:
                update_buy_safety(balance_ok=False, reason="KIS 주문가능금액(ord_psbl_cash) 누락 또는 0원")
                log.error("신규 매수 차단: 잔고 API는 응답했지만 주문가능금액을 확인할 수 없습니다. orderable_cash=%.0f", orderable_cash)
            return result
        except Exception as exc:
            update_buy_safety(balance_ok=False, reason=f"계좌 잔고 조회 실패: {type(exc).__name__}")
            raise
        finally:
            subscribe_held_positions()

    def event_exit_worker() -> None:
        """호환용 작업자 진입점; 실시간 매도 흐름은 SellProcess가 소유합니다."""
        sell_process.event_exit_worker()

    def request_stop(signum, _frame):
        """SIGINT/SIGTERM을 graceful shutdown으로 변환합니다."""
        log.info("종료 신호 수신: %s", signum)
        stop_flag.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    today_key = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    store.reset_daily_if_needed(today_key)
    log.info("KIS V7.1.6 시작: paper=%s trading=%s entry_mode=%s source=%s cwd=%s",
             settings.paper, settings.enable_trading, os.getenv("ENTRY_MODE", "HTS").upper(),
             Path(__file__).resolve(), Path.cwd())
    log.info("START_OUTCOME status=initialized source=%s project_root=%s paper=%s trading=%s",
             Path(__file__).resolve(), Path(__file__).resolve().parent,
             settings.paper, settings.enable_trading)
    log.info("주문 모드 설정: buy=%s sell=%s stop_loss=%s; rate_limit_cooldown=%.1fs",
             os.getenv("KIS_BUY_ORDER_MODE", "AUTO"), os.getenv("KIS_SEL_ORDER_MODE", "BEST_LIMIT"),
             os.getenv("KIS_STOP_LOSS_ORDER_MODE", "BEST_LIMIT"),
             max(1.0, _env_float("KIS_RATE_LIMIT_COOLDOWN_SEC", 10.0)))
    limiter_stats = api.limiter.stats()
    log.info("전략 평가 엔진: mode=%s (KIS10/CUSTOM/ALL)", os.getenv("STRATEGY_EVALUATION_MODE", "ALL").strip().upper())
    log.info("운영 설정 스냅샷: MAX_POSITIONS=%s POSITION_SIZING=%s BUY_TIMEOUT=%ds SELL_TIMEOUT=%ds SHUTDOWN_CANCEL_CONFIRM=%ss DAILY_BAR_TTL=%ss",
             os.getenv("MAX_POSITIONS", "3"), os.getenv("POSITION_SIZING", "true"),
             settings.buy_order_timeout_sec, settings.sell_order_timeout_sec,
             max(180, int(_env_float("SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC", 180))),
             max(300, int(_env_float("DAILY_BAR_CACHE_TTL_SEC", 300))))
    log.info("신규 매수 허용 시간(KST): %s~%s; AUTO_SHUTDOWN=%s (둘 중 빠른 시각에 주문 POST 차단)",
             os.getenv("ENTRY_START_TIME", "09:10"), os.getenv("ENTRY_END_TIME", "15:10"),
             os.getenv("AUTO_SHUTDOWN_TIME", "15:10"))
    log.info("Builder 진단 전략 전체 관찰: %s", os.getenv("BUILDER_OBSERVE_ALL_STRATEGIES", "false").strip().lower())
    # HTS 조건검색은 후보 출처입니다. 평가 엔진 선택과 별개로 단일 조건명을 사용합니다.
    _condition_name = str(settings.condition_name or "").strip()
    _entry_mode = os.getenv("ENTRY_MODE", "HTS").strip().upper()
    if _entry_mode in {"HTS", "ALL"}:
        if not _condition_name:
            log.error("KIS_CONDITION_NAME이 비어 있습니다. HTS 후보 조회를 생략합니다.")
    log.info("REST 안전상한(프로그램 내부): 최대 %d회/초 이하, 최소 간격=%.2f초, %d회/분; 설정 원값=%.2f회/초 %d회/분",
             limiter_stats["configured_sec_limit"], api.limiter.min_interval,
             limiter_stats["configured_min_limit"], settings.max_calls_per_sec, settings.max_calls_per_min)
    log.info("TR별 추가 호출 제한기 활성: %d개 TR (%s)", len(api.tr_limiters), ",".join(sorted(api.tr_limiters)))
    telegram.send(f"KIS V7.1.6 시작\npaper={settings.paper}\ntrading={settings.enable_trading}")
    # 휴장일 API 오류는 영업일로 추정하지 않고 시작을 중단합니다.
    try:
        if not holiday_checker.is_business_day(datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")):
            log.warning("오늘은 휴장일입니다. 프로그램을 종료합니다.")
            telegram.send("KIS V7.1.6: 오늘은 휴장일로 확인되어 종료합니다.")
            return
    except Exception:
        log.exception("휴장일 확인 실패: 안전을 위해 프로그램 시작 중단")
        telegram.send("KIS V7.1.6 오류: 휴장일 확인 실패로 안전 종료합니다.")
        raise

    # 이전 버전에서 체결 카운터가 누락된 상태로 재시작할 수 있으므로
    # KIS 당일 체결이력에서 고유 매수 주문 수를 복구합니다. 조회 실패 시 fail-closed.
    daily_buy_history_ok = _recover_daily_buy_trade_count(api, store, settings)
    # 재시작 시 이력 조회에 실패했거나 복구된 카운터가 한도에 도달하면
    # 당일 신규 매수 차단을 메모리 latch로도 유지합니다. 다른 코드 경로에서
    # 카운터가 잘못 초기화되더라도 같은 날 안전 차단이 풀리지 않게 합니다.
    daily_buy_limit_date = today_key
    daily_buy_limit_latched = _should_latch_daily_buy_limit(
        daily_buy_history_ok, store.state.trades_today, settings.max_trades_per_day
    )
    if daily_buy_limit_latched:
        log.warning(
            "당일 신규 매수 잠금 활성화: date=%s trades_today=%d limit=%d history_ok=%s",
            daily_buy_limit_date, store.state.trades_today,
            settings.max_trades_per_day, daily_buy_history_ok,
        )

    sell_process = SellProcess(
        store=store, orders=orders, settings=settings, stop_event=stop_flag,
        exit_queue=exit_queue, price_cache=price_cache, price_cache_lock=price_cache_lock,
        exit_queue_lock=exit_queue_lock, exit_queued_symbols=exit_queued_symbols,
        submission_guard=exit_submission_guard, telegram=telegram,
        dry_run_logger=log_dry_run_sell_signal, env_float=_env_float,
        price_value=price_value, position_strategy_risk_exit=_position_strategy_risk_exit,
        evaluate_exit_priority=_evaluate_exit_priority, sell_order_window=_sell_order_window,
        is_stop_reason=_is_stop_reason, resolve_order_mode=_resolve_order_mode,
        sell_retry_delay=_sell_retry_delay, symbol_label=symbol_label,
    )
    exit_worker_thread = threading.Thread(target=event_exit_worker, name="event-exit-worker", daemon=True)
    exit_worker_thread.start()

    buy_process = BuyProcess(
        api=api, store=store, engine=engine, orders=orders, realtime=realtime,
        price_cache=price_cache, price_cache_lock=price_cache_lock,
        last_rest_quote=last_rest_quote, settings=settings,
        run_hts_cycle=run_cycle, run_builder_cycle=run_builder_cycle,
    )

    # WebSocket 연결 전에 초기 broker 대사를 완료하고 desired_symbols를 채웁니다.
    # 그러면 최초 _subscribe_all()부터 기존 보유 종목이 포함되어 구독 누락/중복 등록을 줄입니다.
    try:
        _initial_snapshot = reconcile_and_subscribe()
        log_portfolio_snapshot("프로그램 시작", _initial_snapshot.get("holdings_rows", []), _initial_snapshot.get("summary", {}))
    except Exception:
        # 초기 reconcile 실패 시 임의로 local pending을 지우지 않습니다.
        # 잔고 조회만 가능할 경우 시작 스냅샷은 별도로 남기되, 신규 매수 안전 게이트는 닫아 둡니다.
        log.exception("초기 broker reconcile 오류")
        try:
            _fallback_holdings, _fallback_summary = api.balance()
            log_portfolio_snapshot("프로그램 시작(주문대사 실패)", _fallback_holdings, _fallback_summary)
        except Exception:
            log.exception("초기 보유주식 스냅샷 조회도 실패")
    realtime.start_background()
    # 최초 연결은 위 초기 대사로 처리했습니다. 이후 generation 변경은 재연결로 간주합니다.
    last_ws_generation = realtime.connection_generation

    last_scan = 0.0
    # 매수 후 대기시간은 메인 루프 전체를 sleep하지 않고 신규 매수만 지연합니다.
    # 그래야 그동안 매도 신호, 잔고/주문 대사, 종료 처리가 계속 실행됩니다.
    buy_pause_until = 0.0
    last_balance_reconcile = time.time()
    last_order_reconcile = time.time()
    last_uncertain_reconcile = 0.0
    shutdown_done = False
    active_style = "unified"
    yaml_exit_workers = max(1, min(2, int(_env_float("EXIT_YAML_EVAL_WORKERS", 2))))
    yaml_exit_executor = ThreadPoolExecutor(max_workers=yaml_exit_workers, thread_name_prefix="exit-yaml")
    yaml_exit_futures = {}

    def fetch_yaml_exit(symbol: str, strategy_path: str, profile_name: str):
        """선택한 평가 엔진으로 SELL을 평가합니다. ALL은 어느 한 엔진의 SELL이면 신호입니다."""
        mode = os.getenv("STRATEGY_EVALUATION_MODE", "ALL").strip().upper()
        results = []
        bars = None
        data_status = "API_ERROR"
        required_bars = 0
        if mode in {"KIS10", "CUSTOM", "ALL"}:
            try:
                # KIS10과 CUSTOM은 동일 종목/기간의 일봉 snapshot을 공유합니다.
                entry_path = str(resolve_path(os.getenv("ENTRY_STRATEGY_FILE", "config/my_entry_strategy.kis.yaml")))
                required_bars = required_daily_bars(mode, entry_path, strategy_path)
                bars = get_cached_daily_prices(api, symbol, days=required_bars)
                if len(bars) < required_bars:
                    data_status = "INSUFFICIENT_HISTORY"
                    log.warning("[SELL_ENGINE_DATA] symbol=%s status=INSUFFICIENT_HISTORY returned_bars=%d required_bars=%d",
                                symbol, len(bars), required_bars)
                    bars = None
                else:
                    data_status = "OK"
            except Exception as exc:
                data_status = "API_ERROR"
                log.warning("[SELL_ENGINE_DATA] symbol=%s status=API_ERROR error_type=%s", symbol, type(exc).__name__)
        if mode in {"KIS10", "ALL"}:
            try:
                observe_all = os.getenv("BUILDER_OBSERVE_ALL_STRATEGIES", "false").strip().lower() in {"1", "true", "yes", "on"}
                result = OfficialBuilderAdapter(str(resolve_path(os.getenv("STRATEGY_PROFILES_FILE", "config/strategy_profiles.yaml")))).evaluate(
                    "unified", [symbol], observe_all=observe_all, api=api,
                    bars_by_symbol={symbol: bars}, data_status_by_symbol={symbol: data_status},
                ).get(symbol, {})
                results.append(("KIS10", result))
            except Exception as exc:
                log.warning("[SELL_ENGINE] KIS10 평가 실패 symbol=%s error_type=%s", symbol, type(exc).__name__)
                results.append(("KIS10", {"action": "ERROR", "data_ready": False, "reason": str(exc)}))
        if mode in {"CUSTOM", "ALL"}:
            try:
                if bars is None:
                    raise RuntimeError("일봉 과거 데이터 부족" if data_status == "INSUFFICIENT_HISTORY" else "KIS 일봉 API 수집 실패")
                custom = yaml_engine.evaluate(strategy_path, bars, symbol=symbol, purpose="exit")
                results.append(("CUSTOM", custom))
            except Exception as exc:
                log.warning("[SELL_ENGINE] CUSTOM 평가 실패 symbol=%s error_type=%s", symbol, type(exc).__name__)
                results.append(("CUSTOM", {"action": "ERROR", "data_ready": False, "reason": str(exc)}))
        if mode not in {"KIS10", "CUSTOM", "ALL"}:
            return {"action": "ERROR", "strength": 0, "reason": f"invalid STRATEGY_EVALUATION_MODE={mode}", "data_ready": False}
        ready = [(name, result) for name, result in results if result.get("data_ready", result.get("action") in {"BUY", "SELL", "HOLD"})]
        # ALL 매도 규칙은 OR: 하나라도 SELL이면 통합 SELL. 평가가 전부 실패하면 fail-closed.
        from trading.strategy_decision import combine_sell
        kis_action = next((r.get("action", "ERROR") for n, r in results if n == "KIS10"), "NOT_USED")
        custom_action = next((r.get("action", "ERROR") for n, r in results if n == "CUSTOM"), "NOT_USED")
        sell_results = [(name, result) for name, result in ready if result.get("action") == "SELL"]
        if combine_sell(mode, kis_action, custom_action) and sell_results:
            return {"action": "SELL", "strength": max(float(r.get("strength", r.get("sell_score", 0)) or 0) for _, r in sell_results),
                    "reason": " | ".join(f"{n}: {r.get('reason','SELL')}" for n, r in sell_results), "data_ready": True}
        if ready:
            return {"action": "HOLD", "strength": 0, "reason": " | ".join(f"{n}: {r.get('reason','HOLD')}" for n, r in ready), "data_ready": True}
        return {"action": "ERROR", "strength": 0, "reason": "모든 선택 평가 엔진의 SELL 평가 실패", "data_ready": False}

    try:
        while not stop_flag.is_set():
            # 체결통보는 전용 worker가 처리합니다. main loop가 API 제한 대기 중이어도
            # WebSocket 체결 이벤트의 내부 포지션 반영이 지연되지 않도록 분리합니다.
            for deferred_fill in orders.drain_deferred_fill_events():
                fill_event_queue.put(deferred_fill)
            now = time.time()
            # WebSocket 재연결은 연결 성공만으로 체결 누락 복구가 보장되지 않으므로
            # generation 변경을 감지하면 주문/체결/잔고 REST 대사를 우선 수행합니다.
            if realtime.connection_generation != last_ws_generation:
                previous_generation = last_ws_generation
                last_ws_generation = realtime.connection_generation
                if previous_generation == 0 and last_ws_generation == 1:
                    # 최초 연결 직전 시작 대사를 이미 수행했으므로 중복 계좌 조회를 생략합니다.
                    log.info("WebSocket 최초 연결/구독 완료 generation=%d; 시작 대사 결과 사용", last_ws_generation)
                else:
                    try:
                        reconcile_and_subscribe()
                        last_balance_reconcile = time.time()
                        last_order_reconcile = last_balance_reconcile
                        log.warning("WebSocket 재연결 후 REST 주문/체결/잔고 대사 완료 generation=%d", last_ws_generation)
                    except Exception as reconnect_exc:
                        update_buy_safety(balance_ok=False, orders_ok=False, reason=f"WebSocket 재연결 후 대사 실패: {type(reconnect_exc).__name__}")
                        log.exception("WebSocket 재연결 후 REST 대사 실패; 신규 매수 차단 유지")
            # 완료된 YAML exit 평가 결과는 main thread에서만 포지션 상태에 반영합니다.
            for _symbol, _future in list(yaml_exit_futures.items()):
                if not _future.done():
                    continue
                yaml_exit_futures.pop(_symbol, None)
                try:
                    _result = _future.result()
                    _pos = store.get_position(_symbol)
                    if _pos:
                        if _result.get("data_ready"):
                            _pos["yaml_exit_signal"] = _result.get("action") == "SELL"
                            _pos["yaml_exit_reason"] = str(_result.get("reason", "SELL"))
                        else:
                            # 평가가 실패하면 이전 주기의 SELL을 무기한 재사용하지 않습니다.
                            # YAML 위험 기준과 글로벌 하드 손절/익절은 별도로 계속 적용됩니다.
                            _pos["yaml_exit_signal"] = False
                            _pos["yaml_exit_reason"] = str(_result.get("reason", "Strategy Builder 평가 불가"))
                        store.set_position(_symbol, _pos)
                    log.info("[YAML EXIT] %s action=%s strength=%.2f reason=%s",
                             symbol_label(_symbol, (store.get_position(_symbol) or {}).get("name", "")), _result.get("action"), _result.get("strength", 0), _result.get("reason"))
                except Exception:
                    log.exception("YAML 매도 전략 worker 평가 오류: %s", symbol_label(_symbol, (store.get_position(_symbol) or {}).get("name", "")))
            now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
            current_day_key = now_kst.strftime("%Y-%m-%d")
            store.reset_daily_if_needed(current_day_key)
            if current_day_key != daily_buy_limit_date:
                # 날짜가 바뀌면 카운터를 초기화한 뒤 당일 브로커 이력을 다시 확인합니다.
                # 이력 조회가 실패하면 새 날짜에서도 fail-closed 잠금을 유지합니다.
                daily_buy_limit_date = current_day_key
                daily_buy_history_ok = _recover_daily_buy_trade_count(api, store, settings)
                daily_buy_limit_latched = _should_latch_daily_buy_limit(
                    daily_buy_history_ok, store.state.trades_today, settings.max_trades_per_day
                )
                log.info("KST 날짜 변경 후 일일 매수 이력 재검증: date=%s trades_today=%d limit=%d history_ok=%s latch=%s",
                         current_day_key, store.state.trades_today, settings.max_trades_per_day,
                         daily_buy_history_ok, daily_buy_limit_latched)

            # 종료 시각에는 신규/청산 신호 생성을 멈추고 미체결 주문을 재조회·취소·확인합니다.
            shutdown_at = dtime.fromisoformat(os.getenv("AUTO_SHUTDOWN_TIME", "15:10"))
            if now_kst.time() >= shutdown_at:
                if not shutdown_done:
                    log.warning("자동매매 종료 시각 도달(KST %s): 최종 주문/잔고 확인 및 미체결 전체 취소 시작",
                                now_kst.strftime("%H:%M:%S"))
                    telegram.send("자동매매 종료 시각 도달: 미체결 주문을 최종 확인하고 취소합니다.")
                    try:
                        reconcile_and_subscribe()
                    except Exception:
                        log.exception("종료 직전 잔고/주문 대사 실패")
                    try:
                        result = cancel_all_open_orders(
                            api, orders,
                            confirm_timeout_sec=max(180, int(_env_float("SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC", 180))),
                            poll_sec=int(os.getenv("SHUTDOWN_CANCEL_POLL_SEC", "5")),
                        )
                        if result.get("cancelled_or_closed"):
                            # 미체결 0건 확인 후 당일 전체 주문/체결내역을 조회하고,
                            # 잔고까지 다시 동기화한 경우에만 uncertain 파일을 초기화합니다.
                            try:
                                today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
                                all_orders = api.daily_orders(today, today, ccld_dvsn="00")
                                filled_orders = api.daily_orders(today, today, ccld_dvsn="01")
                                orders.reconcile_uncertain_orders(
                                    all_orders, filled_rows=filled_orders,
                                    min_age_sec=int(os.getenv("UNCERTAIN_AUTO_RESOLVE_MIN_AGE_SEC", "180")),
                                    required_checks=int(os.getenv("UNCERTAIN_AUTO_RESOLVE_CHECKS", "3")),
                                )
                                # balance + open orders 재조회가 모두 성공해야 종료 초기화를 허용합니다.
                                reconcile_and_subscribe()
                                archive = orders.archive_and_clear_uncertain_orders("end_of_day_cancel_confirmed_and_final_reconcile_succeeded")
                                telegram.send("자동매매 종료 확인: 미체결 주문 0건. 당일 주문/체결/잔고 최종 조회 완료, uncertain_orders.json 초기화 완료."
                                              + (f"\n미해결 기록 백업={archive}" if archive else ""))
                            except Exception as final_exc:
                                log.exception("종료 최종 주문/체결/잔고 조회 실패; uncertain_orders.json 유지")
                                telegram.send(f"주의: 미체결은 0건이나 최종 조회 실패로 uncertain_orders.json을 유지합니다: {type(final_exc).__name__}: {final_exc}")
                        else:
                            telegram.send(f"주의: 종료 확인 시 미체결 주문이 남았거나 조회 실패. uncertain_orders.json 유지: {result.get('unresolved', [])}")
                    except Exception as exc:
                        log.exception("종료 시 미체결 주문 취소/확인 실패")
                        telegram.send(f"긴급: 종료 시 주문 취소 확인 실패. uncertain_orders.json 유지: {type(exc).__name__}: {exc}")
                    shutdown_done = True
                break

            # ----------------------------------------------------
            # 1. 보유 포지션 청산 관리 (SellProcess)
            # ----------------------------------------------------
            if sell_process.process_positions(
                now=now, api=api, last_rest_quote=last_rest_quote,
                yaml_exit_futures=yaml_exit_futures,
                yaml_exit_executor=yaml_exit_executor, fetch_yaml_exit=fetch_yaml_exit,
                reconcile_and_subscribe=reconcile_and_subscribe,
                show_order_calculation=_show_order_calculation,
                quote_sell_price_for_order=_quote_sell_price_for_order,
                is_no_broker_position_error=_is_no_broker_position_error,
            ):
                last_balance_reconcile = time.time()
                last_order_reconcile = last_balance_reconcile

            # ----------------------------------------------------
            # 2. 잔고 reconcile - 비교적 긴 주기
            # ----------------------------------------------------
            if now - last_balance_reconcile >= settings.balance_interval_sec:
                try:
                    reconcile_and_subscribe()
                    last_balance_reconcile = now
                    last_order_reconcile = now  # reconcile()가 미체결도 함께 조회했으므로 중복 조회 방지
                except Exception as exc:
                    update_buy_safety(balance_ok=False, reason=f"계좌 잔고 대사 실패: {type(exc).__name__}")
                    # 실패 시 last_*를 갱신하지 않으면 루프마다 재호출해 KIS 호출 제한을 유발할 수 있습니다.
                    last_balance_reconcile = now
                    log.exception("broker balance/order reconcile 오류; 다음 재시도는 최소 %.1f초 후", settings.balance_interval_sec)

            # ----------------------------------------------------
            # 3. 미체결 주문만 짧은 주기로 reconcile
            #    -> 종목별 pending을 관리하므로 다른 종목의 매수를 전역 차단하지 않습니다.
            # ----------------------------------------------------
            if now - last_order_reconcile >= settings.order_reconcile_interval_sec:
                try:
                    reconcile_open_orders(api, orders)
                    update_buy_safety(orders_ok=True)
                    last_order_reconcile = now
                except Exception as exc:
                    update_buy_safety(orders_ok=False, reason=f"미체결 주문 조회 실패: {type(exc).__name__}")
                    # 실패 시 매 루프마다 조회하지 않고 설정된 주기를 지켜 호출 폭주를 방지합니다.
                    last_order_reconcile = now
                    log.exception("미체결 주문 reconcile 오류; 다음 재시도는 최소 %.1f초 후", settings.order_reconcile_interval_sec)

            # 3b. 불명확 주문은 1분마다 당일 전체 주문내역으로 대사합니다.
            # 조회가 성공했을 때만 검사 횟수를 올리며, 주문 POST는 절대 자동 재전송하지 않습니다.
            uncertain_interval = max(30, int(os.getenv("UNCERTAIN_RECONCILE_INTERVAL_SEC", "60")))
            if orders.uncertain_symbols and now - last_uncertain_reconcile >= uncertain_interval:
                try:
                    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
                    all_orders = api.daily_orders(today, today, ccld_dvsn="00")
                    filled_orders = api.daily_orders(today, today, ccld_dvsn="01")
                    result = orders.reconcile_uncertain_orders(
                        all_orders, filled_rows=filled_orders,
                        min_age_sec=int(os.getenv("UNCERTAIN_AUTO_RESOLVE_MIN_AGE_SEC", "180")),
                        required_checks=int(os.getenv("UNCERTAIN_AUTO_RESOLVE_CHECKS", "3")),
                    )
                    log.info("불명확 주문 자동 대사: resolved=%d waiting=%d", result["resolved"], result["waiting"])
                    # 3회 이상 성공 조회 또는 180초 이상 미해결이면 Telegram으로 운영자 확인을 요청합니다.
                    # 같은 주문의 알림은 기본 300초 간격으로 제한하며 상태 파일에 알림 시각을 저장합니다.
                    alert_interval = max(60, int(os.getenv("UNCERTAIN_TELEGRAM_ALERT_INTERVAL_SEC", "300")))
                    alert_now = time.time()
                    alert_items = []
                    for symbol, record in orders.uncertain_orders.items():
                        age = alert_now - float(record.get("created_ts", alert_now) or alert_now)
                        checks = int(record.get("checks", 0) or 0)
                        last_alert = float(record.get("last_telegram_alert_ts", 0) or 0)
                        if (checks >= int(os.getenv("UNCERTAIN_AUTO_RESOLVE_CHECKS", "3") or 3) or age >= int(os.getenv("UNCERTAIN_AUTO_RESOLVE_MIN_AGE_SEC", "180") or 180)) and alert_now - last_alert >= alert_interval:
                            alert_items.append((symbol, record, age, checks))
                    if alert_items:
                        lines = ["⚠️ KIS 불명확 주문 운영자 확인 요청", "당일 주문/체결내역에서 아직 확정되지 않았습니다.", "자동 잠금 해제 및 동일 주문 재전송은 하지 않았습니다."]
                        for symbol, record, age, checks in alert_items:
                            lines.append(f"종목={symbol_label(symbol)} 방향={record.get('side', 'UNKNOWN')} 수량={record.get('qty', 0)} 경과={int(age)}초 조회성공={checks}회 오류={record.get('last_error', 'unknown')}")
                            record["last_telegram_alert_ts"] = alert_now
                            record["telegram_alert_count"] = int(record.get("telegram_alert_count", 0) or 0) + 1
                        orders._save_uncertain_symbols()
                        telegram.send("\n".join(lines))
                    if result["resolved"]:
                        # 주문이 발견된 직후 잔고/미체결을 재동기화합니다. 실패해도
                        # 발견한 주문은 pending 예약으로 남아 동일 종목 중복을 막습니다.
                        try:
                            reconcile_and_subscribe()
                            last_balance_reconcile = time.time()
                            last_order_reconcile = last_balance_reconcile
                        except Exception:
                            log.exception("불명확 주문 발견 후 즉시 잔고/미체결 동기화 실패; pending 예약 유지")
                    last_uncertain_reconcile = now
                except Exception:
                    log.exception("불명확 주문 전체내역 대사 실패; 잠금 유지")

            # ----------------------------------------------------
            # 4. timeout 주문 취소 요청
            # ----------------------------------------------------
            if settings.enable_trading:
                for pending in orders.timed_out(now):
                    try:
                        orders.request_cancel(pending)
                        cancel_record = orders.cancel_records.get(pending.order_no, {})
                        cancel_order_no = str(cancel_record.get("cancel_order_no", "") or "미발급/확인중")
                        # 취소 API 성공 응답의 별도 취소 주문번호를 저장하고, 최종 상태는 REST 대사에서 확인합니다.
                        if pending.side == "SELL":
                            # 취소 응답은 최종 잔량을 보장하지 않습니다. 새 SELL을 내기 전
                            # 다음 루프에서 잔고/미체결을 먼저 다시 동기화합니다.
                            pos_after_cancel = store.get_position(pending.symbol)
                            if pos_after_cancel:
                                retry_count = int(pos_after_cancel.get("sell_retry_count", 0) or 0) + 1
                                pos_after_cancel["sell_retry_count"] = retry_count
                                settle_sec = sell_retry_backoff_seconds(
                                    retry_count, max(60.0, _env_float("SELL_RETRY_COOLDOWN_SEC", 60.0)),
                                    _env_float("SELL_RETRY_MAX_BACKOFF_SEC", 900.0),
                                    max(60.0, _env_float("SELL_CANCEL_SETTLE_SEC", 60.0)))
                                pos_after_cancel["exit_retry_after_ts"] = time.time() + settle_sec
                                store.set_position(pending.symbol, pos_after_cancel)
                                store.save()
                            last_balance_reconcile = 0.0
                        log.warning(
                            "timeout 주문 취소 요청: order=%s symbol=%s side=%s; 잔고 대사 후 재주문",
                            pending.order_no, symbol_label(pending.symbol), pending.side,
                        )
                        log.info("취소 요청 연결정보: original_order=%s cancel_order=%s", pending.order_no, cancel_order_no)
                        telegram.send(f"미체결 타임아웃 취소 요청\n종목={symbol_label(pending.symbol, (store.get_position(pending.symbol) or {}).get('name', ''))}\n구분={pending.side}\n원주문번호={pending.order_no}\n취소주문번호={cancel_order_no}\n취소상태=최종 확인 대기")
                    except Exception as exc:
                        orders.record_cancel_error(pending, exc)
                        # KIS 40330000은 취소할 잔량이 없다는 뜻입니다.
                        # 같은 취소 요청을 반복하지 않고 다음 REST 미체결/잔고 조회로
                        # 실제 상태(전량 체결/이미 취소/기타 종료)를 확인합니다.
                        error_text = str(exc)
                        if "40330000" in error_text or "정정/취소할 수량이 없습니다" in error_text:
                            orders.mark_cancel_attempted(pending.order_no)
                            log.warning(
                                "취소 가능 잔량 없음: order=%s; REST 주문/잔고 조회로 상태 확인",
                                pending.order_no,
                            )
                        else:
                            if "응답 불명확" in error_text or "접수 여부" in error_text:
                                # 취소 POST 응답이 불명확하면 자동 재전송하지 않고 조회로 상태를 확인합니다.
                                orders.mark_cancel_attempted(pending.order_no)
                                log.error("취소 결과 불명확; 자동 재전송 중지하고 주문 대사 대기: order=%s", pending.order_no)
                            else:
                                log.error(
                                    "timeout 주문 취소 실패: order=%s error=%s",
                                    pending.order_no, error_text, exc_info=True,
                                )

            # ----------------------------------------------------
            # 5. 신규 진입
            # ----------------------------------------------------
            if now - last_scan >= settings.scan_interval_sec:
                try:
                    if stop_flag.is_set():
                        log.warning("종료 신호 수신으로 신규 진입 cycle을 실행하지 않습니다.")
                    elif orders.integrity_error:
                        # 종목코드를 알 수 없는 손상/접근불가 파일이므로 전체 신규 매수를 차단합니다.
                        # 기존 보유종목의 매도·체결 대사·잔고 관리는 계속 실행합니다.
                        log.critical("불명확 주문 파일 복구 전 신규 매수 전역 차단: %s", orders.integrity_error)
                    elif not _market_open_window():
                        log.debug("신규 진입 시간 외: KST %s", datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S"))
                    elif time.monotonic() < buy_pause_until:
                        remaining = buy_pause_until - time.monotonic()
                        log.info("매수 주문 후 대기중: 신규 매수만 %.1f초 보류; 매도/대사 루프는 계속 실행", remaining)
                    elif daily_buy_limit_latched or store.state.trades_today >= settings.max_trades_per_day:
                        # 복구 실패/한도 도달 latch와 실제 카운터를 함께 검사합니다.
                        daily_buy_limit_latched = True
                        log.info("일일 최대 매수 거래횟수 도달: trades_today=%d limit=%d latch=%s",
                                 store.state.trades_today, settings.max_trades_per_day, daily_buy_limit_latched)
                    elif store.state.daily_start_assets_krw <= 0:
                        # 기준자산을 모르면 손실률을 계산할 수 없습니다.
                        # 안전상 신규 진입은 막되, 이를 실제 손실 한도 도달로 잘못 표기하지 않습니다.
                        log.warning(
                            "일일 손실 제한 기준자산 미확인: realized_pnl=%.0f원; "
                            "손실률 계산 불가로 신규 매수 차단",
                            store.state.daily_realized_pnl_krw,
                        )
                    elif store.daily_loss_pct() <= -settings.max_daily_loss_pct:
                        log.warning("일일 손실 제한 도달: realized_pnl=%.0f원 loss_pct=%.3f%% limit=%.3f%%",
                                    store.state.daily_realized_pnl_krw, store.daily_loss_pct(), settings.max_daily_loss_pct)
                    elif (os.getenv("BLOCK_NEW_BUY_WHILE_PENDING_BUY", "true").strip().lower() in {"1", "true", "yes", "on"}
                          and orders.has_pending_buy_orders()):
                        pending = [o for o in orders.snapshot() if o.side == "BUY" and o.remaining_qty > 0]
                        detail = ", ".join(f"{symbol_label(o.symbol)} 주문={o.order_no} 체결={o.filled_qty}/{o.qty} 잔량={o.remaining_qty}"
                                            for o in pending)
                        if orders.uncertain_symbols:
                            detail = (detail + "; " if detail else "") + "불명확=" + ",".join(symbol_label(item) for item in sorted(orders.uncertain_symbols))
                        log.info("신규 매수 전역 보류: 미완료/불명확 매수 주문 존재; BLOCK_NEW_BUY_WHILE_PENDING_BUY=true (%s)", detail or "주문 상태 대사 중")
                    elif len(set(store.symbols()) | orders.pending_buy_symbols()) >= max(1, int(os.getenv("MAX_POSITIONS", "3"))):
                        # 실제 보유 + 미완료/불명확 매수 종목만 포지션 슬롯으로 계산합니다.
                        # 다른 종목의 미체결/불명확 주문이 있다는 이유만으로 신규 매수를 전역 차단하지 않습니다.
                        # 앱에서 수동 매도한 경우 잔고 reconcile 이후 보유 수량이 갱신되고,
                        # 매도 미체결이 남아 있으면 해당 주문도 별도로 표시됩니다.
                        details = []
                        holdings = []
                        for held_symbol in sorted(store.symbols()):
                            held_pos = store.get_position(held_symbol) or {}
                            held_name = str(held_pos.get("name") or held_symbol)
                            held_qty = int(held_pos.get("qty", 0) or 0)
                            holdings.append(f"{symbol_label(held_symbol, held_name)} {held_qty}주")
                        if holdings:
                            details.append("보유종목=[" + ", ".join(holdings) + "]")

                        pending_items = [o for o in orders.snapshot() if o.remaining_qty > 0]
                        sell_items = [o for o in pending_items if o.side == "SELL"]
                        buy_items = [o for o in pending_items if o.side == "BUY"]
                        if sell_items:
                            details.append("매도미체결=[" + ", ".join(
                                f"{symbol_label(o.symbol)} 주문={o.order_no} 잔량={o.remaining_qty}/{o.qty}"
                                for o in sell_items
                            ) + "]")
                        if buy_items:
                            details.append("매수미체결=[" + ", ".join(
                                f"{symbol_label(o.symbol)} 주문={o.order_no} 잔량={o.remaining_qty}/{o.qty}"
                                for o in buy_items
                            ) + "]")
                        if orders.uncertain_symbols:
                            details.append("주문결과불명확 종목별 대사중=[" + ", ".join(sorted(orders.uncertain_symbols)) + "]")
                        if not details:
                            details.append("상태=미체결/보유 정보 확인 필요")
                        log.info("신규 진입 보류: %s", "; ".join(details))
                    else:
                        active_style, pick = buy_process.select_candidate(active_style=active_style)
                        if pick:
                            register_symbol(pick.symbol, pick.name)

                        pick = buy_process.validate_candidate(
                            pick=pick,
                            buy_safety=buy_safety,
                            symbol_label=symbol_label,
                            log=log,
                        )
                        if pick:
                            prepared_order = buy_process.prepare_candidate_order(
                                pick=pick,
                                price_value=price_value,
                                resolve_order_mode=_resolve_order_mode,
                                quote_price_for_order=_quote_price_for_order,
                                summary_value=_summary_value,
                                calculate_buy_qty=calculate_buy_qty,
                                show_order_calculation=_show_order_calculation,
                                env_float=_env_float,
                                register_symbol=register_symbol,
                                symbol_label=symbol_label,
                                update_buy_safety=update_buy_safety,
                                log=log,
                                order_log=order_log,
                            )
                            qty = prepared_order["qty"]
                            budget_reason = prepared_order["budget_reason"]
                            reference_price = prepared_order["reference_price"]
                            current = prepared_order["current"]
                            fresh_quote = prepared_order["fresh_quote"]
                            sizing_price = prepared_order["sizing_price"]
                            order_mode = prepared_order["order_mode"]
                            mode_reason = prepared_order["mode_reason"]
                            fee_pct = prepared_order["fee_pct"]
                            market_buffer = prepared_order["market_buffer"]
                            if qty <= 0:
                                log.warning("매수 주문 보류 symbol=%s: %s", symbol_label(pick.symbol, pick.name), budget_reason)
                            else:
                                if _show_order_calculation():
                                    market_buffer = max(0.0, _env_float("KIS_MARKET_PRICE_BUFFER_PCT", 1.0)) if order_mode == "MARKET" else 0.0
                                    fee_pct = max(0.0, _env_float("KIS_FEE_RATE_PCT", 0.02))
                                    estimate_price = sizing_price * (1.0 + market_buffer / 100.0)
                                    estimate_gross = qty * estimate_price
                                    estimate_fee = estimate_gross * fee_pct / 100.0
                                    order_log.info("BUY 계산 symbol=%s ref=%.0f current=%.0f change_pct=%.3f mode=%s qty=%d est_price=%.2f est_amount=%.0f fee=%.0f est_total=%.0f reason=%s",
                                                   symbol_label(pick.symbol, pick.name), reference_price, current,
                                                   ((current/reference_price)-1)*100 if reference_price else 0.0,
                                                   order_mode, qty, estimate_price, estimate_gross, estimate_fee, estimate_gross+estimate_fee, mode_reason)
                                submit_result = buy_process.submit_candidate_order(
                                    pick=pick,
                                    active_style=active_style,
                                    qty=qty,
                                    order_mode=order_mode,
                                    sizing_price=sizing_price,
                                    market_buffer_pct=market_buffer,
                                    fee_pct=fee_pct,
                                    daily_buy_limit_latched=daily_buy_limit_latched,
                                    stop_event=stop_flag,
                                    market_open_window=_market_open_window,
                                    update_buy_safety=update_buy_safety,
                                    symbol_label=symbol_label,
                                    log=log,
                                    order_log=order_log,
                                    telegram=telegram,
                                    settings=settings,
                                    env_float=_env_float,
                                    sizing_context=prepared_order.get("sizing_context"),
                                    calculate_buy_qty=calculate_buy_qty,
                                )
                                daily_buy_limit_latched = submit_result["daily_buy_limit_latched"]
                                if submit_result["buy_pause_until"] is not None:
                                    buy_pause_until = submit_result["buy_pause_until"]

                        # V7.1.0은 공격형/안정형 교대를 제거하고 통합 설정을 사용합니다.
                        active_style = "unified"

                    last_scan = now
                except Exception as exc:
                    log.exception("매매 cycle 오류")
                    telegram.send(f"KIS V7.1.6 매매 cycle 오류: {type(exc).__name__}: {exc}")
                    last_scan = now

            time.sleep(0.2)

    finally:
        stop_flag.set()
        try:
            exit_queue.put((0, time.monotonic(), "__STOP__"))
            exit_worker_thread.join(timeout=5.0)
        except Exception:
            log.exception("실시간 청산 작업자 종료 중 오류")
        try:
            yaml_exit_executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python 3.8 compatibility
            yaml_exit_executor.shutdown(wait=False)
        try:
            realtime.stop()
        except Exception:
            log.exception("WebSocket 종료 중 오류; 체결 worker 종료 대사를 계속합니다")
        try:
            for deferred_fill in orders.drain_deferred_fill_events():
                fill_event_queue.put(deferred_fill)
        except Exception:
            log.exception("종료 시 보류 체결 이벤트 큐 이동 실패")
        fill_worker_drained = False
        try:
            fill_event_queue.put(None)
            fill_worker_thread.join(timeout=60.0)
            fill_worker_drained = (
                not fill_worker_thread.is_alive()
                and fill_event_queue.empty()
                and fill_event_queue.unfinished_tasks == 0
            )
            if not fill_worker_drained:
                log.critical("체결 처리 worker/대기 큐가 60초 내 완전히 종료되지 않음; 종료 잔고 조회를 생략하고 마지막 정상 상태를 보존합니다")
            else:
                log.info("체결 처리 worker 종료 확인: pending_queue=%d", fill_event_queue.qsize())
        except Exception:
            log.exception("체결 처리 worker 종료 확인 실패")
        if fill_worker_drained:
            try:
                _final_holdings, _final_summary = api.balance()
                log_portfolio_snapshot("프로그램 종료", _final_holdings, _final_summary)
            except Exception as snapshot_exc:
                log.exception("프로그램 종료 시 최종 보유주식 조회 실패; 마지막 정상 잔고를 확인하십시오")
                telegram.send(f"프로그램 종료 보유주식 조회 실패: {type(snapshot_exc).__name__}: {snapshot_exc}")
        log.info("STOP_OUTCOME status=graceful source=%s project_root=%s", Path(__file__).resolve(), Path(__file__).resolve().parent)
        log.info("KIS V7.1.6 종료")
        telegram.send("KIS V7.1.6 프로그램 종료")


if __name__ == "__main__":
    main()
