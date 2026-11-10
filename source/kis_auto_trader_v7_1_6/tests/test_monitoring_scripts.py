"""Regression checks for Ubuntu/Proot monitoring scripts."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def _install_stdlib_python(fakebin: Path):
    """Avoid the injected sitecustomize overhead in stdlib-only shell integration tests."""
    wrapper = fakebin / "python3"
    wrapper.write_text('#!/usr/bin/env bash\nexec /usr/bin/python3 "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)


def _install_fake_sudo(fakebin: Path):
    """Keep systemd integration tests isolated from the runner's real sudo."""
    wrapper = fakebin / "sudo"
    wrapper.write_text('#!/usr/bin/env bash\nexec "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)


def test_monitoring_entrypoints_exist_and_are_executable():
    for rel in (
        "scripts/start_monitoring.sh",
        "scripts/stop_monitoring.sh",
        "monitoring/monitor_loop.sh",
        "monitoring/watchdog.sh",
        "scripts/start.sh",
        "scripts/stop.sh",
    ):
        path = ROOT / rel
        assert path.is_file(), rel
        assert path.stat().st_mode & 0o111, rel


def test_all_shell_scripts_pass_bash_syntax_check():
    scripts = [
        ROOT / "scripts/start_monitoring.sh",
        ROOT / "scripts/stop_monitoring.sh",
        ROOT / "scripts/start.sh",
        ROOT / "scripts/stop.sh",
        ROOT / "monitoring/monitor_loop.sh",
        ROOT / "monitoring/watchdog.sh",
    ]
    result = subprocess.run(["bash", "-n", *map(str, scripts)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_monitoring_scripts_detect_project_path_and_kst_cutoff():
    start = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    loop = (ROOT / "monitoring/monitor_loop.sh").read_text(encoding="utf-8")
    watchdog = (ROOT / "monitoring/watchdog.sh").read_text(encoding="utf-8")
    assert 'pwd -P' in start and 'PROJECT_ROOT=' in start
    assert 'Asia/Seoul' in loop and 'AUTO_SHUTDOWN_TIME' in loop
    assert 'monitoring_${log_date}.log' in loop and '$LOG_DIR/' in loop
    assert 'Asia/Seoul' in watchdog and 'AUTO_SHUTDOWN_TIME' in watchdog
    assert 'skip=after_cutoff' in watchdog
    assert 'watchdog_${d}.log' in watchdog and '$LOG_DIR/' in watchdog
    start = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    assert 'cat > "$TMP_DIR/kis-auto-trader-monitor-stop.timer"' not in start
    assert "OnCalendar=*-*-* 15:10:00 Asia/Seoul" not in start
    assert "do not exit merely because AUTO_SHUTDOWN_TIME has passed" in start
    assert "Persistent=true" in start
    assert "OnCalendar=Mon..Fri *-*-* 08:00:00 Asia/Seoul" in start
    assert "monitoring ends and restart is disabled" not in loop


def test_stop_monitoring_does_not_kill_trading_process():
    stop = (ROOT / "scripts/stop_monitoring.sh").read_text(encoding="utf-8")
    assert 'systemctl disable --now kis-auto-trader-start.timer kis-auto-trader-watchdog.timer' in stop
    assert 'systemctl stop kis-auto-trader-start.service kis-auto-trader-watchdog.service' in stop
    assert 'runtime_path("monitoring_disabled")' in stop
    assert 'trader_service=untouched' in stop
    assert 'kis-auto-trader-monitor-stop.timer' not in stop
    assert 'kill -TERM "$pid"' in stop  # only the monitor-loop PID
    assert 'scripts/stop.sh' in stop
    assert 'pgrep -f' not in stop


def test_watchdog_does_not_start_after_kst_cutoff(tmp_path):
    import os
    import shutil

    project = tmp_path / "project"
    (project / "monitoring").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "logs").mkdir()
    (project / ".env").write_text("AUTO_SHUTDOWN_TIME=15:10\n", encoding="utf-8")
    shutil.copy2(ROOT / "monitoring/watchdog.sh", project / "monitoring/watchdog.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    fake_date = fakebin / "date"
    fake_date.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *%H:%M*) echo 15:10 ;;\n"
        "  *%Y%m%d*) echo 20261016 ;;\n"
        "  *) echo 2026-10-16T15:10:00+0900 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_date.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_LOG_DIR"] = str(project / "logs")
    env["KIS_DATA_DIR"] = str(project / "json")
    env.pop("MANUAL_STOP_FILE", None); env.pop("START_TRIGGER_FILE", None)
    result = subprocess.run(
        ["bash", str(project / "monitoring/watchdog.sh")],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log = (project / "logs/watchdog_20261016.log").read_text(encoding="utf-8")
    assert "skip=after_cutoff" in log
    assert "process absent; start requested" not in log


def test_monitoring_install_refuses_to_overwrite_an_active_trader_unit():
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "start_monitoring.sh").read_text(encoding="utf-8")
    assert 'systemctl is-active --quiet kis-auto-trader.service' in source
    assert '기존 kis-auto-trader-v5.service가 실행 중입니다' in source
    assert 'systemctl disable --now "$unit"' in source
    assert 'kis-auto-trader-v5-start.timer kis-auto-trader-v5-watchdog.timer kis-auto-trader-v5-monitor-stop.timer' in source
    assert 'systemctl show --property=LoadState --value "$unit"' in source
    assert 'running_main="$(pgrep -af' in source


def _watchdog_project(tmp_path, date_value, time_value, weekday):
    import os
    import shutil
    project = tmp_path / "project"
    (project / "monitoring").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "logs").mkdir()
    (project / "json").mkdir()
    (project / ".env").write_text("AUTO_SHUTDOWN_TIME=15:10\n", encoding="utf-8")
    shutil.copy2(ROOT / "monitoring/watchdog.sh", project / "monitoring/watchdog.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    _install_fake_sudo(fakebin)
    (fakebin / "date").write_text(
        "#!/usr/bin/env bash\ncase \"$*\" in\n"
        f"  *%H:%M*) echo {time_value} ;;\n"
        f"  *%Y%m%d*) echo {date_value} ;;\n"
        f"  *%u*) echo {weekday} ;;\n"
        "  *) echo 2026-10-22T09:00:00+0900 ;;\nesac\n", encoding="utf-8")
    (fakebin / "date").chmod(0o755)
    env = os.environ.copy(); env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_LOG_DIR"] = str(project / "logs")
    env["KIS_DATA_DIR"] = str(project / "json")
    env.pop("MANUAL_STOP_FILE", None); env.pop("START_TRIGGER_FILE", None)
    return project, env



def test_watchdog_blocks_automatic_start_before_0800_kst(tmp_path):
    import os
    import subprocess
    import shutil

    for now in ("00:00", "07:59"):
        case = tmp_path / now.replace(":", "")
        project, env = _watchdog_project(case, "20261022", now, 4)
        result = subprocess.run(
            ["bash", str(project / "monitoring/watchdog.sh"), "--trigger", "watchdog"],
            cwd=project, env=env, text=True, capture_output=True, timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The early-time gate runs before calendar, process, or start-request work.
        assert not list((project / "logs").glob("start_request_*.log"))
        if now == "00:00":
            log = (project / "logs/watchdog_20261022.log").read_text(encoding="utf-8")
            assert "reason=before_start_time" in log
            assert "start_at=08:00" in log


def test_auto_start_time_is_rechecked_under_lock_but_manual_start_is_exempt():
    watchdog = (ROOT / "monitoring/watchdog.sh").read_text(encoding="utf-8")
    request_start = (ROOT / "scripts/request_start.sh").read_text(encoding="utf-8")
    assert 'AUTO_START_AT="08:00"' in watchdog
    assert 'reason=before_start_time_locked' in watchdog
    assert 'policy_start="08:00"' in request_start
    assert 'reason=before_start_time' in request_start
    # request_start applies this policy only to non-manual triggers.
    assert 'if [[ "$TRIGGER" != "manual" ]]; then' in request_start
    assert 'if [[ "$policy_now" < "$policy_start" ]]' in request_start

def test_manual_stop_marker_blocks_same_day_watchdog(tmp_path):
    import json
    project, env = _watchdog_project(tmp_path, "20261022", "09:00", 4)
    (project / "json/manual_stop.json").write_text(json.dumps({"date": "20261022", "trigger": "manual"}), encoding="utf-8")
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (project / "logs/watchdog_20261022.log").read_text(encoding="utf-8")
    assert "skip=manual_stop_marker" in log
    assert "action=start_requested" not in log


def test_expired_manual_stop_marker_does_not_block_next_business_day_checks(tmp_path):
    import json
    import shutil
    project, env = _watchdog_project(tmp_path, "20261009", "09:00", 5)
    (project / "json/manual_stop.json").write_text(json.dumps({"date": "20261008", "trigger": "manual"}), encoding="utf-8")
    shutil.copy2(ROOT / "config/krx_holidays_2026.json", project / "config/krx_holidays_2026.json")
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    # 2026-10-09 is a configured holiday. An expired marker may remain until a
    # permitted start attempt reaches the shared lock; unlocked cleanup is unsafe
    # because it could delete a concurrent same-day manual-stop marker.
    assert (project / "json/manual_stop.json").exists()
    log = (project / "logs/watchdog_20261009.log").read_text(encoding="utf-8")
    assert "manual_stop_marker=expired_pending_lock" in log
    assert "WATCHDOG_OUTCOME status=skipped reason=holiday" in log


def test_manual_stop_writes_marker_even_when_process_is_already_stopped(tmp_path):
    import json
    import shutil
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True); (project / "config").mkdir(); (project / "logs").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/stop.sh", project / "scripts/stop.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"; fakebin.mkdir(); _install_stdlib_python(fakebin)
    (fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fakebin / "pgrep").chmod(0o755)
    import os
    env = os.environ.copy(); env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_LOG_DIR"] = str(project / "logs"); env["KIS_DATA_DIR"] = str(project / "json")
    env.pop("MANUAL_STOP_FILE", None)
    result = subprocess.run(["bash", str(project / "scripts/stop.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    marker = json.loads((project / "json/manual_stop.json").read_text(encoding="utf-8"))
    assert marker["trigger"] == "manual"
    assert marker["reason"] == "operator_requested_stop"
    import stat
    assert stat.S_IMODE((project / "json/start_trigger.lock").stat().st_mode) == 0o660


def test_auto_start_policy_is_rechecked_under_lock_for_systemd_and_proot():
    watchdog = (ROOT / "monitoring/watchdog.sh").read_text(encoding="utf-8")
    request_start = (ROOT / "scripts/request_start.sh").read_text(encoding="utf-8")
    lock_acquired = watchdog.index('log "START_STOP_LOCK action=acquired trigger=$TRIGGER executor=proot"')
    locked_date_check = watchdog.index('LOCKED_TODAY="$(TZ=Asia/Seoul date +%Y%m%d)"')
    assert lock_acquired < locked_date_check
    assert 'reason=weekend_locked' in watchdog
    assert 'reason=holiday_locked' in watchdog
    request_lock = request_start.index('LOCK_ACTIVE=1')
    request_policy = request_start.index('policy_today="$(TZ=Asia/Seoul date +%Y%m%d)"')
    request_systemd_start = request_start.index('"${SYSTEMCTL[@]}" start kis-auto-trader.service')
    assert request_lock < request_policy < request_systemd_start
    assert 'reason=after_cutoff' in request_start
    assert 'reason=weekend' in request_start
    assert 'reason=holiday' in request_start
    assert 'action=defer_cleanup_until_start_lock' in watchdog
    preflight_marker_block = watchdog[watchdog.index('# A manual stop suppresses watchdog recovery'):watchdog.index('# A missing annual KRX calendar')]
    assert 'rm -f -- "$MANUAL_STOP_FILE"' not in preflight_marker_block


def test_systemd_unit_names_are_unversioned_and_trigger_sources_are_explicit():
    units = ROOT / "deploy/systemd"
    assert all("-v5" not in path.name for path in units.iterdir() if path.is_file())
    assert {p.name for p in units.iterdir()} == {
        "kis-auto-trader.service", "kis-auto-trader-start.service", "kis-auto-trader-start.timer",
        "kis-auto-trader-watchdog.service", "kis-auto-trader-watchdog.timer",
    }
    service = (units / "kis-auto-trader.service").read_text(encoding="utf-8")
    scheduler = (units / "kis-auto-trader-start.service").read_text(encoding="utf-8")
    assert "KIS_SYSTEMD_SERVICE=1" in service
    assert "stop.sh --trigger systemd" in service
    assert "watchdog.sh --trigger scheduler" in scheduler
    start = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")
    stop = (ROOT / "scripts/stop.sh").read_text(encoding="utf-8")
    assert "START_OUTCOME status=process_exited" in start
    assert "STOP_OUTCOME status=stopped trigger=$TRIGGER" in stop
    assert "MANUAL_STOP_MARKER action=created" in stop
    start_script = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")
    assert 'exec "$PROJECT_ROOT/scripts/request_start.sh" --trigger manual' in start_script


def test_expired_manual_stop_marker_allows_next_weekday_scheduler_request(tmp_path):
    import json
    import shutil
    # 08:00 is the inclusive earliest automatic-start boundary.
    project, env = _watchdog_project(tmp_path, "20261022", "08:00", 4)
    (project / "json/manual_stop.json").write_text(json.dumps({"date": "20261021", "trigger": "manual"}), encoding="utf-8")
    shutil.copy2(ROOT / "config/krx_holidays_2026.json", project / "config/krx_holidays_2026.json")
    # watchdog expects the helper under scripts/, and fake systemd so no real service is touched.
    (project / "scripts").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "scripts/request_start.sh", project / "scripts/request_start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    fakebin = tmp_path / "bin"
    _install_fake_sudo(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    (fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fakebin / "pgrep").chmod(0o755)
    (fakebin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == is-active ]]; then [[ -f \"$FAKE_SYSTEMD_STARTED\" ]] && exit 0 || exit 1; fi\n"
        "if [[ \"$1\" == start ]]; then touch \"$FAKE_SYSTEMD_STARTED\"; exit 0; fi\n"
        "exit 0\n", encoding="utf-8")
    (fakebin / "systemctl").chmod(0o755)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_SYSTEMD_STARTED"] = str(tmp_path / "started")
    result = subprocess.run(
        ["bash", str(project / "monitoring/watchdog.sh"), "--trigger", "scheduler"],
        cwd=project, env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "started").exists()
    assert not (project / "json/manual_stop.json").exists()
    assert (project / "json/start_trigger").read_text(encoding="utf-8").strip() == "scheduler"
    import stat
    assert stat.S_IMODE((project / "json/start_trigger.lock").stat().st_mode) == 0o660
    log = (project / "logs/watchdog_20261022.log").read_text(encoding="utf-8")
    assert "trigger=scheduler" in log
    assert "manual_stop_marker=expired" in log



def test_systemd_manual_stop_uses_unit_manager_and_leaves_marker(tmp_path):
    import json
    import os
    import shutil
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True); (project / "config").mkdir(); (project / "logs").mkdir(); (project / "json").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/stop.sh", project / "scripts/stop.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"; fakebin.mkdir(); _install_stdlib_python(fakebin)
    _install_fake_sudo(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8"); (fakebin / "ps").chmod(0o755)
    (fakebin / "systemctl").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == is-active ]]; then [[ -f \"$FAKE_STOPPED\" ]] && exit 3 || exit 0; fi\n"
        "if [[ \"$1\" == stop ]]; then touch \"$FAKE_STOPPED\"; exit 0; fi\nexit 0\n", encoding="utf-8")
    (fakebin / "systemctl").chmod(0o755)
    env = os.environ.copy(); env["PATH"] = f"{fakebin}:{env['PATH']}"; env["FAKE_STOPPED"] = str(tmp_path / "stopped")
    env["KIS_LOG_DIR"] = str(project / "logs"); env["KIS_DATA_DIR"] = str(project / "json"); env.pop("MANUAL_STOP_FILE", None)
    result = subprocess.run(["bash", str(project / "scripts/stop.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "stopped").exists()
    assert json.loads((project / "json/manual_stop.json").read_text(encoding="utf-8"))["trigger"] == "manual"



def test_legacy_active_service_check_precedes_disabling_legacy_timers():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    active_check = source.index('systemctl is-active --quiet kis-auto-trader-v5.service')
    timer_disable = source.index('systemctl disable --now "$unit"')
    assert active_check < timer_disable
    assert "기존 서비스와 타이머는 변경하지 않았습니다" in source


def test_monitor_pid_path_is_shared_and_follows_configured_log_dir():
    import os
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "project"
        (root / "config").mkdir(parents=True)
        (root / "config" / "paths.py").write_text((ROOT / "config" / "paths.py").read_text(encoding="utf-8"), encoding="utf-8")
        (root / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
        custom_logs = Path(td) / "custom-logs"
        env = os.environ.copy()
        env["KIS_LOG_DIR"] = str(custom_logs)
        result = subprocess.run(
            [sys.executable, "-c", "import sys; sys.path.insert(0,sys.argv[1]); from config.paths import runtime_path; print(runtime_path('monitor_pid'))", str(root)],
            env=env, text=True, capture_output=True, check=True,
        )
        assert Path(result.stdout.strip()) == custom_logs / "monitoring.pid"
    for rel in ("scripts/start_monitoring.sh", "scripts/stop_monitoring.sh", "scripts/setup.sh", "monitoring/monitor_loop.sh"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert 'runtime_path("monitor_pid")' in source


def test_watchdog_fails_closed_when_new_year_calendar_is_missing(tmp_path):
    import os
    import shutil
    project, env = _watchdog_project(tmp_path, "20270104", "09:00", 1)
    # No 2027 calendar is copied: the year boundary must not reuse 2026 silently.
    env.pop("KIS_PAPER_HOLIDAY_CALENDAR", None)
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 1
    log = (project / "logs/watchdog_20270104.log").read_text(encoding="utf-8")
    assert "reason=holiday_calendar_missing" in log
    assert "fail_closed=1" in log


def test_watchdog_accepts_explicit_year_calendar_override_at_new_year(tmp_path):
    import json
    import os
    import shutil
    project, env = _watchdog_project(tmp_path, "20270104", "09:00", 1)
    calendar = project / "config" / "paper_holidays_2027.json"
    calendar.write_text(json.dumps({"holidays": [{"date": "20270104", "name": "test holiday"}]}), encoding="utf-8")
    env["KIS_PAPER_HOLIDAY_CALENDAR"] = str(calendar)
    env["PATH"] = env["PATH"]
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    # Mark the test date as a holiday so the watchdog proves it parsed the override
    # and exits before any real process start can be requested.
    assert result.returncode == 0, result.stdout + result.stderr
    log = (project / "logs/watchdog_20270104.log").read_text(encoding="utf-8")
    assert "holiday_calendar_missing" not in log
    assert "holiday_calendar_invalid" not in log
    assert "reason=holiday" in log


def test_request_start_does_not_clear_manual_stop_marker_outside_systemd(tmp_path):
    import json
    import os
    import shutil
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    marker = project / "json" / "manual_stop.json"
    marker.write_text(json.dumps({"date": "20261022", "trigger": "manual"}), encoding="utf-8")
    shutil.copy2(ROOT / "scripts/request_start.sh", project / "scripts/request_start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    result = subprocess.run(["bash", str(project / "scripts/request_start.sh"), "--trigger", "manual"], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 4
    assert marker.exists(), "rejected non-systemd start must not erase manual-stop suppression"
    logs = list((project / "logs").glob("control_*.log"))
    assert len(logs) == 1
    assert "executor=non_systemd rejected=1" in logs[0].read_text(encoding="utf-8")


def test_invalid_manual_stop_marker_fails_closed(tmp_path):
    project, env = _watchdog_project(tmp_path, "20261022", "09:00", 4)
    (project / "json/manual_stop.json").write_text("not-json", encoding="utf-8")
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 1
    log = (project / "logs/watchdog_20261022.log").read_text(encoding="utf-8")
    assert "reason=invalid_manual_stop_marker" in log
    assert "fail_closed=1" in log


def test_watchdog_blocks_any_time_after_cutoff_not_only_exact_cutoff(tmp_path):
    import os
    import shutil
    project, env = _watchdog_project(tmp_path, "20261022", "15:30", 4)
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (project / "logs/watchdog_20261022.log").read_text(encoding="utf-8")
    assert "reason=after_cutoff" in log
    assert "action=start_requested" not in log


def test_monitoring_disabled_marker_suppresses_watchdog_across_date_boundary(tmp_path):
    import json
    project, env = _watchdog_project(tmp_path, "20261023", "09:00", 5)
    marker = project / "json" / "monitoring_disabled.json"
    marker.write_text(json.dumps({"disabled": True, "date": "20261022", "reason": "operator_requested_monitoring_stop"}), encoding="utf-8")
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (project / "logs" / "watchdog_20261023.log").read_text(encoding="utf-8")
    assert "reason=monitoring_disabled" in log
    assert "action=start_requested" not in log


def test_invalid_monitoring_disabled_marker_fails_closed(tmp_path):
    project, env = _watchdog_project(tmp_path, "20261023", "09:00", 5)
    (project / "json" / "monitoring_disabled.json").write_text("invalid", encoding="utf-8")
    result = subprocess.run(["bash", str(project / "monitoring/watchdog.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 1
    log = (project / "logs" / "watchdog_20261023.log").read_text(encoding="utf-8")
    assert "reason=invalid_monitoring_disabled_marker" in log
    assert "fail_closed=1" in log


def test_start_monitoring_explicitly_clears_monitoring_disabled_marker():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    assert 'runtime_path("monitoring_disabled")' in source
    assert "clear_monitoring_disabled_marker" in source
    assert source.count("clear_monitoring_disabled_marker") >= 3


def test_stop_monitoring_records_persistent_suppression_without_stopping_trader(tmp_path):
    import json
    import os
    import shutil
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "logs").mkdir()
    (project / "json").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/stop_monitoring.sh", project / "scripts/stop_monitoring.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    result = subprocess.run(["bash", str(project / "scripts/stop_monitoring.sh")], cwd=project, env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    marker = json.loads((project / "json/monitoring_disabled.json").read_text(encoding="utf-8"))
    assert marker["disabled"] is True
    assert marker["reason"] == "operator_requested_monitoring_stop"
    logs = list((project / "logs").glob("control_*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "MONITORING_DISABLED_MARKER action=created" in log
    assert "MONITORING_STOP_OUTCOME status=already_stopped" in log


def test_start_monitoring_resumes_same_active_systemd_service_without_replacing_units():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    active_check = source.index('if $SUDO systemctl is-active --quiet kis-auto-trader.service; then')
    unit_generation = source.index('cat > "$TMP_DIR/kis-auto-trader.service" <<UNIT')
    assert active_check < unit_generation
    assert 'systemctl show --property=WorkingDirectory --value kis-auto-trader.service' in source
    assert 'systemctl show --property=ExecStart --value kis-auto-trader.service' in source
    assert '다른 설치의 Python main.py 프로세스가 있습니다' in source
    assert 'service_already_active=1' in source
    assert '기존 매매 서비스는 유지하고 시작/watchdog timer만 재개했습니다.' in source


def test_proot_monitoring_resume_allows_same_project_trader_but_rejects_other_installations():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    assert 'other_main="$(printf' in source
    assert 'grep -Fv "$PROJECT_ROOT/main.py"' in source
    assert '다른 설치의 Python main.py 프로세스가 있습니다' in source


def test_systemd_restart_policy_delegates_recovery_to_watchdog():
    service = (ROOT / "deploy/systemd/kis-auto-trader.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    assert "Restart=no" in service
    assert "Restart=on-failure" not in service
    assert "Restart=no" in installer
    assert "\nRestart=on-failure\n" not in installer
    assert "Recovery is delegated to watchdog.sh" in service
    assert "Recovery is delegated to watchdog.sh" in installer


def test_reload_config_explicit_start_clears_manual_stop_but_default_does_not():
    source = (ROOT / "scripts/reload_config.sh").read_text(encoding="utf-8")
    assert "clear_manual_stop_marker()" in source
    assert 'if [[ "$MODE" == "start-if-stopped" ]]; then' in source
    assert "MANUAL_STOP_MARKER action=$marker_action trigger=reload_config explicit_start=1" in source
    assert "runtime_path('manual_stop')" in source
    assert "--validate-only" in source
    assert 'MODE="restart-if-running"' in source


def _prepare_reload_config_project(tmp_path):
    import os
    import shutil
    import sys

    project = tmp_path / "reload-project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".venv" / "bin").mkdir(parents=True)
    python_wrapper = project / ".venv" / "bin" / "python"
    python_wrapper.write_text(f"#!/usr/bin/env bash\nexec {sys.executable} \"$@\"\n", encoding="utf-8")
    python_wrapper.chmod(0o755)
    shutil.copy2(ROOT / "scripts/reload_config.sh", project / "scripts/reload_config.sh")
    shutil.copy2(ROOT / "scripts/config_snapshot.py", project / "scripts/config_snapshot.py")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    for rel in ("config/my_entry_strategy.kis.yaml", "config/my_exit_strategy.kis.yaml", "config/strategy_profiles.yaml"):
        destination = project / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, destination)
    (project / ".env").write_text(
        f"KIS_DATA_DIR={project / 'json'}\n"
        f"KIS_LOG_DIR={project / 'custom-logs'}\n"
        "ENTRY_STRATEGY_FILE=config/my_entry_strategy.kis.yaml\n"
        "EXIT_STRATEGY_FILE=config/my_exit_strategy.kis.yaml\n"
        "STRATEGY_PROFILES_FILE=config/strategy_profiles.yaml\n",
        encoding="utf-8",
    )
    return project


def test_shared_control_logger_archives_unwritable_daily_file_for_runtime_user(tmp_path):
    import os
    import shutil
    import subprocess
    import pytest

    if os.geteuid() != 0 or not shutil.which("runuser"):
        pytest.skip("권한 소유자 재현 테스트는 root와 runuser 환경에서만 실행합니다.")
    import tempfile
    import pathlib
    log_dir = pathlib.Path(tempfile.mkdtemp(prefix="kis-log-permission-test-", dir="/tmp"))
    log_dir.chmod(0o777)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    old_log = log_dir / f"control_{today}.log"
    old_log.write_text("previous root-owned log\n", encoding="utf-8")
    old_log.chmod(0o644)
    helper = ROOT / "scripts/lib/log_control.sh"
    command = [
        "runuser", "-u", "nobody", "--", "bash", "-c",
        'source "$1"; kis_log_control "$2" permission_recovery_test',
        "_", str(helper), str(log_dir),
    ]
    result = subprocess.run(command, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    archives = list(log_dir.glob(f"control_{today}.log.unwritable.*"))
    assert len(archives) == 1
    assert "previous root-owned log" in archives[0].read_text(encoding="utf-8")
    assert "permission_recovery_test" in old_log.read_text(encoding="utf-8")
    assert old_log.stat().st_uid != 0
    shutil.rmtree(log_dir)


def test_reload_config_validates_strategy_profiles_yaml():
    source = (ROOT / "scripts/reload_config.sh").read_text(encoding="utf-8")
    assert "STRATEGY_PROFILES_FILE" in source
    assert "parsed.get('strategies')" in source
    assert "parsed.get('decision')" in source


def test_reload_validate_only_rejects_invalid_strategy_profiles_before_restart(tmp_path):
    import os
    import shutil

    project = _prepare_reload_config_project(tmp_path)
    (project / "config/strategy_profiles.yaml").write_text("decision: {}\nstrategies: {}\n", encoding="utf-8")
    fakebin = tmp_path / "bin-invalid-profiles"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env.pop("KIS_LOG_DIR", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/reload_config.sh"), "--validate-only"],
        cwd=project, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode != 0
    assert "STRATEGY_PROFILES_FILE: strategies는 비어 있지 않은 목록이어야 합니다" in result.stderr


def test_reload_validate_only_preserves_manual_stop_marker(tmp_path):
    import json
    import os
    import shutil

    project = _prepare_reload_config_project(tmp_path)
    marker = project / "json/manual_stop.json"
    original = {"date": "20261022", "trigger": "manual", "reason": "operator_requested_stop"}
    marker.write_text(json.dumps(original), encoding="utf-8")
    fakebin = tmp_path / "bin-validate"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env.pop("KIS_LOG_DIR", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/reload_config.sh"), "--validate-only"],
        cwd=project, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(marker.read_text(encoding="utf-8")) == original
    custom_logs = list((project / "custom-logs").glob("control_*.log"))
    assert len(custom_logs) == 1
    assert "RELOAD_OUTCOME status=validated mode=validate-only" in custom_logs[0].read_text(encoding="utf-8")




def test_active_service_policy_migration_is_narrow_and_verified_before_timer_resume():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    assert 'systemctl show --property=ExecStart --value kis-auto-trader.service' in source
    assert 'systemctl show --property=Restart --value kis-auto-trader.service' in source
    assert 'kis-auto-trader.service.d' in source
    assert '10-watchdog-recovery.conf' in source
    assert 'ensure_watchdog_recovery_policy()' in source
    assert '[[ "$effective" == "no" ]]' in source
    assert 'active_process_restarted=0' in source
    policy = source.index('    ensure_watchdog_recovery_policy', source.index('if $SUDO systemctl is-active --quiet kis-auto-trader.service; then'))
    resume_timers = source.index('systemctl enable kis-auto-trader-start.timer kis-auto-trader-watchdog.timer', policy)
    assert policy < resume_timers


def test_reload_explicit_start_clears_manual_stop_and_logs_resume(tmp_path):
    import json
    import os

    project = _prepare_reload_config_project(tmp_path)
    marker = project / "json" / "manual_stop.json"
    marker.write_text(json.dumps({"date": "20261022", "trigger": "manual", "reason": "operator_requested_stop"}), encoding="utf-8")
    fakebin = tmp_path / "bin-start"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    launched = tmp_path / "fake-start-launched"
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    (fakebin / "pgrep").write_text(
        '#!/usr/bin/env bash\n[[ -f "$FAKE_LAUNCHED" ]] && exit 0\nexit 1\n',
        encoding="utf-8",
    )
    (fakebin / "pgrep").chmod(0o755)
    (fakebin / "nohup").write_text(
        '#!/usr/bin/env bash\ntouch "$FAKE_LAUNCHED"\nexit 0\n',
        encoding="utf-8",
    )
    (fakebin / "nohup").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_LAUNCHED"] = str(launched)
    env.pop("KIS_DATA_DIR", None)
    env.pop("KIS_LOG_DIR", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/reload_config.sh"), "--start"],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert launched.exists()
    assert not marker.exists()
    assert not (project / "json" / "start_trigger").exists()
    logs = list((project / "custom-logs").glob("control_*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "MANUAL_STOP_MARKER action=cleared trigger=reload_config explicit_start=1" in log
    assert "RELOAD_ACTION action=start trigger=reload_config service=proot" in log
    assert "RELOAD_OUTCOME status=running mode=start-if-stopped service=proot" in log


def test_active_systemd_service_resumes_timers_without_overwriting_live_units(tmp_path):
    import json
    import os
    import shutil
    import sys

    project = tmp_path / "active-project"
    (project / "scripts").mkdir(parents=True)
    (project / "monitoring").mkdir()
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".venv" / "bin").mkdir(parents=True)
    for rel in (
        "scripts/start_monitoring.sh", "scripts/start.sh", "scripts/stop.sh",
        "scripts/request_start.sh", "scripts/stop_monitoring.sh",
        "monitoring/watchdog.sh", "monitoring/monitor_loop.sh",
    ):
        shutil.copy2(ROOT / rel, project / rel)
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    marker = project / "json" / "monitoring_disabled.json"
    marker.write_text(json.dumps({"disabled": True, "date": "20261022"}), encoding="utf-8")
    (project / ".env").write_text(
        f"KIS_DATA_DIR={project / 'json'}\nKIS_LOG_DIR={project / 'custom-logs'}\nAUTO_SHUTDOWN_TIME=00:00\n",
        encoding="utf-8",
    )
    real_python = sys.executable
    wrapper = project / ".venv" / "bin" / "python"
    wrapper.write_text(
        f'#!/usr/bin/env bash\nif [[ "$1" == "-c" ]]; then exit 0; fi\nexec {real_python} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    _install_fake_sudo(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    (fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fakebin / "pgrep").chmod(0o755)
    calls = tmp_path / "systemctl-calls.log"
    systemctl = f'''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{calls}"
if [[ "$1" == "is-active" ]]; then exit 0; fi
if [[ "$1" == "show" ]]; then
  case "$2:$4" in
    --property=Restart:kis-auto-trader.service) printf '%s\n' "no" ;;
    --property=WorkingDirectory:kis-auto-trader.service) printf '%s\\n' "{project}" ;;
    --property=ExecStart:kis-auto-trader.service) printf '%s\\n' "{project}/scripts/start.sh" ;;
    --property=ExecStart:kis-auto-trader-start.service) printf '%s\\n' "{project}/monitoring/watchdog.sh --trigger scheduler" ;;
    --property=ExecStart:kis-auto-trader-watchdog.service) printf '%s\\n' "{project}/monitoring/watchdog.sh" ;;
    *) exit 1 ;;
  esac
  exit 0
fi
exit 0
'''
    (fakebin / "systemctl").write_text(systemctl, encoding="utf-8")
    (fakebin / "systemctl").chmod(0o755)
    # Never let a test fallback write a real systemd drop-in if the policy check regresses.
    (fakebin / "install").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fakebin / "install").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env.pop("KIS_DATA_DIR", None)
    env.pop("KIS_LOG_DIR", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/start_monitoring.sh")],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    calls_text = calls.read_text(encoding="utf-8")
    assert "enable kis-auto-trader-start.timer kis-auto-trader-watchdog.timer" in calls_text
    assert "start kis-auto-trader-start.timer kis-auto-trader-watchdog.timer" in calls_text
    assert "daemon-reload" not in calls_text
    assert "disable --now kis-auto-trader-v5" not in calls_text
    logs = list((project / "custom-logs").glob("control_*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "MONITORING_DISABLED_MARKER action=cleared" in log
    assert "MONITORING_OUTCOME status=resumed trigger=installer executor=systemd" in log


def test_systemd_automatic_start_rechecks_same_day_manual_stop_under_shared_lock(tmp_path):
    import json
    import os
    import shutil
    from datetime import datetime
    from zoneinfo import ZoneInfo

    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/request_start.sh", project / "scripts/request_start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    (project / "json/manual_stop.json").write_text(json.dumps({"date": today, "trigger": "manual"}), encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    calls = tmp_path / "systemctl-calls"
    (fakebin / "systemctl").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_SYSTEMCTL_CALLS"\nexit 1\n',
        encoding="utf-8",
    )
    (fakebin / "systemctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_SYSTEMCTL_CALLS"] = str(calls)
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    result = subprocess.run(
        ["bash", str(project / "scripts/request_start.sh"), "--trigger", "watchdog"],
        cwd=project, env=env, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 10, result.stdout + result.stderr
    assert not calls.exists(), "suppressed automatic start must not call systemctl"
    assert (project / "json/manual_stop.json").exists()
    control = next((project / "logs").glob("control_*.log")).read_text(encoding="utf-8")
    assert "outcome=suppressed reason=manual_stop_marker" in control


def test_start_stop_race_uses_the_same_lock_and_proot_rechecks_marker():
    stop = (ROOT / "scripts/stop.sh").read_text(encoding="utf-8")
    start = (ROOT / "scripts/request_start.sh").read_text(encoding="utf-8")
    watchdog = (ROOT / "monitoring/watchdog.sh").read_text(encoding="utf-8")
    stop_monitoring = (ROOT / "scripts/stop_monitoring.sh").read_text(encoding="utf-8")
    start_monitoring = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    reload_config = (ROOT / "scripts/reload_config.sh").read_text(encoding="utf-8")
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in stop and 'exec 8>"$LOCK_FILE"' in stop
    assert 'flock -u 8' not in stop, "manual stop must hold the shared lock until the trader stop completes"
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in start and 'exec 9>"$LOCK_FILE"' in start
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in watchdog and 'exec 9>"$LOCK_FILE"' in watchdog
    assert 'KIS_START_LOCK_HELD=1 nohup "$PROJECT_ROOT/scripts/start.sh" --trigger "$TRIGGER"' in watchdog
    assert 'KIS_START_LOCK_HELD=1 nohup "$PROJECT_ROOT/scripts/start.sh" --trigger reload_config' in (ROOT / 'scripts/reload_config.sh').read_text(encoding='utf-8')
    assert 'chmod 0660 "$LOCK_FILE"' in stop and 'chmod 0660 "$LOCK_FILE"' in start and 'chmod 0660 "$LOCK_FILE"' in watchdog
    assert 'reason=manual_stop_marker_race' in watchdog
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in stop_monitoring and 'exec 8>"$LOCK_FILE"' in stop_monitoring
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in start_monitoring and 'flock -x 7' in start_monitoring
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in reload_config and 'flock -x 9' in reload_config
    assert 'rc" -eq 11' in watchdog and 'status=deferred reason=start_stop_lock_busy' in watchdog
    assert watchdog.index('reason=manual_stop_marker_race date=$TODAY executor=proot') < watchdog.index('nohup "$PROJECT_ROOT/scripts/start.sh"')
    start_script = (ROOT / "scripts/start.sh").read_text(encoding="utf-8")
    assert 'KIS_START_LOCK_HELD:-0' in start_script
    assert '"$PYTHON_BIN" -u "$PROJECT_ROOT/main.py" 9>&- &' in start_script
    assert 'flock -u 9' in start_script


def test_request_start_defers_when_manual_stop_holds_shared_lock(tmp_path):
    import fcntl
    import os
    import shutil

    project = tmp_path / "locked-project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/request_start.sh", project / "scripts/request_start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    calls = tmp_path / "systemctl-calls"
    (fakebin / "systemctl").write_text('#!/usr/bin/env bash\nprintf "%s\n" "$*" >> "$FAKE_CALLS"\nexit 1\n', encoding="utf-8")
    (fakebin / "systemctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    env["FAKE_CALLS"] = str(calls)
    lock_path = project / "json" / "start_trigger.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        result = subprocess.run(
            ["bash", str(project / "scripts/request_start.sh"), "--trigger", "watchdog"],
            cwd=project, env=env, text=True, capture_output=True, timeout=15,
        )
        assert result.returncode == 11, result.stdout + result.stderr
    assert not calls.exists(), "a competing manual stop must prevent the start command"
    control = next((project / "logs").glob("control_*.log")).read_text(encoding="utf-8")
    assert "another_start_stop_operation_in_progress" in control


def test_reload_default_mode_respects_same_day_manual_stop_marker(tmp_path):
    import json
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    project = _prepare_reload_config_project(tmp_path)
    marker = project / "json" / "manual_stop.json"
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    original = {"date": today, "trigger": "manual", "reason": "operator_requested_stop"}
    marker.write_text(json.dumps(original), encoding="utf-8")
    fakebin = tmp_path / "bin-block"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env.pop("KIS_DATA_DIR", None)
    env.pop("KIS_LOG_DIR", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/reload_config.sh")],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(marker.read_text(encoding="utf-8")) == original
    assert not (project / "json" / "start_trigger").exists()
    logs = list((project / "custom-logs").glob("control_*.log"))
    assert len(logs) == 1
    log = logs[0].read_text(encoding="utf-8")
    assert "RELOAD_OUTCOME status=blocked_manual_stop mode=restart-if-running" in log
    assert "RELOAD_ACTION action=restart" not in log


def test_reload_config_uses_shared_start_stop_lock_and_holds_it_through_start():
    source = (ROOT / "scripts/reload_config.sh").read_text(encoding="utf-8")
    assert 'LOCK_FILE="${START_TRIGGER_FILE}.lock"' in source
    assert 'exec 9>"$LOCK_FILE"' in source and 'flock -x 9' in source
    lock_call = source.index('acquire_start_stop_lock\n# Default/restart mode')
    marker_clear = source.index('marker_action="$(clear_manual_stop_marker)"')
    start_action = source.index('log_control "RELOAD_ACTION action=start trigger=reload_config service=proot"')
    assert lock_call < marker_clear < start_action
    assert 'KIS_START_LOCK_HELD=1 nohup "$PROJECT_ROOT/scripts/start.sh" --trigger reload_config' in source


def test_request_start_rechecks_monitoring_disabled_marker_under_shared_lock(tmp_path):
    import json
    import os
    import shutil

    project = tmp_path / "monitoring-disabled-project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/request_start.sh", project / "scripts/request_start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    (project / "json/monitoring_disabled.json").write_text(json.dumps({"disabled": True, "reason": "operator_requested_monitoring_stop"}), encoding="utf-8")
    fakebin = tmp_path / "bin-disabled"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    calls = tmp_path / "systemctl-calls-disabled"
    (fakebin / "systemctl").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_CALLS"\nexit 1\n', encoding="utf-8")
    (fakebin / "systemctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    env["FAKE_CALLS"] = str(calls)
    result = subprocess.run(
        ["bash", str(project / "scripts/request_start.sh"), "--trigger", "watchdog"],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 12, result.stdout + result.stderr
    assert not calls.exists(), "monitoring-disabled marker must suppress the start before systemctl"
    control = next((project / "logs").glob("control_*.log")).read_text(encoding="utf-8")
    assert "outcome=suppressed reason=monitoring_disabled" in control


def test_systemd_migration_checks_all_main_processes_before_disabling_legacy_timers():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    preflight = source.index("running_main=\"$(pgrep -af '[p]ython.*main\\.py'")
    timer_disable = source.index('systemctl disable --now "$unit"')
    assert preflight < timer_disable
    assert "기존 unit/timer를 변경하지 않았습니다" in source


def test_proot_start_releases_shared_lock_after_python_process_is_visible(tmp_path):
    import os
    import shutil
    import subprocess
    import time

    project = tmp_path / "proot-start-stop-project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    shutil.copy2(ROOT / "scripts/start.sh", project / "scripts/start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "scripts/stop.sh", project / "scripts/stop.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    (project / ".env").write_text(
        f"KIS_DATA_DIR={project / 'json'}\nKIS_LOG_DIR={project / 'logs'}\n"
        "KIS_PAPER=true\nKIS_ENABLE_TRADING=false\n",
        encoding="utf-8",
    )
    (project / "main.py").write_text(
        "from pathlib import Path\nimport time\n"
        f"Path({str(project / 'json/main_started')!r}).write_text('started')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    real_ps = shutil.which("ps")
    (fakebin / "ps").write_text(
        f'#!/usr/bin/env bash\nif [[ "$*" == "-p 1 -o comm=" ]]; then echo proot; else exec {real_ps} "$@"; fi\n',
        encoding="utf-8",
    )
    (fakebin / "ps").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    starter = subprocess.Popen(
        ["bash", str(project / "scripts/start.sh"), "--trigger", "watchdog"],
        cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        started_file = project / "json/main_started"
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not started_file.exists():
            time.sleep(0.05)
        assert started_file.exists(), "Proot start wrapper did not launch the test process"
        # This must acquire the shared lock while the Python process is still alive.
        stopped = subprocess.run(
            ["bash", str(project / "scripts/stop.sh")],
            cwd=project, env=env, text=True, capture_output=True, timeout=10,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert (project / "json/manual_stop.json").exists()
        assert "STOP_OUTCOME status=stopped" in (next((project / "logs").glob("control_*.log")).read_text(encoding="utf-8"))
        assert starter.wait(timeout=10) != 0
    finally:
        if starter.poll() is None:
            starter.terminate()
            try:
                starter.wait(timeout=3)
            except subprocess.TimeoutExpired:
                starter.kill()
                starter.wait(timeout=3)


def test_reload_proot_start_is_cancelled_if_manual_stop_wins_during_launch(tmp_path):
    import json
    import os

    project = _prepare_reload_config_project(tmp_path)
    marker = project / "json" / "manual_stop.json"
    fakebin = tmp_path / "bin-race"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    launched = tmp_path / "fake-start-race-launched"
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho proot\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    (fakebin / "pgrep").write_text('#!/usr/bin/env bash\n[[ -f "$FAKE_LAUNCHED" ]] && exit 0\nexit 1\n', encoding="utf-8")
    (fakebin / "pgrep").chmod(0o755)
    (fakebin / "nohup").write_text(
        '#!/usr/bin/env bash\nprintf \'{"date":"%s","trigger":"manual"}\\n\' "$FAKE_TODAY" > "$FAKE_MANUAL_STOP_FILE"\ntouch "$FAKE_LAUNCHED"\nexit 0\n',
        encoding="utf-8",
    )
    (fakebin / "nohup").chmod(0o755)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["FAKE_LAUNCHED"] = str(launched)
    env["FAKE_MANUAL_STOP_FILE"] = str(marker)
    env["FAKE_TODAY"] = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    env.pop("KIS_DATA_DIR", None)
    env.pop("KIS_LOG_DIR", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/reload_config.sh"), "--start"],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert launched.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["date"] == env["FAKE_TODAY"]
    control = next((project / "custom-logs").glob("control_*.log")).read_text(encoding="utf-8")
    assert "RELOAD_OUTCOME status=cancelled mode=start-if-stopped reason=concurrent_manual_stop" in control
    assert "RELOAD_OUTCOME status=running mode=start-if-stopped" not in control


def test_systemd_monitoring_transition_marker_is_written_before_timer_start_and_cleared_only_after_active_check():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    active_branch = source.index('if $SUDO systemctl is-active --quiet kis-auto-trader.service; then')
    transition = source.index('write_monitoring_transition_marker\n    $SUDO systemctl enable kis-auto-trader-start.timer', active_branch)
    enable = source.index('$SUDO systemctl enable kis-auto-trader-start.timer', active_branch)
    active_check = source.index('$SUDO systemctl is-active --quiet kis-auto-trader-start.timer', enable)
    clear = source.index('clear_monitoring_disabled_marker', active_check)
    assert transition < enable < active_check < clear
    assert 'reason":"monitoring_start_transition"' in source


def test_systemd_request_start_rechecks_cutoff_before_starting_service(tmp_path):
    import os
    import shutil

    project = tmp_path / "cutoff-project"
    (project / "scripts").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "json").mkdir()
    (project / "logs").mkdir()
    (project / ".env").write_text("AUTO_SHUTDOWN_TIME=09:00\n", encoding="utf-8")
    shutil.copy2(ROOT / "scripts/request_start.sh", project / "scripts/request_start.sh")
    (project / "scripts/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts/lib/log_control.sh", project / "scripts/lib/log_control.sh")
    shutil.copy2(ROOT / "config/paths.py", project / "config/paths.py")
    (project / "config" / "__init__.py").write_text("\"\"\"Isolated config package for shell integration tests.\"\"\"\n", encoding="utf-8")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    (fakebin / "ps").write_text("#!/usr/bin/env bash\necho systemd\n", encoding="utf-8")
    (fakebin / "ps").chmod(0o755)
    (fakebin / "date").write_text(
        "#!/usr/bin/env bash\ncase \"$*\" in\n"
        "  *%H:%M*) echo 10:00 ;;\n"
        "  *%Y%m%d*) echo 20261022 ;;\n"
        "  *%u*) echo 4 ;;\n"
        "  *) echo 2026-10-22T10:00:00+0900 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    (fakebin / "date").chmod(0o755)
    calls = tmp_path / "systemctl-calls.log"
    (fakebin / "systemctl").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FAKE_CALLS"\nexit 0\n',
        encoding="utf-8",
    )
    (fakebin / "systemctl").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["KIS_DATA_DIR"] = str(project / "json")
    env["KIS_LOG_DIR"] = str(project / "logs")
    env["FAKE_CALLS"] = str(calls)
    env.pop("MANUAL_STOP_FILE", None)
    env.pop("START_TRIGGER_FILE", None)
    result = subprocess.run(
        ["bash", str(project / "scripts/request_start.sh"), "--trigger", "scheduler"],
        cwd=project, env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 13, result.stdout + result.stderr
    assert not calls.exists(), "a cutoff-crossing automatic start must not call systemctl"
    control = next((project / "logs").glob("control_*.log")).read_text(encoding="utf-8")
    assert "outcome=suppressed reason=after_cutoff" in control


def test_proot_monitoring_loop_is_started_even_if_initial_watchdog_check_fails():
    source = (ROOT / "scripts/start_monitoring.sh").read_text(encoding="utf-8")
    assert 'MONITORING_OUTCOME status=initial_check_failed trigger=installer executor=proot' in source
    assert 'retry_loop=will_start' in source
    assert 'MONITORING_OUTCOME status=started_with_initial_check_failure' in source
    initial_check = source.index('MONITORING_OUTCOME status=initial_check_failed trigger=installer executor=proot')
    loop_launch = source.index('nohup "$PROJECT_ROOT/monitoring/monitor_loop.sh"')
    assert initial_check < loop_launch
    assert 'fail "Proot watchdog 초기 확인이 실패했습니다' not in source


def test_proot_watchdog_rechecks_cutoff_after_waiting_for_start_stop_lock(tmp_path):
    import os
    import shutil

    project, env = _watchdog_project(tmp_path, "20261022", "09:00", 4)
    shutil.copy2(ROOT / "config/krx_holidays_2026.json", project / "config/krx_holidays_2026.json")
    fakebin = tmp_path / "bin-boundary"
    fakebin.mkdir()
    _install_stdlib_python(fakebin)
    clock = tmp_path / "clock-count"
    (fakebin / "date").write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *%H:%M* ]]; then\n"
        f"  n=0; [[ -f '{clock}' ]] && n=$(cat '{clock}'); n=$((n+1)); echo $n > '{clock}'\n"
        "  if [[ $n -eq 1 ]]; then echo 09:00; else echo 15:10; fi\n"
        "elif [[ \"$*\" == *%Y%m%d* ]]; then echo 20261022\n"
        "elif [[ \"$*\" == *%u* ]]; then echo 4\n"
        "else echo 2026-10-22T15:10:00+0900\nfi\n",
        encoding="utf-8",
    )
    (fakebin / "date").chmod(0o755)
    real_ps = shutil.which("ps")
    (fakebin / "ps").write_text(
        f'#!/usr/bin/env bash\nif [[ "$*" == "-p 1 -o comm=" ]]; then echo proot; else exec {real_ps} "$@"; fi\n',
        encoding="utf-8",
    )
    (fakebin / "ps").chmod(0o755)
    (fakebin / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fakebin / "pgrep").chmod(0o755)
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(project / "monitoring/watchdog.sh"), "--trigger", "watchdog"],
        cwd=project, env=env, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    log = (project / "logs/watchdog_20261022.log").read_text(encoding="utf-8")
    assert "reason=after_cutoff_locked" in log
    assert "action=start_requested executor=proot" not in log
