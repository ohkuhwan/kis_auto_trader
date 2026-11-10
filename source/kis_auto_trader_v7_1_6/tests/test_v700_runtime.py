from pathlib import Path
import re


def test_env_example_documents_every_setting_with_default_and_format():
    root = Path(__file__).resolve().parents[1]
    lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = [(i, re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)) for i, line in enumerate(lines)]
    assignments = [(i, match) for i, match in assignments if match]
    assert len(assignments) >= 90
    for index, match in assignments:
        key = match.group(1)
        prior_lines = lines[max(0, index - 10):index]
        prior = "\n".join(prior_lines)
        assert any(line.startswith("# [설정 메타]") for line in prior_lines), f"{key}: 기본값/형식 메타 누락"
        assert any(line.startswith("# [주의사항]") for line in prior_lines), f"{key}: 주의사항 누락"
        assert f"[{key}]" in prior, f"{key}: 항목 설명 누락"


def test_entrypoint_is_thin_and_application_has_start_stop_outcomes():
    root = Path(__file__).resolve().parents[1]
    entrypoint = (root / "main.py").read_text(encoding="utf-8")
    app = (root / "application.py").read_text(encoding="utf-8")
    assert len(entrypoint.splitlines()) < 30
    assert "_application.main()" in entrypoint
    assert "START_OUTCOME status=initialized" in app
    assert "STOP_OUTCOME status=graceful" in app


def test_start_stop_scripts_log_requested_action_and_actual_stop_outcome():
    root = Path(__file__).resolve().parents[1]
    start = (root / "scripts" / "start.sh").read_text(encoding="utf-8")
    stop = (root / "scripts" / "stop.sh").read_text(encoding="utf-8")
    assert "START_REQUEST trigger=$TRIGGER" in start and "START_LAUNCH trigger=$TRIGGER" in start
    assert "STOP_REQUEST trigger=$TRIGGER" in stop
    assert "STOP_OUTCOME status=stopped" in stop
    assert "STOP_OUTCOME status=timeout" in stop
    assert "STOP_GRACE_TIMEOUT_SEC" in stop and 'seq 1 "$STOP_GRACE_TIMEOUT_SEC"' in stop
    monitoring = (root / "scripts" / "start_monitoring.sh").read_text(encoding="utf-8")
    assert "TimeoutStopSec=300" in monitoring


def test_entry_mode_cycle_uses_defined_configuration_variable():
    """ENTRY_MODE=ALL cycle path must not reference an undefined `mode` name."""
    root = Path(__file__).resolve().parents[1]
    app = (root / "application.py").read_text(encoding="utf-8")
    assert 'if _entry_mode in {"HTS", "ALL"}:' in app
    assert 'if mode in {"HTS", "ALL"}:' not in app
