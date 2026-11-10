from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_account_order_error_locks_both_sides_and_survives_restart(tmp_path, monkeypatch):
    from brokers.kis.client import KisApiError
    from trading.orders import AccountOrderLockedError, OrderManager

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))

    class FailingApi:
        def __init__(self):
            self.calls = []
        def order_cash(self, symbol, side, qty, **kwargs):
            self.calls.append((symbol, side, qty))
            raise KisApiError("expired mock account", msg_cd="40910000",
                              msg1="모의투자 주문이 불가한 계좌입니다.")

    first_api = FailingApi()
    first = OrderManager(first_api, timeout_sec=60)
    with pytest.raises(AccountOrderLockedError):
        first.submit("005930", "BUY", 1)
    assert first.account_order_locked is True
    assert len(first_api.calls) == 1

    lock_path = tmp_path / "account_order_lock.json"
    saved = json.loads(lock_path.read_text(encoding="utf-8"))
    assert saved["locked"] is True
    assert saved["msg_cd"] == "40910000"
    assert saved["requires_manual_release"] is True

    second_api = FailingApi()
    second = OrderManager(second_api, timeout_sec=60)
    with pytest.raises(AccountOrderLockedError):
        second.submit("005930", "SELL", 1)
    assert second_api.calls == []


@pytest.mark.parametrize("code", ["40910000", "IGW00002", "90070000"])
def test_account_context_error_callback_latches_persistent_lock(tmp_path, monkeypatch, code):
    from trading.orders import OrderManager

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))

    class Api:
        pass

    manager = OrderManager(Api(), timeout_sec=60)
    assert manager.api.account_error_handler(
        msg_cd=code, msg1="account context mismatch", method="GET", path="/balance"
    ) is True
    assert manager.account_order_locked
    assert manager.account_order_lock["msg_cd"] == code
    assert manager.mark_account_order_lock_alerted() is True
    assert manager.mark_account_order_lock_alerted() is False


def test_non_account_order_error_does_not_latch_global_lock(tmp_path, monkeypatch):
    from trading.orders import OrderManager

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    manager = OrderManager(SimpleNamespace(), timeout_sec=60)
    assert manager.latch_account_order_lock_from_error(
        msg_cd="40070000", msg1="매매불가 종목", method="POST", path="/order"
    ) is False
    assert manager.account_order_locked is False


def test_corrupt_account_lock_file_fails_closed_and_is_not_overwritten(tmp_path, monkeypatch):
    from trading.orders import OrderManager

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    lock_path = tmp_path / "account_order_lock.json"
    lock_path.write_text("{broken", encoding="utf-8")
    manager = OrderManager(SimpleNamespace(), timeout_sec=60)
    assert manager.account_order_locked is True
    assert lock_path.read_text(encoding="utf-8") == "{broken"


def test_condition_result_maps_mca05918_to_empty_list_and_keeps_seq_zero():
    from brokers.kis.client import KisApi, KisApiError

    api = KisApi.__new__(KisApi)
    api.s = SimpleNamespace(hts_id="test-user")
    seen = {}

    def request(method, path, *, tr_id, params=None, **kwargs):
        seen.update(method=method, path=path, tr_id=tr_id, params=params)
        raise KisApiError("no condition matches", msg_cd="MCA05918",
                          msg1="종목코드 오류입니다.")

    api._request = request
    assert api.condition_result("0") == []
    assert seen["params"]["seq"] == "0"

    api.condition_titles = lambda: [{"condition_nm": "모의투자", "seq": "0"}]
    assert api.find_condition_seq("모의투자") == "0"


def test_token_cache_fingerprint_changes_when_mock_account_changes(tmp_path, monkeypatch):
    from brokers.kis.client import KisApi
    from config.settings import load_settings

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KIS_PAPER", "true")
    monkeypatch.setenv("KIS_APP_KEY", "test-app-key")
    monkeypatch.setenv("KIS_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("KIS_HTS_ID", "test-hts")
    monkeypatch.setenv("KIS_CANO", "50000001")
    first = KisApi(load_settings())

    monkeypatch.setenv("KIS_CANO", "50000002")
    second = KisApi(load_settings())
    assert first._credential_fingerprint != second._credential_fingerprint


def test_cancel_post_is_blocked_after_account_lock(tmp_path, monkeypatch):
    from trading.orders import AccountOrderLockedError, OrderManager, PendingOrder

    monkeypatch.setenv("KIS_DATA_DIR", str(tmp_path))

    class Api:
        def __init__(self):
            self.cancel_calls = 0
        def cancel_order(self, *args):
            self.cancel_calls += 1
            return {}

    api = Api()
    manager = OrderManager(api, timeout_sec=60)
    manager.latch_account_order_lock_from_error(
        msg_cd="IGW00002", msg1="account mismatch", method="POST", path="/order"
    )
    pending = PendingOrder(
        order_no="12345", branch_no="00001", symbol="005930",
        side="SELL", qty=1, filled_qty=0,
    )
    with pytest.raises(AccountOrderLockedError):
        manager.request_cancel(pending)
    assert api.cancel_calls == 0


def test_operator_unlock_script_requires_confirmation_and_preserves_audit(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path

    lock_path = tmp_path / "account_order_lock.json"
    lock_path.write_text(json.dumps({
        "schema_version": 1, "locked": True, "msg_cd": "40910000",
        "msg1": "expired mock account", "alert_sent": True,
    }), encoding="utf-8")
    env = os.environ.copy()
    env["KIS_DATA_DIR"] = str(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "unlock_account_order_lock.py"

    missing_confirmation = subprocess.run(
        [sys.executable, str(script), "--reason", "verified account and API registration"],
        env=env, capture_output=True, text=True,
    )
    assert missing_confirmation.returncode != 0
    assert json.loads(lock_path.read_text(encoding="utf-8"))["locked"] is True

    released = subprocess.run(
        [sys.executable, str(script), "--confirm-account-verified",
         "--reason", "new mock account API and REST WebSocket verified"],
        env=env, capture_output=True, text=True,
    )
    assert released.returncode == 0, released.stderr
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["locked"] is False
    assert data["msg_cd"] == "40910000"
    assert data["manual_release_confirmed"] is True
    assert "new mock account" in data["release_reason"]
