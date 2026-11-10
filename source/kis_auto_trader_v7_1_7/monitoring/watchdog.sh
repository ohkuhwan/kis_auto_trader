#!/usr/bin/env bash
# One-shot watchdog: never override a same-day manual stop; only start in a KST trading window.
set -u
TRIGGER="watchdog"
if [[ $# -gt 0 ]]; then
  if [[ $# -eq 2 && "$1" == "--trigger" ]]; then TRIGGER="$2"
  else echo "사용법: $0 [--trigger watchdog|scheduler|installer]" >&2; exit 2; fi
fi
case "$TRIGGER" in watchdog|scheduler|installer) ;; *) echo "사용법: $0 [--trigger watchdog|scheduler|installer]" >&2; exit 2;; esac
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
ACTOR="${SUDO_USER:-$(id -un)}"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
PY="$(command -v python3 || true)"
LOG_DIR="$PROJECT_ROOT/logs"
if [[ -n "$PY" ]]; then
  resolved_log_dir="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("log_dir"))
PYCODE
)"
  [[ -n "$resolved_log_dir" ]] && LOG_DIR="$resolved_log_dir"
fi
mkdir -p "$LOG_DIR"
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log() {
  local d
  d="$(TZ=Asia/Seoul date +%Y%m%d)"
  kis_log_append "$LOG_DIR" "watchdog_${d}.log" "trigger=$TRIGGER actor=$ACTOR project=$PROJECT_ROOT $*"
}
if [[ -z "$PY" ]]; then log "WATCHDOG_OUTCOME status=failed reason=python_missing fail_closed=1"; exit 1; fi
KST_NOW="$(TZ=Asia/Seoul date +%H:%M)"
# Automatic recovery/scheduler starts are forbidden before 08:00 KST. Manual starts
# go through scripts/start.sh and are intentionally not governed by this watchdog.
AUTO_START_AT="08:00"
should_log_periodic() { local minute="${KST_NOW##*:}"; [[ "$minute" == "00" || "$minute" == "30" ]]; }
TODAY="$(TZ=Asia/Seoul date +%Y%m%d)"
if [[ "$KST_NOW" < "$AUTO_START_AT" ]]; then
  if should_log_periodic; then log "skip=before_start_time WATCHDOG_OUTCOME status=skipped reason=before_start_time now=$KST_NOW start_at=$AUTO_START_AT date=$TODAY"; fi
  exit 0
fi
SHUTDOWN_AT="15:10"
if [[ -f .env ]]; then configured="$(sed -n 's/^AUTO_SHUTDOWN_TIME=//p' .env | tail -n 1 | tr -d '\r"' | cut -d '#' -f1 | xargs || true)"; [[ "$configured" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] && SHUTDOWN_AT="$configured"; fi
if [[ "$KST_NOW" > "$SHUTDOWN_AT" || "$KST_NOW" == "$SHUTDOWN_AT" ]]; then
  if [[ "$KST_NOW" == "$SHUTDOWN_AT" ]] || should_log_periodic; then
    log "skip=after_cutoff WATCHDOG_OUTCOME status=skipped reason=after_cutoff now=$KST_NOW cutoff=$SHUTDOWN_AT date=$TODAY"
  fi
  exit 0
fi
DOW="$(TZ=Asia/Seoul date +%u)"
if [[ "$DOW" -gt 5 ]]; then
  if should_log_periodic; then log "WATCHDOG_OUTCOME status=skipped reason=weekend date=$TODAY weekday=$DOW"; fi
  exit 0
fi
# Monitoring suppression is persistent until start_monitoring.sh explicitly resumes monitoring.
MONITORING_DISABLED_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitoring_disabled"))
PYCODE
)"
if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
  marker_result="$("$PY" - "$MONITORING_DISABLED_FILE" <<'PYCODE'
import json,sys
try:
    data=json.load(open(sys.argv[1],encoding="utf-8"))
    if not isinstance(data,dict) or data.get("disabled") is not True:
        raise ValueError("invalid marker structure")
    print("DISABLED")
