#!/usr/bin/env bash
# V7.1.6: validate .env and strategy YAML, then safely restart only an already-running trader.
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"
ACTOR="${SUDO_USER:-$(id -un)}"
MODE="restart-if-running"
case "${1:-}" in
  "") ;;
  --validate-only) MODE="validate-only" ;;
  --start) MODE="start-if-stopped" ;;
  *) echo "사용법: $0 [--validate-only|--start]" >&2; exit 2 ;;
esac
LOG_DIR="$PROJECT_ROOT/logs"
source "$PROJECT_ROOT/scripts/lib/log_control.sh"
log_control() { kis_log_control "$LOG_DIR" "$*"; }
fail() { log_control "RELOAD_OUTCOME status=failed mode=${MODE:-unknown} user=$ACTOR reason=$*"; printf '[오류] %s\n' "$*" >&2; exit 1; }
record_config_snapshot() {
  local snapshot_output snapshot_line
  if snapshot_output="$("$PY" "$PROJECT_ROOT/scripts/config_snapshot.py" 2>&1)"; then
    while IFS= read -r snapshot_line; do [[ -n "$snapshot_line" ]] && log_control "$snapshot_line"; done <<< "$snapshot_output"
  else
    log_control "CONFIG_SNAPSHOT status=failed reason=helper_error detail=${snapshot_output//$'\n'/; }"
    fail "운영 설정 변경 이력을 기록하지 못했습니다. 설정 추적이 확인될 때까지 reload를 중단합니다."
  fi
}
trap 'rc=$?; log_control "RELOAD_OUTCOME status=failed mode=${MODE:-unknown} user=$ACTOR project_root=$PROJECT_ROOT line=${BASH_LINENO[0]:-unknown} exit_code=$rc"; exit "$rc"' ERR
say() { printf '[설정 리로드] %s\n' "$*"; }
[[ -f .env ]] || fail ".env 파일이 없습니다. .env.example을 복사한 뒤 설정하세요."
PY="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
[[ -n "$PY" && -x "$PY" ]] || fail "Python 3 또는 .venv/bin/python을 찾을 수 없습니다."
say ".env 셸 문법 검사"
bash -n .env || fail ".env 문법 오류가 있습니다. 비밀값은 출력하지 않았습니다."
# Syntax-check before sourcing .env, then apply the configured runtime path overrides.
set -a
# shellcheck disable=SC1091
source .env
set +a
LOG_DIR="$($PY -S - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("log_dir"))
PYCODE
)"
START_TRIGGER_FILE="$($PY -S - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("start_trigger"))
PYCODE
)"
MANUAL_STOP_FILE="$($PY -S - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("manual_stop"))
PYCODE
)"
LOCK_FILE="${START_TRIGGER_FILE}.lock"
LOCK_PARENT="$(dirname -- "$LOCK_FILE")"
mkdir -p "$LOG_DIR" "$LOCK_PARENT"
log_control "RELOAD_REQUEST mode=$MODE user=$ACTOR project_root=$PROJECT_ROOT"
say ".env, 진입/청산 전략 YAML 및 strategy_profiles.yaml 구조 검사"
"$PY" - "$PROJECT_ROOT" <<'PYCODE'
import os, sys
from pathlib import Path
from dotenv import dotenv_values
import yaml
root = Path(sys.argv[1])
env_path = root / '.env'
try:
    env = dotenv_values(env_path, encoding='utf-8')
except Exception as exc:
    print(f'.env 파싱 실패: {type(exc).__name__}', file=sys.stderr)
    raise SystemExit(1)
# The active YAML paths are read from .env; fallback to the repository defaults.
mode = str(env.get('STRATEGY_EVALUATION_MODE') or 'ALL').strip().upper()
if mode not in {'KIS10', 'CUSTOM', 'ALL'}:
    print('STRATEGY_EVALUATION_MODE: KIS10, CUSTOM, ALL 중 하나여야 합니다', file=sys.stderr)
    raise SystemExit(1)
paths = {'STRATEGY_PROFILES_FILE': env.get('STRATEGY_PROFILES_FILE') or 'config/strategy_profiles.yaml'}
if mode in {'CUSTOM', 'ALL'}:
    paths['ENTRY_STRATEGY_FILE'] = env.get('ENTRY_STRATEGY_FILE') or 'config/my_entry_strategy.kis.yaml'
    paths['EXIT_STRATEGY_FILE'] = env.get('EXIT_STRATEGY_FILE') or 'config/my_exit_strategy.kis.yaml'
