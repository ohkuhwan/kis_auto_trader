"""Regression tests for the strategy-test shell runner's Python selection."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _make_fake_python(path: Path, log_path: Path) -> None:
    """Create a tiny interpreter stub that accepts dependency checks and logs execution."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "if [[ \"${1:-}\" == '-c' ]]; then exit 0; fi\n"
        f"printf '%s\\n' \"$*\" > {str(log_path)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _copy_runner_project(tmp_path: Path) -> Path:
    """Create the minimum isolated project tree required by the shell runner."""
    root = tmp_path / "project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    source = Path(__file__).resolve().parent / "kis_strategy_test.sh"
    runner = tests / "kis_strategy_test.sh"
    runner.write_bytes(source.read_bytes())
    runner.chmod(0o755)
    (tests / "kis_strategy_test.py").write_text("# stub\n", encoding="utf-8")
    return root


def test_runner_prefers_project_virtualenv_python(tmp_path: Path) -> None:
    root = _copy_runner_project(tmp_path)
    selected_log = tmp_path / "selected-python.log"
    _make_fake_python(root / ".venv/bin/python", selected_log)

    env = os.environ.copy()
    env.pop("PYTHON_BIN", None)
    result = subprocess.run(
        [str(root / "tests/kis_strategy_test.sh"), "--offline", "--sample-size", "10", "--seed", "42"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert selected_log.read_text(encoding="utf-8").strip() == "tests/kis_strategy_test.py --offline --sample-size 10 --seed 42"


def test_runner_honors_explicit_python_bin_override(tmp_path: Path) -> None:
    root = _copy_runner_project(tmp_path)
    default_log = tmp_path / "default-python.log"
    override_log = tmp_path / "override-python.log"
    _make_fake_python(root / ".venv/bin/python", default_log)
    override = tmp_path / "custom-python"
    _make_fake_python(override, override_log)

    env = os.environ.copy()
    env["PYTHON_BIN"] = str(override)
    result = subprocess.run(
        [str(root / "tests/kis_strategy_test.sh"), "--offline", "--sample-size", "5", "--seed", "42"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert override_log.read_text(encoding="utf-8").strip() == "tests/kis_strategy_test.py --offline --sample-size 5 --seed 42"
    assert not default_log.exists()


def test_offline_strategy_test_respects_isolated_log_dir(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    isolated_logs = tmp_path / "isolated-logs"
    env = os.environ.copy()
    env["KIS_LOG_DIR"] = str(isolated_logs)
    env["KIS_PAPER"] = "true"
    env["KIS_ENABLE_TRADING"] = "false"
    result = subprocess.run(
        [
            os.sys.executable,
            str(root / "tests" / "kis_strategy_test.py"),
            "--offline", "--sample-size", "5", "--seed", "42",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    result_path = isolated_logs / "kis_strategy_test_results.json"
    assert result_path.is_file()
    assert not (root / "logs" / "kis_strategy_test_results.json").exists()
    import csv, json
    database_codes = {
        row[1].strip().zfill(6)
        for row in csv.reader((root / "tests" / "data" / "symbol-properties-database.csv").open(encoding="utf-8"))
        if len(row) >= 4 and row[1].strip().isdigit()
    }
    records = json.loads(result_path.read_text(encoding="utf-8"))["results"]
    sampled_codes = {row["symbol"] for row in records}
    assert len(sampled_codes) == 5
    assert sampled_codes <= database_codes


def test_kis_strategy_test_refuses_live_api_environment(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "KIS_PAPER": "false",
        "KIS_APP_KEY": "TEST_ONLY_KEY",
        "KIS_APP_SECRET": "TEST_ONLY_SECRET",
        "KIS_LOG_DIR": str(tmp_path / "logs"),
        "KIS_ENABLE_TRADING": "false",
    })
    result = subprocess.run(
        [os.sys.executable, str(root / "tests" / "kis_strategy_test.py"), "--sample-size", "5", "--seed", "42"],
        cwd=root, env=env, text=True, capture_output=True, check=False, timeout=15,
    )
    assert result.returncode == 3
    assert "KIS_PAPER=true만 허용" in result.stderr
    assert not (tmp_path / "logs" / "kis_strategy_test_results.json").exists()
