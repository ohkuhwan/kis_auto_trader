#!/usr/bin/env bash
# 최초 설치/업데이트 보조 스크립트.
# 셸 권한 설정 → Python/venv/의존성 설치 → 전체 로컬 검증 → 모니터링 시작.
# .env와 상태/로그 파일은 자동 덮어쓰거나 삭제하지 않습니다.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"

say()  { printf '\n[설치] %s\n' "$*"; }
warn() { printf '[주의] %s\n' "$*" >&2; }
fail() { printf '[오류] %s\n' "$*" >&2; exit 1; }

# 프로젝트가 관리하는 모든 .sh 파일에 실행 권한을 부여합니다.
# .venv/.git 등 생성·외부 디렉터리는 건드리지 않고 scripts/monitoring/tests를 포함합니다.
say "프로젝트 셸 스크립트 실행 권한 설정"
while IFS= read -r -d '' shell_file; do chmod u+x,go+rx "$shell_file"; done < <(find "$PROJECT_ROOT" \
  -path "$PROJECT_ROOT/.venv" -prune -o \
  -path "$PROJECT_ROOT/.git" -prune -o \
  -name "__pycache__" -type d -prune -o \
  -type f -name '*.sh' -print0)


# setup.sh 자체가 실행 권한 없이 호출된 경우도 `bash scripts/setup.sh`로 실행할 수 있습니다.
# 운영 로그 디렉터리는 .env의 KIS_LOG_DIR을 해석한 뒤 필요한 실행기가 생성합니다.
say "프로젝트 경로: $PROJECT_ROOT"
say "실행 사용자: $(id -un), 환경: $(if [[ -f /proc/1/comm ]] && grep -qi systemd /proc/1/comm 2>/dev/null; then echo systemd; else echo Ubuntu/Proot/컨테이너 또는 비-systemd; fi)"

install_python_if_missing() {
  if command -v python3 >/dev/null 2>&1; then return 0; fi
  say "python3가 없어 설치를 시도합니다."
  command -v apt-get >/dev/null 2>&1 || fail "python3가 없습니다. Ubuntu/Debian에서는 apt-get을, 다른 OS에서는 해당 패키지 관리자를 이용해 Python 3.10 이상을 설치하세요."
  if [[ "$EUID" -eq 0 ]]; then
    APT=(apt-get)
  elif command -v sudo >/dev/null 2>&1; then
    APT=(sudo apt-get)
  else
    fail "Python 설치에 관리자 권한이 필요합니다. root 또는 sudo로 다시 실행하세요."
  fi
  "${APT[@]}" update
  "${APT[@]}" install -y python3 python3-venv python3-pip ca-certificates procps
}

install_python_if_missing
command -v bash >/dev/null 2>&1 || fail "bash가 없습니다."
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail "Python 3.10 이상이 필요합니다. 현재 버전: $PY_VERSION"

