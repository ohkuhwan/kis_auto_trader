"""KIS Strategy Builder 10개 기본 전략의 프로젝트 내부 평가 엔진.

공식 저장소 strategy_builder/examples/run_all_strategies.py 및 strategy/strategy_01~10의
전략 정의를 참고해 시그널 계산을 내부화했습니다. 별도 FastAPI/Builder 서버는 사용하지 않습니다.
본 모듈은 주문을 제출하지 않으며, 모든 가격 데이터는 호출자가 제공한 KIS REST OHLCV로만 계산합니다.
"""
from __future__ import annotations
from math import sqrt
from typing import Any

STRATEGY_NAMES = {
    "golden_cross": "골든크로스", "momentum": "모멘텀", "week52_high": "52주 신고가",
    "consecutive": "연속 상승/하락", "disparity": "이격도", "breakout_fail": "돌파 실패",
    "strong_close": "강한 종가", "volatility": "변동성 확장", "mean_reversion": "평균회귀",
    "trend_filter": "추세 필터",
}

def _num(v: Any, default: float = 0.0) -> float:
    try: return float(str(v).replace(',', '').strip()) if v not in (None, '') else default
    except (TypeError, ValueError): return default

def _enabled(v: Any) -> bool:
    return v not in (None, '', 0, 0.0, False, '0', 'false', 'False', 'off', 'OFF')

def _params(raw: dict) -> dict:
    """YAML의 strategy.params 및 지표 params를 합칩니다. 비활성 0/빈 문자열은 기본값 사용."""
    strategy = raw.get('strategy', {}) or {}
    result = {}
    for key, value in (strategy.get('params', {}) or {}).items():
        if isinstance(value, dict): value = value.get('value', value.get('default', 0))
        if _enabled(value): result[key] = value
    # 프리셋 파일은 주요 파라미터를 명시적으로 기재하며 지표 파라미터도 호환합니다.
    for item in strategy.get('indicators', []) or []:
        if not isinstance(item, dict): continue
        alias = str(item.get('alias') or item.get('id') or '')
        for key, value in (item.get('params', {}) or {}).items():
            if _enabled(value):
                result.setdefault(key, value)
                result.setdefault(f'{alias}_{key}', value)
    return result

def _bars(raw_bars: list[dict]) -> list[dict]:
    result=[]
    for row in raw_bars or []:
        close=_num(row.get('close', row.get('stck_clpr', row.get('STCK_CLPR'))))
        if close <= 0: continue
        result.append({'date':str(row.get('date', row.get('stck_bsop_date',''))),
                       'open':_num(row.get('open',row.get('stck_oprc')),close),
                       'high':_num(row.get('high',row.get('stck_hgpr')),close),
                       'low':_num(row.get('low',row.get('stck_lwpr')),close),
                       'close':close,'volume':_num(row.get('volume',row.get('acml_vol')))})
    result.sort(key=lambda x:x['date'])
    # 날짜가 비어 있는 테스트 fixture도 순서를 유지하고 중복 날짜는 그대로 둡니다.
    return result

def _sma(values: list[float], period: int) -> float:
    return sum(values[-period:])/period if period > 0 and len(values)>=period else 0.0

