"""보유 포지션과 KIS WebSocket 시세 구독을 동기화하는 보조 함수."""
from __future__ import annotations

import logging
from trading.symbols import symbol_label


def subscribe_held_positions(store, realtime, logger: logging.Logger | None = None) -> list[str]:
    """수량이 양수인 모든 보유 종목을 realtime desired subscription set에 반영합니다.

    `KisRealtime.subscribe_symbols()`는 연결 전 호출도 보관하므로 시작 시점에도 안전합니다.
    함수는 실제 WebSocket 연결 성공을 주장하지 않고, 구독 요청 목록에 반영된 종목을 반환합니다.
    """
    symbols: list[str] = []
    for symbol in store.symbols():
        pos = store.get_position(symbol) or {}
        try:
            qty = int(pos.get("qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        normalized = str(symbol).strip().zfill(6)
        if qty > 0 and normalized != "000000":
            symbols.append(normalized)
    symbols = sorted(set(symbols))
    # 현재 보유 목록으로 교체해 이미 매도된 종목의 가격 구독이 영구 누적되지 않게 합니다.
    # 구형 테스트 더블/어댑터는 기존 subscribe_symbols()로 호환합니다.
    if hasattr(realtime, "set_held_symbols"):
        realtime.set_held_symbols(symbols)
    elif symbols:
        realtime.subscribe_symbols(symbols)
    if logger is not None:
        logger.info("보유 종목 실시간 구독 동기화 요청: %d종목 %s", len(symbols), [symbol_label(item) for item in symbols] or ["없음"])
    return symbols
