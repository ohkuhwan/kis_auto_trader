#!/usr/bin/env bash
# Start the trader and log both the requested source and the process exit result.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
ACTOR="${SUDO_USER:-$(id -un)}"
LOG_DIR="$PROJECT_ROOT/logs"
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log_control() { kis_log_control "$LOG_DIR" "$*"; }
TRIGGER="manual"
UNIT_CONTEXT="${KIS_SYSTEMD_SERVICE:-0}"
if [[ "${1:-}" == "--trigger" ]]; then
  [[ $# -ge 2 ]] || { echo "사용법: $0 [--trigger manual|systemd|watchdog|scheduler|reload_config|installer]" >&2; exit 2; }
  TRIGGER="$2"; shift 2
fi
[[ $# -eq 0 ]] || { echo "알 수 없는 인자입니다." >&2; exit 2; }
case "$TRIGGER" in manual|systemd|watchdog|scheduler|reload_config|installer) ;; *) echo "잘못된 실행 출처: $TRIGGER" >&2; exit 2;; esac
[[ -f .env ]] || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=missing_env"; echo "ERROR: $PROJECT_ROOT/.env 파일이 없습니다. .env.example을 복사하고 설정하세요." >&2; exit 2; }
set -a
# shellcheck disable=SC1091
source .env
set +a
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
PYTHON_BIN="$(command -v python3 || true)"
[[ -n "$PYTHON_BIN" ]] || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=python_missing"; echo "ERROR: python/python3를 찾을 수 없습니다." >&2; exit 3; }
LOG_DIR="$($PYTHON_BIN - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("log_dir"))
PYCODE
)"
mkdir -p "$LOG_DIR"
START_LOCK_ACTIVE=0
trap 'if [[ "${START_LOCK_ACTIVE:-0}" == "1" ]]; then flock -u 9 2>/dev/null || true; fi' EXIT
resolve_runtime_path() {
  "$PYTHON_BIN" - "$1" "$PROJECT_ROOT" <<'PYCODE'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from config.paths import runtime_path
print(runtime_path(sys.argv[1]))
PYCODE
}
MANUAL_STOP_FILE="$(resolve_runtime_path manual_stop)"
START_TRIGGER_FILE="$(resolve_runtime_path start_trigger)"
# A direct manual start on a systemd host must be managed by the unit, not orphaned in a shell.
if [[ "$UNIT_CONTEXT" != "1" && "$TRIGGER" == "manual" && "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]]; then
  log_control "START_REQUEST trigger=manual reason=operator_requested_start user=$ACTOR action=delegate_to_systemd unit=kis-auto-trader.service"
  exec "$PROJECT_ROOT/scripts/request_start.sh" --trigger manual
fi
# systemd unit sets KIS_SYSTEMD_SERVICE=1. Scheduler/watchdog place a one-shot source token
# before requesting systemctl start; direct shell invocation defaults to manual.
if [[ "$UNIT_CONTEXT" == "1" ]]; then
  if [[ -f "$START_TRIGGER_FILE" ]]; then
    requested="$(head -n 1 "$START_TRIGGER_FILE" 2>/dev/null || true)"
    case "$requested" in manual|scheduler|watchdog|installer|reload_config) TRIGGER="$requested" ;; *) TRIGGER="systemd" ;; esac
    rm -f -- "$START_TRIGGER_FILE"
  else
    TRIGGER="systemd"
  fi
fi
START_LOCK_ACTIVE=0
if [[ "$UNIT_CONTEXT" != "1" ]]; then
  LOCK_FILE="${START_TRIGGER_FILE}.lock"
  LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
  mkdir -p "$LOCK_PARENT"
  if [[ "${KIS_START_LOCK_HELD:-0}" == "1" ]]; then
    # watchdog/reload_config passes the already-held lock descriptor to this wrapper.
    : >&9 || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=inherited_start_stop_lock_missing fail_closed=1"; exit 1; }
    START_LOCK_ACTIVE=1
  else
    command -v flock >/dev/null 2>&1 || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=flock_missing fail_closed=1"; echo "flock 명령이 필요합니다." >&2; exit 1; }
    [[ -e "$LOCK_FILE" ]] || ( umask 0007; : > "$LOCK_FILE" )
    if [[ "$EUID" -eq 0 ]]; then
      lock_group="$(stat -c '%g' "$LOCK_PARENT")"
      chgrp "$lock_group" "$LOCK_FILE" && chmod 0660 "$LOCK_FILE" || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
    elif ! [[ -w "$LOCK_FILE" ]]; then
      if command -v sudo >/dev/null 2>&1; then
        lock_group="$(stat -c '%g' "$LOCK_PARENT")"
        sudo chown "$ACTOR:$lock_group" "$LOCK_FILE" && sudo chmod 0660 "$LOCK_FILE" || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
      else
        log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=start_stop_lock_not_writable fail_closed=1"
        exit 1
      fi
    else
      chmod 0660 "$LOCK_FILE" || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
    fi
    exec 9>"$LOCK_FILE"
    if ! flock -x 9; then log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=start_stop_lock_failed fail_closed=1"; exit 1; fi
    START_LOCK_ACTIVE=1
  fi
  # Re-check after the shared lock is held. A watchdog preflight must not be able
  # to race a later manual stop and launch the trader after the stop has returned.
  if [[ "$TRIGGER" != "manual" && -f "$MANUAL_STOP_FILE" ]]; then
    today="$(TZ=Asia/Seoul date +%Y%m%d)"
    marker_status="$("$PYTHON_BIN" - "$MANUAL_STOP_FILE" "$today" <<'PYCODE'
import json, re, sys
from pathlib import Path
try:
    data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    marker_date=data.get('date') if isinstance(data,dict) else None
    if not isinstance(marker_date,str) or not re.fullmatch(r'\d{8}',marker_date): raise ValueError('invalid date')
    if marker_date == sys.argv[2]: print('TODAY')
    elif marker_date < sys.argv[2]: print('EXPIRED')
    else: print('FUTURE')
except Exception: print('INVALID')
PYCODE
)"
    case "$marker_status" in
      TODAY)
        log_control "START_OUTCOME status=suppressed trigger=$TRIGGER reason=manual_stop_marker date=$today"
        flock -u 9 || true; log_control "START_STOP_LOCK action=released trigger=$TRIGGER reason=manual_stop_marker"; START_LOCK_ACTIVE=0
        exit 0
        ;;
      EXPIRED)
        rm -f -- "$MANUAL_STOP_FILE" || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=manual_stop_marker_expiry_failed fail_closed=1"; exit 1; }
        log_control "MANUAL_STOP_MARKER action=expired trigger=$TRIGGER marker_date=before_$today"
        ;;
      *)
        log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=invalid_or_future_manual_stop_marker fail_closed=1"
        exit 1
        ;;
    esac
  fi
