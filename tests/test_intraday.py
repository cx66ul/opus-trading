from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
import json
import math
import sqlite3

import pytest
from backend.services import intraday as m


def bar(ts, value=10):
    return dict(ts=ts,open=value,high=value+.1,low=value-.1,close=value,volume=100)


def signal(date='2026-10-12 09:31:00', kind='2buy', level='normal'):
    return SimpleNamespace(kind=kind,level=level,price=10.,date=date,reason='test',confidence=.6)


def journal(con):
    return [json.loads(r[0]) for r in con.execute('SELECT payload FROM signals')]


def test_future_and_forming_bars_are_excluded():
    rows=[bar(f'2026-10-12 09:{i}:00') for i in range(30,34)]
    assert [r['ts'] for r in m.normalize(rows,'m1','2026-10-12T09:32:02+08:00')]==[r['ts'] for r in rows[:2]]
    assert m.normalize(rows,'m1','2026-10-12T01:32:02+00:00')==m.normalize(rows,'m1','2026-10-12 09:32:02')


@pytest.mark.parametrize('row', [bar('2026-10-12 12:00:00'),bar('2026-10-12 09:31:00',float('nan')),
    dict(bar('2026-10-12 09:31:00'),high=8),dict(bar('2026-10-12 09:31:00'),volume=-1)])
def test_bad_source_rejected(row):
    with pytest.raises(ValueError):m.normalize([row],'m1','2026-10-12 15:01:00')


def test_wrong_period_rejected():
    with pytest.raises(ValueError):m.normalize([bar('2026-10-12 09:31:00')],'m5','2026-10-12 15:01:00')


@pytest.mark.parametrize('now,expected', [('09:30:30',None),('10:02:10','10:00:00'),
    ('12:00:00','11:30:00'),('13:02:00','11:30:00'),('13:30:10','13:30:00')])
def test_session_grid(now,expected):
    actual=m.expected_bar('2026-10-12 '+now,'m30')
    assert (m.stamp(actual)[11:] if actual else None)==expected


def test_stale_source_worker_failure_and_weekend():
    now='2026-10-12 10:03:20'
    assert m.freshness('2026-10-12 10:03:00',now,now,'m1')['new_signals_allowed']
    assert not m.freshness('2026-10-12 10:01:00',now,now,'m1')['new_signals_allowed']
    assert not m.freshness('2026-10-12 10:03:00','2026-10-12 10:00:00',now,'m1')['new_signals_allowed']
    assert not m.freshness('2026-10-12 10:03:00',now,now,'m1','HTTP error')['new_signals_allowed']
    assert m.freshness('2026-10-09 15:00:00',now,'2026-10-11 10:00:00','m1')['state']=='closed'


def test_first_availability_not_pivot_and_no_bootstrap_live(tmp_path):
    rows=[bar(f'2026-10-12 09:{i}:00') for i in range(30,36)]
    seen=[]
    def evaluator(code,prefix):
        seen.append(prefix[-1]['ts'])
        return [signal()] if len(prefix)>=4 else []
    with closing(m.connect(tmp_path/'cache.sqlite')) as con:
        m.ingest(con,'600152','m1',rows,'2026-10-12 09:36:10',evaluator)
        s=journal(con)[0]
        assert s['available_at']=='2026-10-12 09:33:00' and s['structure_at']=='2026-10-12 09:31:00'
        assert s['mode']=='replay' and s['observed_at']=='2026-10-12 09:36:10'
        assert seen==[r['ts'] for r in rows]


