"""종목별 주문 제출 예약 가드.

서로 다른 종목의 청산 POST가 하나의 전역 락 때문에 직렬 대기하지 않도록 하면서,
동일 종목에 대한 main loop/event worker의 중복 제출 경쟁을 차단합니다.
"""
from __future__ import annotations

import threading
from collections.abc import Callable


class SymbolSubmissionGuard:
    """pending 조회와 종목별 제출 예약을 원자적으로 수행합니다."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reserved: set[str] = set()

    def reserve(self, symbol: str, is_pending: Callable[[str], bool]) -> bool:
        """해당 종목이 pending/예약 상태가 아니면 예약하고 True를 반환합니다."""
        normalized = str(symbol).strip().zfill(6)
        if not normalized or normalized == "000000":
            return False
        with self._lock:
            if normalized in self._reserved or is_pending(normalized):
                return False
            self._reserved.add(normalized)
            return True

    def release(self, symbol: str) -> None:
        """POST 성공/실패 처리 후 예약을 해제합니다. pending 상태는 별도로 유지됩니다."""
        normalized = str(symbol).strip().zfill(6)
        with self._lock:
            self._reserved.discard(normalized)

    def is_reserved(self, symbol: str) -> bool:
        """테스트/진단용 종목 예약 상태 조회입니다."""
        normalized = str(symbol).strip().zfill(6)
        with self._lock:
            return normalized in self._reserved
