# KIS Auto Trader V7.1.6 설정 항목 참조서

현재 `.env.example`의 활성 설정 95개를 기준으로 작성했습니다. 표의 기본값은 배포 예제에 설정된 값이며, 환경변수를 생략했을 때 `config/settings.py`가 사용하는 내부 fallback과 다를 수 있습니다. 실제 동작은 관련 코드 경로와 함께 확인하세요.

## 실행 전 안전 확인

- `KIS_PAPER=true`는 모의투자 서버, `KIS_ENABLE_TRADING=false`는 주문 전송 차단입니다. 두 설정은 서로 다릅니다.
- 예제 자격증명은 실제 모의투자 자격증명으로 바꾸고 `.env`는 커밋/공유하지 마세요.
- 런타임 경로 변경은 기존 데이터를 자동 이전하지 않습니다. 변경 전 백업/이전 절차를 수행하세요.
- `AUTO_SHUTDOWN_TIME`은 보유 주식 자동 매도 시각이 아닙니다.

## API · 계좌 · 실행 안전

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `KIS_PAPER` | `true` | true / false | API 환경 선택입니다. true=모의투자 URL/TR, false=실전 URL/TR. App Key/Secret도 같은 환경에서 발급한 값을 사용하세요. 기본 true. false는 실전 서버이므로 모의투자 검증 중 변경 금지. |
| `KIS_ENABLE_TRADING` | `false` | true / false | 실제 주문 POST 허용 스위치입니다. false면 신호/상태/로그만 확인하는 DRY-RUN이며, 검증 전 true로 바꾸지 마세요. 기본 false. true는 주문 POST 허용. DRY-RUN 후 모의투자에서만 검증. |
| `KIS_APP_KEY` | `예제 자리표시자: 여기에_모의투자_APP_KEY` | 문자열(계정/비밀정보; 예제 값은 교체 필요) | KIS Developers에서 발급한 현재 KIS_PAPER 환경의 App Key입니다. 필수 비밀정보이며 실제 값을 공유/커밋하지 마세요. 실제 값은 .env에만 두고 커밋·공유·로그 출력 금지. |
| `KIS_APP_SECRET` | `예제 자리표시자: 여기에_모의투자_APP_SECRET` | 문자열(계정/비밀정보; 예제 값은 교체 필요) | KIS Developers에서 발급한 현재 KIS_PAPER 환경의 App Secret입니다. 필수 비밀정보이며 실제 값을 공유/커밋하지 마세요. 실제 값은 .env에만 두고 커밋·공유·로그 출력 금지. |
| `KIS_HTS_ID` | `예제 자리표시자: 여기에_HTS_ID` | 문자열(계정/비밀정보; 예제 값은 교체 필요) | 조건검색 목록 조회 및 체결통보 구독에 사용하는 HTS ID입니다. 계좌번호나 API Key와 혼동하지 마세요. 실제 값은 .env에만 두고 커밋·공유·로그 출력 금지. |
| `KIS_CANO` | `예제 자리표시자: 계좌번호_앞8자리` | 문자열(계정/비밀정보; 예제 값은 교체 필요) | 한국투자증권 계좌번호 앞 8자리입니다. 비밀정보로 취급하고 공유하지 마세요. 실제 값은 .env에만 두고 커밋·공유·로그 출력 금지. |
| `KIS_ACNT_PRDT_CD` | `01` | 문자열/숫자; 아래 설명과 config/settings.py 기준 | 계좌 상품 코드입니다. 국내 주식 계좌의 일반적인 값은 01이지만 본인 계좌 설정을 확인하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 런타임 경로

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `KIS_DATA_DIR` | `json` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | 상태·토큰·주문 기록·계좌 주문 잠금·휴장일 캐시 JSON의 공통 디렉터리입니다. 기본 json/. 상대경로는 프로젝트 루트 기준입니다. 비우면 json을 사용합니다. 운영 중 변경하면 기존 JSON을 새 위치로 자동 이동하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MANUAL_STOP_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 수동 정지 표식 파일 경로입니다. 기본값은 KIS_DATA_DIR/manual_stop.json이며, 당일 watchdog 자동 재시작을 막습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `START_TRIGGER_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 예약/watchdog이 systemd 서비스 시작 원인을 전달하는 일회성 파일입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_TOKEN_CACHE_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | REST 접근토큰과 WebSocket 접속키 캐시의 선택적 개별 경로 재정의입니다. 비우면 KIS_DATA_DIR/kis_tokens.json을 사용합니다. 인증키가 들어가므로 공유하거나 커밋하지 마세요. 실제 값은 .env에만 두고 커밋·공유·로그 출력 금지. |
| `BUILDER_SYMBOLS_FILE` | `config/entry_symbols.txt` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | Builder의 수동 종목 목록을 읽는 UTF-8 텍스트 파일 경로입니다. 한 줄에 종목코드 하나씩 입력합니다. 상대 경로는 실행 위치가 아니라 프로젝트 루트 기준입니다. HTS 조건검색 결과는 별도 동적 후보이며, Builder 모드에서는 이 파일의 목록과 합쳐 전략 평가합니다. 예전 BUILDER_ENTRY_SYMBOLS 환경변수는 호환 목적으로도 읽지 않으므로 이 파일만 수정하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `ENTRY_STRATEGY_FILE` | `config/my_entry_strategy.kis.yaml` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | 내부 진입 평가에 사용하는 YAML 전략 파일 경로입니다. 지원되는 YAML subset만 처리하며, 파일 변경 후 테스트하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `EXIT_STRATEGY_FILE` | `config/my_exit_strategy.kis.yaml` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | 보유 종목 YAML 청산 평가에 사용하는 전략 파일 경로입니다. 진입 전략과 별도 파일이며 실제 포지션 위험 안전망과 함께 동작합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `STRATEGY_PROFILES_FILE` | `config/strategy_profiles.yaml` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | 한투 10개 전략의 등록 목록과 BUY/SELL 참여 목록·점수 기준을 정의하는 YAML 경로입니다. 기본값 config/strategy_profiles.yaml. 상대 경로는 프로젝트 루트 기준이며 누락/오류 시 신규 Builder 매수는 차단됩니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `STRATEGY_SIGNAL_LOG` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 전략별 BUY/HOLD/SELL/ERROR를 기록하는 JSONL 경로입니다. 주문/체결 로그와 별개입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_LOG_DIR` | `logs` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | 일자별 운영 로그 및 불명확 주문 감사 백업 디렉터리입니다. 기본값 logs. setup.sh 테스트는 별도 임시 경로를 사용합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `UNCERTAIN_ORDERS_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 주문 POST 결과가 불명확한 종목의 잠금/감사 JSON 경로입니다. HTS 주문·체결·미체결·잔고 확인 전 삭제하지 마세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `CANCEL_ORDERS_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 원주문별 취소 응답/최종 대사 JSON 경로입니다. 기본값은 상대경로입니다. 재시작 후에도 취소 이력을 추적하므로 운영 중 삭제하지 말고 업데이트 전에 백업하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `ACCOUNT_ORDER_LOCK_FILE` | `json/account_order_lock.json` | 경로: 프로젝트 루트 상대경로 또는 절대경로 | `40910000`, `IGW00002`, `90070000` 계좌/API 신청정보 오류로 BUY·SELL 신규 주문과 추가 취소 POST를 잠그는 영구 상태 JSON입니다. 재시작 후에도 유지되며, 계좌·REST·WebSocket 검증 후 `scripts/unlock_account_order_lock.py`로 수동 해제합니다. 손상/읽기 실패 시 fail-closed 합니다. |
| `STATE_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 포지션, 쿨다운, 일일 거래/손익 기준, 당일 매수 재시도 차단 종목을 저장하는 JSON 파일 경로입니다. 상대 경로는 프로젝트 루트 기준입니다. 비우면 KIS_DATA_DIR/state.json을 사용합니다. 재시작/업데이트 전에 백업하세요. |
| `HOLIDAY_CACHE_FILE` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 휴장일 확인 결과를 저장하는 JSON 캐시 파일 경로입니다. 쓰기 권한과 연도별 달력 파일을 확인하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_PAPER_HOLIDAY_CALENDAR` | `빈 값` | 경로: 프로젝트 루트 상대경로 또는 절대경로; 빈 값이면 기본 경로 | 모의투자 휴장일 판단에 사용할 JSON 달력 경로입니다. 비우면 config/krx_holidays_2026.json을 사용합니다(현재 구현 기본값). 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 진입 후보 · 전략 YAML

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `ENTRY_MODE` | `ALL` | HTS / BUILDER / ALL | 신규 진입 후보 출처만 선택합니다. HTS=HTS 조건검색, BUILDER=BUILDER_SYMBOLS_FILE, ALL=두 출처 합집합. 허용값 HTS/BUILDER/ALL. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `STRATEGY_EVALUATION_MODE` | `ALL` | KIS10 / CUSTOM / ALL | 후보 종목 평가 엔진을 선택합니다. ALL 매수는 두 엔진 모두 BUY, ALL 매도는 한 엔진이라도 SELL이면 신호입니다. 1차/2차 손익절 기준은 별도로 적용됩니다. |
| `KIS_CONDITION_NAME` | 빈 값 | HTS 저장 조건명 또는 빈 값 | HTS 후보 수집에 사용하는 단일 서버 저장 조건명입니다. |
| `KIS_CONDITION_CACHE_TTL_SEC` | `60` | 정수 0~600초 | HTS 조건명 목록의 캐시 시간(초)입니다. 기본 60, 허용 0~600. 0이면 매번 조회합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `STRATEGY_BUILDER_MAX_SYMBOLS` | `20` | 정수 1~20개 | 한 회차에 내부 전략 엔진으로 평가할 최대 종목 수입니다. 기본 20, 허용 1~20. 후보 상한 초과 시 HTS 원본 순서 대신 순환 평가하며, 후보별 점수 정렬로 최종 선택합니다. |
| `BUILDER_OBSERVE_ALL_STRATEGIES` | `false` | true / false | false면 strategy_profiles의 BUY/SELL 참여 전략만 평가합니다. true면 투표에 참여하지 않는 등록 전략까지 진단 평가하므로 최대 lookback/일봉 조회량이 늘어납니다. 현재 전체 전략 관찰은 최대 257일입니다. |
| `EXIT_STRATEGY_INTERVAL_SEC` | `60` | 숫자(초); config/settings.py에서 추가 범위 검증 | 보유 종목의 YAML 청산 조건을 재평가하는 간격(초)입니다. 너무 낮추면 REST 호출과 계산량이 증가합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `DAILY_BAR_CACHE_TTL_SEC` | `300` | 숫자(초, 코드 최소 300초) | 동일 종목 일봉 snapshot 재사용 시간입니다. KIS10/CUSTOM 및 보유종목 청산 평가가 공유합니다. 장중 현재가/위험관리는 별도 WebSocket/현재가를 사용합니다. |
| `DAILY_BAR_ERROR_RETRY_SEC` | `60` | 숫자(초, 코드 최소 15초) | 일봉 API 실패 종목의 재시도 대기시간입니다. 실패 캐시 동안 같은 종목의 반복 호출을 억제하고 신규 진입 평가는 fail-closed 합니다. |
| `ENTRY_MAX_DAILY_CHANGE_PCT` | `25` | 양수 숫자(%) | 당일 상승률이 +25% 이상이면 신규 매수에서 제외합니다. 보유 종목 매도에는 적용하지 않습니다. |
| `ENTRY_MIN_DAILY_CHANGE_PCT` | `-15` | 음수 숫자(%) | 당일 하락률이 -15% 이하이면 신규 매수에서 제외합니다. 등락률 확인 실패 시 신규 매수는 보류합니다. |

