# CHANGELOG V7.0.0

## 목표
V6.0.2를 기반으로 기존 주문·체결·대사 안전 경로를 보존하면서 매수/매도 처리 책임을 모듈로 분리하고, 운영 파일을 안전하게 이전할 수 있는 업그레이드 도구를 추가합니다.

## 변경 사항
- `main.py`를 호환 실행 진입점으로 축소하고 애플리케이션 조립 및 루프를 `application.py`로 분리했습니다. 기존 `python main.py` 실행과 `import main` 호환 경로를 유지합니다.
- 체결 이벤트 반영은 `trading/fill_handler.py`, 보유 포지션 청산 평가는 `trading/sell_process.py`, 매수 후보 검증·선정, 주문 직전 시세 재조회, 현금/미체결 예약액 기반 수량 산정 및 마지막 BUY 게이트는 `trading/buy_process.py`로 분리했습니다.
- 최종 BUY 제출은 계속 공유 `OrderManager.submit()`을 사용하며, 주문 직전 종료/거래시간/일일한도 확인, 예약 금액, 잔고 재대사 잠금, Telegram 알림 및 매수 대기시간을 유지합니다.
- `trading/orders.py` 및 `trading/reconcile.py`의 주문 생명주기/대사 동작은 변경하지 않았습니다. `orders.py`의 중복 no-op 대입 한 줄만 제거했으며 주문 동작 변경은 없습니다.
- `scripts/upgrade_migrate.py`를 강화했습니다. 이전 전 JSON/JSONL 및 핵심 주문 상태 구조를 검사하고, 대상 `.migration_backups/<UTC timestamp>/`에 원본 바이트와 SHA-256 manifest를 먼저 백업한 뒤 원자 복사와 사후 해시/구문 검증을 수행합니다. 충돌·손상·사용자 지정 경로와 레거시 경로의 출처 모호성이 있으면 중단하며, 재실행 시 동일 파일은 건너뛰고 누락 파일만 이어서 이전합니다. 전략 JSONL은 자동 병합하지 않습니다.
- 매수 후보 검증·최종 제출 게이트와 운영 파일 마이그레이션의 회귀 테스트를 추가했습니다.
- 10개 내부 전략 YAML 및 기존 오프라인 전략 테스트/로그 분석 도구를 유지합니다.

## 안전 및 호환성
- KIS 주문/취소 POST를 이용한 테스트를 실행하지 않았습니다. 테스트는 격리된 임시 런타임을 사용합니다.
- 운영 주문 상태 JSON 및 `logs/strategy_signals.jsonl`은 릴리스 ZIP에 포함하지 않습니다.
- systemd unit 파일과 식별자를 `kis-auto-trader.service`, `kis-auto-trader-start.*`, `kis-auto-trader-watchdog.*`로 통일했습니다. 설치기는 기존 `kis-auto-trader-v5-*` 매매 서비스가 실행 중인지 확인하고, 구 timer/oneshot의 LoadState와 실제 중지 결과를 확인합니다. 비활성화/중지가 실패하면 기존 unit 파일을 삭제하지 않고 전환을 중단합니다.
- 이 릴리스의 테스트 통과는 신호 계산/코드 경로의 검증이며 전략 수익성 또는 실전 수익을 보장하지 않습니다.


## Runtime path unification (completed)

- Added `config/paths.py` as the single resolver for runtime files; relative paths resolve from the project root rather than the current working directory.
- Canonical JSON runtime directory is `json/`: state, REST/WebSocket token cache, uncertain orders, cancel-order audit records, and holiday cache. Logs remain under `logs/`; static KRX calendars and strategy configuration remain under `config/`.
- Legacy per-file environment variables remain supported as explicit overrides. The migration utility maps known legacy files into `json/`, clears only legacy default path overrides in the copied `.env`, preserves custom overrides, refuses conflicting files, and never deletes the source.
- Added tests for changed working directory, missing files, first creation, restart recovery, and migration compatibility. Runtime JSON remains excluded from the release archive.

## 운영 제어 및 감시 정책

