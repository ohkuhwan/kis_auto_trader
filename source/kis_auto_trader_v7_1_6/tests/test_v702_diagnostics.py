"""V7.0.2의 체결/잔고/API/후보 진단 로그 회귀 검사."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_fill_log_does_not_mislabel_latest_price_times_cumulative_qty():
    source = (ROOT / "trading/fill_handler.py").read_text(encoding="utf-8")
    assert "FILL_AMOUNT_AUDIT" in source
    assert "실제 누적체결금액 아님" in source
    assert "누적체결금액(참고)=" not in source


def test_balance_and_order_history_have_amount_audit_fields():
    client = (ROOT / "brokers/kis/client.py").read_text(encoding="utf-8")
    application = (ROOT / "application.py").read_text(encoding="utf-8")
    reconcile = (ROOT / "trading/reconcile.py").read_text(encoding="utf-8")
    assert "KIS_BALANCE_AUDIT" in client
    assert "KIS_ORDER_HISTORY_AUDIT" in client
    assert "PORTFOLIO_AMOUNT_AUDIT" in application
    assert "RECONCILE_BALANCE_AUDIT" in reconcile
    assert "RECONCILE_POSITION_AUDIT" in reconcile


def test_rate_limit_diagnostics_cover_rest_and_auth_endpoints():
    source = (ROOT / "brokers/kis/client.py").read_text(encoding="utf-8")
    assert source.count("KIS_RATE_LIMIT_WAIT") >= 3
    assert source.count("KIS_RATE_LIMIT_COOLDOWN") >= 3
    # These endpoints use the same self.limiter instance as the REST client.
    assert "self.limiter.acquire()" in source
    assert "self.limiter.defer(cooldown" in source


def test_candidate_and_strategy_decision_logs_explain_provenance_and_gates():
    application = (ROOT / "application.py").read_text(encoding="utf-8")
    adapter = (ROOT / "strategies/builder_engine/official_adapter.py").read_text(encoding="utf-8")
    assert "[HTS_CONDITION]" in application
    assert "[CANDIDATE_SOURCE]" in application
    assert "HTS_SCORED_CANDIDATE" in application
    assert "[CANDIDATE_FILTER]" in application
    assert "[STRATEGY_PROFILE_CONFIG_ITEM]" in adapter
    assert "[STRATEGY_DECISION_AUDIT]" in adapter


def test_fill_notice_logs_distinguish_duplicate_suppression_from_new_fill():
    source = (ROOT / "trading/orders.py").read_text(encoding="utf-8")
    assert "FILL_NOTICE_RECEIVED" in source
    assert "FILL_NOTICE_DUPLICATE_SUPPRESSED" in source
    assert "KIS_ORDER_HISTORY_AUDIT" in (ROOT / "brokers/kis/client.py").read_text(encoding="utf-8")


def test_release_version_and_diagnostic_changelog_are_present():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "7.1.6"
    assert (ROOT / "docs/release/CHANGELOG_V7.0.2.md").is_file()
    assert (ROOT / "docs/release/CHANGELOG_V7.1.1.md").is_file()
    assert (ROOT / "docs/release/CHANGELOG_V7.1.2.md").is_file()
    assert (ROOT / "docs/release/CHANGELOG_V7.1.3.md").is_file()
    assert (ROOT / "docs/release/CHANGELOG_V7.1.4.md").is_file()
    assert (ROOT / "docs/release/CHANGELOG_V7.1.6.md").is_file()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "V7.0.2 진단 로그" in readme


def test_entry_selection_logs_show_hts_input_and_final_candidate_source():
    source = (ROOT / "trading/buy_process.py").read_text(encoding="utf-8")
    assert "[ENTRY_SELECTION_START]" in source
    assert "[ENTRY_SELECTION_HTS]" in source
    assert "[ENTRY_SELECTION_BUILDER]" in source
    assert "[ENTRY_SELECTION_RESULT]" in source