except Exception:
    print("INVALID")
PYCODE
)"
  if [[ "$marker_result" == "DISABLED" ]]; then
    if should_log_periodic; then log "WATCHDOG_OUTCOME status=skipped reason=monitoring_disabled file=$MONITORING_DISABLED_FILE"; fi
    exit 0
  fi
  log "WATCHDOG_OUTCOME status=failed reason=invalid_monitoring_disabled_marker file=$MONITORING_DISABLED_FILE fail_closed=1"
  exit 1
fi
# A manual stop suppresses watchdog recovery only for the KST date on which it was requested.
if [[ -n "$PY" ]]; then
  MANUAL_STOP_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path('manual_stop'))
PYCODE
)"
  if [[ -f "$MANUAL_STOP_FILE" ]]; then
    marker_date="$("$PY" - "$MANUAL_STOP_FILE" <<'PYCODE'
import json,sys
try:
 d=json.load(open(sys.argv[1],encoding='utf-8')); print(d.get('date',''))
except Exception: print('INVALID')
PYCODE
)"
    if [[ "$marker_date" == "$TODAY" ]]; then
      if should_log_periodic; then log "skip=manual_stop_marker WATCHDOG_OUTCOME status=skipped reason=manual_stop_marker date=$TODAY file=$MANUAL_STOP_FILE"; fi
      exit 0
    fi
    if [[ "$marker_date" =~ ^[0-9]{8}$ && "$marker_date" < "$TODAY" ]]; then
      # Do not remove an expired marker during the unlocked preflight. A concurrent
      # stop.sh could replace it with today's marker between the read and rm. The
      # systemd request_start.sh or the Proot locked section clears it safely later.
      log "manual_stop_marker=expired_pending_lock marker_date=$marker_date today=$TODAY action=defer_cleanup_until_start_lock"
    else log "WATCHDOG_OUTCOME status=failed reason=invalid_manual_stop_marker file=$MANUAL_STOP_FILE marker_date=$marker_date fail_closed=1"; exit 1; fi
  fi