현재 사용자 CUSTOM YAML의 활성 조건은 매수 `RSI14 > 30`, 매도 `RSI14 >= 50`입니다. `STRATEGY_EVALUATION_MODE=CUSTOM`은 운영자가 직접 설정하고 `reload_config.sh`로 적용한 선택값이며, KIS10 비활성은 의도된 구성입니다. SMA20 및 거래량 조건은 YAML에서 주석 처리되어 비활성입니다. `strategy_profiles.yaml`의 BUY/SELL 기준은 각각 `min_votes=1`, `min_score=20`이며, 실제 투표 참여 전략은 각 방향의 `strategies` 목록으로 결정됩니다.

## 시간 · 주문 수명주기

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `ENTRY_START_TIME` | `09:10` | KST HH:MM, 24시간제 | KST 기준 신규 매수 판단 시작 시각(HH:MM)입니다. 거래소 실제 주문 가능 여부와는 별개입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `ENTRY_END_TIME` | `15:10` | KST HH:MM, 24시간제 | KST 기준 신규 매수 판단 종료 시각(HH:MM)입니다. 장 마감 동시호가/증권사 정책을 확인해 보수적으로 설정하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `AUTO_SHUTDOWN_TIME` | `15:10` | KST HH:MM, 24시간제 | KST 기준 자동 종료 시각(HH:MM)입니다. 미체결 주문 취소 요청과 최종 대사를 수행하지만 보유 주식을 자동 청산하지는 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC` | `180` | 숫자(초, 코드 최소 180초) | 종료 중 미체결 취소 완료를 확인하기 위해 기다리는 최대 시간(초)입니다. 시간 초과 시 주문 상태가 불확실할 수 있으므로 파일을 유지하고 HTS 확인이 필요합니다. |
| `STOP_GRACE_TIMEOUT_SEC` | `240` | 정수 60~240초 | stop.sh가 SIGTERM 이후 프로세스 종료를 기다리는 시간입니다. systemd `TimeoutStopSec=300`보다 짧게 유지하며 시간 초과 시 새 프로세스 시작을 차단합니다. |
| `SHUTDOWN_CANCEL_POLL_SEC` | `5` | 숫자(초); config/settings.py에서 추가 범위 검증 | 종료 중 미체결 주문 상태를 다시 조회하는 간격(초)입니다. 너무 짧으면 API 호출 제한에 걸릴 수 있습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `ORDER_TIMEOUT_SEC` | `60` | 숫자(초); config/settings.py에서 추가 범위 검증 | 하위 호환용 공통 타임아웃(초)입니다. 새 BUY/SELL 값이 없을 때 매도 기본값이며 매수는 2배입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `BUY_ORDER_TIMEOUT_SEC` | `300` | 숫자(초, 코드 최소 300초) | 매수 미체결 취소 기준(초)입니다. 기존 .env의 180초도 코드에서 최소 300초로 보정합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `SELL_ORDER_TIMEOUT_SEC` | `300` | 숫자(초, 코드 최소 300초) | 매도 미체결 취소 기준(초)입니다. 기존 .env의 180초도 코드에서 최소 300초로 보정합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `SCAN_INTERVAL_SEC` | `30` | 숫자(초); config/settings.py에서 추가 범위 검증 | HTS/Builder 신규 진입 후보를 재검색하는 간격(초)입니다. 짧게 설정하면 API 호출량이 늘어납니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `BALANCE_INTERVAL_SEC` | `60` | 숫자(초); config/settings.py에서 추가 범위 검증 | 잔고 및 포지션을 브로커 응답과 대사하는 주기(초)입니다. 재시작 복구와 체결 정합성에 중요합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `ORDER_RECONCILE_INTERVAL_SEC` | `20` | 숫자(초); config/settings.py에서 추가 범위 검증 | 미체결 주문 목록과 로컬 pending 상태를 대사하는 주기(초)입니다. 취소/부분체결 상태 확정에 사용됩니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `UNCERTAIN_RECONCILE_INTERVAL_SEC` | `60` | 숫자(초); config/settings.py에서 추가 범위 검증 | 접수 응답이 불명확한 주문을 당일 전체 주문/체결내역과 대조하는 주기(초)입니다. 불명확 주문은 자동 재전송하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `UNCERTAIN_AUTO_RESOLVE_MIN_AGE_SEC` | `300` | 숫자(초); config/settings.py에서 추가 범위 검증 | 운영자 확인 알림을 판단하는 최소 경과시간(초)입니다. 이 시간만 지났다고 잠금을 자동 해제하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `UNCERTAIN_AUTO_RESOLVE_CHECKS` | `3` | 문자열/숫자; 아래 설명과 config/settings.py 기준 | 불명확 주문 알림을 위한 성공 조회 횟수 기준입니다. 기준을 충족해도 KIS 내역이 확인되지 않으면 자동 해제하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `ORPHAN_NOTICE_TTL_SEC` | `180` | 숫자(초); config/settings.py에서 추가 범위 검증 | 주문번호의 로컬 pending 등록보다 먼저 도착한 체결통보를 보류하는 최대 시간(초)입니다. 만료 시에도 주문/잔고 대사는 별도로 필요합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `UNCERTAIN_TELEGRAM_ALERT_INTERVAL_SEC` | `180` | 숫자(초); config/settings.py에서 추가 범위 검증 | 미해결 주문에 대해 Telegram 경고를 반복 발송하는 간격(초)입니다. Telegram 설정이 필요합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `QUOTE_STALE_SEC` | `5` | 숫자(초); config/settings.py에서 추가 범위 검증 | 캐시 시세를 최신으로 인정하는 최대 나이(초)입니다. 오래된 시세로 신규 주문을 결정하지 않도록 보수적으로 설정하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `QUOTE_REST_MIN_INTERVAL_SEC` | `5` | 숫자(초); config/settings.py에서 추가 범위 검증 | 같은 종목의 REST 현재가 fallback 요청 사이 최소 간격(초)입니다. 낮출수록 호출량이 증가합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `REENTRY_COOLDOWN_SEC` | `30000` | 정수 초(코드 최소 30000초) | 전량 매도 체결 또는 잔고 대사에서 보유 0이 확인된 후 동일 종목 재매수를 제한합니다. 30,000초는 500분이며 상태 파일에 저장되어 재시작 후 복구됩니다. |
| `SELL_RETRY_COOLDOWN_SEC` | `60` | 숫자(초, 코드 최소 60초) | 미체결 SELL 취소 후 같은 종목 재평가 전 최소 대기시간입니다. 반복 취소 횟수에 따라 지수형 대기시간이 늘어납니다. |
| `SELL_CANCEL_SETTLE_SEC` | `60` | 숫자(초, 코드 최소 60초) | 취소 요청 뒤 REST 주문/잔고 대사와 재평가 전에 확보하는 최소 대기시간입니다. |
| `SELL_NO_BALANCE_RETRY_SEC` | `30` | 숫자(초); config/settings.py에서 추가 범위 검증 | KIS가 잔고 없음/주문가능수량 부족을 명시 거절했을 때 적용하는 재시도 최소 대기시간(초)입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `SELL_RETRY_MAX_BACKOFF_SEC` | `900` | 숫자(초) | 반복 취소 SELL의 지수형 재평가 대기시간 상한입니다. 최대 대기 후에도 매도 신호가 유효하면 재평가를 계속합니다. |
| `BUY_ORDER_POST_SLEEP_SEC` | `60` | 숫자(초); config/settings.py에서 추가 범위 검증 | 매수 주문 접수 후 신규 매수 사이클을 쉬는 시간(초)입니다. 매도/잔고/주문 대사는 계속 실행됩니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `STOP_LOSS_QUOTE_RETRY_SEC` | `5` | 숫자(초); config/settings.py에서 추가 범위 검증 | 손절 주문이 시세/호가 확인 때문에 보류됐을 때 재평가할 간격(초)입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_AUTO_PRICE_MOVE_THRESHOLD_PCT` | `1.0` | 숫자(퍼센트; 2.0은 2%) | AUTO에서 기준가 대비 절대 변동률이 이 값(%) 이상이면 추격 위험으로 매수를 보류하거나 매도 모드를 변경하는 임계값입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `SELL_START_TIME` | `09:00` | KST HH:MM, 24시간제 | KST 기준 매도 주문을 판단할 수 있는 시작 시각(HH:MM)입니다. 신규 매수 시간과 독립적입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `SELL_END_TIME` | `15:20` | KST HH:MM, 24시간제 | KST 기준 매도 주문 판단 종료 시각(HH:MM)입니다. 증권사 주문 가능 시간 및 장 마감 정책을 확인하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_EVENT_EXIT_QUOTE_MAX_AGE_SEC` | `5` | 숫자(초); config/settings.py에서 추가 범위 검증 | 실시간 청산 worker가 허용하는 WebSocket 시세 최대 나이(초)입니다. 오래된 시세로는 주문하지 않고 다음 시세/REST 대사를 기다립니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 보유 한도 · 일일 위험 · 수량

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `MAX_POSITIONS` | `3` | 정수(최소 1) | 동시에 보유하도록 허용하는 최대 종목 수입니다. 계좌/종목별 주문가능 현금과 미수 없는 매수한도를 확인하고, 미체결 예약금을 차감한 뒤 남은 슬롯 수(MAX_POSITIONS - 보유/매수예약 종목 수)로 종목별 예산을 계산합니다. |
| `buy_rejection_blocks` 상태 | 상태 파일 | 종목코드별 당일 기록 | 주문가능금액 명시 거절 후 예수금 fallback 주문도 실패하거나 안전한 축소수량이 없으면 해당 종목을 당일 잠급니다. 상태 파일에 저장되어 재시작 후에도 유지되고 KST 날짜 변경 시 해제됩니다. |
| `매수가능조회` | `inquire-psbl-order` | 종목코드/주문가격/주문구분 | 국내주식 종목별 주문 가능 현금을 확인합니다. `ord_psbl_cash`뿐 아니라 `nrcvb_buy_amt`(미수 없는 매수금액), `nrcvb_buy_qty`(미수 없는 매수수량)를 함께 사용해 신용/미수 한도를 넘지 않도록 합니다. KIS 오류는 `msg_cd`와 `msg1`을 함께 기록하며, 구체적인 현금/증거금 부족 문구가 확인된 경우에만 fallback 후보로 분류합니다. |
| `ALLOW_ADD_TO_EXISTING_POSITION` | `false` | true / false | 이미 보유한 종목에 추가매수를 허용할지 여부입니다. 기본 false이며, 예수금 갱신 지연 중 동일 종목 반복 매수/현금 초과를 막기 위해 false를 권장합니다. true로 설정하면 같은 종목 추가매수가 가능하지만 현금·위험 관리 책임은 운영자에게 있습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MAX_TRADES_PER_DAY` | `5` | 정수(단위는 변수명 참조) | 일일 최대 매수 거래횟수입니다. 실제 매수 첫 체결을 기준으로 집계하며 당일 상태 파일을 임의 삭제하지 마세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MAX_DAILY_LOSS_PCT` | `2.0` | 숫자(퍼센트; 2.0은 2%) | 일일 손실 한도(%)입니다. 한도 도달 시 신규 진입을 제한하는 안전장치이며 손실 방지를 보장하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MAX_CANDIDATES` | `20` | 정수(단위는 변수명 참조) | 각 후보 경로에서 평가할 최대 후보 수입니다. 설정 코드에서 1~20으로 제한됩니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `POSITION_SIZING` | `true` | true / false | true면 계좌 `ord_psbl_cash`를 기본 기준으로 남은 포지션 슬롯과 미체결 매수 예약금을 반영해 수량을 계산합니다. 종목별 매수가능조회 `ord_psbl_cash`와 미수 없는 `nrcvb_buy_amt`/`nrcvb_buy_qty`를 추가 상한으로 적용합니다. 주문가능금액 부족으로 명시 거절되고 주문이력상 미접수가 확인된 경우에만 예수금 기준으로 1회 축소 재계산합니다. |
| `MIN_BUY_ORDER_AMOUNT_KRW` | `10000` | 정수(단위는 변수명 참조) | 계산된 신규 매수 주문 금액의 최소 기준(원)입니다. 이보다 작으면 주문을 보류합니다. 구버전 MIN_ORDER_AMOUNT_KRW 별칭도 지원합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MIN_STOCK_PRICE_KRW` | `0` | 문자열/숫자; 아래 설명과 config/settings.py 기준 | 후보 1주 가격 하한(원)입니다. 0이면 필터 비활성화, 0보다 크면 현재가가 하한 미만인 종목을 제외합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `buy_rejection_blocks` 상태 | 상태 파일 | 종목코드별 당일 기록 | 주문가능금액 명시 거절 후 예수금 fallback 주문도 실패하거나 안전한 축소수량이 없으면 해당 종목을 당일 잠급니다. 상태 파일에 저장되어 재시작 후에도 유지되고 KST 날짜 변경 시 해제됩니다. |
| `BLOCK_NEW_BUY_WHILE_PENDING_BUY` | `true` | true / false | true면 미체결/부분체결/불명확 매수 주문이 있는 동안 다른 종목의 신규 매수를 차단합니다. 중복 매수 위험을 줄입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 주문 방식 · 비용 · 수량

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `ORDER_QTY` | `1` | 정수(단위는 변수명 참조) | POSITION_SIZING=false일 때 사용할 기본 주문 수량(주)입니다. POSITION_SIZING=true이면 계산된 수량이 우선합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_BUY_ORDER_MODE` | `AUTO` | MARKET / BEST_LIMIT / AUTO | 매수 주문 방식입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_SEL_ORDER_MODE` | `BEST_LIMIT` | MARKET / BEST_LIMIT / AUTO | 일반 매도 주문 방식입니다. 손절 설정과 별도로 적용됩니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_STOP_LOSS_ORDER_MODE` | `BEST_LIMIT` | MARKET / BEST_LIMIT / AUTO | 손절 매도 주문 방식입니다. 체결 우선순위와 호가/시세 검증 정책을 함께 고려하세요. 기본 예제는 BEST_LIMIT. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_AUTO_SELECTED_MODE` | `BEST_LIMIT` | MARKET / BEST_LIMIT | AUTO가 급격한 변동 조건에 해당하지 않을 때 선택할 실제 방식입니다. 허용값 MARKET 또는 BEST_LIMIT. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_MARKET_PRICE_BUFFER_PCT` | `1.0` | 숫자(퍼센트; 2.0은 2%) | 시장가 매수 수량 산정에 더하는 가격 상승 여유율(%)입니다. 체결가격을 보장하는 값이 아닙니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_FEE_RATE_PCT` | `0.02` | 숫자(퍼센트; 2.0은 2%) | 손익/예산 추정에 적용하는 매수·매도 수수료율(%)입니다. 예시 0.02는 0.02%이며 실제 계좌 요율을 확인하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_SELL_TAX_RATE_PCT` | `0.20` | 숫자(퍼센트; 2.0은 2%) | 매도 순손익 추정에 추가할 세금/거래비용 비율(%)입니다. 상품·시장·세법에 따라 다를 수 있으므로 실제 적용 요율을 확인하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_SHOW_ORDER_CALCULATION` | `true` | true / false | true면 주문 수량, 추정 가격, 금액, 수수료/세금 계산을 로그에 표시합니다. 계좌 인증정보는 표시하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 청산 기준

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `EXIT_STOP_LOSS_HARD_PCT` | `2.0` | 숫자(퍼센트; 2.0은 2%) | 2차 강제 손절 임계값(%)입니다. 선택 평가 엔진의 신호와 무관하게 적용됩니다. 예: 2.0은 평균매입가 대비 -2% 조건을 뜻합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `EXIT_STOP_LOSS_FIRST_PCT` | `1.5` | 숫자(퍼센트; 2.0은 2%) | 1차 손절 임계값(%)입니다. 선택 평가 엔진의 SELL 신호와 함께 충족해야 합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `EXIT_TAKE_PROFIT_FIRST_PCT` | `1.5` | 숫자(퍼센트; 2.0은 2%) | 1차 익절 임계값(%)입니다. 선택 평가 엔진의 SELL 신호와 함께 충족해야 합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `EXIT_TAKE_PROFIT_HARD_PCT` | `3.0` | 숫자(퍼센트; 2.0은 2%) | 2차 강제 익절 임계값(%)입니다. 선택 평가 엔진의 신호와 무관하게 적용됩니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## Telegram 알림

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `빈 값` | 문자열(계정/비밀정보; 예제 값은 교체 필요) | 선택 사항인 Telegram Bot API 토큰입니다. 빈 값이면 알림이 비활성화됩니다. 실제 값은 .env에만 두고 커밋·공유·로그 출력 금지. |
| `TELEGRAM_CHAT_ID` | `빈 값` | 문자열/숫자; 아래 설명과 config/settings.py 기준 | 알림을 받을 Telegram 개인/그룹 채팅 ID입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 로그 · API 호출 제한

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `LOG_LEVEL` | `DEBUG` | DEBUG / INFO / WARNING / ERROR / CRITICAL | 로그 상세 수준입니다. DEBUG/INFO/WARNING/ERROR/CRITICAL 중 하나를 사용합니다. 운영 기본값은 INFO입니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MAX_CALLS_PER_SEC` | `0.8` | 숫자(초); config/settings.py에서 추가 범위 검증 | 프로그램 내부 REST 호출 제한기의 초당 상한 설정입니다. 코드에서 추가 안전상한을 적용하며 KIS 공식 한도를 의미하지 않습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `MAX_CALLS_PER_MIN` | `40` | 정수(단위는 변수명 참조) | 프로그램 내부 REST 호출 제한기의 분당 상한 설정입니다. 계정별 실제 KIS 제한을 확인하고 보수적으로 설정하세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `RATE_LIMIT_SAFETY_MARGIN` | `0.80` | 숫자 0.1~1.0 | 내부 호출 상한에 곱하는 여유 비율(0.1~1.0)입니다. 0.8은 설정 상한의 80% 수준으로 제한합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_RATE_LIMIT_COOLDOWN_SEC` | `15` | 숫자(초); config/settings.py에서 추가 범위 검증 | 명시적 호출 제한 응답 후 추가 대기 시간(초). 기본 10초입니다. KIS의 명시적 호출 제한(EGW00201/EGW00215/HTTP 429) 응답 이후 REST 요청을 추가로 늦춥니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_TR_RATE_LIMITS_JSON` | `{}` | JSON 객체: TR ID별 `per_sec`, `per_min` 양수 | 일봉/현재가/잔고/주문·체결/매수가능/HTS 조건검색 TR별 보수적 기본 상한이 내장되어 있으며 이 JSON으로 덮어쓸 수 있습니다. 수치는 KIS 공식 한도가 아니라 프로그램 내부 안전 상한입니다. |

