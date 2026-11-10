"""V7.0.0: runtime 경로 변경/재시작에도 매매 안전장치가 유지되는지 검증."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading

import pytest


def _clear_legacy_path_overrides(monkeypatch):
    for name in (
        "STATE_FILE", "UNCERTAIN_ORDERS_FILE", "CANCEL_ORDERS_FILE",
        "KIS_TOKEN_CACHE_FILE", "HOLIDAY_CACHE_FILE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_custom_data_dir_preserves_state_and_uncertain_buy_lock_across_restart(tmp_path, monkeypatch):
    from config.paths import runtime_path
    from storage.state import StateStore
    from trading.orders import OrderManager
    from brokers.kis.client import AmbiguousOrderError

    _clear_legacy_path_overrides(monkeypatch)
    data_dir = tmp_path / "relocated-data"
    monkeypatch.setenv("KIS_DATA_DIR", str(data_dir))

    state_path = runtime_path("state")
    store = StateStore(str(state_path))
    store.set_position("005930", {
        "symbol": "005930", "name": "삼성전자", "qty": 2,
        "avg_price": 70000, "high_price": 71000, "strategy": "stable",
    })
    store.state.cooldowns["005930"] = 1234567890.0
    store.save()

    class AmbiguousApi:
        def order_cash(self, *args, **kwargs):
            raise AmbiguousOrderError("simulated timeout after POST")

    first = OrderManager(AmbiguousApi(), 30)
    assert first.uncertain_file == data_dir / "uncertain_orders.json"
    assert first.cancel_records_file == data_dir / "cancel_orders.json"
    with pytest.raises(AmbiguousOrderError):
        first.submit("005930", "BUY", 1, reservation_price=70000)
    assert first.pending_for_symbol("005930")

    # New process: state, cooldown, and uncertain-order exclusion must survive.
    restored_store = StateStore(str(runtime_path("state")))
    restored_manager = OrderManager(AmbiguousApi(), 30)
    assert restored_store.get_position("005930")["qty"] == 2
    assert restored_store.state.cooldowns["005930"] == 1234567890.0
    assert restored_manager.pending_for_symbol("005930")
    assert restored_manager.has_pending_buy_orders()
    assert json.loads(runtime_path("uncertain_orders").read_text(encoding="utf-8"))["orders"][0]["side"] == "BUY"


def test_broker_open_order_recovered_after_restart_still_locks_symbol(tmp_path, monkeypatch):
    from trading.orders import OrderManager

    monkeypatch.setenv("UNCERTAIN_ORDERS_FILE", str(tmp_path / "uncertain.json"))
    monkeypatch.setenv("CANCEL_ORDERS_FILE", str(tmp_path / "cancel.json"))
    manager = OrderManager(api=object(), timeout_sec=30)
    result = manager.sync_open_orders([{
        "odno": "ORDER-77", "pdno": "005930", "sll_buy_dvsn_cd": "02",
        "ord_qty": "5", "tot_ccld_qty": "2", "rmn_qty": "3",
        "ord_gno_brno": "001",
    }])

    assert result["added"] == 1
    assert manager.pending_for_symbol("005930")
    recovered = manager.snapshot()[0]
    assert recovered.order_no == "ORDER-77"
    assert recovered.qty == 5
    assert recovered.filled_qty == 2
    assert recovered.remaining_qty == 3


def test_corrupt_cancel_audit_file_is_not_overwritten_on_restart(tmp_path, monkeypatch):
    from trading.orders import OrderManager

    original = b'{"records": broken'
    cancel_path = tmp_path / "cancel.json"
    cancel_path.write_bytes(original)
    monkeypatch.setenv("CANCEL_ORDERS_FILE", str(cancel_path))
    monkeypatch.setenv("UNCERTAIN_ORDERS_FILE", str(tmp_path / "uncertain.json"))

    manager = OrderManager(api=object(), timeout_sec=30)
    assert manager.cancel_records_integrity_error
    manager._save_cancel_records()
    assert cancel_path.read_bytes() == original


def test_duplicate_fill_notice_and_stale_cumulative_notice_do_not_double_count():
    from trading.orders import OrderManager

    class Api:
        def order_cash(self, *args, **kwargs):
            return {"output": {"ODNO": "FILL-1", "KRX_FWDG_ORD_ORGNO": "001"}}

    manager = OrderManager(Api(), 30)
    manager.submit("005930", "BUY", 5)
    notice = {
        "ODER_NO": "FILL-1", "CNTG_YN": "2", "CNTG_QTY": "2",
        "TOT_CCLD_QTY": "2", "CNTG_UNPR": "70000", "STCK_CNTG_HOUR": "090001",
    }
    assert manager.on_notice(notice).qty == 2
    assert manager.on_notice(notice) is None
    stale = dict(notice, CNTG_QTY="1", TOT_CCLD_QTY="1", STCK_CNTG_HOUR="090000")
    assert manager.on_notice(stale) is None
    assert manager.pending["FILL-1"].filled_qty == 2


def test_order_post_ambiguous_exception_is_never_automatically_retried(tmp_path, monkeypatch):
    from brokers.kis.client import AmbiguousOrderError
    from trading.orders import OrderManager

    monkeypatch.setenv("UNCERTAIN_ORDERS_FILE", str(tmp_path / "uncertain.json"))
    monkeypatch.setenv("CANCEL_ORDERS_FILE", str(tmp_path / "cancel.json"))

    class Api:
        calls = 0
        def order_cash(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("simulated socket timeout")

    api = Api()
    manager = OrderManager(api, 30)
    with pytest.raises(AmbiguousOrderError):
        manager.submit("005930", "BUY", 1)
    assert api.calls == 1
    assert manager.pending_for_symbol("005930")
    assert manager.has_pending_buy_orders()
