"""storage/state.py - 재시작 가능한 로컬 상태 저장소."""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
import logging
import math

state_log = logging.getLogger(__name__)


class StateRecoveryError(RuntimeError):
    """기존 상태 파일을 안전하게 복구할 수 없어 실행을 중단해야 할 때 발생합니다."""


@dataclass
class Position:
    """브로커 잔고와 매매관리에 필요한 한 종목 상태입니다."""
    symbol: str
    name: str = ""
    qty: int = 0
    avg_price: float = 0.0
    high_price: float = 0.0
    pending_order_no: str = ""
    pending_side: str = ""
    strategy: str = "unified"
    last_exit_ts: float = 0.0


@dataclass
class BotState:
    """프로그램 전체 상태입니다."""
    positions: dict = field(default_factory=dict)
    cooldowns: dict = field(default_factory=dict)
    # 명시적인 2차 매수 실패 종목은 당일 재시도를 막고 재시작 후에도 복구합니다.
    buy_rejection_blocks: dict = field(default_factory=dict)
    trades_today: int = 0
    realized_pnl_pct: float = 0.0  # 구버전 상태 파일 호환용 레거시 지표
    daily_realized_pnl_krw: float = 0.0
    daily_start_assets_krw: float = 0.0
    date_key: str = ""


class StateStore:
    """상태를 thread-safe하게 읽고 원자적으로 JSON 저장합니다."""

    def __init__(self, path: str):
        """상태 파일 경로를 등록하고 기존 상태를 복구합니다."""
        self.path = path
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> BotState:
        """상태 파일이 없으면 최초 상태를 만들고, 기존 파일 손상/접근 오류는 조용히 초기화하지 않습니다."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return BotState()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            message = (f"상태 파일을 안전하게 읽을 수 없습니다: {self.path} "
                       f"({type(exc).__name__}: {exc}). 원본을 보존하고 파일을 복구한 뒤 재시작하십시오.")
            state_log.critical(message)
            raise StateRecoveryError(message) from exc

        try:
            if not isinstance(raw, dict):
                raise ValueError("최상위 JSON 값은 객체여야 합니다")
            positions = raw.get("positions", {})
            cooldowns = raw.get("cooldowns", {})
            buy_rejection_blocks = raw.get("buy_rejection_blocks", {})
            if not isinstance(positions, dict) or not isinstance(cooldowns, dict) or not isinstance(buy_rejection_blocks, dict):
                raise ValueError("positions, cooldowns, buy_rejection_blocks는 객체여야 합니다")
            if any(not isinstance(value, dict) for value in positions.values()):
                raise ValueError("positions 항목은 객체여야 합니다")
            for symbol, expires in cooldowns.items():
                expiry_value = float(expires)
                if not str(symbol).strip() or not math.isfinite(expiry_value) or expiry_value < 0:
                    raise ValueError("cooldowns 종목코드/만료시각 형식이 유효하지 않습니다")
            for symbol, block in buy_rejection_blocks.items():
                if not str(symbol).strip() or not isinstance(block, dict):
                    raise ValueError("buy_rejection_blocks 종목코드/기록 형식이 유효하지 않습니다")
                if not str(block.get("date_key", "")).strip():
                    raise ValueError("buy_rejection_blocks date_key 누락")
            return BotState(
                positions=positions,
                cooldowns=cooldowns,
                buy_rejection_blocks=buy_rejection_blocks,
                trades_today=int(raw.get("trades_today", 0)),
                realized_pnl_pct=float(raw.get("realized_pnl_pct", 0.0)),
                daily_realized_pnl_krw=float(raw.get("daily_realized_pnl_krw", 0.0)),
                daily_start_assets_krw=float(raw.get("daily_start_assets_krw", 0.0)),
                date_key=str(raw.get("date_key", "")),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            message = (f"상태 파일 스키마가 손상되었습니다: {self.path} "
                       f"({type(exc).__name__}: {exc}). 원본을 보존하고 파일을 복구한 뒤 재시작하십시오.")
            state_log.critical(message)
            raise StateRecoveryError(message) from exc

    def save(self) -> None:
        """임시 파일 저장 후 os.replace로 원자 교체합니다."""
        with self.lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self.state), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def reset_daily_if_needed(self, date_key: str) -> None:
        """KST 날짜가 바뀌면 일일 카운터를 초기화합니다. 구형 YYYYMMDD도 정규화합니다."""
        normalized = str(date_key or "").strip()
        if len(normalized) == 8 and normalized.isdigit():
            normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:8]}"
        with self.lock:
            stored = str(self.state.date_key or "").strip()
            if len(stored) == 8 and stored.isdigit():
                stored = f"{stored[:4]}-{stored[4:6]}-{stored[6:8]}"
            if stored != normalized:
                self.state.date_key = normalized
                # 2차 매수 실패 종목 잠금은 당일 한정입니다. 날짜 변경 시 자동 해제합니다.
                self.state.buy_rejection_blocks = {}
                self.state.trades_today = 0
                self.state.realized_pnl_pct = 0.0
                self.state.daily_realized_pnl_krw = 0.0
                self.state.daily_start_assets_krw = 0.0
                self.save()
            elif self.state.date_key != normalized:
                # 같은 날짜의 구형 형식은 카운터를 초기화하지 않고 표기만 정규화합니다.
                self.state.date_key = normalized
                self.save()

    def get_position(self, symbol: str) -> dict | None:
        """종목 상태를 thread-safe하게 반환합니다."""
        with self.lock:
            value = self.state.positions.get(symbol)
            return dict(value) if isinstance(value, dict) else None

    def set_position(self, symbol: str, position: dict) -> None:
        """종목 포지션을 저장합니다."""
        with self.lock:
            self.state.positions[symbol] = dict(position)

    def increment_trade_count(self) -> None:
        """실제 BUY 첫 체결 기준으로 일일 거래횟수를 1 증가시킵니다."""
        with self.lock:
            self.state.trades_today += 1

    def add_realized_pnl(self, pnl_pct: float) -> None:
        """구버전 상태 파일 호환용 단순 수익률 지표를 기록합니다."""
        with self.lock:
            self.state.realized_pnl_pct += float(pnl_pct)

    def add_realized_pnl_krw(self, amount: float) -> None:
        """수수료/세금 차감 후 실현손익 금액을 기록합니다."""
        with self.lock:
            self.state.daily_realized_pnl_krw += float(amount)

    def set_daily_start_assets(self, amount: float) -> None:
        """당일 손실률의 분모가 되는 최초 총자산을 한 번만 설정합니다."""
        with self.lock:
            if self.state.daily_start_assets_krw <= 0 and amount > 0:
                self.state.daily_start_assets_krw = float(amount)
                self.save()

    def daily_loss_pct(self) -> float:
        """당일 실현 순손익/당일 시작 총자산 비율(%)을 반환합니다."""
        with self.lock:
            base = float(self.state.daily_start_assets_krw or 0)
            if base <= 0:
                # 기준 자산을 조회하지 못했을 때는 손실 제한을 해제하지 않도록 -inf 반환
                return float("-inf")
            return float(self.state.daily_realized_pnl_krw) / base * 100.0

    def remove_position(self, symbol: str) -> None:
        """종목 포지션을 삭제합니다."""
        with self.lock:
            self.state.positions.pop(symbol, None)

    def symbols(self) -> list[str]:
        """현재 로컬 포지션 종목코드 목록을 반환합니다."""
        with self.lock:
            return list(self.state.positions.keys())

    def buy_rejection_block(self, symbol: str, date_key: str | None = None) -> dict | None:
        """당일 2차 매수 실패 잠금 기록을 반환합니다. 날짜가 다르면 비활성입니다."""
        normalized = str(symbol or "").strip().zfill(6)
        with self.lock:
            block = self.state.buy_rejection_blocks.get(normalized)
            if not isinstance(block, dict):
                return None
            expected = str(date_key or self.state.date_key or "").replace("-", "")
            actual = str(block.get("date_key", "")).replace("-", "")
            return dict(block) if expected and actual == expected else None

    def mark_buy_rejection_block(self, symbol: str, reason: str, date_key: str) -> None:
        """매수 2차 실패 종목을 당일 잠그고 원자 저장합니다."""
        normalized = str(symbol or "").strip().zfill(6)
        normalized_date = str(date_key or "").strip().replace("-", "")
        if not normalized or not normalized_date:
            raise ValueError("매수 실패 잠금에는 종목코드와 날짜가 필요합니다")
        with self.lock:
            self.state.buy_rejection_blocks[normalized] = {
                "date_key": normalized_date,
                "reason": str(reason or "second_buy_attempt_failed")[:240],
                "created_at": time.time(),
            }
            self.save()

    def cooldown_active(self, symbol: str, now: float | None = None) -> bool:
        """마지막 청산 후 cooldown이 끝나지 않았는지 확인합니다."""
        now = time.time() if now is None else now
        with self.lock:
            pos = self.state.positions.get(symbol)
            if pos and float(pos.get("last_exit_ts", 0) or 0) > now:
                return True
            return float(self.state.cooldowns.get(symbol, 0) or 0) > now

    def mark_exit_cooldown(self, symbol: str, cooldown_sec: int) -> None:
        """청산 후 재진입 금지 시각을 기록합니다."""
        with self.lock:
            expires = time.time() + cooldown_sec
            self.state.cooldowns[symbol] = expires
            pos = self.state.positions.get(symbol)
            if pos:
                pos["last_exit_ts"] = expires
            # Persist immediately: a process crash between fill handling and its
            # caller's next save must not reopen the same-symbol entry gate.
            self.save()
