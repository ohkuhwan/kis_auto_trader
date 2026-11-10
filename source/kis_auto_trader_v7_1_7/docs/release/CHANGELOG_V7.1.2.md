# CHANGELOG V7.1.2

## 전략 YAML risk 항목의 실제 적용 범위 명시

- `config/strategies/strategy_01_*.kis.yaml`부터 `strategy_10_*.kis.yaml`까지 10개 전략 파일의 `risk` 블록에 실행 적용 범위를 명확히 주석으로 추가했습니다.
- `stop_loss`, `take_profit`, `trailing_stop`의 `enabled` 및 `percent`는 현재 KIS10 공통 포지션 청산 실행 기준으로 적용되지 않는다는 점을 명시했습니다.
- 실제 1차/2차 손익절은 `.env`의 `EXIT_STOP_LOSS_FIRST_PCT`, `EXIT_STOP_LOSS_HARD_PCT`, `EXIT_TAKE_PROFIT_FIRST_PCT`, `EXIT_TAKE_PROFIT_HARD_PCT`가 관리함을 문서화했습니다.
- 전략별 risk를 실제로 적용하는 기능은 이번 릴리스에 추가하지 않았습니다. 향후 적용 여부와 정책을 결정한 후 별도 구현 및 테스트해야 합니다.
- 전략 조건, 점수 계산, 후보 선정, 주문/청산 실행 로직은 변경하지 않았습니다.

## 검증 범위

- 변경 대상: YAML 주석, README, 버전 파일, 변경 이력 문서
- YAML 파싱 및 기존 테스트 전체를 다시 실행해 확인합니다.
- Oracle Cloud의 예약 자동 시작은 이번 변경의 범위가 아니며, 실제 예약 실행은 별도로 확인해야 합니다.
