"""Builder 수동 후보 출처와 cwd 독립성을 운영 설정 파일과 분리해 검증합니다.

테스트 종목은 삼성전자(005930), 현대차(005380)로 고정합니다.
실제 config/entry_symbols.txt는 읽거나 변경하지 않습니다.
"""
from __future__ import annotations
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 테스트 전용 고정 종목: 삼성전자, 현대차
TEST_SYMBOLS = ["005930", "005380"]


def test_builder_symbol_list_ignores_legacy_environment_variable(monkeypatch, tmp_path):
    from config.builder_symbols import load_builder_symbols
    symbol_file = tmp_path / "builder_symbols_fixture.txt"
    symbol_file.write_text(
        "# fixed test symbols\n005930\n005380\n005930\ninvalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BUILDER_SYMBOLS_FILE", str(symbol_file))
    monkeypatch.setenv("BUILDER_ENTRY_SYMBOLS", "035420,051910")
    assert load_builder_symbols() == TEST_SYMBOLS


def test_builder_symbol_file_relative_path_is_project_root_based_when_cwd_changes(monkeypatch, tmp_path):
    import config.builder_symbols as builder_symbols
    from config.paths import PROJECT_ROOT, runtime_path

    monkeypatch.delenv("BUILDER_SYMBOLS_FILE", raising=False)
    monkeypatch.delenv("BUILDER_ENTRY_SYMBOLS", raising=False)
    monkeypatch.chdir(tmp_path)

    # 경로 계산만 운영 기본 경로로 확인하고, 실제 운영 파일은 열지 않습니다.
    assert runtime_path("builder_symbols") == (PROJECT_ROOT / "config/entry_symbols.txt").resolve()

    # 로더 동작은 별도 테스트 fixture로 검증해 운영 entry_symbols.txt 내용에 의존하지 않습니다.
    fixture = tmp_path / "builder_symbols_fixture.txt"
    fixture.write_text("005930\n005380\n", encoding="utf-8")
    monkeypatch.setattr(builder_symbols, "runtime_path", lambda name: fixture)
    assert builder_symbols.load_builder_symbols() == TEST_SYMBOLS


def test_review_symbol_source_does_not_read_legacy_environment_variable(monkeypatch):
    import importlib.util
    script = ROOT / "scripts" / "run_strategy_review.py"
    spec = importlib.util.spec_from_file_location("run_strategy_review_under_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    # 리뷰 스크립트의 파일 I/O를 격리하고, 고정 테스트 종목만 공급합니다.
    monkeypatch.setattr(module, "load_builder_symbols", lambda: list(TEST_SYMBOLS))
    monkeypatch.setenv("BUILDER_SYMBOLS_FILE", "/path/that/must/not/be/read.txt")
    monkeypatch.setenv("BUILDER_ENTRY_SYMBOLS", "000660")
    args = type("Args", (), {"symbols": None})()
    assert module.symbols_from_sources(args) == TEST_SYMBOLS


def test_hts_and_manual_builder_sources_are_union_deduplicated_separately():
    from config.builder_symbols import combine_candidate_symbols
    assert combine_candidate_symbols(["005930", "005380"], ["005380", "005930"]) == TEST_SYMBOLS


def test_relative_builder_symbols_file_override_is_project_root_relative(monkeypatch, tmp_path):
    from config.paths import PROJECT_ROOT, runtime_path
    monkeypatch.setenv("BUILDER_SYMBOLS_FILE", "custom/builder_symbols_fixture.txt")
    monkeypatch.chdir(tmp_path)
    assert runtime_path("builder_symbols") == (PROJECT_ROOT / "custom/builder_symbols_fixture.txt").resolve()