def _cross_up(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    return prev_a <= prev_b and curr_a > curr_b

def _cross_down(prev_a: float, prev_b: float, curr_a: float, curr_b: float) -> bool:
    return prev_a >= prev_b and curr_a < curr_b

def _yaml_values(rows: list[dict], definitions: list[dict], params: dict) -> tuple[dict, dict]:
    """YAML indicators를 현재/직전 봉 기준으로 계산합니다. 조건식은 이 결과만 참조합니다."""
    def at(prefix: list[dict]) -> dict:
        if not prefix: return {}
        closes=[x['close'] for x in prefix]; highs=[x['high'] for x in prefix]; lows=[x['low'] for x in prefix]
        last=prefix[-1]; prev=closes[-2] if len(closes)>1 else closes[-1]
        out={'close':closes[-1],'open':last['open'],'high':last['high'],'low':last['low'],
             'volume':last['volume'],'prev_close':prev}
        for item in definitions or []:
            if not isinstance(item,dict): continue
            if str(item.get('enabled',True)).lower() in {'false','0','off','no',''}: continue
            kind=str(item.get('id','')).lower(); alias=str(item.get('alias') or kind); ip=item.get('params') or {}
            period_raw=ip.get('period',20)
            if isinstance(period_raw,str) and period_raw.startswith('$'): period_raw=params.get(period_raw[1:],20)
            n=max(1,int(_num(period_raw,20)))
            if kind in {'sma','ma','moving_average'}: value=_sma(closes,n)
            elif kind in {'maximum','max'}:
                value=max(highs[-n-1:-1] or highs[-n:])
                if alias == 'high_252': value *= (1.0 + _num(params.get('breakout_margin_pct', 0), 0) / 100.0)
            elif kind in {'minimum','min'}: value=min(lows[-n-1:-1] or lows[-n:])
            elif kind=='momentum': value=(closes[-1]/closes[-n-1]-1) if len(closes)>n and closes[-n-1] else 0.0
            elif kind=='disparity':
                ma=_sma(closes,n); value=closes[-1]/ma*100 if ma else 0.0
            elif kind=='consecutive':
                up=down=0
                for i in range(len(closes)-1,0,-1):
                    if closes[i]>closes[i-1] and down==0: up+=1
                    else: break
                for i in range(len(closes)-1,0,-1):
                    if closes[i]<closes[i-1] and up==0: down+=1
                    else: break
                value=up if up else -down
            elif kind=='strong_close': value=(last['close']-last['low'])/(last['high']-last['low']) if last['high']>last['low'] else 0.0
            elif kind=='mean_deviation':
                ma=_sma(closes,n); value=(closes[-1]/ma-1)*100 if ma else 0.0
            elif kind=='breakout_fail':
                within=max(1,int(_num(params.get('fail_within_days',3),3))); lookback=max(2,int(_num(params.get('lookback_days',20),20)))
                recent=highs[-within:]; prior=highs[-(lookback+within):-within]
                drop=(closes[-1]/max(recent)-1)*100 if recent else 0.0
                threshold=_num(params.get('fail_threshold_pct',-3),-3)
                value=1.0 if prior and recent and max(recent)>max(prior) and drop<=threshold else 0.0
            elif kind=='volatility_breakout':
                n=max(2,int(_num(params.get('lookback_days',10),10))); breakout=_num(params.get('breakout_pct',3),3)
                import statistics
                rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes)) if closes[i-1]>0]
                rolling=[statistics.pstdev(rets[j-n:j]) for j in range(n,len(rets)+1)]
                change=(closes[-1]/prev-1)*100 if prev else 0.0
                value=1.0 if rolling and rolling[-1]<=min(rolling[-n:])*1.1 and change>=breakout else 0.0
            elif kind=='atr':
                true_ranges=[]
                for i in range(max(0,len(prefix)-n),len(prefix)):
                    previous=closes[i-1] if i>0 else prefix[i]['close']
                    true_ranges.append(max(prefix[i]['high']-prefix[i]['low'],abs(prefix[i]['high']-previous),abs(prefix[i]['low']-previous)))
                value=sum(true_ranges)/len(true_ranges) if true_ranges else 0.0
            else: raise ValueError(f'YAML indicator not supported: {kind} (alias={alias})')
            out[alias]=value
        return out
    return at(rows), at(rows[:-1]) if len(rows)>1 else at(rows)

