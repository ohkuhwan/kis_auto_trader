#!/usr/bin/env bash
# Stop only this project's trader. Manual requests create a same-day watchdog suppression marker.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
ACTOR="${SUDO_USER:-$(id -un)}"
TRIGGER="manual"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --trigger) [[ $# -ge 2 ]] || { echo "--trigger 값이 필요합니다." >&2; exit 2; }; TRIGGER="$2"; shift 2 ;;
    *) echo "사용법: $0 [--trigger manual|systemd|watchdog|scheduler|reload_config|installer]" >&2; exit 2 ;;
  esac
done
case "$TRIGGER" in manual|systemd|watchdog|scheduler|reload_config|installer) ;; *) echo "잘못된 실행 출처: $TRIGGER" >&2; exit 2;; esac
LOG_DIR="$PROJECT_ROOT/logs"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log_control() { kis_log_control "$LOG_DIR" "$*"; }
STOP_LOCK_ACTIVE=0
trap 'rc=$?; if [[ "${STOP_LOCK_ACTIVE:-0}" == "1" ]]; then log_control "START_STOP_LOCK action=released trigger=$TRIGGER user=$ACTOR exit_code=$rc" || true; fi' EXIT
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { log_control "STOP_OUTCOME status=failed trigger=$TRIGGER reason=python_missing"; echo "Python 3가 필요합니다." >&2; exit 3; }
resolve_runtime_path() { "$PY" - "$1" "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[2])
from config.paths import runtime_path
print(runtime_path(sys.argv[1]))
PYCODE
}
LOG_DIR="$(resolve_runtime_path log_dir)"
mkdir -p "$LOG_DIR"
MANUAL_STOP_FILE="$(resolve_runtime_path manual_stop)"
START_TRIGGER_FILE="$(resolve_runtime_path start_trigger)"
if [[ "$TRIGGER" == "manual" ]]; then
  command -v flock >/dev/null 2>&1 || { log_control "STOP_OUTCOME status=failed trigger=manual reason=flock_missing fail_closed=1"; echo "오류: 시작/정지 동기화를 위해 flock 명령이 필요합니다." >&2; exit 1; }
  mkdir -p "$(dirname -- "$MANUAL_STOP_FILE")" "$(dirname -- "$START_TRIGGER_FILE")"
  # Hold the shared start/stop lock through the actual stop. The service ExecStop
  # uses trigger=systemd and deliberately does not acquire this manual-operation lock.
  LOCK_FILE="${START_TRIGGER_FILE}.lock"
  LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
  mkdir -p "$LOCK_PARENT"
  [[ -e "$LOCK_FILE" ]] || ( umask 0007; : > "$LOCK_FILE" )
  if [[ "$EUID" -eq 0 ]]; then
    lock_group="$(stat -c '%g' "$LOCK_PARENT")"
    chgrp "$lock_group" "$LOCK_FILE" && chmod 0660 "$LOCK_FILE" || { log_control "STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
  elif ! [[ -w "$LOCK_FILE" ]]; then
    if command -v sudo >/dev/null 2>&1; then
      lock_group="$(stat -c '%g' "$LOCK_PARENT")"
      sudo chown "$ACTOR:$lock_group" "$LOCK_FILE" && sudo chmod 0660 "$LOCK_FILE" || { log_control "STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
    else
      log_control "STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_not_writable fail_closed=1"
      exit 1
    fi
  else
    chmod 0660 "$LOCK_FILE" || { log_control "STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
  fi
  exec 8>"$LOCK_FILE"
  if ! flock -x 8; then
    log_control "STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_failed fail_closed=1"
    echo "오류: 시작/정지 동기화 잠금을 얻지 못했습니다." >&2
    exit 1
  fi
  if ! "$PY" - "$MANUAL_STOP_FILE" <<'PYCODE'
import getpass, json, os, sys, tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
path=Path(sys.argv[1]); path.parent.mkdir(parents=True,exist_ok=True)
now=datetime.now(ZoneInfo('Asia/Seoul'))
data={'date':now.strftime('%Y%m%d'),'timestamp':now.isoformat(timespec='seconds'),'trigger':'manual','reason':'operator_requested_stop','user':os.getenv('SUDO_USER') or getpass.getuser()}
fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=str(path.parent))
try:
  with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False); f.write('\n'); f.flush(); os.fsync(f.fileno())
  os.chmod(tmp,0o600); os.replace(tmp,path)
  dir_fd=os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
  try:
      os.fsync(dir_fd)
  finally:
      os.close(dir_fd)
finally:
  if os.path.exists(tmp): os.unlink(tmp)
PYCODE
  then
    log_control "STOP_OUTCOME status=failed trigger=manual reason=manual_stop_marker_write_failed file=$MANUAL_STOP_FILE user=$ACTOR"
    echo "오류: 수동 정지 표식을 기록하지 못해 watchdog 재시작 억제를 보장할 수 없습니다." >&2
    exit 1
  fi
  log_control "MANUAL_STOP_MARKER action=created trigger=manual user=$ACTOR reason=operator_requested_stop file=$MANUAL_STOP_FILE date=$(TZ=Asia/Seoul date +%Y%m%d)"
fi
# For a systemd-managed trader, stop the unit itself. Watchdog is the sole recovery
# authority; the service unit uses Restart=no so direct child signals do not auto-relaunch.
if [[ "$TRIGGER" == "manual" && "$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ')" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then
  if [[ "$EUID" -eq 0 ]]; then SYSTEMCTL=(systemctl)
  elif command -v sudo >/dev/null 2>&1; then SYSTEMCTL=(sudo systemctl)
  else SYSTEMCTL=(systemctl)
  fi
  if "${SYSTEMCTL[@]}" is-active --quiet kis-auto-trader.service; then
    if [[ "$EUID" -ne 0 ]] && ! command -v sudo >/dev/null 2>&1; then
      log_control "STOP_OUTCOME status=failed trigger=manual reason=systemd_permission_missing"; echo "systemd 서비스 종료에는 sudo/root 권한이 필요합니다." >&2; exit 1
    fi
    log_control "STOP_REQUEST trigger=manual reason=operator_requested_stop user=$ACTOR project_root=$PROJECT_ROOT action=systemctl_stop unit=kis-auto-trader.service"
    if "${SYSTEMCTL[@]}" stop kis-auto-trader.service && ! "${SYSTEMCTL[@]}" is-active --quiet kis-auto-trader.service; then
      log_control "STOP_OUTCOME status=stopped trigger=manual project_root=$PROJECT_ROOT unit=kis-auto-trader.service"
      echo "KIS 자동매매 systemd 서비스를 종료했습니다. 당일 watchdog 자동 재시작은 억제됩니다."
      exit 0
    fi
    log_control "STOP_OUTCOME status=failed trigger=manual project_root=$PROJECT_ROOT unit=kis-auto-trader.service"
    echo "오류: systemd 서비스 종료 결과를 확인하지 못했습니다." >&2; exit 1
  fi
fi
pattern="[p]ython.*$PROJECT_ROOT/main\.py"
pids="$(pgrep -f "$pattern" || true)"
log_control "STOP_REQUEST trigger=$TRIGGER reason=${TRIGGER}_stop user=$ACTOR project_root=$PROJECT_ROOT pid=$$ target_pids=${pids//$'\n'/,}"
if [[ -z "$pids" ]]; then
  echo "KIS 자동매매 프로세스가 실행 중이 아닙니다."
  log_control "STOP_OUTCOME status=already_stopped trigger=$TRIGGER project_root=$PROJECT_ROOT"
  exit 0
fi
while IFS= read -r pid; do [[ "$pid" =~ ^[0-9]+$ ]] || continue; kill -TERM "$pid" 2>/dev/null || true; done <<< "$pids"
STOP_GRACE_TIMEOUT_SEC="${STOP_GRACE_TIMEOUT_SEC:-240}"
[[ "$STOP_GRACE_TIMEOUT_SEC" =~ ^[0-9]+$ ]] || STOP_GRACE_TIMEOUT_SEC=240
STOP_GRACE_TIMEOUT_SEC=$(( STOP_GRACE_TIMEOUT_SEC < 60 ? 60 : STOP_GRACE_TIMEOUT_SEC > 240 ? 240 : STOP_GRACE_TIMEOUT_SEC ))
log_control "STOP_WAIT_BEGIN trigger=$TRIGGER timeout_sec=$STOP_GRACE_TIMEOUT_SEC target_pids=${pids//$'\n'/,}"
for _ in $(seq 1 "$STOP_GRACE_TIMEOUT_SEC"); do remaining="$(pgrep -f "$pattern" || true)"; [[ -z "$remaining" ]] && break; sleep 1; done
remaining="$(pgrep -f "$pattern" || true)"
if [[ -n "$remaining" ]]; then
  log_control "STOP_OUTCOME status=timeout trigger=$TRIGGER project_root=$PROJECT_ROOT timeout_sec=$STOP_GRACE_TIMEOUT_SEC remaining_pids=${remaining//$'\n'/,} fail_closed=1"
  echo "오류: 종료 신호 후에도 프로세스가 남아 있습니다: ${remaining//$'\n'/,}" >&2
  exit 1
fi
log_control "STOP_OUTCOME status=stopped trigger=$TRIGGER project_root=$PROJECT_ROOT"
echo "KIS 자동매매 프로세스 종료를 확인했습니다."