for key, raw_path in paths.items():
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        print(f'{key}: 전략 파일을 찾을 수 없습니다: {path}', file=sys.stderr)
        raise SystemExit(1)
    try:
        with path.open('r', encoding='utf-8') as handle:
            parsed = yaml.safe_load(handle)
    except Exception as exc:
        print(f'{key}: YAML 파싱 실패 ({type(exc).__name__}) - {path}', file=sys.stderr)
        raise SystemExit(1)
    if parsed is not None and not isinstance(parsed, dict):
        print(f'{key}: YAML 최상위 값은 mapping이어야 합니다: {path}', file=sys.stderr)
        raise SystemExit(1)
    if key == 'STRATEGY_PROFILES_FILE':
        if not isinstance(parsed, dict):
            print(f'{key}: 비어 있지 않은 mapping이어야 합니다: {path}', file=sys.stderr)
            raise SystemExit(1)
        if not isinstance(parsed.get('strategies'), list) or not parsed.get('strategies'):
            print(f'{key}: strategies는 비어 있지 않은 목록이어야 합니다: {path}', file=sys.stderr)
            raise SystemExit(1)
        if not isinstance(parsed.get('decision'), dict):
            print(f'{key}: decision mapping이 필요합니다: {path}', file=sys.stderr)
            raise SystemExit(1)
        for side in ('buy', 'sell'):
            section = parsed['decision'].get(side)
            if not isinstance(section, dict) or not isinstance(section.get('strategies'), list) or not section['strategies']:
                print(f'{key}: decision.{side}.strategies는 비어 있지 않은 목록이어야 합니다: {path}', file=sys.stderr)
                raise SystemExit(1)
        ids = [item.get('id') for item in parsed['strategies'] if isinstance(item, dict)]
        if len(ids) != len(parsed['strategies']) or len(set(ids)) != len(ids):
            print(f'{key}: strategies 항목의 id가 중복되거나 잘못되었습니다: {path}', file=sys.stderr)
            raise SystemExit(1)
        known = set(ids)
        for side in ('buy', 'sell'):
            for item in parsed['decision'][side]['strategies']:
                if not isinstance(item, dict) or item.get('id') not in known:
                    print(f'{key}: decision.{side}.strategies에 등록되지 않은 ID가 있습니다: {path}', file=sys.stderr)
                    raise SystemExit(1)
    print(f'{key}: OK ({path.relative_to(root) if path.is_relative_to(root) else path})')
print('.env: 파싱 OK (비밀값 미출력)')
PYCODE

if [[ "$MODE" == "validate-only" ]]; then
  say "검증만 완료했습니다. 프로세스는 변경하지 않았습니다."
  log_control "RELOAD_OUTCOME status=validated mode=validate-only user=$ACTOR"
  exit 0
fi
write_start_trigger() {
  "$PY" -S - "$PROJECT_ROOT" <<'PYCODE'
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
path=runtime_path('start_trigger'); path.parent.mkdir(parents=True,exist_ok=True)
fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=str(path.parent))
try:
    with os.fdopen(fd,'w',encoding='utf-8') as f: f.write('reload_config\n'); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,0o644); os.replace(tmp,path)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PYCODE
}
clear_start_trigger() {
  "$PY" -S - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
runtime_path('start_trigger').unlink(missing_ok=True)
PYCODE
}
clear_manual_stop_marker() {
  "$PY" -S - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
path = runtime_path('manual_stop')
if path.exists():
    path.unlink()
    print('cleared')
else:
    print('absent')
PYCODE
}
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
  exec 9>"$LOCK_FILE"
  flock -x 9 || fail "시작/정지 잠금을 얻지 못했습니다."
  log_control "START_STOP_LOCK action=acquired trigger=reload_config mode=$MODE user=$ACTOR"
}
acquire_start_stop_lock
# Default/restart mode must not override a same-day manual stop. --start is the
# explicit operator resume path and may clear the marker while holding the shared lock.
if [[ "$MODE" != "start-if-stopped" && -f "$MANUAL_STOP_FILE" ]]; then
  today="$(TZ=Asia/Seoul date +%Y%m%d)"
  marker_status="$("$PY" - "$MANUAL_STOP_FILE" "$today" <<'PYCODE'
import json,re,sys
from pathlib import Path
try:
    data=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    marker_date=data.get("date") if isinstance(data,dict) else None
    if not isinstance(marker_date,str) or not re.fullmatch(r"\d{8}",marker_date): raise ValueError("invalid date")
    if marker_date == sys.argv[2]: print("TODAY")
    elif marker_date < sys.argv[2]: print("EXPIRED")
    else: print("FUTURE")
except Exception: print("INVALID")
PYCODE
)"
  case "$marker_status" in
    TODAY)
      log_control "RELOAD_OUTCOME status=blocked_manual_stop mode=$MODE date=$today reason=explicit_start_required user=$ACTOR"
      say "오늘 수동 정지 표식이 있어 재시작하지 않았습니다. 명시적으로 재개하려면 --start를 사용하세요."
      exit 0
      ;;
    EXPIRED)
      rm -f -- "$MANUAL_STOP_FILE" || fail "만료된 수동 정지 표식을 제거하지 못했습니다."
      log_control "MANUAL_STOP_MARKER action=expired trigger=reload_config user=$ACTOR marker_date=before_$today"
      ;;
    *) fail "수동 정지 표식이 손상되었거나 미래 날짜입니다. 안전을 위해 자동 재시작을 중단합니다." ;;
  esac
