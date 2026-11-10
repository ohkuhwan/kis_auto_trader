#!/usr/bin/env bash
# V7.1.6: install persistent timers; watchdog asks systemd to start the trader service.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
ACTOR="${SUDO_USER:-$(id -un)}"
LOG_DIR="$PROJECT_ROOT/logs"
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log_control() { kis_log_control "$LOG_DIR" "$*"; }
say() { printf '[모니터링 설치] %s\n' "$*"; }
fail() { log_control "MONITORING_OUTCOME status=failed trigger=installer user=$ACTOR project_root=$PROJECT_ROOT reason=$*"; printf '[오류] %s\n' "$*" >&2; exit 1; }

say "프로젝트 경로: $PROJECT_ROOT"
say "사용자: $(id -un), KST: $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S %Z')"
[[ -f .env ]] || fail ".env가 없습니다. cp .env.example .env 실행 후 API/계좌 설정을 완료하세요."
set -a
# shellcheck disable=SC1091
source .env
set +a
command -v python3 >/dev/null 2>&1 || fail "python3가 없습니다. Ubuntu 패키지 관리자로 python3/python3-venv/python3-pip를 설치하세요."
LOG_DIR="$(python3 - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("log_dir"))
PYCODE
)"
MONITOR_PID_FILE="$(python3 - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitor_pid"))
PYCODE
)"
MONITORING_DISABLED_FILE="$(python3 - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitoring_disabled"))
PYCODE
)"
START_TRIGGER_FILE="$(python3 - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("start_trigger"))
PYCODE
)"
LOCK_FILE="${START_TRIGGER_FILE}.lock"
LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
mkdir -p "$LOG_DIR" "$(dirname -- "$MONITOR_PID_FILE")" "$(dirname -- "$MONITORING_DISABLED_FILE")" "$LOCK_PARENT"
acquire_start_stop_lock() {
  command -v flock >/dev/null 2>&1 || fail "flock 명령이 필요합니다."
  [[ -e "$LOCK_FILE" ]] || ( umask 0007; : > "$LOCK_FILE" )
  if [[ "$EUID" -eq 0 ]]; then
    local lock_group
    lock_group="$(stat -c '%g' "$LOCK_PARENT")"
    chgrp "$lock_group" "$LOCK_FILE" && chmod 0660 "$LOCK_FILE" || fail "시작/정지 잠금 권한을 설정하지 못했습니다."
  elif ! [[ -w "$LOCK_FILE" ]]; then
    if command -v sudo >/dev/null 2>&1; then
      local lock_group
      lock_group="$(stat -c '%g' "$LOCK_PARENT")"
      sudo chown "$ACTOR:$lock_group" "$LOCK_FILE" && sudo chmod 0660 "$LOCK_FILE" || fail "시작/정지 잠금 권한을 설정하지 못했습니다."
    else
      fail "시작/정지 잠금 파일에 쓸 수 없습니다."
    fi
  else
    chmod 0660 "$LOCK_FILE" || fail "시작/정지 잠금 권한을 설정하지 못했습니다."
  fi
  exec 7>"$LOCK_FILE"
  flock -x 7 || fail "시작/정지 잠금을 얻지 못했습니다."
  log_control "START_STOP_LOCK action=acquired trigger=installer user=$ACTOR"
}
release_start_stop_lock() {
  flock -u 7 || fail "시작/정지 잠금을 해제하지 못했습니다."
  exec 7>&-
  log_control "START_STOP_LOCK action=released trigger=installer user=$ACTOR"
}
clear_monitoring_disabled_marker() {
  if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
    rm -f -- "$MONITORING_DISABLED_FILE" || fail "감시 중지 표식을 제거하지 못했습니다: $MONITORING_DISABLED_FILE"
    log_control "MONITORING_DISABLED_MARKER action=cleared trigger=installer user=$ACTOR file=$MONITORING_DISABLED_FILE"
  else
    log_control "MONITORING_DISABLED_MARKER action=absent trigger=installer user=$ACTOR file=$MONITORING_DISABLED_FILE"
  fi
}
write_monitoring_transition_marker() {
  if ! python3 - "$MONITORING_DISABLED_FILE" <<'PYCODE'
import getpass, json, os, sys, tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
path=Path(sys.argv[1]); path.parent.mkdir(parents=True,exist_ok=True)
now=datetime.now(ZoneInfo("Asia/Seoul"))
data={"disabled":True,"date":now.strftime("%Y%m%d"),"timestamp":now.isoformat(timespec="seconds"),"trigger":"installer","reason":"monitoring_start_transition","user":os.getenv("SUDO_USER") or getpass.getuser()}
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
    fail "감시 시작 전 fail-closed 표식을 기록하지 못했습니다. 타이머를 활성화하지 않습니다."
  fi
  log_control "MONITORING_DISABLED_MARKER action=created trigger=installer reason=monitoring_start_transition user=$ACTOR file=$MONITORING_DISABLED_FILE"
}
ensure_watchdog_recovery_policy() {
  local previous effective policy_tmp
  previous="$($SUDO systemctl show --property=Restart --value kis-auto-trader.service 2>/dev/null || true)"
  if [[ "$previous" != "no" ]]; then
    local dropin_dir="/etc/systemd/system/kis-auto-trader.service.d"
    policy_tmp="$(mktemp)"
    printf '[Service]\nRestart=no\n' > "$policy_tmp"
    if ! $SUDO install -d -m 0755 "$dropin_dir" || ! $SUDO install -m 0644 "$policy_tmp" "$dropin_dir/10-watchdog-recovery.conf"; then
      rm -f -- "$policy_tmp"
      fail "watchdog 복구 정책 override 설치 실패. 매매 서비스는 중지하지 않았습니다."
    fi
    rm -f -- "$policy_tmp"
    $SUDO systemctl daemon-reload || fail "watchdog 복구 정책 적용을 위한 daemon-reload 실패. 매매 서비스는 중지하지 않았습니다."
  fi
  effective="$($SUDO systemctl show --property=Restart --value kis-auto-trader.service 2>/dev/null || true)"
  [[ "$effective" == "no" ]] || fail "Restart=no 정책 적용을 확인하지 못했습니다. 안전을 위해 타이머 재개를 중단합니다."
  log_control "SYSTEMD_POLICY action=watchdog_recovery_only previous_restart=${previous:-unknown} effective_restart=$effective active_process_restarted=0"
}
log_control "MONITORING_REQUEST trigger=installer user=$ACTOR project_root=$PROJECT_ROOT pid=$$"
trap 'rc=$?; log_control "MONITORING_OUTCOME status=failed trigger=installer user=$ACTOR project_root=$PROJECT_ROOT line=${BASH_LINENO[0]:-unknown} exit_code=$rc"; exit "$rc"' ERR
command -v bash >/dev/null 2>&1 || fail "bash를 찾을 수 없습니다."

