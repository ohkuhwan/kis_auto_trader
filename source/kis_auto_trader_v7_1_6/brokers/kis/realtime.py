"""brokers/kis/realtime.py - KIS WebSocket 실시간 체결가/체결통보.

운영 안정성을 위해 유지하는 핵심 원칙
- asyncio.run()으로 만든 loop를 외부 thread에서 재사용하지 않습니다.
- WebSocket 전용 thread가 직접 new_event_loop()를 만들고 run_forever 계열로 유지합니다.
- 연결이 끊기면 같은 loop 안에서 reconnect합니다.
- 연결이 끊긴 동안 들어온 subscribe 요청은 desired set에 보관했다가 재연결 후 재구독합니다.
- main thread가 subscribe_symbols()를 호출해도 loop가 아직 준비되지 않았으면 오류 없이 대기/보관합니다.
"""

import asyncio
import base64
import json
import logging
import os
import threading
from typing import Callable

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import websockets

logger = logging.getLogger(__name__)

PRICE_FIELDS = [
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN", "PRDY_VRSS", "PRDY_CTRT",
    "WGHN_AVRG_STCK_PRC", "STCK_OPRC", "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL",
    "ACML_VOL", "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU", "CTTR",
    "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE", "PRDY_VOL_VRSS_ACML_VOL_RATE",
    "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN", "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN",
    "HGPR_VRSS_PRPR", "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1", "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN",
    "VOL_TNRT", "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE", "HOUR_CLS_CODE",
    "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
]

NOTICE_FIELDS = [
    "CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS", "ODER_KIND",
    "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR", "STCK_CNTG_HOUR", "RFUS_YN",
    "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY", "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB",
    "POPUP_YN", "FILLER", "CRDT_CLS", "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC",
]


def decrypt_payload(encoded: str, key: str, iv: str) -> str:
    """KIS AES-256-CBC Base64 payload를 복호화합니다."""
    if not key or not iv:
        raise ValueError("AES key/iv가 없습니다.")
    raw = base64.b64decode(encoded)
    cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
    return unpad(cipher.decrypt(raw), AES.block_size).decode("utf-8")


def parse_pipe_payload(text: str) -> tuple[str, str, str, str]:
    """KIS 실시간 payload를 flag/TR/count/data로 분리합니다."""
    parts = text.split("|", 3)
    if len(parts) != 4:
        raise ValueError("실시간 payload 형식 오류")
    return parts[0], parts[1], parts[2], parts[3]


