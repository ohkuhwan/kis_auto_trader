from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from trading.buy_process import BuyProcess


def _make_process(monkeypatch, mode, hts_result=None, builder_result=None, hts_candidates=None):
    calls = {"hts": [], "builder": []}
    settings = SimpleNamespace(
        condition_name="AUTO_CONDITION",
        max_candidates=20, quote_stale_sec=5.0, quote_rest_min_interval_sec=1.0,
    )
    def hts(*args, **kwargs):
        calls["hts"].append((args, kwargs))
        kwargs["candidate_sink"].extend(hts_candidates or [])
        return hts_result
    def builder(*args, **kwargs):
        calls["builder"].append((args, kwargs))
        return builder_result
    process = BuyProcess(
        api=object(), store=object(), engine=object(), orders=object(), realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={}, settings=settings,
        run_hts_cycle=hts, run_builder_cycle=builder,
    )
    monkeypatch.setenv("ENTRY_MODE", mode)
    return process, calls


def test_hts_mode_uses_hts_only_as_candidate_source(monkeypatch):
    hts_candidate = SimpleNamespace(symbol="005930")
    selected = SimpleNamespace(symbol="005930", score=65)
    process, calls = _make_process(monkeypatch, "HTS", hts_result=hts_candidate,
                                   builder_result=selected, hts_candidates=[hts_candidate])
    style, pick = process.select_candidate(active_style="aggressive")
    assert style == "unified"
    assert pick is selected
    assert len(calls["hts"]) == 1
    assert len(calls["builder"]) == 1
    assert calls["hts"][0][0][9] == "AUTO_CONDITION"
    assert calls["builder"][0][1]["include_manual"] is False
    assert calls["builder"][0][1]["extra_symbols"] == ["005930"]


def test_all_entry_mode_unions_hts_and_manual_candidate_sources(monkeypatch):
    hts_candidate = SimpleNamespace(symbol="005930")
    selected = SimpleNamespace(symbol="000660", score=75)
    process, calls = _make_process(monkeypatch, "ALL", hts_result=hts_candidate,
                                   builder_result=selected, hts_candidates=[hts_candidate])
    style, pick = process.select_candidate(active_style="stable")
    assert style == "unified"
    assert pick is selected
    assert calls["builder"][0][1]["extra_symbols"] == ["005930"]
    assert calls["builder"][0][1]["include_manual"] is True


def test_builder_entry_mode_uses_manual_candidate_source(monkeypatch):
    selected = SimpleNamespace(symbol="000660", score=75)
    process, calls = _make_process(monkeypatch, "BUILDER", builder_result=selected)
    style, pick = process.select_candidate(active_style="aggressive")
    assert style == "unified"
    assert pick is selected
    assert not calls["hts"]
    assert calls["builder"][0][1]["profile_name"] == "unified"
    assert calls["builder"][0][1]["include_manual"] is True


def test_invalid_entry_mode_raises(monkeypatch):
    process, calls = _make_process(monkeypatch, "INVALID")

    with pytest.raises(ValueError, match="ENTRY_MODE"):
        process.select_candidate(active_style="stable")

    assert not calls["hts"]
    assert not calls["builder"]


