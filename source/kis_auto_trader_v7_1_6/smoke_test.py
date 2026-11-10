"""smoke_test.py - KIS 서버 없이 호환 핵심 상태머신을 검증합니다."""

import os
import tempfile
import time
from pathlib import Path

# smoke test를 직접 실행해도 프로젝트 루트의 운영 상태/주문 기록/로그를 건드리지 않습니다.
# 임시 디렉터리는 프로세스 종료 시 TemporaryDirectory가 정리합니다.
_SMOKE_RUNTIME = tempfile.TemporaryDirectory(prefix="kis-auto-trader-smoke-")
_SMOKE_RUNTIME_DIR = Path(_SMOKE_RUNTIME.name).resolve()
os.environ.update({
    "KIS_TEST_MODE": "1",
    "KIS_TEST_RUNTIME_DIR": str(_SMOKE_RUNTIME_DIR),
    "KIS_DATA_DIR": str(_SMOKE_RUNTIME_DIR / "json"),
    "KIS_LOG_DIR": str(_SMOKE_RUNTIME_DIR / "logs"),
    "STRATEGY_SIGNAL_LOG": str(_SMOKE_RUNTIME_DIR / "logs" / "strategy_signals.jsonl"),
    "STATE_FILE": str(_SMOKE_RUNTIME_DIR / "state.json"),
    "UNCERTAIN_ORDERS_FILE": str(_SMOKE_RUNTIME_DIR / "uncertain_orders.json"),
    "CANCEL_ORDERS_FILE": str(_SMOKE_RUNTIME_DIR / "cancel_orders.json"),
    "HOLIDAY_CACHE_FILE": str(_SMOKE_RUNTIME_DIR / "holiday_config.json"),
    "KIS_TOKEN_CACHE_FILE": str(_SMOKE_RUNTIME_DIR / "kis_tokens.json"),
    "KIS_ENABLE_TRADING": "false",
    "KIS_PAPER": "true",
    "KIS_APP_KEY": "TEST_ONLY",
    "KIS_APP_SECRET": "TEST_ONLY",
    "KIS_HTS_ID": "TEST_ONLY",
    "KIS_CANO": "00000000",
})

# 개발/검증 환경에 선택 패키지가 없어도 상태머신 테스트를 실행할 수 있도록 최소 stub을 둡니다.
try:
    import dotenv  # noqa: F401
except ImportError:
    import sys, types
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

try:
    import Crypto  # noqa: F401
except ImportError:
    import sys, types
    crypto = types.ModuleType("Crypto")
    cipher = types.ModuleType("Crypto.Cipher")
    padding = types.ModuleType("Crypto.Util.Padding")

    class _AES:
        MODE_CBC = 2

        @staticmethod
        def new(*args, **kwargs):
            raise RuntimeError("smoke test에서는 AES 복호화를 실행하지 않습니다.")

    cipher.AES = _AES
    padding.unpad = lambda data, block_size: data
    sys.modules["Crypto"] = crypto
    sys.modules["Crypto.Cipher"] = cipher
    sys.modules["Crypto.Cipher.AES"] = cipher
    sys.modules["Crypto.Util"] = types.ModuleType("Crypto.Util")
    sys.modules["Crypto.Util.Padding"] = padding

try:
    import websockets  # noqa: F401
except ImportError:
    import sys, types
    websockets = types.ModuleType("websockets")

    async def _connect(*args, **kwargs):
        raise RuntimeError("smoke test에서는 실제 WebSocket 연결을 실행하지 않습니다.")

    websockets.connect = _connect
    sys.modules["websockets"] = websockets

from config import load_settings
from trading.orders import OrderManager
from storage.state import StateStore
from strategies.engine import StrategyEngine
from brokers.kis.realtime import KisRealtime
from trading.reconcile import reconcile


class DummyApi:
    """주문/WebSocket 테스트에 필요한 최소 API mock입니다."""

    class S:
        paper = True
        hts_id = "TESTHTS"
        ws_url = "ws://127.0.0.1:1"

    s = S()
    approval_key = "dummy"
    open_rows = []
    cancel_calls = []

    def issue_ws_approval_key(self):
        return self.approval_key

    def order_cash(self, symbol, side, qty, price=0, market=True):
        return {
            "output": {
                "ODNO": "12345678",
                "KRX_FWDG_ORD_ORGNO": "000000",
            }
        }

    def cancel_order(self, order_no, branch_no):
        self.cancel_calls.append((order_no, branch_no))
        return {"rt_cd": "0"}