fi
if [[ "$TRIGGER" == "manual" ]]; then
  rm -f -- "$MANUAL_STOP_FILE"
  log_control "MANUAL_STOP_MARKER action=cleared trigger=manual"
fi
case "$TRIGGER" in
  manual) START_REASON="operator_requested_start" ;;
  scheduler) START_REASON="weekday_schedule" ;;
  watchdog) START_REASON="process_recovery" ;;
  reload_config) START_REASON="configuration_reload" ;;
  installer) START_REASON="installer_start" ;;
  *) START_REASON="service_manager_start" ;;
esac
log_control "START_REQUEST trigger=$TRIGGER reason=$START_REASON user=$ACTOR project_root=$PROJECT_ROOT pid=$$ paper=${KIS_PAPER:-unset} trading=${KIS_ENABLE_TRADING:-unset}"
# Refuse a second process for this exact installation while holding the shared lock.
if pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then
  log_control "START_OUTCOME status=already_running trigger=$TRIGGER project_root=$PROJECT_ROOT"
  echo "이미 실행 중인 KIS 프로세스가 있습니다: $PROJECT_ROOT" >&2
  if [[ "$START_LOCK_ACTIVE" == "1" ]]; then flock -u 9 || true; log_control "START_STOP_LOCK action=released trigger=$TRIGGER reason=already_running"; START_LOCK_ACTIVE=0; fi
  exit 0
fi
if snapshot_output="$("$PYTHON_BIN" "$PROJECT_ROOT/scripts/config_snapshot.py" 2>&1)"; then
  while IFS= read -r snapshot_line; do [[ -n "$snapshot_line" ]] && log_control "$snapshot_line"; done <<< "$snapshot_output"
else
  log_control "CONFIG_SNAPSHOT status=failed reason=helper_error detail=${snapshot_output//$'\n'/; }"
  echo "WARNING: 운영 설정 snapshot 기록에 실패했습니다. 매매 설정은 변경하지 않고 계속합니다." >&2
fi
log_control "START_LAUNCH trigger=$TRIGGER source=$PROJECT_ROOT/main.py python=$PYTHON_BIN"
if [[ "$UNIT_CONTEXT" == "1" ]]; then
  # systemd owns this foreground process; request_start.sh serializes its start.
  set +e
  "$PYTHON_BIN" -u "$PROJECT_ROOT/main.py"
  rc=$?
  set -e
else
  # Spawn the Python process while holding the lock, but do not pass the lock FD
  # to Python. Once its command line is visible, release the lock so stop.sh can
  # acquire it and reliably stop the now-existing process.
  set +e
  "$PYTHON_BIN" -u "$PROJECT_ROOT/main.py" 9>&- &
  child_pid=$!
  set -e
  launch_confirmed=0
  for _ in $(seq 1 50); do
    if ! kill -0 "$child_pid" 2>/dev/null; then break; fi
    child_args="$(ps -p "$child_pid" -o args= 2>/dev/null || true)"
    if [[ "$child_args" == *"$PROJECT_ROOT/main.py"* ]]; then launch_confirmed=1; break; fi
    sleep 0.1
  done
  if [[ "$START_LOCK_ACTIVE" == "1" ]]; then flock -u 9 || true; log_control "START_STOP_LOCK action=released trigger=$TRIGGER reason=python_process_visible child_pid=$child_pid"; START_LOCK_ACTIVE=0; fi
  if [[ "$launch_confirmed" -ne 1 ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
    rc=1
    log_control "START_OUTCOME status=launch_unconfirmed trigger=$TRIGGER child_pid=$child_pid project_root=$PROJECT_ROOT"
    exit "$rc"
  fi
  set +e
  wait "$child_pid"
  rc=$?
  set -e
fi
log_control "START_OUTCOME status=process_exited trigger=$TRIGGER exit_code=$rc project_root=$PROJECT_ROOT"
exit "$rc"
