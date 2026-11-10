import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _summary(cash=900000, assets=1000000):
    return {"dnca_tot_amt": str(cash), "ord_psbl_cash": str(cash), "nass_amt": str(assets)}


@pytest.mark.parametrize(
    "max_positions,held_count,expected_budget",
    [(3, 0, 300000), (3, 1, 450000), (3, 2, 900000),
     (5, 0, 180000), (5, 2, 300000), (5, 4, 900000)],
)
def test_position_budget_is_available_cash_divided_by_remaining_slots(
    max_positions, held_count, expected_budget
):
    from risk.position_sizer import calculate_buy_qty
    qty, budget, reason = calculate_buy_qty(
        _summary(), held_count, price=10000, max_positions=max_positions,
        min_order_amount=10000, fee_rate_pct=0, price_buffer_pct=0,
    )
    assert budget == pytest.approx(expected_budget)
    assert qty == expected_budget // 10000
    assert f"remaining_slots={max_positions-held_count}" in reason


def test_position_sizing_blocks_when_no_slots_remain():
    from risk.position_sizer import calculate_buy_qty
    qty, budget, reason = calculate_buy_qty(_summary(), 3, 10000, max_positions=3)
    assert (qty, budget) == (0, 0.0)
    assert "한도 도달" in reason


def test_existing_180_second_order_timeouts_are_clamped_to_300(monkeypatch):
    from config.settings import load_settings
    monkeypatch.setenv("BUY_ORDER_TIMEOUT_SEC", "180")
    monkeypatch.setenv("SELL_ORDER_TIMEOUT_SEC", "180")
    settings = load_settings()
    assert settings.buy_order_timeout_sec == 300
    assert settings.sell_order_timeout_sec == 300


def test_shutdown_cancel_confirmation_timeout_has_180_second_floor():
    source = (ROOT / "application.py").read_text(encoding="utf-8")
    assert 'confirm_timeout_sec=max(180, int(_env_float("SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC", 180)))' in source


def test_env_example_and_configuration_reference_use_requested_timeouts():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    docs = (ROOT / "docs/configuration/CONFIGURATION_REFERENCE.md").read_text(encoding="utf-8")
    assert "SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC=180" in env
    assert "BUY_ORDER_TIMEOUT_SEC=300" in env
    assert "SELL_ORDER_TIMEOUT_SEC=300" in env
    assert "`SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC` | `180`" in docs
    assert "`BUY_ORDER_TIMEOUT_SEC` | `300`" in docs
    assert "`SELL_ORDER_TIMEOUT_SEC` | `300`" in docs


def test_watchdog_and_start_request_logs_use_shared_log_permission_helper():
    watchdog = (ROOT / "monitoring/watchdog.sh").read_text(encoding="utf-8")
    helper = (ROOT / "scripts/lib/log_control.sh").read_text(encoding="utf-8")
    assert 'kis_log_append "$LOG_DIR" "watchdog_${d}.log"' in watchdog
    assert 'kis_prepare_shared_log "$LOG_DIR" "start_request_${TODAY}.log"' in watchdog
    assert 'chown --reference="$log_dir" "$log_file"' in helper
    assert 'umask 0002' in helper


@pytest.mark.skipif(os.geteuid() != 0, reason="root ownership normalization test requires root")
def test_shared_log_is_owned_like_log_directory_and_group_writable(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # A root-created log directory owned by an alternate account models the
    # Ubuntu service-user directory with a root-run systemd watchdog.
    import pwd
    try:
        account = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("nobody account unavailable")
    os.chown(log_dir, account.pw_uid, account.pw_gid)
    subprocess.run(
        ["bash", "-lc", f'source "{ROOT}/scripts/lib/log_control.sh"; kis_prepare_shared_log "{log_dir}" "watchdog_20261030.log"'],
        check=True, capture_output=True, text=True,
    )
    target = log_dir / "watchdog_20261030.log"
    st = target.stat()
    assert st.st_uid == account.pw_uid
    assert st.st_gid == account.pw_gid
    assert st.st_mode & 0o020