class ReconcileApi(DummyApi):
    """수동 매도 후 잔고 및 미체결 주문 동기화 테스트용 API."""
    def __init__(self, holdings=None, open_rows=None):
        self._holdings = holdings or []
        self._open_rows = open_rows or []

    def balance(self):
        return self._holdings, {}

    def daily_orders(self, start_date, end_date, order_no="", ccld_dvsn="00"):
        return self._open_rows


def test_settings():
    """Settings 생성과 호환 설정이 정상인지 확인합니다."""
    s = load_settings()
    assert s.max_candidates > 0
    assert s.order_qty > 0
    assert s.sell_retry_cooldown_sec > 0


def test_state_atomic():
    """상태 저장/복구와 cooldown을 검증합니다."""
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "state.json")
        store = StateStore(path)
        store.set_position(
            "005930",
            {
                "symbol": "005930",
                "qty": 1,
                "avg_price": 100.0,
                "high_price": 100.0,
                "strategy": "stable",
            },
        )
        store.increment_trade_count()
        store.mark_exit_cooldown("005930", 60)
        store.save()
        loaded = StateStore(path)
        assert loaded.get_position("005930")["qty"] == 1
        assert loaded.state.trades_today == 1
        assert loaded.cooldown_active("005930")


def test_manual_sale_removes_local_position_and_persists():
    """앱에서 전량 매도한 뒤 실제 잔고 0이면 state.json 보유 종목을 제거합니다."""
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "state.json")
        store = StateStore(path)
        store.set_position("005930", {
            "symbol": "005930", "name": "삼성전자", "qty": 16,
            "avg_price": 100.0, "high_price": 110.0, "strategy": "stable",
        })
        store.save()
        api = ReconcileApi(holdings=[], open_rows=[])
        orders = OrderManager(api, 8)
        result = reconcile(api, store, orders)
        assert result["positions"] == 0
        assert store.symbols() == []
        loaded = StateStore(path)
        assert loaded.symbols() == []


def test_manual_partial_sale_updates_quantity_and_restores_sell_pending():
    """부분 매도 후 잔고 수량과 앱 매도 미체결 주문을 함께 반영합니다."""
    with tempfile.TemporaryDirectory() as d:
        path = str(Path(d) / "state.json")
        store = StateStore(path)
        store.set_position("005930", {
            "symbol": "005930", "name": "삼성전자", "qty": 16,
            "avg_price": 100.0, "high_price": 110.0, "strategy": "stable",
        })
        store.save()
        api = ReconcileApi(
            holdings=[{
                "pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "10",
                "pchs_avg_pric": "100.0", "prpr": "105.0",
            }],
            open_rows=[{
                "odno": "76543210", "ord_gno_brno": "000111", "pdno": "005930",
                "sll_buy_dvsn_cd": "01", "ord_qty": "6", "tot_ccld_qty": "2",
            }],
        )
        orders = OrderManager(api, 8)
        reconcile(api, store, orders)
        assert store.get_position("005930")["qty"] == 10
        assert orders.has_pending()
        pending = orders.snapshot()[0]
        assert pending.side == "SELL"
        assert pending.remaining_qty == 4


def test_order_full_fill():
    """웹소켓 전량 체결 후에도 REST 잔고/미체결 대사 전까지 pending 잠금을 유지합니다."""
    api = DummyApi()
    orders = OrderManager(api, 8)
    orders.submit("005930", "BUY", 1)
    assert orders.pending_for_symbol("005930")
    assert orders.on_notice({"ODER_NO": "12345678", "CNTG_YN": "1", "CNTG_QTY": "1", "CNTG_UNPR": "100"}) is None
    fill = orders.on_notice({"ODER_NO": "12345678", "CNTG_YN": "2", "CNTG_QTY": "1", "CNTG_UNPR": "100"})
    assert fill is not None
    assert fill.qty == 1
    assert orders.has_pending()
    orders.pending["12345678"].created_ts = time.time() - 30
    orders.sync_open_orders([])  # production reconcile() calls balance() immediately before this
    assert not orders.has_pending()


def test_order_partial_fill():
    """부분체결은 pending을 유지하고 누적 체결량을 증가시키는지 확인합니다."""
    api = DummyApi()
    orders = OrderManager(api, 8)
    orders.submit("005930", "BUY", 5)
    first = orders.on_notice({"ODER_NO": "12345678", "CNTG_YN": "2", "CNTG_QTY": "2", "CNTG_UNPR": "100"})
    assert first is not None and first.first_fill
    assert orders.pending["12345678"].filled_qty == 2
    second = orders.on_notice({"ODER_NO": "12345678", "CNTG_YN": "2", "CNTG_QTY": "3", "CNTG_UNPR": "101", "STCK_CNTG_HOUR": "123501"})
    assert second is not None and not second.first_fill
    assert orders.has_pending()
    orders.pending["12345678"].created_ts = time.time() - 30
    orders.sync_open_orders([])
    assert not orders.has_pending()


