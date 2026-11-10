from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_setup_script_exists_and_is_executable():
    path = ROOT / "scripts" / "setup.sh"
    assert path.is_file()
    assert path.stat().st_mode & 0o111


def test_setup_runs_requested_validation_and_installs_dev_dependencies():
    script = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    for required in (
        "python3 -m venv .venv",
        '"$PY" -m pip install --upgrade pip',
        '"$PY" -m pip install -r requirements.txt',
        '"$PY" -m pip install -r requirements-dev.txt',
        '"$PY" -m compileall -q .',
        '"$PY" smoke_test.py',
        '"$PY" -m pytest -q',
        '"$PY" monitoring/healthcheck.py',
        'scripts/start_monitoring.sh',
        'SETUP_ALLOW_LIVE_TRADING=YES',
        '지속 모니터링 상태를 확인합니다.',
        'kis-auto-trader-start.timer',
        'kis-auto-trader-watchdog.timer',
    ):
        assert required in script


def test_setup_preserves_existing_env_and_does_not_double_start():
    script = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    assert 'if [[ ! -f .env ]]; then' in script
    assert 'cp .env.example .env' in script
    assert '"$PROJECT_ROOT/scripts/start_monitoring.sh"' in script
    assert '직접 start.sh를 다시 실행하지 않습니다' in script


def test_setup_isolates_tests_from_operational_state_and_logs():
    script = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    for required in (
        'TEST_RUNTIME_DIR="$(mktemp -d',
        'export KIS_DATA_DIR="$TEST_RUNTIME_DIR/json"',
        'export KIS_LOG_DIR="$TEST_RUNTIME_DIR/logs"',
        'export STRATEGY_SIGNAL_LOG="$TEST_RUNTIME_DIR/logs/strategy_signals.jsonl"',
        'export STATE_FILE="$TEST_RUNTIME_DIR/state.json"',
        'export UNCERTAIN_ORDERS_FILE="$TEST_RUNTIME_DIR/uncertain_orders.json"',
        'export CANCEL_ORDERS_FILE="$TEST_RUNTIME_DIR/cancel_orders.json"',
        'export HOLIDAY_CACHE_FILE="$TEST_RUNTIME_DIR/holiday_config.json"',
        'export KIS_ENABLE_TRADING=false',
        'cleanup_test_runtime',
        'unset KIS_TEST_MODE KIS_TEST_RUNTIME_DIR KIS_DATA_DIR KIS_LOG_DIR STRATEGY_SIGNAL_LOG STATE_FILE UNCERTAIN_ORDERS_FILE CANCEL_ORDERS_FILE HOLIDAY_CACHE_FILE',
    ):
        assert required in script


def test_pytest_has_default_runtime_isolation():
    source = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'KIS_DATA_DIR' in source
    assert 'KIS_LOG_DIR' in source
    assert 'STATE_FILE' in source
    assert 'UNCERTAIN_ORDERS_FILE' in source
    assert 'CANCEL_ORDERS_FILE' in source
    assert 'STRATEGY_SIGNAL_LOG' in source
    assert 'HOLIDAY_CACHE_FILE' in source
    assert 'pytest_sessionfinish' in source
    assert 'shutil.rmtree(_runtime_dir, ignore_errors=True)' in source
    assert 'os.environ["CANCEL_ORDERS_FILE"] = str(_runtime_dir / "cancel_orders.json")' in source


def test_setup_script_clears_temporary_token_cache_override():
    text = (Path(__file__).resolve().parents[1] / "scripts" / "setup.sh").read_text(encoding="utf-8")
    assert "export KIS_TOKEN_CACHE_FILE=\"$TEST_RUNTIME_DIR/kis_tokens.json\"" in text
    assert "unset KIS_TEST_MODE KIS_TEST_RUNTIME_DIR KIS_DATA_DIR KIS_LOG_DIR STRATEGY_SIGNAL_LOG STATE_FILE UNCERTAIN_ORDERS_FILE CANCEL_ORDERS_FILE HOLIDAY_CACHE_FILE KIS_TOKEN_CACHE_FILE" in text


def test_smoke_test_forces_all_mutable_paths_into_temporary_runtime():
    source = (ROOT / "smoke_test.py").read_text(encoding="utf-8")
    assert 'tempfile.TemporaryDirectory(prefix="kis-auto-trader-smoke-")' in source
    assert '"CANCEL_ORDERS_FILE": str(_SMOKE_RUNTIME_DIR / "cancel_orders.json")' in source
    assert '"STRATEGY_SIGNAL_LOG": str(_SMOKE_RUNTIME_DIR / "logs" / "strategy_signals.jsonl")' in source
    assert '"KIS_ENABLE_TRADING": "false"' in source
    assert '"KIS_TEST_MODE": "1"' in source
    assert '"KIS_DATA_DIR": str(_SMOKE_RUNTIME_DIR / "json")' in source


def test_central_runtime_path_module_and_json_layout_are_documented():
    root = Path(__file__).resolve().parents[1]
    paths = (root / "config" / "paths.py").read_text(encoding="utf-8")
    env = (root / ".env.example").read_text(encoding="utf-8")
    assert 'PROJECT_ROOT = Path(__file__).resolve().parents[1]' in paths
    assert 'PROJECT_ROOT / "json"' in paths
    assert 'KIS_DATA_DIR=json' in env
    assert 'KIS_DATA_DIR/kis_tokens.json' in env


def test_setup_grants_execute_permissions_to_project_shell_scripts():
    script = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    assert 'find "$PROJECT_ROOT"' in script
    assert '"$PROJECT_ROOT/.venv" -prune' in script
    assert '"$PROJECT_ROOT/.git" -prune' in script
    assert '"$PROJECT_ROOT/tests"' not in script  # all project-owned dirs are found recursively
    assert "-type f -name '*.sh' -print0" in script


def test_config_paths_import_does_not_require_runtime_dependencies():
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import config.paths; assert 'config.settings' not in sys.modules"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_env_metadata_is_before_each_setting_and_lines_fit_terminal_width():
    import re, unicodedata
    env = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = [i for i, line in enumerate(env) if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line)]
    assert len(assignments) == len(set(re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", env[i]).group(1) for i in assignments))
    assert len(assignments) >= 90
    for i in assignments:
        prior = env[max(0, i-4):i]
        assert any("[설정 메타]" in line for line in prior), env[i]
        assert any("[주의사항]" in line for line in prior), env[i]
    width = lambda line: sum(2 if unicodedata.east_asian_width(ch) in "FWA" else 1 for ch in line)
    assert not [(i+1, width(line), line) for i, line in enumerate(env) if line.startswith("#") and width(line) > 80]


def test_docs_are_grouped_and_vendor_is_removed():
    assert (ROOT / "docs" / "operations" / "V7.0.0_OPERATIONS_MANUAL.md").is_file()
    assert (ROOT / "docs" / "architecture" / "V7.0.0_SYSTEM_FLOW.mmd").is_file()
    assert (ROOT / "docs" / "configuration" / "CONFIGURATION_REFERENCE.md").is_file()
    assert (ROOT / "docs" / "validation" / "V7.0.0_VALIDATION.md").is_file()
    assert not (ROOT / "vendor").exists()
    assert not list((ROOT / "docs").glob("CHANGELOG_V5*.md"))
