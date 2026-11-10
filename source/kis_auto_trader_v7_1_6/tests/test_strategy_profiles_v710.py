from datetime import date, timedelta
from pathlib import Path
import yaml

from strategies.builder_engine.official_adapter import OfficialBuilderAdapter
from strategies.builder_engine.yaml_engine import YamlStrategyEngine

ROOT = Path(__file__).resolve().parents[1]

def _bars(count=280):
    start = date(2025, 1, 1)
    result = []
    for i in range(count):
        close = 1000 + i * 2
        result.append({
            "stck_bsop_date": (start + timedelta(days=i)).strftime("%Y%m%d"),
            "stck_oprc": str(close - 1), "stck_hgpr": str(close + 3),
            "stck_lwpr": str(close - 3), "stck_clpr": str(close),
            "acml_vol": str(100000 + i * 100),
        })
    return result

class _Api:
    def daily_prices(self, symbol, days=260):
        return _bars()

def test_strategy_profiles_has_unified_buy_and_sell_lists_without_legacy_profiles():
    raw = yaml.safe_load((ROOT / "config/strategy_profiles.yaml").read_text(encoding="utf-8"))
    assert "profiles" not in raw
    assert "enabled" not in raw["strategies"][0]
    assert len(raw["strategies"]) == 10
    assert raw["decision"]["buy"]["strategies"]
    assert raw["decision"]["sell"]["strategies"]
    buy_ids = {x["id"] for x in raw["decision"]["buy"]["strategies"]}
    sell_ids = {x["id"] for x in raw["decision"]["sell"]["strategies"]}
    assert "breakout_fail" not in buy_ids
    assert "breakout_fail" in sell_ids

def test_kis10_adapter_evaluates_unified_config_with_directional_lists():
    bars = _bars()
    adapter = OfficialBuilderAdapter(str(ROOT / "config/strategy_profiles.yaml"))
    result = adapter.evaluate("unified", ["005930"], api=_Api(), bars_by_symbol={"005930": bars})["005930"]
    assert result["action"] in {"BUY", "SELL", "HOLD", "ERROR"}
    assert len(result["strategy_results"]) == 10
    for item in result["strategy_results"]:
        assert "buy_enabled" in item and "sell_enabled" in item

def test_custom_yaml_files_only_define_their_own_direction():
    entry = yaml.safe_load((ROOT / "config/my_entry_strategy.kis.yaml").read_text(encoding="utf-8"))["strategy"]
    exit_ = yaml.safe_load((ROOT / "config/my_exit_strategy.kis.yaml").read_text(encoding="utf-8"))["strategy"]
    assert "entry" in entry and "exit" not in entry
    assert "exit" in exit_ and "entry" not in exit_
    engine = YamlStrategyEngine()
    bars = _bars()
    assert engine.evaluate(str(ROOT / "config/my_entry_strategy.kis.yaml"), bars, purpose="entry")["data_ready"]
    assert engine.evaluate(str(ROOT / "config/my_exit_strategy.kis.yaml"), bars, purpose="exit")["data_ready"]
