#!/usr/bin/env bash
# Request a systemd start with an auditable one-shot source token.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
ACTOR="${SUDO_USER:-$(id -un)}"
TRIGGER=""
while [[ $# -gt 0 ]]; do case "$1" in --trigger) [[ $# -ge 2 ]] || exit 2; TRIGGER="$2"; shift 2;; *) echo "알 수 없는 인자: $1" >&2; exit 2;; esac; done
case "$TRIGGER" in manual|scheduler|watchdog|installer) ;; *) echo "허용된 시작 출처는 manual, scheduler, watchdog 또는 installer입니다." >&2; exit 2;; esac
[[ -f .env ]] && { set -a; source .env; set +a; }
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "Python 3가 필요합니다." >&2; exit 3; }
START_TRIGGER_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path('start_trigger'))
PYCODE
)"
MANUAL_STOP_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path('manual_stop'))
PYCODE
)"
MONITORING_DISABLED_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path('monitoring_disabled'))
PYCODE
)"
LOG_DIR="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path('log_dir'))
PYCODE
)"
mkdir -p "$(dirname -- "$START_TRIGGER_FILE")" "$LOG_DIR"
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log_control() {
  local message="$*" date_tag
  date_tag="$(TZ=Asia/Seoul date +%Y%m%d)"
  kis_log_control "$LOG_DIR" "$message"
  # watchdog의 stdout redirection만 의존하지 않고 요청 스크립트가 직접 감사 로그를 기록합니다.
  kis_log_append "$LOG_DIR" "start_request_${date_tag}.log" "$message"
}
LOCK_ACTIVE=0
trap 'rc=$?; if [[ "${LOCK_ACTIVE:-0}" == "1" ]]; then log_control "START_STOP_LOCK action=released trigger=$TRIGGER user=$ACTOR exit_code=$rc" || true; fi' EXIT
if [[ "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" != "systemd" ]] || ! command -v systemctl >/dev/null 2>&1; then
  log_control "START_REQUEST trigger=$TRIGGER executor=non_systemd rejected=1 user=$ACTOR"
  echo "systemd 환경에서만 사용 가능합니다." >&2; exit 4
fi
if [[ "$EUID" -eq 0 ]]; then SYSTEMCTL=(systemctl)
elif command -v sudo >/dev/null 2>&1; then SYSTEMCTL=(sudo systemctl)
else log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=systemd_permission_missing user=$ACTOR"; echo "systemd 서비스 시작에는 sudo/root 권한이 필요합니다." >&2; exit 1
fi
# Share a lock with manual stop while publishing the marker and requesting a start.
# A manual stop that wins the lock cannot be undone by a watchdog check already in flight.
command -v flock >/dev/null 2>&1 || { log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=flock_missing fail_closed=1"; echo "flock 명령이 필요합니다." >&2; exit 1; }
LOCK_FILE="${START_TRIGGER_FILE}.lock"
LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
mkdir -p "$LOCK_PARENT"
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
if ! flock -n 9; then
  log_control "START_REQUEST trigger=$TRIGGER outcome=another_start_stop_operation_in_progress user=$ACTOR unit=kis-auto-trader.service"
  exit 11
fi
LOCK_ACTIVE=1
log_control "START_STOP_LOCK action=acquired trigger=$TRIGGER user=$ACTOR"
# Automatic sources must re-check persistent monitoring suppression after acquiring the lock.
if [[ "$TRIGGER" != "manual" && -f "$MONITORING_DISABLED_FILE" ]]; then
  monitoring_status="$("$PY" - "$MONITORING_DISABLED_FILE" <<'PYCODE'
import json,sys
try:
    data=json.load(open(sys.argv[1],encoding="utf-8"))
    if not isinstance(data,dict) or data.get("disabled") is not True: raise ValueError("invalid marker")
    print("DISABLED")
except Exception: print("INVALID")
PYCODE
)"
  if [[ "$monitoring_status" == "DISABLED" ]]; then
    log_control "START_REQUEST trigger=$TRIGGER outcome=suppressed reason=monitoring_disabled user=$ACTOR file=$MONITORING_DISABLED_FILE"
    exit 12
  fi
  log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=invalid_monitoring_disabled_marker fail_closed=1 user=$ACTOR"
  exit 1
fi
# Automatic sources must re-check the manual-stop marker after acquiring the lock.
if [[ "$TRIGGER" != "manual" && -f "$MANUAL_STOP_FILE" ]]; then
  today="$(TZ=Asia/Seoul date +%Y%m%d)"
  marker_status="$("$PY" - "$MANUAL_STOP_FILE" "$today" <<'PYCODE'
import json, re, sys
from pathlib import Path
path=Path(sys.argv[1]); today=sys.argv[2]
try:
    data=json.loads(path.read_text(encoding='utf-8'))
    marker_date=data.get('date') if isinstance(data,dict) else None
    if not isinstance(marker_date,str) or not re.fullmatch(r'\d{8}',marker_date): raise ValueError('invalid date')
    if marker_date == today: print('TODAY')
    elif marker_date < today: print('EXPIRED')
    else: print('FUTURE')