def _yaml_group(group: dict, values: dict, previous: dict, params: dict | None = None) -> tuple[bool, list[str]]:
    """YAML entry/exit condition groups. Missing references/operators are hard errors."""
    if not isinstance(group,dict) or str(group.get('enabled',True)).lower() in {'false','0','off','no',''}:
        return False, ['조건 그룹 비활성화']
    conditions=group.get('conditions',[]) or []
    if not conditions: return False, ['조건 없음']
    logic=str(group.get('logic','AND')).upper(); checks=[]; reasons=[]
    for cond in conditions:
        left_key=str(cond.get('indicator','')); right_raw=cond.get('compare_to')
        if left_key not in values: raise ValueError(f'YAML 조건의 indicator를 찾을 수 없음: {left_key}')
        left=values[left_key]
        if isinstance(right_raw,str) and right_raw in values: right=values[right_raw]
        elif isinstance(right_raw,str) and right_raw.startswith('-$'):
            key=right_raw[2:]; source=(params or {}).get(key)
            if source in (None,''): raise ValueError(f'YAML 파라미터를 해석할 수 없음: {right_raw!r}')
            right=-abs(_num(source))
        elif isinstance(right_raw,str) and right_raw.startswith('$'):
            key=right_raw[1:]; source=(params or {}).get(key)
            if source in (None,''): raise ValueError(f'YAML 파라미터를 해석할 수 없음: {right_raw!r}')
            right=_num(source,float('nan'))
        else:
            right=_num(right_raw,float('nan'))
            if right!=right: raise ValueError(f'YAML compare_to를 해석할 수 없음: {right_raw!r}')
        op=str(cond.get('operator','')).lower()
        if op in {'greater_than','gt','>'}: ok=left>right
        elif op in {'less_than','lt','<'}: ok=left<right
        elif op in {'greater_equal','gte','>='}: ok=left>=right
        elif op in {'less_equal','lte','<='}: ok=left<=right
        elif op in {'equals','equal','==','='}: ok=left==right
        elif op in {'cross_above','cross_below'}:
            prev_left=previous.get(left_key,left)
            prev_right=previous.get(right_raw,right) if isinstance(right_raw,str) else right
            ok=(prev_left<=prev_right and left>right) if op=='cross_above' else (prev_left>=prev_right and left<right)
        else: raise ValueError(f'YAML operator not supported: {op}')
        checks.append(ok); reasons.append(f'{left_key} {op} {right_raw}: {"충족" if ok else "미충족"}')
    return (any(checks) if logic=='OR' else all(checks)), reasons