fi
if [[ "$MODE" == "start-if-stopped" ]]; then
  marker_action="$(clear_manual_stop_marker)" || fail "수동 정지 표식을 확인/해제하지 못했습니다. 안전을 위해 시작을 중단합니다."
  log_control "MANUAL_STOP_MARKER action=$marker_action trigger=reload_config explicit_start=1 user=$ACTOR"
fi
PID1_COMM="$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ' || true)"
if [[ "$PID1_COMM" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then
  if [[ "$EUID" -eq 0 ]]; then SUDO=(); else command -v sudo >/dev/null 2>&1 || fail "systemd 서비스 변경에 sudo/root 권한이 필요합니다."; SUDO=(sudo); fi
  if "${SUDO[@]}" systemctl is-active --quiet kis-auto-trader.service; then
    record_config_snapshot
    say "실행 중인 systemd 서비스를 재시작합니다 (.env/YAML 검증 완료)."
    log_control "RELOAD_ACTION action=restart trigger=reload_config service=systemd"
    write_start_trigger
    if ! "${SUDO[@]}" systemctl restart kis-auto-trader.service; then clear_start_trigger; fail "systemd 재시작 요청이 실패했습니다."; fi
  elif [[ "$MODE" == "start-if-stopped" ]]; then
    record_config_snapshot
    say "중지된 systemd 서비스를 시작합니다 (--start 명시됨)."
    log_control "RELOAD_ACTION action=start trigger=reload_config service=systemd explicit=1"
    write_start_trigger
    if ! "${SUDO[@]}" systemctl start kis-auto-trader.service; then clear_start_trigger; fail "systemd 시작 요청이 실패했습니다."; fi
  else
    say "설정 검증 완료. 서비스가 중지 상태여서 자동 시작하지 않았습니다. 시작하려면 --start를 지정하세요."
    log_control "RELOAD_OUTCOME status=validated_no_start mode=$MODE reason=service_stopped"
    exit 0
  fi
  sleep 2
  "${SUDO[@]}" systemctl is-active --quiet kis-auto-trader.service || fail "서비스가 active 상태가 아닙니다. journalctl -u kis-auto-trader.service 확인이 필요합니다."
  say "systemd 서비스 재시작/시작 확인 완료."
  log_control "RELOAD_OUTCOME status=active mode=$MODE service=systemd"
  exit 0
fi

# Proot/non-systemd fallback: only restart a process already running unless --start was requested.
if pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then
  say "기존 프로세스에 종료 신호를 보냅니다."
  log_control "RELOAD_ACTION action=stop trigger=reload_config service=proot"
  "$PROJECT_ROOT/scripts/stop.sh" --trigger reload_config
  for _ in $(seq 1 5); do
    pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1 || break
    sleep 1
  done
  pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1 && fail "기존 프로세스가 종료되지 않았습니다. 중복 실행을 방지하기 위해 시작하지 않습니다."
elif [[ "$MODE" != "start-if-stopped" ]]; then
  say "설정 검증 완료. 실행 중인 프로세스가 없어 자동 시작하지 않았습니다. 시작하려면 --start를 지정하세요."
  log_control "RELOAD_OUTCOME status=validated_no_start mode=$MODE reason=process_stopped service=proot"
  exit 0
fi
record_config_snapshot
say "프로세스를 시작합니다."
log_control "RELOAD_ACTION action=start trigger=reload_config service=proot"
KIS_START_LOCK_HELD=1 nohup "$PROJECT_ROOT/scripts/start.sh" --trigger reload_config >> "$LOG_DIR/reload_config.log" 2>&1 </dev/null &
for _ in $(seq 1 30); do
  if [[ -f "$MANUAL_STOP_FILE" ]]; then
    log_control "RELOAD_OUTCOME status=cancelled mode=$MODE reason=concurrent_manual_stop marker=$MANUAL_STOP_FILE"
    say "시작 확인 중 수동 정지 요청이 들어와 정지 상태를 유지합니다."
    exit 0
  fi
  if pgrep -f "[p]ython.*$PROJECT_ROOT/main\.py" >/dev/null 2>&1; then
    if [[ -f "$MANUAL_STOP_FILE" ]]; then
      log_control "RELOAD_OUTCOME status=cancelled mode=$MODE reason=concurrent_manual_stop marker=$MANUAL_STOP_FILE"
      say "시작 확인 중 수동 정지 요청이 들어와 정지 상태를 유지합니다."
      exit 0
    fi
    say "프로세스 시작 확인 완료."
    log_control "RELOAD_OUTCOME status=running mode=$MODE service=proot"
    exit 0
  fi
  sleep 1
done
if [[ -f "$MANUAL_STOP_FILE" ]]; then
  log_control "RELOAD_OUTCOME status=cancelled mode=$MODE reason=concurrent_manual_stop marker=$MANUAL_STOP_FILE"
  say "시작 확인 중 수동 정지 요청이 들어와 정지 상태를 유지합니다."
  exit 0
fi
fail "시작 후 프로세스를 확인하지 못했습니다. $LOG_DIR/reload_config.log와 trader 로그를 확인하세요."
