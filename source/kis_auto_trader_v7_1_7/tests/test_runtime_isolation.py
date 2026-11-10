"""운영용 런타임 파일 경로가 pytest 전용 디렉터리로 격리되는지 검증합니다."""
from __future__ import annotations

import os
from pathlib import Path


def test_mutable_runtime_paths_are_inside_test_runtime_dir():
    runtime_dir = Path(os.environ["KIS_TEST_RUNTIME_DIR"]).resolve()
    from config.paths import runtime_path
    assert runtime_path("monitoring_disabled").resolve().is_relative_to(runtime_dir)
    variable_names = (
        "KIS_DATA_DIR",
        "KIS_LOG_DIR",
        "STRATEGY_SIGNAL_LOG",
        "STATE_FILE",
        "UNCERTAIN_ORDERS_FILE",
        "CANCEL_ORDERS_FILE",
        "HOLIDAY_CACHE_FILE",
        "KIS_TOKEN_CACHE_FILE",
    )
    for variable_name in variable_names:
        configured_path = Path(os.environ[variable_name]).resolve()
        assert configured_path == runtime_dir or runtime_dir in configured_path.parents, (
            f"{variable_name}가 테스트 전용 경로 밖을 가리킵니다: {configured_path}"
        )


def test_cancel_order_record_does_not_default_to_project_root():
    runtime_dir = Path(os.environ["KIS_TEST_RUNTIME_DIR"]).resolve()
    cancel_path = Path(os.environ["CANCEL_ORDERS_FILE"]).resolve()
    project_root_cancel_path = Path(__file__).resolve().parents[1] / "cancel_orders.json"
    assert cancel_path != project_root_cancel_path.resolve()
    assert runtime_dir in cancel_path.parents


def test_canonical_paths_are_project_root_relative_even_when_cwd_changes(tmp_path, monkeypatch):
    from config.paths import PROJECT_ROOT, runtime_paths

    for key in (
        "KIS_DATA_DIR", "KIS_LOG_DIR", "STATE_FILE", "KIS_TOKEN_CACHE_FILE",
        "UNCERTAIN_ORDERS_FILE", "CANCEL_ORDERS_FILE", "HOLIDAY_CACHE_FILE",
        "STRATEGY_SIGNAL_LOG", "BUILDER_SYMBOLS_FILE", "KIS_PAPER_HOLIDAY_CALENDAR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    paths = runtime_paths()
    assert paths["data_dir"] == (PROJECT_ROOT / "json").resolve()
    assert paths["state"] == (PROJECT_ROOT / "json/state.json").resolve()
    assert paths["token_cache"] == (PROJECT_ROOT / "json/kis_tokens.json").resolve()
    assert paths["uncertain_orders"] == (PROJECT_ROOT / "json/uncertain_orders.json").resolve()
    assert paths["cancel_orders"] == (PROJECT_ROOT / "json/cancel_orders.json").resolve()
    assert paths["monitoring_disabled"] == (PROJECT_ROOT / "json/monitoring_disabled.json").resolve()
    assert paths["holiday_cache"] == (PROJECT_ROOT / "json/holiday_config.json").resolve()
    assert paths["strategy_signal_log"] == (PROJECT_ROOT / "logs/strategy_signals.jsonl").resolve()
    assert paths["builder_symbols"] == (PROJECT_ROOT / "config/entry_symbols.txt").resolve()


def test_legacy_per_file_relative_override_remains_project_root_relative(monkeypatch, tmp_path):
    from config.paths import PROJECT_ROOT, runtime_paths

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path / "central"))
    monkeypatch.setenv("CANCEL_ORDERS_FILE", "legacy/cancel_orders.json")
    paths = runtime_paths()
    assert paths["data_dir"] == (tmp_path / "central").resolve()
    assert paths["cancel_orders"] == (PROJECT_ROOT / "legacy/cancel_orders.json").resolve()