fi
# A missing annual KRX calendar must not silently turn a holiday into a trading day.
CALENDAR_FILE="$PROJECT_ROOT/config/krx_holidays_${TODAY:0:4}.json"
CALENDAR_FILE="${KIS_PAPER_HOLIDAY_CALENDAR:-$CALENDAR_FILE}"
if [[ "$CALENDAR_FILE" != /* ]]; then CALENDAR_FILE="$PROJECT_ROOT/$CALENDAR_FILE"; fi
if [[ ! -f "$CALENDAR_FILE" ]]; then log "WATCHDOG_OUTCOME status=failed reason=holiday_calendar_missing file=$CALENDAR_FILE date=$TODAY fail_closed=1"; exit 1; fi
# Check the year-specific KRX holiday calendar and optional user-added holidays.
if [[ -n "$PY" ]]; then
  calendar_result="$("$PY" - "$CALENDAR_FILE" "$TODAY" <<'PYCODE' 2>&1
import json, os, sys
from pathlib import Path
day=sys.argv[2]; calendar=Path(sys.argv[1])
try:
    data=json.loads(calendar.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or not isinstance(data.get('holidays',[]),list):
        raise ValueError('invalid calendar structure')
except Exception as exc:
    print(f'CALENDAR_ERROR:{type(exc).__name__}:{exc}')
    raise SystemExit(2)
extra={x.strip().replace('-','') for x in os.getenv('KIS_PAPER_HOLIDAYS','').split(',') if x.strip()}
holidays={str(x.get('date','')).replace('-','') for x in data.get('holidays',[]) if isinstance(x,dict)}
if day in holidays:
    print('HOLIDAY:calendar')
    raise SystemExit(1)
if day in extra:
    print('HOLIDAY:manual')
    raise SystemExit(1)
PYCODE
)"
  calendar_rc=$?
  if [[ "$calendar_rc" -eq 1 ]]; then
    if should_log_periodic; then log "WATCHDOG_OUTCOME status=skipped reason=holiday date=$TODAY detail=${calendar_result//$'\n'/; }"; fi
    exit 0
  fi
  if [[ "$calendar_rc" -ne 0 ]]; then log "WATCHDOG_OUTCOME status=failed reason=holiday_calendar_invalid date=$TODAY file=$CALENDAR_FILE detail=${calendar_result//$'\n'/; } fail_closed=1"; exit 1; fi
fi
# Account order lock is a trading-degraded state, not a dead process.
# If the process is down, watchdog may restart it: OrderManager restores the lock and
# the restarted process continues read-only reconciliation without sending order POSTs.
ACCOUNT_ORDER_LOCK_ACTIVE=0
ACCOUNT_ORDER_LOCK_STATUS="UNLOCKED"
ACCOUNT_ORDER_LOCK_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("account_order_lock"))
PYCODE
)"
if [[ -f "$ACCOUNT_ORDER_LOCK_FILE" ]]; then
  account_lock_status="$("$PY" - "$ACCOUNT_ORDER_LOCK_FILE" <<'PYCODE'
import json,sys
try:
    data=json.load(open(sys.argv[1],encoding="utf-8"))
    if not isinstance(data,dict) or not isinstance(data.get("locked"),bool):
        raise ValueError("invalid lock marker structure")
    print("LOCKED" if data["locked"] else "UNLOCKED")
except Exception as exc:
    print("INVALID:" + type(exc).__name__)
PYCODE
)"
  case "$account_lock_status" in
    LOCKED) ACCOUNT_ORDER_LOCK_ACTIVE=1; ACCOUNT_ORDER_LOCK_STATUS="LOCKED" ;;
    INVALID*) ACCOUNT_ORDER_LOCK_ACTIVE=1; ACCOUNT_ORDER_LOCK_STATUS="INVALID" ;;
  esac
fi
PID1_COMM="$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ' || true)"
if [[ "$PID1_COMM" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then
  if [[ "$EUID" -eq 0 ]]; then SYSTEMCTL=(systemctl); elif command -v sudo >/dev/null 2>&1; then SYSTEMCTL=(sudo systemctl); else log "ERROR=systemd_permission_missing"; exit 1; fi
  if "${SYSTEMCTL[@]}" is-active --quiet kis-auto-trader.service; then
    if [[ "$ACCOUNT_ORDER_LOCK_ACTIVE" -eq 1 ]]; then
      if should_log_periodic; then log "WATCHDOG_OUTCOME status=degraded reason=account_order_lock lock_status=$ACCOUNT_ORDER_LOCK_STATUS service=active action=none"; fi
    elif should_log_periodic; then
      log "WATCHDOG_OUTCOME status=healthy reason=service_active unit=kis-auto-trader.service"
    fi
    exit 0
  fi
  if pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then log "WATCHDOG_OUTCOME status=deferred reason=orphan_process_present service_inactive=1"; exit 0; fi
  if [[ "$ACCOUNT_ORDER_LOCK_ACTIVE" -eq 1 ]]; then
    log "action=start_requested unit=kis-auto-trader.service account_order_lock=$ACCOUNT_ORDER_LOCK_STATUS mode=read_only_reconcile"
  else
    log "action=start_requested unit=kis-auto-trader.service now=$KST_NOW cutoff=$SHUTDOWN_AT"
  fi
  if [[ "$EUID" -eq 0 ]]; then
    START_REQUEST_LOG="$(kis_prepare_shared_log "$LOG_DIR" "start_request_${TODAY}.log")" || { log "WATCHDOG_OUTCOME status=start_request_failed reason=start_request_log_unavailable"; exit 1; }
    if "$PROJECT_ROOT/scripts/request_start.sh" --trigger "$TRIGGER" >> "$START_REQUEST_LOG" 2>&1; then rc=0; else rc=$?; fi
  else
    START_REQUEST_LOG="$(kis_prepare_shared_log "$LOG_DIR" "start_request_${TODAY}.log")" || { log "WATCHDOG_OUTCOME status=start_request_failed reason=start_request_log_unavailable"; exit 1; }
    if sudo "$PROJECT_ROOT/scripts/request_start.sh" --trigger "$TRIGGER" >> "$START_REQUEST_LOG" 2>&1; then rc=0; else rc=$?; fi
  fi
  if [[ "$rc" -eq 10 ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=manual_stop_marker_race trigger=$TRIGGER unit=kis-auto-trader.service"
    exit 0
  elif [[ "$rc" -eq 11 ]]; then
    log "WATCHDOG_OUTCOME status=deferred reason=start_stop_lock_busy trigger=$TRIGGER unit=kis-auto-trader.service"
    exit 0
  elif [[ "$rc" -eq 12 ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=monitoring_disabled_race trigger=$TRIGGER unit=kis-auto-trader.service"
    exit 0
  elif [[ "$rc" -eq 13 ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=start_window_policy trigger=$TRIGGER unit=kis-auto-trader.service"
    exit 0
  elif [[ "$rc" -eq 0 ]]; then
    log "WATCHDOG_OUTCOME status=start_request_accepted trigger=$TRIGGER unit=kis-auto-trader.service"
  else
    log "WATCHDOG_OUTCOME status=start_request_failed trigger=$TRIGGER unit=kis-auto-trader.service exit_code=$rc"
  fi
  exit "$rc"
fi
# Serialize the Proot start decision with scripts/stop.sh and re-check the marker
# after acquiring the shared lock, not only during the earlier watchdog preflight.
START_TRIGGER_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path('start_trigger'))
PYCODE
)"
LOCK_FILE="${START_TRIGGER_FILE}.lock"
LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
mkdir -p "$LOCK_PARENT"
if ! command -v flock >/dev/null 2>&1; then log "WATCHDOG_OUTCOME status=failed reason=flock_missing fail_closed=1 executor=proot"; exit 1; fi
[[ -e "$LOCK_FILE" ]] || ( umask 0007; : > "$LOCK_FILE" )
if [[ "$EUID" -eq 0 ]]; then
  lock_group="$(stat -c '%g' "$LOCK_PARENT")"
  chgrp "$lock_group" "$LOCK_FILE" && chmod 0660 "$LOCK_FILE" || { log "WATCHDOG_OUTCOME status=failed reason=start_stop_lock_permissions_failed fail_closed=1 executor=proot"; exit 1; }
elif ! [[ -w "$LOCK_FILE" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    lock_group="$(stat -c '%g' "$LOCK_PARENT")"
    sudo chown "$ACTOR:$lock_group" "$LOCK_FILE" && sudo chmod 0660 "$LOCK_FILE" || { log "WATCHDOG_OUTCOME status=failed reason=start_stop_lock_permissions_failed fail_closed=1 executor=proot"; exit 1; }
  else
    log "WATCHDOG_OUTCOME status=failed reason=start_stop_lock_not_writable fail_closed=1 executor=proot"
    exit 1
  fi
else
  chmod 0660 "$LOCK_FILE" || { log "WATCHDOG_OUTCOME status=failed reason=start_stop_lock_permissions_failed fail_closed=1 executor=proot"; exit 1; }
fi
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "WATCHDOG_OUTCOME status=deferred reason=start_stop_lock_busy executor=proot"
  exit 0
fi
log "START_STOP_LOCK action=acquired trigger=$TRIGGER executor=proot"
# Re-evaluate the complete auto-start window under the shared lock. The date,
# weekday, cutoff, and annual holiday calendar may change while waiting for it.
LOCKED_TODAY="$(TZ=Asia/Seoul date +%Y%m%d)"
LOCKED_NOW="$(TZ=Asia/Seoul date +%H:%M)"
LOCKED_DOW="$(TZ=Asia/Seoul date +%u)"
if [[ "$LOCKED_NOW" < "$AUTO_START_AT" ]]; then
  log "WATCHDOG_OUTCOME status=skipped reason=before_start_time_locked now=$LOCKED_NOW start_at=$AUTO_START_AT date=$LOCKED_TODAY executor=proot"
  exit 0
fi
if [[ "$LOCKED_NOW" > "$SHUTDOWN_AT" || "$LOCKED_NOW" == "$SHUTDOWN_AT" ]]; then
  log "WATCHDOG_OUTCOME status=skipped reason=after_cutoff_locked now=$LOCKED_NOW cutoff=$SHUTDOWN_AT date=$LOCKED_TODAY executor=proot"
  exit 0
fi
if [[ "$LOCKED_DOW" -gt 5 ]]; then
  log "WATCHDOG_OUTCOME status=skipped reason=weekend_locked date=$LOCKED_TODAY weekday=$LOCKED_DOW executor=proot"
  exit 0
fi
LOCKED_CALENDAR_FILE="$PROJECT_ROOT/config/krx_holidays_${LOCKED_TODAY:0:4}.json"
LOCKED_CALENDAR_FILE="${KIS_PAPER_HOLIDAY_CALENDAR:-$LOCKED_CALENDAR_FILE}"
if [[ "$LOCKED_CALENDAR_FILE" != /* ]]; then LOCKED_CALENDAR_FILE="$PROJECT_ROOT/$LOCKED_CALENDAR_FILE"; fi
if [[ ! -f "$LOCKED_CALENDAR_FILE" ]]; then
  log "WATCHDOG_OUTCOME status=failed reason=holiday_calendar_missing_under_start_lock file=$LOCKED_CALENDAR_FILE date=$LOCKED_TODAY fail_closed=1 executor=proot"
  exit 1
fi
if LOCKED_CALENDAR_RESULT="$("$PY" - "$LOCKED_CALENDAR_FILE" "$LOCKED_TODAY" <<'PYCODE' 2>&1
import json, os, sys
from pathlib import Path
day=sys.argv[2]
try:
    data=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if not isinstance(data,dict) or not isinstance(data.get('holidays',[]),list): raise ValueError('invalid calendar structure')
except Exception as exc:
    print(f'CALENDAR_ERROR:{type(exc).__name__}:{exc}')
    raise SystemExit(2)
extra={x.strip().replace('-','') for x in os.getenv('KIS_PAPER_HOLIDAYS','').split(',') if x.strip()}
holidays={str(x.get('date','')).replace('-','') for x in data.get('holidays',[]) if isinstance(x,dict)}
if day in holidays:
    print('HOLIDAY:calendar')
    raise SystemExit(1)
if day in extra:
    print('HOLIDAY:manual')
    raise SystemExit(1)
PYCODE
)"; then
  LOCKED_CALENDAR_RC=0
else
  LOCKED_CALENDAR_RC=$?
fi
if [[ "$LOCKED_CALENDAR_RC" -eq 1 ]]; then
  log "WATCHDOG_OUTCOME status=skipped reason=holiday_locked date=$LOCKED_TODAY detail=${LOCKED_CALENDAR_RESULT//$'\n'/; } executor=proot"
  exit 0
elif [[ "$LOCKED_CALENDAR_RC" -ne 0 ]]; then
  log "WATCHDOG_OUTCOME status=failed reason=holiday_calendar_invalid_under_start_lock date=$LOCKED_TODAY file=$LOCKED_CALENDAR_FILE detail=${LOCKED_CALENDAR_RESULT//$'\n'/; } fail_closed=1 executor=proot"
  exit 1
fi
TODAY="$LOCKED_TODAY"
KST_NOW="$LOCKED_NOW"
# Re-check persistent monitoring suppression after acquiring the shared lock.
if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
  locked_monitoring_status="$("$PY" - "$MONITORING_DISABLED_FILE" <<'PYCODE'
import json,sys
try:
    data=json.load(open(sys.argv[1],encoding="utf-8"))
    if not isinstance(data,dict) or data.get("disabled") is not True: raise ValueError("invalid marker")
    print("DISABLED")
except Exception: print("INVALID")
PYCODE
)"
  if [[ "$locked_monitoring_status" == "DISABLED" ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=monitoring_disabled_race executor=proot file=$MONITORING_DISABLED_FILE"
    exit 0
  fi
  log "WATCHDOG_OUTCOME status=failed reason=invalid_monitoring_disabled_marker_under_start_lock file=$MONITORING_DISABLED_FILE fail_closed=1 executor=proot"
  exit 1
fi
if [[ -f "$MANUAL_STOP_FILE" ]]; then
  locked_marker_date="$("$PY" - "$MANUAL_STOP_FILE" <<'PYCODE'
import json,sys
try:
 d=json.load(open(sys.argv[1],encoding='utf-8')); print(d.get('date',''))
except Exception: print('INVALID')
PYCODE
)"
  if [[ "$locked_marker_date" == "$TODAY" ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=manual_stop_marker_race date=$TODAY executor=proot"
    exit 0
  fi
  if [[ "$locked_marker_date" =~ ^[0-9]{8}$ && "$locked_marker_date" < "$TODAY" ]]; then
    if rm -f -- "$MANUAL_STOP_FILE"; then log "MANUAL_STOP_MARKER action=expired_under_start_lock marker_date=$locked_marker_date today=$TODAY executor=proot";
    else log "WATCHDOG_OUTCOME status=failed reason=manual_stop_marker_expiry_failed fail_closed=1 executor=proot"; exit 1; fi
  else
    log "WATCHDOG_OUTCOME status=failed reason=invalid_manual_stop_marker_under_start_lock date=$locked_marker_date fail_closed=1 executor=proot"
    exit 1
  fi
fi
if pgrep -f "[p]ython[^ ]* -u $PROJECT_ROOT/main\.py" >/dev/null 2>&1 || pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then
  if [[ "$ACCOUNT_ORDER_LOCK_ACTIVE" -eq 1 ]]; then
    if should_log_periodic; then log "WATCHDOG_OUTCOME status=degraded reason=account_order_lock lock_status=$ACCOUNT_ORDER_LOCK_STATUS executor=proot process=active action=none"; fi
  elif should_log_periodic; then
    log "WATCHDOG_OUTCOME status=healthy reason=trader_process_present executor=proot"
  fi
  exit 0
fi
if [[ "$ACCOUNT_ORDER_LOCK_ACTIVE" -eq 1 ]]; then
  log "action=start_requested executor=proot account_order_lock=$ACCOUNT_ORDER_LOCK_STATUS mode=read_only_reconcile"
else
  log "action=start_requested executor=proot now=$KST_NOW cutoff=$SHUTDOWN_AT"
fi
KIS_START_LOCK_HELD=1 nohup "$PROJECT_ROOT/scripts/start.sh" --trigger "$TRIGGER" >> "$LOG_DIR/startup_${TODAY}.log" 2>&1 </dev/null &
pid=$!
sleep 1
if kill -0 "$pid" 2>/dev/null || pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then
  log "WATCHDOG_OUTCOME status=start_launched trigger=$TRIGGER executor=proot launcher_pid=$pid"
else
  if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=monitoring_disabled_race trigger=$TRIGGER executor=proot"
    exit 0
  fi
  if [[ -f "$MANUAL_STOP_FILE" ]]; then
    log "WATCHDOG_OUTCOME status=skipped reason=manual_stop_marker_race trigger=$TRIGGER executor=proot"
    exit 0
  fi
  log "WATCHDOG_OUTCOME status=start_failed trigger=$TRIGGER executor=proot launcher_pid=$pid"
  exit 1
fi