- `scripts/start.sh`와 `scripts/stop.sh`는 `manual`, `systemd`, `watchdog`, `scheduler`, `reload_config`, `installer` 실행 출처와 요청/결과를 제어 로그에 기록합니다.
- `scripts/stop_monitoring.sh`는 `KIS_DATA_DIR/monitoring_disabled.json`에 영구 감시 중지 표식을 원자 기록한 뒤 타이머/진행 중인 oneshot 확인 작업 또는 Proot 감시 루프를 중지합니다. 매매 서비스는 중지하지 않으며, watchdog은 표식이 유효한 동안 날짜가 바뀌어도 자동 시작하지 않습니다. `scripts/start_monitoring.sh`를 명시적으로 실행해야 표식이 제거되고 감시가 재개됩니다.
- 현재 systemd 매매 서비스가 같은 프로젝트 경로에서 실행 중이면 활성 unit 본문과 매매 프로세스를 교체/재시작하지 않고 타이머만 재개합니다. 기존 유효 `Restart` 정책이 `no`가 아니면 제한된 drop-in으로 `Restart=no`를 적용하고 `systemctl show`로 확인한 뒤 진행합니다. 다른 프로젝트의 활성 서비스/매매 프로세스는 변경하거나 종료하지 않습니다.
- systemd 서비스는 `Restart=no`를 사용하고, 장애 복구는 KST cutoff·주말·휴장일·수동 정지 표식을 검사하는 watchdog 한 곳에서 수행합니다. 이를 통해 systemd의 자동 재시작이 수동 정지/거래시간 규칙을 우회하지 않게 합니다.
- Proot PID 파일은 `config/paths.py`의 `monitor_pid` 경로를 사용해 시작·중지·설치 상태 검사가 같은 파일을 확인합니다. 사용자 지정 `KIS_LOG_DIR`도 일관되게 반영합니다. systemd 시작 요청과 수동 정지는 동일한 `start_trigger.lock`을 공유하고, 잠금 획득 후 자동 시작 표식을 재검사해 수동 정지와의 경쟁 상태를 방어합니다. Proot watchdog도 시작 직전 동일 잠금/표식을 확인하고, `start.sh`는 Python 프로세스가 확인된 뒤 잠금을 해제합니다. Python 자식에는 잠금 FD를 상속하지 않아 장시간 실행 중 수동 정지가 잠금에 막히지 않도록 했습니다.
- `scripts/reload_config.sh --validate-only`와 기본 모드는 수동 정지 상태를 해제하지 않습니다. `--start`는 설정 검증 후 사용자가 명시적으로 재개를 요청한 경우에만 수동 정지 표식을 제거하고, 실행 중인 프로세스의 종료/재시작/신규 시작 결과를 별도 기록합니다.
- watchdog의 정상/억제 판단 로그는 KST 정각·30분 및 핵심 상태 전환 중심으로 제한해 분 단위 timer 실행에 따른 로그 폭증을 방지합니다. 시작/정지 요청과 실제 결과에는 실행 출처, 사용자, 프로젝트 경로, 실행기와 결과 상태를 기록합니다.
- 수동 정지는 `json/manual_stop.json`(또는 `MANUAL_STOP_FILE`)에 KST 날짜 표식을 남기며, 같은 날짜의 watchdog 복구를 차단합니다. 날짜가 바뀌면 표식이 만료되어 다음 평일 예약 시작을 방해하지 않습니다. 해당 연도 KRX 달력 파일이 없으면 자동 시작은 fail-closed 처리됩니다.
- 예약 시작은 watchdog과 같은 시간·요일·휴장일·수동 정지 검사를 통과해야 합니다. `Persistent=true`로 놓친 예약을 다음 부팅에 확인하더라도 일요일/휴장일/컷오프 뒤에는 시작하지 않습니다.
- Proot 감시 루프는 컷오프 후에도 살아 있어 다음 영업일을 확인합니다.


