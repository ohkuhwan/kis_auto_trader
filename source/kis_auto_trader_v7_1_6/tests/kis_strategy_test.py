#!/usr/bin/env python3
"""V7.0.0: 10개 내부 전략의 YAML 항목/시그널/KIS API 데이터 테스트.

기본 실행은 .env의 KIS 계정으로 시세 조회만 수행하며 주문 API는 호출하지 않습니다.
--offline 은 첨부 종목 DB의 무작위 코드/이름과 합성 OHLCV를 사용하며 API/인증 없이 평가합니다.
--sample-size는 5 또는 10, --seed는 랜덤 종목 추출 재현값입니다.
"""
from __future__ import annotations
import argparse, csv, json, logging, random, sys, time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from strategies.builder_engine.internal_engine import evaluate_strategy, STRATEGY_NAMES
from strategies.builder_engine.official_adapter import OfficialBuilderAdapter
from config.paths import runtime_path

EXPECTED = {
    "golden_cross", "momentum", "week52_high", "consecutive", "disparity",
    "breakout_fail", "strong_close", "volatility", "mean_reversion", "trend_filter"
}

def load_symbols(path: Path) -> list[dict[str, str]]:
    """첨부 symbol-properties-database에서 종목코드/명을 읽어 중복을 제거합니다."""
    if not path.is_file(): raise FileNotFoundError(f"종목 데이터베이스가 없습니다: {path}")
    result=[]; seen=set()
    # 원본 파일이 UTF-8이 아닌 경우도 처리하되 원본을 변경하지 않습니다.
    text=None
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try: text=path.read_text(encoding=enc); break
        except UnicodeDecodeError: continue
    if text is None: raise UnicodeError(f"종목 데이터베이스 인코딩을 판별할 수 없습니다: {path}")
    import io
    for row in csv.reader(io.StringIO(text)):
        if len(row)<4: continue
        code=str(row[1]).strip().zfill(6); name=str(row[3]).strip(); kind=str(row[2]).strip().lower()
        if len(code)!=6 or not code.isdigit() or not name or name.lower() in {"name","종목명"}: continue
        if kind not in {"equity", "stock", "common_stock", ""}: continue
        if code in seen: continue
        seen.add(code); result.append({"code":code,"name":name})
    if not result: raise ValueError(f"유효한 종목코드를 찾지 못했습니다: {path}")
    return result

def synthetic_bars(n: int = 280, seed: int = 17) -> list[dict]:
    """오프라인 테스트 fixture: 상승/하락 구간을 포함하는 합성 OHLCV입니다."""
    rng=random.Random(seed); close=10000.0; bars=[]
    for i in range(n):
        drift=0.0008 if i%70<45 else -0.0004
        close=max(100.0,close*(1+drift+rng.uniform(-0.012,0.012)))
        spread=close*rng.uniform(0.003,0.025)
        low=max(1.0,close-spread*rng.uniform(.3,1.0)); high=close+spread*rng.uniform(.3,1.0)
        op=close*(1+rng.uniform(-.01,.01))
        bars.append({"date":f"{2020+i//240:04d}{(i//20)%12+1:02d}{i%20+1:02d}","open":op,"high":max(high,op,close),"low":min(low,op,close),"close":close,"volume":int(rng.randint(10000,1000000))})
    return bars

