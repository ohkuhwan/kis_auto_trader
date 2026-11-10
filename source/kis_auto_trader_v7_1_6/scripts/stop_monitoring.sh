#!/usr/bin/env bash
# Stop monitoring only. Use scripts/stop.sh or systemctl stop to stop trading separately.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
ACTOR="${SUDO_USER:-$(id -un)}"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || { echo "Python 3가 필요합니다." >&2; exit 3; }
LOG_DIR="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("log_dir"))
PYCODE
)"
MONITOR_PID_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitor_pid"))
PYCODE
)"
MONITORING_DISABLED_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitoring_disabled"))
PYCODE
)"
START_TRIGGER_FILE="$("$PY" - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("start_trigger"))
PYCODE
)"
LOCK_FILE="${START_TRIGGER_FILE}.lock"
LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
mkdir -p "$LOG_DIR" "$(dirname -- "$MONITOR_PID_FILE")" "$(dirname -- "$MONITORING_DISABLED_FILE")" "$LOCK_PARENT"
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log_control() { kis_log_control "$LOG_DIR" "$*"; }
say() { printf '[모니터링 종료] %s\n' "$*"; }
log_control "MONITORING_STOP_REQUEST trigger=manual user=$ACTOR project_root=$PROJECT_ROOT pid=$$"
command -v flock >/dev/null 2>&1 || { log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=flock_missing fail_closed=1"; echo "flock 명령이 필요합니다." >&2; exit 1; }
[[ -e "$LOCK_FILE" ]] || ( umask 0007; : > "$LOCK_FILE" )
if [[ "$EUID" -eq 0 ]]; then
  lock_group="$(stat -c '%g' "$LOCK_PARENT")"
  chgrp "$lock_group" "$LOCK_FILE" && chmod 0660 "$LOCK_FILE" || { log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
elif ! [[ -w "$LOCK_FILE" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    lock_group="$(stat -c '%g' "$LOCK_PARENT")"
    sudo chown "$ACTOR:$lock_group" "$LOCK_FILE" && sudo chmod 0660 "$LOCK_FILE" || { log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
  else
    log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_not_writable fail_closed=1"; exit 1
  fi
else
  chmod 0660 "$LOCK_FILE" || { log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_permissions_failed fail_closed=1"; exit 1; }
fi
exec 8>"$LOCK_FILE"
flock -x 8 || { log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=start_stop_lock_failed fail_closed=1"; exit 1; }
log_control "START_STOP_LOCK action=acquired trigger=stop_monitoring user=$ACTOR"
if ! "$PY" - "$MONITORING_DISABLED_FILE" <<'PYCODE'
import getpass, json, os, sys, tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
path=Path(sys.argv[1]); path.parent.mkdir(parents=True,exist_ok=True)
now=datetime.now(ZoneInfo("Asia/Seoul"))
data={"disabled":True,"date":now.strftime("%Y%m%d"),"timestamp":now.isoformat(timespec="seconds"),"trigger":"manual","reason":"operator_requested_monitoring_stop","user":os.getenv("SUDO_USER") or getpass.getuser()}
fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=str(path.parent))
try:
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(data,stream,ensure_ascii=False); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
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
  log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual reason=disabled_marker_write_failed file=$MONITORING_DISABLED_FILE"
  echo "오류: 감시 중지 표식을 기록하지 못했습니다. 감시 중지 절차를 중단합니다." >&2
  exit 1
fi
log_control "MONITORING_DISABLED_MARKER action=created trigger=manual user=$ACTOR file=$MONITORING_DISABLED_FILE"
PID1_COMM="$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ' || true)"
if [[ "$PID1_COMM" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then
  if [[ "$EUID" -eq 0 ]]; then SUDO=""; else command -v sudo >/dev/null 2>&1 || { log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual executor=systemd reason=sudo_missing user=$ACTOR"; say "sudo/root 권한이 필요합니다."; exit 1; }; SUDO="sudo"; fi
  if $SUDO systemctl disable --now kis-auto-trader-start.timer kis-auto-trader-watchdog.timer; then
    # Stop any oneshot check already dispatched by a timer. Do not stop the trader service.
    if $SUDO systemctl stop kis-auto-trader-start.service kis-auto-trader-watchdog.service; then
      for unit in kis-auto-trader-start.timer kis-auto-trader-watchdog.timer kis-auto-trader-start.service kis-auto-trader-watchdog.service; do
        if $SUDO systemctl is-active --quiet "$unit"; then
          log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual executor=systemd reason=unit_still_active unit=$unit trader_service=untouched"
          echo "오류: 감시 unit이 아직 active 상태입니다: $unit" >&2; exit 1
        fi
      done
      log_control "MONITORING_STOP_OUTCOME status=stopped trigger=manual executor=systemd timers=kis-auto-trader-start.timer,kis-auto-trader-watchdog.timer checks=kis-auto-trader-start.service,kis-auto-trader-watchdog.service trader_service=untouched"
      say "systemd의 예약 타이머와 진행 중인 감시 확인 작업을 중지했습니다. 매매 서비스는 별도로 실행 중일 수 있습니다."
    else
      rc=$?
      log_control "MONITORING_STOP_OUTCOME status=partial trigger=manual executor=systemd timers=stopped check_stop_exit_code=$rc trader_service=untouched"
      echo "예약 타이머는 중지했지만 진행 중인 감시 확인 작업을 모두 중지하지 못했습니다." >&2
      exit "$rc"
    fi
  else
    rc=$?
    log_control "MONITORING_STOP_OUTCOME status=failed trigger=manual executor=systemd exit_code=$rc"
    exit "$rc"
  fi
else
  monitor_is_running() {
    [[ "${pid:-}" =~ ^[0-9]+$ ]] || return 1
    local args state
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    state="$(ps -p "$pid" -o stat= 2>/dev/null || true)"
    [[ -n "$args" && "$state" != Z* ]] && grep -Fq "$PROJECT_ROOT/monitoring/monitor_loop.sh" <<< "$args"
  }
  if [[ -f "$MONITOR_PID_FILE" ]]; then
    pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
    if monitor_is_running; then
      log_control "MONITORING_STOP_ACTION action=signal trigger=manual executor=proot pid=$pid user=$ACTOR"
      kill -TERM "$pid" 2>/dev/null || true
      stopped=0
      for _ in $(seq 1 10); do
        if ! monitor_is_running; then stopped=1; break; fi
        sleep 1
      done
      if [[ "$stopped" -eq 1 ]]; then
        rm -f -- "$MONITOR_PID_FILE"
        log_control "MONITORING_STOP_OUTCOME status=stopped trigger=manual executor=proot pid=$pid"
        say "Proot 감시 루프(PID $pid) 종료를 확인했습니다."
      else
        log_control "MONITORING_STOP_OUTCOME status=timeout trigger=manual executor=proot pid=$pid"
        echo "오류: Proot 감시 루프가 종료되지 않았습니다 (PID $pid)." >&2; exit 1
      fi
    else
      rm -f -- "$MONITOR_PID_FILE"
      log_control "MONITORING_STOP_OUTCOME status=already_stopped trigger=manual executor=proot stale_pid_file=1"
      say "감시 루프는 실행 중이 아닙니다."
    fi
  else
    log_control "MONITORING_STOP_OUTCOME status=already_stopped trigger=manual executor=proot pid_file_missing=1"
    say "감시 루프 PID 파일이 없습니다. 이미 중지되었을 수 있습니다."
  fi
fi
say "주의: 매매 프로그램은 중지하지 않았습니다. 매매도 중지하려면 ./scripts/stop.sh 를 별도로 실행하세요."
