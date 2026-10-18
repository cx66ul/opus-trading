"""Tracked minute charts. Closed bars only; observation journal never backdates live signals."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.domain import chan
from backend.services.tracked_news import tracked_pool

CST = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / 'data' / 'opus-intraday.sqlite'
PERIODS = {'m1': 1, 'm5': 5, 'm30': 30, 'm60': 60}
VERSION = 'chan-observed-v1'


def wallclock():
    return datetime.now(CST)


def local(dt):
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=CST) if dt.tzinfo is None else dt.astimezone(CST)


def stamp(dt):
    return local(dt).strftime('%Y-%m-%d %H:%M:%S')


def in_session(now):
    now = local(now)
    t = now.strftime('%H:%M:%S')
    return now.weekday() < 5 and ('09:30:00' <= t <= '11:30:59' or '13:00:00' <= t <= '15:00:59')


def expected_bar(now, period):
    """Timestamp of last completed exchange bar; no synthetic lunch bars."""
    now = local(now) - timedelta(seconds=5)  # publication/close grace
    minute = now.hour * 60 + now.minute
    step = PERIODS[period]
    if minute < 570:
        return None
    if minute < 780:
        start, elapsed = 570, min(minute, 690) - 570
    else:
        start, elapsed = 780, min(minute, 900) - 780
    offset = elapsed // step * step
    if offset == 0 and (start == 780 or period != 'm1'):
        if start == 570:
            return None
        start, offset = 690, 0
    return now.replace(hour=(start + offset)//60, minute=(start + offset)%60, second=0, microsecond=0)


def normalize(rows, period, now):
    """Reject off-grid/malformed bars. Incomplete and future bars never enter analysis."""
    step = PERIODS[period]
    result = {}
    cutoff = local(now) - timedelta(seconds=5)
    for row in rows:
        ts = local(row['ts'])
        minute = ts.hour * 60 + ts.minute
        start = 570 if 570 <= minute <= 690 else 780 if 780 < minute <= 900 else None
        if start is None or ts.second or ts.weekday() >= 5 or (minute-start) % step:
            raise ValueError(f'{period} 时间网格异常: {row["ts"]}')
        if minute == 570 and period != 'm1':
            raise ValueError('非一分钟周期含集合竞价单点')
        values = [float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')]
        o,h,l,c,v = values
        if not all(math.isfinite(x) for x in values) or min(o,h,l,c) <= 0 or v < 0 or not l <= min(o,c) <= max(o,c) <= h:
            raise ValueError('分钟 OHLC/成交量异常')
        if ts <= cutoff:
            result[stamp(ts)] = dict(ts=stamp(ts), trade_date=stamp(ts), open=o, high=h, low=l, close=c, volume=v)
    return [result[k] for k in sorted(result)]


def freshness(latest, fetched, now, period, error=''):
    now = local(now)
    session = in_session(now)
    expected = expected_bar(now, period)
    current = bool(latest and local(latest).date() == now.date())
    fresh = bool(current and expected and local(latest) >= expected and fetched and
                 0 <= (now-local(fetched)).total_seconds() <= 120 and not error)
    state = 'live' if session and fresh else 'stale' if session else 'closed'
    return dict(state=state, fresh=fresh, session=session, new_signals_allowed=session and fresh,
                expected_at=stamp(expected) if expected else None)


def connect(path=CACHE):
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript('''
      CREATE TABLE IF NOT EXISTS bars(code TEXT,period TEXT,ts TEXT,payload TEXT,
        PRIMARY KEY(code,period,ts));
      CREATE TABLE IF NOT EXISTS feeds(code TEXT,period TEXT,latest TEXT,fetched TEXT,error TEXT,
        PRIMARY KEY(code,period));
      CREATE TABLE IF NOT EXISTS signals(code TEXT,period TEXT,key TEXT,payload TEXT,
        PRIMARY KEY(code,period,key));
      CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY,code TEXT,period TEXT,key TEXT,
        observed_at TEXT,payload TEXT);
    ''')
    return con


def signal_key(s):
    return f'{VERSION}|{s.kind}|{s.date}|{s.price:.6f}'


def evaluate(code, bars):
    return chan.analyze(code, bars, bars[-1]['ts']).signals if bars else []


def ingest(con, code, period, rows, now, evaluator=evaluate):
    """Replay new prefixes; freeze first observation and append all later state changes.

    Historical first availability is only relative to the finite source window.
    After an outage/bootstrap, catch-up is labelled replay, never retroactive live.
    """
    now = local(now)
    rows = normalize(rows, period, now)
    if not rows:
        raise ValueError('数据源未返回已完成分钟线')
    feed = con.execute('SELECT * FROM feeds WHERE code=? AND period=?', (code,period)).fetchone()
    old_latest = feed['latest'] if feed else None
    cached = {r['ts']: json.loads(r['payload']) for r in con.execute(
        'SELECT ts,payload FROM bars WHERE code=? AND period=?', (code,period))}
    revised = any(r['ts'] in cached and cached[r['ts']] != r for r in rows)
    # Keep a fixed analysis start rather than a rolling window that silently changes history.
    cached.update({r['ts']: r for r in rows})
    bars = [cached[k] for k in sorted(cached) if local(k) <= now-timedelta(seconds=5)]
    latest = bars[-1]['ts']
    journal = {r['key']: json.loads(r['payload']) for r in con.execute(
        'SELECT key,payload FROM signals WHERE code=? AND period=?', (code,period))}
    indices = [i for i,b in enumerate(bars) if not old_latest or b['ts'] > old_latest]
    if revised and not indices:
        indices = [len(bars)-1]
    live_ok = freshness(latest, stamp(now), now, period)['new_signals_allowed']
    continuity = bool(old_latest and (local(latest)-local(old_latest)).total_seconds() <= PERIODS[period]*60+90)
    with con:
        for i in indices:
            prefix = bars[:i+1]
            found = evaluator(code, prefix)
            keys = {signal_key(s) for s in found}
            for s in found:
                key = signal_key(s)
                if key not in journal:
                    live = bool(old_latest and continuity and live_ok and i == len(bars)-1 and not revised)
                    journal[key] = dict(kind=s.kind, level=s.level, price=s.price, structure_at=s.date,
                        available_at=prefix[-1]['ts'], observed_at=stamp(now),
                        mode='live' if live else 'replay', active=True,
                        status='candidate' if s.level == 'weak' or s.kind.startswith('weak') else 'confirmed',
                        reason=s.reason, confidence=s.confidence, version=VERSION,
                        marker_price=prefix[-1]['close'], window_start=bars[0]['ts'])
                    con.execute('INSERT INTO revisions(code,period,key,observed_at,payload) VALUES (?,?,?,?,?)',
                        (code,period,key,stamp(now),json.dumps(journal[key],ensure_ascii=False)))
            for key, signal in journal.items():
                active = key in keys
                if active != signal['active']:
                    signal['active'] = active
                    signal['changed_at'] = stamp(now)
                    con.execute('INSERT INTO revisions(code,period,key,observed_at,payload) VALUES (?,?,?,?,?)',
                        (code,period,key,stamp(now),json.dumps(signal,ensure_ascii=False)))
        con.executemany('INSERT OR REPLACE INTO bars VALUES (?,?,?,?)',
            [(code,period,r['ts'],json.dumps(r)) for r in rows])
        con.executemany('INSERT OR REPLACE INTO signals VALUES (?,?,?,?)',
            [(code,period,key,json.dumps(s,ensure_ascii=False)) for key,s in journal.items()])
        con.execute('INSERT OR REPLACE INTO feeds VALUES (?,?,?,?,?)', (code,period,latest,stamp(now),''))
    return dict(code=code, period=period, bars=len(bars), latest=latest, signals=len(journal))


class IntradayService:
    def __init__(self, main_path, cache_path=CACHE):
        self.main_path, self.cache_path = main_path, cache_path
        self.stop_event = threading.Event()
        self.thread = None
        self.error = ''
        self.heartbeat = None

    def pool(self):
        with closing(sqlite3.connect(str(self.main_path), timeout=10)) as con:
            con.row_factory = sqlite3.Row
            return tracked_pool(con)

    def start(self):
        if os.environ.get('OPUS_INTRADAY_DISABLE') == '1':
            return
        self.thread = threading.Thread(target=self.run, name='opus-intraday', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def run(self):
        from backend.data.adapters.tencent import TencentAdapter
        adapter = TencentAdapter(qps=3)
        try:
            with closing(connect(self.cache_path)) as con:
                while not self.stop_event.is_set():
                    try:
                        now = wallclock()
                        # Weekday polling is intentional: today's bar proves trading activity.
                        # Never assume an unknown/holiday calendar entry licenses live signals.
                        for stock in self.pool():
                            for period in PERIODS:
                                if self.stop_event.is_set():
                                    return
                                feed = con.execute('SELECT * FROM feeds WHERE code=? AND period=?',
                                                   (stock['code'],period)).fetchone()
                                age = (now-local(feed['fetched'])).total_seconds() if feed and feed['fetched'] else 1e9
                                if age < 30 or (not in_session(now) and age < 3600):
                                    from backend.services.intraday_image_runtime import update
                                    update(con, stock['code'], period, stamp(wallclock()), False)
                                    continue
                                try:
                                    rows = adapter.fetch_minute(stock['code'], period, 320)
                                    ingest(con, stock['code'], period, rows, wallclock())
                                    from backend.services.intraday_image_runtime import update
                                    current = wallclock()
                                    complete = normalize(rows, period, current)
                                    allowed = freshness(complete[-1]['ts'] if complete else None, stamp(current), current, period)['new_signals_allowed']
                                    update(con, stock['code'], period, stamp(current), allowed)
                                except Exception as exc:
                                    with con:
                                        con.execute('''INSERT INTO feeds VALUES (?,?,?,?,?) ON CONFLICT(code,period)
                                            DO UPDATE SET error=excluded.error,fetched=excluded.fetched''',
                                            (stock['code'],period,None,stamp(wallclock()),str(exc)[:250]))
                        self.heartbeat = stamp(wallclock())
                        self.error = ''
                    except Exception as exc:
                        self.error = str(exc)[:250]
                    if self.stop_event.wait(15):
                        break
        finally:
            adapter.close()

    def read(self, code, period, now):
        pool = {s['code']: s for s in self.pool()}
        stock = pool.get(code)
        with closing(connect(self.cache_path)) as con:
            feed = con.execute('SELECT * FROM feeds WHERE code=? AND period=?', (code,period)).fetchone()
            rows = [json.loads(r[0]) for r in con.execute(
                'SELECT payload FROM bars WHERE code=? AND period=? ORDER BY ts DESC LIMIT 960', (code,period))][::-1]
            signals = [json.loads(r[0]) for r in con.execute(
                'SELECT payload FROM signals WHERE code=? AND period=?', (code,period))]
            from backend.services.intraday_image_runtime import read as read_images, validation
            image_signals, image_policy = read_images(con, code, period)
            if image_signals is not None:
                signals = image_signals
        rows = [r for r in rows if local(r['ts']) <= local(now)-timedelta(seconds=5)]
        state = freshness(feed['latest'] if feed else None, feed['fetched'] if feed else None, now, period,
                          feed['error'] if feed else '')
        running = bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())
        state['new_signals_allowed'] &= bool(stock and running)
        return dict(code=code, name=stock['name'] if stock else '', period=period,
            trade_date=rows[-1]['ts'][:10] if rows else None, tracked=bool(stock), running=running,
            held=bool(stock and stock['held']), source='腾讯公开分钟行情',
            latest=feed['latest'] if feed else None, fetched_at=feed['fetched'] if feed else None,
            error=feed['error'] if feed else '', service_error=self.error, heartbeat=self.heartbeat,
            image_policy=dict(version=image_policy['version'], hash=image_policy['hash'],
                enabled=image_policy['enabled'] and period in ('m1','m5'),
                validation=image_policy['validation_status'],
                backtest=validation(image_policy),
                note='三图量价规则已融合；缺少当时大盘/板块证据时买点仅为候选。打板与KDJ/MA含糊语料未启用。做T最多按隔夜底仓1/3观察；未知可卖股数不提供交易股数。'),
            **state, bars=[dict(d=r['ts'],o=r['open'],h=r['high'],l=r['low'],c=r['close'],v=r['volume'],pct=None) for r in rows],
            signals=sorted(signals,key=lambda s:s['available_at']),
            note='B/S 为规则观察点，非收益预测或自动交易指令。弱信号仅为候选；确认后结构仍可能失效。卖点需自行核对 T+1、可用持仓和流动性。规则置信度不是胜率。')
