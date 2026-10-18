from copy import deepcopy
from datetime import datetime,timedelta
from contextlib import closing
import json

import pytest
from backend.domain import intraday_images as r
from backend.domain.intraday_t_backtest import simulate


def bars(day='2026-10-09',step=1):
    out=[]
    for start,end in ((570,690),(780,900)):
        for n in range(start+step,end+1,step):
            ts=f'{day} {n//60:02}:{n%60:02}:00'
            out.append(dict(ts=ts,trade_date=ts,open=10.,close=10.02,low=10.,high=10.05,volume=1000000.))
    return out


def test_closed_groups_do_not_leak_partial_or_lunch():
    data=bars()
    assert r.group_bars(data[:14],1,15)==[]
    fifteen=r.group_bars(data[:15],1,15)
    assert len(fifteen)==1 and fifteen[0]['ts'].endswith('09:45:00')
    assert len(r.group_bars(data,1,15))==16
    assert len(r.group_bars(data[:120]+data[121:],1,15))==15


def test_open_lock_does_not_look_at_later_recovery():
    data=bars();data[2]['low']=9.8;data[9]['close']=9.9
    assert not r.open_guard(data[:9],1)['known']
    assert r.open_guard(data[:10],1)['blocked']
    assert r.open_guard(data,1)['blocked']
    assert not r.open_guard(data[:4]+data[5:],1)['known']


def test_market_unknown_future_and_stale_never_pass():
    p=r.load_policy();at='2026-10-09 10:00:00'
    context=dict(ts=at,observed_at=at,market_good=True,limit_down_count=3,sector_strong=True)
    assert not r.environment_gate(context,at,p,True)
    for bad in ({},{**context,'ts':'2026-10-09 10:01:00'},
                {**context,'observed_at':'2026-10-09 10:01:00'},
                {**context,'ts':'2026-10-09 09:50:00'},
                {**context,'limit_down_count':11},{**context,'sector_strong':None}):
        assert r.environment_gate(bad,at,p,True)


def test_future_bars_cannot_change_observations_and_gate_is_real():
    data=bars();data[9]['volume']=2000000
    p=r.load_policy()
    partial=r.stream('600152',data[:10],1,data[9]['ts'],p)
    full=r.stream('600152',data,1,data[9]['ts'],p)
    assert partial==full
    buy=[s for s in full if s['rule_id']=='T01']
    assert buy and buy[0]['status']=='candidate' and buy[0]['gate_reasons']
    ablation=r.stream('600152',data[:10],1,data[9]['ts'],p,price_only=True)
    assert [s for s in ablation if s['rule_id']=='T01'][0]['status']=='confirmed'
    assert all(s['interpretation']=='price_only_ablation' for s in ablation)


def test_genuine_known_context_can_confirm():
    data=bars();data[9]['volume']=2000000;p=r.load_policy()
    def ctx(at):return dict(ts=at,observed_at=at,market_good=True,limit_down_count=1,sector_strong=True)
    result=r.stream('600152',data[:10],1,data[9]['ts'],p,ctx)
    assert [s for s in result if s['rule_id']=='T01'][0]['status']=='confirmed'


def test_five_minute_cannot_emit_one_minute_rule():
    data=bars(step=5)
    data[-1].update(open=11,close=10,volume=100000000)
    assert 'T05' not in [s['rule_id'] for s in r.patterns(data,5,r.load_policy())]


def event(b,side):
    return dict(available_at=b['ts'],kind=side,active=True,status='confirmed',rule_id='fixture')


def test_backtest_latency_t1_costs_and_unclosed_marking():
    data=bars('2026-10-08',5)+bars('2026-10-09',5)
    for b in data:b['low']=9.9
    # Several sell/buy cycles exhaust the original overnight sellable inventory.
    events=[event(data[i], 'sell' if j%2==0 else 'buy') for j,i in enumerate(range(48,84,4))]
    result=simulate('600152',data,events,.001)
    assert result['fills'] and result['fees']>0
    indices={b['ts']:i for i,b in enumerate(data)}
    for fill in result['fills']:
        assert indices[fill['filled_at']]==indices[fill['signal_at']]+2
        assert fill['fee']>=5
        if fill['side']=='buy':assert fill['available_after']==fill['available_before']
        else:assert fill['available_after']==fill['available_before']-fill['shares']
    assert result['skips'].get('T1_unavailable_shares',0)>0
    assert result['win_rate_pct'] is None
    final=result['equity'][-1]
    assert result['net_return_pct']==pytest.approx((final['equity']/100000-1)*100)


def test_flat_limit_and_zero_volume_cannot_fill():
    data=bars('2026-10-08',5)+bars('2026-10-09',5)
    for field in ('flat','limit','zero'):
        copy=deepcopy(data);b=copy[50]
        if field=='flat':b.update(open=10,close=10,high=10,low=10)
        elif field=='limit':b.update(open=11.03,high=11.04,low=11.02,close=11.03)
        else:b['volume']=0
        result=simulate('600152',copy,[event(copy[48],'buy')],.001)
        assert not result['fills'] and result['skips']['unverified_limit_or_flat_bar_fill']==1


def test_candidates_and_blocked_never_trade():
    data=bars('2026-10-08',5)+bars('2026-10-09',5)
    for changes in ({'status':'candidate'},{'active':False}):
        ev={**event(data[48],'buy'),**changes}
        assert not simulate('600152',data,[ev])['fills']


def test_runtime_separate_version_and_preserves_first_observation(tmp_path,monkeypatch):
    from backend.services.intraday import connect,ingest
    from backend.services import intraday_image_runtime as runtime
    monkeypatch.setattr(runtime,'capture_context',lambda *args:None)
    data=bars()[:12];data[9]['volume']=2000000
    with closing(connect(tmp_path/'cache.sqlite')) as con:
        ingest(con,'600152','m1',data,'2026-10-09 09:43:10')
        runtime.update(con,'600152','m1','2026-10-11 23:00:00',False)
        first,p=runtime.read(con,'600152','m1')
        assert first and all(s['mode']=='replay' for s in first)
        runtime.update(con,'600152','m1','2026-10-11 23:10:00',False)
        assert runtime.read(con,'600152','m1')[0]==first
        assert con.execute('SELECT count(*) FROM bars').fetchone()[0]==12
        # Same latest timestamp but revised source must not silently keep a now-impossible trigger.
        data[9]['volume']=1000000
        ingest(con,'600152','m1',data,'2026-10-09 09:43:10')
        runtime.update(con,'600152','m1','2026-10-11 23:20:00',False)
        corrected=runtime.read(con,'600152','m1')[0]
        old_t01=next(e for e in first if e['rule_id']=='T01')
        new_t01=next(e for e in corrected if e['rule_id']=='T01')
        assert new_t01['source_revised'] and not new_t01['active']
        assert new_t01['observed_at']==old_t01['observed_at']
