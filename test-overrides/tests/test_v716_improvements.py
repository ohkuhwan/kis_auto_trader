from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_position_sizer_uses_orderable_cash_not_deposit_as_primary_basis():
    from risk.position_sizer import calculate_buy_qty

    qty, budget, reason = calculate_buy_qty(
        {"dnca_tot_amt": "300000", "ord_psbl_cash": "900000"},
        held_count=0, price=10000, max_positions=3,
        fee_rate_pct=0, price_buffer_pct=0,
    )
    assert budget == pytest.approx(300000)
    assert qty == 30
    assert "cash_source=orderable_cash" in reason


def test_position_sizer_caps_orderable_cash_by_no_credit_amount_and_quantity():
    from risk.position_sizer import calculate_buy_qty

    qty, budget, reason = calculate_buy_qty(
        {"dnca_tot_amt": "300000", "ord_psbl_cash": "900000"},
        held_count=0, price=10000, max_positions=3,
        fee_rate_pct=0, price_buffer_pct=0,
        buyable_cash_limit=450000, buyable_qty_limit=12,
    )
    assert budget == pytest.approx(150000)
    assert qty == 12
    assert "no_credit_qty_limit=12" in reason


def test_position_sizer_deposit_fallback_is_explicit_and_respects_reservations():
    from risk.position_sizer import calculate_buy_qty

    qty, budget, reason = calculate_buy_qty(
        {"dnca_tot_amt": "300000", "ord_psbl_cash": "900000"},
        held_count=1, price=10000, max_positions=3,
        fee_rate_pct=0, price_buffer_pct=0,
        reserved_cash=50000, cash_basis="deposit", buyable_cash_limit=800000,
    )
    assert budget == pytest.approx(125000)
    assert qty == 12
    assert "cash_source=deposit_fallback" in reason


def test_buying_power_classifier_only_matches_explicit_cash_limit_messages():
    from brokers.kis.client import _is_buying_power_rejection

    assert _is_buying_power_rejection({"msg_cd": "ANY", "msg1": "주문가능금액이 부족합니다"})
    assert _is_buying_power_rejection({"msg_cd": "ANY", "msg1": "미수없는매수금액을 초과했습니다"})
    assert not _is_buying_power_rejection({"msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과"})
    assert not _is_buying_power_rejection({"msg_cd": "ANY", "msg1": "네트워크 오류"})
    assert not _is_buying_power_rejection({"msg_cd": "ANY", "msg1": "주문가능금액 조회 실패"})


def test_domestic_buyable_inquiry_uses_correct_paper_tr_and_no_credit_fields(monkeypatch):
    from brokers.kis.client import KisApi

    api = KisApi.__new__(KisApi)
    api.s = SimpleNamespace(paper=True, cano="12345678", acnt_prdt_cd="01")
    captured = {}

    def fake_request(method, path, *, tr_id, params=None, **kwargs):
        captured.update(method=method, path=path, tr_id=tr_id, params=params)
        return {"output": {
            "ord_psbl_cash": "900000", "nrcvb_buy_amt": "450000", "nrcvb_buy_qty": "45",
        }}

    api._request = fake_request
    result = api.orderable_cash("005930", 70000, order_mode="BEST_LIMIT")
    assert captured["method"] == "GET"
    assert captured["path"].endswith("/inquire-psbl-order")
    assert captured["tr_id"] == "VTTC8908R"
    assert captured["params"]["ORD_DVSN"] == "03"
    assert captured["params"]["ORD_UNPR"] == "0"
    assert captured["params"]["PDNO"] == "005930"
    assert result["nrcvb_buy_amt"] == "450000"
    assert result["nrcvb_buy_qty"] == "45"

    captured.clear()
    api.orderable_cash("005930", 70000, order_mode="MARKET")
    assert captured["params"]["ORD_DVSN"] == "01"
    assert captured["params"]["ORD_UNPR"] == ""


