# CHANGELOG V7.1.0

## 목표

공격형/안정형 중복 프로필을 제거하고, 종목 후보 선정과 평가 엔진을 분리했습니다.

## 주요 변경

- `ENTRY_MODE=HTS|BUILDER|ALL`은 종목 후보의 출처 선택만 담당합니다.
- 신규 `STRATEGY_EVALUATION_MODE=KIS10|CUSTOM|ALL`은 평가 엔진 선택을 담당합니다.
- `KIS_CONDITION_AGGRESSIVE`와 `KIS_CONDITION_STABLE`을 단일 `KIS_CONDITION_NAME`으로 통합했습니다.
- `strategy_profiles.yaml`을 단일 `decision` 구조로 변경했습니다. `decision.buy.strategies`와 `decision.sell.strategies`는 독립 목록입니다.
- `strategies[].enabled`와 `profiles.aggressive/stable`을 제거했습니다. 방향별 전략 목록에 없는 전략은 해당 방향 투표에 참여하지 않습니다.
- `CUSTOM`은 `my_entry_strategy.kis.yaml` 및 `my_exit_strategy.kis.yaml`을 사용합니다. `KIS10`은 10개 전략 파일만 사용합니다.
- `ALL` 매수는 KIS10과 CUSTOM이 모두 BUY일 때만 승인합니다. `ALL` 전략 매도는 어느 한 엔진이라도 SELL이면 신호가 됩니다.
- 1차 청산은 전략 SELL 신호와 `EXIT_STOP_LOSS_FIRST_PCT` 또는 `EXIT_TAKE_PROFIT_FIRST_PCT` 기준을 함께 충족할 때 실행합니다. 2차 `EXIT_STOP_LOSS_HARD_PCT` / `EXIT_TAKE_PROFIT_HARD_PCT`는 전략 신호와 무관하게 적용합니다.
- 과거 포지션에 저장된 `strategy_risk`는 더 이상 독립 청산을 발생시키지 않습니다. 주문·체결·미체결·대사·재진입 쿨다운 및 API 호출 제한 안전장치는 유지합니다.
- `reload_config.sh`는 평가 모드에 맞는 전략 파일만 검증하고, 통합 프로필의 ID 참조 및 BUY/SELL 목록을 확인합니다.

## 검증 범위 및 제한

- 오프라인 pytest/smoke 및 YAML/셸 문법 검증을 수행합니다.
- Oracle Cloud Ubuntu와 proot에서 실제 설치·예약 자동 시작을 수행한 것은 아닙니다.
- 모의투자에서 각 평가 모드와 주문/체결/재시작 시나리오를 확인하기 전 실전 주문으로 전환하지 마세요.
