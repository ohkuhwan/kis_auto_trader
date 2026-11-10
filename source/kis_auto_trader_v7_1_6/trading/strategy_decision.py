"""전략 엔진 간 BUY/SELL 신호 결합 규칙. 점수 계산은 각 엔진이 독립적으로 수행합니다."""

def combine_buy(mode: str, kis10_action: str, custom_action: str) -> bool:
    """KIS10/CUSTOM 단독은 해당 엔진 BUY, ALL은 두 엔진 모두 BUY일 때만 승인합니다."""
    mode = str(mode).strip().upper()
    if mode == "KIS10": return kis10_action == "BUY"
    if mode == "CUSTOM": return custom_action == "BUY"
    if mode == "ALL": return kis10_action == "BUY" and custom_action == "BUY"
    raise ValueError("STRATEGY_EVALUATION_MODE는 KIS10, CUSTOM, ALL 중 하나여야 합니다.")

def combine_sell(mode: str, kis10_action: str, custom_action: str) -> bool:
    """KIS10/CUSTOM 단독은 해당 엔진 SELL, ALL은 하나라도 SELL이면 신호입니다."""
    mode = str(mode).strip().upper()
    if mode == "KIS10": return kis10_action == "SELL"
    if mode == "CUSTOM": return custom_action == "SELL"
    if mode == "ALL": return kis10_action == "SELL" or custom_action == "SELL"
    raise ValueError("STRATEGY_EVALUATION_MODE는 KIS10, CUSTOM, ALL 중 하나여야 합니다.")