def test_order_manager_rejection_check_allows_retry_only_when_history_has_no_candidate(tmp_path, monkeypatch):
    from trading.orders import OrderManager

    class Api:
        def daily_orders(self, *args, **kwargs):
            return []

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    manager = OrderManager(Api(), timeout_sec=60)
    assert manager.confirm_rejected_order_absent("005930", "BUY", 10, 1000.0, "explicit rejection") is True
    assert not manager.uncertain_symbols


def test_order_manager_rejection_check_locks_when_history_candidate_exists(tmp_path, monkeypatch):
    from trading.orders import OrderManager

    class Api:
        def daily_orders(self, *args, **kwargs):
            # Time omitted intentionally: it cannot be safely excluded as an old order.
            return [{"pdno": "005930", "sll_buy_dvsn_cd": "02", "ord_qty": "10", "odno": "12345"}]

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    manager = OrderManager(Api(), timeout_sec=60)
    assert manager.confirm_rejected_order_absent("005930", "BUY", 10, 1000.0, "explicit rejection") is False
    assert "005930" in manager.uncertain_symbols


def test_buy_process_retries_once_with_deposit_only_after_explicit_rejection_and_history_absence(monkeypatch):
    from brokers.kis.client import InsufficientBuyingPowerError
    from trading.buy_process import BuyProcess

    class Orders:
        def __init__(self):
            self.submitted = []
            self.history_checks = []
        def submit(self, symbol, side, qty, **kwargs):
            self.submitted.append(qty)
            if len(self.submitted) == 1:
                raise InsufficientBuyingPowerError("buying power rejected", msg_cd="MOCK", msg1="주문가능금액 부족")
            return SimpleNamespace(order_no="PAPER-RETRY")
        def confirm_rejected_order_absent(self, *args):
            self.history_checks.append(args)
            return True
        def pending_buy_reservation(self):
            return 0.0, 0
        def pending_buy_symbols(self):
            return set()

    class Api:
        def balance(self):
            return [], {"dnca_tot_amt": "100000", "ord_psbl_cash": "300000"}
        def orderable_cash(self, *args, **kwargs):
            return {"ord_psbl_cash": "300000", "nrcvb_buy_amt": "300000", "nrcvb_buy_qty": "100"}

    orders = Orders()
    store = SimpleNamespace(
        state=SimpleNamespace(trades_today=0),
        get_position=lambda symbol: None,
        symbols=lambda: [],
    )
    process = BuyProcess(
        api=Api(), store=store, engine=object(), orders=orders, realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={},
        settings=SimpleNamespace(enable_trading=True, max_trades_per_day=5, paper=True),
        run_hts_cycle=lambda *a, **k: None, run_builder_cycle=lambda *a, **k: None,
    )
    monkeypatch.setenv("BUY_ORDER_POST_SLEEP_SEC", "0")
    logs = []
    result = process.submit_candidate_order(
        pick=SimpleNamespace(symbol="005930", name="삼성전자", score=50, reason="test"),
        active_style="unified", qty=10, order_mode="LIMIT", sizing_price=10000,
        market_buffer_pct=0, fee_pct=0, daily_buy_limit_latched=False,
        stop_event=threading.Event(), market_open_window=lambda: True,
        update_buy_safety=lambda **kwargs: logs.append(kwargs),
        symbol_label=lambda symbol, name=None: f"{symbol}({name})",
        log=SimpleNamespace(warning=lambda *a, **k: None),
        order_log=SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, exception=lambda *a, **k: None),
        telegram=SimpleNamespace(send=lambda message: None),
        settings=process.settings, env_float=lambda key, default: 0.0,
        sizing_context={
            "summary": {"dnca_tot_amt": "300000", "ord_psbl_cash": "900000"},
            "symbol_buyable": {}, "effective_positions": 0, "max_positions": 3,
            "min_order_amount": 10000, "reserved_cash": 0,
            "position_sizing_enabled": True, "fee_pct": 0.0, "market_buffer": 0.0,
            "sizing_price": 10000, "original_qty": 10,
        },
    )
    assert result["submitted"] is True
    assert result["order"].order_no == "PAPER-RETRY"
    assert orders.submitted == [10, 3]
    assert len(orders.history_checks) == 1


