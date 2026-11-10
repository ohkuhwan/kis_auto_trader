"""Regression tests for internal strategy strength (0..1) to profile score (0..100)."""
from pathlib import Path
import yaml
import pytest

from strategies.builder_engine.official_adapter import OfficialBuilderAdapter
import strategies.builder_engine.internal_engine as internal_engine

@pytest.mark.parametrize(("signal_action", "expected_action", "score_key"), [
    ("BUY", "BUY", "score"),
    ("SELL", "SELL", "sell_score"),
])
def test_strength_070_is_70_profile_score_for_buy_and_sell(tmp_path, monkeypatch, signal_action, expected_action, score_key):
    strategy_file = tmp_path / "strategy.kis.yaml"
    strategy_file.write_text(yaml.safe_dump({"strategy": {"id": "test_strategy", "enabled": True}}), encoding="utf-8")
    profile_file = tmp_path / "strategy_profiles.yaml"
    profile_file.write_text(yaml.safe_dump({
        "strategies": [{"id": "test_strategy", "file": str(strategy_file)}],
        "decision": {
            "buy": {"strategies": [{"id": "test_strategy", "weight": 1}], "min_votes": 1, "min_score": 50},
            "sell": {"strategies": [{"id": "test_strategy", "weight": 1}], "min_votes": 1, "min_score": 50},
        },
        "logging": {"jsonl_path": str(tmp_path / "signals.jsonl")},
    }), encoding="utf-8")

    def fake_evaluate_strategy(*args, **kwargs):
        return {"action": signal_action, "strength": 0.70, "reason": "regression fixture", "status": "success"}

    monkeypatch.setattr(internal_engine, "evaluate_strategy", fake_evaluate_strategy)
    adapter = OfficialBuilderAdapter(str(profile_file))
    result = adapter.evaluate("unified", ["005930"], api=object(), bars_by_symbol={"005930": [{"fixture": True}]})["005930"]

    assert result[score_key] == 70.0
    assert result["action"] == expected_action


def test_strength_is_clamped_to_unit_interval_before_weighted_aggregation(tmp_path, monkeypatch):
    strategy_file = tmp_path / "strategy.kis.yaml"
    strategy_file.write_text(yaml.safe_dump({"strategy": {"id": "test_strategy", "enabled": True}}), encoding="utf-8")
    profile_file = tmp_path / "strategy_profiles.yaml"
    profile_file.write_text(yaml.safe_dump({
        "strategies": [{"id": "test_strategy", "file": str(strategy_file)}],
        "decision": {
            "buy": {"strategies": [{"id": "test_strategy", "weight": 1}], "min_votes": 1, "min_score": 99},
            "sell": {"strategies": [{"id": "test_strategy", "weight": 1}], "min_votes": 1, "min_score": 99},
        },
        "logging": {"jsonl_path": str(tmp_path / "signals.jsonl")},
    }), encoding="utf-8")
    monkeypatch.setattr(internal_engine, "evaluate_strategy", lambda *a, **kw: {
        "action": "BUY", "strength": 1.7, "reason": "out-of-range fixture", "status": "success"
    })
    result = OfficialBuilderAdapter(str(profile_file)).evaluate(
        "unified", ["005930"], api=object(), bars_by_symbol={"005930": [{"fixture": True}]}
    )["005930"]
    assert result["score"] == 100.0
    assert result["action"] == "BUY"
