"""통합 경로 구조에서 CLI와 마이그레이션 이후 재시작을 검증합니다."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from config.paths import PROJECT_ROOT
from scripts.upgrade_migrate import migrate


def test_strategy_log_cli_default_is_independent_of_cwd(tmp_path, monkeypatch, capsys):
    from scripts import analyze_strategy_logs
    from config.paths import runtime_path

    monkeypatch.setenv("KIS_LOG_DIR", str(tmp_path / "canonical-logs"))
    monkeypatch.delenv("STRATEGY_SIGNAL_LOG", raising=False)
    monkeypatch.chdir(tmp_path)
    log = runtime_path("strategy_signal_log")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"profile": "stable", "strategy_id": "test", "symbol": "005930", "action": "HOLD"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["analyze_strategy_logs.py"])
    assert analyze_strategy_logs.main() == 0
    assert str(log) in capsys.readouterr().out


def test_migration_then_first_start_and_restart_uses_new_json_layout(tmp_path, monkeypatch):
    from config.paths import runtime_path
    from storage.state import StateStore

    source = tmp_path / "old-install"
    destination = tmp_path / "new-install"
    source.mkdir()
    destination.mkdir()
    old_state = source / "state.json"
    old_state.write_text(json.dumps({"positions": {"005930": {"symbol": "005930", "qty": 3, "avg_price": 70000}}}), encoding="utf-8")
    old_cancel = source / "cancel_orders.json"
    old_cancel.write_text(json.dumps({"updated_at": 0, "records": {}}), encoding="utf-8")

    copied, skipped = migrate(source, destination)
    assert skipped == []
    assert any(line.endswith("json/state.json") and line.startswith("복사 완료:") for line in copied)
    assert any(line.endswith("json/cancel_orders.json") and line.startswith("복사 완료:") for line in copied)
    assert old_state.exists() and old_cancel.exists()
    assert (destination / "json/state.json").read_bytes() == old_state.read_bytes()

    # The destination package root becomes the root for default paths after installation.
    monkeypatch.setattr("config.paths.PROJECT_ROOT", destination)
    monkeypatch.setenv("KIS_DATA_DIR", "json")
    monkeypatch.delenv("STATE_FILE", raising=False)
    store = StateStore(str(runtime_path("state")))
    assert store.get_position("005930")["qty"] == 3
    store.set_position("005930", {"symbol": "005930", "qty": 2, "avg_price": 71000})
    store.save()
    restarted = StateStore(str(runtime_path("state")))
    assert restarted.get_position("005930")["qty"] == 2
    assert (destination / "json/cancel_orders.json").is_file()


def test_runtime_artifacts_are_ignored_by_release_gitignore():
    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required in ("/json/", "logs/*.log", "logs/*.jsonl", "logs/*.json", ".env", ".venv/", ".migration_backups/"):
        assert required in ignore