# Python은 venv를 통해 프로젝트 내부에 격리합니다.
if [[ ! -x .venv/bin/python ]]; then
  say "가상환경 생성: python3 -m venv .venv"
  if ! python3 -m venv .venv; then
    warn "가상환경 생성 실패. python3-venv/python3-pip 패키지 설치를 시도합니다."
    if [[ "$EUID" -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
      apt-get update && apt-get install -y python3-venv python3-pip
    elif command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
    else
      fail "python3-venv를 자동 설치할 수 없습니다. 관리자 권한으로 OS의 Python venv 패키지를 설치한 뒤 다시 실행하세요."
    fi
    python3 -m venv .venv || fail "OS 패키지 설치 후에도 가상환경을 만들지 못했습니다."
  fi
else
  say "기존 .venv 가상환경을 사용합니다."
fi

PY="$PROJECT_ROOT/.venv/bin/python"
say "pip 업그레이드"
"$PY" -m pip install --upgrade pip
say "런타임 의존성 설치"
"$PY" -m pip install -r requirements.txt
say "개발/테스트 의존성 설치"
"$PY" -m pip install -r requirements-dev.txt

# 테스트는 운영 계정/상태/로그와 완전히 분리된 임시 디렉터리에서 실행합니다.
# 기존 logs/ 및 json/ 아래 운영 런타임 파일은 테스트 때문에 수정/삭제하지 않습니다.
# 주문 취소 기록도 임시 경로로 격리하고, 종료 후 임시 디렉터리만 제거합니다.
TEST_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/kis-auto-trader-setup-test.XXXXXX")"
cleanup_test_runtime() {
  if [[ -n "${TEST_RUNTIME_DIR:-}" && -d "$TEST_RUNTIME_DIR" ]]; then rm -rf -- "$TEST_RUNTIME_DIR"; fi
}
trap cleanup_test_runtime EXIT
export KIS_TEST_MODE=1
export KIS_TEST_RUNTIME_DIR="$TEST_RUNTIME_DIR"
export KIS_DATA_DIR="$TEST_RUNTIME_DIR/json"
export KIS_LOG_DIR="$TEST_RUNTIME_DIR/logs"
export STRATEGY_SIGNAL_LOG="$TEST_RUNTIME_DIR/logs/strategy_signals.jsonl"
export STATE_FILE="$TEST_RUNTIME_DIR/state.json"
export UNCERTAIN_ORDERS_FILE="$TEST_RUNTIME_DIR/uncertain_orders.json"
export CANCEL_ORDERS_FILE="$TEST_RUNTIME_DIR/cancel_orders.json"
export HOLIDAY_CACHE_FILE="$TEST_RUNTIME_DIR/holiday_config.json"
export KIS_TOKEN_CACHE_FILE="$TEST_RUNTIME_DIR/kis_tokens.json"
export KIS_ENABLE_TRADING=false
export KIS_PAPER=true
export KIS_APP_KEY=TEST_ONLY
export KIS_APP_SECRET=TEST_ONLY
export KIS_HTS_ID=TEST_ONLY
export KIS_CANO=00000000
export PYTHONPYCACHEPREFIX="$TEST_RUNTIME_DIR/pycache"


say "Python 소스 컴파일 검증"
"$PY" -m compileall -q .
say "스모크 테스트"
"$PY" smoke_test.py
say "전체 테스트 (pytest -q)"
# 테스트는 가짜 systemctl을 PATH 앞에 두지만, `sudo systemctl` 호출은 실제 sudo가
# 테스트용 systemctl을 우회할 수 있습니다. pytest 프로세스에만 sudo 래퍼를 적용합니다.
# 실제 설치/모니터링 실행에는 이 임시 PATH를 적용하지 않습니다.
TEST_COMMAND_BIN="$TEST_RUNTIME_DIR/test-command-bin"
mkdir -p "$TEST_COMMAND_BIN"
cat > "$TEST_COMMAND_BIN/sudo" <<'SUDO_TEST_WRAPPER'
#!/usr/bin/env bash
# 테스트 전용: sudo 옵션을 건너뛰고 대상 명령을 현재 테스트 환경에서 실행합니다.
while (($#)); do
  case "$1" in
    -n|-E|-H|-S|-k) shift ;;
    -u|-g|-h|-p|-C) shift; (($#)) && shift ;;
    --) shift; break ;;
    -*) shift ;;
    *) break ;;
  esac
done
(($#)) || exit 0
exec "$@"
SUDO_TEST_WRAPPER
chmod 700 "$TEST_COMMAND_BIN/sudo"
PATH="$TEST_COMMAND_BIN:$PATH" "$PY" -m pytest -q

# 환경 설정이 없으면 템플릿만 만들고 안전하게 중단합니다. 키/계좌를 임의로 만들거나
# 미설정 상태에서 매매 프로세스를 실행하지 않습니다. 재실행하면 설치/검증은 안전하게 반복됩니다.
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  warn ".env가 없어 .env.example을 복사했습니다. 키/계좌는 자동 설정하지 않았습니다."
  cat <<'MSG'
.env 설정을 완료한 다음 setup.sh를 다시 실행하세요.
  nano .env
필수 확인: KIS_PAPER=true, KIS_ENABLE_TRADING=false(최초 실행 권장),
KIS_APP_KEY, KIS_APP_SECRET, KIS_HTS_ID, KIS_CANO.
MSG
  exit 2
fi
chmod 600 .env

# 환경 파일은 실행하지 않고 필요한 값만 읽어 검사합니다.
read_env_value() {
  local key="$1"
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" .env | tail -n 1 | sed 's/[[:space:]]*#.*$//' | tr -d '\r' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" | xargs || true
}
missing=()
for key in KIS_APP_KEY KIS_APP_SECRET KIS_HTS_ID KIS_CANO; do
  value="$(read_env_value "$key")"
  if [[ -z "$value" || "$value" == *"여기에_"* || "$value" == *"계좌번호_"* || "$value" == *"YOUR_"* || "$value" == *"CHANGEME"* ]]; then
    missing+=("$key")
  fi
done
if ((${#missing[@]} > 0)); then
  warn ".env의 필수 설정이 비어 있거나 예제 값입니다: ${missing[*]}"
  warn "설정 후 setup.sh를 다시 실행하세요. 매매/모니터링은 시작하지 않았습니다."
  exit 2
fi

# Ensure the configured log directory is writable by the account that will run
# the service. Do not hard-code Ubuntu usernames and do not chmod log files globally.
ensure_runtime_log_directory() {
  local raw_path target_user target_group resolved_path existed_in_advance target_can_write
  raw_path="$(read_env_value KIS_LOG_DIR)"
  [[ -n "$raw_path" ]] || raw_path="logs"
  if [[ "$raw_path" = /* ]]; then resolved_path="$raw_path"; else resolved_path="$PROJECT_ROOT/$raw_path"; fi
  target_user="${SUDO_USER:-$(id -un)}"
  id "$target_user" >/dev/null 2>&1 || fail "로그 실행 계정을 확인할 수 없습니다: $target_user"
  target_group="$(id -gn "$target_user")" || fail "로그 실행 계정의 기본 그룹을 확인할 수 없습니다: $target_user"
  existed_in_advance=0
  [[ -d "$resolved_path" ]] && existed_in_advance=1
  mkdir -p -- "$resolved_path" || fail "로그 디렉터리를 생성할 수 없습니다: $resolved_path"
  if [[ "$EUID" -eq 0 ]]; then
    target_can_write=unknown
    if command -v runuser >/dev/null 2>&1; then
      if runuser -u "$target_user" -- test -w "$resolved_path"; then target_can_write=yes; else target_can_write=no; fi
    elif command -v su >/dev/null 2>&1; then
      if su "$target_user" -s /bin/sh -c 'test -w "$1"' sh "$resolved_path"; then target_can_write=yes; else target_can_write=no; fi
    elif [[ "$target_user" == "root" ]]; then
      target_can_write=yes
    fi
    if [[ "$target_can_write" == "no" || ( "$target_can_write" == "unknown" && "$existed_in_advance" -eq 0 ) ]]; then
      # Existing external paths (e.g. /tmp or /var/log) are never chowned blindly.
      case "$resolved_path" in
        "$PROJECT_ROOT"|"$PROJECT_ROOT"/*)
          chown "$target_user:$target_group" "$resolved_path" || fail "프로젝트 로그 디렉터리 소유권을 설정할 수 없습니다: $resolved_path"
          chmod 2775 "$resolved_path" || fail "프로젝트 로그 디렉터리 권한을 설정할 수 없습니다: $resolved_path"
          ;;
        *)
          if [[ "$existed_in_advance" -eq 0 ]]; then
            chown "$target_user:$target_group" "$resolved_path" || fail "새 로그 디렉터리 소유권을 설정할 수 없습니다: $resolved_path"
            chmod 2775 "$resolved_path" || fail "새 로그 디렉터리 권한을 설정할 수 없습니다: $resolved_path"
          else
            fail "외부 로그 디렉터리에 서비스 계정이 쓸 수 없습니다: $resolved_path. 해당 디렉터리 권한을 별도로 설정하세요."
          fi
          ;;
      esac
    fi
    if command -v runuser >/dev/null 2>&1; then
      runuser -u "$target_user" -- test -w "$resolved_path" || fail "서비스 계정이 로그 디렉터리에 쓸 수 없습니다: $resolved_path"
    elif command -v su >/dev/null 2>&1; then
      su "$target_user" -s /bin/sh -c 'test -w "$1"' sh "$resolved_path" || fail "서비스 계정이 로그 디렉터리에 쓸 수 없습니다: $resolved_path"
    elif [[ "$target_user" != "root" ]]; then
      fail "서비스 계정의 로그 디렉터리 권한을 확인할 su/runuser 명령이 없습니다: $resolved_path"
    fi
  elif [[ ! -w "$resolved_path" ]]; then
    if command -v sudo >/dev/null 2>&1; then
      case "$resolved_path" in
        "$PROJECT_ROOT"|"$PROJECT_ROOT"/*)
          sudo chown "$target_user:$target_group" "$resolved_path" && sudo chmod 2775 "$resolved_path" || fail "프로젝트 로그 디렉터리 권한을 자동 복구하지 못했습니다: $resolved_path"
          ;;
        *) fail "외부 로그 디렉터리에 쓰기 권한이 없습니다: $resolved_path. 해당 디렉터리 권한을 별도로 설정하세요." ;;
      esac
    else
      fail "로그 디렉터리에 쓰기 권한이 없습니다: $resolved_path (디렉터리 소유권을 확인하세요)"
    fi
  fi
  if [[ "$EUID" -ne 0 ]]; then
    [[ -w "$resolved_path" ]] || fail "현재 실행 계정이 로그 디렉터리에 쓸 수 없습니다: $resolved_path"
  fi
  say "로그 디렉터리 쓰기 권한 확인: $resolved_path (실행 계정=$target_user)"
}
ensure_runtime_log_directory

PAPER="$(read_env_value KIS_PAPER | tr '[:upper:]' '[:lower:]')"
TRADING="$(read_env_value KIS_ENABLE_TRADING | tr '[:upper:]' '[:lower:]')"
if [[ "$PAPER" == "false" && ( "$TRADING" == "true" || "$TRADING" == "1" || "$TRADING" == "yes" || "$TRADING" == "y" || "$TRADING" == "on" ) ]]; then
  if [[ "${SETUP_ALLOW_LIVE_TRADING:-}" != "YES" ]]; then
    fail "실전 주문이 활성화된 .env입니다. 실전 자동시작을 허용하려면 위험을 확인한 뒤 SETUP_ALLOW_LIVE_TRADING=YES ./scripts/setup.sh 로 명시하세요."
  fi
fi

# 테스트 산출물을 폐기한 뒤 운영 환경변수로 복귀합니다. 운영 파일은 건드리지 않습니다.
cleanup_test_runtime
TEST_RUNTIME_DIR=""
trap - EXIT
unset KIS_TEST_MODE KIS_TEST_RUNTIME_DIR KIS_DATA_DIR KIS_LOG_DIR STRATEGY_SIGNAL_LOG STATE_FILE UNCERTAIN_ORDERS_FILE CANCEL_ORDERS_FILE HOLIDAY_CACHE_FILE KIS_TOKEN_CACHE_FILE
unset KIS_ENABLE_TRADING KIS_PAPER KIS_APP_KEY KIS_APP_SECRET KIS_HTS_ID KIS_CANO
unset PYTHONPYCACHEPREFIX

say "셸 문법 검사"
while IFS= read -r -d '' shell_file; do bash -n "$shell_file"; done < <(find scripts monitoring -type f -name '*.sh' -print0)

say "모니터링 설치 및 시작 (Ubuntu systemd / Proot 자동 감지)"
"$PROJECT_ROOT/scripts/start_monitoring.sh"

# start_monitoring.sh는 매매 가능 시간에는 프로세스를 시작하고, 종료 시각 이후/주말에는
# 타이머만 유지합니다. 별도로 직접 start.sh를 다시 실행하지 않습니다.
# 종료 기준 시각 이후 또는 주말에는 매매 프로세스가 실행되지 않는 것이 정상입니다.
# 이때는 trader healthcheck 대신 지속 모니터링 타이머/루프가 살아 있는지 확인합니다.
now_kst="$(TZ=Asia/Seoul date +%H:%M)"
dow_kst="$(TZ=Asia/Seoul date +%u)"
cutoff="$(read_env_value AUTO_SHUTDOWN_TIME)"
[[ "$cutoff" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || cutoff="15:10"
if [[ "$dow_kst" -gt 5 || "$now_kst" > "$cutoff" || "$now_kst" == "$cutoff" ]]; then
  say "현재 KST $now_kst / 요일 $dow_kst: 매매 프로세스 미기동 시간입니다. 지속 모니터링 상태를 확인합니다."
  pid1_comm="$(ps -p 1 -o comm= 2>/dev/null | tr -d ' ' || true)"
  if [[ "$pid1_comm" == "systemd" ]] && command -v systemctl >/dev/null 2>&1; then
    systemctl is-active --quiet kis-auto-trader-start.timer || fail "시작 타이머가 active 상태가 아닙니다. systemctl status kis-auto-trader-start.timer 확인 필요"
    systemctl is-active --quiet kis-auto-trader-watchdog.timer || fail "watchdog 타이머가 active 상태가 아닙니다. systemctl status kis-auto-trader-watchdog.timer 확인 필요"
  else
    monitor_pid_file="$($PY - "$PROJECT_ROOT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from config.paths import runtime_path
print(runtime_path("monitor_pid"))
PYCODE
)"
    monitor_pid="$(cat "$monitor_pid_file" 2>/dev/null || true)"
    monitor_args="$(ps -p "$monitor_pid" -o args= 2>/dev/null || true)"
    monitor_state="$(ps -p "$monitor_pid" -o stat= 2>/dev/null || true)"
    if [[ ! "$monitor_pid" =~ ^[0-9]+$ ]] || [[ -z "$monitor_args" || "$monitor_state" == Z* ]] || ! grep -Fq "$PROJECT_ROOT/monitoring/monitor_loop.sh" <<< "$monitor_args"; then
      warn "Proot 감시 루프 PID를 확인할 수 없습니다. runtime log directory의 monitoring_$(TZ=Asia/Seoul date +%Y%m%d).log 확인 필요"
      exit 1
    fi
  fi
  say "매매는 시작하지 않았지만 예약/감시가 활성 상태입니다. 다음 평일 08:00 KST 시작을 확인합니다."
else
  say "매매 프로세스 상태 확인: monitoring/healthcheck.py"
  health_ok=0
  for attempt in $(seq 1 30); do
    if "$PY" monitoring/healthcheck.py; then health_ok=1; break; fi
    sleep 1
  done
  if [[ "$health_ok" -ne 1 ]]; then
    warn "healthcheck에서 매매 프로세스를 확인하지 못했습니다. 매매를 정상 시작했다고 판단하지 않습니다."
    warn "logs/trader_$(TZ=Asia/Seoul date +%Y%m%d).log, logs/error_$(TZ=Asia/Seoul date +%Y%m%d).log, logs/watchdog_$(TZ=Asia/Seoul date +%Y%m%d).log을 확인하세요."
    exit 1
  fi
fi

say "설치·검증·모니터링 시작 완료."
printf '프로젝트: %s\n' "$PROJECT_ROOT"
printf 'Python: %s\n' "$($PY --version 2>&1)"
printf '모니터링 중지: ./scripts/stop_monitoring.sh\n'
printf '매매 중지: ./scripts/stop.sh\n'
printf '일자별 로그: %s/logs/*_YYYYMMDD.log (KST)\n' "$PROJECT_ROOT"