except Exception: print('INVALID')
PYCODE
)"
  case "$marker_status" in
    TODAY)
      log_control "START_REQUEST trigger=$TRIGGER outcome=suppressed reason=manual_stop_marker date=$today user=$ACTOR"
      exit 10
      ;;
    EXPIRED)
      if rm -f -- "$MANUAL_STOP_FILE"; then log_control "MANUAL_STOP_MARKER action=expired trigger=$TRIGGER user=$ACTOR";
      else log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=manual_stop_marker_expiry_failed fail_closed=1"; exit 1; fi
      ;;
    *)
      log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=invalid_or_future_manual_stop_marker fail_closed=1 user=$ACTOR"
      exit 1
      ;;
  esac
fi
# Scheduler/watchdog/installer starts must re-check the complete KST trading
# window while holding the same lock as manual stop. The watchdog's earlier
# preflight alone is not authoritative because it can wait across a cutoff,
# midnight, weekend, or holiday boundary before acquiring this lock.
if [[ "$TRIGGER" != "manual" ]]; then
  policy_today="$(TZ=Asia/Seoul date +%Y%m%d)"
  policy_now="$(TZ=Asia/Seoul date +%H:%M)"
  policy_dow="$(TZ=Asia/Seoul date +%u)"
  policy_start="08:00"
  if [[ "$policy_now" < "$policy_start" ]]; then
    log_control "START_REQUEST trigger=$TRIGGER outcome=suppressed reason=before_start_time date=$policy_today now=$policy_now start_at=$policy_start user=$ACTOR"
    exit 13
  fi
  policy_cutoff="${AUTO_SHUTDOWN_TIME:-15:10}"
  [[ "$policy_cutoff" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || policy_cutoff="15:10"
  if [[ "$policy_now" > "$policy_cutoff" || "$policy_now" == "$policy_cutoff" ]]; then
    log_control "START_REQUEST trigger=$TRIGGER outcome=suppressed reason=after_cutoff date=$policy_today now=$policy_now cutoff=$policy_cutoff user=$ACTOR"
    exit 13
  fi
  if [[ "$policy_dow" -gt 5 ]]; then
    log_control "START_REQUEST trigger=$TRIGGER outcome=suppressed reason=weekend date=$policy_today weekday=$policy_dow user=$ACTOR"
    exit 13
  fi
  policy_calendar="$PROJECT_ROOT/config/krx_holidays_${policy_today:0:4}.json"
  policy_calendar="${KIS_PAPER_HOLIDAY_CALENDAR:-$policy_calendar}"
  if [[ "$policy_calendar" != /* ]]; then policy_calendar="$PROJECT_ROOT/$policy_calendar"; fi
  if [[ ! -f "$policy_calendar" ]]; then
    log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=holiday_calendar_missing file=$policy_calendar date=$policy_today fail_closed=1"
    exit 1
  fi
  if policy_result="$("$PY" - "$policy_calendar" "$policy_today" <<'PYCODE' 2>&1
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
    :
  else
    policy_rc=$?
    if [[ "$policy_rc" -eq 1 ]]; then
      log_control "START_REQUEST trigger=$TRIGGER outcome=suppressed reason=holiday date=$policy_today detail=${policy_result//$'\n'/; } user=$ACTOR"
      exit 13
    fi
    log_control "START_OUTCOME status=failed trigger=$TRIGGER reason=holiday_calendar_invalid file=$policy_calendar date=$policy_today detail=${policy_result//$'\n'/; } fail_closed=1"
    exit 1
  fi
fi
if [[ "$TRIGGER" == "manual" ]]; then rm -f -- "$MANUAL_STOP_FILE"; log_control "MANUAL_STOP_MARKER action=cleared trigger=manual user=$ACTOR"; fi
if "${SYSTEMCTL[@]}" is-active --quiet kis-auto-trader.service; then
  log_control "START_REQUEST trigger=$TRIGGER outcome=already_active unit=kis-auto-trader.service user=$ACTOR"
  exit 0
fi
# Atomic token write; start.sh consumes it when systemd launches the service.
tmp="${START_TRIGGER_FILE}.tmp.$$"
printf '%s\n' "$TRIGGER" > "$tmp"
chmod 644 "$tmp"
mv -f -- "$tmp" "$START_TRIGGER_FILE"
if pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then
  rm -f -- "$START_TRIGGER_FILE"
  log_control "START_OUTCOME status=orphan_process_present trigger=$TRIGGER user=$ACTOR unit=kis-auto-trader.service"
  echo "systemd 서비스는 비활성이지만 동일 프로젝트의 Python 프로세스가 남아 있어 시작을 중단합니다." >&2
  exit 1
fi
log_control "START_REQUEST trigger=$TRIGGER reason=$(case "$TRIGGER" in manual) echo operator_requested_start;; scheduler) echo weekday_schedule;; watchdog) echo process_recovery;; installer) echo installer_start;; esac) user=$ACTOR unit=kis-auto-trader.service action=systemctl_start"
if "${SYSTEMCTL[@]}" start kis-auto-trader.service; then
  sleep 1
  if "${SYSTEMCTL[@]}" is-active --quiet kis-auto-trader.service; then
    log_control "START_OUTCOME status=active trigger=$TRIGGER unit=kis-auto-trader.service"
    exit 0
  fi
  rm -f -- "$START_TRIGGER_FILE"
  log_control "START_OUTCOME status=not_active trigger=$TRIGGER unit=kis-auto-trader.service"
  echo "systemctl start 명령 후 서비스가 active가 아닙니다." >&2; exit 1
else
  rc=$?
  rm -f -- "$START_TRIGGER_FILE"
  log_control "START_OUTCOME status=failed trigger=$TRIGGER unit=kis-auto-trader.service exit_code=$rc"
  exit "$rc"
fi
