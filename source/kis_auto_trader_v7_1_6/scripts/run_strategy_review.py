#!/usr/bin/env python3
"""Run internal profile evaluation using KIS REST daily bars; never places orders."""
from __future__ import annotations
import argparse, os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from strategies.builder_engine.official_adapter import OfficialBuilderAdapter
from config.paths import resolve_path
from config.builder_symbols import load_builder_symbols, normalize_symbols


def symbols_from_sources(args) -> list[str]:
    """Use explicit CLI symbols plus the one configured manual symbol file."""
    values = args.symbols.replace(';', ',').split(',') if args.symbols else []
    return normalize_symbols([*values, *load_builder_symbols()])


def main():
    load_dotenv(dotenv_path=ROOT / ".env")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['unified'], default='unified', help='V7.1.6 기준 통합 전략 설정만 지원합니다.')
    parser.add_argument('--symbols', help='Comma/semicolon-separated stock codes')
    args=parser.parse_args()
    symbols=symbols_from_sources(args)
    if not symbols:
        parser.error('종목이 없습니다. --symbols 또는 BUILDER_SYMBOLS_FILE로 지정한 종목 목록 파일을 설정하세요.')
    from config.settings import load_settings
    from brokers.kis.client import KisApi
    settings=load_settings()
    if not settings.app_key or not settings.app_secret:
        parser.error('KIS_APP_KEY/KIS_APP_SECRET이 필요합니다. .env 설정을 확인하세요.')
    api=KisApi(settings); api.ensure_token()
    adapter=OfficialBuilderAdapter(str(resolve_path(os.getenv('STRATEGY_PROFILES_FILE','config/strategy_profiles.yaml'))))
    profiles=['unified']
    bars_by_symbol={}
    for symbol in symbols:
        try: bars_by_symbol[symbol]=api.daily_prices(symbol,days=260)
        except Exception as exc:
            print(f"[DATA ERROR] {symbol}: {type(exc).__name__}: {exc}")
            bars_by_symbol[symbol]=None
    for profile in profiles:
        results=adapter.evaluate(profile,symbols,api=api,bars_by_symbol=bars_by_symbol)
        print(f'\n[{profile}] 평가 종목={len(symbols)}')
        for symbol,result in sorted(results.items()):
            enabled=[x for x in result['strategy_results'] if x.get('decision_enabled')]
            votes=','.join(x['strategy_id'] for x in enabled if x.get('action')=='BUY') or '-'
            print(f"{symbol} action={result['action']} score={result['score']:.2f} votes={result['buy_votes']} buy_strategies={votes} errors={len(result['errors'])}")
    print('\n평가 로그 기록 완료. 주문은 제출하지 않았습니다.')

if __name__=='__main__': main()
