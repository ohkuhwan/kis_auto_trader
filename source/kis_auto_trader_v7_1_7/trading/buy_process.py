"""매수 후보 선정, 주문 수량 준비 및 마지막 주문 제출 안전 게이트를 담당하는 조정 계층.

전략별 후보 평가와 수량/현금 계산은 기존 계산기를 사용하며, 최종 BUY는
공유 OrderManager를 통해서만 제출해 주문 상태·예약·체결 대사를 일원화한다.
"""
from __future__ import annotations

import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Callable, Any

from brokers.kis.client import InsufficientBuyingPowerError

log = logging.getLogger(__name__)


class BuyProcess:
    """HTS와 Builder 후보를 기존 엔진으로 평가하고 최종 후보를 선택한다."""
    def __init__(
        self, *, api, store, engine, orders, realtime, price_cache, price_cache_lock,
        last_rest_quote, settings, run_hts_cycle: Callable[..., Any],
        run_builder_cycle: Callable[..., Any],
    ) -> None:
        self.api = api
        self.store = store
        self.engine = engine
        self.orders = orders
        self.realtime = realtime
        self.price_cache = price_cache
        self.price_cache_lock = price_cache_lock
        self.last_rest_quote = last_rest_quote
        self.settings = settings
        self.run_hts_cycle = run_hts_cycle
        self.run_builder_cycle = run_builder_cycle

    def select_candidate(self, *, active_style: str = "unified"):
        """ENTRY_MODE로 후보 출처를 정하고, 별도 평가 모드로 최종 BUY 후보를 판정합니다."""
        mode = os.getenv("ENTRY_MODE", "HTS").strip().upper()
        evaluation_mode = os.getenv("STRATEGY_EVALUATION_MODE", "ALL").strip().upper()
        if mode not in {"HTS", "BUILDER", "ALL"}:
            raise ValueError("ENTRY_MODE는 HTS, BUILDER, ALL 중 하나여야 합니다.")
        if evaluation_mode not in {"KIS10", "CUSTOM", "ALL"}:
            raise ValueError("STRATEGY_EVALUATION_MODE는 KIS10, CUSTOM, ALL 중 하나여야 합니다.")
        active_style = "unified"
        condition = self.settings.condition_name
        log.info("[ENTRY_SELECTION_START] entry_mode=%s evaluation_mode=%s condition=%r", mode, evaluation_mode, condition)
        hts_candidates = []
        if mode in {"HTS", "ALL"}:
            self.run_hts_cycle(
                self.api, self.store, self.engine, self.orders, self.realtime,
                self.price_cache, self.price_cache_lock, self.last_rest_quote,
                active_style, condition, self.settings.max_candidates,
                self.settings.quote_stale_sec, self.settings.quote_rest_min_interval_sec,
                candidate_sink=hts_candidates,
            )
        log.info("[ENTRY_SELECTION_HTS] candidate_count=%d symbols=%s", len(hts_candidates),
                 [getattr(candidate, "symbol", "UNKNOWN") for candidate in hts_candidates])
        pick = self.run_builder_cycle(
            self.api, self.store, self.price_cache, self.price_cache_lock,
            self.settings.quote_stale_sec, profile_name="unified",
            extra_symbols=[candidate.symbol for candidate in hts_candidates],
            include_manual=mode in {"BUILDER", "ALL"},
            evaluation_mode=evaluation_mode,
        )
        log.info("[ENTRY_SELECTION_BUILDER] evaluation_mode=%s selected=%s", evaluation_mode, getattr(pick, "symbol", "NONE"))
        log.info("[ENTRY_SELECTION_RESULT] entry_mode=%s evaluation_mode=%s hts_candidates=%d selected=%s score=%s",
                 mode, evaluation_mode, len(hts_candidates), getattr(pick, "symbol", "NONE"),
                 getattr(pick, "score", "NA"))
        return active_style, pick

    def validate_candidate(
        self, *, pick, buy_safety: dict, symbol_label: Callable[..., str], log,
    ):
        """후보 종목의 주문 상태·보유 여부·계좌 대사 안전 조건을 확인한다.

        기존 포지션 추가매수는 명시적으로 허용된 경우에만 통과시킨다. 이 검사는
        후보 선정을 바꾸지 않고, 최종 주문 전 안전 게이트만 적용한다.
        """
        if not pick:
            return None
        if hasattr(self.store, "buy_rejection_block"):
            today_key = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
            rejection_block = self.store.buy_rejection_block(pick.symbol, today_key)
            if rejection_block:
                log.warning("당일 2차 매수 실패 종목 잠금으로 후보 제외: symbol=%s reason=%s",
                            symbol_label(pick.symbol, pick.name), rejection_block.get("reason", ""))
                return None
        if self.orders.pending_for_symbol(pick.symbol):
            log.info(
                "신규 매수 후보 제외: 해당 종목의 미체결/불명확 주문 대사 중 symbol=%s",
                symbol_label(pick.symbol, pick.name),
            )
            return None
        if os.getenv("ALLOW_ADD_TO_EXISTING_POSITION", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            held = self.store.get_position(pick.symbol)
            if held and int(held.get("qty", 0) or 0) > 0:
                log.info(
                    "신규 매수 후보 제외: 이미 보유한 종목은 추가매수 차단 symbol=%s 보유수량=%d주",
                    symbol_label(pick.symbol, held.get("name") or pick.name),
                    int(held.get("qty", 0) or 0),
                )
                return None
        if not (buy_safety["balance_ok"] and buy_safety["orders_ok"]):
            log.warning(
                "신규 매수 차단 symbol=%s: 계좌/미체결 대사 안전 확인 미완료 balance_ok=%s orders_ok=%s reason=%s",
                symbol_label(pick.symbol, pick.name), buy_safety["balance_ok"], buy_safety["orders_ok"],
                buy_safety.get("reason") or "계좌/미체결 대사 미완료",
            )
            return None
        return pick

    def prepare_candidate_order(
        self, *, pick, price_value: Callable[..., float], resolve_order_mode: Callable[..., Any],
        quote_price_for_order: Callable[..., float], summary_value: Callable[..., float],
        calculate_buy_qty: Callable[..., Any], show_order_calculation: Callable[[], bool],
        env_float: Callable[..., float], register_symbol: Callable[..., Any],
        symbol_label: Callable[..., str], update_buy_safety: Callable[..., Any], log, order_log,
    ) -> dict[str, Any]:
        """주문 직전 최신 시세, 주문 방식, 계좌 현금 및 예약액을 검증해 수량을 산정한다.

        시세 재조회 실패, 주문가능금액 누락, 예약가격 불명확 또는 사이징 오류는
        신규 매수를 차단합니다. 고정수량 모드에서도 현금/수수료/시장가 버퍼 검증을
        수행하며, 실제 주문 제출은 별도 submit_candidate_order에서 처리합니다.
        """
        settings = self.settings
        qty = settings.order_qty
        budget_reason = "고정 수량 설정"
        reference_price = price_value(pick.price)
        fresh_quote: dict[str, Any] = {}
        current = 0.0
        try:
            fresh_quote = dict(self.api.current_price(pick.symbol) or {})
            fresh_quote["_received_ts"] = time.time()
            current = price_value(fresh_quote)
            quote_name = str(
                fresh_quote.get("hts_kor_isnm") or fresh_quote.get("prdt_name")
                or fresh_quote.get("stck_kor_isnm") or ""
            ).strip()
            if quote_name:
                pick.name = quote_name
                register_symbol(pick.symbol, quote_name)
            if current <= 0:
                raise ValueError("현재가가 0 이하입니다")
            with self.price_cache_lock:
                self.price_cache[pick.symbol] = dict(fresh_quote)
            if reference_price <= 0:
                reference_price = current
        except Exception:
            log.exception("매수 직전 최신 시세 조회 실패: 매수 차단 symbol=%s", symbol_label(pick.symbol, pick.name))
            current = 0.0

        sizing_price = current
        order_mode = "HOLD"
        mode_reason = "최신 시세 확인 실패"
        if current > 0:
            order_mode, mode_reason = resolve_order_mode(
                os.getenv("KIS_BUY_ORDER_MODE", "BEST_LIMIT"), "BUY", current, reference_price,
            )
        if order_mode == "HOLD":
            order_log.warning(
                "BUY 보류 symbol=%s reference=%.0f current=%.0f reason=%s",
                symbol_label(pick.symbol, pick.name), reference_price, current, mode_reason,
            )
            qty = 0
            budget_reason = mode_reason

        # POSITION_SIZING=false 및 주문 계산 로그 비활성 조합에서도 예약 단가가 정의되도록 설정.
        fee_pct = max(0.0, env_float("KIS_FEE_RATE_PCT", 0.02))
        market_buffer = max(0.0, env_float("KIS_MARKET_PRICE_BUFFER_PCT", 1.0)) if order_mode == "MARKET" else 0.0
        if qty > 0:
            try:
                # 계좌 주문가능금액 + 종목별 매수가능조회 + 미수 없는 매수한도를 모두 확인합니다.
                _holdings, summary = self.api.balance()
                account_orderable = summary_value(summary, "ord_psbl_cash", "ORD_PSBL_CASH")
                deposit = summary_value(summary, "dnca_tot_amt", "DNCA_TOT_AMT")
                if account_orderable <= 0:
                    update_buy_safety(balance_ok=False, reason="매수 직전 KIS 주문가능금액(ord_psbl_cash) 누락 또는 0원")
                    raise ValueError("KIS 주문가능금액(ord_psbl_cash) 확인 실패/0원; 신규 매수 차단")
                update_buy_safety(balance_ok=True)
                fee_pct = max(0.0, env_float("KIS_FEE_RATE_PCT", 0.02))
                market_buffer = max(0.0, env_float("KIS_MARKET_PRICE_BUFFER_PCT", 1.0)) if order_mode == "MARKET" else 0.0
                sizing_price = quote_price_for_order(fresh_quote, order_mode, current)
                reserved_gross, unknown_reservations = self.orders.pending_buy_reservation()
                if unknown_reservations:
                    raise ValueError(
                        f"미체결 매수 예약가격 확인 불가 주문 {unknown_reservations}건; "
                        "현금 예약액을 안전하게 계산할 수 없어 매수 차단"
                    )
                reserved_cash = reserved_gross
                symbol_buyable = dict(self.api.orderable_cash(pick.symbol, int(sizing_price), order_mode=order_mode) or {})
                symbol_orderable = summary_value(symbol_buyable, "ord_psbl_cash", "ORD_PSBL_CASH")
                no_credit_cash = summary_value(symbol_buyable, "nrcvb_buy_amt", "NRCVB_BUY_AMT")
                no_credit_qty = int(summary_value(symbol_buyable, "nrcvb_buy_qty", "NRCVB_BUY_QTY"))
                if symbol_orderable <= 0 or no_credit_cash <= 0 or no_credit_qty <= 0:
                    raise ValueError(
                        "종목별 매수가능조회에서 주문가능현금 또는 미수 없는 매수금액/수량을 "
                        f"확인하지 못했습니다: ord_psbl_cash={symbol_orderable:.0f}, "
                        f"nrcvb_buy_amt={no_credit_cash:.0f}, nrcvb_buy_qty={no_credit_qty}; 신규 매수 차단"
                    )
                # 미수 사용 금지: 계좌/종목 주문가능금액과 미수 없는 매수금액 중 가장 작은 값을 상한으로 둡니다.
                effective_cash_limit = min(account_orderable, symbol_orderable, no_credit_cash)
                sizing_summary = dict(summary)
                sizing_summary["ord_psbl_cash"] = str(min(account_orderable, symbol_orderable))
                position_sizing_enabled = os.getenv("POSITION_SIZING", "true").strip().lower() in {"1", "true", "yes", "on"}
                effective_positions = len(set(self.store.symbols()) | self.orders.pending_buy_symbols())
                max_positions = max(1, int(os.getenv("MAX_POSITIONS", "3")))
                min_order_amount = float(os.getenv("MIN_BUY_ORDER_AMOUNT_KRW", os.getenv("MIN_ORDER_AMOUNT_KRW", "10000")))
                if position_sizing_enabled:
                    qty, budget, budget_reason = calculate_buy_qty(
                        sizing_summary, effective_positions, sizing_price,
                        max_positions=max_positions, min_order_amount=min_order_amount,
                        max_single_asset_ratio=None, fee_rate_pct=fee_pct,
                        price_buffer_pct=market_buffer, reserved_cash=reserved_cash,
                        cash_basis="orderable", buyable_cash_limit=no_credit_cash,
                        buyable_qty_limit=no_credit_qty,
                    )
                else:
                    available_cash = max(0.0, effective_cash_limit - reserved_cash)
                    estimated_unit_cost = sizing_price * (1.0 + market_buffer / 100.0) * (1.0 + fee_pct / 100.0)
                    estimated_cost = qty * estimated_unit_cost
                    qty = min(qty, no_credit_qty)
                    if estimated_cost > available_cash or qty <= 0:
                        qty = 0
                        budget_reason = f"고정수량 예상금액 {estimated_cost:.0f}원 또는 미수 없는 수량 한도 초과; 가용현금={available_cash:.0f}원"
                    else:
                        budget_reason = f"고정수량 현금검증 통과: 예상금액={estimated_cost:.0f}, 가용현금={available_cash:.0f}, no_credit_qty={no_credit_qty}"
                    budget = available_cash
                sizing_context = {
                    "summary": dict(summary), "symbol_buyable": dict(symbol_buyable),
                    "effective_positions": effective_positions, "max_positions": max_positions,
                    "min_order_amount": min_order_amount, "reserved_cash": reserved_cash,
                    "position_sizing_enabled": position_sizing_enabled,
                    "fee_pct": fee_pct, "market_buffer": market_buffer,
                    "sizing_price": sizing_price, "original_qty": qty,
                }
                order_log.info(
                    "BUYING_POWER_AUDIT symbol=%s order_mode=%s deposit=%.0f account_ord_psbl_cash=%.0f "
                    "symbol_ord_psbl_cash=%.0f nrcvb_buy_amt=%.0f nrcvb_buy_qty=%d "
                    "effective_cash_limit=%.0f reserved_buy_cash=%.0f sizing_basis=orderable_cash",
                    symbol_label(pick.symbol, pick.name), order_mode, deposit, account_orderable,
                    symbol_orderable, no_credit_cash, no_credit_qty, effective_cash_limit, reserved_cash,
                )
                if show_order_calculation():
                    log.info(
                        "포지션 사이징 symbol=%s mode=%s current=%.0f sizing_price=%.0f qty=%d budget=%.0f "
                        "reserved_cash=%.0f fee_pct=%.4f buffer_pct=%.2f detail=%s",
                        symbol_label(pick.symbol, pick.name), order_mode, current, sizing_price, qty,
                        budget, reserved_cash, fee_pct, market_buffer, budget_reason,
                    )
            except Exception as exc:
                update_buy_safety(balance_ok=False, reason=f"매수 직전 계좌/종목별 매수가능조회 실패: {type(exc).__name__}")
                log.exception("포지션 사이징/계좌·종목별 매수가능조회 실패: 신규 매수 차단 및 다음 정상 대사까지 차단 유지")
                qty = 0
                budget_reason = f"계좌/종목별 매수가능조회 실패: {type(exc).__name__}: {exc}"

        return {
            "qty": qty, "budget_reason": budget_reason, "reference_price": reference_price,
            "current": current, "fresh_quote": fresh_quote, "sizing_price": sizing_price,
            "order_mode": order_mode, "mode_reason": mode_reason,
            "fee_pct": fee_pct, "market_buffer": market_buffer,
            "sizing_context": locals().get("sizing_context"),
        }

    def submit_candidate_order(
        self, *, pick, active_style: str, qty: int, order_mode: str,
        sizing_price: float, market_buffer_pct: float, fee_pct: float,
        daily_buy_limit_latched: bool, stop_event, market_open_window: Callable[[], bool],
        update_buy_safety: Callable[..., Any], symbol_label: Callable[..., str],
        log, order_log, telegram, settings, env_float: Callable[..., float],
        sizing_context: dict[str, Any] | None = None, calculate_buy_qty: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """마지막 BUY 안전 게이트와 공통 OrderManager 제출을 수행한다.

        매수 수량·현금 검증은 기존 계산 경로에서 끝난 값만 받는다. 이 메서드는
        주문 직전 종료/시간/일일한도 재확인, 주문 예약, 안전 잠금, 알림과 대기시간
        갱신을 한 곳에서 처리해 main 루프와 주문 제출 경계를 분리한다.
        """
        result = {
            "submitted": False,
            "daily_buy_limit_latched": daily_buy_limit_latched,
            "buy_pause_until": None,
            "order": None,
        }
        label = symbol_label(pick.symbol, pick.name)
        if settings.enable_trading and getattr(self.orders, "account_order_locked", False):
            update_buy_safety(orders_ok=False, reason="계좌 전체 주문 잠금: 운영자 검증/해제 전 신규 매수 차단")
            log.warning("계좌 전체 주문 잠금으로 신규 BUY 제출 차단: %s", label)
            return result
        if settings.enable_trading and stop_event.is_set():
            log.warning("종료 신호 확인: 신규 BUY 제출 차단 symbol=%s", label)
        elif settings.enable_trading and not market_open_window():
            log.warning(
                "신규 매수 주문 최종 시간 게이트 차단: %s 현재 KST=%s 허용=%s~%s AUTO_SHUTDOWN=%s",
                label, datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S"),
                os.getenv("ENTRY_START_TIME", "09:10"), os.getenv("ENTRY_END_TIME", "15:10"),
                os.getenv("AUTO_SHUTDOWN_TIME", "15:10"),
            )
        elif settings.enable_trading and (
            daily_buy_limit_latched or self.store.state.trades_today >= settings.max_trades_per_day
        ):
            # 전략/계좌 조회 중 카운터가 한도에 도달하는 경합도 POST 직전에 차단한다.
            result["daily_buy_limit_latched"] = True
            log.warning(
                "신규 매수 주문 최종 한도 게이트 차단: %s trades_today=%d limit=%d",
                label, self.store.state.trades_today, settings.max_trades_per_day,
            )
        elif settings.enable_trading:
            reserve_price_per_share = sizing_price * (1.0 + market_buffer_pct / 100.0) * (1.0 + fee_pct / 100.0)
            request_started_ts = time.time()
            try:
                order = self.orders.submit(
                    pick.symbol, "BUY", qty, strategy=active_style, order_mode=order_mode,
                    reservation_price=reserve_price_per_share,
                    position_qty_before_submit=int((self.store.get_position(pick.symbol) or {}).get("qty", 0) or 0),
                    stop_event=stop_event,
                )
            except InsufficientBuyingPowerError as first_error:
                # 주문가능금액 부족이라는 KIS 명시 거절만 fallback 대상입니다.
                # 먼저 주문이력에서 미접수를 확인하지 못하면 재주문하지 않습니다.
                order_log.warning(
                    "KIS 현금매수 한도 부족 명시 거절: symbol=%s msg_cd=%s msg1=%s; 주문이력 미접수 확인 시작",
                    label, first_error.msg_cd or "unknown", first_error.msg1 or str(first_error),
                )
                if not self.orders.confirm_rejected_order_absent(
                    pick.symbol, "BUY", qty, request_started_ts, first_error,
                ):
                    update_buy_safety(orders_ok=False, reason="매수 거절 후 주문이력 미접수 확인 실패; 동일 종목 재주문 잠금")
                    return result
                if not sizing_context:
                    order_log.error("예수금 fallback 계산자료가 없어 재주문하지 않습니다: symbol=%s", label)
                    if hasattr(self.store, "mark_buy_rejection_block"):
                        self.store.mark_buy_rejection_block(pick.symbol, "buying_power_rejected_without_fallback_context", datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"))
                    return result
                try:
                    _fresh_holdings, fresh_summary = self.api.balance()
                    fresh_symbol = dict(self.api.orderable_cash(
                        pick.symbol, int(sizing_context["sizing_price"]), order_mode=order_mode,
                    ) or {})
                    fresh_account_orderable = float(str(fresh_summary.get("ord_psbl_cash", fresh_summary.get("ORD_PSBL_CASH", 0)) or 0).replace(",", ""))
                    fresh_deposit = float(str(fresh_summary.get("dnca_tot_amt", fresh_summary.get("DNCA_TOT_AMT", 0)) or 0).replace(",", ""))
                    fresh_symbol_orderable = float(str(fresh_symbol.get("ord_psbl_cash", fresh_symbol.get("ORD_PSBL_CASH", 0)) or 0).replace(",", ""))
                    fresh_no_credit_cash = float(str(fresh_symbol.get("nrcvb_buy_amt", fresh_symbol.get("NRCVB_BUY_AMT", 0)) or 0).replace(",", ""))
                    fresh_no_credit_qty = int(float(str(fresh_symbol.get("nrcvb_buy_qty", fresh_symbol.get("NRCVB_BUY_QTY", 0)) or 0).replace(",", "")))
                    if min(fresh_account_orderable, fresh_deposit, fresh_symbol_orderable, fresh_no_credit_cash) <= 0 or fresh_no_credit_qty <= 0:
                        raise ValueError("예수금 fallback 또는 미수 없는 매수가능조회 값이 0원/누락")
                    fresh_reserved, unknown = self.orders.pending_buy_reservation()
                    if unknown:
                        raise ValueError(f"fallback 중 미체결 매수 예약가격 확인 불가 {unknown}건")
                    fallback_cash_limit = min(fresh_account_orderable, fresh_symbol_orderable, fresh_no_credit_cash)
                    fallback_summary = dict(fresh_summary)
                    fallback_summary["ord_psbl_cash"] = str(min(fresh_account_orderable, fresh_symbol_orderable))
                    if sizing_context.get("position_sizing_enabled"):
                        if calculate_buy_qty is None:
                            from risk.position_sizer import calculate_buy_qty as calculate_buy_qty_impl
                            calculate_buy_qty = calculate_buy_qty_impl
                        retry_qty, retry_budget, retry_reason = calculate_buy_qty(
                            fallback_summary, len(set(self.store.symbols()) | self.orders.pending_buy_symbols()),
                            float(sizing_context["sizing_price"]),
                            max_positions=int(sizing_context["max_positions"]),
                            min_order_amount=float(sizing_context["min_order_amount"]),
                            fee_rate_pct=float(sizing_context["fee_pct"]),
                            price_buffer_pct=float(sizing_context["market_buffer"]),
                            reserved_cash=fresh_reserved, cash_basis="deposit",
                            buyable_cash_limit=fallback_cash_limit,
                            buyable_qty_limit=fresh_no_credit_qty,
                        )
                    else:
                        unit_cost = float(sizing_context["sizing_price"]) * (1.0 + float(sizing_context["market_buffer"]) / 100.0)
                        unit_cost *= 1.0 + float(sizing_context["fee_pct"]) / 100.0
                        available = max(0.0, min(fresh_deposit, fallback_cash_limit) - fresh_reserved)
                        retry_qty = min(int(qty), fresh_no_credit_qty, int(available // unit_cost) if unit_cost > 0 else 0)
                        retry_budget = available
                        retry_reason = f"deposit_fallback available={available:.0f} no_credit_qty={fresh_no_credit_qty}"
                    if retry_qty <= 0 or retry_qty >= int(qty):
                        order_log.error(
                            "예수금 fallback 재계산 후 안전한 축소 수량이 없어 재주문 중단: symbol=%s original_qty=%d retry_qty=%d reason=%s",
                            label, qty, retry_qty, retry_reason,
                        )
                        if hasattr(self.store, "mark_buy_rejection_block"):
                            self.store.mark_buy_rejection_block(pick.symbol, "no_safe_reduced_qty_after_buying_power_rejection", datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"))
                        return result
                    order_log.warning(
                        "예수금 fallback 재계산: symbol=%s original_qty=%d retry_qty=%d deposit=%.0f account_orderable=%.0f symbol_orderable=%.0f no_credit_cash=%.0f no_credit_qty=%d budget=%.0f reason=%s",
                        label, qty, retry_qty, fresh_deposit, fresh_account_orderable,
                        fresh_symbol_orderable, fresh_no_credit_cash, fresh_no_credit_qty,
                        retry_budget, retry_reason,
                    )
                    qty = retry_qty
                    reserve_price_per_share = float(sizing_context["sizing_price"]) * (
                        1.0 + float(sizing_context["market_buffer"]) / 100.0
                    ) * (1.0 + float(sizing_context["fee_pct"]) / 100.0)
                except Exception as fallback_error:
                    update_buy_safety(balance_ok=False, reason=f"예수금 fallback 재조회/계산 실패: {type(fallback_error).__name__}")
                    order_log.exception("예수금 fallback 재계산 실패; 추가 주문 금지 symbol=%s", label)
                    return result
                # 단 한 번만 재주문합니다. 두 번째 거절/모든 기타 오류는 추가 재시도하지 않습니다.
                try:
                    order = self.orders.submit(
                        pick.symbol, "BUY", qty, strategy=active_style, order_mode=order_mode,
                        reservation_price=reserve_price_per_share,
                        position_qty_before_submit=int((self.store.get_position(pick.symbol) or {}).get("qty", 0) or 0),
                        stop_event=stop_event,
                    )
                except InsufficientBuyingPowerError as second_error:
                    reason = f"second_buy_attempt_rejected:{second_error.msg_cd or 'unknown'}:{second_error.msg1 or str(second_error)}"
                    order_log.error(
                        "예수금 fallback 2차 주문도 명시 거절; 당일 종목 잠금: symbol=%s msg_cd=%s msg1=%s",
                        label, second_error.msg_cd or "unknown", second_error.msg1 or str(second_error),
                    )
                    if hasattr(self.store, "mark_buy_rejection_block"):
                        self.store.mark_buy_rejection_block(pick.symbol, reason, datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"))
                    return result
                except Exception as second_error:
                    reason = f"second_buy_attempt_failed:{type(second_error).__name__}"
                    order_log.exception("예수금 fallback 2차 주문 실패; 당일 종목 잠금 symbol=%s", label)
                    if hasattr(self.store, "mark_buy_rejection_block"):
                        self.store.mark_buy_rejection_block(pick.symbol, reason, datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"))
                    update_buy_safety(orders_ok=False, reason="예수금 fallback 2차 주문 결과 불명확/실패; 주문 대사 대기")
                    return result
            order_log.warning(
                "BUY 접수 symbol=%s qty=%d mode=%s score=%.2f order=%s "
                "reserve_per_share=%.2f trades_today_before=%d limit=%d latch=%s paper=%s",
                label, qty, order_mode, pick.score, order.order_no, reserve_price_per_share,
                self.store.state.trades_today, settings.max_trades_per_day,
                result["daily_buy_limit_latched"], settings.paper,
            )
            # 주문 전 잔고 스냅샷에 이번 주문이 반영되지 않았을 수 있으므로 재대사까지 잠근다.
            update_buy_safety(balance_ok=False, reason="매수 주문 제출 후 최신 잔고 재대사 대기")
            telegram.send(
                f"매수 주문 접수\n환경={'모의투자' if settings.paper else '실전'}\n"
                f"종목={pick.name}({pick.symbol})\n수량={qty}\n방식={order_mode}\n"
                f"점수={pick.score:.2f}\n사유={pick.reason}\n주문번호={order.order_no}"
            )
            post_sleep = max(0.0, env_float("BUY_ORDER_POST_SLEEP_SEC", 60.0))
            if post_sleep > 0:
                result["buy_pause_until"] = time.monotonic() + post_sleep
                order_log.info(
                    "매수 주문 후 신규 매수 대기 시작: %.0f초; 매도/잔고/주문 대사는 계속 실행합니다.",
                    post_sleep,
                )
            result.update(submitted=True, order=order)
        else:
            order_log.warning(
                "[DRY-RUN] BUY symbol=%s score=%.1f mode=%s qty=%d reason=%s",
                label, pick.score, order_mode, qty, pick.reason,
            )
        return result

