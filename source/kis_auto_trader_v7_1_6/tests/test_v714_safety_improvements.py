import json
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_reentry_cooldown_is_persisted_across_state_store_restart(tmp_path):
    from storage.state import StateStore
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.mark_exit_cooldown("005930", 30000)
    assert path.exists()
    reloaded = StateStore(str(path))
    assert reloaded.cooldown_active("005930")
    assert reloaded.state.cooldowns["005930"] > 0


def test_daily_change_filter_reads_kis_previous_day_change_fields():
    from application import _daily_change_pct
    assert _daily_change_pct({"prdy_ctrt": "25.00"}) == 25.0
    assert _daily_change_pct({"PRDY_CTRT": "-15.00"}) == -15.0
    assert _daily_change_pct({"prdy_ctrt": ""}) is None
    assert _daily_change_pct({"prdy_ctrt": "not-a-number"}) is None


def test_daily_bar_cache_reuses_same_symbol_snapshot(monkeypatch):
    import application
    class FakeApi:
        calls = 0
        def daily_prices(self, symbol, days=260):
            self.calls += 1
            return [{"date": "20261030", "close": 100}]
    api = FakeApi()
    application._DAILY_BAR_CACHE.clear()
    application._DAILY_BAR_KEY_LOCKS.clear()
    monkeypatch.setenv("DAILY_BAR_CACHE_TTL_SEC", "60")
    first = application.get_cached_daily_prices(api, "005930", 260)
    second = application.get_cached_daily_prices(api, "005930", 260)
    assert first == second
    assert api.calls == 1


def test_sell_retry_backoff_increases_and_is_capped():
    from application import sell_retry_backoff_seconds
    assert sell_retry_backoff_seconds(1, 60, 900, 10) == 60
    assert sell_retry_backoff_seconds(2, 60, 900, 10) == 120
    assert sell_retry_backoff_seconds(3, 60, 900, 10) == 240
    assert sell_retry_backoff_seconds(10, 60, 900, 10) == 900
    assert sell_retry_backoff_seconds(1, 60, 900, 75) == 75


def test_strong_close_zero_range_is_hold_not_error():
    import yaml
    from strategies.builder_engine.internal_engine import evaluate_strategy
    path = ROOT / "config/strategies/strategy_07_strong_close.kis.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    bars = [{"date": f"202610{i:02d}", "open": 100, "high": 100,
             "low": 100, "close": 100, "volume": 1000} for i in range(1, 27)]
    result = evaluate_strategy(raw, bars, symbol="005930", purpose="both")
    assert result["action"] == "HOLD"
    assert result["status"] == "success"
    assert "범위 0" in result["reason"]


def test_internal_strategy_data_shortage_is_not_api_error():
    import yaml
    from strategies.builder_engine.internal_engine import evaluate_strategy
    path = ROOT / "config/strategies/strategy_01_golden_cross.kis.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    bars = [{"date": "20261030", "close": 100, "high": 100, "low": 100}]
    result = evaluate_strategy(raw, bars, symbol="005930", purpose="both")
    assert result["action"] == "HOLD"
    assert result["status"] == "insufficient_data"
    assert result["data_ready"] is False


def test_custom_yaml_insufficient_bars_is_not_api_error():
    from strategies.builder_engine.yaml_engine import YamlStrategyEngine
    engine = YamlStrategyEngine()
    result = engine.evaluate(str(ROOT / "config/my_entry_strategy.kis.yaml"),
                             [{"date": "20261030", "close": 100}],
                             symbol="005930", purpose="entry")
    assert result["action"] == "HOLD"
    assert result["status"] == "insufficient_data"
    assert result["data_ready"] is False
    assert result["buy_allowed"] is False


def test_telegram_failure_log_never_contains_bot_token_or_url(monkeypatch, caplog):
    import notifications.telegram as telegram_module
    secret = "test_secret_token_do_not_log"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secret)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")
    def fail(*args, **kwargs):
        raise RuntimeError(f"request failed https://api.telegram.org/bot{secret}/sendMessage")
    monkeypatch.setattr(telegram_module.requests, "post", fail)
    notifier = telegram_module.TelegramNotifier()
    with caplog.at_level(logging.WARNING, logger="notifications.telegram"):
        assert notifier.send("sensitive notification body") is False
    assert secret not in caplog.text
    assert "test-chat-id" not in caplog.text
    assert "sensitive notification body" not in caplog.text
    assert "api.telegram.org" not in caplog.text


def test_tr_rate_limiter_configuration_and_priority_mapping():
    from brokers.kis.client import KisApi
    assert KisApi._request_priority("POST", "VTTC0012U") == 0
    assert KisApi._request_priority("GET", "VTTC8434R") == 1
    assert KisApi._request_priority("GET", "VTTC0081R") == 1
    assert KisApi._request_priority("GET", "FHKST03010100") == 10


def test_env_has_daily_change_limits_and_500_minute_cooldown():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "REENTRY_COOLDOWN_SEC=30000" in env
    assert "ENTRY_MAX_DAILY_CHANGE_PCT=25" in env
    assert "ENTRY_MIN_DAILY_CHANGE_PCT=-15" in env
    assert "KIS_TR_RATE_LIMITS_JSON={}" in env


