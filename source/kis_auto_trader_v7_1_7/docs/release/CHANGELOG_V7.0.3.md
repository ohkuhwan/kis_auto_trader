# CHANGELOG V7.0.3

## 수정 사항

- `scripts/lib/log_control.sh`에 공통 일별 제어 로그 기록 함수를 추가하고 시작·정지·설정 리로드·모니터링 관련 스크립트가 이를 사용하도록 통일했습니다.
- 당일 `control_YYYYMMDD.log`가 현재 실행 계정에서 쓰기 불가능하고 로그 디렉터리는 쓸 수 있는 경우, 기존 로그를 `.unwritable.<timestamp>`로 보관한 뒤 새 로그를 생성합니다. 기존 로그 내용을 덮어쓰거나 삭제하지 않습니다.
- 새 제어 로그 파일은 `umask 0002`로 생성해 그룹 쓰기 권한을 허용합니다. 사용자 이름을 `ubuntu`로 하드코딩하지 않습니다.
- `setup.sh`에서 `.env`의 `KIS_LOG_DIR`를 확인하고 서비스 실행 계정의 디렉터리 쓰기 권한을 사전 검사합니다. 프로젝트 내부 로그 디렉터리 또는 새로 생성한 디렉터리만 필요한 경우 소유권/권한을 복구합니다. 기존 외부 경로의 소유권은 임의 변경하지 않으며, 접근 불가 시 이유를 출력하며 중단합니다.
- `setup.sh`를 `sudo`로 실행한 경우에도 `start_monitoring.sh`가 `SUDO_USER`를 systemd 서비스 사용자로 선택하도록 수정했습니다.
- `reload_config.sh`가 `STRATEGY_PROFILES_FILE`(기본 `config/strategy_profiles.yaml`)을 진입/청산 YAML과 함께 검사합니다. YAML 파싱과 최상위 mapping, 비어 있지 않은 `strategies` 목록 및 `profiles` mapping을 검증하고, 실패 시 프로세스를 재시작하지 않습니다.
- 런타임 버전 표기를 V7.0.3으로 갱신했습니다. V7.0.2의 과거 진단 자료는 변경하지 않았습니다.

## 검증

- `bash -n` 셸 문법 검사
- `tests/test_monitoring_scripts.py` 회귀 테스트
- 전체 pytest, smoke test 및 패키지 내 파일/권한 검사 결과는 릴리스 검증 후 기록합니다.

## 주의

- 이 릴리스는 모의투자 우선 설정과 기존 주문 안전장치를 유지합니다. 설치 완료만으로 실전 주문 적합성이 입증되는 것은 아닙니다.