def _submission_process(monkeypatch, *, trading=True, trades_today=0, max_trades=5):
    class Orders:
        def __init__(self):
            self.calls = []
        def submit(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(order_no="PAPER-123")
        def pending_buy_reservation(self):
            return (0.0, 0)
        def pending_buy_symbols(self):
            return set()

    store = SimpleNamespace(
        state=SimpleNamespace(trades_today=trades_today),
        get_position=lambda symbol: None,
        symbols=lambda: [],
    )
    orders = Orders()
    settings = SimpleNamespace(enable_trading=trading, max_trades_per_day=max_trades, paper=True, order_qty=2)
    process = BuyProcess(
        api=SimpleNamespace(), store=store, engine=object(), orders=orders, realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={}, settings=settings,
        run_hts_cycle=lambda *a, **k: None, run_builder_cycle=lambda *a, **k: None,
    )
    pick = SimpleNamespace(symbol="005930", name="삼성전자", score=42.5, reason="테스트", price=70000)
    calls = {"safety": [], "telegram": [], "logs": []}
    class Logger:
        def warning(self, *args, **kwargs): calls["logs"].append(args)
        def info(self, *args, **kwargs): calls["logs"].append(args)
        def exception(self, *args, **kwargs): calls["logs"].append(args)
    class Telegram:
        def send(self, message): calls["telegram"].append(message)
    monkeypatch.setenv("BUY_ORDER_POST_SLEEP_SEC", "0")
    return process, pick, orders, store, settings, calls, Logger(), Telegram()


def _submit(process, pick, settings, calls, logger, telegram, *, stop_event=None, market_open=True, latch=False):
    import threading
    return process.submit_candidate_order(
        pick=pick, active_style="stable", qty=2, order_mode="LIMIT", sizing_price=70000,
        market_buffer_pct=0.0, fee_pct=0.02, daily_buy_limit_latched=latch,
        stop_event=stop_event or threading.Event(), market_open_window=lambda: market_open,
        update_buy_safety=lambda **kwargs: calls["safety"].append(kwargs),
        symbol_label=lambda symbol, name=None: f"{symbol}({name or 'unknown'})",
        log=logger, order_log=logger, telegram=telegram, settings=settings,
        env_float=lambda key, default: float(default),
    )


def test_submit_candidate_order_uses_shared_order_manager_and_locks_buy_safety(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)

    result = _submit(process, pick, settings, calls, logger, telegram)

    assert result["submitted"] is True
    assert result["order"].order_no == "PAPER-123"
    assert orders.calls[0][0][:3] == ("005930", "BUY", 2)
    assert orders.calls[0][1]["reservation_price"] == pytest.approx(70014.0)
    assert calls["safety"] == [{"balance_ok": False, "reason": "매수 주문 제출 후 최신 잔고 재대사 대기"}]
    assert len(calls["telegram"]) == 1


def test_submit_candidate_order_final_time_gate_blocks_order(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)

    result = _submit(process, pick, settings, calls, logger, telegram, market_open=False)

    assert result["submitted"] is False
    assert not orders.calls
    assert not calls["telegram"]


def test_submit_candidate_order_latches_daily_limit_at_final_gate(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(
        monkeypatch, trades_today=5, max_trades=5,
    )

    result = _submit(process, pick, settings, calls, logger, telegram)

    assert result["submitted"] is False
    assert result["daily_buy_limit_latched"] is True
    assert not orders.calls


def test_submit_candidate_order_dry_run_never_calls_order_manager(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch, trading=False)

    result = _submit(process, pick, settings, calls, logger, telegram)

    assert result["submitted"] is False
    assert not orders.calls
    assert not calls["telegram"]


def test_validate_candidate_blocks_symbol_with_pending_order(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    orders.pending_for_symbol = lambda symbol: True

    result = process.validate_candidate(
        pick=pick, buy_safety={"balance_ok": True, "orders_ok": True},
        symbol_label=lambda symbol, name=None: f"{symbol}({name})", log=logger,
    )

    assert result is None
    assert calls["logs"]


def test_validate_candidate_blocks_existing_position_by_default(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    store.get_position = lambda symbol: {"qty": 3, "name": "삼성전자"}
    orders.pending_for_symbol = lambda symbol: False
    monkeypatch.delenv("ALLOW_ADD_TO_EXISTING_POSITION", raising=False)

    result = process.validate_candidate(
        pick=pick, buy_safety={"balance_ok": True, "orders_ok": True},
        symbol_label=lambda symbol, name=None: f"{symbol}({name})", log=logger,
    )

    assert result is None


def test_validate_candidate_requires_both_balance_and_order_reconcile(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    orders.pending_for_symbol = lambda symbol: False

    result = process.validate_candidate(
        pick=pick, buy_safety={"balance_ok": True, "orders_ok": False, "reason": "미체결 조회 실패"},
        symbol_label=lambda symbol, name=None: f"{symbol}({name})", log=logger,
    )

    assert result is None
    assert calls["logs"]


def test_validate_candidate_allows_candidate_when_every_gate_passes(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    orders.pending_for_symbol = lambda symbol: False
    store.get_position = lambda symbol: None
    monkeypatch.delenv("ALLOW_ADD_TO_EXISTING_POSITION", raising=False)

    result = process.validate_candidate(
        pick=pick, buy_safety={"balance_ok": True, "orders_ok": True},
        symbol_label=lambda symbol, name=None: f"{symbol}({name})", log=logger,
    )

    assert result is pick


def _prepare(process, pick, calls, logger, monkeypatch, *, quote=None, summary=None, reservation=(0.0, 0), sizing=True):
    process.api.current_price = lambda symbol: quote if quote is not None else {"STCK_PRPR": "70000", "prdt_name": "삼성전자"}
    active_summary = summary if summary is not None else {"ord_psbl_cash": "200000", "dnca_tot_amt": "200000"}
    process.api.balance = lambda: ([], active_summary)
    process.api.orderable_cash = lambda symbol, price, order_mode="MARKET": {
        "ord_psbl_cash": active_summary.get("ord_psbl_cash", "200000"),
        "nrcvb_buy_amt": active_summary.get("ord_psbl_cash", "200000"),
        "nrcvb_buy_qty": "100",
    }
    process.orders.pending_buy_reservation = lambda: reservation
    monkeypatch.setenv("POSITION_SIZING", "true" if sizing else "false")
    def set_safety(**kwargs): calls["safety"].append(kwargs)
    return process.prepare_candidate_order(
        pick=pick, price_value=lambda data: float(data.get("STCK_PRPR", 0) or 0) if isinstance(data, dict) else float(data or 0),
        resolve_order_mode=lambda *args: ("LIMIT", "test order mode"),
        quote_price_for_order=lambda quote_data, mode, current: current,
        summary_value=lambda data, *keys: next((float(data[k]) for k in keys if k in data), 0.0),
        calculate_buy_qty=lambda *args, **kwargs: (1, 70000.0, "test sizing"),
        show_order_calculation=lambda: False, env_float=lambda key, default: float(default),
        register_symbol=lambda symbol, name: None,
        symbol_label=lambda symbol, name=None: f"{symbol}({name or 'unknown'})",
        update_buy_safety=set_safety, log=logger, order_log=logger,
    )


def test_prepare_candidate_order_rechecks_quote_and_uses_account_cash(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    result = _prepare(process, pick, calls, logger, monkeypatch, quote={"STCK_PRPR": "71000", "prdt_name": "삼성전자"})

    assert result["current"] == 71000
    assert result["reference_price"] == 70000
    assert result["qty"] == 1
    assert result["order_mode"] == "LIMIT"
    assert calls["safety"] == [{"balance_ok": True}]


def test_prepare_candidate_order_blocks_unknown_pending_cash_reservation(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    result = _prepare(process, pick, calls, logger, monkeypatch, reservation=(50000.0, 1))

    assert result["qty"] == 0
    assert result["budget_reason"].startswith("계좌/종목별 매수가능조회 실패:")
    assert calls["safety"][-1]["balance_ok"] is False


def test_prepare_candidate_order_blocks_when_latest_quote_fails(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    process.api.current_price = lambda symbol: (_ for _ in ()).throw(RuntimeError("quote failed"))
    result = process.prepare_candidate_order(
        pick=pick, price_value=lambda data: float(data.get("STCK_PRPR", 0) or 0) if isinstance(data, dict) else float(data or 0),
        resolve_order_mode=lambda *args: ("LIMIT", "test"),
        quote_price_for_order=lambda quote_data, mode, current: current,
        summary_value=lambda data, *keys: 0.0,
        calculate_buy_qty=lambda *args, **kwargs: (1, 1.0, "should not run"),
        show_order_calculation=lambda: False, env_float=lambda key, default: float(default),
        register_symbol=lambda symbol, name: None,
        symbol_label=lambda symbol, name=None: f"{symbol}({name or 'unknown'})",
        update_buy_safety=lambda **kwargs: calls["safety"].append(kwargs),
        log=logger, order_log=logger,
    )
    assert result["qty"] == 0
    assert result["order_mode"] == "HOLD"
    assert result["current"] == 0


def test_prepare_candidate_order_fixed_quantity_still_enforces_available_cash(monkeypatch):
    process, pick, orders, store, settings, calls, logger, telegram = _submission_process(monkeypatch)
    result = _prepare(
        process, pick, calls, logger, monkeypatch,
        summary={"ord_psbl_cash": "90000", "dnca_tot_amt": "90000"}, sizing=False,
    )

    assert result["qty"] == 0
    assert "가용현금" in result["budget_reason"]