def test_user_custom_yaml_comments_match_active_conditions():
    entry = (ROOT / "config/my_entry_strategy.kis.yaml").read_text(encoding="utf-8")
    exit_yaml = (ROOT / "config/my_exit_strategy.kis.yaml").read_text(encoding="utf-8")
    assert 'right: 30' in entry
    assert 'RSI가 30 초과' in entry
    assert 'right: 50' in exit_yaml
    assert 'RSI 50 이상' in exit_yaml


def test_balance_reconcile_starts_cooldown_when_sell_notice_was_missed(tmp_path, monkeypatch):
    from storage.state import StateStore
    from trading.reconcile import reconcile
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.set_position("005930", {"symbol": "005930", "qty": 2, "avg_price": 100, "name": "삼성전자"})
    store.save()
    monkeypatch.setenv("REENTRY_COOLDOWN_SEC", "30000")
    class FakeApi:
        def balance(self):
            return [], {"tot_evlu_amt": "1000000", "ord_psbl_cash": "1000000"}
        def daily_orders(self, *args, **kwargs):
            return []
    reconcile(FakeApi(), store, orders=None)
    assert store.get_position("005930") is None
    assert store.cooldown_active("005930")



def test_kis_api_loads_optional_per_tr_rate_limits(monkeypatch, tmp_path):
    from config.settings import load_settings
    from brokers.kis.client import KisApi
    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KIS_TR_RATE_LIMITS_JSON",
                       '{"FHKST03010100":{"per_sec":0.4,"per_min":12}}')
    api = KisApi(load_settings())
    assert "FHKST03010100" in api.tr_limiters
    stats = api.tr_limiters["FHKST03010100"].stats()
    assert stats["configured_min_limit"] == 9  # 12/min * 0.80 safety margin


def test_builder_blocks_daily_change_at_inclusive_thresholds(monkeypatch):
    import time
    import application
    class FakeStore:
        def cooldown_active(self, symbol):
            return False
    class FakeApi:
        def daily_prices(self, symbol, days=260):
            return [{"date": "20261030", "open": 100, "high": 101, "low": 99,
                     "close": 100, "volume": 1000}] * 260
        def current_price(self, symbol):
            raise AssertionError("fresh quote already contains daily change")
    class FakeAdapter:
        def __init__(self, path): pass
        def evaluate(self, *args, **kwargs):
            return {"005930": {"action": "BUY", "score": 90, "reason": "test"}}
    class FakeCustomEngine:
        def __init__(self, *args, **kwargs): pass
        def evaluate(self, *args, **kwargs):
            return {"action": "BUY", "strength": 100, "data_ready": True, "buy_allowed": True, "reason": "test"}
    monkeypatch.setattr(application, "OfficialBuilderAdapter", FakeAdapter)
    monkeypatch.setattr(application, "YamlStrategyEngine", FakeCustomEngine)
    monkeypatch.setenv("ENTRY_MAX_DAILY_CHANGE_PCT", "25")
    monkeypatch.setenv("ENTRY_MIN_DAILY_CHANGE_PCT", "-15")
    monkeypatch.setenv("DAILY_BAR_CACHE_TTL_SEC", "60")
    for pct in (25.0, -15.0):
        application._DAILY_BAR_CACHE.clear()
        application._DAILY_BAR_KEY_LOCKS.clear()
        quote = {"STCK_PRPR": "100", "PRDY_CTRT": str(pct), "_received_ts": time.time()}
        result = application.run_builder_cycle(
            FakeApi(), FakeStore(), {"005930": quote}, __import__("threading").RLock(),
            quote_stale_sec=5, extra_symbols=["005930"], include_manual=False,
            evaluation_mode="ALL")
        assert result is None



def test_uploaded_golden_cross_settings_are_preserved_but_risk_yaml_is_documented_as_inactive():
    import yaml
    path = ROOT / "config/strategies/strategy_01_golden_cross.kis.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["strategy"]["params"]["short_period"] == 3
    assert raw["strategy"]["params"]["long_period"] == 10
    assert raw["strategy"]["entry"]["logic"] == "OR"
    text = path.read_text(encoding="utf-8")
    assert "현재 KIS10 내부 신호 평가 및 공통 청산 실행은 이 값을" in text
    assert "실제 1차/2차 손익절 기준은 .env" in text



def test_legacy_env_values_cannot_reduce_new_cooldown_and_sell_retry_safety_minimums(monkeypatch):
    from config.settings import load_settings
    monkeypatch.setenv("REENTRY_COOLDOWN_SEC", "900")
    monkeypatch.setenv("SELL_RETRY_COOLDOWN_SEC", "10")
    settings = load_settings()
    assert settings.reentry_cooldown_sec >= 30000 if hasattr(settings, "reentry_cooldown_sec") else settings.strategy.cooldown_sec >= 30000
    assert settings.sell_retry_cooldown_sec >= 60