def evaluate_strategy(raw: dict, raw_bars: list[dict], symbol: str = '', purpose: str = 'both') -> dict:
    """단일 전략 YAML을 OHLCV로 평가해 BUY/SELL/HOLD/ERROR를 반환합니다.

    strength는 0~1 범위의 신호 강도이며 수익 확률이 아닙니다. 보유 여부를 모르는
    단일 평가에서 SELL은 매도 조건의 관측값입니다. 매매 주문 여부는 상위 위험관리 계층이 결정합니다.
    """
    sid=str((raw.get('strategy') or {}).get('id','')).strip()
    meta=raw.get('metadata') or {}
    strategy_config=raw.get('strategy') or {}
    if str(strategy_config.get('enabled', True)).strip().lower() in {'false','0','off','no',''}:
        return {'symbol':symbol,'strategy_id':sid,'action':'HOLD','strength':0.0,'reason':'전략 YAML enabled 비활성화',
                'data_ready':True,'bars':len(raw_bars or []),'required_bars':0,'status':'disabled'}
    try:
        rows=_bars(raw_bars); closes=[x['close'] for x in rows]; highs=[x['high'] for x in rows]
        lows=[x['low'] for x in rows]; volumes=[x['volume'] for x in rows]
        if purpose not in {'entry','exit','both'}: raise ValueError('purpose must be entry, exit, or both')
        if not rows: return {'symbol':symbol,'action':'ERROR','strength':0.0,'reason':'KIS 일봉 데이터 없음','data_ready':False}
        p=_params(raw); last=rows[-1]; close=closes[-1]; prev=closes[-2] if len(closes)>1 else close
        action='HOLD'; strength=0.0; reason='조건 미충족'; required=1
        if sid=='golden_cross':
            short=max(1,int(_num(p.get('short_period',5),5))); long=max(2,int(_num(p.get('long_period',20),20))); required=long+1
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            a0=_sma(closes[:-1],short); b0=_sma(closes[:-1],long); a1=_sma(closes,short); b1=_sma(closes,long)
            if purpose!='exit' and _cross_up(a0,b0,a1,b1): action,strength,reason='BUY',0.70,f'골든크로스 MA{short}/{long}'
            elif purpose!='entry' and _cross_down(a0,b0,a1,b1): action,strength,reason='SELL',0.70,f'데드크로스 MA{short}/{long}'
        elif sid=='momentum':
            n=max(1,int(_num(p.get('lookback_days',60),60))); required=n+1
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            ret=close/closes[-n-1]-1; buy=_num(p.get('buy_threshold',0.30),0.30); sell=_num(p.get('sell_threshold',-0.20),-0.20)
            if purpose!='exit' and ret>=buy: action,strength,reason='BUY',min(1.0,0.5+ret),f'{n}일 수익률 +{ret*100:.1f}%'
            elif purpose!='entry' and ret<=sell: action,strength,reason='SELL',0.80,f'{n}일 수익률 {ret*100:.1f}%'
        elif sid=='week52_high':
            n=max(2,int(_num(p.get('lookback_days',252),252))); margin=_num(p.get('breakout_margin_pct',0),0); required=n+1
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            prior=max(highs[-n-1:-1] or highs[-n:]); threshold=prior*(1+margin/100)
            if purpose!='exit' and close>threshold: action,strength,reason='BUY',0.85,f'52주 고점 돌파: close={close:g}, threshold={threshold:g}'
        elif sid=='consecutive':
            buy_n=max(1,int(_num(p.get('buy_days',5),5))); sell_n=max(1,int(_num(p.get('sell_days',5),5))); required=max(buy_n,sell_n)+1
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            ups=downs=0
            for i in range(len(closes)-1,0,-1):
                if closes[i]>closes[i-1] and downs==0: ups+=1
                else: break
            for i in range(len(closes)-1,0,-1):
                if closes[i]<closes[i-1] and ups==0: downs+=1
                else: break
            if purpose!='exit' and ups>=buy_n: action,strength,reason='BUY',min(.9,.5+ups*.08),f'{ups}일 연속 상승'
            elif purpose!='entry' and downs>=sell_n: action,strength,reason='SELL',min(.9,.5+downs*.08),f'{downs}일 연속 하락'
        elif sid=='disparity':
            n=max(1,int(_num(p.get('period',20),20))); buy=_num(p.get('oversold_threshold',90),90); sell=_num(p.get('overbought_threshold',110),110); required=n
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            ma=_sma(closes,n); disp=close/ma*100 if ma else 0
            if purpose!='exit' and disp<buy: action,strength,reason='BUY',min(1.0,.5+(buy-disp)/20),f'이격도 {disp:.2f} 과매도'
            elif purpose!='entry' and disp>sell: action,strength,reason='SELL',min(1.0,.5+(disp-sell)/20),f'이격도 {disp:.2f} 과매수'
        elif sid=='breakout_fail':
            n=max(2,int(_num(p.get('lookback_days',20),20))); within=max(1,int(_num(p.get('fail_within_days',3),3))); threshold=_num(p.get('fail_threshold_pct',-3),-3); required=n+within+1
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            prior_window=highs[-(n+within):-within] or highs[:-within]; prior_high=max(prior_window); recent_high=max(highs[-within:]); drop=(close/recent_high-1)*100 if recent_high else 0
            if purpose!='entry' and recent_high>prior_high and drop<=threshold: action,strength,reason='SELL',0.90,f'돌파 실패: 고점 대비 {drop:.2f}%'
        elif sid=='strong_close':
            min_ratio=_num(p.get('min_close_ratio',.8),.8); span=last['high']-last['low']; required=1
            if span<=0:
                # 장 시작 전/당일 고저가 미형성 구간은 전략 오류가 아니라 평가 보류입니다.
                action,strength,reason='HOLD',0.0,'당일 고가/저가 범위 0: 강한 종가 평가 보류'
            else:
                ratio=(close-last['low'])/span
                if purpose!='exit' and ratio>=min_ratio: action,strength,reason='BUY',min(1.0,.5+ratio*.4),f'강한 종가 비율 {ratio*100:.1f}%'
        elif sid=='volatility':
            n=max(2,int(_num(p.get('lookback_days',10),10))); breakout=_num(p.get('breakout_pct',3),3); required=n+10
            if len(rows)<required: raise ValueError(f'데이터 부족: {len(rows)} bars, 최소 {required}')
            # 공식 프리셋의 calc_volatility 의미에 맞춰 종가 일간수익률의 롤링 표준편차를 계산합니다.
            import statistics
            returns=[(closes[i]/closes[i-1]-1.0) for i in range(1,len(closes)) if closes[i-1]>0]
            rolling=[]
            for end_idx in range(n, len(returns)+1): rolling.append(statistics.pstdev(returns[end_idx-n:end_idx]))
            curr=rolling[-1] if rolling else 0.0; minimum=min(rolling[-n:]) if rolling else 0.0; change=(close/prev-1)*100 if prev else 0
            if purpose!='exit' and curr<=minimum*1.1 and change>=breakout: action,strength,reason='BUY',.75,f'변동성 축소 후 돌파 +{change:.2f}%' 
        elif sid=='mean_reversion':
            n=max(1,int(_num(p.get('period',5),5))); buy=_num(p.get('buy_threshold',p.get('buy_threshold_pct',-3)),-3); sell=_num(p.get('sell_threshold',p.get('sell_threshold_pct',3)),3); required=n
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            ma=_sma(closes,n); deviation=(close/ma-1)*100 if ma else 0
            if purpose!='exit' and deviation<=buy: action,strength,reason='BUY',min(1.0,.5+abs(deviation)/10),f'{n}일 평균 대비 {deviation:.2f}% 이탈'
            elif purpose!='entry' and deviation>=sell: action,strength,reason='SELL',min(1.0,.5+deviation/10),f'{n}일 평균 대비 +{deviation:.2f}% 이탈'
        elif sid=='trend_filter':
            n=max(2,int(_num(p.get('ma_period',60),60))); required=n
            if len(closes)<required: raise ValueError(f'데이터 부족: {len(closes)} bars, 최소 {required}')
            ma=_sma(closes,n)
            if purpose!='exit' and close>ma and close>prev: action,strength,reason='BUY',.65,f'종가가 MA{n} 위이며 전일 대비 상승'
            elif purpose!='entry' and close<ma and close<prev: action,strength,reason='SELL',.65,f'종가가 MA{n} 아래이며 전일 대비 하락'
        else:
            # Builder에서 Export한 사용자 정의 YAML도 공통 조건 엔진으로 평가합니다.
            return evaluate_custom(raw, rows, symbol, purpose)
        # Keep the preset's signal strength as the first choice. YAML conditions remain
        # authoritative for BUY/SELL, but replacing every matching preset strength with a
        # generic 0.70/0.80 would silently change the official strategy's score semantics.
        preset_action, preset_strength = action, strength
        strategy_cfg=raw.get('strategy') or {}
        if str(strategy_cfg.get('enabled',True)).strip().lower() in {'false','0','off','no',''}:
            action,strength,reason='HOLD',0.0,'전략 YAML enabled 비활성화'
        elif sid == 'strong_close' and last['high'] <= last['low']:
            # 당일 고가/저가 범위가 아직 형성되지 않은 경우는 전략 오류가 아니라 평가 보류입니다.
            action,strength,reason='HOLD',0.0,'당일 고가/저가 범위 0: 강한 종가 평가 보류'
        elif strategy_cfg.get('entry') is not None or strategy_cfg.get('exit') is not None:
            values,previous=_yaml_values(rows,strategy_cfg.get('indicators',[]) or [],p)
            entry_ok,entry_reasons=_yaml_group(strategy_cfg.get('entry') or {},values,previous,p)
            exit_ok,exit_reasons=_yaml_group(strategy_cfg.get('exit') or {},values,previous,p)
            if purpose=='entry':
                if entry_ok:
                    action='BUY'; strength=preset_strength if preset_action=='BUY' else 0.70
                    reason='YAML 진입 조건 충족: '+'; '.join(entry_reasons)
                else:
                    action,strength,reason='HOLD',0.0,'YAML 진입 조건 미충족: '+'; '.join(entry_reasons)
            elif purpose=='exit':
                if exit_ok:
                    action='SELL'; strength=preset_strength if preset_action=='SELL' else 0.80
                    reason='YAML 청산 조건 충족: '+'; '.join(exit_reasons)
                else:
                    action,strength,reason='HOLD',0.0,'YAML 청산 조건 미충족: '+'; '.join(exit_reasons)
            elif exit_ok:
                action='SELL'; strength=preset_strength if preset_action=='SELL' else 0.80
                reason='YAML 청산 조건 충족: '+'; '.join(exit_reasons)
            elif entry_ok:
                action='BUY'; strength=preset_strength if preset_action=='BUY' else 0.70
                reason='YAML 진입 조건 충족: '+'; '.join(entry_reasons)
            else:
                action,strength,reason='HOLD',0.0,'YAML 조건 미충족: '+'; '.join(entry_reasons)
        return {'symbol':symbol,'strategy_id':sid,'action':action,'strength':round(max(0,min(1,strength)),4),
                'reason':reason,'data_ready':True,'bars':len(rows),'required_bars':required,
                'indicators':{'close':close,'previous_close':prev},'status':'success'}
    except Exception as exc:
        message = str(exc)
        if '데이터 부족' in message or 'OHLCV 데이터 없음' in message:
            return {'symbol':symbol,'strategy_id':sid,'action':'HOLD','strength':0.0,
                    'reason':f'평가 데이터 부족: {message}','data_ready':False,'status':'insufficient_data',
                    'error':None}
        return {'symbol':symbol,'strategy_id':sid,'action':'ERROR','strength':0.0,
                'reason':f'{type(exc).__name__}: {exc}','data_ready':False,'status':'error','error':str(exc)}