def test_buy_process_does_not_retry_when_rejection_history_is_not_confirmed(monkeypatch):
    from brokers.kis.client import InsufficientBuyingPowerError
    from trading.buy_process import BuyProcess

    class Orders:
        def __init__(self): self.submitted = 0
        def submit(self, *args, **kwargs):
            self.submitted += 1
            raise InsufficientBuyingPowerError("no cash", msg_cd="MOCK", msg1="주문가능금액 부족")
        def confirm_rejected_order_absent(self, *args): return False
        def pending_buy_reservation(self): return 0.0, 0
        def pending_buy_symbols(self): return set()

    orders = Orders()
    store = SimpleNamespace(state=SimpleNamespace(trades_today=0), get_position=lambda s: None, symbols=lambda: [])
    process = BuyProcess(
        api=object(), store=store, engine=object(), orders=orders, realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={},
        settings=SimpleNamespace(enable_trading=True, max_trades_per_day=5, paper=True),
        run_hts_cycle=lambda *a, **k: None, run_builder_cycle=lambda *a, **k: None,
    )
    monkeypatch.setenv("BUY_ORDER_POST_SLEEP_SEC", "0")
    result = process.submit_candidate_order(
        pick=SimpleNamespace(symbol="005930", name="삼성전자", score=50, reason="test"),
        active_style="unified", qty=10, order_mode="LIMIT", sizing_price=10000,
        market_buffer_pct=0, fee_pct=0, daily_buy_limit_latched=False,
        stop_event=threading.Event(), market_open_window=lambda: True,
        update_buy_safety=lambda **kwargs: None,
        symbol_label=lambda symbol, name=None: symbol,
        log=SimpleNamespace(warning=lambda *a, **k: None),
        order_log=SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, exception=lambda *a, **k: None),
        telegram=SimpleNamespace(send=lambda message: None),
        settings=process.settings, env_float=lambda key, default: 0.0,
        sizing_context={"position_sizing_enabled": True},
    )
    assert result["submitted"] is False
    assert orders.submitted == 1


def test_custom_mode_requests_only_yaml_lookback_and_kis10_keeps_260_days(monkeypatch):
    from application import required_daily_bars

    monkeypatch.setenv("STRATEGY_EVALUATION_MODE", "CUSTOM")
    entry = str(ROOT / "config/my_entry_strategy.kis.yaml")
    exit_path = str(ROOT / "config/my_exit_strategy.kis.yaml")
    assert required_daily_bars("CUSTOM", entry, exit_path) == 25
    # Current decision profile excludes week52_high (252 bars) and the active
    # longest lookback is 60 trading days, plus five warm-up bars.
    assert required_daily_bars("KIS10", entry, exit_path) == 65
    monkeypatch.setenv("BUILDER_OBSERVE_ALL_STRATEGIES", "true")
    assert required_daily_bars("KIS10", entry, exit_path) == 257


