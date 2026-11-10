# CHANGELOG V7.0.2

## 목표
V7.0.0의 2026-10-26 모의투자 로그와 V7.0.1 소스를 대조해, 금액·API 제한·후보 출처·체결/잔고 대사를 더 쉽게 검증하도록 진단 로그를 보강했습니다. 전략 임계값이나 주문 방식은 변경하지 않았습니다.

## 변경 사항
- `FILL_AMOUNT_AUDIT`: 개별 체결금액과 `최근체결가 × 누적수량` 참고 계산을 분리합니다. 후자는 실제 누적 체결금액으로 표기하지 않습니다. `KIS_ORDER_HISTORY_AUDIT`는 실제 주문이력 응답에 존재하는 누적 금액/평균단가 필드를 기록합니다.
- `KIS_BALANCE_AUDIT`, `PORTFOLIO_AMOUNT_AUDIT`, `RECONCILE_BALANCE_AUDIT`, `RECONCILE_POSITION_AUDIT`: KIS 잔고 요약 원본 필드, 평가 합계, 기준자산 선택 필드, 로컬/브로커 수량 보정 전후를 기록합니다. 계좌 인증정보는 기록하지 않습니다.
- `KIS_RATE_LIMIT_WAIT` (정상 1.56초 간격 대기 로그 폭주 방지를 위해 2초 이상 대기 시): REST, 토큰 발급, WebSocket approval key 발급의 limiter 대기시간과 최근 호출량을 기록합니다. 기존 공용 limiter와 cooldown 정책은 유지합니다.
- `[HTS_CONDITION]`, `[CANDIDATE_SOURCE]`, `[CANDIDATE_FILTER]`: 조건명/seq/결과 종목과 HTS/수동 파일 출처를 분리 기록합니다.
- `FILL_NOTICE_RECEIVED`, `FILL_NOTICE_DUPLICATE_SUPPRESSED`: 체결통보의 개별 수량·가격·누적수량과 중복 억제 판정을 추적합니다.
- `[STRATEGY_PROFILE_CONFIG]`, `[STRATEGY_PROFILE_CONFIG_ITEM]`, `[STRATEGY_DECISION_AUDIT]`: 전역 전략 enabled와 프로필 decision_enabled, 점수/투표/차단 게이트를 구분합니다.
- 버전 표기를 7.0.2로 갱신했습니다. ZIP에 포함되는 셸 스크립트 실행 권한도 보존합니다.

## 안전 범위
- 매수/매도 임계값, 포지션 한도, 주문 유형, 재시도 정책, 주문 제출 흐름은 의도적으로 변경하지 않았습니다.
- 잔고 요약 필드 간 금액 차이의 원인을 추정해 회계 계산을 바꾸지 않았습니다. 로그의 235원 차이는 KIS 응답 간 차이라고 확정할 수 없으며, 기존 코드가 마지막 체결가 × 누적수량을 참고값으로 계산한 결과임을 확인했습니다. `cumulative_actual_amount=UNKNOWN`은 FillEvent에 실제 누적 체결금액 필드가 없음을 명시합니다.
- 모의투자 설정을 유지하고 실전 전환을 수행하지 않습니다.

## 새 로그 분석 키워드
`FILL_AMOUNT_AUDIT`, `KIS_BALANCE_AUDIT`, `KIS_ORDER_HISTORY_AUDIT`, `PORTFOLIO_AMOUNT_AUDIT`, `RECONCILE_BALANCE_AUDIT`, `RECONCILE_POSITION_AUDIT`, `KIS_RATE_LIMIT_WAIT`, `KIS_RATE_LIMIT_COOLDOWN`, `[ENTRY_SELECTION_START]`, `[ENTRY_SELECTION_HTS]`, `[ENTRY_SELECTION_BUILDER]`, `[ENTRY_SELECTION_RESULT]`, `[HTS_CONDITION]`, `[CANDIDATE_SOURCE]`, `[CANDIDATE_FILTER]`, `FILL_NOTICE_RECEIVED`, `FILL_NOTICE_DUPLICATE_SUPPRESSED`, `[STRATEGY_PROFILE_CONFIG]`, `[STRATEGY_PROFILE_CONFIG_ITEM]`, `[STRATEGY_DECISION_AUDIT]`.
