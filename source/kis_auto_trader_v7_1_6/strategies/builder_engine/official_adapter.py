"""내부 전략 평가 어댑터.

전략 파일(.kis.yaml)과 KIS REST OHLCV를 프로젝트 내부 엔진에 전달하고, 프로필 투표와
JSONL 로그를 관리합니다. 외부 Strategy Builder HTTP/API 서버를 호출하지 않으며 주문도 제출하지 않습니다.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import yaml
from config.paths import runtime_path, resolve_path

log = logging.getLogger(__name__)
_LOG_LOCK = threading.Lock()
_SIGNAL_CACHE_LOCK = threading.Lock()
_SIGNAL_CACHE: dict[tuple, tuple[float, dict]] = {}


def _now_kst() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def _load_yaml(path: str | Path) -> dict:
    p = resolve_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"전략 파일이 없습니다: {p}")
    with p.open("r", encoding="utf-8") as fh:
        value = yaml.safe_load(fh) or {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML 최상위는 mapping이어야 합니다: {p}")
    if not isinstance(value.get("strategy"), dict):
        raise ValueError(f"공식 .kis.yaml의 strategy mapping이 없습니다: {p}")
    return value


def _substitute(value: Any, params: dict) -> Any:
    """YAML 내 $parameter 참조를 profile override 또는 strategy.params 기본값으로 치환합니다."""
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in params:
            raise ValueError(f"전략 파라미터를 해석할 수 없습니다: {value}")
        return params[name]
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    return value


def _resolved_params(strategy: dict, overrides: dict | None) -> dict:
    result = {}
    definitions = strategy.get("params", {}) or {}
    if isinstance(definitions, dict):
        for name, definition in definitions.items():
            if isinstance(definition, dict) and "default" in definition:
                result[name] = definition["default"]
            else:
                result[name] = definition
    result.update(overrides or {})
    return result


def _operand(value: Any, aliases: set[str]) -> dict:
    if isinstance(value, dict):
        if "value" in value:
            return {"type": "value", "value": value["value"]}
        if "price" in value or "priceField" in value:
            return {"type": "price", "priceField": value.get("price", value.get("priceField"))}
        alias = value.get("indicator") or value.get("alias")
        if alias:
            return {"type": "indicator", "indicatorAlias": alias,
                    "indicatorOutput": value.get("output", "value")}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"type": "value", "value": value}
    if isinstance(value, str):
        if value in aliases:
            return {"type": "indicator", "indicatorAlias": value, "indicatorOutput": "value"}
        if value.lower() in {"close", "open", "high", "low", "volume", "change"}:
            return {"type": "price", "priceField": value.lower()}
        try:
            return {"type": "value", "value": float(value)}
        except ValueError:
            raise ValueError(f"조건 operand를 해석할 수 없습니다: {value}")
    raise ValueError(f"조건 operand 유형을 지원하지 않습니다: {value!r}")


def _condition_to_builder(condition: dict, aliases: set[str], index: int, side: str) -> dict:
    """공식 YAML 조건을 BuilderState의 left/right operand 구조로 변환합니다."""
    if not isinstance(condition, dict):
        raise ValueError(f"조건은 mapping이어야 합니다: {condition!r}")
    # 공식 YAML candlestick condition
    if condition.get("candlestick"):
        return {"id": f"{side}_{index}", "isCandlestick": True,
                "candlestickAlias": condition["candlestick"],
                "candlestickSignal": condition.get("signal", "detected"),
                "left": {"type": "value", "value": 1}, "operator": "greater_than",
                "right": {"type": "value", "value": 0}}
    indicator = condition.get("indicator")
    if not indicator:
        raise ValueError(f"조건에 indicator가 없습니다: {condition}")
    output = condition.get("output", "value") or "value"
    left = _operand({"indicator": indicator, "output": output}, aliases)
    right = _operand(condition.get("compare_to"), aliases)
    operator = str(condition.get("operator", "")).strip()
    supported = {"cross_above", "cross_below", "greater_than", "less_than",
                 "greater_equal", "less_equal", "equals"}
    if operator not in supported:
        raise ValueError(f"지원하지 않는 Builder DSL 연산자: {operator!r}")
    if condition.get("compare_output") and right.get("type") == "indicator":
        right["indicatorOutput"] = condition["compare_output"]
    return {"id": f"{side}_{index}", "left": left, "operator": operator, "right": right}


def yaml_to_builder_state(raw: dict, param_overrides: dict | None = None) -> dict:
    """기존 Export YAML 호환성 검사에서 사용할 BuilderState 구조로 변환합니다.

    지원되지 않는 조건/필드는 임의로 생략하지 않고 오류 처리합니다. 이 함수가 성공해도
    이 변환기는 호환성 도구이며 V7.0.0 런타임에서 외부 Builder API를 호출하지 않습니다.
    """
    strategy = raw["strategy"]
    params = _resolved_params(strategy, param_overrides)
    strategy = _substitute(strategy, params)
    metadata = raw.get("metadata", {}) or {}
    indicators = []
    aliases: set[str] = set()
    for i, item in enumerate(strategy.get("indicators", []) or []):
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError(f"잘못된 indicator 정의: {item!r}")
        alias = str(item.get("alias") or item["id"])
        if alias in aliases:
            raise ValueError(f"중복 indicator alias: {alias}")
        aliases.add(alias)
        indicator = {"id": f"{alias}_{i}", "indicatorId": str(item["id"]),
                     "alias": alias, "params": item.get("params", {}) or {}}
        if item.get("output"):
            indicator["output"] = item["output"]
        if item.get("name"):
            indicator["displayName"] = item["name"]
        indicators.append(indicator)
    # Official Export stores candlestick patterns separately from regular indicators.
    for i, item in enumerate(strategy.get("candlesticks", []) or []):
        if not isinstance(item, dict) or not item.get("id"):
            raise ValueError(f"잘못된 candlestick 정의: {item!r}")
        alias = str(item.get("alias") or item["id"])
        if alias in aliases:
            raise ValueError(f"중복 candlestick alias: {alias}")
        aliases.add(alias)
        indicators.append({"id": f"{alias}_candle_{i}", "indicatorId": str(item["id"]),
                           "alias": alias, "params": {}, "isCandlestick": True})
    # Price fields are native Builder operands, not named indicators.
    def group(name: str) -> dict:
        value = strategy.get(name, {}) or {}
        conditions = value.get("conditions", []) or []
        return {"logic": str(value.get("logic", "AND")).upper(),
                "conditions": [_condition_to_builder(c, aliases, i, name)
                               for i, c in enumerate(conditions)]}
    risk = raw.get("risk", {}) or {}
    def risk_item(key: str, camel: str) -> dict:
        item = risk.get(key, {}) or {}
        return {"enabled": bool(item.get("enabled", False)),
                "percent": float(item.get("percent", 0) or 0)}
    state = {
        "metadata": {"id": str(strategy.get("id") or metadata.get("name", "custom_strategy")),
                     "name": str(metadata.get("name") or strategy.get("id") or "custom_strategy"),
                     "description": str(metadata.get("description", "")),
                     "category": str(strategy.get("category", "custom")),
                     "tags": metadata.get("tags", []) or [], "author": str(metadata.get("author", "user"))},
        "indicators": indicators,
        "entry": group("entry"),
        "exit": group("exit"),
        "risk": {"stopLoss": risk_item("stop_loss", "stopLoss"),
                 "takeProfit": risk_item("take_profit", "takeProfit"),
                 "trailingStop": risk_item("trailing_stop", "trailingStop")},
    }
    if not state["entry"]["conditions"]:
        raise ValueError("진입 조건이 비어 있습니다. 매수 시그널은 평가하지 않습니다.")
    return state


class OfficialBuilderAdapter:
    """내부 전략 엔진에서 종목별 시그널을 계산합니다."""
    def __init__(self, profile_path: str = "config/strategy_profiles.yaml"):
        self.profile_path = resolve_path(profile_path)
        self._profiles_cache: tuple[float, dict] | None = None

    def load_profiles(self) -> dict:
        if not self.profile_path.is_file():
            raise FileNotFoundError(f"전략 프로필 파일이 없습니다: {self.profile_path}")
        mtime = self.profile_path.stat().st_mtime
        if self._profiles_cache and self._profiles_cache[0] == mtime:
            return self._profiles_cache[1]
        with self.profile_path.open("r", encoding="utf-8") as fh:
            value = yaml.safe_load(fh) or {}
        if not isinstance(value, dict) or not isinstance(value.get("decision"), dict):
            raise ValueError("strategy_profiles.yaml에 decision mapping이 필요합니다")
        self._profiles_cache = (mtime, value)
        return value

    def _log_record(self, profile: str, strategy_id: str, symbol: str, result: dict,
                    decision_enabled: bool, weight: float, path: str) -> None:
        config = self.load_profiles().get("logging", {}) or {}
        log_path = runtime_path("strategy_signal_log") if not config.get("jsonl_path") else resolve_path(config["jsonl_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": _now_kst(), "profile": profile, "strategy_id": strategy_id,
                  "strategy_file": path, "symbol": symbol, "action": result.get("action", "ERROR"),
                  "strength": float(result.get("strength", 0) or 0), "reason": str(result.get("reason", "")),
                  "status": result.get("status", "unknown"), "decision_enabled": decision_enabled,
                  "weight": weight, "error": result.get("error", "")}
        # Profile-level diagnostics carry the actual thresholds and failed gates as structured JSON fields.
        for field in ("failed_gates", "buy_votes", "buy_score", "sell_votes", "sell_score",
                      "min_buy_votes", "min_buy_score", "min_sell_votes", "min_sell_score",
                      "primary_buy", "primary_sell"):
            if field in result:
                record[field] = result[field]
        with _LOG_LOCK:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def evaluate(self, profile_name: str = "unified", symbols: list[str] | None = None, *,
                 observe_all: bool = True, api=None,
                 bars_by_symbol: dict[str, list[dict]] | None = None,
                 data_status_by_symbol: dict[str, str] | None = None) -> dict[str, dict]:
        """통합 설정의 BUY/SELL 전략 목록을 독립적으로 집계합니다.

        decision.buy.strategies와 decision.sell.strategies는 서로 독립입니다.
        observe_all은 진단 목적으로 목록 외 전략도 기록할지 여부만 제어하며,
        목록 외 전략은 어떤 경우에도 BUY/SELL 투표에 참여하지 않습니다.
        """
        from strategies.builder_engine.internal_engine import evaluate_strategy
        if api is None:
            raise ValueError("내부 전략 평가에는 KIS API 객체(api)가 필요합니다.")
        config = self.load_profiles()
        decision = config.get("decision")
        if not isinstance(decision, dict):
            raise ValueError("strategy_profiles.yaml에 decision mapping이 필요합니다")
        buy_cfg = decision.get("buy", {}) or {}
        sell_cfg = decision.get("sell", {}) or {}
        if not isinstance(buy_cfg, dict) or not isinstance(sell_cfg, dict):
            raise ValueError("decision.buy 및 decision.sell은 mapping이어야 합니다")
        specs_list = config.get("strategies", []) or []
        all_specs = {str(x["id"]): x for x in specs_list if isinstance(x, dict) and x.get("id")}
        if len(all_specs) != len(specs_list):
            raise ValueError("strategies 목록에 잘못된 항목 또는 중복 ID가 있습니다")
        def direction_map(cfg: dict, name: str) -> dict[str, float]:
            raw = cfg.get("strategies", []) or []
            if not isinstance(raw, list):
                raise ValueError(f"decision.{name}.strategies는 목록이어야 합니다")
            result: dict[str, float] = {}
            for item in raw:
                if not isinstance(item, dict) or not item.get("id"):
                    raise ValueError(f"decision.{name}.strategies 항목은 id가 필요합니다")
                sid = str(item["id"])
                if sid not in all_specs:
                    raise ValueError(f"decision.{name}에서 등록되지 않은 전략 ID 참조: {sid}")
                if sid in result:
                    raise ValueError(f"decision.{name}에 중복 전략 ID: {sid}")
                weight = float(item.get("weight", 1.0))
                if weight <= 0:
                    raise ValueError(f"decision.{name} 전략 가중치는 0보다 커야 합니다: {sid}")
                result[sid] = weight
            return result
        buy_map = direction_map(buy_cfg, "buy")
        sell_map = direction_map(sell_cfg, "sell")
        if not buy_map or not sell_map:
            raise ValueError("매수/매도 전략 목록은 각각 하나 이상 필요합니다")
        eval_ids = list(dict.fromkeys(list(buy_map) + list(sell_map)))
        if observe_all:
            eval_ids = list(all_specs)
        log.info("[STRATEGY_PROFILE_CONFIG] profile=unified buy_strategies=%s sell_strategies=%s min_buy_score=%s min_buy_votes=%s min_sell_score=%s min_sell_votes=%s",
                 sorted(buy_map), sorted(sell_map), buy_cfg.get("min_score", 50), buy_cfg.get("min_votes", 2),
                 sell_cfg.get("min_score", 20), sell_cfg.get("min_votes", 1))
        for sid, spec in all_specs.items():
            try:
                strategy_raw = _load_yaml(spec.get("file", ""))
                yaml_enabled = str((strategy_raw.get("strategy") or {}).get("enabled", True)).strip().lower() not in {"false", "0", "off", "no", ""}
            except Exception:
                yaml_enabled = False
            log.info("[STRATEGY_PROFILE_CONFIG_ITEM] profile=unified strategy=%s file=%s yaml_enabled=%s buy_participant=%s sell_participant=%s buy_weight=%s sell_weight=%s",
                     sid, spec.get("file", ""), yaml_enabled, sid in buy_map, sid in sell_map, buy_map.get(sid, 0), sell_map.get(sid, 0))
        symbol_list = list(dict.fromkeys(str(x).zfill(6) for x in (symbols or [])))
        results = {sym: {"symbol": sym, "strategy_results": [], "buy_votes": 0, "buy_weight": 0.0,
                         "sell_votes": 0, "sell_weight": 0.0, "buy_decision_weight": 0.0,
                         "sell_decision_weight": 0.0, "primary_buy": False, "primary_sell": False,
                         "errors": [], "buy_errors": [], "sell_errors": [],
                         "buy_data_incomplete": [], "sell_data_incomplete": []} for sym in symbol_list}
        shared_bars = dict(bars_by_symbol or {})
        data_status = dict(data_status_by_symbol or {})
        for symbol in symbol_list:
            if symbol in shared_bars:
                continue
            try:
                shared_bars[symbol] = api.daily_prices(symbol, days=260)
                if not shared_bars[symbol]:
                    raise RuntimeError("KIS 일봉 API가 빈 데이터를 반환했습니다.")
            except Exception as exc:
                shared_bars[symbol] = None
                log.warning("[KIS10_STRATEGY] 일봉 수집 실패 symbol=%s: %s", symbol, exc)
        for sid in eval_ids:
            spec = all_specs[sid]
            buy_weight = buy_map.get(sid, 0.0)
            sell_weight = sell_map.get(sid, 0.0)
            for symbol in symbol_list:
                path = str(spec.get("file", ""))
                try:
                    raw = _load_yaml(path)
                    if str((raw.get("strategy") or {}).get("enabled", True)).strip().lower() in {"false", "0", "off", "no", ""}:
                        result = {"action": "HOLD", "strength": 0.0, "reason": "전략 YAML enabled 비활성화", "data_ready": True, "status": "disabled"}
                    elif shared_bars.get(symbol) is None:
                        missing_status = str(data_status.get(symbol, "API_ERROR")).upper()
                        if missing_status == "INSUFFICIENT_HISTORY":
                            result = {
                                "action": "HOLD", "strength": 0.0,
                                "reason": "일봉 과거 데이터 부족; 필요한 lookback 미충족",
                                "status": "insufficient_data", "data_ready": False,
                            }
                        else:
                            result = {
                                "action": "ERROR", "strength": 0.0,
                                "reason": "KIS 일봉 API 조회 실패; fail-closed",
                                "status": "api_error", "error": "daily_bar_api_error",
                            }
                    else:
                        overrides = spec.get("params", {}) or {}
                        if overrides:
                            raw = dict(raw); strategy_raw = dict(raw.get("strategy", {}))
                            params = dict(strategy_raw.get("params", {}) or {}); params.update(overrides)
                            strategy_raw["params"] = params; raw["strategy"] = strategy_raw
                        result = evaluate_strategy(raw, shared_bars[symbol], symbol=symbol, purpose="both")
                    if result.get("action") not in {"BUY", "SELL", "HOLD"}:
                        result["action"] = "ERROR"
                    result.setdefault("status", "success" if result.get("action") != "ERROR" else "error")
                except Exception as exc:
                    result = {"action": "ERROR", "strength": 0.0, "reason": f"{type(exc).__name__}: {exc}", "status": "error", "error": str(exc)}
                buy_enabled = sid in buy_map
                sell_enabled = sid in sell_map
                self._log_record("unified", sid, symbol, result, buy_enabled or sell_enabled,
                                 max(buy_weight, sell_weight), path)
                results[symbol]["strategy_results"].append({"strategy_id": sid, **result,
                    "decision_enabled": buy_enabled or sell_enabled, "buy_enabled": buy_enabled,
                    "sell_enabled": sell_enabled, "buy_weight": buy_weight, "sell_weight": sell_weight})
                if result.get("action") == "ERROR":
                    if buy_enabled:
                        results[symbol]["buy_errors"].append(sid)
                    if sell_enabled:
                        results[symbol]["sell_errors"].append(sid)
                    if buy_enabled or sell_enabled:
                        results[symbol]["errors"].append(sid)
                elif result.get("status") == "insufficient_data":
                    if buy_enabled: results[symbol]["buy_data_incomplete"].append(sid)
                    if sell_enabled: results[symbol]["sell_data_incomplete"].append(sid)
                # YAML 비활성/데이터 부족 전략은 점수 분모에서 제외하되, 부족 데이터는 BUY 승인 차단으로 별도 관리합니다.
                if result.get("status") not in {"disabled", "insufficient_data"}:
                    if buy_enabled:
                        results[symbol]["buy_decision_weight"] += buy_weight
                        if result.get("action") == "BUY":
                            results[symbol]["buy_votes"] += 1
                            # internal_engine.evaluate_strategy() returns strength on the 0.0~1.0 scale.
                            # Keep that scale during weighted aggregation; the final score converts to 0~100.
                            results[symbol]["buy_weight"] += buy_weight * max(0.0, min(1.0, float(result.get("strength", 0) or 0)))
                    if sell_enabled:
                        results[symbol]["sell_decision_weight"] += sell_weight
                        if result.get("action") == "SELL":
                            results[symbol]["sell_votes"] += 1
                            # SELL strength uses the same 0.0~1.0 scale as BUY strength.
                            results[symbol]["sell_weight"] += sell_weight * max(0.0, min(1.0, float(result.get("strength", 0) or 0)))
        min_buy_score = max(0.0, min(100.0, float(buy_cfg.get("min_score", 50))))
        min_buy_votes = max(1, int(buy_cfg.get("min_votes", 2)))
        min_sell_score = max(0.0, min(100.0, float(sell_cfg.get("min_score", 20))))
        min_sell_votes = max(1, int(sell_cfg.get("min_votes", 1)))
        for symbol, result in results.items():
            buy_denom = result["buy_decision_weight"]
            sell_denom = result["sell_decision_weight"]
            buy_score = result["buy_weight"] / buy_denom * 100 if buy_denom else 0.0
            sell_score = result["sell_weight"] / sell_denom * 100 if sell_denom else 0.0
            result["score"] = round(buy_score, 2); result["sell_score"] = round(sell_score, 2)
            buy_ok = result["buy_votes"] >= min_buy_votes and buy_score >= min_buy_score
            sell_ok = result["sell_votes"] >= min_sell_votes and sell_score >= min_sell_score
            participating = [x for x in result["strategy_results"] if x.get("decision_enabled")]
            all_failed = bool(result["errors"]) and not any(x.get("action") in {"BUY", "SELL", "HOLD"} for x in participating)
            # A valid SELL meeting its gates remains actionable even if another SELL strategy errored.
            # BUY requires all configured BUY-side strategies to evaluate successfully; errors fail closed.
            if sell_ok:
                result["action"] = "SELL"
            elif result["buy_errors"]:
                result["action"] = "ERROR"
            elif result["buy_data_incomplete"]:
                result["action"] = "HOLD"
                result["status"] = "insufficient_data"
            elif buy_ok:
                result["action"] = "BUY"
            elif result["sell_errors"]:
                result["action"] = "ERROR"
            elif result["sell_data_incomplete"]:
                result["action"] = "HOLD"
                result["status"] = "insufficient_data"
            else:
                result["action"] = "HOLD"
            failed = []
            if result["buy_votes"] < min_buy_votes: failed.append(f"매수투표수 미충족 {result['buy_votes']}/{min_buy_votes}")
            if buy_score < min_buy_score: failed.append(f"매수점수 미충족 {buy_score:.2f}/{min_buy_score:.2f}")
            if result["sell_votes"] < min_sell_votes: failed.append(f"매도투표수 미충족 {result['sell_votes']}/{min_sell_votes}")
            if sell_score < min_sell_score: failed.append(f"매도점수 미충족 {sell_score:.2f}/{min_sell_score:.2f}")
            result["failed_gates"] = failed
            # SELL 평가의 데이터 준비 상태를 별도로 제공합니다. 매수측 데이터 부족만으로
            # 유효한 매도 신호를 막지는 않지만, SELL측 데이터가 부족한 HOLD는 준비되지 않은 상태입니다.
            result["data_ready"] = bool(
                result["action"] != "ERROR"
                and (result["action"] == "SELL" or not result["sell_data_incomplete"])
            )
            if result["sell_data_incomplete"] and result["action"] != "SELL":
                result["status"] = "insufficient_data"
            log.info("[STRATEGY_DECISION_AUDIT] profile=unified symbol=%s action=%s buy_score=%.2f min_buy_score=%.2f buy_votes=%d min_buy_votes=%d sell_score=%.2f min_sell_score=%.2f sell_votes=%d min_sell_votes=%d errors=%s failed_gates=%s",
                     symbol, result["action"], buy_score, min_buy_score, result["buy_votes"], min_buy_votes,
                     sell_score, min_sell_score, result["sell_votes"], min_sell_votes, result["errors"], failed)
            result["reason"] = (f"KIS10 unified action={result['action']}; buy_votes={result['buy_votes']}/{min_buy_votes}; "
                                f"buy_score={buy_score:.2f}/{min_buy_score:.2f}; sell_votes={result['sell_votes']}/{min_sell_votes}; "
                                f"sell_score={sell_score:.2f}/{min_sell_score:.2f}; errors={result['errors']}")
            self._log_record("unified", "__decision__", symbol,
                {"action": result["action"], "strength": max(buy_score, sell_score), "reason": result["reason"],
                 "status": "error" if result["action"] == "ERROR" else "success", "failed_gates": failed,
                 "buy_votes": result["buy_votes"], "buy_score": buy_score, "sell_votes": result["sell_votes"],
                 "sell_score": sell_score, "min_buy_votes": min_buy_votes, "min_buy_score": min_buy_score,
                 "min_sell_votes": min_sell_votes, "min_sell_score": min_sell_score}, True, 1.0, str(self.profile_path))
        return results

    def get_profile_risk(self, profile_name: str = "unified") -> dict:
        """V7.1.1에서는 전략 프로필별 위험 복제를 제거합니다.

        1차/2차 손익절은 공통 청산 판정기가 관리하므로 빈 매수별 위험 설정을 반환합니다.
        """
        return {}


def re_safe_id(value: str) -> str:
    """Builder local_* 식별자에 사용할 안전한 ASCII 토큰을 만듭니다."""
    import re
    result = re.sub(r"[^A-Za-z0-9_]", "_", value)
    return result if result and result[0].isalpha() else "strategy_" + result