def test_broker_query_race_does_not_drop_fresh_pending():
    """신규 주문이 broker 미체결 조회에 잠시 늦게 나타나도 pending을 조기 삭제하지 않습니다."""
    api = DummyApi()
    orders = OrderManager(api, 8)
    orders.submit("005930", "BUY", 1)
    result = orders.sync_open_orders([])
    assert result["removed"] == 0
    assert orders.has_pending()


def test_cancel_then_broker_remove():
    """취소 요청 후 broker 미체결 목록에서 사라지면 local pending도 제거되는지 확인합니다."""
    api = DummyApi()
    orders = OrderManager(api, 1)
    orders.submit("0004Y0", "BUY", 1)
    pending = orders.timed_out(time.time() + 2)[0]
    api.cancel_order(pending.order_no, pending.branch_no)
    orders.mark_cancel_attempted(pending.order_no)
    assert orders.has_pending()

    result = orders.sync_open_orders([])
    assert result["removed"] == 1
    assert not orders.has_pending()


def test_cancel_rejected_then_remote_closed():
    """취소 잔량 없음 응답 후, remote 미체결 0건이면 local pending을 정리합니다."""
    api = DummyApi()
    orders = OrderManager(api, 1)
    orders.submit("005930", "BUY", 1)
    pending = orders.timed_out(time.time() + 2)[0]
    # main.py에서 KIS 40330000을 받았을 때 수행하는 상태 전이를 모사합니다.
    orders.mark_cancel_attempted(pending.order_no)
    result = orders.sync_open_orders([])
    assert result["removed"] == 1
    assert not orders.has_pending()


def test_remote_missing_old_pending_cleanup():
    """취소 플래그가 유실되어도 충분히 오래된 주문은 remote 0건 조회로 정리합니다."""
    api = DummyApi()
    orders = OrderManager(api, 8)
    order = orders.submit("005930", "BUY", 1)
    order.created_ts = time.time() - 60
    result = orders.sync_open_orders([])
    assert result["removed"] == 1
    assert not orders.has_pending()


def test_fresh_pending_grace_period():
    """주문 접수 직후 broker 조회에 아직 안 보이는 pending은 유예합니다."""
    api = DummyApi()
    orders = OrderManager(api, 8)
    orders.submit("005930", "BUY", 1)
    result = orders.sync_open_orders([])
    assert result["removed"] == 0
    assert orders.has_pending()


def test_restart_recovery_open_order():
    """로컬에 없던 broker 미체결 주문을 재시작 시 복구할 수 있는지 확인합니다."""
    api = DummyApi()
    orders = OrderManager(api, 8)
    result = orders.sync_open_orders([
        {
            "odno": "87654321",
            "ord_gno_brno": "111111",
            "pdno": "000660",
            "sll_buy_dvsn_cd": "02",
            "ord_qty": "3",
            "tot_ccld_qty": "1",
        }
    ])
    assert result["added"] == 1
    assert orders.pending_for_symbol("000660")
    assert orders.pending["87654321"].filled_qty == 1


def test_strategy_exit():
    """손절 신호가 정상 계산되는지 확인합니다."""
    engine = StrategyEngine(load_settings())
    hit, reason = engine.exit_signal(
        {"avg_price": 100, "high_price": 100, "strategy": "stable"},
        98.0,
    )
    assert hit and "STOP_LOSS" in reason


def test_realtime_subscribe_before_connect():
    """WebSocket 연결 전에 subscribe 요청을 저장해도 예외가 없어야 합니다."""
    events = []
    rt = KisRealtime(DummyApi(), lambda x: events.append(x), lambda x: events.append(x))
    rt.subscribe_symbols(["005930", "000660"])
    assert "005930" in rt.desired_symbols
    assert "000660" in rt.desired_symbols


def main():
    """모든 offline smoke test를 실행합니다."""
    tests = [
        test_settings,
        test_state_atomic,
        test_manual_sale_removes_local_position_and_persists,
        test_manual_partial_sale_updates_quantity_and_restores_sell_pending,
        test_order_full_fill,
        test_order_partial_fill,
        test_broker_query_race_does_not_drop_fresh_pending,
        test_cancel_then_broker_remove,
        test_cancel_rejected_then_remote_closed,
        test_remote_missing_old_pending_cleanup,
        test_fresh_pending_grace_period,
        test_restart_recovery_open_order,
        test_strategy_exit,
        test_realtime_subscribe_before_connect,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Compatibility smoke tests: PASS")


if __name__ == "__main__":
    main()
