"""strategies/engine.py - 공격형/안정형 2차 점수화 및 청산 판단."""

from dataclasses import dataclass
from typing import Optional

from config import Settings, StrategyConfig


def num(row: dict, *keys: str, default: float = 0.0) -> float:
    """여러 후보 필드 중 숫자로 변환 가능한 첫 값을 반환합니다."""
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return default


@dataclass
class Candidate:
    """점수화 대상 종목입니다."""
    symbol: str
    name: str
    hts: dict
    price: dict
    score: float = 0.0
    reason: str = ""


class StrategyEngine:
    """통합 호환 점수 계산기입니다. 실거래 BUY/SELL 승인은 YAML 평가 엔진이 담당합니다."""

    def __init__(self, settings: Settings):
        self.s = settings

    def _common_metrics(self, c: Candidate) -> dict:
        p, h = c.price or {}, c.hts or {}
        current = num(p, "stck_prpr", "STCK_PRPR", "prpr", default=num(h, "prpr", "stck_prpr"))
        open_price = num(p, "stck_oprc", "STCK_OPRC", default=num(h, "oprc"))
        high = num(p, "stck_hgpr", "STCK_HGPR", default=num(h, "hgpr"))
        low = num(p, "stck_lwpr", "STCK_LWPR", default=num(h, "lwpr"))
        volume = num(p, "acml_vol", "ACML_VOL", default=num(h, "acml_vol"))
        pct = num(p, "prdy_ctrt", "PRDY_CTRT", "chgrate", "chgrate2", default=num(h, "chgrate", "chgrate2"))
        trade_value = num(p, "acml_tr_pbmn", "ACML_TR_PBMN", "acml_tr_pbmn", default=num(h, "stotprice"))
        return {"current": current, "open": open_price, "high": high, "low": low,
                "volume": volume, "pct": pct, "trade_value": trade_value}

    def _score_unified(self, candidate: Candidate) -> Candidate:
        """호환용 후보 점수입니다. 자동매매 승인에는 사용하지 않습니다."""
        metrics = self._common_metrics(candidate)
        current, open_price, high = metrics["current"], metrics["open"], metrics["high"]
        if current <= 0:
            candidate.score, candidate.reason = -1.0, "현재가 없음"
            return candidate
        if metrics["pct"] > self.s.strategy.max_chase_pct:
            candidate.score, candidate.reason = -1.0, f"추격 차단: 등락률 {metrics['pct']:.2f}%"
            return candidate
        score = 20.0 if 0 <= metrics["pct"] <= 2.0 else 10.0 if metrics["pct"] <= 3.0 else 3.0
        if metrics["trade_value"] >= 5e9: score += 20
        elif metrics["trade_value"] >= 2e9: score += 12
        elif metrics["trade_value"] >= 1e9: score += 6
        if metrics["volume"] > 0: score += 8
        if open_price > 0 and current >= open_price: score += 15
        if high > 0 and current / high >= 0.97: score += 8
        candidate.score = min(100.0, score)
        candidate.reason = "통합 호환 점수(실제 승인은 YAML 평가 엔진 담당)"
        return candidate

    def score(self, candidates: list[Candidate], style: str = "unified") -> list[Candidate]:
        return sorted((self._score_unified(candidate) for candidate in candidates),
                      key=lambda item: item.score, reverse=True)

    def pick_one(self, candidates: list[Candidate], style: str = "unified") -> Optional[Candidate]:
        ranked = self.score(candidates, style)
        return ranked[0] if ranked and ranked[0].score >= self.s.strategy.min_score else None

    def exit_signal(self, position: dict, current_price: float) -> tuple[bool, str]:
        """하위 호환 API: 공통 2차 손익률 기준만 검사합니다."""
        avg = float(position.get("avg_price", 0) or 0)
        if avg <= 0 or current_price <= 0:
            return False, ""
        pnl = (current_price / avg - 1.0) * 100.0
        if pnl <= -self.s.strategy.stop_loss_pct:
            return True, f"STOP_LOSS_HARD {pnl:.2f}%"
        if pnl >= self.s.strategy.take_profit_pct:
            return True, f"TAKE_PROFIT_HARD {pnl:.2f}%"
        return False, ""
