"""Thread-safe REST 호출 제한기: 1초/60초 sliding window + 오류 후 cooldown."""

import math
import heapq
import itertools
import threading
import time
from collections import deque


class RateLimiter:
    """설정된 상한을 초과하지 않도록 sliding-window 방식으로 대기합니다.

    초당 설정값이 소수여도 실제 1초 구간에는 정수 건수만 허용할 수 있으므로
    floor를 적용합니다. 예: 1.5 * 0.80 = 1.2 -> 실제 안전상한 1회/초.
    """

    def __init__(self, max_calls_per_sec: float, max_calls_per_min: int, safety_margin: float = 0.8):
        margin = min(1.0, max(0.1, float(safety_margin)))
        self.max_sec_rate = max(0.1, float(max_calls_per_sec) * margin)
        self.max_sec = max(1, int(math.floor(self.max_sec_rate)))
        self.min_interval = 1.0 / self.max_sec_rate
        self.min_limit = max(1, int(math.floor(max(1, int(max_calls_per_min)) * margin)))
        self._timestamps = deque()
        self._condition = threading.Condition(threading.Lock())
        self._waiters: list[tuple[int, int]] = []
        self._ticket_counter = itertools.count()
        self._cooldown_until = 0.0
        self.total_calls = 0
        self.wait_count = 0

    def defer(self, seconds: float, reason: str = "") -> None:
        """브로커 제한 응답 이후 후속 호출을 잠시 늦춥니다."""
        seconds = max(0.0, float(seconds))
        if not seconds:
            return
        with self._condition:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)
            self._condition.notify_all()

    def acquire(self, priority: int = 10) -> float:
        """호출 우선순위와 sliding-window 제한을 함께 적용합니다.

        priority가 작을수록 먼저 실행합니다. 주문/잔고/주문대사는 전략용
        일봉 조회보다 우선하도록 KisApi가 우선순위를 전달합니다.
        """
        waited = 0.0
        ticket = next(self._ticket_counter)
        waiter = (int(priority), ticket)
        with self._condition:
            heapq.heappush(self._waiters, waiter)
            self._condition.notify_all()
            try:
                while True:
                    now = time.monotonic()
                    while self._timestamps and now - self._timestamps[0] >= 60.0:
                        self._timestamps.popleft()
                    recent_sec = [stamp for stamp in self._timestamps if now - stamp < 1.0]
                    window_wait = max(0.0, 1.0 - (now - recent_sec[0])) if len(recent_sec) >= self.max_sec else 0.0
                    interval_wait = max(0.0, self.min_interval - (now - self._timestamps[-1])) if self._timestamps else 0.0
                    sec_wait = max(window_wait, interval_wait)
                    min_wait = max(0.0, 60.0 - (now - self._timestamps[0])) if len(self._timestamps) >= self.min_limit else 0.0
                    cooldown_wait = max(0.0, self._cooldown_until - now)
                    sleep_for = max(sec_wait, min_wait, cooldown_wait)
                    is_first = bool(self._waiters) and self._waiters[0] == waiter
                    if is_first and sleep_for <= 0:
                        heapq.heappop(self._waiters)
                        stamp = time.monotonic()
                        self._timestamps.append(stamp)
                        self.total_calls += 1
                        self._condition.notify_all()
                        return waited
                    self.wait_count += 1
                    # Re-check promptly when another caller arrives with higher priority.
                    timeout = min(max(sleep_for, 0.01), 0.25) if is_first else 0.25
                    before = time.monotonic()
                    self._condition.wait(timeout=timeout)
                    waited += max(0.0, time.monotonic() - before)
            except BaseException:
                try:
                    self._waiters.remove(waiter)
                    heapq.heapify(self._waiters)
                except ValueError:
                    pass
                self._condition.notify_all()
                raise

    def stats(self) -> dict:
        """최근 호출 수와 유효 안전상한을 반환합니다."""
        with self._condition:
            now = time.monotonic()
            recent_1s = sum(1 for t in self._timestamps if now - t < 1.0)
            recent_60s = sum(1 for t in self._timestamps if now - t < 60.0)
            return {"recent_1s": recent_1s, "recent_60s": recent_60s,
                    "configured_sec_limit": self.max_sec, "configured_sec_rate": self.max_sec_rate,
                    "configured_min_limit": self.min_limit,
                    "total_calls": self.total_calls, "wait_count": self.wait_count,
                    "queued_calls": len(self._waiters),
                    "cooldown_remaining_sec": max(0.0, self._cooldown_until - now)}