## 휴장일

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `KIS_PAPER_HOLIDAY_FALLBACK` | `WEEKDAY` | WEEKDAY / STRICT | 모의투자 휴장일 API가 지원되지 않을 때의 대체 판정입니다. WEEKDAY=요일/로컬 달력 기반, STRICT=확실하지 않으면 시작 중단. 기본 WEEKDAY는 임시 휴장일을 놓칠 수 있습니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_PAPER_HOLIDAYS` | `빈 값` | 문자열/숫자; 아래 설명과 config/settings.py 기준 | 모의투자 달력에 추가할 임시 휴장일 목록입니다. YYYYMMDD 또는 YYYY-MM-DD를 쉼표로 구분합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## worker · WebSocket

| 변수 | `.env.example` 기본값 | 허용값/형식 | 의미·주의사항 |
|---|---|---|---|
| `BUILDER_EVAL_WORKERS` | `2` | 정수 1~2 | Builder 후보 일봉 조회/전략 평가의 worker 수입니다. 코드에서 1~2로 제한합니다. 병렬 REST 호출도 공용 RateLimiter를 통과합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `EXIT_YAML_EVAL_WORKERS` | `2` | 정수 1~2 | 보유 종목 YAML 청산 일봉 조회/평가 worker 수입니다. 코드에서 1~2로 제한하며 상태 갱신은 main thread에서 수행합니다. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |
| `KIS_WS_MAX_PRICE_SUBSCRIPTIONS` | `30` | 정수 1 이상 | WebSocket 가격 구독 상한입니다. 정수 1 이상을 사용하며 기본값 30입니다. 보유 종목을 우선 구독하고 조건검색 후보는 이 상한 안에서 교체합니다. KIS 계정별 구독 제한보다 높게 설정하지 마세요. 변경 후 pytest/smoke 및 모의투자 로그 확인 권장. |

## 안전한 설정 변경 절차

1. `.env`를 백업하고 한 번에 한 범주씩 수정합니다.
2. `./scripts/reload_config.sh --validate-only`로 `.env`와 YAML을 검증합니다.
3. `pytest -q`, `python smoke_test.py`, `./tests/kis_strategy_test.sh --offline --sample-size 10 --seed 42`를 실행합니다.
4. 온라인 시세 테스트는 `KIS_PAPER=true`에서 주문/취소 API를 호출하지 않는 범위로 수행합니다.
5. HTS/브로커 주문 내역을 대조하기 전 상태·불명확 주문·취소 감사 JSON을 삭제하지 마세요.

## 추가 구현 주의사항

- `KIS_PAPER_HOLIDAY_FALLBACK=WEEKDAY`는 임시 휴장일을 놓칠 수 있습니다. 엄격한 운영은 `STRICT`와 해당 연도 KRX 달력 파일을 함께 확인하세요.
- 손절 `AUTO`는 유효하고 신선한 시세/호가가 없으면 주문을 보류할 수 있어 손절이 지연될 수 있습니다.
- 호출 제한 환경변수는 프로그램 내부 제한이며 KIS 계정별 공식 한도를 의미하지 않습니다.


## 설정 간 상호작용과 실제 적용 위치

다음 내용은 단일 변수의 설명만으로 놓치기 쉬운 조합 규칙입니다. 기본값은 `.env.example`에 기록된 예제값이며, 생략 시 코드 내부 기본값과 항상 같지는 않습니다.

### 1. API 환경과 주문 전송은 별개

- `KIS_PAPER=true`는 모의투자 API URL/TR 식별자를 선택합니다. `false`는 실전 환경을 선택하므로 자격증명도 해당 환경에서 발급한 값이어야 합니다.
- `KIS_ENABLE_TRADING=false`는 주문 전송을 막는 DRY-RUN 게이트입니다. `KIS_PAPER=true`여도 `KIS_ENABLE_TRADING=true`이면 모의투자 서버에 주문을 보낼 수 있습니다.
- `KIS_PAPER=false`와 주문 전송 활성화가 동시에 설정되면 `setup.sh`는 `SETUP_ALLOW_LIVE_TRADING=YES` 명시 없이 설치 후 자동 시작을 허용하지 않습니다. 이 우회 변수를 설정해도 실전 주문의 위험은 사라지지 않습니다.
- 관련 구현: `config/settings.py`의 설정 로딩/검증, `brokers/kis/client.py`의 API 환경과 주문 요청, `trading/orders.py`의 주문 전송 게이트.

### 2. 런타임 경로와 상태 보존

- `KIS_DATA_DIR`는 상태·토큰·미확정 주문·취소 이력·계좌 주문 잠금·휴장일 캐시의 기본 디렉터리를 정합니다. `STATE_FILE`, `KIS_TOKEN_CACHE_FILE`, `UNCERTAIN_ORDERS_FILE`, `CANCEL_ORDERS_FILE`, `ACCOUNT_ORDER_LOCK_FILE`, `HOLIDAY_CACHE_FILE` 등 개별 경로 설정은 각 파일의 위치를 따로 재정의합니다.
- `KIS_LOG_DIR`는 일자별 로그의 위치를 정합니다. `STRATEGY_SIGNAL_LOG`가 비어 있으면 전략 신호 로그는 기본 로그 디렉터리 아래에 생성됩니다.
- 상대 경로는 `config/paths.py`를 통해 프로젝트 루트를 기준으로 해석합니다. 경로를 변경해도 이전 위치의 파일이 자동 복사되거나 병합되지 않습니다. 이전 파일과 새 파일이 분리되면 포지션·쿨다운·불명확 주문 잠금이 서로 다르게 보일 수 있으므로, 종료 후 백업하고 파일별로 이전해야 합니다.
- `MANUAL_STOP_FILE` 및 시작 표식 경로는 watchdog 동작에 영향을 줍니다. 수동 중지 표식은 의도적인 정지와 자동 재시작을 구분하는 안전 상태이므로 임의 삭제하지 마세요.

### 3. 신규 후보 경로와 전략 파일

- `BUILDER_SYMBOLS_FILE`은 후보 종목 목록이고, `STRATEGY_PROFILES_FILE`은 한투 10개 전략의 BUY/SELL 참여 목록과 점수 기준을 정의합니다. `ENTRY_STRATEGY_FILE`과 `EXIT_STRATEGY_FILE`은 자체 전략의 매수·매도 지표를 각각 정의합니다.
- `STRATEGY_BUILDER_MAX_SYMBOLS`, `BUILDER_EVAL_WORKERS`, `EXIT_YAML_EVAL_WORKERS`는 평가량과 동시 요청 수에 영향을 줍니다. worker 수를 늘려도 KIS 호출 제한이 늘어나는 것은 아닙니다. 공용 제한기를 사용하더라도 너무 많은 평가 대상은 응답 지연이나 호출 제한을 일으킬 수 있습니다.
- 한투 10개 전략은 `strategy_profiles.yaml`의 BUY/SELL 목록에 등록된 방향에서만 투표합니다. 목록 밖의 전략은 해당 방향의 점수·투표에 반영되지 않습니다.

### 4. 주문 수명주기와 불명확 결과

- 주문 타임아웃은 곧 주문 실패를 의미하지 않습니다. 서버가 주문을 접수했지만 응답이 유실되었을 수 있으므로, `UNCERTAIN_ORDERS_FILE`에 기록된 항목은 HTS 주문·체결·미체결·잔고와 대조해야 합니다.
- 취소 API가 성공 응답을 반환해도 원주문이 실제 미체결 목록에서 사라졌는지 별도로 확인해야 합니다. `CANCEL_ORDERS_FILE`은 취소 요청과 후속 대사 기록에 사용되므로 확인 전 지우지 마세요.
- `ACCOUNT_ORDER_LOCK_FILE`이 활성화되면 계좌/인증 오류가 해결되어도 재시작만으로 주문이 재개되지 않습니다. KIS Developers 신청 계좌·HTS ID·App Key/Secret·CANO·상품코드를 검증하고 REST 잔고 및 WebSocket 연결을 확인한 뒤 운영자가 해제 스크립트를 실행해야 합니다. 잠금 중 기존 미체결 주문은 자동 취소되지 않으므로 HTS에서 직접 확인하세요.
- `MAX_POSITIONS`, `MAX_TRADES_PER_DAY`, `MAX_DAILY_LOSS_PCT`, 매수 예산/현금 버퍼 설정은 서로 다른 진입 제한입니다. 이 값들이 설정되어 있어도 갭·슬리피지·수수료·세금·체결 지연 때문에 계좌 전체 손실 상한을 보장하지는 않습니다.
- `AUTO_SHUTDOWN_TIME`은 신규 실행/정리 루틴의 시간 기준이며 보유 종목을 자동으로 전량 매도한다는 뜻이 아닙니다. 종료 시 미체결 취소와 최종 대사가 실패하면 감사/불명확 상태가 남을 수 있으며, 이는 안전을 위한 의도된 동작일 수 있습니다.

### 5. 호출 제한과 실시간 시세

- `MAX_CALLS_PER_SEC`, `MAX_CALLS_PER_MIN`, `RATE_LIMIT_SAFETY_MARGIN`, `KIS_RATE_LIMIT_COOLDOWN_SEC`는 공통 호출 제한기 설정입니다. `KIS_TR_RATE_LIMITS_JSON`은 TR별 추가 제한을 지정합니다. 주문/취소 및 잔고·주문대사는 전략 일봉 조회보다 높은 우선순위를 가집니다. 이 값들은 KIS의 계정별 공식 한도나 보장을 뜻하지 않습니다.
- `KIS_WS_MAX_PRICE_SUBSCRIPTIONS`는 실시간 가격 구독의 상한입니다. 보유 종목이 우선 구독될 수 있으며, 한도를 초과하는 후보는 실시간 가격을 받지 못할 수 있습니다.
- 손절 `AUTO` 정책은 시세가 오래되었거나 호가/잔량이 불충분한 경우 가격을 신뢰하지 않고 주문을 보류할 수 있습니다. 이 경우 손절 신호가 발생했어도 실제 주문 시점은 지연될 수 있습니다.

### 6. 로그·알림·휴장일

- `LOG_LEVEL`은 로그 상세 수준을 정합니다. DEBUG는 운영 로그를 크게 늘릴 수 있으며, 로그에는 종목·주문 상태와 같은 운영 정보가 포함될 수 있습니다. 자격증명은 로그에 남기지 말고 로그 파일 권한을 제한하세요.
- `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`는 함께 설정해야 합니다. Telegram 전송 실패 로그는 예외 전문/요청 URL/응답 본문/메시지 전문을 기록하지 않도록 처리합니다. Bot Token은 로그나 공유 파일에 남기지 마세요.
- `KIS_PAPER_HOLIDAY_FALLBACK=WEEKDAY`는 휴장일 API가 불완전한 경우 평일을 영업일로 추정할 수 있습니다. 특별 휴장일을 놓칠 수 있으므로 연도별 달력과 KIS/거래소 공지를 확인하세요. `STRICT`는 불확실한 경우 시작을 중단하는 보수적 선택입니다.

### 7. 설정 변경 후 검증 순서

1. `.env`와 상태/불명확 주문/취소 감사 파일을 백업합니다.
2. 한 번에 한 설정 또는 한 그룹만 변경합니다.
3. `./scripts/reload_config.sh --validate-only`로 설정과 YAML을 확인합니다.
4. `pytest -q`, `python smoke_test.py`, `./tests/kis_strategy_test.sh --offline --sample-size 10 --seed 42`를 실행합니다.
5. 온라인 확인이 필요하면 `KIS_PAPER=true`를 유지하고, 테스트 목적상 주문·취소 API를 보내지 않도록 합니다.
6. 실제 주문·체결·미체결·잔고는 HTS와 프로그램의 일자별 로그를 대조합니다. 불명확 주문 잠금은 원인 확인 없이 제거하지 않습니다.
