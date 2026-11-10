#!/usr/bin/env python3
"""Safely migrate KIS runtime data into a V7 installation.

The source is never modified or deleted. Preflight validates recognized JSON and
JSONL before any writes. A byte-for-byte source backup is created before copying;
files are installed atomically without overwriting destinations. Identical files
are idempotently skipped, while differing files or ambiguous source copies stop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

RUNTIME_FILES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("state.json", ("json/state.json", "state.json"), "json"),
    ("uncertain_orders.json", ("json/uncertain_orders.json", "uncertain_orders.json"), "json"),
    ("cancel_orders.json", ("json/cancel_orders.json", "cancel_orders.json"), "json"),
    ("kis_tokens.json", ("json/kis_tokens.json", "kis_tokens.json"), "json"),
    ("holiday_config.json", ("json/holiday_config.json", "config/holiday_config.json", "holiday_config.json"), "json"),
    ("manual_stop.json", ("json/manual_stop.json", "manual_stop.json"), "json"),
    ("monitoring_disabled.json", ("json/monitoring_disabled.json", "monitoring_disabled.json"), "json"),
    ("strategy_signals.jsonl", ("logs/strategy_signals.jsonl", "strategy_signals.jsonl"), "jsonl"),
)
PATH_ENV_TO_FILE = {
    "STATE_FILE": "state.json",
    "UNCERTAIN_ORDERS_FILE": "uncertain_orders.json",
    "CANCEL_ORDERS_FILE": "cancel_orders.json",
    "KIS_TOKEN_CACHE_FILE": "kis_tokens.json",
    "HOLIDAY_CACHE_FILE": "holiday_config.json",
    "MANUAL_STOP_FILE": "manual_stop.json",
    "STRATEGY_SIGNAL_LOG": "strategy_signals.jsonl",
}
LEGACY_DEFAULTS = {
    "STATE_FILE": {"state.json"},
    "UNCERTAIN_ORDERS_FILE": {"uncertain_orders.json"},
    "CANCEL_ORDERS_FILE": {"cancel_orders.json"},
    "KIS_TOKEN_CACHE_FILE": {"kis_tokens.json"},
    "HOLIDAY_CACHE_FILE": {"config/holiday_config.json", "holiday_config.json"},
    "MANUAL_STOP_FILE": {"manual_stop.json"},
    "STRATEGY_SIGNAL_LOG": {"logs/strategy_signals.jsonl", "strategy_signals.jsonl"},
}
SECRET_NAMES = {".env", "kis_tokens.json"}


class MigrationItem(NamedTuple):
    name: str
    source: Path
    destination: Path
    data: bytes
    kind: str
    secret: bool = False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines without expanding variables or executing shell syntax."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            values[key] = value
    return values


def _prepared_env(source_env: Path) -> bytes:
    data = source_env.read_bytes()
    text = data.decode("utf-8-sig")
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if "=" in content and not content.lstrip().startswith("#"):
            key, value = content.split("=", 1)
            unquoted = value.strip().strip('"').strip("'")
            if key.strip() in LEGACY_DEFAULTS and unquoted in LEGACY_DEFAULTS[key.strip()]:
                content = f"{key}="
        output.append(content + ending)
    return "".join(output).encode("utf-8")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path, stop: Path) -> bool:
    """Detect symlinks from a file up to (and including) a known project root."""
    path = Path(os.path.abspath(path))
    stop = Path(os.path.abspath(stop))
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def _resolve_old(raw: str, source: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = source / path
    return path.resolve(strict=False)


def _json_load_no_duplicates(text: str, name: str):
    """Reject duplicate object keys instead of silently choosing the last value."""
    def object_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name}: 중복 JSON 키 {key!r}가 있어 상태 출처가 모호합니다")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=object_hook)


def _validate_data(name: str, data: bytes, kind: str) -> None:
    """Validate syntax before migration; do not rewrite or normalize payload schemas."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}: UTF-8이 아닌 데이터라 이전을 중단합니다: {exc}") from exc
    if kind == "env":
        return
    if kind == "json":
        try:
            payload = _json_load_no_duplicates(text, name)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name}: 손상된 JSON (line={exc.lineno}, column={exc.colno}); 원본을 보존하고 중단합니다") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{name}: 최상위 JSON 값이 객체가 아니므로 이전을 중단합니다")
        if name == "state.json":
            if not isinstance(payload.get("positions", {}), dict) or not isinstance(payload.get("cooldowns", {}), dict):
                raise ValueError("state.json: positions/cooldowns 형식이 객체가 아니므로 이전을 중단합니다")
            if any(not isinstance(value, dict) for value in payload.get("positions", {}).values()):
                raise ValueError("state.json: positions 항목이 객체가 아니므로 이전을 중단합니다")
        elif name == "uncertain_orders.json":
            orders, symbols = payload.get("orders", []), payload.get("symbols", [])
            if not isinstance(orders, list) or not isinstance(symbols, list) or any(not isinstance(item, dict) for item in orders):
                raise ValueError("uncertain_orders.json: orders/symbols 구조가 유효하지 않아 이전을 중단합니다")
            for item in orders:
                symbol = str(item.get("symbol", "")).strip()
                if not symbol or not symbol.isdigit() or len(symbol) > 6 or symbol.zfill(6) == "000000":
                    raise ValueError("uncertain_orders.json: 출처를 확인할 수 없는 주문 종목코드가 있어 이전을 중단합니다")
                if str(item.get("side", "UNKNOWN")).upper() not in {"BUY", "SELL", "UNKNOWN"}:
                    raise ValueError("uncertain_orders.json: 주문 방향이 불명확해 이전을 중단합니다")
                try:
                    qty = int(item.get("qty", 0) or 0)
                    created_ts = float(item.get("created_ts", 1) or 1)
                    checks = int(item.get("checks", 0) or 0)
                    reservation_price = float(item.get("reservation_price", 0) or 0)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("uncertain_orders.json: 주문 수량/시각/조회횟수/예약가격 형식이 불명확합니다") from exc
                if qty < 0 or created_ts <= 0 or checks < 0 or reservation_price < 0:
                    raise ValueError("uncertain_orders.json: 주문 상태 값 범위가 유효하지 않아 이전을 중단합니다")
            for value in symbols:
                symbol = str(value).strip()
                if not symbol.isdigit() or len(symbol) > 6 or symbol.zfill(6) == "000000":
                    raise ValueError("uncertain_orders.json: symbols 목록에 출처를 확인할 수 없는 종목코드가 있습니다")
        elif name == "cancel_orders.json":
            records = payload.get("records")
            if not isinstance(records, dict) or any(not isinstance(value, dict) for value in records.values()):
                raise ValueError("cancel_orders.json: records 구조가 유효하지 않아 이전을 중단합니다")
        elif name == "kis_tokens.json":
            # Token cache may be stale, but it must at least be an object; expiry/fingerprint is checked by KisApi.
            if payload and not isinstance(payload.get("schema_version", 1), int):
                raise ValueError("kis_tokens.json: schema_version 형식이 유효하지 않아 이전을 중단합니다")
        elif name == "manual_stop.json":
            if not str(payload.get("date", "")).strip().isdigit() or len(str(payload.get("date", "")).strip()) != 8:
                raise ValueError("manual_stop.json: 수동 정지 날짜가 불명확해 자동 이전을 중단합니다")
        elif name == "monitoring_disabled.json":
            if payload.get("disabled") is not True:
                raise ValueError("monitoring_disabled.json: 중지 표식이 유효하지 않아 자동 이전을 중단합니다")
        return
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = _json_load_no_duplicates(line, f"{name} line={line_no}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name}: 손상된 JSONL line={line_no}; 원본을 보존하고 중단합니다") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{name}: JSONL line={line_no}이 객체 레코드가 아니므로 중단합니다")