def evaluate_custom(raw: dict, rows: list[dict], symbol: str, purpose: str) -> dict:
    """사용자 정의 .kis.yaml의 기본 SMA/EMA/RSI/ATR/최고·최저 지표 및 비교식을 평가합니다.

    지원하지 않는 지표/연산자는 조용히 무시하지 않고 ERROR로 반환해 fail-closed 합니다.
    """
    import statistics
    strategy=raw.get('strategy') or {}; closes=[r['close'] for r in rows]; highs=[r['high'] for r in rows]; lows=[r['low'] for r in rows]
    values={'close':closes[-1],'open':rows[-1]['open'],'high':highs[-1],'low':lows[-1],'volume':rows[-1]['volume']}
    prev_values={k:v for k,v in values.items()}; indicators={}
    for item in strategy.get('indicators',[]) or []:
        kind=str(item.get('id','')).lower(); alias=str(item.get('alias') or kind); params=item.get('params') or {}; n=max(1,int(_num(params.get('period',20),20)))
        if kind in {'sma','ma','maximum','minimum','rsi','atr','momentum'}:
            if len(rows)<n: raise ValueError(f'{alias} 계산 데이터 부족: {len(rows)} < {n}')
            if kind in {'sma','ma'}: val=_sma(closes,n)
            elif kind=='maximum': val=max(highs[-n:])
            elif kind=='minimum': val=min(lows[-n:])
            elif kind=='momentum': val=(closes[-1]/closes[-n]-1)*100 if closes[-n] else 0
            elif kind=='rsi':
                diffs=[closes[i]-closes[i-1] for i in range(max(1,len(closes)-n),len(closes))]; gain=sum(max(x,0) for x in diffs)/max(1,len(diffs)); loss=sum(max(-x,0) for x in diffs)/max(1,len(diffs)); val=100 if loss==0 else 100-100/(1+gain/loss)
            else: val=sum((highs[i]-lows[i]) for i in range(len(rows)-n,len(rows)))/n
        else: raise ValueError(f'사용자 정의 YAML에서 지원하지 않는 지표: {kind}')
        values[alias]=val; indicators[alias]=val
    def group_result(group):
        conditions=(group or {}).get('conditions',[]) or []; logic=str((group or {}).get('logic','AND')).upper(); checks=[]; reasons=[]
        for c in conditions:
            left_name=str(c.get('indicator','close')); left=values.get(left_name)
            right_raw=c.get('compare_to'); right=values.get(right_raw) if isinstance(right_raw,str) and right_raw in values else _num(right_raw,float('nan'))
            op=str(c.get('operator','')).lower()
            if left is None or right!=right or op not in {'greater_than','less_than','greater_equal','less_equal','equals','cross_above','cross_below'}: raise ValueError(f'조건 해석 불가: {c}')
            if op=='greater_than': ok=left>right
            elif op=='less_than': ok=left<right
            elif op=='greater_equal': ok=left>=right
            elif op=='less_equal': ok=left<=right
            elif op=='equals': ok=left==right
            else:
                # crossover에는 이전 봉 값이 필요하므로 단일 시점 지표가 아닌 종가/MA를 재계산합니다.
                prev_left=left; prev_right=right
                if left_name=='close': prev_left=closes[-2] if len(closes)>1 else left
                if isinstance(right_raw,str) and right_raw in indicators:
                    def indicator_at(index):
                        item=next((x for x in strategy.get('indicators',[]) if (x.get('alias') or x.get('id'))==right_raw),{})
                        kind=str(item.get('id','')).lower(); n=max(1,int(_num((item.get('params') or {}).get('period',20),20)))
                        if kind in {'sma','ma'}: return _sma(closes[:index+1],n)
                        if kind=='maximum': return max(highs[max(0,index-n+1):index+1])
                        return right
                    prev_right=indicator_at(len(rows)-2) if len(rows)>1 else right
                ok=(prev_left<=prev_right and left>right) if op=='cross_above' else (prev_left>=prev_right and left<right)
            checks.append(ok); reasons.append(f'{left_name} {op} {right_raw}: {"충족" if ok else "미충족"}')
        return (bool(checks) and (any(checks) if logic=='OR' else all(checks))),reasons
    try:
        if len(rows)<1: raise ValueError('OHLCV 데이터 없음')
        entry_ok,entry_reason=group_result(strategy.get('entry') or {})
        exit_ok,exit_reason=group_result(strategy.get('exit') or {})
        action='SELL' if purpose!='entry' and exit_ok else ('BUY' if purpose!='exit' and entry_ok else 'HOLD')
        return {'symbol':symbol,'strategy_id':strategy.get('id'),'action':action,'strength':1.0 if action in {'BUY','SELL'} else 0.0,
                'reason':'; '.join(exit_reason if action=='SELL' else entry_reason) or '조건 미충족','data_ready':True,'status':'success','indicators':indicators}
    except Exception as exc:
        if '데이터 부족' in str(exc) or 'OHLCV 데이터 없음' in str(exc):
            return {'symbol':symbol,'strategy_id':strategy.get('id'),'action':'HOLD','strength':0.0,
                    'reason':f'평가 데이터 부족: {exc}','data_ready':False,'status':'insufficient_data','error':None}
        return {'symbol':symbol,'strategy_id':strategy.get('id'),'action':'ERROR','strength':0.0,'reason':str(exc),'data_ready':False,'status':'error','error':str(exc)}