def validate_yaml_files() -> list[tuple[str, dict]]:
    files=sorted((ROOT/"config"/"strategies").glob("*.kis.yaml"))
    if len(files)!=10: raise AssertionError(f"전략 YAML 파일은 10개여야 합니다. 현재={len(files)}")
    loaded=[]; ids=set()
    for path in files:
        raw=yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw,dict): raise AssertionError(f"YAML 최상위 mapping 오류: {path.name}")
        strategy=raw.get("strategy") or {}; sid=strategy.get("id")
        if not sid or sid in ids: raise AssertionError(f"전략 ID 누락/중복: {path.name} id={sid}")
        ids.add(sid)
        for section in ("metadata","strategy","risk"):
            if not isinstance(raw.get(section),dict): raise AssertionError(f"필수 섹션 누락: {path.name}/{section}")
        for risk_key in ("stop_loss","take_profit","trailing_stop"):
            risk=raw["risk"].get(risk_key)
            if not isinstance(risk,dict) or "enabled" not in risk or "percent" not in risk:
                raise AssertionError(f"위험관리 항목/필드 누락: {path.name}/{risk_key}")
        for key in ("enabled","execution_mode","params","indicators","entry","exit","category"):
            if key not in strategy: raise AssertionError(f"전략 설정 항목 누락: {path.name}/strategy/{key}")
        if not isinstance(strategy.get("params"),dict) or not strategy["params"]:
            raise AssertionError(f"전략 파라미터가 비어 있습니다: {path.name}")
        if not isinstance(strategy.get("indicators"),list) or not strategy["indicators"]:
            raise AssertionError(f"지표 메타데이터가 비어 있습니다: {path.name}")
        for indicator in strategy["indicators"]:
            for field in ("id","alias","enabled","params"):
                if field not in indicator: raise AssertionError(f"지표 필드 누락: {path.name}/{indicator}/{field}")
            if not isinstance(indicator["params"],dict): raise AssertionError(f"지표 params는 mapping이어야 합니다: {path.name}")
        for group_name in ("entry","exit"):
            group=strategy[group_name]
            if not all(key in group for key in ("enabled","logic","conditions")):
                raise AssertionError(f"조건 그룹 필드 누락: {path.name}/{group_name}")
        if not all(key in raw["metadata"] for key in ("name","description","author","tags")):
            raise AssertionError(f"metadata 필드 누락: {path.name}")
        if "#" not in path.read_text(encoding="utf-8"):
            raise AssertionError(f"설명 주석이 없습니다: {path.name}")
        loaded.append((path.name,raw))
    if ids!=EXPECTED: raise AssertionError(f"전략 ID 불일치: expected={EXPECTED}, actual={ids}")
    return loaded

