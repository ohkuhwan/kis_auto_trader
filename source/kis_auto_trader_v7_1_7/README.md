# KIS Auto Trader V7.1.7 운영 안내

V7.1.7은 **모의투자 우선** 버전입니다. `.env`에서 `KIS_PAPER=true`를 유지하고, 주문 동작을 검증하기 전에는 `KIS_ENABLE_TRADING=false`를 권장합니다.

## 1. 종목 선정과 평가 엔진은 별도 설정입니다

- `ENTRY_MODE=HTS`: HTS 서버 저장 조건검색 결과만 후보로 사용합니다.
- `ENTRY_MODE=BUILDER`: `BUILDER_SYMBOLS_FILE`의 종목만 후보로 사용합니다.
- `ENTRY_MODE=ALL`: HTS 후보와 파일 후보를 합집합으로 만들고 중복을 제거합니다.
- `STRATEGY_EVALUATION_MODE=KIS10`: 한투 10개 전략 엔진만 사용합니다.
- `STRATEGY_EVALUATION_MODE=CUSTOM`: `my_entry_strategy.kis.yaml` 및 `my_exit_strategy.kis.yaml`만 사용합니다.
- `STRATEGY_EVALUATION_MODE=ALL`: 두 엔진을 독립 평가하고 매수는 AND, 매도는 OR로 결합합니다.

## 2. BUY / SELL 결합 규칙

| 모드 | 매수 | 전략 기반 매도 |
|---|---|---|
| `KIS10` | 한투 10개 전략의 BUY 판정 | 한투 10개 전략의 SELL 판정 |
| `CUSTOM` | 사용자 정의 진입 YAML의 BUY | 사용자 정의 청산 YAML의 SELL |
| `ALL` | 두 엔진 모두 BUY여야 승인 | 어느 한 엔진이라도 SELL이면 신호 |

`ALL`의 BUY 점수는 두 엔진 점수 중 낮은 값으로 후보 우선순위를 정합니다. 서로 다른 점수 척도를 합산하지 않습니다. 점수는 수익 확률이 아닙니다.

## 3. 손익절 우선순위

- 1차: 전략 SELL 신호와 `EXIT_STOP_LOSS_FIRST_PCT`(손실 방향) 또는 `EXIT_TAKE_PROFIT_FIRST_PCT`(수익 방향)를 함께 충족해야 합니다.
- 2차: `EXIT_STOP_LOSS_HARD_PCT` 또는 `EXIT_TAKE_PROFIT_HARD_PCT`에 도달하면 전략 신호와 무관하게 강제 청산을 요청합니다.
- 주문 제출, 부분체결, 미체결 대사, 잔고 동기화, 재진입 쿨다운, 주문 중복 방지, API 호출 제한 보호는 별도 안전장치로 유지합니다.

## 4. 설정 파일

- `.env`: 후보 출처, 평가 엔진, 공통 운영/손익절 설정
- `config/strategy_profiles.yaml`: 한투 10개 전략의 BUY/SELL 참여 목록과 점수 기준
- `config/my_entry_strategy.kis.yaml`: 사용자 정의 매수 지표와 진입 조건
- `config/my_exit_strategy.kis.yaml`: 사용자 정의 매도 지표와 청산 조건
- `config/strategies/strategy_01_*.kis.yaml` ~ `strategy_10_*.kis.yaml`: 한투 10개 내부 전략 정의

주의: `config/strategies/strategy_*.kis.yaml`의 `risk.stop_loss`, `risk.take_profit`, `risk.trailing_stop`은 V7.1.2 기준으로 설명/호환용이며, 현재 포지션 청산 기준에 적용되지 않습니다. 실제 1차/2차 손익절은 `.env`의 `EXIT_STOP_LOSS_FIRST_PCT`, `EXIT_STOP_LOSS_HARD_PCT`, `EXIT_TAKE_PROFIT_FIRST_PCT`, `EXIT_TAKE_PROFIT_HARD_PCT`가 관리합니다. 전략별 risk 적용은 별도 구현·테스트 후 결정합니다.

각 YAML의 주석은 지원 스키마와 항목의 의미를 설명합니다. `indicators[].alias`는 계산된 지표값을 조건식에서 참조하는 이름이며, 새로운 `type`을 추가하려면 `strategies/builder_engine/yaml_engine.py`에 계산 구현과 테스트를 추가해야 합니다.

## 5. 설정 검증과 테스트

```bash
./scripts/reload_config.sh --validate-only
.venv/bin/python -m pytest -q
.venv/bin/python smoke_test.py
```

