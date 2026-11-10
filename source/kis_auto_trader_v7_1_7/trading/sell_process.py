"""보유 포지션의 실시간 위험 이벤트를 주문으로 전환하는 매도 실행 모듈."""
from __future__ import annotations

import logging
import os
import queue
import time

from config.paths import resolve_path

log = logging.getLogger(__name__)
order_log = logging.getLogger("trading.order")


class SellProcess:
    """실시간 청산 작업자를 소유하고 공통 OrderManager로만 주문한다.

    이 단계에서는 기존 이벤트 작업자의 알고리즘과 순서를 유지하고, 클로저
    의존성을 명시적 생성자 인자로 바꾸는 것만 수행한다.
    """
    def __init__(
        self, *, store, orders, settings, stop_event, exit_queue, price_cache,
        price_cache_lock, exit_queue_lock, exit_queued_symbols, submission_guard,
        telegram, dry_run_logger, env_float, price_value, position_strategy_risk_exit,
        evaluate_exit_priority, sell_order_window, is_stop_reason, resolve_order_mode,
        sell_retry_delay, symbol_label,
    ) -> None:
        self.store = store
        self.orders = orders
        self.settings = settings
        self.stop_event = stop_event
        self.exit_queue = exit_queue
        self.price_cache = price_cache
        self.price_cache_lock = price_cache_lock
        self.exit_queue_lock = exit_queue_lock
        self.exit_queued_symbols = exit_queued_symbols
        self.submission_guard = submission_guard
        self.telegram = telegram
        self.dry_run_logger = dry_run_logger
        self.env_float = env_float
        self.price_value = price_value
        self.position_strategy_risk_exit = position_strategy_risk_exit
        self.evaluate_exit_priority = evaluate_exit_priority
        self.sell_order_window = sell_order_window
        self.is_stop_reason = is_stop_reason
        self.resolve_order_mode = resolve_order_mode
        self.sell_retry_delay = sell_retry_delay
        self.symbol_label = symbol_label
        # 보유 종목의 조건 미충족 진단 로그는 종목별 60초 간격으로 제한합니다.
        self._last_exit_diagnostic: dict[str, float] = {}

    def process_positions(
        self, *, now: float, api, last_rest_quote: dict,
        yaml_exit_futures: dict, yaml_exit_executor, fetch_yaml_exit,
        reconcile_and_subscribe, show_order_calculation,
        quote_sell_price_for_order, is_no_broker_position_error,
    ) -> bool:
        """일반 주기에서 보유 포지션을 평가하고 필요한 매도 주문을 제출한다.

        반환값은 브로커 잔고 불일치 오류 때문에 즉시 대사를 수행했는지 나타낸다.
        주문/체결/잔고 상태 머신은 기존 OrderManager와 reconcile 함수에 남긴다.
        """
        reconciled_immediately = False
        for symbol in self.store.symbols():
            with self.exit_queue_lock:
                if symbol in self.exit_queued_symbols:
                    continue
            if self.stop_event.is_set():
                log.warning("종료 신호 확인: 추가 매도 주문 판단을 중단합니다.")
                break
            pos = self.store.get_position(symbol)
            if not pos:
                continue
            if self.orders.pending_for_symbol(symbol):
                continue
            retry_after = float(pos.get("exit_retry_after_ts", 0) or 0)
            if now < retry_after:
                continue

            with self.price_cache_lock:
                quote = dict(self.price_cache.get(symbol, {}))
            received_ts = float(quote.get("_received_ts", 0) or 0)
            quote_is_fresh = bool(quote) and received_ts > 0 and (now - received_ts) <= self.settings.quote_stale_sec
            current = self.price_value(quote) if quote_is_fresh else 0.0
            if not quote_is_fresh:
                last = float(last_rest_quote.get(symbol, 0) or 0)
                if now - last >= self.settings.quote_rest_min_interval_sec:
                    try:
                        quote = dict(api.current_price(symbol) or {})
                        current = self.price_value(quote)
                        quote["_received_ts"] = time.time()
                        with self.price_cache_lock:
                            self.price_cache[symbol] = dict(quote)
                        last_rest_quote[symbol] = time.time()
                    except Exception:
                        log.exception("보유종목 최신 현재가 REST fallback 실패: %s", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")))
                        current = 0.0
                else:
                    current = 0.0
            if current <= 0:
                log.warning("청산 판단 보류: 최신 시세 확인 불가 symbol=%s", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")))
                continue

            if current > float(pos.get("high_price", 0) or 0):
                pos["high_price"] = current
                self.store.set_position(symbol, pos)

            exit_path = str(resolve_path(os.getenv("EXIT_STRATEGY_FILE", "config/my_exit_strategy.kis.yaml")))
            interval = max(30, int(os.getenv("EXIT_STRATEGY_INTERVAL_SEC", "60")))
            last_eval = float(pos.get("yaml_exit_eval_ts", 0) or 0)
            evaluation_mode = os.getenv("STRATEGY_EVALUATION_MODE", "ALL").strip().upper()
            custom_exit_needed = evaluation_mode in {"CUSTOM", "ALL"}
            if ((not custom_exit_needed or os.path.isfile(exit_path)) and now - last_eval >= interval
                    and symbol not in yaml_exit_futures):
                pos["yaml_exit_eval_ts"] = now
                self.store.set_position(symbol, pos)
                profile_name = str(pos.get("strategy", "unified"))
                yaml_exit_futures[symbol] = yaml_exit_executor.submit(fetch_yaml_exit, symbol, exit_path, profile_name)

            avg_price = float(pos.get("avg_price", 0) or 0)
            pnl_pct = ((current / avg_price) - 1.0) * 100.0 if avg_price > 0 else 0.0
            should_exit, reason = self.position_strategy_risk_exit(pos, current)
            if not should_exit:
                should_exit, reason = self.evaluate_exit_priority(
                    pnl_pct, bool(pos.get("yaml_exit_signal", False)),
                    stop_loss_hard_pct=self.env_float("EXIT_STOP_LOSS_HARD_PCT", 2.0),
                    stop_loss_first_pct=self.env_float("EXIT_STOP_LOSS_FIRST_PCT", self.env_float("EXIT_STOP_LOSS_YAML_PCT", 1.5)),
                    take_profit_first_pct=self.env_float("EXIT_TAKE_PROFIT_FIRST_PCT", self.env_float("EXIT_TAKE_PROFIT_YAML_PCT", 1.5)),
                    take_profit_hard_pct=self.env_float("EXIT_TAKE_PROFIT_HARD_PCT", 3.0),
                )
            if not should_exit:
                last_diag = self._last_exit_diagnostic.get(symbol, 0.0)
                if now - last_diag >= 60.0:
                    self._last_exit_diagnostic[symbol] = now
                    log.info("[SELL_EVALUATION] symbol=%s decision=HOLD pnl_pct=%.3f yaml_exit_signal=%s yaml_exit_reason=%s "
                             "strategy_risk=not_triggered hard_stop_pct=%.3f first_stop_pct=%.3f first_take_pct=%.3f hard_take_pct=%.3f "
                             "reason=매도 조건 미충족; YAML 조건과 안전망을 계속 감시",
                             self.symbol_label(symbol, pos.get("name", "")), pnl_pct,
                             bool(pos.get("yaml_exit_signal", False)), pos.get("yaml_exit_reason", "평가 대기"),
                             self.env_float("EXIT_STOP_LOSS_HARD_PCT", 2.0),
                             self.env_float("EXIT_STOP_LOSS_FIRST_PCT", self.env_float("EXIT_STOP_LOSS_YAML_PCT", 1.5)),
                             self.env_float("EXIT_TAKE_PROFIT_FIRST_PCT", self.env_float("EXIT_TAKE_PROFIT_YAML_PCT", 1.5)),
                             self.env_float("EXIT_TAKE_PROFIT_HARD_PCT", 3.0))
                continue
            log.info("[SELL_EVALUATION] symbol=%s decision=SELL pnl_pct=%.3f reason=%s", self.symbol_label(symbol, pos.get("name", "")), pnl_pct, reason)
            if not self.sell_order_window():
                log.info("매도 시그널은 있으나 매도 주문 가능 시간 외라 대기: %s reason=%s", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), reason)
                continue
            if not self.settings.enable_trading:
                self.dry_run_logger(symbol, reason)
                continue
            qty = int(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue

            try:
                is_stop = self.is_stop_reason(reason)
                configured = os.getenv("KIS_STOP_LOSS_ORDER_MODE", "MARKET") if is_stop else os.getenv("KIS_SEL_ORDER_MODE", "BEST_LIMIT")
                reference = float(pos.get("avg_price", 0) or 0)
                order_mode, mode_reason = self.resolve_order_mode(
                    configured, "SELL", current, reference,
                    high_price=float(pos.get("high_price", 0) or 0),
                    is_stop_loss=is_stop, quote=quote, order_qty=qty)
                if order_mode == "HOLD":
                    retry_sec = max(0.5, self.env_float("STOP_LOSS_QUOTE_RETRY_SEC", 1.0)) if is_stop else max(1.0, self.settings.sell_retry_cooldown_sec)
                    pos["exit_retry_after_ts"] = time.time() + retry_sec
                    self.store.set_position(symbol, pos)
                    log.warning("매도 주문 보류: %s reason=%s policy=%s 재평가=%.1f초", self.symbol_label(symbol, pos.get("name", "")), reason, mode_reason, retry_sec)
                    continue
                if show_order_calculation():
                    sell_buffer = max(0.0, self.env_float("KIS_MARKET_PRICE_BUFFER_PCT", 1.0)) if order_mode == "MARKET" else 0.0
                    fee_pct = max(0.0, self.env_float("KIS_FEE_RATE_PCT", 0.02))
                    tax_pct = max(0.0, self.env_float("KIS_SELL_TAX_RATE_PCT", 0.20))
                    base_sell_price = quote_sell_price_for_order(quote, order_mode, current)
                    estimate_price = base_sell_price * (1.0 - sell_buffer / 100.0)
                    estimate_gross = qty * estimate_price
                    estimate_fee = estimate_gross * fee_pct / 100.0
                    estimate_tax = estimate_gross * tax_pct / 100.0
                    order_log.info("SELL 계산 symbol=%s qty=%d current=%.0f mode=%s est_price=%.2f est_gross=%.0f fee=%.0f tax=%.0f est_net=%.0f stop=%s policy=%s",
                                   self.symbol_label(symbol, pos.get("name", "")), qty, current, order_mode, estimate_price, estimate_gross, estimate_fee, estimate_tax,
                                   estimate_gross-estimate_fee-estimate_tax, is_stop, mode_reason)
                if self.stop_event.is_set():
                    log.warning("종료 신호 확인: 신규 SELL 제출 차단 symbol=%s", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")))
                    break
                if not self.submission_guard.reserve(symbol, self.orders.pending_for_symbol):
                    continue
                try:
                    order = self.orders.submit(symbol, "SELL", qty, strategy=str(pos.get("strategy", "unified")),
                                               order_mode=order_mode, position_qty_before_submit=int(pos.get("qty", 0) or 0), stop_event=self.stop_event)
                    pos["pending_order_no"] = order.order_no
                    pos["pending_side"] = "SELL"
                    self.store.set_position(symbol, pos)
                    self.store.save()
                finally:
                    self.submission_guard.release(symbol)
                order_log.warning("SELL 접수 symbol=%s qty=%d mode=%s reason=%s order=%s",
                                  self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), qty, order_mode, reason, order.order_no)
                self.telegram.send(f"매도 주문 접수\n종목={self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get('name', ''))}\n수량={qty}\n방식={order_mode}\n사유={reason}\n주문번호={order.order_no}")
            except Exception as exc:
                no_broker_position = is_no_broker_position_error(exc)
                retry_sec = self.sell_retry_delay(exc, self.settings.sell_retry_cooldown_sec)
                if not self.orders.pending_for_symbol(symbol):
                    pos["exit_retry_after_ts"] = time.time() + retry_sec
                    self.store.set_position(symbol, pos)
                if no_broker_position:
                    log.error("매도 거절: 브로커 잔고 없음/불일치 symbol=%s; 동일 매도 재시도 보류 %.1f초 후 즉시 잔고 대사", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), retry_sec)
                    try:
                        reconcile_and_subscribe()
                        reconciled_immediately = True
                    except Exception:
                        log.exception("매도 잔고 불일치 후 즉시 잔고 대사 실패; 재시도 대기 유지: %s", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")))
                else:
                    log.exception("SELL 주문 실패: %s; 재평가 대기=%.1f초", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), retry_sec)
                self.telegram.send(f"SELL 주문 오류: {self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get('name', ''))} {type(exc).__name__}: {exc}")
        return reconciled_immediately

    def event_exit_worker(self) -> None:
        """실시간 위험 이벤트 전용 작업자; 기존 주문 상태 머신을 통해서만 매도합니다."""
        while not self.stop_event.is_set():
            try:
                item = self.exit_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self.exit_queue.task_done()
                break
            priority, _queued_at, symbol = item
            try:
                pos = self.store.get_position(symbol)
                if not pos or int(pos.get("qty", 0) or 0) <= 0 or self.orders.pending_for_symbol(symbol):
                    continue
                now_ts = time.time()
                if now_ts < float(pos.get("exit_retry_after_ts", 0) or 0):
                    continue
                with self.price_cache_lock:
                    quote = dict(self.price_cache.get(symbol, {}))
                received_ts = float(quote.get("_received_ts", 0) or 0)
                max_age = max(1.0, self.env_float("KIS_EVENT_EXIT_QUOTE_MAX_AGE_SEC", self.settings.quote_stale_sec))
                current = self.price_value(quote) if received_ts > 0 and now_ts - received_ts <= max_age else 0.0
                avg_price = float(pos.get("avg_price", 0) or 0)
                if current <= 0 or avg_price <= 0:
                    continue
                if current > float(pos.get("high_price", 0) or 0):
                    pos["high_price"] = current
                pnl_pct = (current / avg_price - 1.0) * 100.0
                should_exit, reason = self.position_strategy_risk_exit(pos, current)
                if not should_exit:
                    should_exit, reason = self.evaluate_exit_priority(
                        pnl_pct, bool(pos.get("yaml_exit_signal", False)),
                        stop_loss_hard_pct=self.env_float("EXIT_STOP_LOSS_HARD_PCT", 2.0),
                        stop_loss_first_pct=self.env_float("EXIT_STOP_LOSS_FIRST_PCT", self.env_float("EXIT_STOP_LOSS_YAML_PCT", 1.5)),
                        take_profit_first_pct=self.env_float("EXIT_TAKE_PROFIT_FIRST_PCT", self.env_float("EXIT_TAKE_PROFIT_YAML_PCT", 1.5)),
                        take_profit_hard_pct=self.env_float("EXIT_TAKE_PROFIT_HARD_PCT", 3.0),
                    )
                if not should_exit:
                    continue
                if not self.sell_order_window():
                    log.info("실시간 매도 이벤트는 있으나 주문 가능 시간 외: %s reason=%s", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), reason)
                    continue
                if not self.settings.enable_trading:
                    self.dry_run_logger(symbol, reason, realtime=True)
                    continue
                qty = int(pos.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                is_stop = self.is_stop_reason(reason)
                configured = os.getenv("KIS_STOP_LOSS_ORDER_MODE", "MARKET") if is_stop else os.getenv("KIS_SEL_ORDER_MODE", "BEST_LIMIT")
                order_mode, mode_reason = self.resolve_order_mode(
                    configured, "SELL", current, avg_price,
                    high_price=float(pos.get("high_price", 0) or 0),
                    is_stop_loss=is_stop, quote=quote, order_qty=qty)
                if order_mode == "HOLD":
                    retry_sec = max(0.5, self.env_float("STOP_LOSS_QUOTE_RETRY_SEC", 1.0)) if is_stop else max(1.0, self.settings.sell_retry_cooldown_sec)
                    pos["exit_retry_after_ts"] = time.time() + retry_sec
                    self.store.set_position(symbol, pos)
                    log.warning("실시간 매도 주문 보류: %s reason=%s policy=%s 재평가=%.1f초", self.symbol_label(symbol, pos.get("name", "")), reason, mode_reason, retry_sec)
                    continue
                # main loop와 이벤트 작업자가 같은 종목을 동시에 매도하지 않도록
                # pending 검사/종목별 예약만 원자적으로 수행하고 REST POST 중에는 전역 락을 잡지 않습니다.
                if not self.submission_guard.reserve(symbol, self.orders.pending_for_symbol):
                    continue
                # 종목별 예약만 유지하고 네트워크 요청 중 전역 락은 잡지 않습니다.
                try:
                    order = self.orders.submit(symbol, "SELL", qty, strategy=str(pos.get("strategy", "unified")),
                                          order_mode=order_mode, position_qty_before_submit=int(pos.get("qty", 0) or 0), stop_event=self.stop_event)
                    pos["pending_order_no"] = order.order_no
                    pos["pending_side"] = "SELL"
                    self.store.set_position(symbol, pos)
                    self.store.save()
                finally:
                    self.submission_guard.release(symbol)
                order_log.warning("실시간 SELL 접수 symbol=%s qty=%d mode=%s priority=%d reason=%s order=%s",
                                  self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), qty, order_mode, priority, reason, order.order_no)
                self.telegram.send(f"실시간 매도 주문 접수\n종목={self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get('name', ''))}\n수량={qty}\n방식={order_mode}\n사유={reason}\n주문번호={order.order_no}")
            except Exception as exc:
                retry_sec = self.sell_retry_delay(exc, self.settings.sell_retry_cooldown_sec)
                pos = self.store.get_position(symbol)
                if pos and not self.orders.pending_for_symbol(symbol):
                    pos["exit_retry_after_ts"] = time.time() + retry_sec
                    self.store.set_position(symbol, pos)
                log.exception("실시간 청산 작업 실패: symbol=%s 재평가=%.1f초", self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get("name", "")), retry_sec)
                self.telegram.send(f"실시간 SELL 오류: {self.symbol_label(symbol, (self.store.get_position(symbol) or {}).get('name', ''))} {type(exc).__name__}: {exc}")
            finally:
                with self.exit_queue_lock:
                    self.exit_queued_symbols.discard(symbol)
                self.exit_queue.task_done()