class KisRealtime:
    """KIS WebSocket의 연결 생명주기와 구독 상태를 관리합니다."""

    def __init__(self, api, on_price: Callable[[dict], None], on_notice: Callable[[dict], None]):
        """콜백과 WebSocket 상태를 초기화합니다."""
        self.api = api
        self.on_price = on_price
        self.on_notice = on_notice
        self.key = ""
        self.iv = ""
        self.ws = None
        self.loop = None
        self.thread = None
        self.loop_ready = threading.Event()
        self.stop_event = threading.Event()
        self.state_lock = threading.RLock()
        self._subscription_sync_lock: asyncio.Lock | None = None
        self.connection_generation = 0
        self.desired_symbols: set[str] = set()
        self.active_symbols: set[str] = set()
        # 후보 구독이 스캔마다 누적되면 KIS WebSocket의 MAX SUBSCRIBE OVER를 유발할 수 있습니다.
        # 보유 종목은 우선 보존하고 후보 구독은 설정된 상한 내에서 교체합니다.
        self.held_symbols: set[str] = set()
        self.candidate_symbols: set[str] = set()
        self.max_price_subscriptions = max(1, int(os.getenv("KIS_WS_MAX_PRICE_SUBSCRIPTIONS", "30")))

    async def _send_subscribe(self, tr_id: str, tr_key: str, tr_type: str = "1") -> None:
        """현재 연결에 하나의 구독/해제 요청을 전송합니다."""
        if self.ws is None:
            raise RuntimeError("WebSocket 연결이 없습니다.")
        approval_key = self.api.issue_ws_approval_key()
        payload = {
            "header": {"approval_key": approval_key, "custtype": "P", "tr_type": tr_type, "content-type": "utf-8"},
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    def _refresh_desired_symbols_locked(self) -> set[str]:
        """보유 종목을 우선하고 후보 종목은 상한 내에서만 desired set에 유지합니다."""
        held = set(self.held_symbols)
        room = max(0, self.max_price_subscriptions - len(held))
        candidates = sorted(self.candidate_symbols - held)[:room]
        self.desired_symbols = held | set(candidates)
        return set(self.desired_symbols)

    def set_candidate_symbols(self, symbols: list[str]) -> None:
        """이번 조건검색 회차의 후보 구독 목록을 교체해 오래된 후보가 누적되지 않게 합니다."""
        normalized = {str(s).strip().zfill(6) for s in symbols if str(s).strip() and str(s).strip() != "000000"}
        with self.state_lock:
            self.candidate_symbols = normalized
            self._refresh_desired_symbols_locked()
        self._schedule_subscription_sync()

    def set_held_symbols(self, symbols: list[str]) -> None:
        """현재 잔고의 보유 종목 목록으로 우선 구독 대상을 교체합니다."""
        normalized = {str(s).strip().zfill(6) for s in symbols if str(s).strip() and str(s).strip() != "000000"}
        with self.state_lock:
            self.held_symbols = normalized
            self._refresh_desired_symbols_locked()
        self._schedule_subscription_sync()

    def _schedule_subscription_sync(self) -> None:
        """실행 중이면 구독 차이를 비동기로 반영하고, 연결 전이면 desired set만 보관합니다."""
        loop = self.loop
        if loop is None or loop.is_closed() or not self.loop_ready.is_set():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.sync_price_subscriptions(), loop)
            def report(done_future):
                try:
                    done_future.result()
                except Exception as exc:
                    logger.warning("실시간 구독 동기화 실패: %s; 다음 연결/동기화 때 재시도합니다.", exc)
            future.add_done_callback(report)
        except RuntimeError as exc:
            logger.warning("실시간 구독 동기화를 예약하지 못했습니다: %s", exc)

    async def sync_price_subscriptions(self) -> None:
        """동시 sync 작업을 직렬화해 동일 종목 중복 구독 요청을 방지합니다."""
        if self.ws is None:
            return
        if self._subscription_sync_lock is None:
            self._subscription_sync_lock = asyncio.Lock()
        async with self._subscription_sync_lock:
            if self.ws is None:
                return
            with self.state_lock:
                desired = set(self.desired_symbols)
                active = set(self.active_symbols)
            for symbol in sorted(active - desired):
                await self._send_subscribe("H0STCNT0", symbol, tr_type="2")
                with self.state_lock:
                    self.active_symbols.discard(symbol)
                await asyncio.sleep(0.1)
            with self.state_lock:
                active = set(self.active_symbols)
            for symbol in sorted(desired - active):
                await self._send_subscribe("H0STCNT0", symbol, tr_type="1")
                with self.state_lock:
                    self.active_symbols.add(symbol)
                await asyncio.sleep(0.1)

    async def _subscribe_all(self) -> None:
        """연결 직후 체결통보/가격 구독을 한 번씩 복구하고 generation을 증가시킵니다."""
        if self._subscription_sync_lock is None:
            self._subscription_sync_lock = asyncio.Lock()
        async with self._subscription_sync_lock:
            notice_tr_id = "H0STCNI9" if self.api.s.paper else "H0STCNI0"
            await self._send_subscribe(notice_tr_id, self.api.s.hts_id)
            await asyncio.sleep(0.2)
            with self.state_lock:
                symbols = sorted(self._refresh_desired_symbols_locked())
                self.active_symbols.clear()
            for symbol in symbols:
                await self._send_subscribe("H0STCNT0", symbol)
                with self.state_lock:
                    self.active_symbols.add(symbol)
                await asyncio.sleep(0.1)
            self.connection_generation += 1
            logger.info("WebSocket 구독 복구 완료: notice + %d symbols generation=%d", len(symbols), self.connection_generation)

    async def subscribe_price(self, symbols: list[str]) -> None:
        """호환용 가격 구독 함수입니다. 원하는 목록 전체가 아니라 현재 상한 적용 목록을 동기화합니다."""
        normalized = {str(s).strip().zfill(6) for s in symbols if str(s).strip() and str(s).strip() != "000000"}
        with self.state_lock:
            self.candidate_symbols = normalized
            self._refresh_desired_symbols_locked()
        await self.sync_price_subscriptions()

    def subscribe_symbols(self, symbols: list[str]) -> None:
        """보유/체결 종목을 우선 구독 목록에 추가합니다. 후보 종목은 set_candidate_symbols()를 사용합니다."""
        normalized = {str(s).strip().zfill(6) for s in symbols if str(s).strip() and str(s).strip() != "000000"}
        if not normalized:
            return
        with self.state_lock:
            self.held_symbols.update(normalized)
            self._refresh_desired_symbols_locked()
        self._schedule_subscription_sync()

    def start_background(self) -> None:
        """WebSocket 전용 thread를 시작하고 loop 준비를 기다립니다."""
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.loop_ready.clear()
        self.thread = threading.Thread(target=self._thread_main, name="kis-websocket", daemon=True)
        self.thread.start()
        self.loop_ready.wait(timeout=5)

    def _thread_main(self) -> None:
        """WebSocket thread 안에서 asyncio loop를 생성하고 끝까지 소유합니다."""
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self.loop_ready.set()
        try:
            loop.run_until_complete(self.run())
        except Exception:
            logger.exception("WebSocket thread 비정상 종료")
        finally:
            self.ws = None
            try:
                loop.run_until_complete(asyncio.sleep(0))
            except Exception:
                pass
            loop.close()
            self.loop = None
            self.loop_ready.clear()

    async def _receive_loop(self) -> None:
        """현재 연결에서 JSON 응답, 체결가, 체결통보를 계속 수신합니다."""
        while not self.stop_event.is_set():
            raw = await asyncio.wait_for(self.ws.recv(), timeout=35.0)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if not raw:
                continue

            if raw.startswith("{"):
                try:
                    data = json.loads(raw)
                    header = data.get("header", {}) or {}
                    body = data.get("body", {}) or {}
                    tr_id = str(header.get("tr_id", ""))
                    if tr_id == "PINGPONG":
                        await self.ws.send(raw)
                        continue
                    if str(body.get("rt_cd", "")) == "1":
                        message = str(body.get("msg1", "") or "")
                        tr_key = str(header.get("tr_key", "") or "")
                        if "ALREADY IN SUBSCRIBE" in message.upper() and tr_id == "H0STCNT0":
                            # 서버가 이미 구독 중임을 알려준 경우 오류로 취급하지 않고
                            # active set을 동기화해 동일 구독 재요청을 방지합니다.
                            with self.state_lock:
                                self.active_symbols.add(tr_key.zfill(6))
                            logger.info("WebSocket 구독 상태 확인(이미 구독 중): symbol=%s", tr_key)
                        else:
                            logger.error("WebSocket API 오류 tr_id=%s tr_key=%s msg=%s", tr_id, tr_key, message)
                    output = body.get("output", {}) or {}
                    if tr_id in ("H0STCNI0", "H0STCNI9") and output.get("key") and output.get("iv"):
                        self.key, self.iv = str(output["key"]), str(output["iv"])
                        logger.info("체결통보 AES 세션키 수신 완료 (%s)", tr_id)
                except json.JSONDecodeError:
                    logger.warning("WebSocket JSON 응답 파싱 실패: %s", raw[:300])
                continue

            try:
                flag, tr_id, _count, payload = parse_pipe_payload(raw)
                if flag == "0" and tr_id == "H0STCNT0":
                    values = payload.split("^")
                    self.on_price(dict(zip(PRICE_FIELDS, values)))
                elif flag == "1" and tr_id in ("H0STCNI0", "H0STCNI9"):
                    if not self.key or not self.iv:
                        logger.warning("체결통보 AES 키가 아직 없어 메시지를 건너뜁니다.")
                        continue
                    values = decrypt_payload(payload, self.key, self.iv).split("^")
                    self.on_notice(dict(zip(NOTICE_FIELDS, values)))
            except Exception:
                logger.exception("실시간 메시지 처리 실패")

    async def run(self) -> None:
        """WebSocket 연결을 유지하고 끊기면 지수 backoff로 재접속합니다."""
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self.api.issue_ws_approval_key()
                async with websockets.connect(
                    self.api.s.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1000,
                ) as ws:
                    self.ws = ws
                    self.key = ""
                    self.iv = ""
                    with self.state_lock:
                        self.active_symbols.clear()
                    logger.info("KIS WebSocket 연결 성공: %s", self.api.s.ws_url)
                    await self._subscribe_all()
                    backoff = 1.0
                    try:
                        await self._receive_loop()
                    except asyncio.TimeoutError:
                        logger.warning("WebSocket 수신 timeout - 연결을 재확인합니다.")
                        try:
                            await ws.ping()
                            continue
                        except Exception:
                            raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                logger.warning("WebSocket 연결/수신 오류: %s; %.1f초 후 재접속", exc, backoff)
                self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)
            finally:
                self.ws = None
                with self.state_lock:
                    self.active_symbols.clear()

    def stop(self, timeout: float = 5.0) -> None:
        """WebSocket thread와 loop를 정상 종료합니다."""
        self.stop_event.set()
        loop = self.loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(lambda: None)
        thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
