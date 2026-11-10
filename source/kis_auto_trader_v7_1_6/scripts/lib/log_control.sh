#!/usr/bin/env bash
# Shared daily log helpers for Ubuntu/systemd and Proot/non-systemd.
# Files are aligned to the log directory owner/group so root-run watchdog tasks
# do not leave root-only artifacts that the trader account cannot append/read.

kis_prepare_shared_log() {
  local log_dir="${1:?log directory required}" file_name="${2:?log filename required}"
  local log_file archive_file timestamp
  mkdir -p -- "$log_dir" || {
    printf '[오류] 로그 디렉터리를 생성할 수 없습니다: %s\n' "$log_dir" >&2
    return 1
  }
  log_file="$log_dir/$file_name"
  timestamp="$(TZ=Asia/Seoul date +%Y%m%dT%H%M%S).$$"

  if [[ -e "$log_file" && "$EUID" -eq 0 ]]; then
    # root 실행으로 만들어진 파일도 디렉터리의 운영 계정/그룹에 맞춥니다.
    chown --reference="$log_dir" "$log_file" 2>/dev/null || true
    chmod 0664 "$log_file" 2>/dev/null || true
  fi
  if [[ -e "$log_file" && ! -w "$log_file" ]]; then
    if [[ -w "$log_dir" ]]; then
      archive_file="${log_file}.unwritable.${timestamp}"
      mv -- "$log_file" "$archive_file" || {
        printf '[오류] 쓰기 불가 로그를 보관할 수 없습니다: %s\n' "$log_file" >&2
        return 1
      }
      printf '[주의] 쓰기 불가 로그를 보관했습니다: %s\n' "$archive_file" >&2
    else
      printf '[오류] 로그 파일/디렉터리 쓰기 권한이 없습니다: %s\n' "$log_file" >&2
      return 1
    fi
  fi
  ( umask 0002; : >> "$log_file" ) || {
    printf '[오류] 로그 파일을 열 수 없습니다: %s\n' "$log_file" >&2
    return 1
  }
  if [[ "$EUID" -eq 0 ]]; then
    chown --reference="$log_dir" "$log_file" 2>/dev/null || true
    chmod 0664 "$log_file" 2>/dev/null || true
  fi
  printf '%s\n' "$log_file"
}

kis_log_append() {
  local log_dir="${1:?log directory required}" file_name="${2:?log filename required}" message="${3:-}"
  local log_file
  log_file="$(kis_prepare_shared_log "$log_dir" "$file_name")" || return 1
  printf '%s %s\n' "$(TZ=Asia/Seoul date '+%Y-%m-%dT%H:%M:%S%z')" "$message" >> "$log_file"
}

kis_log_control() {
  local log_dir="${1:?log directory required}" message="${2:-}" date_tag
  date_tag="$(TZ=Asia/Seoul date +%Y%m%d)"
  kis_log_append "$log_dir" "control_${date_tag}.log" "$message"
}