`KIS10` 모드에서는 사용자 정의 YAML 파일이 필수 경로가 아닙니다. `CUSTOM`과 `ALL`에서는 진입·청산 YAML이 검증됩니다. 실제 KIS 서버의 API 제한 및 주문·체결 동작은 모의투자에서 별도로 확인하세요.

자세한 변경 사항: [CHANGELOG_V7.1.7.md](docs/release/CHANGELOG_V7.1.7.md)


## 6. V7.1.6 추가 안전장치

- `REENTRY_COOLDOWN_SEC`는 최소 30,000초(500분)입니다. 전량 매도 체결 또는 잔고 대사에서 보유 0이 확인된 시점부터 종목별로 기록하며 재시작 후에도 복구합니다.
- `ENTRY_MAX_DAILY_CHANGE_PCT=25`, `ENTRY_MIN_DAILY_CHANGE_PCT=-15`로 당일 등락률이 +25% 이상 또는 -15% 이하인 신규 매수 후보를 제외합니다. 등락률 확인 실패 시 신규 매수는 보류하며 기존 보유 종목 청산에는 적용하지 않습니다.
- 미체결 SELL 취소 후 재평가 대기는 최소 60초이며, 반복 취소 시 최대 900초까지 지수형으로 늘어납니다.
- 일봉 데이터는 기본 300초 캐시로 재사용합니다. CUSTOM은 YAML 지표 lookback에 필요한 일봉만 요청하고, KIS10/ALL은 strategy_profiles의 BUY/SELL 참여 전략 중 최대 lookback을 사용합니다. `BUILDER_OBSERVE_ALL_STRATEGIES=true`이면 모든 전략 lookback을 포함합니다. API 실패 종목은 60초 재시도 대기 캐시를 적용합니다.
- HTS와 사용자 종목 목록은 합집합/중복 제거 후 평가합니다. 후보 상한을 넘으면 HTS 응답 원본 순서 대신 순환 평가하고, 회차 내 승인 후보는 점수순으로 선택합니다.
- Telegram 전송 실패 로그에는 Bot Token, Chat ID, 요청 URL, 응답 본문 또는 메시지 전문을 남기지 않습니다.
- `SHUTDOWN_CANCEL_CONFIRM_TIMEOUT_SEC`는 최소 180초, `BUY_ORDER_TIMEOUT_SEC`와 `SELL_ORDER_TIMEOUT_SEC`는 각각 최소 300초입니다. 기존 `.env`에 더 짧은 값이 남아 있어도 코드에서 최소값을 적용합니다.
- `MAX_POSITIONS`에 따라 매수 예산을 동적으로 배분합니다. 기본 기준은 계좌 `ord_psbl_cash`이며, 종목별 `inquire-psbl-order`의 `ord_psbl_cash`, 미수 없는 `nrcvb_buy_amt`/`nrcvb_buy_qty`를 추가 상한으로 적용합니다. 미체결 매수 예약금 차감 후 남은 슬롯 수로 나눕니다. 주문가능금액 부족으로 명시 거절되고 주문이력에서 미접수가 확인된 경우에만 예수금 기준으로 더 작은 수량을 1회 재계산합니다. 두 번째 실패/축소 불가 종목은 당일 잠금이 저장되어 재시작 후에도 유지됩니다. 부족 오류는 `msg_cd`와 `msg1`을 함께 기록하며, 특정 국내주식 오류코드를 추측해 하드코딩하지 않습니다.
- TR별 API 호출 제한은 코드의 보수적 기본값을 사용하고 `KIS_TR_RATE_LIMITS_JSON`으로 덮어쓸 수 있습니다. 이 값은 KIS 공식 한도가 아니라 프로그램 내부 상한입니다.
- 종료 시 `stop.sh`는 기본 240초까지 graceful 종료를 기다리며 systemd `TimeoutStopSec=300`으로 맞춥니다. 종료 확인이 안 되면 새 프로세스 시작을 허용하지 않습니다.
- 체결통보는 전용 worker에서 main loop와 분리해 처리합니다. 잔고 snapshot 반영과 체결 worker 상태 갱신은 공용 lock으로 직렬화합니다.
- `reload_config.sh`/실제 시작 시 비밀값을 제외한 주요 운영 설정 변경이 `json/config_snapshot.json`과 control 로그에 기록됩니다.
- root로 실행되는 systemd watchdog의 `watchdog_*.log` 및 `start_request_*.log`도 로그 디렉터리 소유자/그룹 기준으로 생성·정규화해 서비스 계정과 권한을 공유합니다. `request_start.sh`가 `start_request_*.log`에 직접 감사 이벤트를 기록합니다.

