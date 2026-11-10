from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from trading.sell_process import SellProcess
from trading.submission_guard import SymbolSubmissionGuard


class FakeStore:
    def __init__(self):
        self.positions = {
            "005930": {
                "symbol": "005930", "name": "삼성전자", "qty": 3,
                "avg_price": 100.0, "high_price": 100.0,
                "strategy": "stable", "yaml_exit_eval_ts": 10**9,
                "exit_retry_after_ts": 0.0,
            }
        }
        self.saved = 0

    def symbols(self):
        return list(self.positions)

    def get_position(self, symbol):
        value = self.positions.get(symbol)
        return dict(value) if value else None

    def set_position(self, symbol, value):
        self.positions[symbol] = dict(value)

    def save(self):
        self.saved += 1


class FakeOrders:
    def __init__(self):
        self.submissions = []

    def pending_for_symbol(self, symbol):
        return False

    def submit(self, symbol, side, qty, **kwargs):
        self.submissions.append((symbol, side, qty, kwargs))
        return SimpleNamespace(order_no="MOCK-ORDER-1")


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)


def test_general_sell_process_submits_through_shared_order_manager(monkeypatch):
    monkeypatch.setenv("EXIT_STRATEGY_FILE", "/path/that/does/not/exist.yaml")
    store = FakeStore()
    orders = FakeOrders()
    telegram = FakeTelegram()
    quote = {"STCK_PRPR": "90", "_received_ts": 999.0}
    process = SellProcess(
        store=store, orders=orders,
        settings=SimpleNamespace(
            quote_stale_sec=5.0, quote_rest_min_interval_sec=1.0,
            sell_retry_cooldown_sec=2.0, enable_trading=True,
        ),
        stop_event=threading.Event(), exit_queue=queue.PriorityQueue(),
        price_cache={"005930": quote}, price_cache_lock=threading.RLock(),
        exit_queue_lock=threading.RLock(), exit_queued_symbols=set(),
        submission_guard=SymbolSubmissionGuard(), telegram=telegram,
        dry_run_logger=lambda *args, **kwargs: None,
        env_float=lambda _name, default: default,
        price_value=lambda row: float(row.get("STCK_PRPR", 0) or 0),
        position_strategy_risk_exit=lambda _pos, _price: (True, "STOP_LOSS"),
        evaluate_exit_priority=lambda *_args, **_kwargs: (False, ""),
        sell_order_window=lambda: True,
        is_stop_reason=lambda reason: "STOP" in reason,
        resolve_order_mode=lambda *_args, **_kwargs: ("MARKET", "test policy"),
        sell_retry_delay=lambda _exc, default: default,
        symbol_label=lambda symbol, name="": f"{name or symbol}({symbol})",
    )

    reconciled = process.process_positions(
        now=1000.0, api=SimpleNamespace(), last_rest_quote={},
        yaml_exit_futures={}, yaml_exit_executor=SimpleNamespace(),
        fetch_yaml_exit=lambda *_args: None,
        reconcile_and_subscribe=lambda: None,
        show_order_calculation=lambda: False,
        quote_sell_price_for_order=lambda _quote, _mode, current: current,
        is_no_broker_position_error=lambda _exc: False,
    )

    assert reconciled is False
    assert len(orders.submissions) == 1
    symbol, side, qty, kwargs = orders.submissions[0]
    assert (symbol, side, qty) == ("005930", "SELL", 3)
    assert kwargs["order_mode"] == "MARKET"
    assert kwargs["position_qty_before_submit"] == 3
    assert store.positions["005930"]["pending_order_no"] == "MOCK-ORDER-1"
    assert store.positions["005930"]["pending_side"] == "SELL"
    assert not process.submission_guard.is_reserved("005930")
    assert any("매도 주문 접수" in message for message in telegram.messages)