def test_first_creation_and_restart_use_same_canonical_state_path(tmp_path, monkeypatch):
    from config.paths import runtime_path
    from storage.state import StateStore

    for key in ("KIS_DATA_DIR", "STATE_FILE", "KIS_LOG_DIR", "KIS_TOKEN_CACHE_FILE",
                "UNCERTAIN_ORDERS_FILE", "CANCEL_ORDERS_FILE", "HOLIDAY_CACHE_FILE",
                "STRATEGY_SIGNAL_LOG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path / "runtime-json"))
    path = runtime_path("state")
    assert not path.exists()
    first = StateStore(str(path))
    first.set_position("005930", {"symbol": "005930", "qty": 2, "avg_price": 70000})
    first.save()
    assert path.is_file()
    second = StateStore(str(runtime_path("state")))
    assert second.get_position("005930")["qty"] == 2


def test_missing_cancel_records_are_created_in_json_directory_on_first_save(tmp_path, monkeypatch):
    from config.paths import runtime_path
    from trading.orders import OrderManager

    for key in ("KIS_DATA_DIR", "CANCEL_ORDERS_FILE", "UNCERTAIN_ORDERS_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path / "json"))
    manager = OrderManager(object(), 10)
    assert manager.cancel_records_file == runtime_path("cancel_orders")
    assert manager.cancel_records_file.is_file()
    manager._save_cancel_records()
    assert manager.cancel_records_file.is_file()
    assert manager.cancel_records_file.parent == tmp_path / "json"
    restarted = OrderManager(object(), 10)
    assert restarted.cancel_records_integrity_error is None


def test_token_cache_first_creation_and_restart_in_canonical_directory(tmp_path, monkeypatch):
    import time
    from brokers.kis.client import KisApi
    from config.paths import runtime_path
    from config.settings import load_settings

    for key in ("KIS_DATA_DIR", "KIS_TOKEN_CACHE_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path / "json"))
    monkeypatch.setenv("KIS_PAPER", "true")
    monkeypatch.setenv("KIS_APP_KEY", "TEST_KEY_FOR_PATH_TEST")
    monkeypatch.setenv("KIS_APP_SECRET", "TEST_SECRET_FOR_PATH_TEST")
    first = KisApi(load_settings())
    assert first.token_cache_path == runtime_path("token_cache")
    assert not first.token_cache_path.exists()
    first.token = "offline-test-token"
    first.token_expire_at = time.time() + 3600
    first.approval_key = "offline-test-approval"
    first._ws_approval_expire_at = time.time() + 3600
    first._save_token_cache()
    assert first.token_cache_path.is_file()

    restarted = KisApi(load_settings())
    assert restarted.token == "offline-test-token"
    assert restarted.approval_key == "offline-test-approval"


def test_holiday_cache_first_creation_and_restart_use_canonical_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from config.paths import runtime_path
    from storage.holiday import HolidayChecker

    for key in ("KIS_DATA_DIR", "HOLIDAY_CACHE_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path / "json"))

    class Api:
        s = SimpleNamespace(paper=False)
        def check_holiday(self, day):
            return True

    path = runtime_path("holiday_cache")
    assert not path.exists()
    assert HolidayChecker(Api()).is_business_day("20261022") is True
    assert path.is_file()

    class MustUseCacheApi:
        s = SimpleNamespace(paper=False)
        def check_holiday(self, day):
            raise AssertionError("유효한 캐시가 있으면 API를 다시 호출하면 안 됩니다")

    assert HolidayChecker(MustUseCacheApi()).is_business_day("20261022") is True


def test_log_file_creation_uses_canonical_log_directory_after_cwd_change(tmp_path, monkeypatch):
    import logging
    from config.paths import runtime_path
    from storage.daily_logging import KSTDailyFileHandler, KSTFormatter

    for key in ("KIS_DATA_DIR", "KIS_LOG_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KIS_LOG_DIR", str(tmp_path / "logs"))
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    log_dir = runtime_path("log_dir")
    logger = logging.getLogger("test.runtime.path.creation")
    logger.setLevel(logging.INFO)
    handler = KSTDailyFileHandler(log_dir, "trader")
    handler.setFormatter(KSTFormatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("path-first-create-test")
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert list(log_dir.glob("trader_*.log"))
