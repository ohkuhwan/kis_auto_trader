#!/usr/bin/env python3
"""비밀값을 제외한 운영 설정 변경 이력을 안전하게 기록합니다."""
from __future__ import annotations

import json
import os
import tempfile
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SAFE_KEYS = (
    "KIS_PAPER", "KIS_ENABLE_TRADING", "ENTRY_MODE", "STRATEGY_EVALUATION_MODE",
    "KIS_CONDITION_NAME", "KIS_CONDITION_AGGRESSIVE", "KIS_CONDITION_STABLE",
    "BUILDER_SYMBOLS_FILE", "STRATEGY_PROFILES_FILE", "ENTRY_STRATEGY_FILE", "EXIT_STRATEGY_FILE",
    "MAX_POSITIONS", "POSITION_SIZING", "MAX_TRADES_PER_DAY", "MAX_CANDIDATES",
    "STRATEGY_BUILDER_MAX_SYMBOLS", "MIN_BUY_ORDER_AMOUNT_KRW", "KIS_FEE_RATE_PCT",
    "KIS_MARKET_PRICE_BUFFER_PCT",
    "MAX_DAILY_LOSS_PCT", "SCAN_INTERVAL_SEC", "BALANCE_INTERVAL_SEC",
    "ORDER_RECONCILE_INTERVAL_SEC", "MAX_CALLS_PER_SEC", "MAX_CALLS_PER_MIN",
    "KIS_RATE_LIMIT_SAFETY_MARGIN", "QUOTE_STALE_SEC", "QUOTE_REST_MIN_INTERVAL_SEC",
    "DAILY_BAR_CACHE_TTL_SEC", "DAILY_BAR_ERROR_RETRY_SEC",
    "KIS_TR_RATE_LIMITS_JSON", "BUY_ORDER_TIMEOUT_SEC", "SELL_ORDER_TIMEOUT_SEC",
    "SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC", "STOP_GRACE_TIMEOUT_SEC", "AUTO_SHUTDOWN_TIME",
    "ENTRY_START_TIME", "ENTRY_END_TIME", "KIS_BUY_ORDER_MODE", "KIS_SEL_ORDER_MODE",
    "KIS_STOP_LOSS_ORDER_MODE", "BUILDER_OBSERVE_ALL_STRATEGIES",
    "REENTRY_COOLDOWN_SEC", "SELL_RETRY_COOLDOWN_SEC",
    "ENTRY_MAX_DAILY_CHANGE_PCT", "ENTRY_MIN_DAILY_CHANGE_PCT",
)


def main() -> int:
    env_path = ROOT / ".env"
    values: dict[str, str] = {}
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    snapshot = {key: values.get(key, "<unset>") for key in SAFE_KEYS}
    os.environ.update(values)
    from config.paths import runtime_path
    target = runtime_path("config_snapshot")
    previous = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("settings"), dict):
                previous = loaded["settings"]
        except Exception:
            backup = target.with_name(target.name + f".corrupt_{datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d_%H%M%S')}")
            try:
                os.replace(target, backup)
                print(f"CONFIG_SNAPSHOT status=previous_snapshot_invalid backup={backup}")
            except OSError:
                print("CONFIG_SNAPSHOT status=previous_snapshot_invalid action=backup_failed")
                return 2

    changed = [key for key in SAFE_KEYS if previous.get(key, "<unset>") != snapshot[key]]
    now = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    payload = {"schema_version": 1, "updated_at_kst": now, "settings": snapshot}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    if not previous:
        print(f"CONFIG_SNAPSHOT status=created changed_keys={','.join(changed)} file={target}")
    elif changed:
        changes = ";".join(f"{key}:{previous.get(key, '<unset>')}->{snapshot[key]}" for key in changed)
        print(f"CONFIG_SNAPSHOT status=updated changed_count={len(changed)} changes={changes} file={target}")
    else:
        print(f"CONFIG_SNAPSHOT status=unchanged key_count={len(SAFE_KEYS)} file={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
