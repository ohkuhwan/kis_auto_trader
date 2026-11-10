#!/usr/bin/env python3
"""Summarize Strategy Builder JSONL signal logs without changing trading state."""
from __future__ import annotations
import argparse, json, sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from config.paths import runtime_path, resolve_path


def load_rows(paths: list[Path]) -> list[dict]:
    rows=[]
    for path in paths:
        if not path.exists():
            continue
        with path.open('r', encoding='utf-8') as fh:
            for line_no, line in enumerate(fh, 1):
                line=line.strip()
                if not line:
                    continue
                try:
                    item=json.loads(line)
                    if isinstance(item, dict): rows.append(item)
                except json.JSONDecodeError:
                    print(f"WARN: invalid JSON {path}:{line_no}", file=sys.stderr)
    return rows


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', help='JSONL files; default logs/strategy_signals.jsonl')
    parser.add_argument('--profile', choices=['all','unified'], default='all')
    parser.add_argument('--top', type=int, default=20)
    args=parser.parse_args()
    paths=[resolve_path(x) for x in args.paths] if args.paths else [runtime_path('strategy_signal_log')]
    rows=load_rows(paths)
    if args.profile != 'all': rows=[r for r in rows if r.get('profile') == args.profile]
    if not rows:
        print('전략 평가 로그가 없습니다. 먼저 scripts/run_strategy_review.sh를 실행하세요.')
        return 1
    by_strategy=defaultdict(list); by_symbol=defaultdict(list); profiles=Counter(); actions=Counter(); final_actions=Counter(); errors=[]
    for r in rows:
        sid=str(r.get('strategy_id','unknown')); sym=str(r.get('symbol','unknown'))
        by_strategy[(str(r.get('profile','?')),sid)].append(r)
        by_symbol[(str(r.get('profile','?')),sym)].append(r)
        profiles[str(r.get('profile','?'))]+=1
        action_key=(str(r.get('profile','?')),str(r.get('action','ERROR')).upper())
        if sid == '__profile__': final_actions[action_key]+=1
        else: actions[action_key]+=1
        if str(r.get('action','')).upper()=='ERROR' or r.get('status')=='error': errors.append(r)
    print('='*100)
    print('KIS Strategy Builder 평가 로그 분석')
    print(f'로그 파일: {", ".join(str(p) for p in paths)}')
    print(f'레코드: {len(rows)} | 프로필: {dict(profiles)} | 오류 레코드: {len(errors)}')
    print('\n[프로필별 전략별 시그널 수: __profile__ 집계 행 제외]')
    for profile in sorted({p for p,_ in actions}):
        print(f"  {profile}: BUY={actions[(profile,'BUY')]} HOLD={actions[(profile,'HOLD')]} SELL={actions[(profile,'SELL')]} ERROR={actions[(profile,'ERROR')]}")
    print('\n[프로필 최종 판단 수]')
    for profile in sorted({p for p,_ in final_actions}):
        print(f"  {profile}: BUY={final_actions[(profile,'BUY')]} HOLD={final_actions[(profile,'HOLD')]} SELL={final_actions[(profile,'SELL')]} ERROR={final_actions[(profile,'ERROR')]}")
    print('\n[전략별 결과]')
    print(f"{'프로필':<12} {'전략 ID':<22} {'N':>5} {'BUY':>5} {'HOLD':>5} {'SELL':>5} {'ERR':>5} {'평균강도':>9} {'결정사용':>9}")
    for (profile,sid), items in sorted(by_strategy.items()):
        c=Counter(str(x.get('action','ERROR')).upper() for x in items)
        strengths=[float(x.get('strength',0) or 0) for x in items if str(x.get('action','')).upper()!='ERROR']
        decision='Y' if any(x.get('decision_enabled') for x in items) else 'N'
        print(f"{profile:<12} {sid:<22} {len(items):>5} {c['BUY']:>5} {c['HOLD']:>5} {c['SELL']:>5} {c['ERROR']:>5} {mean(strengths) if strengths else 0:>9.3f} {decision:>9}")
    print('\n[종목별 BUY 의견 요약]')
    sym_rows=[]
    for (profile,symbol), items in by_symbol.items():
        active=[x for x in items if x.get('decision_enabled') and x.get('strategy_id') != '__profile__']
        buys=[x for x in active if str(x.get('action','')).upper()=='BUY']
        errs=[x for x in items if str(x.get('action','')).upper()=='ERROR']
        if buys or errs:
            sym_rows.append((len(buys),profile,symbol,buys,errs))
    for votes,profile,symbol,buys,errs in sorted(sym_rows, reverse=True)[:max(1,args.top)]:
        ids=','.join(str(x.get('strategy_id')) for x in buys) or '-'
        print(f"  {profile:<12} {symbol}: BUY {votes}개 [{ids}] | ERROR {len(errs)}개")
    if errors:
        print('\n[오류 샘플: 최대 10건]')
        for r in errors[:10]:
            print(f"  {r.get('timestamp')} {r.get('profile')} {r.get('strategy_id')} {r.get('symbol')}: {r.get('error') or r.get('reason')}")
    print('\n해석 주의: 이 보고서는 시그널 발생 빈도/오류를 요약할 뿐, 수익률·승률·미래 성과를 의미하지 않습니다.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