## 이전 버전 자료

V7.0.2 진단 로그 검토 기록은 [V7.0.2 로그 검토](docs/validation/V7.0.2_LOG_REVIEW_20261026.md)에 보존되어 있습니다.


자동 모니터링은 KST 08:00 이전에 자동 시작되지 않습니다. 명시적 수동 시작은 이 제한의 대상이 아니며, 평일·휴장일·수동 중지·종료 시각 정책은 그대로 적용됩니다.

## 7. 계좌/API 인증 오류와 영구 주문 잠금 (V7.1.7)

다음 KIS 응답은 종목별 오류가 아니라 계좌 또는 API 신청정보 단위 오류로 취급합니다.

| 오류 코드 | 의미/조치 |
|---|---|
| `40910000` | 만료된 모의투자 계좌 등 주문 불가 계좌. 새 모의계좌 발급 및 API 재신청 확인 |
| `IGW00002` | 인증 시점 계좌번호와 요청 계좌번호 불일치. App Key/Secret과 CANO 연결 확인 |
| `90070000` | 모의투자 처리계좌 ID와 사용자정보 불일치. HTS ID 변경 여부 및 기존 신청정보 삭제·재신청 확인 |

위 오류가 REST 응답에서 확인되면 `json/account_order_lock.json`에 잠금 상태를 원자 저장합니다. 잠금 중에는 신규 BUY와 SELL 제출을 모두 차단합니다. 프로그램을 재시작해도 잠금은 유지되며, 잔고·미체결·체결 대사는 계속 수행합니다. watchdog는 프로세스가 살아 있어도 `degraded / account_order_lock`로 기록하고 자동 재시작하지 않습니다.

**잠금 해제 전 확인 순서**

1. 프로그램을 중지하고 KIS Developers에서 현재 모의투자 계좌가 Open API 신청정보에 연결되어 있는지 확인합니다.
2. 새 모의투자 계좌가 국내주식 계좌인지 확인하고, HTS ID·App Key·App Secret·CANO·계좌상품코드가 같은 신청정보를 가리키는지 대조합니다.
3. 새 계좌에 연결된 App Key/Secret을 `.env`에 반영합니다. App Key/Secret, CANO, 상품코드 또는 HTS ID가 바뀌면 이전 인증 캐시를 재사용하지 않도록 fingerprint가 달라집니다.
4. 기존 `json/kis_tokens.json`을 백업 후 삭제하거나 이름을 바꿉니다. 프로그램 시작 시 REST 접근 토큰과 WebSocket 승인키를 각각 새로 발급받게 합니다.
5. 주문 기능을 켜기 전에 REST 잔고 조회가 새 계좌의 보유·예수금으로 응답하는지, WebSocket 연결과 체결통보 구독이 정상인지 확인합니다.
6. 계좌/API 신청정보 검증이 끝난 경우에만 다음 명령으로 잠금을 수동 해제합니다.

```bash
python3 scripts/unlock_account_order_lock.py \
  --confirm-account-verified \
  --reason "새 모의계좌 API 신청 및 REST·WebSocket 검증 완료"
```

잠금 파일을 직접 삭제하는 방식은 권장하지 않습니다. 해제 스크립트는 기존 잠금 기록을 보존하고 해제 시각·사유를 기록합니다. 잠금 파일이 손상되었거나 읽을 수 없으면 안전을 위해 주문을 차단합니다.

## 8. HTS 조건검색 결과 0건

- `psearch-title`에서 받은 `seq`를 그대로 사용합니다. `seq="0"`도 유효한 값일 수 있으므로 임의로 거부하지 않습니다.
- `psearch-result`가 `MCA05918`을 반환하면 KIS 조건검색 결과가 0건인 상태로 정규화합니다. 이를 인증/서버 장애로 오인해 반복 오류로 기록하지 않습니다.
- 조건명 자체를 찾지 못하거나 응답 구조가 잘못된 경우는 별도 설정/조회 오류로 기록합니다.

## 9. 인증 캐시 범위

REST access token과 WebSocket approval key는 별도로 발급·관리됩니다. 캐시 fingerprint에는 API 환경, App Key, App Secret의 해시, CANO, 계좌상품코드, HTS ID를 포함합니다. 계좌 또는 인증 구성이 바뀌면 이전 캐시가 자동 재사용되지 않습니다. 실제 키·토큰·승인키는 로그에 출력하거나 저장소에 커밋하지 마세요.
