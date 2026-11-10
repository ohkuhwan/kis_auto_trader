"""V7 체결 반영 모듈의 핵심 상태 전이 회귀 테스트."""
from types import SimpleNamespace

from storage.state import StateStore
from trading.fill_handler import apply_fill_state
from trading.orders import FillEvent


def _apply(store, engine, fill):
    apply_fill_state(
        store,
        engine,
        fill,
        load_profile_risk=lambda _strategy: {},
        env_float=lambda _name, default: default,
        symbol_name=lambda symbol: f"name-{symbol}",
        symbol_label=lambda symbol, name="": f"{symbol}({name or 'unknown'})",
    )


def _engine():
    return SimpleNamespace(
        s=SimpleNamespace(
            aggressive=SimpleNamespace(cooldown_sec=90),
            stable=SimpleNamespace(cooldown_sec=60),
        )
    )


def test_buy_fill_updates_position_average_and_counts_only_first_fill(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    engine = _engine()
    first = FillEvent(
        order_no="B001", symbol="005930", side="BUY", qty=2, price=100.0,
        event_ts=1.0, first_fill=True, strategy="stable",
        cumulative_filled_qty=2, remaining_qty=3,
    )
    _apply(store, engine, first)
    second = FillEvent(
        order_no="B001", symbol="005930", side="BUY", qty=3, price=110.0,
        event_ts=2.0, first_fill=False, strategy="stable",
        cumulative_filled_qty=5, remaining_qty=0,
    )
    _apply(store, engine, second)

    position = store.get_position("005930")
    assert position["qty"] == 5
    assert position["avg_price"] == 106.0
    assert position["high_price"] == 110.0
    assert store.state.trades_today == 1
    assert StateStore(str(tmp_path / "state.json")).get_position("005930")["qty"] == 5


def test_sell_fill_updates_realized_pnl_and_removes_position_on_full_exit(tmp_path):
    store = StateStore(str(tmp_path / "state.json"))
    store.set_position("005930", {
        "symbol": "005930", "name": "삼성전자", "qty": 3,
        "avg_price": 100.0, "high_price": 120.0, "strategy": "stable",
        "pending_order_no": "S001", "pending_side": "SELL",
    })
    store.save()

    fill = FillEvent(
        order_no="S001", symbol="005930", side="SELL", qty=3, price=120.0,
        event_ts=3.0, first_fill=True, strategy="stable",
        cumulative_filled_qty=3, remaining_qty=0,
    )
    _apply(store, _engine(), fill)

    assert store.get_position("005930") is None
    assert store.state.daily_realized_pnl_krw > 0
    assert store.state.cooldowns["005930"] > 0
    assert StateStore(str(tmp_path / "state.json")).get_position("005930") is None