def test_daily_bar_cache_reuses_success_and_throttles_repeated_failure(monkeypatch):
    import application

    application._DAILY_BAR_CACHE.clear()
    application._DAILY_BAR_FAILURE_CACHE.clear()
    application._DAILY_BAR_KEY_LOCKS.clear()

    class GoodApi:
        def __init__(self): self.calls = 0
        def daily_prices(self, symbol, days):
            self.calls += 1
            return [{"close": 100}] * days

    good = GoodApi()
    monkeypatch.setenv("DAILY_BAR_CACHE_TTL_SEC", "300")
    assert len(application.get_cached_daily_prices(good, "005930", 25)) == 25
    assert len(application.get_cached_daily_prices(good, "005930", 25)) == 25
    assert good.calls == 1

    application._DAILY_BAR_CACHE.clear()
    application._DAILY_BAR_FAILURE_CACHE.clear()
    application._DAILY_BAR_KEY_LOCKS.clear()

    class BadApi:
        def __init__(self): self.calls = 0
        def daily_prices(self, symbol, days):
            self.calls += 1
            raise RuntimeError("mock api failure")

    bad = BadApi()
    with pytest.raises(RuntimeError, match="mock api failure"):
        application.get_cached_daily_prices(bad, "000660", 25)
    with pytest.raises(RuntimeError, match="재시도 대기"):
        application.get_cached_daily_prices(bad, "000660", 25)
    assert bad.calls == 1


def test_third_party_http_debug_loggers_are_suppressed():
    from application import _configure_logging

    _configure_logging()
    assert logging.getLogger("urllib3").level >= logging.WARNING
    assert logging.getLogger("requests").level >= logging.WARNING
    assert logging.getLogger("urllib3.connectionpool").disabled is True


def test_reconcile_holds_fill_lock_for_balance_snapshot_but_not_order_history(tmp_path):
    from storage.state import StateStore
    from trading.reconcile import reconcile

    lock = threading.Lock()

    class Api:
        def balance(self):
            assert lock.locked()
            return [], {"nass_amt": "100000"}
        def daily_orders(self, *args, **kwargs):
            assert not lock.locked()
            return []

    store = StateStore(str(tmp_path / "state.json"))
    result = reconcile(Api(), store, state_lock=lock)
    assert result["positions"] == 0


def test_start_request_and_config_snapshot_security_are_explicit():
    request_source = (ROOT / "scripts/request_start.sh").read_text(encoding="utf-8")
    start_source = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")
    assert 'kis_log_append "$LOG_DIR" "start_request_${date_tag}.log"' in request_source
    assert "config_snapshot.py" in start_source
    snapshot_source = (ROOT / "scripts/config_snapshot.py").read_text(encoding="utf-8")
    assert "SAFE_KEYS" in snapshot_source
    assert "TELEGRAM_BOT_TOKEN" not in snapshot_source
    assert "KIS_APP_SECRET" not in snapshot_source


def test_shutdown_cutoff_is_part_of_final_order_time_gate():
    source = (ROOT / "application.py").read_text(encoding="utf-8")
    assert 'shutdown = dtime.fromisoformat(os.getenv("AUTO_SHUTDOWN_TIME", "15:10"))' in source
    assert "effective_end = min(end, shutdown)" in source


def test_systemd_stop_timeout_is_longer_than_script_grace_period():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    template = (ROOT / "deploy/systemd/kis-auto-trader.service").read_text(encoding="utf-8")
    assert "TimeoutStopSec=300" in source
    assert "TimeoutStopSec=300" in template
    assert "STOP_GRACE_TIMEOUT_SEC" in (ROOT / "scripts/stop.sh").read_text(encoding="utf-8")


def test_state_store_persists_second_buy_failure_block_until_next_kst_day(tmp_path):
    from storage.state import StateStore

    path = str(tmp_path / "state.json")
    store = StateStore(path)
    store.reset_daily_if_needed("20261030")
    store.mark_buy_rejection_block("005930", "second attempt failed", "20261030")
    assert store.buy_rejection_block("005930", "20261030")["reason"] == "second attempt failed"
    restored = StateStore(path)
    assert restored.buy_rejection_block("005930", "20261030")["reason"] == "second attempt failed"
    assert restored.buy_rejection_block("005930", "20261031") is None
    restored.reset_daily_if_needed("20261031")
    assert restored.buy_rejection_block("005930", "20261031") is None


