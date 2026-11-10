# V7.1.6 변경사항

## 기준 및 적용 원칙
- V7.1.5 전체 소스를 기준으로 기존 기능과 상태 저장 형식을 보존하는 최소 범위 수정입니다.
- 모의투자 우선이며 실전 주문 전환을 의미하지 않습니다.
- `STRATEGY_EVALUATION_MODE=CUSTOM`은 운영자가 `.env`에서 선택하고 `reload_config.sh`로 적용한 정상 설정으로 취급합니다.

## 1. 주문가능금액 및 종목별 매수 가능 확인
- 기본 포지션 예산은 계좌 `ord_psbl_cash` 기준으로 계산합니다.
- 국내주식 `inquire-psbl-order`(모의 `VTTC8908R`, 실전 `TTTC8908R`)를 종목/주문가격/주문구분별로 조회합니다.
- 종목별 `ord_psbl_cash`와 `nrcvb_buy_amt`, `nrcvb_buy_qty`를 상한으로 적용합니다. `nrcvb_*`는 한국투자증권 공식 샘플에서 미수 없는 매수금액/수량으로 표기된 필드입니다.
- 주문 POST의 명시적 현금/증거금 부족 메시지만 fallback 후보로 분류합니다. 공식 샘플/공개 문서에서 국내 현금매수 전체에 공통 적용되는 단일 부족 오류코드를 확인할 수 없어 코드를 임의로 하드코딩하지 않았습니다. KIS `msg_cd`와 `msg1`을 모두 기록하고 구체적인 부족 문구만 fallback 대상으로 분류합니다.
- 명시 거절 후 당일 주문이력에서 동일 종목/방향/수량 주문이 없음을 확인한 경우에만 최신 잔고와 매수가능조회로 예수금 상한 재계산을 수행합니다.
- 예수금 fallback은 최초 주문보다 작은 수량으로 1회만 재주문합니다. 접수 여부 불명확, 주문이력 조회 실패/후보 존재는 재주문하지 않고 불명확 주문 잠금을 유지합니다.
- fallback 2차 주문 실패 또는 안전한 축소수량 산출 불가 시 해당 종목을 당일 잠급니다. 이 잠금은 state 파일에 저장되어 재시작 후에도 유지되고 다음 KST 거래일에 해제됩니다.
- 기존 미체결 예약금, `MAX_POSITIONS`, 수수료, 가격 버퍼 및 포지션 사이징을 유지합니다.

## 2. 로그 분석 후 운영 보완
1. **보안:** urllib3/requests/httpx/httpcore DEBUG 로그를 WARNING으로 제한하고, KIS 요청 로그의 계좌번호를 마스킹합니다. Telegram 전송 실패 로그는 URL/토큰/본문을 출력하지 않습니다.
2. **API 호출량:** CUSTOM YAML 지표 lookback에 필요한 일봉 수만 조회합니다. KIS10/ALL은 strategy_profiles의 BUY/SELL 참여 전략 최대 lookback(+5봉)을 계산하며, `BUILDER_OBSERVE_ALL_STRATEGIES=true`이면 전체 전략을 포함합니다. 현재 프로필에서 기본 참여전략은 65일, 전체 관찰은 257일입니다. 일봉 캐시 기본 하한은 300초이며 API 실패 종목은 60초 재시도 대기 캐시를 적용합니다.
3. **TR별 호출 제한:** 일봉/현재가/잔고/주문·체결/매수가능/HTS 조건검색에 보수적인 프로그램 내부 기본 limiter를 추가했습니다. `.env`의 `KIS_TR_RATE_LIMITS_JSON`으로 덮어쓸 수 있습니다. 이는 KIS 공식 한도 수치가 아닙니다.
4. **재시작:** graceful stop 대기 기본 240초, systemd `TimeoutStopSec=300`으로 연장합니다. 종료가 확인되지 않으면 새 프로세스 시작을 계속 차단합니다.
5. **체결 처리:** WebSocket 체결통보를 main loop의 REST 대기와 분리한 전용 worker에서 처리합니다. 종료 시 WebSocket을 멈추고 보류 체결 이벤트를 처리한 후 최종 잔고 조회를 수행합니다.
6. **자동 종료:** `AUTO_SHUTDOWN_TIME`을 신규 매수 최종 시간 게이트에도 적용합니다. 평가 도중 종료 시각을 넘겨도 주문 POST 직전에 차단됩니다.
7. **설정 추적:** `.env`의 비밀값을 제외한 매매/운영 설정 변경을 `json/config_snapshot.json`에 원자 저장하고 control 로그에 변경 키/값을 남깁니다.
8. **시작 요청 로그:** `request_start.sh`의 control 기록을 `start_request_YYYYMMDD.log`에도 직접 기록합니다. watchdog stdout redirection에만 의존하지 않습니다.
9. **전략 평가:** CUSTOM 모드는 활성 YAML indicator 기간에 맞춰 일봉 수를 계산합니다. KIS10/ALL은 실제 BUY/SELL 참여전략의 최대 lookback을 계산합니다. `BUILDER_OBSERVE_ALL_STRATEGIES`를 진입·청산 평가 모두에서 실제로 존중합니다. 일봉 부족(`INSUFFICIENT_HISTORY`)과 API 오류(`API_ERROR`)를 분리하고 실패 종목 재호출을 제한합니다.
10. **주문 대사/감사:** 잔고+미체결을 함께 조회한 직후 별도 미체결 조회가 중복 실행되지 않도록 reconcile 타임스탬프를 동기화합니다. REST 주문이력의 누적 체결금액 필드를 별도로 기록합니다.

## 검증 원칙
- 전체 pytest 회귀, Python compile, YAML 파싱, shell syntax, ZIP 무결성 검사를 수행합니다.
- API 실제 주문/매수가능조회는 테스트에서 모의 객체로 대체합니다. 이 패키지 빌드만으로 실계좌/모의계좌의 실제 응답이나 주문 성공을 보증하지 않습니다.
- 실전 전환 전 모의투자에서 `ord_psbl_cash`, `nrcvb_buy_amt`, `nrcvb_buy_qty`, 명시 거절 `msg_cd/msg1`, 주문이력 대사, fallback 1회 제한을 별도로 검증해야 합니다.
