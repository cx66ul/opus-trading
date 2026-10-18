"""Causal interpretation of three user screenshots; thresholds are disclosed, not fitted.

Returns observations, never orders. Unknown environment cannot confirm a buy.
Uses only completed bars supplied by the caller, with an explicit as_of cutoff.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

from backend.domain import chan

POLICY_PATH = Path(__file__).resolve().parents[2]/'config/intraday_image_strategy.json'
CST = timezone(timedelta(hours=8))


def load_policy():
    raw = POLICY_PATH.read_bytes()
    p = json.loads(raw)
    assert 0 < p['shrink_ratio'] < 1 < p['expand_ratio']
    assert 0 < p['max_t_fraction'] <= .5
    p['hash'] = hashlib.sha256(raw).hexdigest()
    return p


def timestamp(value):
    dt = datetime.fromisoformat(str(value))
    return (dt.replace(tzinfo=CST) if dt.tzinfo is None else dt.astimezone(CST)).strftime('%Y-%m-%d %H:%M:%S')


def group_bars(rows, source_minutes, target_minutes):
    """Only complete, same-session groups. Never use an unfinished 15-minute candle."""
    groups = {}
    for b in rows:
        dt = datetime.fromisoformat(b['ts'])
        minute = dt.hour*60+dt.minute
        start = 570 if 570 < minute <= 690 else 780 if 780 < minute <= 900 else None
        if start is None:
            continue  # 09:30 auction point is not a continuous 1-minute candle
        endpoint = start + ((minute-start-1)//target_minutes+1)*target_minutes
        key = dt.replace(hour=endpoint//60,minute=endpoint%60,second=0).strftime('%Y-%m-%d %H:%M:%S')
        groups.setdefault(key, []).append(b)
    result = []
    for key, group in sorted(groups.items()):
        end = datetime.fromisoformat(key)
        expected = [(end-timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S')
                    for i in range(target_minutes-source_minutes,-1,-source_minutes)]
        if [b['ts'] for b in group] != expected:
            continue
        result.append(dict(ts=key,trade_date=key,open=group[0]['open'],high=max(b['high'] for b in group),
            low=min(b['low'] for b in group),close=group[-1]['close'],volume=sum(b['volume'] for b in group)))
    return result


def open_guard(rows, step):
    if not rows:
        return dict(known=False,blocked=False,held=False)
    day = rows[-1]['ts'][:10]
    today = [b for b in rows if b['ts'][:10] == day]
    first = next((b for b in today if b['ts'][11:16] == ('09:31' if step==1 else '09:35')), None)
    auction = next((b for b in today if b['ts'][11:16]=='09:30'), None)
    opening = auction['open'] if auction else first['open'] if first else None
    early = [b for b in today if '09:30' < b['ts'][11:16] <= '09:40']
    wanted = [f'09:{x:02}' for x in range(30+step,41,step)]
    known = bool(opening and [b['ts'][11:16] for b in early] == wanted)
    below = known and min(b['low'] for b in early) < opening
    return dict(known=known,blocked=bool(below and early[-1]['close']<opening),
                held=bool(known and not below),opening=opening)


def environment_gate(context, at, p, sector=False):
    reasons = []
    context = context or {}
    for key in ('ts','observed_at'):
        try:
            age=(datetime.fromisoformat(at)-datetime.fromisoformat(timestamp(context[key]))).total_seconds()
            if not 0 <= age <= p['market_max_age_seconds']:
                reasons.append('市场资料过期或晚于信号')
        except (KeyError,TypeError,ValueError):
            reasons.append('缺少可追溯的盘中市场资料')
    if context.get('market_good') is not True:
        reasons.append('大盘良好条件未证实')
    count=context.get('limit_down_count')
    if type(count) is not int or count > p['limit_down_max'] or count < 0:
        reasons.append('跌停家数≤10条件未证实')
    if sector and context.get('sector_strong') is not True:
        reasons.append('板块强势条件未证实')
    return list(dict.fromkeys(reasons))


def trend(rows, up=True):
    if len(rows)<6:
        return False
    a,b=mean(r['close'] for r in rows[-5:]),mean(r['close'] for r in rows[-6:-1])
    return rows[-1]['close']>a>b if up else rows[-1]['close']<a<b


def patterns(rows, step, p):
    """Point-in-time triggers. No assumed knowledge of a later high/low."""
    if len(rows)<6:
        return []
    at=rows[-1]['ts']; day=at[:10]; cur,prev=rows[-1],rows[-2]
    today=[b for b in rows if b['ts'][:10]==day]
    five=group_bars(rows,step,5) if step==1 else rows
    fifteen=group_bars(rows,step,15)
    guard=open_guard(rows,step)
    found=[]
    def emit(rule,side,reason,sector=False):
        found.append(dict(rule_id=rule,kind=f'{rule}_{side}',reason=reason,sector=sector,
                          structure_at=at,price=cur['close']))
    red=sum(b['volume'] for b in today if b['close']>b['open'])
    green=sum(b['volume'] for b in today if b['close']<b['open'])
    if (guard['held'] and '09:40'<=at[11:16]<='10:00' and red>0 and red>green*p['red_green_ratio']
            and cur['close']>cur['open'] and prev['volume']>0 and cur['volume']>=prev['volume']*p['expand_ratio']):
        emit('T01','buy','开盘前10分钟守住开盘价；红量占优，当前阳线放量')
    if len(five)>=7 and five[-1]['ts']==at:
        f,fp=five[-1],five[-2]
        shrinking=f['close']<f['open'] and fp['volume']>0 and f['volume']<=fp['volume']*p['shrink_ratio']
        first=not (fp['close']<fp['open'] and fp['volume']<=five[-3]['volume']*p['shrink_ratio'])
        if trend(five[:-1]) and shrinking and first and len(fifteen)>=2:
            v1,v2=fifteen[-1],fifteen[-2]
            if v1['volume']<=v2['volume'] and f['high']<=max(b['high'] for b in five[-6:-1]):
                emit('T03','sell','5分钟上升趋势首根缩量阴线，15分钟量未扩张且未突破前高')
        if trend(five[:-1],False) and f['close']>f['open'] and fp['close']<=fp['open']:
            emit('T04R','buy','5分钟下降段第一根阳线：仅接回候选，需大盘与板块共同确认',True)
        for seq in (five,fifteen):
            if len(seq)<8 or seq[-1]['ts']!=at:continue
            a,b,c=seq[-3:]
            if (trend(seq[:-3]) and a['close']<a['open'] and b['close']<b['open']
                and a['volume']<=seq[-4]['volume']*p['shrink_ratio'] and b['volume']<=a['volume']*p['shrink_ratio']
                and c['close']>c['open']):
                emit('T04S','buy','趋势内连续两根缩量阴线后出现阳线确认；需大盘与板块强势',True)
                break
    if (step==1 and cur['close']<cur['open'] and mean(b['volume'] for b in rows[-6:-1])>0
        and cur['volume']>=mean(b['volume'] for b in rows[-6:-1])*p['expand_ratio']
        and len(five)>=2 and five[-1]['close']<five[-2]['close']):
        emit('T05','sell','1分钟放量阴线，最近已完成5分钟同步走弱')
    if len(rows)>=35:
        _,_,hist=chan.macd_series([b['close'] for b in rows])
        if hist[-1]<hist[-2]<hist[-3] and cur['close']<prev['close']<rows[-3]['close']:
            emit('T06','sell','MACD柱连续下降且价格连续下拐，动能减弱观察')
    if at[11:16]>=p['late_exit_start'] and cur['close']<cur['open'] and cur['close']<prev['close']:
        emit('T07','sell','14:30后完成K线收阴且价格下拐；不预知尾盘最高点')
    return found


def stream(code, rows, step, as_of, p, context_at=None, price_only=False):
    """Same stream for UI and backtest. Fixed prefix analysis, chronological first hits.

    price_only is an explicitly named experimental ablation, never the live default.
    """
    cutoff=timestamp(as_of)
    rows=sorted([dict(r,trade_date=r['ts']) for r in rows if r['ts']<=cutoff],key=lambda b:b['ts'])
    seen=set(); previous=set(); out=[]
    for i,b in enumerate(rows):
        prefix=rows[:i+1];at=b['ts'];guard=open_guard(prefix,step)
        base=[]
        for s in chan.analyze(code,prefix,at).signals:
            key=(s.kind,s.date,s.price)
            if key in seen:continue
            seen.add(key)
            base.append(dict(rule_id='CHAN',kind=s.kind,structure_at=s.date,price=s.price,
                reason=s.reason,sector=False,weak=s.level=='weak' or s.kind.startswith('weak'),confidence=s.confidence))
        matched=patterns(prefix,step,p) if p['enabled'] else []
        # Only emit on the false->true edge; repeated weakening bars aren't new alerts.
        active={s['rule_id'] for s in matched}
        matched=[s for s in matched if s['rule_id'] not in previous]
        previous=active
        for s in base+matched:
            buy='buy' in s['kind'];reasons=[]
            if buy and p['enabled']:
                if not guard['known']:reasons.append('前10分钟数据未完成或有缺口')
                if guard['blocked']:reasons.append('开盘10分钟未收回开盘价：全天不加仓')
                if not price_only:reasons+=environment_gate(context_at(at) if context_at else None,at,p,s['sector'])
            candidate=s.get('weak',False) or bool(reasons)
            s.setdefault('confidence', None)
            out.append(dict(**s,available_at=at,marker_price=b['close'],status='candidate' if candidate else 'confirmed',
                active=not guard['blocked'] if buy and p['enabled'] else True,
                gate_reasons=reasons,reason_detail='；'.join(reasons),
                version=p['version'],policy_hash=p['hash'],max_t_fraction=p['max_t_fraction'],
                interpretation='price_only_ablation' if price_only else 'full',window_start=rows[0]['ts']))
    return out