def test_sensitive_text_redaction_masks_telegram_account_and_credentials():
    from brokers.kis.client import _redact_sensitive_text

    bot_token = "bot" + "123456" + ":" + "ABC_DEF"
    source = f"https://api.telegram.org/{bot_token}/sendMessage?chat_id=12345678 CANO=12345678 appsecret=SECRET approval_key=WSKEY"
    redacted = _redact_sensitive_text(source)
    assert ("123456" + ":" + "ABC_DEF") not in redacted
    assert "chat_id=12345678" not in redacted
    assert "CANO=12345678" not in redacted
    assert "SECRET" not in redacted
    assert "WSKEY" not in redacted


def test_application_uses_fill_worker_and_balance_snapshot_lock():
    source = (ROOT / "application.py").read_text(encoding="utf-8")
    reconcile_source = (ROOT / "trading/reconcile.py").read_text(encoding="utf-8")
    assert 'name="fill-event-worker"' in source
    assert 'realtime.stop()' in source and 'fill_event_queue.put(None)' in source
    assert 'state_lock=fill_apply_lock' in source
    assert 'with (state_lock if state_lock is not None else nullcontext())' in reconcile_source


def test_buy_process_candidate_validation_honors_persisted_daily_rejection_block(tmp_path, monkeypatch):
    from storage.state import StateStore
    from trading.buy_process import BuyProcess

    store = StateStore(str(tmp_path / "state.json"))
    today = "20261030"
    store.reset_daily_if_needed(today)
    store.mark_buy_rejection_block("005930", "second_attempt_failed", today)
    process = BuyProcess(
        api=object(), store=store, engine=object(),
        orders=SimpleNamespace(pending_for_symbol=lambda symbol: False), realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={},
        settings=SimpleNamespace(enable_trading=True),
        run_hts_cycle=lambda *a, **k: None, run_builder_cycle=lambda *a, **k: None,
    )
    monkeypatch.setattr("trading.buy_process.datetime", SimpleNamespace(
        now=lambda tz=None: __import__("datetime").datetime(2026, 10, 30, 10, 0, tzinfo=tz)
    ))
    logs = []
    pick = SimpleNamespace(symbol="005930", name="삼성전자")
    result = process.validate_candidate(
        pick=pick, buy_safety={"balance_ok": True, "orders_ok": True},
        symbol_label=lambda symbol, name=None: f"{symbol}({name})",
        log=SimpleNamespace(warning=lambda *a, **k: logs.append(a), info=lambda *a, **k: logs.append(a)),
    )
    assert result is None
    assert logs


def test_second_fallback_order_failure_persists_same_day_symbol_block(tmp_path, monkeypatch):
    from brokers.kis.client import InsufficientBuyingPowerError
    from storage.state import StateStore
    from trading.buy_process import BuyProcess

    # Freeze the KST date to match this test's isolated StateStore date key.
    monkeypatch.setattr("trading.buy_process.datetime", SimpleNamespace(
        now=lambda tz=None: __import__("datetime").datetime(2026, 10, 30, 10, 0, tzinfo=tz)
    ))

    class Orders:
        def __init__(self): self.submitted = []
        def submit(self, symbol, side, qty, **kwargs):
            self.submitted.append(qty)
            raise InsufficientBuyingPowerError("no cash", msg_cd="MOCK", msg1="주문가능금액 부족")
        def confirm_rejected_order_absent(self, *args): return True
        def pending_buy_reservation(self): return 0.0, 0
        def pending_buy_symbols(self): return set()

    class Api:
        def balance(self): return [], {"dnca_tot_amt": "100000", "ord_psbl_cash": "300000"}
        def orderable_cash(self, *args, **kwargs):
            return {"ord_psbl_cash": "300000", "nrcvb_buy_amt": "300000", "nrcvb_buy_qty": "100"}

    store = StateStore(str(tmp_path / "state.json"))
    today = "20261030"
    store.reset_daily_if_needed(today)
    orders = Orders()
    process = BuyProcess(
        api=Api(), store=store, engine=object(), orders=orders, realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={},
        settings=SimpleNamespace(enable_trading=True, max_trades_per_day=5, paper=True),
        run_hts_cycle=lambda *a, **k: None, run_builder_cycle=lambda *a, **k: None,
    )
    monkeypatch.setenv("BUY_ORDER_POST_SLEEP_SEC", "0")
    result = process.submit_candidate_order(
        pick=SimpleNamespace(symbol="005930", name="삼성전자", score=50, reason="test"),
        active_style="unified", qty=10, order_mode="LIMIT", sizing_price=10000,
        market_buffer_pct=0, fee_pct=0, daily_buy_limit_latched=False,
        stop_event=threading.Event(), market_open_window=lambda: True,
        update_buy_safety=lambda **kwargs: None, symbol_label=lambda symbol, name=None: symbol,
        log=SimpleNamespace(warning=lambda *a, **k: None),
        order_log=SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, exception=lambda *a, **k: None),
        telegram=SimpleNamespace(send=lambda message: None), settings=process.settings,
        env_float=lambda key, default: 0.0,
        sizing_context={"position_sizing_enabled": True, "max_positions": 3, "min_order_amount": 10000,
                        "fee_pct": 0, "market_buffer": 0, "sizing_price": 10000},
    )
    assert result["submitted"] is False
    assert orders.submitted == [10, 3]
    assert store.buy_rejection_block("005930", today)["reason"].startswith("second_buy_attempt_rejected")


