"""YAML 기반 전략 평가 어댑터.

공식 KIS strategy_builder의 YAML 전략 개념을 자동매매 프로그램에 연결하기 위한
독립 어댑터입니다. 공식 저장소 전체를 복제한 것은 아니며, 아래 README에 명시한
조건/지표 스키마를 평가합니다. 공식 builder 버전별 DSL 전체를 지원한다고 가정하지 않습니다.
"""
from __future__ import annotations
import logging
from trading.symbols import symbol_label
from pathlib import Path
from typing import Any
import yaml

log = logging.getLogger(__name__)


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _field(row: dict, *names: str, default=None):
    for name in names:
        if name in row and row[name] not in (None, ''):
            return row[name]
    return default


class YamlStrategyEngine:
    """간단한 `.kis.yaml` 전략 로더 및 지표/조건 평가기."""
    def __init__(self, min_buy_score: float = 70.0):
        self.min_buy_score = max(0.0, min(100.0, float(min_buy_score)))
        self._cache: dict[str, tuple[float, dict]] = {}

    def load(self, path: str) -> dict:
        """YAML 파일을 UTF-8로 로드하고 최소 구조를 검증합니다."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"전략 파일이 없습니다: {p}")
        cache_key = str(p.resolve())
        mtime = p.stat().st_mtime
        cached = self._cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
        with p.open('r', encoding='utf-8') as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"YAML 최상위 구조는 mapping이어야 합니다: {p}")
        strategy = raw.get('strategy', raw)
        if not isinstance(strategy, dict):
            raise ValueError(f"strategy는 mapping이어야 합니다: {p}")
        self._cache[cache_key] = (mtime, strategy)
        return strategy

    def evaluate(self, path: str, bars: list[dict], *, symbol: str = '', purpose: str = 'both') -> dict:
        """OHLCV 시계열로 BUY/SELL/HOLD 및 별도 0~100 강도를 계산합니다.

        BUY 강도는 진입 조건 충족률과 조건별 가중치에 기반한 휴리스틱입니다.
        통계적으로 보정된 수익확률이 아닙니다. 데이터가 부족하면 HOLD가 아니라
        `ERROR`와 `data_ready=False`를 반환하여 API 오류와 조건 미충족을 구분합니다.
        """
        try:
            strategy = self.load(path)
            rows = self._normalise_bars(bars)
            if len(rows) < 25:
                return {'symbol': symbol, 'action': 'HOLD', 'strength': 0.0,
                        'reason': f'평가 데이터 부족: {len(rows)} bars (최소 25)', 'data_ready': False,
                        'status': 'insufficient_data', 'buy_allowed': False}
            values = self._indicators(rows, strategy.get('indicators', []))
            entry = strategy.get('entry', strategy.get('buy', {})) or {}
            exit_rule = strategy.get('exit', strategy.get('sell', {})) or {}
            entry_ok, entry_score, entry_reasons = self._evaluate_group(entry, values, rows)
            exit_ok, exit_score, exit_reasons = self._evaluate_group(exit_rule, values, rows)
            risk = strategy.get('risk_management', {}) or {}
            # 포지션이 없는 진입 후보에 매도 조건을 적용하지 않도록 purpose를 분리합니다.
            # 강제 손절/익절은 YAML reference_entry_price가 명시된 경우에만 이 레이어에서 평가됩니다.
            risk_reason = self._risk_reason(rows, risk) if purpose in {'exit', 'both'} else ''
            if purpose not in {'entry', 'exit', 'both'}:
                raise ValueError("purpose는 entry, exit, both 중 하나여야 합니다.")
            if purpose == 'entry':
                if entry_ok:
                    action, strength, reason = 'BUY', entry_score, '매수 조건 충족: ' + '; '.join(entry_reasons)
                else:
                    action, strength, reason = 'HOLD', 0.0, '매수 조건 미충족'
            elif purpose == 'exit':
                if risk_reason:
                    action, strength, reason = 'SELL', 100.0, risk_reason
                elif exit_ok:
                    _exit_label = '매도 조건 미충족 항목 포함' if any('(미충족)' in item for item in exit_reasons) else '매도 조건 충족'
                    action, strength, reason = 'SELL', max(50.0, exit_score), _exit_label + ': ' + '; '.join(exit_reasons)
                else:
                    action, strength, reason = 'HOLD', 0.0, '매도 조건 미충족'
            elif risk_reason:
                action, strength, reason = 'SELL', 100.0, risk_reason
            elif exit_ok:
                _exit_label = '매도 조건 미충족 항목 포함' if any('(미충족)' in item for item in exit_reasons) else '매도 조건 충족'
                action, strength, reason = 'SELL', max(50.0, exit_score), _exit_label + ': ' + '; '.join(exit_reasons)
            elif entry_ok:
                action, strength, reason = 'BUY', entry_score, '매수 조건 충족: ' + '; '.join(entry_reasons)
            else:
                action, strength, reason = 'HOLD', 0.0, '조건 미충족'
            return {'symbol': symbol, 'action': action, 'strength': round(float(strength), 2),
                    'reason': reason, 'data_ready': True, 'indicators': values,
                    'entry_conditions_met': entry_ok, 'exit_conditions_met': exit_ok,
                    'buy_allowed': action == 'BUY' and strength >= self.min_buy_score}
        except Exception as exc:
            if '데이터 부족' in str(exc):
                log.info('YAML 전략 평가 보류(데이터 부족): symbol=%s file=%s', symbol_label(symbol), path)
                return {'symbol': symbol, 'action': 'HOLD', 'strength': 0.0,
                        'reason': f'평가 데이터 부족: {exc}', 'data_ready': False,
                        'status': 'insufficient_data', 'buy_allowed': False}
            log.exception('YAML 전략 평가 실패: symbol=%s file=%s', symbol_label(symbol), path)
            return {'symbol': symbol, 'action': 'ERROR', 'strength': 0.0,
                    'reason': f'{type(exc).__name__}: {exc}', 'data_ready': False, 'status': 'error'}

    @staticmethod
    def _normalise_bars(bars: list[dict]) -> list[dict]:
        result = []
        for row in bars or []:
            close = _number(_field(row, 'close', 'stck_clpr', 'STCK_CLPR'))
            if not close or close <= 0:
                continue
            result.append({
                'date': str(_field(row, 'date', 'stck_bsop_date', 'STCK_BSOP_DATE', default='')),
                'open': _number(_field(row, 'open', 'stck_oprc', 'STCK_OPRC')) or close,
                'high': _number(_field(row, 'high', 'stck_hgpr', 'STCK_HGPR')) or close,
                'low': _number(_field(row, 'low', 'stck_lwpr', 'STCK_LWPR')) or close,
                'close': close,
                'volume': _number(_field(row, 'volume', 'acml_vol', 'ACML_VOL')) or 0.0,
            })
        result.sort(key=lambda x: x['date'])
        return result

    @staticmethod
    def _indicators(rows: list[dict], definitions: Any) -> dict:
        closes = [r['close'] for r in rows]
        volumes = [r['volume'] for r in rows]
        last = rows[-1]
        out: dict[str, float] = {'close': closes[-1], 'open': last['open'], 'high': last['high'],
                                 'low': last['low'], 'volume': volumes[-1]}
        for item in definitions or []:
            if not isinstance(item, dict):
                continue
            alias = str(item.get('alias') or item.get('id') or '').strip()
            kind = str(item.get('type') or item.get('indicator') or item.get('name') or '').lower()
            params = item.get('params') or {}
            period = int(params.get('period', item.get('period', 20)) or 20)
            period = max(1, period)
            required = period + 1 if kind in {'rsi', 'pct_change', 'return_pct'} else period
            if len(rows) < required:
                raise ValueError(f'데이터 부족: {alias} {len(rows)} bars (최소 {required})')
            if not alias:
                continue
            if kind in {'sma', 'ma', 'moving_average', 'simple_moving_average'}:
                out[alias] = sum(closes[-period:]) / period
            elif kind in {'ema', 'exponential_moving_average'}:
                alpha = 2.0 / (period + 1.0)
                ema = closes[0]
                for v in closes[1:]:
                    ema = alpha * v + (1.0 - alpha) * ema
                out[alias] = ema
            elif kind == 'rsi':
                changes = [closes[i] - closes[i-1] for i in range(1, len(closes))][-period:]
                gains = sum(max(x, 0) for x in changes) / max(1, len(changes))
                losses = sum(max(-x, 0) for x in changes) / max(1, len(changes))
                out[alias] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
            elif kind in {'volume_sma', 'average_volume'}:
                out[alias] = sum(volumes[-period:]) / period
            elif kind in {'pct_change', 'return_pct'}:
                base = closes[-period-1] if len(closes) > period else closes[0]
                out[alias] = (closes[-1] / base - 1.0) * 100 if base else 0.0
            else:
                raise ValueError(f'지원하지 않는 지표: {kind} (alias={alias})')
        return out

    @staticmethod
    def _resolve(value: Any, values: dict, rows: list[dict]) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            if value in values:
                return values[value]
            return _number(value)
        if isinstance(value, dict):
            if 'value' in value:
                return _number(value['value'])
            ref = value.get('indicator') or value.get('alias') or value.get('field')
            if ref in values:
                return values[ref]
        return None

    def _evaluate_group(self, group: Any, values: dict, rows: list[dict]) -> tuple[bool, float, list[str]]:
        if isinstance(group, list):
            conditions, logic = group, 'AND'
        elif isinstance(group, dict):
            conditions = group.get('conditions', group.get('rules', []))
            logic = str(group.get('logic', group.get('operator_logic', 'AND'))).upper()
            if not conditions and {'left', 'operator', 'right'} <= set(group):
                conditions = [group]
        else:
            return False, 0.0, []
        if not conditions:
            return False, 0.0, []
        results, reasons, weights = [], [], []
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            left = self._resolve(cond.get('left', cond.get('lhs')), values, rows)
            right = self._resolve(cond.get('right', cond.get('rhs')), values, rows)
            op = str(cond.get('operator', cond.get('op', '>'))).strip().lower()
            if left is None or right is None:
                raise ValueError(f'조건 참조값을 계산할 수 없음: {cond}')
            ok = {
                '>': left > right, 'gt': left > right,
                '>=': left >= right, 'gte': left >= right,
                '<': left < right, 'lt': left < right,
                '<=': left <= right, 'lte': left <= right,
                '==': left == right, '=': left == right, 'eq': left == right,
                '!=': left != right, 'ne': left != right,
            }.get(op)
            if ok is None:
                raise ValueError(f'지원하지 않는 비교 연산자: {op}')
            results.append(ok)
            weight = max(0.0, float(cond.get('weight', 1.0) or 1.0))
            weights.append(weight)
            reasons.append(f"{cond.get('left', cond.get('lhs'))} {op} {cond.get('right', cond.get('rhs'))} ({'충족' if ok else '미충족'})")
        if not results:
            return False, 0.0, []
        success = all(results) if logic != 'OR' else any(results)
        denom = sum(weights) or len(weights)
        score = 100.0 * sum(w for w, ok in zip(weights, results) if ok) / denom
        # AND 전략은 전체 조건 충족 시에만 BUY, OR는 하나 이상 충족 시 BUY.
        return success, max(0.0, min(100.0, score)), reasons

    def evaluate_position_risk(self, path: str, position: dict, current_price: float) -> tuple[bool, str]:
        """YAML risk_management의 손절/익절/트레일링 규칙을 실제 보유 포지션에 적용합니다."""
        try:
            strategy = self.load(path)
            risk = strategy.get('risk_management', {}) or {}
            avg = _number(position.get('avg_price')) or 0.0
            if avg <= 0 or current_price <= 0:
                return False, ''
            pnl = (current_price / avg - 1.0) * 100.0
            stop = _number(risk.get('stop_loss_pct'))
            take = _number(risk.get('take_profit_pct'))
            trailing_gap = _number(risk.get('trailing_stop_pct', risk.get('trailing_gap_pct')))
            trailing_start = _number(risk.get('trailing_start_pct'))
            high = max(_number(position.get('high_price')) or 0.0, current_price)
            if stop is not None and pnl <= -abs(stop):
                return True, f'YAML_STOP_LOSS {pnl:.2f}% <= -{abs(stop):.2f}%'
            if take is not None and pnl >= abs(take):
                return True, f'YAML_TAKE_PROFIT {pnl:.2f}% >= {abs(take):.2f}%'
            if trailing_gap is not None and trailing_gap > 0 and high > 0:
                start = trailing_start if trailing_start is not None else 0.0
                high_pnl = (high / avg - 1.0) * 100.0
                drawdown = (current_price / high - 1.0) * 100.0
                if high_pnl >= start and drawdown <= -abs(trailing_gap):
                    return True, f'YAML_TRAILING_STOP drawdown={drawdown:.2f}% from high'
            return False, ''
        except Exception:
            log.exception('YAML risk_management 평가 실패: file=%s', path)
            return False, ''

    @staticmethod
    def _risk_reason(rows: list[dict], risk: dict) -> str:
        """YAML 위험 규칙을 OHLCV만으로 평가합니다. 포지션 기반 규칙은 exit_monitor에서 처리."""
        if not isinstance(risk, dict):
            return ''
        last = rows[-1]
        stop = _number(risk.get('stop_loss_pct'))
        take = _number(risk.get('take_profit_pct'))
        # 가격만으로 수익률을 추정할 수 없으므로 entry_price를 명시한 경우에만 계산.
        entry = _number(risk.get('reference_entry_price'))
        if entry and entry > 0:
            pnl = (last['close'] / entry - 1.0) * 100.0
            if stop is not None and pnl <= -abs(stop):
                return f'YAML 강제 손절: {pnl:.2f}%'
            if take is not None and pnl >= abs(take):
                return f'YAML 강제 익절: {pnl:.2f}%'
        return ''
