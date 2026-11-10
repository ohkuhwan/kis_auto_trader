import pytest
from trading.strategy_decision import combine_buy, combine_sell

@pytest.mark.parametrize("mode,kis,custom,expected", [
    ("KIS10", "BUY", "HOLD", True),
    ("KIS10", "HOLD", "BUY", False),
    ("CUSTOM", "HOLD", "BUY", True),
    ("CUSTOM", "BUY", "HOLD", False),
    ("ALL", "BUY", "BUY", True),
    ("ALL", "BUY", "HOLD", False),
    ("ALL", "ERROR", "BUY", False),
])
def test_buy_combination(mode, kis, custom, expected):
    assert combine_buy(mode, kis, custom) is expected

@pytest.mark.parametrize("mode,kis,custom,expected", [
    ("KIS10", "SELL", "HOLD", True),
    ("CUSTOM", "HOLD", "SELL", True),
    ("ALL", "SELL", "HOLD", True),
    ("ALL", "HOLD", "SELL", True),
    ("ALL", "SELL", "SELL", True),
    ("ALL", "HOLD", "HOLD", False),
    ("ALL", "ERROR", "HOLD", False),
])
def test_sell_combination(mode, kis, custom, expected):
    assert combine_sell(mode, kis, custom) is expected

def test_invalid_mode_fails_closed():
    with pytest.raises(ValueError):
        combine_buy("UNKNOWN", "BUY", "BUY")
    with pytest.raises(ValueError):
        combine_sell("UNKNOWN", "SELL", "SELL")


def test_first_exit_threshold_requires_strategy_sell_and_second_is_forced():
    from application import _evaluate_exit_priority
    # 1차 손절: SELL 신호와 -1.5% 기준을 함께 충족해야 합니다.
    assert _evaluate_exit_priority(-1.6, True)[0] is True
    assert _evaluate_exit_priority(-1.6, False)[0] is False
    # 1차 익절: SELL 신호와 +1.5% 기준을 함께 충족해야 합니다.
    assert _evaluate_exit_priority(1.6, True)[0] is True
    assert _evaluate_exit_priority(1.6, False)[0] is False
    # 2차 기준은 전략 신호와 무관하게 작동합니다.
    assert _evaluate_exit_priority(-2.0, False)[0] is True
    assert _evaluate_exit_priority(3.0, False)[0] is True