if [[ ! -x .venv/bin/python ]]; then
  say "Python 가상환경이 없어 생성합니다."
  python3 -m venv .venv || fail "가상환경 생성 실패. Proot에서 python3-venv 패키지가 설치 가능한지 확인하세요."
fi
if ! .venv/bin/python -c 'import dotenv, requests, yaml, websockets, Crypto' >/dev/null 2>&1; then
  say "필수 Python 패키지 누락 감지: requirements.txt 설치를 시도합니다."
  .venv/bin/python -m pip install --upgrade pip || fail "pip 업그레이드 실패. 네트워크/인증서/패키지 저장소를 확인하세요."
  .venv/bin/python -m pip install -r requirements.txt || fail "requirements.txt 설치 실패. 오류 로그를 확인하세요."
fi
.venv/bin/python -c 'import dotenv, requests, yaml, websockets, Crypto' >/dev/null 2>&1 || fail "필수 패키지 import 검증 실패."
chmod +x scripts/start.sh scripts/stop.sh scripts/request_start.sh monitoring/watchdog.sh monitoring/monitor_loop.sh scripts/start_monitoring.sh scripts/stop_monitoring.sh

# IMPORTANT: do not exit merely because AUTO_SHUTDOWN_TIME has passed.
# The scheduler must still be installed/enabled so it can check again on the next business day.
# watchdog.sh refuses restarts after cutoff; main.py performs its own safe shutdown.
cutoff="$(sed -n 's/^AUTO_SHUTDOWN_TIME=//p' .env | tail -n 1 | tr -d '\r"' | cut -d '#' -f1 | xargs || true)"
[[ "$cutoff" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || cutoff="15:10"
now="$(TZ=Asia/Seoul date +%H:%M)"
dow="$(TZ=Asia/Seoul date +%u)"

PID1_COMM="$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ' || true)"
HAS_SYSTEMD=0
if [[ "$PID1_COMM" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then HAS_SYSTEMD=1; fi
if [[ "$HAS_SYSTEMD" -eq 1 ]]; then
  if [[ "$EUID" -eq 0 ]]; then SUDO=""; else command -v sudo >/dev/null 2>&1 || fail "root 권한 또는 sudo가 필요합니다."; SUDO="sudo"; fi
  # If this installation's trader is already active, resume monitoring without replacing live unit files.
  if $SUDO systemctl is-active --quiet kis-auto-trader.service; then
    running_main="$(pgrep -af '[p]ython.*main\.py' || true)"
    other_main="$(printf '%s\n' "$running_main" | grep -Fv "$PROJECT_ROOT/main.py" || true)"
    [[ -z "$other_main" ]] || fail "현재 systemd 서비스 외에 다른 설치의 Python main.py 프로세스가 있습니다. 타이머 재개를 중단합니다: $other_main"
    existing_workdir="$($SUDO systemctl show --property=WorkingDirectory --value kis-auto-trader.service 2>/dev/null || true)"
    existing_trader_exec="$($SUDO systemctl show --property=ExecStart --value kis-auto-trader.service 2>/dev/null || true)"
    [[ "$existing_workdir" == "$PROJECT_ROOT" && "$existing_trader_exec" == *"$PROJECT_ROOT/scripts/start.sh"* ]] || fail "kis-auto-trader.service는 실행 중이지만 WorkingDirectory/ExecStart가 현재 프로젝트와 다릅니다. 다른 설치의 서비스를 변경하지 않습니다."
    start_exec="$($SUDO systemctl show --property=ExecStart --value kis-auto-trader-start.service 2>/dev/null || true)"
    watchdog_exec="$($SUDO systemctl show --property=ExecStart --value kis-auto-trader-watchdog.service 2>/dev/null || true)"
    [[ "$start_exec" == *"$PROJECT_ROOT/monitoring/watchdog.sh"* ]] || fail "기존 예약 시작 unit이 현재 프로젝트를 가리키지 않거나 없습니다. 매매 서비스는 변경하지 않았습니다."
    [[ "$watchdog_exec" == *"$PROJECT_ROOT/monitoring/watchdog.sh"* ]] || fail "기존 watchdog unit이 현재 프로젝트를 가리키지 않거나 없습니다. 매매 서비스는 변경하지 않았습니다."
    # Keep systemd from bypassing watchdog manual-stop/cutoff/holiday gates.
    ensure_watchdog_recovery_policy
    acquire_start_stop_lock
    write_monitoring_transition_marker
    $SUDO systemctl enable kis-auto-trader-start.timer kis-auto-trader-watchdog.timer
    $SUDO systemctl start kis-auto-trader-start.timer kis-auto-trader-watchdog.timer
    $SUDO systemctl is-active --quiet kis-auto-trader-start.timer || fail "예약 시작 타이머가 active 상태가 아닙니다."
    $SUDO systemctl is-active --quiet kis-auto-trader-watchdog.timer || fail "watchdog 타이머가 active 상태가 아닙니다."
    clear_monitoring_disabled_marker
    release_start_stop_lock
    if [[ "$dow" -le 5 && "$now" < "$cutoff" ]]; then
      if $SUDO "$PROJECT_ROOT/monitoring/watchdog.sh" --trigger installer; then
        log_control "MONITORING_ACTION action=resume_check trigger=installer outcome=success executor=systemd"
      else
        rc=$?
        log_control "MONITORING_OUTCOME status=resume_check_failed trigger=installer executor=systemd exit_code=$rc"
        fail "감시 재개 후 watchdog 확인이 실패했습니다(exit=$rc)."
      fi
    fi
    if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
      log_control "MONITORING_OUTCOME status=cancelled trigger=installer executor=systemd reason=concurrent_monitoring_stop marker=$MONITORING_DISABLED_FILE"
      say "감시 재개 중 수동 감시 중지 요청이 들어와 중지 상태를 유지합니다."
      exit 0
    fi
    $SUDO systemctl is-active --quiet kis-auto-trader-start.timer || fail "재개 확인 후 예약 시작 타이머가 더 이상 active 상태가 아닙니다."
    $SUDO systemctl is-active --quiet kis-auto-trader-watchdog.timer || fail "재개 확인 후 watchdog 타이머가 더 이상 active 상태가 아닙니다."
    log_control "MONITORING_OUTCOME status=resumed trigger=installer executor=systemd user=$ACTOR service_already_active=1"
    say "기존 매매 서비스는 유지하고 시작/watchdog timer만 재개했습니다."
    exit 0
  fi
  say "systemd 환경 확인. 현재 경로를 반영해 unit 파일을 생성합니다."
  log_control "MONITORING_ACTION action=install_units trigger=installer executor=systemd user=$ACTOR"
  UNIT_DIR="/etc/systemd/system"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  USER_NAME="${SUDO_USER:-$(id -un)}"
  cat > "$TMP_DIR/kis-auto-trader.service" <<UNIT
[Unit]
Description=KIS Auto Trader V7.1.6
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$PROJECT_ROOT
Environment=KIS_SYSTEMD_SERVICE=1
ExecStart=$PROJECT_ROOT/scripts/start.sh
ExecStop=$PROJECT_ROOT/scripts/stop.sh --trigger systemd
# Recovery is delegated to watchdog.sh, which enforces manual-stop, KST cutoff,
# weekday and holiday rules before requesting a new start.
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=300
[Install]
WantedBy=multi-user.target
UNIT
  cat > "$TMP_DIR/kis-auto-trader-start.service" <<UNIT
[Unit]
Description=Start KIS Auto Trader V7.1.6 on schedule
[Service]
Type=oneshot
ExecStart=$PROJECT_ROOT/monitoring/watchdog.sh --trigger scheduler
UNIT
  cat > "$TMP_DIR/kis-auto-trader-start.timer" <<'UNIT'
[Unit]
Description=Start KIS Auto Trader V7.1.6 every weekday at 08:00 KST
[Timer]
OnCalendar=Mon..Fri *-*-* 08:00:00 Asia/Seoul
Persistent=true
Unit=kis-auto-trader-start.service
[Install]
WantedBy=timers.target
UNIT
  cat > "$TMP_DIR/kis-auto-trader-watchdog.service" <<UNIT
[Unit]
Description=KIS Auto Trader V7.1.6 watchdog check
[Service]
Type=oneshot
# This unit must be root so it can start the system-level trader service.
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PROJECT_ROOT/monitoring/watchdog.sh
UNIT
  cat > "$TMP_DIR/kis-auto-trader-watchdog.timer" <<'UNIT'
[Unit]
Description=Check KIS Auto Trader V7.1.6 service every minute; remain active after cutoff
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Unit=kis-auto-trader-watchdog.service
[Install]
WantedBy=timers.target
UNIT
  # Check both trader units before changing any legacy timer/unit state.
  # An active legacy trader is left untouched and its timers are not disabled.
  if $SUDO systemctl is-active --quiet kis-auto-trader.service; then
    fail "새 매매 서비스가 실행 중입니다. 안전한 시간에 정상 종료한 뒤 다시 설치하세요."
  fi
  if $SUDO systemctl is-active --quiet kis-auto-trader-v5.service; then
    fail "기존 kis-auto-trader-v5.service가 실행 중입니다. 기존 서비스와 타이머는 변경하지 않았습니다. ./scripts/stop.sh를 통해 매매를 정상 종료한 뒤 다시 설치하세요."
  fi
  # Do not change legacy timers if any manually launched or differently named trader is present.
  running_main="$(pgrep -af '[p]ython.*main\.py' || true)"
  if [[ -n "$running_main" ]]; then
    fail "다른 설치 또는 수동 실행의 Python main.py 프로세스가 있습니다. 기존 unit/timer를 변경하지 않았습니다: $running_main"
  fi
  # Disable only legacy units that are actually installed. Do not hide a failed
  # disable/stop: removing the files while a timer remains active could schedule
  # an old start after the new service is installed.
  for unit in kis-auto-trader-v5-start.timer kis-auto-trader-v5-watchdog.timer kis-auto-trader-v5-monitor-stop.timer; do
    load_state="$($SUDO systemctl show --property=LoadState --value "$unit" 2>/dev/null || true)"
    if [[ "$load_state" == "loaded" ]]; then
      $SUDO systemctl disable --now "$unit" || fail "기존 타이머 비활성화에 실패했습니다: $unit. 기존 unit 파일은 아직 제거하지 않았습니다."
      if $SUDO systemctl is-active --quiet "$unit"; then fail "기존 타이머가 여전히 active 상태입니다: $unit"; fi
    fi
  done
  for unit in kis-auto-trader-v5-watchdog.service kis-auto-trader-v5-start.service kis-auto-trader-v5-monitor-stop.service; do
    load_state="$($SUDO systemctl show --property=LoadState --value "$unit" 2>/dev/null || true)"
    if [[ "$load_state" == "loaded" ]]; then
      $SUDO systemctl stop "$unit" || fail "기존 oneshot 감시 작업 중지에 실패했습니다: $unit. 기존 unit 파일은 아직 제거하지 않았습니다."
      if $SUDO systemctl is-active --quiet "$unit"; then fail "기존 oneshot 감시 작업이 여전히 active 상태입니다: $unit"; fi
    fi
  done
  load_state="$($SUDO systemctl show --property=LoadState --value kis-auto-trader-v5.service 2>/dev/null || true)"
  if [[ "$load_state" == "loaded" ]]; then
    $SUDO systemctl disable kis-auto-trader-v5.service || fail "기존 매매 서비스 비활성화에 실패했습니다. 기존 unit 파일은 아직 제거하지 않았습니다."
  fi
  # Re-check after disabling old timers; never terminate the legacy trader automatically.
  if $SUDO systemctl is-active --quiet kis-auto-trader-v5.service; then
    fail "전환 도중 기존 kis-auto-trader-v5.service가 활성화되었습니다. 기존 서비스는 종료하지 않았으며 unit 전환을 중단했습니다."
  fi
  running_main="$(pgrep -af '[p]ython.*main\.py' || true)"
  if [[ -n "$running_main" ]]; then
    fail "전환 중 Python main.py 프로세스가 감지되어 unit 이름 이전을 중단합니다. 매매 프로세스는 종료하지 않았습니다: $running_main"
  fi
  $SUDO rm -f "$UNIT_DIR/kis-auto-trader-v5.service" "$UNIT_DIR/kis-auto-trader-v5-start.service" "$UNIT_DIR/kis-auto-trader-v5-start.timer" "$UNIT_DIR/kis-auto-trader-v5-watchdog.service" "$UNIT_DIR/kis-auto-trader-v5-watchdog.timer" "$UNIT_DIR/kis-auto-trader-v5-monitor-stop.service" "$UNIT_DIR/kis-auto-trader-v5-monitor-stop.timer"
  $SUDO install -m 0644 "$TMP_DIR"/*.service "$TMP_DIR"/*.timer "$UNIT_DIR/"
  $SUDO systemctl daemon-reload
  ensure_watchdog_recovery_policy
  if command -v systemd-analyze >/dev/null 2>&1; then
    $SUDO systemd-analyze verify "$UNIT_DIR/kis-auto-trader.service" "$UNIT_DIR/kis-auto-trader-start.service" "$UNIT_DIR/kis-auto-trader-watchdog.service" "$UNIT_DIR/kis-auto-trader-start.timer" "$UNIT_DIR/kis-auto-trader-watchdog.timer"
  fi
  acquire_start_stop_lock
  write_monitoring_transition_marker
  $SUDO systemctl enable kis-auto-trader-start.timer kis-auto-trader-watchdog.timer
  $SUDO systemctl start kis-auto-trader-start.timer kis-auto-trader-watchdog.timer
  $SUDO systemctl is-active --quiet kis-auto-trader-start.timer || fail "예약 시작 타이머가 active 상태가 아닙니다."
  $SUDO systemctl is-active --quiet kis-auto-trader-watchdog.timer || fail "watchdog 타이머가 active 상태가 아닙니다."
  log_control "MONITORING_OUTCOME status=timers_active trigger=installer executor=systemd units=kis-auto-trader-start.timer,kis-auto-trader-watchdog.timer"
  # Clear suppression only after timer installation succeeds; explicit installer action resumes monitoring.
  clear_monitoring_disabled_marker
  release_start_stop_lock
  # Install/enable timers even after cutoff. Only start the app immediately during the pre-cutoff weekday window.
  if [[ "$dow" -le 5 && ( "$now" < "$cutoff" ) ]]; then
    if $SUDO "$PROJECT_ROOT/monitoring/watchdog.sh" --trigger installer; then
      log_control "MONITORING_ACTION action=initial_check trigger=installer outcome=success executor=systemd"
    else
      rc=$?
      log_control "MONITORING_OUTCOME status=initial_check_failed trigger=installer executor=systemd exit_code=$rc"
      fail "초기 watchdog 확인 실패(exit=$rc). 타이머는 활성 상태로 유지했으므로 상태를 확인하세요."
    fi
    say "systemd service 시작 확인을 요청했고, 시작/watchdog timer는 계속 활성화되어 있습니다."
  else
    say "현재 KST $now / 요일 $dow: 즉시 매매 프로세스는 시작하지 않았습니다. 예약 타이머는 계속 활성화되어 다음 평일 08:00에 다시 확인합니다."
  fi
  if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
    log_control "MONITORING_OUTCOME status=cancelled trigger=installer executor=systemd reason=concurrent_monitoring_stop marker=$MONITORING_DISABLED_FILE"
    say "감시 설치 중 수동 감시 중지 요청이 들어와 중지 상태를 유지합니다."
    exit 0
  fi
  $SUDO systemctl is-active --quiet kis-auto-trader-start.timer || fail "최종 확인에서 예약 시작 타이머가 active 상태가 아닙니다."
  $SUDO systemctl is-active --quiet kis-auto-trader-watchdog.timer || fail "최종 확인에서 watchdog 타이머가 active 상태가 아닙니다."
  log_control "MONITORING_OUTCOME status=ready trigger=installer executor=systemd user=$ACTOR"
  say "상태 확인: systemctl list-timers --all | grep kis-auto-trader"
else
  say "systemd PID 1을 찾지 못했습니다 ($(ps -p 1 -o comm= 2>/dev/null | xargs || echo unknown)). Proot/컨테이너 방식으로 실행합니다."
  # A different installation's main.py must not be left running during migration.
  running_main="$(pgrep -af '[p]ython.*main\.py' || true)"
  other_main="$(printf '%s\n' "$running_main" | grep -Fv "$PROJECT_ROOT/main.py" || true)"
  if [[ -n "$other_main" ]]; then
    fail "다른 설치의 Python main.py 프로세스가 있습니다. 중복 실행을 막기 위해 모니터링 설치를 중단합니다. 먼저 해당 설치를 정상 종료하세요: $other_main"
  fi
  # Explicitly starting monitoring clears the persistent suppression marker under the shared start/stop lock.
  acquire_start_stop_lock
  clear_monitoring_disabled_marker
  release_start_stop_lock
  # Watchdog applies weekday/cutoff rules; the monitor loop remains alive after cutoff
  # so it can check again on the next business day.
  INITIAL_WATCHDOG_RC=0
  if "$PROJECT_ROOT/monitoring/watchdog.sh" --trigger installer; then
    log_control "MONITORING_ACTION action=initial_check trigger=installer outcome=success executor=proot"
  else
    INITIAL_WATCHDOG_RC=$?
    log_control "MONITORING_OUTCOME status=initial_check_failed trigger=installer executor=proot exit_code=$INITIAL_WATCHDOG_RC retry_loop=will_start"
    say "초기 watchdog 확인은 실패했지만 Proot 감시 루프는 시작해 주기적으로 재확인합니다. 상세 원인은 watchdog 로그를 확인하세요."
  fi
  monitor_is_running() {
    [[ "${monitor_pid:-}" =~ ^[0-9]+$ ]] || return 1
    local args state
    args="$(ps -p "$monitor_pid" -o args= 2>/dev/null || true)"
    state="$(ps -p "$monitor_pid" -o stat= 2>/dev/null || true)"
    [[ -n "$args" && "$state" != Z* ]] && grep -Fq "$PROJECT_ROOT/monitoring/monitor_loop.sh" <<< "$args"
  }
  if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
    log_control "MONITORING_OUTCOME status=cancelled trigger=installer executor=proot reason=concurrent_monitoring_stop marker=$MONITORING_DISABLED_FILE"
    say "감시 재개 중 수동 중지 요청이 들어와 Proot 감시 루프를 시작하지 않았습니다."
    exit 0
  fi
  monitor_pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
  if monitor_is_running; then
    say "Proot 감시 루프가 이미 실행 중입니다 (PID $monitor_pid)."
    log_control "MONITORING_OUTCOME status=already_running trigger=installer executor=proot pid=$monitor_pid"
  else
    rm -f -- "$MONITOR_PID_FILE"
    nohup "$PROJECT_ROOT/monitoring/monitor_loop.sh" 7>&- >> "$LOG_DIR/monitor_loop_launcher.log" 2>&1 </dev/null &
    launch_pid=$!
    for _ in $(seq 1 5); do
      monitor_pid="$(cat "$MONITOR_PID_FILE" 2>/dev/null || true)"
      if monitor_is_running; then break; fi
      sleep 1
    done
    if ! monitor_is_running; then
      log_control "MONITORING_OUTCOME status=failed trigger=installer executor=proot launcher_pid=$launch_pid reason=monitor_loop_not_confirmed"
      fail "Proot 감시 루프 PID를 확인하지 못했습니다. $LOG_DIR/monitor_loop_launcher.log를 확인하세요."
    fi
    if [[ -f "$MONITORING_DISABLED_FILE" ]]; then
      kill -TERM "$monitor_pid" 2>/dev/null || true
      log_control "MONITORING_OUTCOME status=cancelled trigger=installer executor=proot pid=$monitor_pid reason=concurrent_monitoring_stop_after_launch"
      exit 0
    fi
    if [[ "$INITIAL_WATCHDOG_RC" -eq 0 ]]; then
      log_control "MONITORING_OUTCOME status=started trigger=installer executor=proot pid=$monitor_pid launcher_pid=$launch_pid"
    else
      log_control "MONITORING_OUTCOME status=started_with_initial_check_failure trigger=installer executor=proot pid=$monitor_pid launcher_pid=$launch_pid initial_exit_code=$INITIAL_WATCHDOG_RC retry_loop=active"
      say "Proot 감시 루프는 시작했지만 초기 확인은 실패했습니다. 감시 로그에서 원인을 확인하세요."
    fi
    say "Proot 감시 루프 시작 확인 완료 (PID $monitor_pid). 로그: $LOG_DIR/monitoring_YYYYMMDD.log"
  fi
fi
