#!/usr/bin/env python3
"""Read-only local health check for the KIS trader process and log freshness."""
from __future__ import annotations
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from config.paths import runtime_path

LOG_DIR = runtime_path("log_dir")
LOG = LOG_DIR / f"trader_{datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d')}.log"

def main() -> int:
    # Use /proc on Linux; this script does not send orders or call the broker API.
    proc = Path("/proc")
    found = False
    if proc.exists():
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                cmd = (item / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
            except (OSError, PermissionError):
                continue
            try:
                cwd = str((item / "cwd").resolve())
            except (OSError, RuntimeError):
                cwd = ""
            if "main.py" in cmd and (Path(cwd) == ROOT or ROOT in Path(cwd).parents):
                found = True
                break
    print(f"process={'RUNNING' if found else 'NOT_FOUND'}")
    if LOG.exists():
        age = int(time.time() - LOG.stat().st_mtime)
        print(f"trader_log_age_seconds={age}")
    else:
        print("trader_log=NOT_FOUND")
    return 0 if found else 1

if __name__ == "__main__":
    raise SystemExit(main())
