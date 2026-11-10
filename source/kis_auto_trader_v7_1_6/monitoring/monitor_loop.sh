#!/usr/bin/env bash
# V7.1.6 Proot fallback: keep monitoring after cutoff; watchdog blocks out-of-window starts.
set -u
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
LOG_DIR="$PROJECT_ROOT/logs"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
if command -v python3 >/dev/null 2>&1; then LOG_DIR="$(python3 - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("log_dir"))
PYCODE
)"; fi
mkdir -p "$LOG_DIR"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { printf 'monitor loop: Python is required to resolve runtime paths\n' >&2; exit 3; }
PID_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitor_pid"))
PYCODE
)"
mkdir -p "$(dirname -- "$PID_FILE")"
log() { local log_date; log_date="$(TZ=Asia/Seoul date +%Y%m%d)"; printf '%s monitor: %s\n' "$(TZ=Asia/Seoul date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$LOG_DIR/monitoring_${log_date}.log"; }
read_shutdown_time() {
  local value=""
  if [[ -f .env ]]; then value="$(sed -n 's/^AUTO_SHUTDOWN_TIME=//p' .env | tail -n 1 | tr -d '\r"' | cut -d '#' -f1 | xargs || true)"; fi
  if [[ "$value" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then printf '%s' "$value"; else printf '15:10'; fi
}
trap 'rm -f "$PID_FILE"; log "MONITOR_OUTCOME status=stopped trigger=monitor_loop pid=$$"; exit 0' TERM INT
printf '%s\n' "$$" > "$PID_FILE"
log "MONITOR_OUTCOME status=started trigger=monitor_loop pid=$$ project=$PROJECT_ROOT cutoff=$(read_shutdown_time) timezone=Asia/Seoul persistent=1"
while true; do
  now="$(TZ=Asia/Seoul date +%H:%M)"
  cutoff="$(read_shutdown_time)"
  # Do not terminate the monitor at cutoff. watchdog.sh simply refuses to restart
  # the trader outside the configured window and on weekends; this loop checks again later.
  if "$PROJECT_ROOT/monitoring/watchdog.sh"; then :; else rc=$?; log "WATCHDOG_OUTCOME status=failed exit_code=$rc"; fi
  if [[ "$now" > "$cutoff" || "$now" == "$cutoff" ]]; then
    sleep 300 & wait $! || true
  else
    sleep 30 & wait $! || true
  fi
done
