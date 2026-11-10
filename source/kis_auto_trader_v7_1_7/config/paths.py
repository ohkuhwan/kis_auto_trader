"""Central runtime path resolution for KIS Auto Trader.

Relative paths are always resolved against the project root, never the process cwd.
KIS_DATA_DIR defaults to ``json``. Per-file environment variables remain supported
and override the centralized defaults for backward compatibility.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_path(value: str | Path, *, base: Path | None = None) -> Path:
    """Expand a path and resolve relative values against the current project root."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT if base is None else base) / path
    return path.resolve(strict=False)


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return resolve_path(raw) if raw else default.resolve(strict=False)


def runtime_paths() -> dict[str, Path]:
    """Return canonical paths, honoring legacy per-file overrides when provided."""
    data_dir = _env_path("KIS_DATA_DIR", PROJECT_ROOT / "json")
    log_dir = _env_path("KIS_LOG_DIR", PROJECT_ROOT / "logs")
    return {
        "data_dir": data_dir,
        "manual_stop": _env_path("MANUAL_STOP_FILE", data_dir / "manual_stop.json"),
        "monitoring_disabled": data_dir / "monitoring_disabled.json",
        "start_trigger": _env_path("START_TRIGGER_FILE", data_dir / "start_trigger"),
        "log_dir": log_dir,
        "monitor_pid": log_dir / "monitoring.pid",
        "state": _env_path("STATE_FILE", data_dir / "state.json"),
        "token_cache": _env_path("KIS_TOKEN_CACHE_FILE", data_dir / "kis_tokens.json"),
        "config_snapshot": data_dir / "config_snapshot.json",
        "uncertain_orders": _env_path("UNCERTAIN_ORDERS_FILE", data_dir / "uncertain_orders.json"),
        "cancel_orders": _env_path("CANCEL_ORDERS_FILE", data_dir / "cancel_orders.json"),
        "holiday_cache": _env_path("HOLIDAY_CACHE_FILE", data_dir / "holiday_config.json"),
        "strategy_signal_log": _env_path("STRATEGY_SIGNAL_LOG", log_dir / "strategy_signals.jsonl"),
        "builder_symbols": _env_path("BUILDER_SYMBOLS_FILE", PROJECT_ROOT / "config" / "entry_symbols.txt"),
        "holiday_calendar": _env_path("KIS_PAPER_HOLIDAY_CALENDAR", PROJECT_ROOT / "config" / "krx_holidays_2026.json"),
    }


def runtime_path(name: str) -> Path:
    """Resolve one known runtime path by key."""
    try:
        return runtime_paths()[name]
    except KeyError as exc:
        raise KeyError(f"Unknown runtime path key: {name}") from exc