def _destination_for_custom_path(old_path: Path, source: Path, destination: Path) -> Path | None:
    """Relocate source-contained custom paths; external paths remain external and untouched."""
    if not _inside(old_path, source):
        return None
    return destination / old_path.resolve().relative_to(source.resolve())


def _build_items(source: Path, destination: Path) -> tuple[list[MigrationItem], list[str]]:
    source_env = source / ".env"
    if _has_symlink_component(source_env, source):
        raise ValueError("원본 .env 경로에 심볼릭 링크가 있습니다. 경로 출처를 확인한 뒤 일반 파일로 복사해 재시도하십시오.")
    env_values = _read_env(source_env)
    items: list[MigrationItem] = []
    notes: list[str] = []
    if source_env.is_file():
        items.append(MigrationItem(".env", source_env, destination / ".env", _prepared_env(source_env), "env", True))

    data_dir_raw = env_values.get("KIS_DATA_DIR", "").strip()
    log_dir_raw = env_values.get("KIS_LOG_DIR", "").strip()
    # Values matching canonical defaults are not treated as custom overrides.
    data_dir_is_custom = bool(data_dir_raw and data_dir_raw not in {"json", "./json"})
    log_dir_is_custom = bool(log_dir_raw and log_dir_raw not in {"logs", "./logs"})
    data_dir_old = _resolve_old(data_dir_raw, source) if data_dir_raw else source / "json"
    log_dir_old = _resolve_old(log_dir_raw, source) if log_dir_raw else source / "logs"

    for filename, candidates, kind in RUNTIME_FILES:
        env_key = next((key for key, file in PATH_ENV_TO_FILE.items() if file == filename), None)
        raw = env_values.get(env_key, "").strip() if env_key else ""
        old_default = (data_dir_old / filename) if kind == "json" else (log_dir_old / filename)
        canonical = (destination / "json" / filename) if kind == "json" else (destination / "logs" / filename)
        custom_path = False

        # Explicit custom per-file paths take precedence. Relative paths are rooted at the old project.
        if raw and not (env_key and raw in LEGACY_DEFAULTS.get(env_key, set())):
            custom_path = True
            old_default = _resolve_old(raw, source)
            if _inside(old_default, source) and _has_symlink_component(old_default, source):
                raise ValueError(f"{env_key}: 사용자 지정 경로에 심볼릭 링크가 있어 출처를 확정할 수 없습니다: {old_default}")
            if not Path(raw).expanduser().is_absolute() and not _inside(old_default, source):
                raise ValueError(f"사용자 지정 상대 경로가 기존 설치 디렉터리 밖을 가리킵니다: {env_key}={raw}. 절대 경로로 명시하거나 수동 이전하십시오.")
            new_path = _destination_for_custom_path(old_default, source, destination)
            if new_path is None:
                if old_default.is_file():
                    notes.append(f"사용자 지정 외부 경로는 복사하지 않고 현 위치를 유지: {env_key}={old_default}")
                    external_data = old_default.read_bytes()
                    _validate_data(filename, external_data, kind)
                    items.append(MigrationItem(filename, old_default, old_default, external_data, kind, filename in SECRET_NAMES))
                else:
                    notes.append(f"사용자 지정 외부 경로가 존재하지 않아 이동하지 않음: {env_key}={old_default}")
                continue
            canonical = new_path
        elif data_dir_is_custom and filename != "strategy_signals.jsonl":
            # Preserve a custom data directory layout in the new installation.
            custom_path = True
            if data_dir_raw and not Path(data_dir_raw).expanduser().is_absolute() and not _inside(data_dir_old, source):
                raise ValueError(f"KIS_DATA_DIR 상대 경로가 기존 설치 디렉터리 밖을 가리킵니다: {data_dir_raw}. 절대 경로로 명시하거나 수동 이전하십시오.")
            new_path = _destination_for_custom_path(old_default, source, destination)
            if new_path is None:
                if old_default.is_file():
                    notes.append(f"KIS_DATA_DIR 외부 경로는 복사하지 않고 현 위치를 유지: {old_default}")
                    external_data = old_default.read_bytes()
                    _validate_data(filename, external_data, kind)
                    items.append(MigrationItem(filename, old_default, old_default, external_data, kind, filename in SECRET_NAMES))
                continue
            canonical = new_path
        elif log_dir_is_custom and filename == "strategy_signals.jsonl":
            custom_path = True
            if log_dir_raw and not Path(log_dir_raw).expanduser().is_absolute() and not _inside(log_dir_old, source):
                raise ValueError(f"KIS_LOG_DIR 상대 경로가 기존 설치 디렉터리 밖을 가리킵니다: {log_dir_raw}. 절대 경로로 명시하거나 수동 이전하십시오.")
            new_path = _destination_for_custom_path(old_default, source, destination)
            if new_path is None:
                if old_default.is_file():
                    notes.append(f"KIS_LOG_DIR 외부 경로는 복사하지 않고 현 위치를 유지: {old_default}")
                    external_data = old_default.read_bytes()
                    _validate_data(filename, external_data, kind)
                    items.append(MigrationItem(filename, old_default, old_default, external_data, kind, filename in SECRET_NAMES))
                continue
            canonical = new_path

        if custom_path:
            existing = [old_default] if old_default.is_file() else []
            if not existing:
                legacy_existing = [source / rel for rel in candidates if (source / rel).is_file()]
                if legacy_existing:
                    raise FileExistsError(
                        f"사용자 지정 경로에는 파일이 없지만 구버전 기본 경로에 데이터가 있어 출처를 결정할 수 없습니다: "
                        f"configured={old_default}, legacy={', '.join(map(str, legacy_existing))}. 자동 이전을 중단합니다."
                    )
        else:
            existing = [source / rel for rel in candidates if (source / rel).is_file()]
            if any(_has_symlink_component(path, source) for path in existing):
                raise ValueError(f"{filename}: 원본 런타임 파일 경로에 심볼릭 링크가 있어 출처를 확정할 수 없습니다. 일반 파일로 확인 후 재시도하십시오.")
            if old_default.is_file() and old_default not in existing:
                existing.insert(0, old_default)
            # Prefer the resolved configured path when it exists, then canonical legacy order.
            if old_default in existing:
                existing = [old_default] + [p for p in existing if p != old_default]
        if not existing:
            if custom_path:
                notes.append(f"사용자 지정 경로에 이전할 파일이 없음: {env_key or 'KIS_DATA_DIR/KIS_LOG_DIR'}={old_default}")
            continue
        contents = [p.read_bytes() for p in existing]
        if any(payload != contents[0] for payload in contents[1:]):
            raise FileExistsError(f"원본의 중복 경로 데이터가 서로 다릅니다: {filename}: {', '.join(map(str, existing))}")
        chosen = existing[0]
        _validate_data(filename, contents[0], kind)
        # A custom path is preserved in .env and therefore its destination should match that path.
        items.append(MigrationItem(filename, chosen, canonical, contents[0], kind, filename in SECRET_NAMES))

    # Deduplicate aliases that resolve to the same destination, refusing ambiguous content.
    by_destination: dict[Path, MigrationItem] = {}
    for item in items:
        key = item.destination.resolve(strict=False)
        prior = by_destination.get(key)
        if prior and prior.data != item.data:
            raise FileExistsError(f"동일 대상 경로로 매핑되는 파일이 서로 다릅니다: {prior.name}, {item.name}, {key}")
        if prior:
            continue
        by_destination[key] = item
    return list(by_destination.values()), notes


