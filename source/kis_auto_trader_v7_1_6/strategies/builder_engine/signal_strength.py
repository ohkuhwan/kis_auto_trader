"""신호 점수와 매수 후보 정렬. 점수는 확률이 아닌 휴리스틱 지표입니다."""

def score_signal(action: str, condition_score: float, trend_ok: bool, volume_ok: bool,
                 momentum_pct: float, overheated: bool = False) -> tuple[float, str]:
    """조건 충족 40 + 추세 20 + 거래량 15 + 모멘텀 15 + 위험 조정 10점 모델."""
    if action != 'BUY':
        return 0.0, f'action={action}; 매수 점수 계산 제외'
    parts = {
        'conditions': max(0.0, min(40.0, condition_score * 0.40)),
        'trend': 20.0 if trend_ok else 0.0,
        'volume': 15.0 if volume_ok else 0.0,
        'momentum': max(0.0, min(15.0, max(0.0, momentum_pct) * 3.0)),
        'risk_adjustment': -10.0 if overheated else 10.0,
    }
    score = max(0.0, min(100.0, sum(parts.values())))
    reason = ', '.join(f'{k}={v:.1f}' for k, v in parts.items())
    return round(score, 2), reason