def test_live_confirmation_invalidation_restart_and_reactivation(tmp_path):
    path=tmp_path/'cache.sqlite'
    rows=[bar(f'2026-10-12 09:{i}:00') for i in range(30,35)]
    with closing(m.connect(path)) as con:
        m.ingest(con,'600152','m1',rows[:2],'2026-10-12 09:31:10',lambda c,p:[])
        m.ingest(con,'600152','m1',rows[:3],'2026-10-12 09:32:10',lambda c,p:[signal()])
        first=journal(con)[0].copy()
        assert first['mode']=='live' and first['status']=='confirmed'
        m.ingest(con,'600152','m1',rows[:4],'2026-10-12 09:33:10',lambda c,p:[])
        assert not journal(con)[0]['active']
    with closing(m.connect(path)) as con:
        m.ingest(con,'600152','m1',rows,'2026-10-12 09:34:10',lambda c,p:[signal()])
        s=journal(con)[0]
        assert s['active'] and s['observed_at']==first['observed_at'] and s['available_at']==first['available_at']
        assert con.execute('SELECT count(*) FROM revisions').fetchone()[0]==3


def test_candidate_and_outage_catchup_never_live(tmp_path):
    rows=[bar(f'2026-10-12 09:{i}:00') for i in range(30,45)]
    with closing(m.connect(tmp_path/'cache.sqlite')) as con:
        m.ingest(con,'600152','m1',rows[:2],'2026-10-12 09:31:10',lambda c,p:[])
        m.ingest(con,'600152','m1',rows,'2026-10-12 09:44:10',lambda c,p:[signal(kind='weak3buy',level='weak')])
        assert journal(con)[0]['mode']=='replay' and journal(con)[0]['status']=='candidate'


def test_real_engine_causal_prefix_replay(tmp_path):
    start=datetime(2026,10,12,9,30)
    rows=[bar(m.stamp(start+timedelta(minutes=i)),10+math.sin(i*.37)+.3*math.sin(i*.71)) for i in range(120)]
    with closing(m.connect(tmp_path/'cache.sqlite')) as con:
        m.ingest(con,'600152','m1',rows,'2026-10-12 11:30:10')
        found=journal(con)
        assert found, 'synthetic series must exercise the actual Chan engine'
        for s in found:
            prefix=[dict(r,trade_date=r['ts']) for r in rows if r['ts']<=s['available_at']]
            keys={m.signal_key(x) for x in m.evaluate('600152',prefix)}
            key=f"{m.VERSION}|{s['kind']}|{s['structure_at']}|{s['price']:.6f}"
            assert key in keys
            assert key not in {m.signal_key(x) for x in m.evaluate('600152',prefix[:-1])}


def test_pool_null_holdings_and_removal(tmp_path):
    main=tmp_path/'main.sqlite'
    with closing(sqlite3.connect(main)) as con:
        con.executescript('''CREATE TABLE stock(id TEXT,name TEXT,industry TEXT,concept TEXT);
          CREATE TABLE position(code TEXT,status TEXT,shares INTEGER); CREATE TABLE watchlist(code TEXT);
          INSERT INTO stock VALUES ('600152','维科技术',NULL,NULL),('603232','格尔软件',NULL,NULL);
          INSERT INTO position VALUES ('600152','holding',NULL); INSERT INTO watchlist VALUES ('603232');''')
    service=m.IntradayService(main,tmp_path/'cache.sqlite')
    assert {s['code'] for s in service.pool()}=={'600152','603232'}
    with closing(sqlite3.connect(main)) as con:
        con.execute('DELETE FROM watchlist');con.commit()
    result=service.read('603232','m1',m.local('2026-10-12 10:00:00'))
    assert not result['tracked'] and not result['new_signals_allowed']


def test_api_validation_and_no_daily_date_dependency(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.api import webapi
    fake=SimpleNamespace(read=lambda code,period,now:dict(code=code,period=period))
    monkeypatch.setattr(webapi.app.state,'intraday',fake,raising=False)
    client=TestClient(webapi.app)
    assert client.get('/api/intraday/600152?period=m5').json()==dict(code='600152',period='m5')
    assert client.get('/api/intraday/600152?period=m2').status_code==422
    assert client.get('/api/intraday/bad').status_code==422