def test_rate_limit_error_is_not_retried_using_deposit():
    from brokers.kis.client import RateLimitError
    from trading.buy_process import BuyProcess

    class Orders:
        def __init__(self): self.calls = 0
        def submit(self, *args, **kwargs):
            self.calls += 1
            raise RateLimitError("EGW00201")

    orders = Orders()
    store = SimpleNamespace(state=SimpleNamespace(trades_today=0), get_position=lambda s: None)
    process = BuyProcess(
        api=object(), store=store, engine=object(), orders=orders, realtime=object(),
        price_cache={}, price_cache_lock=threading.RLock(), last_rest_quote={},
        settings=SimpleNamespace(enable_trading=True, max_trades_per_day=5, paper=True),
        run_hts_cycle=lambda *a, **k: None, run_builder_cycle=lambda *a, **k: None,
    )
    with pytest.raises(RateLimitError):
        process.submit_candidate_order(
            pick=SimpleNamespace(symbol="005930", name="삼성전자", score=50, reason="test"),
            active_style="unified", qty=10, order_mode="LIMIT", sizing_price=10000,
            market_buffer_pct=0, fee_pct=0, daily_buy_limit_latched=False,
            stop_event=threading.Event(), market_open_window=lambda: True,
            update_buy_safety=lambda **kwargs: None, symbol_label=lambda symbol, name=None: symbol,
            log=SimpleNamespace(warning=lambda *a, **k: None),
            order_log=SimpleNamespace(warning=lambda *a, **k: None),
            telegram=SimpleNamespace(send=lambda message: None),
            settings=process.settings, env_float=lambda key, default: 0.0,
        )
    assert orders.calls == 1


def test_kis10_strategy_adapter_distinguishes_insufficient_history_from_api_error(tmp_path, monkeypatch):
    import yaml
    from strategies.builder_engine.official_adapter import OfficialBuilderAdapter

    profile = {
        "strategies": [
            {"id": "s1", "file": str(ROOT / "config/strategies/strategy_01_golden_cross.kis.yaml"), "params": {}},
        ],
        "decision": {
            "buy": {"min_votes": 1, "min_score": 1, "strategies": [{"id": "s1", "weight": 1}]},
            "sell": {"min_votes": 1, "min_score": 1, "strategies": [{"id": "s1", "weight": 1}]},
        },
    }
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    adapter = OfficialBuilderAdapter(str(path))
    class Api:
        def daily_prices(self, *args, **kwargs): raise AssertionError("shared bars must prevent extra fetch")
    results = adapter.evaluate(
        "unified", ["005930"], observe_all=False, api=Api(),
        bars_by_symbol={"005930": None}, data_status_by_symbol={"005930": "INSUFFICIENT_HISTORY"},
    )
    assert results["005930"]["action"] == "HOLD"
    assert results["005930"]["status"] == "insufficient_data"
    assert results["005930"]["buy_data_incomplete"] == ["s1"]
    results = adapter.evaluate(
        "unified", ["005930"], observe_all=False, api=Api(),
        bars_by_symbol={"005930": None}, data_status_by_symbol={"005930": "API_ERROR"},
    )
    assert results["005930"]["action"] == "ERROR"
    assert results["005930"]["buy_errors"] == ["s1"]