def _atomic_write(path: Path, data: bytes, *, mode: int | None = None, no_overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        if no_overwrite:
            try:
                os.link(tmp, path)
            except FileExistsError as exc:
                raise FileExistsError(f"복사 중 대상 파일이 생성되어 중단했습니다: {path}") from exc
        else:
            os.replace(tmp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _make_backup(items: list[MigrationItem], source: Path, destination: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = destination / ".migration_backups"
    if backup_root.is_symlink():
        raise ValueError(f"백업 경로가 심볼릭 링크입니다: {backup_root}")
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_root, 0o700)
    backup = backup_root / stamp
    backup.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chmod(backup, 0o700)
    manifest: dict[str, object] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "destination": str(destination),
        "files": [],
    }
    entries: list[dict[str, str]] = []
    for index, item in enumerate(items):
        # Preserve the exact original source bytes, including .env before legacy-default normalization.
        original = item.source.read_bytes()
        relative = item.source.resolve().relative_to(source) if _inside(item.source, source) else Path("external") / f"{index}_{item.source.name}"
        backup_rel = Path("files") / relative
        backup_file = backup / backup_rel
        backup_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (backup_file.parent, *backup_file.parent.parents):
            if directory == backup or backup in directory.parents:
                os.chmod(directory, 0o700)
        _atomic_write(backup_file, original, mode=0o600)
        if _sha(backup_file.read_bytes()) != _sha(original):
            raise OSError(f"이전 전 백업 검증 실패: {backup_file}")
        entries.append({"source": str(item.source), "backup": backup_rel.as_posix(), "sha256": _sha(original), "destination": str(item.destination), "prepared_sha256": _sha(item.data)})
    manifest["files"] = entries
    _atomic_write(backup / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(), mode=0o600)
    return backup


def migrate(source: Path, destination: Path, *, dry_run: bool = False) -> tuple[list[str], list[str]]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"원본 설치 디렉터리가 없습니다: {source}")
    if source == destination:
        raise ValueError("원본과 대상 설치 경로가 같습니다.")
    if _inside(destination, source) or _inside(source, destination):
        raise ValueError("원본과 대상은 서로 포함되지 않는 별도 설치 디렉터리여야 합니다.")

    items, notes = _build_items(source, destination)
    copied: list[str] = []
    skipped: list[str] = []
    conflicts: list[str] = []
    for item in items:
        if item.destination != item.source and _has_symlink_component(item.destination, destination):
            conflicts.append(f"대상 경로에 심볼릭 링크가 있어 자동 이전하지 않음: {item.destination}")
            continue
        try:
            _validate_data(item.name, item.data, item.kind)
        except ValueError:
            raise
        if item.destination.exists() or item.destination.is_symlink():
            if item.destination.is_symlink():
                conflicts.append(f"심볼릭 링크 대상은 자동 이전하지 않음: {item.destination}")
                continue
            if item.destination.is_file() and item.destination.read_bytes() == item.data:
                skipped.append(f"동일 파일 유지: {item.destination}")
                continue
            conflicts.append(str(item.destination))
    if conflicts:
        raise FileExistsError("대상 파일이 이미 존재하고 내용이 다르므로 이전을 중단했습니다(아무 파일도 복사하지 않음): " + ", ".join(conflicts) + ". 파일을 별도 보관하고 수동 비교하세요.")

    if dry_run:
        return [f"복사 예정: {item.destination} (원본={item.source})" for item in items if not item.destination.exists()], skipped + notes

    pending_items = [item for item in items if not item.destination.exists()]
    # Snapshot external custom-path files during an actual migration, even though they stay in place.
    # Existing secret files whose permissions will be hardened are backed up before chmod as well.
    external_items = [item for item in items if item.destination == item.source]
    permission_items = [item for item in items if item.secret and item.destination.is_file()
                        and not item.destination.is_symlink()
                        and item.destination.stat().st_mode & 0o777 != 0o600]
    backup_candidates = pending_items + (external_items if pending_items else []) + permission_items
    backup_items_by_path: dict[Path, MigrationItem] = {}
    for item in backup_candidates:
        backup_items_by_path[item.destination.resolve(strict=False)] = item
    backup_items = list(backup_items_by_path.values())
    backup = _make_backup(backup_items, source, destination) if backup_items else None
    # Back up only files that will actually be copied. Identical reruns create no redundant backup.
    # Each destination is atomic and never overwritten.
    for item in pending_items:
        _atomic_write(item.destination, item.data, mode=0o600 if item.secret else 0o640, no_overwrite=True)
        copied.append(f"복사 완료: {item.destination}")

    # Verify every destination, including identical files skipped during preflight.
    for item in items:
        if not item.destination.is_file():
            raise OSError(f"이전 후 검증 실패: 대상 파일이 없습니다: {item.destination}")
        actual = item.destination.read_bytes()
        if _sha(actual) != _sha(item.data):
            raise OSError(f"이전 후 검증 실패: SHA-256 불일치: {item.destination}")
        _validate_data(item.name, actual, item.kind)
        if item.secret and not item.destination.is_symlink() and item.destination.stat().st_mode & 0o777 != 0o600:
            os.chmod(item.destination, 0o600)
    if backup:
        copied.append(f"백업 완료: {backup}")
    if items:
        copied.append(f"이전 후 검증 완료: {len(items)}개 파일 (SHA-256 + JSON/JSONL 구문)")
    return copied, skipped + notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="기존 설치 디렉터리 (원본은 수정/삭제하지 않음)")
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1], help="새 설치 디렉터리 (기본: 이 프로젝트 루트)")
    parser.add_argument("--dry-run", action="store_true", help="파일 변경 없이 이전 대상과 충돌 여부만 확인")
    args = parser.parse_args()
    try:
        copied, skipped = migrate(args.source, args.destination, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2
    for line in copied + skipped:
        print(line)
    print("원본 설치 디렉터리와 원본 운영 파일은 삭제하거나 변경하지 않았습니다.")
    print("기존 대상 충돌은 덮어쓰지 않으며, 재실행 시 동일 파일은 건너뛰고 누락 파일만 이어서 이전합니다.")
    print("strategy_signals.jsonl은 JSONL로 검증 후 대상이 없을 때만 복사하며 자동 병합하지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
