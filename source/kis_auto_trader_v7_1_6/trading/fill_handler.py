"""체결 결과를 로컬 포지션 상태에 반영하는 공통 처리 로직.

주문 접수/체결 통보 해석은 trading.orders.OrderManager가 담당하고,
이 모듈은 OrderManager가 만든 FillEvent의 로컬 반영만 담당한다.
"""
from __future__ import annotations

import logging
from typing import Callable, Any

from trading.orders import FillEvent

log = logging.getLogger(__name__)
order_log = logging.getLogger("trading.order")
fill_log = logging.getLogger("trading.fill")

def apply_fill_state(
    store: Any,
    engine: Any,
    fill: FillEvent,
    *,
    load_profile_risk: Callable[[str], dict],
    env_float: Callable[[str, float], float],
    symbol_name: Callable[[str], str],
    symbol_label: Callable[..., str],
) -> None:
    """실제 체결통보를 로컬 포지션에 반영한다.

    주문 상태 머신과 잔고 대사는 수행하지 않는다. 중복 반영 여부는 호출자가
    확인하고, 이 함수는 반영하기로 결정된 단일 FillEvent만 처리한다.
    """

    pos = store.get_position(fill.symbol)

    if fill.side == "BUY":
        if pos:
            old_qty = int(pos.get("qty", 0) or 0)
            old_avg = float(pos.get("avg_price", 0) or 0)
            new_qty = old_qty + fill.qty
            new_avg = ((old_avg * old_qty) + (fill.price * fill.qty)) / new_qty if new_qty else fill.price
            pos["qty"] = new_qty
            pos["avg_price"] = new_avg
            pos["high_price"] = max(float(pos.get("high_price", 0) or 0), fill.price)
            pos["pending_order_no"] = ""
            pos["pending_side"] = ""
            pos["exit_retry_after_ts"] = 0.0
            if not pos.get("strategy_risk"):
                pos["strategy_risk"] = load_profile_risk(fill.strategy)
        else:
            pos = {
                "symbol": fill.symbol,
                "name": symbol_name(fill.symbol),
                "qty": fill.qty,
                "avg_price": fill.price,
                "high_price": fill.price,
                "pending_order_no": "",
                "pending_side": "",
                "strategy": fill.strategy,
                "strategy_risk": load_profile_risk(fill.strategy),
                "last_exit_ts": 0.0,
                "exit_retry_after_ts": 0.0,
            }
        store.set_position(fill.symbol, pos)
        if fill.first_fill:
            store.increment_trade_count()
        log.info(
            "BUY 체결 반영: order=%s symbol=%s delta=%d cumulative=%d remaining=%d price=%.2f strategy=%s",
            fill.order_no, symbol_label(fill.symbol, pos.get("name", "")), fill.qty, fill.cumulative_filled_qty, fill.remaining_qty, fill.price, fill.strategy,
        )
        store.save()
        return

    # SELL 체결
    if not pos:
        log.warning("SELL 체결인데 로컬 포지션 없음: %s", symbol_label(fill.symbol))
        return

    old_qty = int(pos.get("qty", 0) or 0)
    fill_qty = min(int(fill.qty), old_qty)
    remain = max(0, old_qty - fill_qty)
    avg = float(pos.get("avg_price", 0) or 0)
    fee_pct = max(0.0, env_float("KIS_FEE_RATE_PCT", 0.02)) / 100.0
    sell_tax_pct = max(0.0, env_float("KIS_SELL_TAX_RATE_PCT", 0.20)) / 100.0
    gross_pnl = (fill.price - avg) * fill_qty
    buy_fee_allocated = avg * fill_qty * fee_pct
    sell_fee = fill.price * fill_qty * fee_pct
    sell_tax = fill.price * fill_qty * sell_tax_pct
    net_pnl = gross_pnl - buy_fee_allocated - sell_fee - sell_tax
    store.add_realized_pnl_krw(net_pnl)
    store.save()

    if remain == 0:
        strategy = str(pos.get("strategy", "unified"))
        cooldown = max(30000, int(env_float("REENTRY_COOLDOWN_SEC", 30000)))
        daily_loss_pct = store.daily_loss_pct()
        daily_loss_label = f"{daily_loss_pct:.3f}%" if daily_loss_pct != float("-inf") else "N/A(기준자산 미확인)"
        log.info("SELL 청산: order=%s symbol=%s delta=%d cumulative=%d remaining=%d net_pnl=%.0f원 daily_loss_pct=%s cooldown=%ds",
                 fill.order_no, symbol_label(fill.symbol, pos.get("name", "")), fill.qty, fill.cumulative_filled_qty, fill.remaining_qty,
                 net_pnl, daily_loss_label, cooldown)
        store.remove_position(fill.symbol)
        store.mark_exit_cooldown(fill.symbol, cooldown)
    else:
        pos["qty"] = remain
        pos["pending_order_no"] = ""
        pos["pending_side"] = ""
        # 부분 매도 체결이 확인되면 이전 미체결 재시도 누적을 초기화합니다.
        pos["sell_retry_count"] = 0
        pos["exit_retry_after_ts"] = 0.0
        store.set_position(fill.symbol, pos)
        log.info("SELL 부분체결: order=%s symbol=%s delta=%d cumulative=%d order_remaining=%d position_remain=%d price=%.2f net_pnl=%.0f원",
                 fill.order_no, symbol_label(fill.symbol, pos.get("name", "")), fill.qty, fill.cumulative_filled_qty, fill.remaining_qty,
                 remain, fill.price, net_pnl)

    store.save()



