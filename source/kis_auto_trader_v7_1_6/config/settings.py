"""
config/settings.py - KIS 자동매매 설정

config 패키지에서 설정 책임을 관리합니다.
- KIS 인증/계좌
- REST 호출 안전상한
- WebSocket/REST 시세 운영
- 후보 수/주문 수량
- 통합 전략 평가 설정
- 자동주문 활성화 여부

주의: RATE LIMIT 숫자는 KIS의 공식 고정 한도를 주장하는 값이 아니라
프로그램 내부의 보수적인 안전상한입니다. 최신 KIS 정책과 계정별 제한을
확인한 뒤 .env에서 조정하십시오.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv
from config.paths import PROJECT_ROOT, runtime_path

# .env는 프로세스 cwd가 아니라 프로젝트 루트에서만 로드합니다.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class StrategyConfig:
    """전략 하나의 진입/청산 및 재진입 설정입니다."""

    name: str
    min_score: float
    max_chase_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_start_pct: float
    trailing_gap_pct: float
    cooldown_sec: int
    max_position_ratio: float


@dataclass(frozen=True)
class Settings:
    """프로그램 전체 설정입니다."""

    paper: bool
    enable_trading: bool
    app_key: str
    app_secret: str
    hts_id: str
    cano: str
    acnt_prdt_cd: str
    api_base: str
    ws_url: str
    condition_name: str
    scan_interval_sec: int
    balance_interval_sec: int
    order_reconcile_interval_sec: int
    quote_stale_sec: int
    quote_rest_min_interval_sec: float
    order_timeout_sec: int
    buy_order_timeout_sec: int
    sell_order_timeout_sec: int
    sell_retry_cooldown_sec: int
    max_trades_per_day: int
    max_daily_loss_pct: float
    max_candidates: int
    order_qty: int
    max_calls_per_sec: float
    max_calls_per_min: int
    rate_limit_safety_margin: float
    state_file: str
    strategy: StrategyConfig


def _bool(name: str, default: bool) -> bool:
    """환경변수의 true/false 문자열을 boolean으로 변환합니다."""
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "y", "yes", "on"}


def _int(name: str, default: int) -> int:
    """정수 환경변수를 읽고 잘못된 값은 기본값으로 처리합니다."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    """실수 환경변수를 읽고 잘못된 값은 기본값으로 처리합니다."""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def load_settings() -> Settings:
    """.env를 읽어 Settings 객체를 생성합니다.

    자동주문은 KIS_ENABLE_TRADING=true를 명시적으로 켜야 합니다.
    실수로 실전 주문이 나가는 것을 막기 위해 기본값은 false입니다.
    """
    paper = _bool("KIS_PAPER", True)
    return Settings(
        paper=paper,
        enable_trading=_bool("KIS_ENABLE_TRADING", False),
        app_key=os.getenv("KIS_APP_KEY", "").strip(),
        app_secret=os.getenv("KIS_APP_SECRET", "").strip(),
        hts_id=os.getenv("KIS_HTS_ID", "").strip(),
        cano=os.getenv("KIS_CANO", "").strip(),
        acnt_prdt_cd=os.getenv("KIS_ACNT_PRDT_CD", "01").strip(),
        api_base=("https://openapivts.koreainvestment.com:29443" if paper else "https://openapi.koreainvestment.com:9443"),
        ws_url=("ws://ops.koreainvestment.com:31000" if paper else "ws://ops.koreainvestment.com:21000"),
        condition_name=os.getenv("KIS_CONDITION_NAME", "").strip(),
        scan_interval_sec=max(5, _int("SCAN_INTERVAL_SEC", 30)),
        balance_interval_sec=max(10, _int("BALANCE_INTERVAL_SEC", 60)),
        order_reconcile_interval_sec=max(10, _int("ORDER_RECONCILE_INTERVAL_SEC", 20)),
        quote_stale_sec=max(1, _int("QUOTE_STALE_SEC", 5)),
        quote_rest_min_interval_sec=max(1.0, _float("QUOTE_REST_MIN_INTERVAL_SEC", 3.0)),
        order_timeout_sec=max(3, _int("ORDER_TIMEOUT_SEC", 60)),
        # V7.1.6: BUY/SELL 미체결 취소 기준은 최소 300초로 통일합니다.
        # 기존 .env에 180초가 남아 있어도 짧은 취소 기준으로 되돌아가지 않도록 하한을 강제합니다.
        buy_order_timeout_sec=max(300, _int("BUY_ORDER_TIMEOUT_SEC", max(300, _int("ORDER_TIMEOUT_SEC", 60) * 2))),
        sell_order_timeout_sec=max(300, _int("SELL_ORDER_TIMEOUT_SEC", max(300, _int("ORDER_TIMEOUT_SEC", 60)))),
        sell_retry_cooldown_sec=max(60, _int("SELL_RETRY_COOLDOWN_SEC", 60)),
        max_trades_per_day=max(1, _int("MAX_TRADES_PER_DAY", 5)),
        max_daily_loss_pct=max(0.1, _float("MAX_DAILY_LOSS_PCT", 2.0)),
        max_candidates=max(1, min(20, _int("MAX_CANDIDATES", 10))),
        order_qty=max(1, _int("ORDER_QTY", 1)),
        max_calls_per_sec=max(0.1, min(0.8, _float("MAX_CALLS_PER_SEC", 0.8))),
        max_calls_per_min=max(1, min(45, _int("MAX_CALLS_PER_MIN", 45))),
        rate_limit_safety_margin=min(1.0, max(0.1, _float("RATE_LIMIT_SAFETY_MARGIN", 0.80))),
        state_file=str(runtime_path("state")),
        # 통합 전략 설정입니다. 실제 매수/매도 신호는 YAML 엔진이 결정하고,
        # 이 값은 레거시 StrategyEngine API 호환용 최소 설정으로만 사용합니다.
        strategy=StrategyConfig(name="unified", min_score=0.0, max_chase_pct=4.0,
            stop_loss_pct=max(0.1, _float("EXIT_STOP_LOSS_HARD_PCT", 2.0)),
            take_profit_pct=max(0.1, _float("EXIT_TAKE_PROFIT_HARD_PCT", 3.0)),
            trailing_start_pct=999.0, trailing_gap_pct=0.0,
            cooldown_sec=max(30000, _int("REENTRY_COOLDOWN_SEC", 30000)), max_position_ratio=1.0),
    )
