import json
from pathlib import Path

import pytest

from scripts import upgrade_migrate
from scripts.upgrade_migrate import migrate


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_migration_copies_runtime_files_creates_byte_exact_backup_and_verifies(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    env = _write(source, ".env", b"KIS_PAPER=true\n")
    state = _write(source, "state.json", b'{"positions": {}}')
    cancel = _write(source, "cancel_orders.json", b'{"updated_at": 1, "records": {"7788": {"status": "cancelled"}}}')
    signal_log = _write(source, "logs/strategy_signals.jsonl", b'{"event":"BUY"}\n')

    copied, skipped = migrate(source, destination)

    assert any("복사 완료:" in line and line.endswith("json/state.json") for line in copied)
    assert any("복사 완료:" in line and line.endswith("json/cancel_orders.json") for line in copied)
    assert any("복사 완료:" in line and line.endswith("logs/strategy_signals.jsonl") for line in copied)
    assert any("백업 완료:" in line for line in copied)
    assert any("이전 후 검증 완료:" in line for line in copied)
    assert skipped == []
    assert (destination / ".env").read_bytes() == env.read_bytes()
    assert (destination / "json" / "state.json").read_bytes() == state.read_bytes()
    assert (destination / "json" / "cancel_orders.json").read_bytes() == cancel.read_bytes()
    assert (destination / "logs" / "strategy_signals.jsonl").read_bytes() == signal_log.read_bytes()
    assert (destination / ".env").stat().st_mode & 0o777 == 0o600
    assert env.exists() and state.exists() and cancel.exists() and signal_log.exists()
    backups = list((destination / ".migration_backups").glob("*/manifest.json"))
    assert len(backups) == 1
    manifest = json.loads(backups[0].read_text(encoding="utf-8"))
    assert {item["source"] for item in manifest["files"]} >= {str(env), str(state), str(cancel), str(signal_log)}
    state_backup = next(backups[0].parent / item["backup"] for item in manifest["files"] if item["source"] == str(state))
    assert state_backup.read_bytes() == state.read_bytes()


def test_migration_preflight_conflict_copies_nothing_and_creates_no_backup(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "state.json", b'{"ok": true}')
    _write(source, "cancel_orders.json", b'{"updated_at": 1, "records": {}}')
    _write(destination, "json/cancel_orders.json", b'{"updated_at": 1, "records": {"different": {}}}')

    with pytest.raises(FileExistsError, match="아무 파일도 복사하지 않음"):
        migrate(source, destination)

    assert not (destination / "json" / "state.json").exists()
    assert (source / "state.json").read_bytes() == b'{"ok": true}'
    assert not (destination / ".migration_backups").exists()


def test_migration_skips_identical_destination_and_dry_run_does_not_write(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "state.json", b'{"state": "same"}')
    _write(destination, "json/state.json", b'{"state": "same"}')
    _write(source, "uncertain_orders.json", b'{"orders": []}')

    copied, skipped = migrate(source, destination, dry_run=True)

    assert len(copied) == 1 and "복사 예정:" in copied[0] and "json/uncertain_orders.json" in copied[0]
    assert any("동일 파일 유지:" in line and line.endswith("json/state.json") for line in skipped)
    assert not (destination / "json" / "uncertain_orders.json").exists()
    assert not (destination / ".migration_backups").exists()


def test_migration_rejects_same_or_nested_source_destination(tmp_path):
    with pytest.raises(ValueError, match="같습니다"):
        migrate(tmp_path, tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="서로 포함되지 않는"):
        migrate(tmp_path, child)


def test_identical_existing_env_permissions_are_hardened_without_overwrite(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, ".env", b"KIS_PAPER=true\n")
    existing = _write(destination, ".env", b"KIS_PAPER=true\n")
    existing.chmod(0o644)

    copied, skipped = migrate(source, destination)

    assert not any("복사 완료:" in line for line in copied)
    assert any("백업 완료:" in line for line in copied)
    assert any("동일 파일 유지:" in line and line.endswith("/.env") for line in skipped)
    assert existing.stat().st_mode & 0o777 == 0o600
    assert existing.read_bytes() == b"KIS_PAPER=true\n"


def test_migration_rewrites_only_legacy_default_path_overrides(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    env = _write(source, ".env", b"STATE_FILE=state.json\nCANCEL_ORDERS_FILE=cancel_orders.json\nKIS_LOG_DIR=/custom/logs\n")
    _write(source, "state.json", b'{"positions": {}}')
    migrate(source, destination)
    migrated = (destination / ".env").read_text(encoding="utf-8")
    assert "STATE_FILE=\n" in migrated
    assert "CANCEL_ORDERS_FILE=\n" in migrated
    assert "KIS_LOG_DIR=/custom/logs\n" in migrated
    assert env.read_bytes() == b"STATE_FILE=state.json\nCANCEL_ORDERS_FILE=cancel_orders.json\nKIS_LOG_DIR=/custom/logs\n"


def test_corrupted_json_aborts_all_writes_and_preserves_source(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "state.json", b'{"positions": ')  # malformed critical state
    _write(source, "cancel_orders.json", b'{"order_no":"1"}')

    with pytest.raises(ValueError, match="손상된 JSON"):
        migrate(source, destination)

    assert not (destination / ".env").exists()
    assert not (destination / "json" / "cancel_orders.json").exists()
    assert not (destination / ".migration_backups").exists()
    assert (source / "state.json").read_bytes() == b'{"positions": '


def test_corrupted_jsonl_aborts_and_never_appends_to_existing_log(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "logs/strategy_signals.jsonl", b'{"event":"BUY"}\nnot-json\n')

    with pytest.raises(ValueError, match="손상된 JSONL line=2"):
        migrate(source, destination)
    assert not (destination / "logs" / "strategy_signals.jsonl").exists()


def test_custom_relative_state_path_is_migrated_to_matching_destination(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, ".env", b"STATE_FILE=custom/runtime-state.json\n")
    state = _write(source, "custom/runtime-state.json", b'{"positions": {"005930": {"qty": 1}}}')
    _write(source, "json/state.json", b'{"legacy_unused": true}')

    copied, _ = migrate(source, destination)

    assert (destination / "custom/runtime-state.json").read_bytes() == state.read_bytes()
    assert not (destination / "json/state.json").exists()
    assert "STATE_FILE=custom/runtime-state.json\n" in (destination / ".env").read_text()


def test_custom_external_path_is_preserved_and_not_copied(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    external = tmp_path / "shared" / "state.json"
    source.mkdir(); destination.mkdir(); external.parent.mkdir()
    external.write_bytes(b'{"positions": {}}')
    _write(source, ".env", f"STATE_FILE={external}\n".encode())

    _, notes = migrate(source, destination)

    assert external.read_bytes() == b'{"positions": {}}'
    assert not (destination / "json" / "state.json").exists()
    assert any("외부 경로는 복사하지 않고 현 위치를 유지" in note for note in notes)
    manifests = list((destination / ".migration_backups").glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert any(item["source"] == str(external) for item in manifest["files"])


def test_interrupted_migration_can_be_rerun_without_overwrite_or_duplication(tmp_path, monkeypatch):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "state.json", b'{"state": 1}')
    _write(source, "cancel_orders.json", b'{"updated_at": 1, "records": {"1": {}}}')
    original_atomic_write = upgrade_migrate._atomic_write
    interrupted = {"done": False}

    def fail_once(path, data, *, mode=None, no_overwrite=False):
        if no_overwrite and path.name == "cancel_orders.json" and not interrupted["done"]:
            interrupted["done"] = True
            raise OSError("simulated interruption")
        return original_atomic_write(path, data, mode=mode, no_overwrite=no_overwrite)

    monkeypatch.setattr(upgrade_migrate, "_atomic_write", fail_once)
    with pytest.raises(OSError, match="simulated interruption"):
        migrate(source, destination)
    assert (destination / "json" / "state.json").read_bytes() == b'{"state": 1}'
    assert not (destination / "json" / "cancel_orders.json").exists()

    monkeypatch.setattr(upgrade_migrate, "_atomic_write", original_atomic_write)
    copied, skipped = migrate(source, destination)
    assert (destination / "json" / "state.json").read_bytes() == b'{"state": 1}'
    assert (destination / "json" / "cancel_orders.json").read_bytes() == b'{"updated_at": 1, "records": {"1": {}}}'
    assert any("동일 파일 유지:" in line and line.endswith("json/state.json") for line in skipped)
    assert any("이전 후 검증 완료:" in line for line in copied)


def test_ambiguous_uncertain_order_state_aborts_without_copying_anything(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "state.json", b'{"positions": {}}')
    _write(source, "uncertain_orders.json", b'{"orders": [{"symbol": "UNKNOWN", "side": "BUY", "qty": 1}]}')

    with pytest.raises(ValueError, match="출처를 확인할 수 없는 주문 종목코드"):
        migrate(source, destination)
    assert not (destination / ".env").exists()
    assert not (destination / "json/state.json").exists()
    assert (source / "uncertain_orders.json").exists()


def test_jsonl_destination_conflict_never_merges_or_appends(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    source_log = _write(source, "logs/strategy_signals.jsonl", b'{"event":"BUY"}\n')
    destination_log = _write(destination, "logs/strategy_signals.jsonl", b'{"event":"SELL"}\n')

    with pytest.raises(FileExistsError, match="아무 파일도 복사하지 않음"):
        migrate(source, destination)
    assert destination_log.read_bytes() == b'{"event":"SELL"}\n'
    assert source_log.read_bytes() == b'{"event":"BUY"}\n'


def test_custom_path_missing_but_legacy_state_exists_fails_closed(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, ".env", b"STATE_FILE=custom/state.json\n")
    _write(source, "state.json", b'{"positions": {}}')

    with pytest.raises(FileExistsError, match="출처를 결정할 수 없습니다"):
        migrate(source, destination)
    assert not (destination / ".env").exists()
    assert not (destination / "json" / "state.json").exists()


def test_duplicate_json_keys_in_order_state_are_rejected_as_ambiguous(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "uncertain_orders.json", b'{"orders": [], "orders": [{"symbol": "005930", "side": "BUY"}]}')

    with pytest.raises(ValueError, match="중복 JSON 키"):
        migrate(source, destination)
    assert not (destination / "json" / "uncertain_orders.json").exists()


def test_custom_data_and_log_directories_are_preserved_and_migrated(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, ".env", b"KIS_DATA_DIR=runtime-data\nKIS_LOG_DIR=runtime-logs\n")
    state = _write(source, "runtime-data/state.json", b'{"positions": {}}')
    signal_log = _write(source, "runtime-logs/strategy_signals.jsonl", b'{"event":"HOLD"}\n')

    migrate(source, destination)

    assert (destination / "runtime-data/state.json").read_bytes() == state.read_bytes()
    assert (destination / "runtime-logs/strategy_signals.jsonl").read_bytes() == signal_log.read_bytes()
    assert "KIS_DATA_DIR=runtime-data\n" in (destination / ".env").read_text()
    assert "KIS_LOG_DIR=runtime-logs\n" in (destination / ".env").read_text()


def test_canonical_and_legacy_state_files_with_different_contents_are_ambiguous(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, ".env", b"KIS_DATA_DIR=json\n")
    _write(source, "json/state.json", b'{"positions": {}}')
    _write(source, "state.json", b'{"positions": {"005930": {"qty": 1}}}')

    with pytest.raises(FileExistsError, match="중복 경로 데이터가 서로 다릅니다"):
        migrate(source, destination)
    assert not (destination / ".env").exists()
    assert not (destination / "json/state.json").exists()


def test_missing_runtime_files_is_a_clean_noop(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()

    copied, skipped = migrate(source, destination)

    assert copied == []
    assert skipped == []
    assert not (destination / ".migration_backups").exists()
    assert list(destination.iterdir()) == []


def test_relative_custom_path_escaping_old_install_is_rejected(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, ".env", b"STATE_FILE=../shared/state.json\n")
    shared = tmp_path / "shared"
    shared.mkdir()
    _write(shared, "state.json", b'{"positions": {}}')

    with pytest.raises(ValueError, match="상대 경로가 기존 설치 디렉터리 밖"):
        migrate(source, destination)
    assert not (destination / ".env").exists()


def test_source_runtime_symlink_is_rejected_before_any_copy(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    outside = tmp_path / "state.json"
    outside.write_text('{"positions": {}}', encoding="utf-8")
    (source / "state.json").symlink_to(outside)

    with pytest.raises(ValueError, match="심볼릭 링크"):
        migrate(source, destination)
    assert not (destination / "json/state.json").exists()


def test_destination_symlink_is_not_followed_or_overwritten(tmp_path):
    source = tmp_path / "old"
    destination = tmp_path / "new"
    source.mkdir(); destination.mkdir()
    _write(source, "state.json", b'{"positions": {}}')
    outside = tmp_path / "external-state.json"
    outside.write_text('{"positions": {}}', encoding="utf-8")
    (destination / "json").mkdir()
    (destination / "json/state.json").symlink_to(outside)

    with pytest.raises(FileExistsError, match="심볼릭 링크"):
        migrate(source, destination)
    assert outside.read_text(encoding="utf-8") == '{"positions": {}}'