def test_application_respects_builder_observe_all_setting():
    source = (ROOT / "application.py").read_text(encoding="utf-8")
    assert 'observe_all = os.getenv("BUILDER_OBSERVE_ALL_STRATEGIES", "false")' in source
    assert 'observe_all=observe_all' in source
    assert 'data_status_by_symbol=bar_status_by_symbol' in source
    assert 'observe_all=observe_all' in source
    assert 'status=INSUFFICIENT_HISTORY' in source


def test_config_snapshot_records_safe_settings_without_secrets(tmp_path):
    import shutil
    import stat
    import sys

    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    shutil.copy2(ROOT / "scripts/config_snapshot.py", project / "scripts/config_snapshot.py")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    shutil.copy2(ROOT / "config/__init__.py", project / "config/__init__.py")
    telegram_key = "TELEGRAM_BOT_" + "TOKEN"
    (project / ".env").write_text(
        "KIS_DATA_DIR=json\nSTRATEGY_EVALUATION_MODE=CUSTOM\n"
        "KIS_APP_SECRET=TEST_KIS_SECRET\n" + telegram_key + "=TEST_TELEGRAM_TOKEN\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(project / "scripts/config_snapshot.py")],
        cwd=project, capture_output=True, text=True, check=True,
    )
    snapshot_path = project / "json/config_snapshot.json"
    raw = snapshot_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["settings"]["STRATEGY_EVALUATION_MODE"] == "CUSTOM"
    assert "TEST_KIS_SECRET" not in raw
    assert "TEST_TELEGRAM_TOKEN" not in raw
    assert stat.S_IMODE(snapshot_path.stat().st_mode) in {0o600, 0o660}
    assert "CONFIG_SNAPSHOT status=created" in completed.stdout


def test_kis_api_invalid_tr_override_keeps_builtin_rate_limiters(monkeypatch, tmp_path):
    from config.settings import Settings, StrategyConfig
    from brokers.kis.client import KisApi

    monkeypatch.setenv("KIS_TR_RATE_LIMITS_JSON", "not-json")
    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    strategy = StrategyConfig("default", 20, 1, 2, 3, 1, 1, 900, 0.2)
    settings = Settings(
        paper=True, enable_trading=False, app_key="", app_secret="", hts_id="",
        cano="", acnt_prdt_cd="01", api_base="https://example.invalid", ws_url="",
        condition_name="", scan_interval_sec=30, balance_interval_sec=60,
        order_reconcile_interval_sec=20, quote_stale_sec=5, quote_rest_min_interval_sec=1.0,
        order_timeout_sec=8, buy_order_timeout_sec=300, sell_order_timeout_sec=300,
        sell_retry_cooldown_sec=60, max_trades_per_day=5, max_daily_loss_pct=2.0,
        max_candidates=20, order_qty=1, max_calls_per_sec=1.5, max_calls_per_min=32,
        rate_limit_safety_margin=0.8, state_file="", strategy=strategy,
    )
    api = KisApi(settings)
    assert "FHKST03010100" in api.tr_limiters
    assert "VTTC8908R" in api.tr_limiters
    assert "VTTC0012U" in api.tr_limiters