## 재감사 반영 사항 (2026-10-22)
- 감시 테스트 harness의 임시 `config` 패키지 격리를 보완하고 stdlib 전용 helper의 인터프리터 시작 지연을 줄였습니다. `tests/test_monitoring_scripts.py` 45개와 전체 292개 pytest가 정상 종료됩니다.
- 기존 `config/my_entry_strategy.kis.yaml` 및 `config/my_exit_strategy.kis.yaml`의 거래 조건/호환 ID는 유지하면서, V5.0/V5.1.5로 오해될 수 있던 활성 설정 주석과 표시 이름을 V7.0.0 호환 예제로 갱신했습니다.
- 내부 엔진에서 YAML 조건이 최종 액션을 정할 때 프리셋의 공식 신호 강도를 보존하도록 수정했습니다. 공식 모멘텀 매도, 52주 신고가 매수, 돌파 실패 매도, 추세 필터 강도를 대조해 보정하고, 내부 프리셋과 YAML 지표의 breakout/high lookback 범위를 일치시켰습니다.
- `kis_strategy_test.py` 오프라인 모드는 첨부 종목 DB에서 무작위 종목코드/이름을 선택하고 합성 OHLCV로 평가합니다. 결과 JSON은 `KIS_LOG_DIR`을 따르며, 온라인 시세 모드는 `KIS_PAPER=true`만 허용합니다.
- 공식 52주 신고가 구현이 현재가 API의 `w52_high`를 사용한다는 차이를 문서화했습니다. 내부 엔진은 추가 REST 호출을 억제하기 위해 252거래일 일봉 고가를 사용하며, 정확한 데이터 소스 parity를 보장하지 않습니다.
- 전체 pytest 149 passed, 오프라인 smoke 14 passed, 컴파일 및 셸 문법 검사 통과. 최종 배포 아카이브의 파일 구성과 실행 권한/런타임 제외 규칙을 확인하고 추출본에서 같은 테스트를 통과했습니다. 첫 설치 `.env` 미설정 안전 중단도 mock 환경에서 확인했습니다. 이번 유지보수에서는 문서 분류/설치 경로/주석 구조를 보완한 뒤 최종 ZIP을 다시 생성합니다.


## 문서·설치·배포 정리

- 현재 릴리스 변경 이력은 `docs/release/`에 두고, 과거 버전 문서는 최종 배포 ZIP에서 제외했습니다.
- `README.md`를 현재 V7.0.0 실행·설치·업데이트·안전 원칙 중심으로 재작성했습니다. 현재 작업 트리에 `README.me`가 없어 직접 비교는 불가능함을 문서에 기록했습니다.
- `.env.example` 활성 설정 108개에 기본값, 허용값/형식, 운영 영향, 주의사항 메타를 추가/정리했습니다.
- `docs/configuration/CONFIGURATION_REFERENCE.md`, `docs/operations/V7.0.0_OPERATIONS_MANUAL.md`, `docs/architecture/V7.0.0_SYSTEM_FLOW.md`, `docs/README.md`를 최신화/추가했습니다.
- `scripts/reload_config.sh`의 `config.paths` 전용 Python 보조 호출은 `-S`로 site 초기화를 건너뛰어 격리된 감시 테스트에서 불필요한 지연을 줄입니다. 설정/YAML 검증은 정상 Python 환경으로 유지합니다.

## 상세 운영 문서 보강
- V5.1.7 문서의 산출 형식과 구성 원칙을 V7.0.0 소스에 맞춰 재작성: Markdown, Word, PDF 운영 매뉴얼 제공.
- 별도 Mermaid 흐름도 원본(`V7.0.0_SYSTEM_FLOW.mmd`)과 파일·메소드 참조표 추가.
- V7.0.0의 실제 모듈 경계, 10개 YAML 전략, 프로필 투표, 주문/체결/불명확 주문 대사, systemd/watchdog 운영 절차 및 모의투자 체크리스트를 문서화.
- 이 문서 생성 과정에서는 KIS 실전 API나 실제 주문을 호출하지 않음.


## V7.0.0 문서/설치 정리 보완

- `docs/`를 운영·아키텍처·설정·검증·릴리스·색인 디렉터리로 분류하고 현재 버전 문서만 유지했습니다. 이전 릴리스 문서는 현재 배포 ZIP에서 제외했습니다.
- 사용하지 않는 `vendor/` 디렉터리를 제거했습니다.
- `.env.example`의 설정 메타/주의사항을 각 변수 위로 이동하고 공통 설명을 그룹 단위로 정리했습니다.
- `config` 패키지의 설정 모듈 import를 지연시켜, 외부 의존성 설치 전 `config.paths`를 읽을 때 `python-dotenv`가 없어도 경로 해석이 가능하도록 수정했습니다.
- `setup.sh`가 `scripts/`, `monitoring/`, `tests/`를 포함한 프로젝트 소유 `.sh` 파일에 실행 권한을 부여하도록 수정했습니다. `.venv/`, `.git/` 등은 제외합니다.

- 유지보수 후 최종 ZIP은 현재 프로젝트 파일 147개를 포함하며 과거 릴리스 문서, `.env`, 런타임 상태/로그, 캐시 및 `vendor/`는 포함하지 않습니다.