class FillHandler:
    """체결 이벤트의 중복 판정, 상태 반영, 로그/알림을 조정한다.

    OrderManager는 주문 통보를 FillEvent로 해석하고, 이 클래스는 그 이벤트를
    로컬 상태에 반영한다. 잔고 대사 자체는 수행하지 않는다.
    """
    def __init__(
        self, *, store, engine, orders, realtime, telegram, settings,
        fill_apply_lock, log_post_fill_positions, fill_may_already_be_in_balance,
        apply_fill_callback, register_symbol, symbol_label,
    ) -> None:
        self.store = store
        self.engine = engine
        self.orders = orders
        self.realtime = realtime
        self.telegram = telegram
        self.settings = settings
        self.fill_apply_lock = fill_apply_lock
        self.log_post_fill_positions = log_post_fill_positions
        self.fill_may_already_be_in_balance = fill_may_already_be_in_balance
        self.apply_fill_callback = apply_fill_callback
        self.register_symbol = register_symbol
        self.symbol_label = symbol_label

    def apply_fill_event(self, fill: FillEvent) -> None:
        """체결 상태 변경을 직렬화하고, 신규/추가 매수 보유 종목을 실시간 구독합니다."""
        with self.fill_apply_lock:
            if fill.side == "BUY":
                # 상태 반영이 대사 뒤로 지연되더라도 체결 종목의 시세 구독은 먼저 요청합니다.
                self.realtime.subscribe_symbols([fill.symbol])
            # 체결 알림은 REST 잔고에 체결이 먼저 반영된 경우에도 반드시 발송합니다.
            # 다만 상태를 다시 더하면 보유수량이 중복되므로 상태 반영은 별도로 판단합니다.
            current_pos = self.store.get_position(fill.symbol) or {}
            display_name = str(current_pos.get("name", "") or "").strip()
            code_name = self.symbol_label(fill.symbol, display_name)
            total_order_qty = int(fill.cumulative_filled_qty) + int(fill.remaining_qty)
            fill_amount = float(fill.price) * int(fill.qty)
            order_log.warning(
                f"{fill.side} 실체결 order={fill.order_no} symbol={code_name} 총주문수량={total_order_qty} "
                f"이번체결={fill.qty} 누적체결={fill.cumulative_filled_qty} 주문잔량={fill.remaining_qty} "
                f"체결가={fill.price:,.0f} 체결금액={fill_amount:,.0f}")
            fill_log.info(
                f"실제체결 구분={'매수' if fill.side == 'BUY' else '매도'} 종목={code_name} 주문번호={fill.order_no} "
                f"주문수량={total_order_qty} 이번체결수량={fill.qty} 누적체결수량={fill.cumulative_filled_qty} "
                f"주문잔량={fill.remaining_qty} 체결단가={fill.price:,.0f}원 이번체결금액={fill_amount:,.0f}원 "
                f"최근체결가×누적수량(참고계산, 실제 누적체결금액 아님)={float(fill.price) * int(fill.cumulative_filled_qty):,.0f}원")
            order_log.info(
                "FILL_AMOUNT_AUDIT order=%s side=%s symbol=%s fill_qty=%d fill_price=%.2f fill_amount=%.0f cumulative_qty=%d latest_price_times_cumulative_qty=%.0f cumulative_actual_amount=UNKNOWN reason=FillEvent_has_no_per_order_cumulative_amount",
                fill.order_no, fill.side, code_name, int(fill.qty), float(fill.price), fill_amount,
                int(fill.cumulative_filled_qty), float(fill.price) * int(fill.cumulative_filled_qty))
            self.telegram.send(f"주문 체결 통보\n환경={'모의투자' if self.settings.paper else '실전'}\n구분={'매수' if fill.side == 'BUY' else '매도'}\n종목={code_name}\n총 주문수량={total_order_qty}주\n이번 체결수량={fill.qty}주\n누적 체결수량={fill.cumulative_filled_qty}주\n체결단가={fill.price:,.0f}원\n이번 체결금액={fill_amount:,.0f}원\n주문잔량={fill.remaining_qty}주\n주문번호={fill.order_no}")
            if self.fill_may_already_be_in_balance(fill.event_ts, self.orders.last_balance_reconcile_ts):
                # REST 응답이 체결 이후에 끝났더라도 KIS 잔고 응답 자체가 지연 반영일 수 있습니다.
                # 타임스탬프만으로 중복을 판정하지 않고, 현재 로컬 잔고가 누적 체결량을 실제로 포함하는지 확인합니다.
                current_qty = int(current_pos.get("qty", 0) or 0)
                baseline_qty = max(0, int(getattr(fill, "position_qty_before_submit", 0) or 0))
                cumulative_qty = max(0, int(fill.cumulative_filled_qty or fill.qty))
                if fill.side == "BUY":
                    snapshot_reflects_fill = current_qty >= baseline_qty + cumulative_qty
                else:
                    expected_remaining = max(0, baseline_qty - cumulative_qty)
                    snapshot_reflects_fill = current_qty <= expected_remaining
                if snapshot_reflects_fill:
                    log.info("체결 이벤트 중복 반영 생략: REST 잔고 수량이 누적체결을 반영 order=%s symbol=%s side=%s current_qty=%d baseline_qty=%d cumulative=%d event_ts=%.3f balance_ts=%.3f",
                             fill.order_no, code_name, fill.side, current_qty, baseline_qty, cumulative_qty,
                             fill.event_ts, self.orders.last_balance_reconcile_ts)
                    # REST 잔고가 포지션을 먼저 반영한 경우에도 당일 매수 횟수는 첫 체결에서 집계해야 합니다.
                    # 포지션 수량은 건드리지 않고 카운터만 갱신하여 다음 매수 한도 검사에 반영합니다.
                    if fill.side == "BUY" and fill.first_fill:
                        self.store.increment_trade_count()
                        self.store.save()
                        log.info("당일 매수 거래횟수 반영: order=%s symbol=%s trades_today=%d (REST 잔고 체결 선반영)",
                                 fill.order_no, code_name, self.store.state.trades_today)
                    self.log_post_fill_positions(fill.symbol, fill.price)
                    return
                log.warning("체결 이후 REST 잔고가 아직 누적체결을 반영하지 않아 로컬 체결 반영: order=%s symbol=%s side=%s current_qty=%d baseline_qty=%d cumulative=%d event_ts=%.3f balance_ts=%.3f",
                            fill.order_no, code_name, fill.side, current_qty, baseline_qty, cumulative_qty,
                            fill.event_ts, self.orders.last_balance_reconcile_ts)
            self.apply_fill_callback(self.store, self.engine, fill)
            updated_pos = self.store.get_position(fill.symbol) or {}
            if updated_pos.get("name"):
                self.register_symbol(fill.symbol, updated_pos.get("name", ""))
            self.log_post_fill_positions(fill.symbol, fill.price)