def make_api():
    from config.settings import load_settings
    settings=load_settings()
    if not settings.paper:
        raise RuntimeError("전략 테스트의 KIS API 모드는 KIS_PAPER=true만 허용합니다. 실전 API 사용을 차단했습니다.")
    if not settings.app_key or not settings.app_secret:
        raise RuntimeError("KIS_APP_KEY/KIS_APP_SECRET이 없습니다. .env를 설정하거나 --offline을 사용하세요.")
    # Import the network client only after the paper-only guard passes.
    from brokers.kis.client import KisApi
    api=KisApi(settings); api.ensure_token()
    return api

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size",type=int,choices=(5,10),default=5,help="랜덤 추출 종목 수(5 또는 10).")
    parser.add_argument("--seed",type=int,default=None,help="랜덤 시드. 지정하면 같은 종목을 다시 선택합니다.")
    parser.add_argument("--symbols-file",default=str(ROOT/"tests/data/symbol-properties-database.csv"),help="symbol-properties-database.csv 경로.")
    parser.add_argument("--offline",action="store_true",help="종목 DB에서 무작위 선택 + 합성 OHLCV; KIS API/인증 없음.")
    parser.add_argument("--profile",choices=("both","aggressive","stable"),default="both",help="프로필 집계 검증 대상.")
    parser.add_argument("--delay",type=float,default=0.35,help="KIS 종목 간 대기 초(호출 제한 보호).")
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    try: strategies=validate_yaml_files()
    except Exception as exc:
        print(f"[FAIL] YAML 구조 검증: {exc}",file=sys.stderr); return 2
    print(f"[PASS] 전략 YAML 10개 및 필수 항목/주석 검증 완료")
    if args.offline:
        # Use real codes/names from the attached symbol-properties database while
        # keeping all price bars synthetic, so this path is reproducible and offline.
        try: all_symbols=load_symbols(Path(args.symbols_file))
        except Exception as exc: print(f"[FAIL] 종목 DB 로드: {exc}",file=sys.stderr); return 2
        rng=random.Random(args.seed); symbols=rng.sample(all_symbols,min(args.sample_size,len(all_symbols)))
        bars_by_symbol={x["code"]:synthetic_bars(seed=(args.seed or 17)+i) for i,x in enumerate(symbols)}
        api=None
        print("[MODE] OFFLINE: 첨부 종목 DB에서 무작위 추출 + 합성 OHLCV, KIS 인증/주문 없음")
    else:
        try: all_symbols=load_symbols(Path(args.symbols_file))
        except Exception as exc: print(f"[FAIL] 종목 DB 로드: {exc}",file=sys.stderr); return 2
        rng=random.Random(args.seed); symbols=rng.sample(all_symbols,min(args.sample_size,len(all_symbols)))
        try: api=make_api()
        except Exception as exc: print(f"[FAIL] KIS API 초기화: {exc}",file=sys.stderr); return 3
        bars_by_symbol={}
        print(f"[MODE] KIS API 시세조회 전용 / paper={api.s.paper} / 주문 호출 없음")
    print("\n[선택 종목]")
    for stock in symbols: print(f"  {stock['code']} {stock['name']}")
    print("\n[전략별 시그널]")
    records=[]; errors=0
    for stock in symbols:
        code=stock["code"]
        if code not in bars_by_symbol:
            try:
                bars_by_symbol[code]=api.daily_prices(code,days=260)
                if args.delay>0: time.sleep(args.delay)
            except Exception as exc:
                bars_by_symbol[code]=None
                print(f"  [DATA ERROR] {code} {stock['name']}: {type(exc).__name__}: {exc}")
        for filename,raw in strategies:
            if bars_by_symbol.get(code) is None:
                result={"action":"ERROR","strength":0,"reason":"시세 수집 실패","data_ready":False}; errors+=1
            else:
                result=evaluate_strategy(raw,bars_by_symbol[code],symbol=code,purpose="both")
                if result.get("action")=="ERROR": errors+=1
            row={"symbol":code,"name":stock["name"],"file":filename,"strategy_id":raw["strategy"]["id"],**result}
            records.append(row)
            print(f"  {code} {stock['name']:<16.16} | {raw['strategy']['id']:<16} | {result.get('action','ERROR'):<5} | strength={float(result.get('strength',0) or 0):.3f} | bars={result.get('bars','-')} | {result.get('reason','')}")
    # 각 전략 YAML의 활성화/위험관리/파라미터가 실제 결과 경로에서 확인되도록 별도 점검 결과 출력.
    print("\n[YAML 필드 사용/안전 점검]")
    for filename,raw in strategies:
        st=raw["strategy"]; risk=raw["risk"]
        enabled=str(st.get("enabled",True)).lower() not in {"false","0","off","no",""}
        params=st.get("params",{}) or {}; active_params=sum(v not in (0,0.0,"",None) for v in params.values())
        risk_on=[k for k,v in risk.items() if isinstance(v,dict) and str(v.get("enabled",False)).lower() not in {"false","0","off","no",""}]
        own=[r for r in records if r.get("file")==filename]
        actions={a:sum(1 for x in own if x.get("action")==a) for a in ("BUY","SELL","HOLD","ERROR")}
        print(f"  {st['id']}: enabled={enabled}, params={active_params}/{len(params)}, risk_enabled={','.join(risk_on) or '-'}, signals={actions}")
    if not args.offline and api:
        # 프로필 결합 로직을 같은 KIS 시세로 실행하여 BUY/SELL 투표 경로도 검사합니다.
        adapter=OfficialBuilderAdapter(str(ROOT/"config/strategy_profiles.yaml"))
        profiles=["unified"]
        for profile in profiles:
            try:
                combined=adapter.evaluate(profile,[s["code"] for s in symbols],observe_all=True,api=api,bars_by_symbol=bars_by_symbol)
                print(f"\n[프로필 집계: {profile}]")
                for code,item in combined.items(): print(f"  {code}: {item['action']} buy_votes={item['buy_votes']} buy_score={item['score']:.2f} sell_votes={item['sell_votes']} errors={len(item['errors'])}")
            except Exception as exc:
                errors+=1; print(f"[PROFILE ERROR] {profile}: {type(exc).__name__}: {exc}")
    output=runtime_path("log_dir")/"kis_strategy_test_results.json"
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({"offline":args.offline,"sample_size":len(symbols),"seed":args.seed,"results":records},ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"\n결과 JSON: {output}")
    print(f"총 평가={len(records)}, ERROR={errors}. 이 테스트는 신호 계산/설정 검증이며 수익성 또는 실전 수익을 보장하지 않습니다.")
    return 0 if errors==0 else 1

if __name__=="__main__": raise SystemExit(main())
