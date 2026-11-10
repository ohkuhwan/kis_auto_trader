"""brokers/kis/client.py - KIS REST API 래퍼.

현재 KIS 공식 샘플의 국내주식 주문 TR을 우선 반영합니다.
- 매수: TTTC0012U / VTTC0012U
- 매도: TTTC0011U / VTTC0011U
- 취소: TTTC0013U / VTTC0013U
- 잔고: TTTC8434R / VTTC8434R
- 당일 주문체결: TTTC0081R / VTTC0081R
"""

import threading
import time
import os
import logging
import json
import hashlib
import tempfile
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import requests

from config import Settings
from config.paths import runtime_path
from .rate_limiter import RateLimiter


class KisApiError(RuntimeError):
    """KIS API 호출 실패를 나타내며, 명시 응답의 오류 코드를 보존합니다."""
    def __init__(self, message: str, *, msg_cd: str = "", msg1: str = "", http_status: int | None = None):
        super().__init__(message)
        self.msg_cd = str(msg_cd or "").strip().upper()
        self.msg1 = str(msg1 or "")
        self.http_status = http_status


_ACCOUNT_CONTEXT_ERROR_CODES = {"40910000", "IGW00002", "90070000"}


class AmbiguousOrderError(KisApiError):
    """주문 요청이 브로커에 접수됐는지 응답만으로 확정할 수 없는 경우입니다."""


class RateLimitError(KisApiError):
    """KIS가 초당/호출량 제한(EGW00201/EGW00215 등)을 명시적으로 반환한 경우입니다."""


class InsufficientBuyingPowerError(KisApiError):
    """현금매수 요청이 주문가능/미수 없는 매수한도 부족으로 명시 거절된 경우입니다."""
    def __init__(self, message: str, *, msg_cd: str = "", msg1: str = ""):
        super().__init__(message)
        self.msg_cd = str(msg_cd or "")
        self.msg1 = str(msg1 or "")


log = logging.getLogger(__name__)

_RATE_LIMIT_CODES = {"EGW00201", "EGW00215"}
_RATE_LIMIT_TEXTS = ("초당 거래건수를 초과", "허용 가능한 초당 거래건수를 초과", "호출 한도를 초과")
_BUYING_POWER_TEXTS = (
    "주문가능금액 부족", "주문가능금액이 부족", "주문가능금액 초과", "주문가능금액을 초과",
    "주문 가능 금액 부족", "주문 가능 금액이 부족", "주문가능현금 부족",
    "매수가능금액 부족", "매수가능금액이 부족", "매수가능금액을 초과",
    "미수없는매수금액 부족", "미수없는매수금액을 초과", "미수 없는 매수금액 부족",
    "증거금 부족", "증거금이 부족", "현금 부족", "현금이 부족", "주문금액을 초과",
)


def _is_buying_power_rejection(payload: dict) -> bool:
    """국내 현금매수의 명시적인 한도 부족 메시지만 fallback 대상으로 분류합니다.

    KIS는 계좌/주문구분별 업무 거절 코드를 일관된 단일 코드로 보장하지 않으므로
    msg_cd는 감사 로그에 보존하고, msg1의 구체적인 현금/증거금 문구로 제한합니다.
    네트워크 오류·HTTP 오류·호출 제한은 이 판별에 포함하지 않습니다.
    """
    message = str(payload.get("msg1", payload.get("msg", "")) or "").strip()
    normalized = message.replace(" ", "")
    return bool(message) and any(fragment in message or fragment.replace(" ", "") in normalized
                                      for fragment in _BUYING_POWER_TEXTS)


def _mask_account(value: object) -> str:
    raw = str(value or "")
    return ("*" * max(0, len(raw) - 4) + raw[-4:]) if raw else ""


def _redact_sensitive_text(value: object) -> str:
    """외부 HTTP 예외/응답문에 섞일 수 있는 계좌·토큰·승인키를 마스킹합니다."""
    text = str(value or "")
    text = re.sub(r"(?i)(bot)\d{6,}:[A-Za-z0-9_-]+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)(CANO[\"']?\s*[:=]\s*[\"']?)(\d{6,})",
        lambda m: m.group(1) + _mask_account(m.group(2)), text,
    )
    for key in ("chat_id", "appkey", "appsecret", "access_token", "approval_key", "authorization", "aes_key", "iv", "token"):
        text = re.sub(
            rf"(?i)({key}[\"']?\s*[:=]\s*[\"']?)([^,\s&\"' }}]+)",
            r"\1[REDACTED]", text,
        )
    return text[:1000]


def _is_rate_limit_payload(payload: dict) -> bool:
    """HTTP 상태와 무관하게 KIS의 알려진 제한 코드/메시지를 공통 판별합니다."""
    code = str(payload.get("msg_cd", "")).strip().upper()
    message = str(payload.get("msg1", payload.get("msg", ""))).strip()
    return code in _RATE_LIMIT_CODES or any(fragment in message for fragment in _RATE_LIMIT_TEXTS)



class KisApi:
    """KIS 국내주식 REST 호출을 담당합니다."""

    def __init__(self, settings: Settings):
        """HTTP 세션, 토큰 캐시, 호출 제한기를 초기화합니다."""
        self.s = settings
        # OrderManager가 등록하는 callback으로, 계좌/인증 단위 오류를 주문 잠금에 전달합니다.
        self.account_error_handler = None
        self.session = requests.Session()
        self.token = ""
        self.token_expire_at = 0.0
        self.approval_key = ""
        # REST access token과 WebSocket approval_key는 동일 JSON 파일에 저장합니다.
        # 환경/앱키가 다르면 캐시를 재사용하지 않아 모의·실전 키 혼용을 방지합니다.
        self.token_cache_path = runtime_path("token_cache")
        # 계좌·상품코드·HTS ID 또는 App Secret이 바뀌면 이전 REST/WS 인증 캐시를 재사용하지 않습니다.
        # 원문 비밀값은 fingerprint에만 해시로 반영하며 로그/캐시에 저장하지 않습니다.
        credential_context = "|".join((
            str(settings.api_base), str(settings.app_key),
            hashlib.sha256(str(settings.app_secret).encode("utf-8")).hexdigest(),
            str(settings.cano), str(settings.acnt_prdt_cd), str(settings.hts_id),
        ))
        self._credential_fingerprint = hashlib.sha256(credential_context.encode("utf-8")).hexdigest()
        self._ws_approval_expire_at = 0.0
        self._token_retry_after = 0.0
        self.limiter = RateLimiter(settings.max_calls_per_sec, settings.max_calls_per_min, settings.rate_limit_safety_margin)
        # 주요 국내주식 TR에는 프로그램 내부 보수적 기본 제한을 적용합니다.
        # .env JSON으로 덮어쓸 수 있고, 미등록 TR은 공통 limiter를 따릅니다.
        # 모든 TR은 공통 limiter도 반드시 통과합니다.
        self.tr_limiters: dict[str, RateLimiter] = {}
        # 아래 값은 KIS의 공식 고정 한도가 아니라 이 프로그램의 보수적 TR별 상한입니다.
        # 계정/앱키별 제한은 달라질 수 있으므로 .env의 JSON 설정으로 개별 조정할 수 있습니다.
        default_tr_config = {
            "FHKST03010100": {"per_sec": 0.40, "per_min": 18},  # 일봉
            "FHKST01010100": {"per_sec": 0.50, "per_min": 24},  # 현재가
            "VTTC8434R": {"per_sec": 0.25, "per_min": 12},    # 모의 잔고
            "TTTC8434R": {"per_sec": 0.25, "per_min": 12},    # 실전 잔고
            "VTTC0081R": {"per_sec": 0.25, "per_min": 12},    # 모의 주문/체결
            "TTTC0081R": {"per_sec": 0.25, "per_min": 12},    # 실전 주문/체결
            "VTTC8908R": {"per_sec": 0.25, "per_min": 12},    # 모의 매수가능
            "TTTC8908R": {"per_sec": 0.25, "per_min": 12},    # 실전 매수가능
            "VTTC0012U": {"per_sec": 0.25, "per_min": 12},    # 모의 현금매수
            "TTTC0012U": {"per_sec": 0.25, "per_min": 12},    # 실전 현금매수
            "VTTC0011U": {"per_sec": 0.25, "per_min": 12},    # 모의 현금매도
            "TTTC0011U": {"per_sec": 0.25, "per_min": 12},    # 실전 현금매도
            "VTTC0013U": {"per_sec": 0.25, "per_min": 12},    # 모의 정정/취소
            "TTTC0013U": {"per_sec": 0.25, "per_min": 12},    # 실전 정정/취소
            "HHKST03900300": {"per_sec": 0.20, "per_min": 10}, # HTS 조건명
            "HHKST03900400": {"per_sec": 0.20, "per_min": 10}, # HTS 조건결과
        }
        tr_config = {}
        raw_tr_config = os.getenv("KIS_TR_RATE_LIMITS_JSON", "{}") or "{}"
        try:
            parsed_tr_config = json.loads(raw_tr_config)
            if isinstance(parsed_tr_config, dict):
                tr_config = parsed_tr_config
            else:
                log.error("KIS_TR_RATE_LIMITS_JSON은 JSON 객체여야 합니다. 내장 TR 기본 제한을 사용합니다.")
        except (TypeError, ValueError, json.JSONDecodeError):
            # 잘못된 사용자 override가 안전한 내장 제한까지 제거하지 않도록 합니다.
            log.error("KIS_TR_RATE_LIMITS_JSON 파싱 실패. 내장 TR 기본 제한을 사용합니다.")
        effective_tr_config = dict(default_tr_config)
        for tr_code, limits in tr_config.items():
            key = str(tr_code).strip()
            if not key or not isinstance(limits, dict):
                log.error("잘못된 TR별 제한 항목을 무시합니다: tr_id=%s", key or "EMPTY")
                continue
            merged = {**effective_tr_config.get(key, {}), **limits}
            try:
                per_sec = float(merged.get("per_sec", settings.max_calls_per_sec))
                per_min = int(merged.get("per_min", settings.max_calls_per_min))
                if per_sec <= 0 or per_min <= 0:
                    raise ValueError("limit must be positive")
            except (TypeError, ValueError):
                log.error("TR별 제한값이 유효하지 않아 해당 TR의 내장 기본값을 유지합니다: tr_id=%s", key)
                continue
            effective_tr_config[key] = merged
        for tr_code, limits in effective_tr_config.items():
            try:
                per_sec = float(limits.get("per_sec", settings.max_calls_per_sec))
                per_min = int(limits.get("per_min", settings.max_calls_per_min))
                self.tr_limiters[str(tr_code).strip()] = RateLimiter(
                    per_sec, per_min, settings.rate_limit_safety_margin)
            except (TypeError, ValueError):
                log.error("TR별 내장 제한 초기화 실패; 해당 TR은 공통 limiter를 사용합니다: tr_id=%s", tr_code)
        self._token_lock = threading.Lock()
        self._approval_lock = threading.Lock()
        self._cache_lock = threading.RLock()
        # 조건검색 목록은 매 scan마다 바뀌지 않으므로 짧게 캐시해 불필요한 REST 호출을 줄입니다.
        # 기본 TTL 60초이며, HTS에서 조건을 새로 저장한 경우 TTL 이후 자동 반영됩니다.
        self._condition_titles_cache: list[dict] | None = None
        self._condition_titles_cache_at = 0.0
        self._condition_titles_lock = threading.Lock()
        self._load_token_cache()

    def _load_token_cache(self) -> None:
        """같은 KIS 환경/App Key의 만료되지 않은 REST·WebSocket 키를 복구합니다."""
        try:
            with self.token_cache_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            try:
                os.chmod(self.token_cache_path, 0o600)
            except OSError:
                pass
            if not isinstance(data, dict) or data.get("credential_fingerprint") != self._credential_fingerprint:
                log.info("KIS 토큰 캐시가 현재 환경/App Key와 달라 재사용하지 않습니다.")
                return
            now = time.time()
            self._token_retry_after = float(data.get("rest_retry_after_epoch", 0) or 0)
            rest_token = str(data.get("rest_access_token", "") or "")
            rest_expiry = float(data.get("rest_expires_at_epoch", 0) or 0)
            if rest_token and rest_expiry > now + 300:
                self.token = rest_token
                self.token_expire_at = rest_expiry
            ws_key = str(data.get("ws_approval_key", "") or "")
            ws_expiry = float(data.get("ws_expires_at_epoch", 0) or 0)
            if ws_key and ws_expiry > now + 300:
                self.approval_key = ws_key
                self._ws_approval_expire_at = ws_expiry
            if self.token or self.approval_key:
                log.info("KIS 토큰 캐시 복구: REST=%s WebSocket=%s",
                         "유효" if self.token else "없음/만료", "유효" if self.approval_key else "없음/만료")
        except FileNotFoundError:
            # 최초 실행은 정상입니다. 키 발급 후 캐시 파일을 만듭니다.
            return
        except (OSError, ValueError, TypeError) as exc:
            # 캐시가 손상되어도 원본은 보존하고, 발급 시도는 API 서버의 제한을 따릅니다.
            log.warning("KIS 토큰 캐시를 읽지 못했습니다. 원본을 보존합니다: %s", type(exc).__name__)

    def _save_token_cache(self) -> None:
        """두 인증키와 만료시각을 원자 저장하고 파일 권한을 소유자 전용으로 제한합니다."""
        # 구형 단위 테스트/외부 어댑터가 __new__로 만든 부분 객체는 저장하지 않습니다.
        if not hasattr(self, "_cache_lock") or not hasattr(self, "token_cache_path"):
            return
        with self._cache_lock:
            path = self.token_cache_path
            payload = {
                "schema_version": 1,
                "credential_fingerprint": self._credential_fingerprint,
                "environment": "paper" if self.s.paper else "live",
                "rest_access_token": self.token,
                "rest_expires_at_epoch": self.token_expire_at,
                "rest_retry_after_epoch": self._token_retry_after,
                "rest_expires_at_kst": datetime.fromtimestamp(
                    self.token_expire_at, ZoneInfo("Asia/Seoul")
                ).strftime("%Y-%m-%d %H:%M:%S") if self.token_expire_at else "",
                "ws_approval_key": self.approval_key,
                "ws_expires_at_epoch": self._ws_approval_expire_at,
                "ws_expires_at_kst": datetime.fromtimestamp(
                    self._ws_approval_expire_at, ZoneInfo("Asia/Seoul")
                ).strftime("%Y-%m-%d %H:%M:%S") if self._ws_approval_expire_at else "",
                "updated_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_name = None
            try:
                fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
                try:
                    os.fchmod(fd, 0o600)
                except (AttributeError, OSError):
                    pass
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
                tmp_name = None
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                # 키를 메모리에서 사용 가능하면 API 호출은 계속하되, 캐시 저장 실패를 명시합니다.
                log.error("KIS 토큰 캐시 저장 실패: path=%s error=%s", path, type(exc).__name__)
            finally:
                if tmp_name:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass

    @staticmethod
    def _request_priority(method: str, tr_id: str) -> int:
        """계좌/주문 정합성 요청을 전략용 시세 수집보다 우선합니다."""
        code = str(tr_id or "").upper()
        if method.upper() == "POST" or code.endswith(("0011U", "0012U", "0013U")):
            return 0  # 주문/취소
        if code.endswith(("8434R", "0081R", "8908R")):
            return 1  # 잔고 및 주문/체결 대사
        if "01010100" in code:
            return 3  # 현재가
        if "03010100" in code:
            return 10  # 일봉/전략 데이터
        return 5

    def _notify_account_context_error(self, msg_cd: str, msg1: str, method: str, path: str) -> None:
        """계좌/인증 단위 오류를 주문 관리자에 전달합니다. callback 실패가 원본 API 오류를 덮지 않습니다."""
        code = str(msg_cd or "").strip().upper()
        handler = getattr(self, "account_error_handler", None)
        if code not in _ACCOUNT_CONTEXT_ERROR_CODES or not callable(handler):
            return
        try:
            handler(msg_cd=code, msg1=str(msg1 or ""), method=str(method), path=str(path))
        except Exception as exc:
            log.critical("계좌 주문 잠금 callback 실패: msg_cd=%s method=%s path=%s error=%s",
                         code, method, path, type(exc).__name__)

    def _request(self, method: str, path: str, *, tr_id: str, params=None, body=None, retry: int | None = None) -> dict:
        """REST 요청을 보냅니다. 조회만 자동 재시도하고 주문 POST는 중복 제출 방지를 위해 재시도하지 않습니다."""
        method = method.upper().strip()
        url = self.s.api_base + path
        # 주문/취소 POST는 응답 유실 시 서버에서 이미 접수됐을 수 있습니다.
        # GET만 기본 재시도합니다. 호출자가 retry를 넘겨도 POST는 재시도하지 않습니다.
        safe_retry = (2 if retry is None else max(0, int(retry))) if method in {"GET", "HEAD"} else 0
        for attempt in range(safe_retry + 1):
            request_started = time.monotonic()
            safe_params = {}
            for key, value in (params or {}).items():
                key_text = str(key)
                if key_text.lower() in {"user_id", "appkey", "appsecret", "authorization", "token"}:
                    continue
                if key_text.upper() == "CANO":
                    safe_params[key_text] = _mask_account(value)
                else:
                    safe_params[key_text] = value
            log.debug("KIS_REST_REQUEST method=%s path=%s tr_id=%s attempt=%d/%d params=%s",
                      method, path, tr_id, attempt + 1, safe_retry + 1, safe_params)
            token = self.ensure_token()
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": self.s.app_key,
                "appsecret": self.s.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            }
            request_priority = self._request_priority(method, tr_id)
            tr_limiter = getattr(self, "tr_limiters", {}).get(str(tr_id))
            tr_wait = tr_limiter.acquire(priority=request_priority) if tr_limiter else 0.0
            limiter_wait = self.limiter.acquire(priority=request_priority)
            limiter_wait += tr_wait
            if limiter_wait >= 2.0:
                stats = self.limiter.stats()
                log.warning("KIS_RATE_LIMIT_WAIT method=%s path=%s tr_id=%s attempt=%d waited_sec=%.2f recent_1s=%d recent_60s=%d minute_limit=%d cooldown_remaining_sec=%.2f",
                            method, path, tr_id, attempt + 1, limiter_wait, stats["recent_1s"],
                            stats["recent_60s"], stats["configured_min_limit"], stats["cooldown_remaining_sec"])
            try:
                response = self.session.request(method, url, headers=headers, params=params, json=body, timeout=10)
            except requests.RequestException as exc:
                elapsed_ms = (time.monotonic() - request_started) * 1000
                log.warning("KIS_REST_NETWORK_ERROR method=%s path=%s tr_id=%s attempt=%d elapsed_ms=%.1f error_type=%s error=%s",
                            method, path, tr_id, attempt + 1, elapsed_ms, type(exc).__name__, _redact_sensitive_text(exc))
                if method not in {"GET", "HEAD"}:

                    raise AmbiguousOrderError(f"{method} {path} 응답 불명확: 브로커 접수 여부 확인 전 재전송 금지: {_redact_sensitive_text(exc)}") from exc
                if attempt >= safe_retry:
                    raise KisApiError(f"네트워크 오류: {_redact_sensitive_text(exc)}") from exc
                time.sleep(0.5 * (2 ** attempt))
                continue
            elapsed_ms = (time.monotonic() - request_started) * 1000
            try:
                response_summary = response.json()
            except (ValueError, AttributeError):
                response_summary = {}
            response_msg_cd = str(response_summary.get("msg_cd", response_summary.get("error_code", "")) or "").strip()
            response_rt_cd = str(response_summary.get("rt_cd", "") or "").strip()
            if response.status_code >= 400 or response_rt_cd not in {"", "0"}:
                log.warning("KIS_REST_RESPONSE_ERROR method=%s path=%s tr_id=%s attempt=%d http_status=%s rt_cd=%s msg_cd=%s elapsed_ms=%.1f msg=%s",
                            method, path, tr_id, attempt + 1, response.status_code, response_rt_cd,
                            response_msg_cd, elapsed_ms, _redact_sensitive_text(response_summary.get("msg1", response_summary.get("error_description", response.text[:240])))[:240])
            else:
                log.info("KIS_REST_RESPONSE method=%s path=%s tr_id=%s attempt=%d http_status=%s rt_cd=%s elapsed_ms=%.1f",
                         method, path, tr_id, attempt + 1, response.status_code, response_rt_cd or "0", elapsed_ms)
            if response.status_code == 401 and attempt < safe_retry:
                with self._token_lock:
                    self.token = ""
                    self.token_expire_at = 0.0
                    self._save_token_cache()
                continue
            if response.status_code in (429, 500, 502, 503, 504):
                # KIS가 HTTP 500 안에 EGW00201 JSON을 실어 보내는 경우가 있습니다.
                # 이건 일반적인 응답 유실과 구분 가능한 명시적 호출 제한 거절입니다.
                try:
                    error_payload = response.json()
                except (ValueError, AttributeError):
                    error_payload = {}
                msg_cd = str(error_payload.get("msg_cd", "")).strip().upper()
                if msg_cd in _ACCOUNT_CONTEXT_ERROR_CODES:
                    self._notify_account_context_error(msg_cd, error_payload.get("msg1", ""), method, path)
                if response.status_code == 429:
                    try:
                        retry_after_sec = float(response.headers.get("Retry-After", "5") or 5)
                    except (TypeError, ValueError):
                        retry_after_sec = 5.0
                    cooldown = max(1.0, retry_after_sec, float(os.getenv("KIS_RATE_LIMIT_COOLDOWN_SEC", "10")))
                    self.limiter.defer(cooldown, "HTTP 429 rate limit")
                    log.error("KIS_RATE_LIMIT_COOLDOWN source=http_429 path=%s tr_id=%s cooldown_sec=%.1f", path, tr_id, cooldown)
                    raise RateLimitError(f"KIS 호출 제한 HTTP 429: {_redact_sensitive_text(error_payload.get('msg1', response.text[:300]))}; cooldown={cooldown:.1f}s")
                if _is_rate_limit_payload(error_payload):
                    cooldown = max(1.0, float(os.getenv("KIS_RATE_LIMIT_COOLDOWN_SEC", "10")))
                    reason = f"HTTP 응답 {msg_cd or 'RATE_LIMIT'}"
                    self.limiter.defer(cooldown, reason)
                    log.error("KIS_RATE_LIMIT_COOLDOWN source=%s path=%s tr_id=%s cooldown_sec=%.1f", msg_cd or "RATE_LIMIT", path, tr_id, cooldown)
                    raise RateLimitError(
                        f"KIS 호출 제한 {msg_cd or 'RATE_LIMIT'} HTTP {response.status_code}: "
                        f"{_redact_sensitive_text(error_payload.get('msg1', response.text[:300]))}; cooldown={cooldown:.1f}s"
                    )
                # HTTP 5xx라도 KIS가 유효한 JSON 업무거절(rt_cd != 0)을 명시하면
                # 접수 불명확으로 오분류하지 않습니다. POST는 어떤 경우에도 자동 재전송하지 않습니다.
                if str(error_payload.get("rt_cd", "")) not in ("", "0", "None") and error_payload.get("msg1"):
                    if (method == "POST" and path.endswith("/trading/order-cash")
                            and tr_id in {"VTTC0012U", "TTTC0012U"}
                            and _is_buying_power_rejection(error_payload)):
                        raise InsufficientBuyingPowerError(
                            f"KIS 현금매수 한도 부족 명시 거절: msg_cd={msg_cd} msg={error_payload.get('msg1')}",
                            msg_cd=msg_cd, msg1=_redact_sensitive_text(error_payload.get("msg1", "")))
                    raise KisApiError(f"KIS 명시 거절 HTTP {response.status_code}: rt_cd={error_payload.get('rt_cd')} msg={_redact_sensitive_text(error_payload.get('msg1'))} msg_cd={msg_cd}",
                                      msg_cd=msg_cd, msg1=_redact_sensitive_text(error_payload.get("msg1", "")),
                                      http_status=response.status_code)
                if method not in {"GET", "HEAD"}:
                    raise AmbiguousOrderError(f"{method} {path} HTTP {response.status_code}: 주문 접수 여부를 조회해야 합니다. 응답={_redact_sensitive_text(response.text[:300])}")
                if attempt >= safe_retry:
                    raise KisApiError(f"HTTP {response.status_code}: {_redact_sensitive_text(response.text[:500])}",
                                      msg_cd=msg_cd, msg1=_redact_sensitive_text(error_payload.get("msg1", "")),
                                      http_status=response.status_code)
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = 0.5 * (2 ** attempt)
                time.sleep(min(max(0.1, delay), 10.0))
                continue
            if response.status_code >= 400:
                try:
                    http_error_payload = response.json()
                except (ValueError, AttributeError):
                    http_error_payload = {}
                msg_cd = str(http_error_payload.get("msg_cd", "")).strip().upper()
                if msg_cd in _ACCOUNT_CONTEXT_ERROR_CODES:
                    self._notify_account_context_error(msg_cd, http_error_payload.get("msg1", ""), method, path)
                if (method == "POST" and path.endswith("/trading/order-cash")
                        and tr_id in {"VTTC0012U", "TTTC0012U"}
                        and str(http_error_payload.get("rt_cd", "")) not in ("", "0", "None")
                        and _is_buying_power_rejection(http_error_payload)):
                    msg_cd = str(http_error_payload.get("msg_cd", "")).strip().upper()
                    safe_msg1 = _redact_sensitive_text(http_error_payload.get("msg1", ""))
                    raise InsufficientBuyingPowerError(
                        f"KIS 현금매수 한도 부족 명시 거절: msg_cd={msg_cd} msg={safe_msg1}",
                        msg_cd=msg_cd, msg1=safe_msg1,
                    )
                raise KisApiError(f"HTTP {response.status_code}: {_redact_sensitive_text(response.text[:500])}",
                                  msg_cd=msg_cd, msg1=_redact_sensitive_text(http_error_payload.get("msg1", "")),
                                  http_status=response.status_code)
            try:
                data = response.json()
            except ValueError as exc:
                if method not in {"GET", "HEAD"}:
                    raise AmbiguousOrderError(f"{method} {path} 응답 JSON 불명확: 주문 접수 여부를 조회해야 합니다.") from exc
                raise KisApiError(f"JSON 응답 파싱 실패: {_redact_sensitive_text(response.text[:500])}") from exc
            if data.get("rt_cd") not in (None, "0"):
                msg_cd = str(data.get("msg_cd", "")).strip().upper()
                safe_msg1 = _redact_sensitive_text(data.get("msg1", ""))
                if msg_cd in _ACCOUNT_CONTEXT_ERROR_CODES:
                    self._notify_account_context_error(msg_cd, safe_msg1, method, path)
                message = f"KIS error rt_cd={data.get('rt_cd')} msg={safe_msg1} msg_cd={msg_cd}"
                if _is_rate_limit_payload(data):
                    # EGW00201/EGW00215는 명시적인 호출 제한 거절입니다.
                    # 주문 POST를 재전송하지 않고, 공용 limiter cooldown으로 모든 REST 요청을 늦춥니다.
                    cooldown = max(1.0, float(os.getenv("KIS_RATE_LIMIT_COOLDOWN_SEC", "10")))
                    self.limiter.defer(cooldown, f"응답 {msg_cd or 'RATE_LIMIT'}")
                    log.error("KIS_RATE_LIMIT_COOLDOWN source=%s path=%s tr_id=%s cooldown_sec=%.1f", msg_cd or "RATE_LIMIT", path, tr_id, cooldown)
                    raise RateLimitError(f"{message}; cooldown={cooldown:.1f}s")
                if (method == "POST" and path.endswith("/trading/order-cash")
                        and tr_id in {"VTTC0012U", "TTTC0012U"}
                        and _is_buying_power_rejection(data)):
                    raise InsufficientBuyingPowerError(
                        message, msg_cd=msg_cd, msg1=safe_msg1)
                raise KisApiError(message, msg_cd=msg_cd, msg1=safe_msg1, http_status=response.status_code)
            data["_tr_cont"] = response.headers.get("tr_cont", "")
            return data
        raise KisApiError("API 호출 실패")

    def ensure_token(self) -> str:
        """접근토큰을 필요할 때만 발급합니다."""
        with self._token_lock:
            now = time.time()
            if self.token and now < self.token_expire_at - 300:
                return self.token
            if now < self._token_retry_after:
                remaining = self._token_retry_after - now
                raise KisApiError(
                    f"KIS 접근토큰 발급 재시도 대기 중: 약 {remaining:.0f}초 남음 "
                    "(EGW00133 등 발급 제한 후 반복 호출 방지)"
                )
            limiter_wait = self.limiter.acquire()
            if limiter_wait >= 2.0:
                stats = self.limiter.stats()
                log.warning("KIS_RATE_LIMIT_WAIT method=POST path=/oauth2/tokenP tr_id=TOKEN waited_sec=%.2f recent_1s=%d recent_60s=%d minute_limit=%d cooldown_remaining_sec=%.2f",
                            limiter_wait, stats["recent_1s"], stats["recent_60s"],
                            stats["configured_min_limit"], stats["cooldown_remaining_sec"])
            try:
                response = self.session.post(
                    self.s.api_base + "/oauth2/tokenP",
                    json={"grant_type": "client_credentials", "appkey": self.s.app_key, "appsecret": self.s.app_secret},
                    timeout=10,
                )
            except requests.RequestException as exc:
                raise KisApiError(f"토큰 발급 네트워크 오류: {exc}") from exc
            if response.status_code >= 400:
                try:
                    error_payload = response.json()
                except (ValueError, AttributeError):
                    error_payload = {}
                error_code = str(error_payload.get("error_code", "")).strip().upper()
                error_text = _redact_sensitive_text(error_payload.get("error_description", response.text[:500]))
                if error_code == "EGW00133" or "1분당 1회" in error_text:
                    # 발급 제한 응답 뒤 즉시 반복 요청하면 제한을 더 악화시킬 수 있습니다.
                    # 재시작 후에도 같은 제한을 존중하도록 남은 대기시각을 JSON에 저장합니다.
                    self._token_retry_after = time.time() + 60.0
                    self._save_token_cache()
                raise KisApiError(
                    f"토큰 발급 실패 HTTP {response.status_code}: "
                    f"{error_code or 'UNKNOWN'} {error_text[:500]}"
                )
            try:
                data = response.json()
                self.token = data["access_token"]
            except (ValueError, KeyError) as exc:
                raise KisApiError(f"토큰 응답 형식 오류: {_redact_sensitive_text(response.text[:500])}") from exc
            # API 응답의 만료 일시를 우선 사용합니다. 없을 때만 expires_in(기본 24시간)을 씁니다.
            expiry_text = str(data.get("access_token_token_expired", "") or "").strip()
            parsed_expiry = 0.0
            if expiry_text:
                try:
                    parsed_expiry = datetime.strptime(expiry_text, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=ZoneInfo("Asia/Seoul")
                    ).timestamp()
                except ValueError:
                    parsed_expiry = 0.0
            self.token_expire_at = parsed_expiry or (time.time() + int(data.get("expires_in", 86400)))
            self._token_retry_after = 0.0
            self._save_token_cache()
            return self.token

    def issue_ws_approval_key(self) -> str:
        """WebSocket approval_key를 발급합니다."""
        with self._approval_lock:
            if self.approval_key and time.time() < self._ws_approval_expire_at - 300:
                return self.approval_key
            limiter_wait = self.limiter.acquire()
            if limiter_wait >= 2.0:
                stats = self.limiter.stats()
                log.warning("KIS_RATE_LIMIT_WAIT method=POST path=/oauth2/Approval tr_id=WS_APPROVAL waited_sec=%.2f recent_1s=%d recent_60s=%d minute_limit=%d cooldown_remaining_sec=%.2f",
                            limiter_wait, stats["recent_1s"], stats["recent_60s"],
                            stats["configured_min_limit"], stats["cooldown_remaining_sec"])
            try:
                response = self.session.post(
                    self.s.api_base + "/oauth2/Approval",
                    json={"grant_type": "client_credentials", "appkey": self.s.app_key, "secretkey": self.s.app_secret},
                    timeout=10,
                )
            except requests.RequestException as exc:
                raise KisApiError(f"WebSocket approval key 네트워크 오류: {exc}") from exc
            if response.status_code >= 400:
                raise KisApiError(f"WebSocket approval key 발급 실패 HTTP {response.status_code}: {_redact_sensitive_text(response.text[:500])}")
            try:
                self.approval_key = response.json()["approval_key"]
            except (ValueError, KeyError) as exc:
                raise KisApiError(f"approval_key 응답 형식 오류: {_redact_sensitive_text(response.text[:500])}") from exc
            # 공식 샘플은 접속키를 24시간 기준으로 재발급합니다. 만료 5분 전부터 갱신합니다.
            self._ws_approval_expire_at = time.time() + 86400
            self._save_token_cache()
            return self.approval_key

    def condition_titles(self, force_refresh: bool = False) -> list[dict]:
        """HTS 저장 조건 목록을 짧게 캐시해 반복 REST 호출과 호출 제한 위험을 낮춥니다.

        force_refresh=True는 운영자가 즉시 최신 HTS 조건 목록을 확인해야 할 때 사용합니다.
        캐시 TTL은 KIS_CONDITION_CACHE_TTL_SEC(기본 60초)이며 목록이 비어 있어도 TTL 동안 캐시합니다.
        """
        ttl = max(0.0, min(600.0, float(os.getenv("KIS_CONDITION_CACHE_TTL_SEC", "60"))))
        with self._condition_titles_lock:
            now = time.monotonic()
            if (not force_refresh and self._condition_titles_cache is not None
                    and now - self._condition_titles_cache_at < ttl):
                return [dict(row) for row in self._condition_titles_cache]
            data = self._request("GET", "/uapi/domestic-stock/v1/quotations/psearch-title",
                                 tr_id="HHKST03900300", params={"user_id": self.s.hts_id})
            rows = data.get("output2") or []
            if not isinstance(rows, list):
                raise KisApiError("HTS 조건검색 목록 응답 output2 형식이 list가 아닙니다.")
            self._condition_titles_cache = [dict(row) for row in rows if isinstance(row, dict)]
            self._condition_titles_cache_at = time.monotonic()
            return [dict(row) for row in self._condition_titles_cache]

    def condition_result(self, seq: str) -> list[dict]:
        """HTS 조건 하나의 종목 결과를 조회합니다.

        KIS는 검색 결과가 0건인 경우에도 rt_cd=1 / MCA05918을 반환할 수 있습니다.
        이 코드는 조건검색 장애가 아니라 빈 결과로 정규화합니다. seq="0"도 유효한 값이므로
        임의로 거부하거나 다른 seq로 대체하지 않습니다.
        """
        try:
            data = self._request("GET", "/uapi/domestic-stock/v1/quotations/psearch-result",
                                 tr_id="HHKST03900400", params={"user_id": self.s.hts_id, "seq": str(seq)})
        except KisApiError as exc:
            if getattr(exc, "msg_cd", "") == "MCA05918":
                log.info("HTS 조건검색 결과 0건(정상 빈 결과): seq=%s msg_cd=MCA05918", seq)
                return []
            raise
        rows = data.get("output2") or []
        if not isinstance(rows, list):
            raise KisApiError(f"HTS 조건검색 결과 output2 형식 오류: seq={seq}")
        return rows

    def find_condition_seq(self, condition_name: str) -> str:
        """조건명과 정확히 일치하는 seq를 반환합니다."""
        wanted = condition_name.strip()
        rows = self.condition_titles()
        for row in rows:
            name = str(row.get("condition_nm", "")).strip()
            if name == wanted:
                seq = str(row.get("seq", "")).strip()
                if not seq:
                    raise KisApiError(f"조건은 찾았지만 seq가 없습니다: {wanted}")
                return seq
        available = [str(r.get("condition_nm", "")).strip() for r in rows if r.get("condition_nm")]
        raise KisApiError(f"HTS 조건을 찾지 못했습니다: {wanted}; 현재 조건={available}")

    def current_price(self, symbol: str) -> dict:
        """WebSocket 가격이 없거나 오래된 경우 REST 현재가를 조회합니다."""
        return self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-price", tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        ).get("output", {})

    def check_holiday(self, date_yyyymmdd: str) -> bool:
        """KIS chk-holiday API로 해당 날짜가 영업일인지 확인합니다.

        조회 실패는 휴장으로 간주하지 않고 예외로 전달해 매매 시작을 fail-closed 합니다.
        """
        params = {
            "BASS_DT": str(date_yyyymmdd),
            "CTX_AREA_NK100": "",
            "CTX_AREA_FK100": "",
        }
        data = self._request(
            "GET", "/uapi/domestic-stock/v1/quotations/chk-holiday",
            tr_id="CTCA0903R", params=params,
        )
        rows = data.get("output") or data.get("output1") or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if str(row.get("bass_dt", row.get("BASS_DT", ""))).strip() == str(date_yyyymmdd):
                return str(row.get("bzdy_yn", row.get("BIZ_DAY_YN", "N"))).upper() == "Y"
        # 일부 응답은 조회 기준일 단일 행을 반환하므로 날짜 필드가 없어도 첫 행을 사용합니다.
        if rows:
            return str(rows[0].get("bzdy_yn", rows[0].get("BIZ_DAY_YN", "N"))).upper() == "Y"
        raise KisApiError(f"휴장일 API 응답에 결과가 없습니다: {date_yyyymmdd}")

    def daily_prices(self, symbol: str, days: int = 80) -> list[dict]:
        """일봉 OHLCV를 조회해 전략 평가용 공통 필드로 반환합니다.

        KIS 차트 API 응답 건수 제한에 걸려 장기 전략(예: 252거래일 신고가)이 최근
        일부 봉만 받지 않도록 날짜 구간을 나누어 조회하고 날짜 기준으로 중복 제거합니다.
        80일 이하 요청은 기존과 같이 보통 1회 호출이며, 장기 요청은 구간 수만큼 호출합니다.
        """
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        requested = max(25, int(days))
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        cursor_end = today
        collected: dict[str, dict] = {}
        # 120 calendar days는 휴장일을 제외해도 약 80거래일입니다. 최대 8구간으로 과도한 호출을 방지합니다.
        max_chunks = min(8, max(1, (requested + 59) // 60))
        for _ in range(max_chunks):
            cursor_start = cursor_end - timedelta(days=120 if requested > 80 else max(120, requested * 3))
            data = self._request(
                "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                tr_id="FHKST03010100",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": str(symbol).zfill(6),
                    "FID_INPUT_DATE_1": cursor_start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": cursor_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
                },
            )
            raw = data.get("output2") or data.get("output") or []
            for row in raw:
                date = str(row.get("stck_bsop_date", row.get("STCK_BSOP_DATE", ""))).strip()
                close = row.get("stck_clpr", row.get("STCK_CLPR", 0))
                try:
                    if not date or float(str(close).replace(",", "")) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                collected[date] = {
                    "date": date,
                    "open": row.get("stck_oprc", row.get("STCK_OPRC", 0)),
                    "high": row.get("stck_hgpr", row.get("STCK_HGPR", 0)),
                    "low": row.get("stck_lwpr", row.get("STCK_LWPR", 0)),
                    "close": close,
                    "volume": row.get("acml_vol", row.get("ACML_VOL", 0)),
                }
            if len(collected) >= requested or not raw:
                break
            # 다음 구간은 방금 조회한 구간보다 하루 이전에서 시작해 중복/누락을 최소화합니다.
            cursor_end = cursor_start - timedelta(days=1)
        bars = [collected[key] for key in sorted(collected)]
        return bars[-requested:]

    def order_cash(self, symbol: str, side: str, qty: int, price: int = 0, market: bool = True, best_limit: bool = False) -> dict:
        """현금 매수/매도 주문을 제출합니다.

        KIS 공식 최신 examples_llm의 TR 매핑을 사용합니다.
        주문 성공은 접수 성공이지 체결 성공이 아니며, 체결통보/조회로 확인합니다.
        KIS_TEST_MODE=1인 테스트 프로세스에서는 실수로 실제 주문이 나가지 않도록 차단합니다.
        """
        if os.getenv("KIS_TEST_MODE", "").strip() == "1":
            raise KisApiError("KIS_TEST_MODE=1: 테스트 실행 중 실제 주문 POST를 차단했습니다.")
        side = side.upper().strip()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if qty <= 0:
            raise ValueError("qty must be positive")
        if market:
            ord_dvsn, ord_unpr = "01", "0"
        elif best_limit:
            # KIS 국내주식 주문 구분: 03=최유리지정가. 가격은 0으로 전달합니다.
            ord_dvsn, ord_unpr = "03", "0"
        else:
            if price <= 0:
                raise ValueError("일반 지정가 주문은 price가 필요합니다.")
            ord_dvsn, ord_unpr = "00", str(int(price))
        if self.s.paper:
            tr_id = "VTTC0012U" if side == "BUY" else "VTTC0011U"
        else:
            tr_id = "TTTC0012U" if side == "BUY" else "TTTC0011U"
        body = {
            "CANO": self.s.cano,
            "ACNT_PRDT_CD": self.s.acnt_prdt_cd,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": ord_unpr,
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "",
            "CNDT_PRIC": "",
        }
        return self._request("POST", "/uapi/domestic-stock/v1/trading/order-cash", tr_id=tr_id, body=body)

    def cancel_order(self, orgn_odno: str, krx_fwdg_ord_orgno: str) -> dict:
        """원주문 잔량 전량 취소를 요청합니다."""
        if os.getenv("KIS_TEST_MODE", "").strip() == "1":
            raise KisApiError("KIS_TEST_MODE=1: 테스트 실행 중 실제 주문 취소 POST를 차단했습니다.")
        if not orgn_odno or not krx_fwdg_ord_orgno:
            raise ValueError("원주문번호와 KRX_FWDG_ORD_ORGNO가 필요합니다.")
        tr_id = "VTTC0013U" if self.s.paper else "TTTC0013U"
        body = {
            "CANO": self.s.cano,
            "ACNT_PRDT_CD": self.s.acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno,
            "ORGN_ODNO": orgn_odno,
            "ORD_DVSN": "01",
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        return self._request("POST", "/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id=tr_id, body=body)

    def orderable_cash(self, symbol: str = "005930", unit_price: int = 0, order_mode: str = "MARKET") -> dict:
        """국내주식 매수가능조회 API에서 주문가능현금 필드를 가져옵니다.

        잔고조회(inquire-balance)의 output2는 계좌 요약이며, 모든 응답에서
        ord_psbl_cash가 제공된다고 가정할 수 없습니다. KIS 공식 API 예제의
        별도 inquire-psbl-order 엔드포인트를 사용합니다.

        공식 open-trading-api 예제의 TR/필드에 맞춰 종목, 주문가격, 주문구분을 전달합니다.
        응답의 ord_psbl_cash와 미수 없는 nrcvb_buy_amt/nrcvb_buy_qty를 별도 상한으로 사용합니다.
        시장가는 ORD_UNPR 공란, 최유리는 0, 지정가는 실제 주문가격을 전달합니다.
        """
        pdno = str(symbol or "005930").strip().zfill(6)
        mode = str(order_mode or "MARKET").strip().upper()
        ord_dvsn = {"MARKET": "01", "BEST_LIMIT": "03", "LIMIT": "00"}.get(mode)
        if ord_dvsn is None:
            raise ValueError(f"지원하지 않는 매수가능조회 주문 방식: {mode}")
        if mode == "MARKET":
            # KIS 공식 샘플의 시장가 매수가능조회는 ORD_UNPR 공란을 사용합니다.
            ord_unpr = ""
        elif mode == "BEST_LIMIT":
            # 실제 최유리지정가 주문과 동일하게 주문가격 0을 사용합니다.
            ord_unpr = "0"
        else:
            try:
                price = int(float(str(unit_price or 0).replace(",", "")))
            except (TypeError, ValueError):
                price = 0
            if price <= 0:
                quote = self.current_price(pdno)
                try:
                    price = int(float(str(quote.get("stck_prpr", quote.get("STCK_PRPR", 0)) or 0).replace(",", "")))
                except (TypeError, ValueError):
                    price = 0
            if price <= 0:
                raise KisApiError(f"매수가능조회 단가를 확인할 수 없습니다: symbol={pdno}")
            ord_unpr = str(price)
        tr_id = "VTTC8908R" if self.s.paper else "TTTC8908R"
        data = self._request(
            "GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            tr_id=tr_id,
            params={
                "CANO": self.s.cano,
                "ACNT_PRDT_CD": self.s.acnt_prdt_cd,
                "PDNO": pdno,
                "ORD_UNPR": ord_unpr,
                "ORD_DVSN": ord_dvsn,
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = data.get("output") or data.get("output1") or {}
        if isinstance(output, list):
            output = output[0] if output and isinstance(output[0], dict) else {}
        if not isinstance(output, dict):
            output = {}
        # Preserve the broker's original string value; the caller performs numeric validation.
        return output

    def balance(self) -> tuple[list, dict]:
        """잔고와 주문가능현금을 조회해 통합된 계좌 요약을 반환합니다.

        잔고조회 output2를 파싱한 뒤, ord_psbl_cash가 없거나 0이면 KIS 공식
        매수가능조회 API를 추가 호출합니다. 매수가능조회 실패 시 잔고 데이터는
        반환하되 필드를 임의 추정하지 않습니다. 이 경우 상위 안전 게이트가 매수를 차단합니다.
        """
        tr_id = "VTTC8434R" if self.s.paper else "TTTC8434R"
        all_rows: list[dict] = []
        summary: dict = {}
        fk100 = ""
        nk100 = ""
        for _ in range(10):
            params = {
                "CANO": self.s.cano, "ACNT_PRDT_CD": self.s.acnt_prdt_cd,
                "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "01", "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                "CTX_AREA_FK100": fk100, "CTX_AREA_NK100": nk100,
            }
            data = self._request("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", tr_id=tr_id, params=params)
            all_rows.extend(data.get("output1") or [])
            output2 = data.get("output2") or {}
            # KIS 잔고조회 응답의 output2는 환경/응답 형태에 따라
            # dict가 아니라 1개 항목을 담은 list로 올 수 있습니다.
            # 이를 놓치면 총자산이 0으로 남아 일일손실률이 -inf가 됩니다.
            if isinstance(output2, list):
                if output2 and isinstance(output2[0], dict):
                    summary = output2[0]
            elif isinstance(output2, dict):
                summary = output2
            fk100 = str(data.get("ctx_area_fk100", "") or "")
            nk100 = str(data.get("ctx_area_nk100", "") or "")
            tr_cont = str(data.get("_tr_cont", "") or data.get("tr_cont", "") or data.get("header", {}).get("tr_cont", ""))
            if tr_cont not in {"M", "F"} or not fk100 and not nk100:
                break
            time.sleep(0.2)
        # 일부 계좌/응답의 잔고 output2에는 ord_psbl_cash가 없으므로 공식
        # 매수가능조회로 보완합니다. 실패를 예수금으로 대체하지 않는 fail-closed 정책입니다.
        try:
            existing_orderable = 0.0
            try:
                existing_orderable = float(str(summary.get("ord_psbl_cash", summary.get("ORD_PSBL_CASH", 0)) or 0).replace(",", ""))
            except (TypeError, ValueError):
                existing_orderable = 0.0
            if existing_orderable <= 0:
                first_holding = next((row for row in all_rows
                                      if str(row.get("pdno", row.get("PDNO", ""))).strip()), {})
                first_symbol = str(first_holding.get("pdno", first_holding.get("PDNO", "005930"))).strip().zfill(6)
                raw_price = first_holding.get("prpr", first_holding.get("STCK_PRPR", 0))
                buyable = self.orderable_cash(first_symbol, raw_price)
                raw_cash = buyable.get("ord_psbl_cash", buyable.get("ORD_PSBL_CASH"))
                if raw_cash not in (None, ""):
                    # A legitimate zero remains zero; it is not replaced with deposit cash.
                    summary["ord_psbl_cash"] = str(raw_cash)
                else:
                    log.warning("KIS 매수가능조회 응답에 ord_psbl_cash 필드가 없습니다: fields=%s", sorted(buyable.keys()))
        except Exception as exc:
            log.warning("KIS 매수가능조회 실패; 신규 매수 안전 게이트에서 차단합니다: %s", exc)
        # 금액 차이 원인을 사후 대조할 수 있도록 KIS 원본 요약 필드와 보유행 합계를 남깁니다.
        # 전체 응답/계좌 식별정보는 기록하지 않고 정합성 확인에 필요한 필드만 선택합니다.
        audit_keys = (
            "tot_evlu_amt", "TOT_EVLU_AMT", "nass_amt", "NASS_AMT", "tot_asst_amt", "TOT_ASST_AMT",
            "dnca_tot_amt", "DNCA_TOT_AMT", "ord_psbl_cash", "ORD_PSBL_CASH",
            "pchs_amt_smtl_amt", "PCHS_AMT_SMTL_AMT", "evlu_pfls_smtl_amt", "EVLU_PFLS_SMTL_AMT",
            "scts_evlu_amt", "SCTS_EVLU_AMT")
        summary_audit = {key: summary.get(key) for key in audit_keys if key in summary}
        def _audit_num(row, *keys):
            for key in keys:
                if row.get(key) not in (None, ""):
                    try:
                        return float(str(row[key]).replace(",", ""))
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0
        holdings_qty = sum(int(_audit_num(row, "hldg_qty", "HLDG_QTY")) for row in all_rows)
        holdings_eval = sum(_audit_num(row, "evlu_amt", "EVLU_AMT") for row in all_rows)
        holdings_cost = sum(_audit_num(row, "pchs_amt", "PCHS_AMT", "pchs_amt_smtl_amt") for row in all_rows)
        log.info("KIS_BALANCE_AUDIT rows=%d holdings_qty=%d holdings_eval_sum=%.0f holdings_cost_sum=%.0f summary_fields=%s",
                 len(all_rows), holdings_qty, holdings_eval, holdings_cost, summary_audit)
        return all_rows, summary

    def daily_orders(self, start_date: str, end_date: str, order_no: str = "", ccld_dvsn: str = "00") -> list[dict]:
        """당일 주문체결 내역을 조회합니다.

        ccld_dvsn은 KIS 공식 API 기준 00=전체, 01=체결, 02=미체결입니다.
        매수/매도 구분 코드 02를 이용해 재시작/취소 후 남아 있는 실제 미체결 주문을
        OrderManager와 동기화합니다.
        """
        tr_id = "VTTC0081R" if self.s.paper else "TTTC0081R"
        all_rows: list[dict] = []
        fk100, nk100 = "", ""
        for _ in range(10):
            params = {
                "CANO": self.s.cano, "ACNT_PRDT_CD": self.s.acnt_prdt_cd,
                "INQR_STRT_DT": start_date, "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": "00", "PDNO": "", "CCLD_DVSN": str(ccld_dvsn), "INQR_DVSN": "00",
                "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "", "ODNO": order_no,
                "INQR_DVSN_1": "", "CTX_AREA_FK100": fk100, "CTX_AREA_NK100": nk100,
                "EXCG_ID_DVSN_CD": "KRX",
            }
            data = self._request("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", tr_id=tr_id, params=params)
            all_rows.extend(data.get("output1") or [])
            fk100 = str(data.get("ctx_area_fk100", "") or "")
            nk100 = str(data.get("ctx_area_nk100", "") or "")
            tr_cont = str(data.get("_tr_cont", "") or data.get("tr_cont", "") or data.get("header", {}).get("tr_cont", ""))
            if tr_cont not in {"M", "F"} or not fk100 and not nk100:
                break
            time.sleep(0.2)
        if str(ccld_dvsn) == "00":
            # 체결금액 차이를 검증할 수 있도록 주문이력의 실제 응답 필드만 제한적으로 기록합니다.
            # 원본 응답 전체에는 운영상 불필요한 정보가 있을 수 있어 식별/금액 관련 필드만 추립니다.
            for row in all_rows:
                filled = row.get("tot_ccld_qty", row.get("TOT_CCLD_QTY", 0))
                try:
                    filled_qty = int(float(str(filled or 0).replace(",", "")))
                except (TypeError, ValueError):
                    filled_qty = 0
                if filled_qty <= 0:
                    continue
                audit = {key: row.get(key) for key in (
                    "odno", "ODNO", "pdno", "PDNO", "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD",
                    "ord_qty", "ORD_QTY", "tot_ccld_qty", "TOT_CCLD_QTY", "rmn_qty", "RMN_QTY",
                    "avg_prvs", "AVG_PRVS", "avg_pric", "AVG_PRIC", "tot_ccld_amt", "TOT_CCLD_AMT",
                    "ccld_amt", "CCLD_AMT", "ord_unpr", "ORD_UNPR", "ord_tmd", "ORD_TMD") if key in row}
                log.info("KIS_ORDER_HISTORY_AUDIT requested_order_no=%s fields=%s", order_no or "ALL", audit)
        return all_rows
